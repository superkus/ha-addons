"""Generic IPMI SDR sensor plugin implemented through one bounded ``ipmitool`` process.

Both public operations call :meth:`IpmiPlugin._read_once`; therefore validation and a
normal read have identical command, timeout, parsing, and classification behaviour.
The password leaves ``SecretStr`` only while constructing the child environment.  It
never appears in argv, a result representation, a failure detail, or a log record.
"""

from __future__ import annotations

import asyncio
import math
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar, Final, Protocol

from pydantic import BaseModel

from remote_energy_meter.plugins.interface import (
    FailureClassification,
    PowerReading,
    ReadFailure,
    ReadResult,
    ValidationResult,
)
from remote_energy_meter.plugins.ipmi.parser import parse_sensor_output
from remote_energy_meter.plugins.ipmi.schema import MAX_REQUEST_TIMEOUT_S, IpmiParams

__all__ = [
    "DEFAULT_EXECUTABLE",
    "IPMI_PASSWORD_ENV",
    "PLUGIN_ID",
    "AsyncioCommandRunner",
    "CommandLaunchError",
    "CommandResult",
    "CommandRunner",
    "CommandTimeoutError",
    "ExecutableUnavailableError",
    "IpmiPlugin",
    "classify_ipmitool_failure",
]

PLUGIN_ID: Final = "ipmi"
DEFAULT_EXECUTABLE: Final = "ipmitool"
IPMI_PASSWORD_ENV: Final = "IPMI_PASSWORD"
IPMITOOL_RETRY_COUNT: Final = 1
_TERMINATION_GRACE_S: Final = 0.25

_AUTHENTICATION_OUTPUT: Final = re.compile(
    r"(?:unauthori[sz]ed\s+name|invalid\s+user(?:\s*name)?|password\s+verification\s+failed|"
    r"authentication\s+(?:failed|type)|rakp\s+[24]\s+message|"
    r"unable\s+to\s+establish\s+ipmi\s+v2\s*/\s*rmcp\+\s+session)",
    re.IGNORECASE,
)
_SENSOR_MISSING_OUTPUT: Final = re.compile(
    r"(?:sensor\s+data\s+record\s+not\s+found|sensor\s+not\s+found|unable\s+to\s+find\s+sensor)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class CommandResult:
    """A completed child process, with payloads deliberately omitted from ``repr``."""

    returncode: int
    stdout: bytes = field(repr=False)
    stderr: bytes = field(repr=False)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(returncode={self.returncode}, "
            f"stdout_bytes={len(self.stdout)}, stderr_bytes={len(self.stderr)})"
        )


class CommandTimeoutError(Exception):
    """The child did not finish before its operation bound."""


class ExecutableUnavailableError(Exception):
    """The configured executable could not be found."""


class CommandLaunchError(Exception):
    """The child process could not be started for another operating-system reason."""


class CommandRunner(Protocol):
    """Injectable boundary between the plugin and child-process execution."""

    async def run(
        self,
        argv: tuple[str, ...],
        env: Mapping[str, str],
        timeout_s: float,
    ) -> CommandResult:
        """Run exactly one command and return its captured result."""
        ...


class _ChildProcess(Protocol):
    @property
    def returncode(self) -> int | None: ...

    async def communicate(self) -> tuple[bytes, bytes]: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


type ProcessFactory = Callable[[tuple[str, ...], Mapping[str, str]], Awaitable[_ChildProcess]]


async def _create_subprocess(argv: tuple[str, ...], env: Mapping[str, str]) -> _ChildProcess:
    return await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=dict(env),
    )


def _executable_name(argv: tuple[str, ...]) -> str:
    if not argv:
        return DEFAULT_EXECUTABLE
    return Path(argv[0]).name or DEFAULT_EXECUTABLE


class AsyncioCommandRunner:
    """``create_subprocess_exec`` runner with bounded terminate/kill/reap cleanup."""

    __slots__ = ("_process_factory", "_termination_grace_s")

    def __init__(
        self,
        *,
        process_factory: ProcessFactory | None = None,
        termination_grace_s: float = _TERMINATION_GRACE_S,
    ) -> None:
        self._process_factory = process_factory if process_factory is not None else _create_subprocess
        self._termination_grace_s = termination_grace_s

    async def run(
        self,
        argv: tuple[str, ...],
        env: Mapping[str, str],
        timeout_s: float,
    ) -> CommandResult:
        executable = _executable_name(argv)
        try:
            process = await self._process_factory(argv, env)
        except FileNotFoundError:
            raise ExecutableUnavailableError(f"{executable} executable is unavailable") from None
        except OSError:
            raise CommandLaunchError(f"{executable} executable could not be started") from None

        try:
            async with asyncio.timeout(timeout_s):
                stdout, stderr = await process.communicate()
        except TimeoutError:
            await self._terminate_kill_reap(process)
            raise CommandTimeoutError(f"{executable} exceeded its operation timeout") from None
        except asyncio.CancelledError:
            await self._terminate_kill_reap(process)
            raise

        returncode = process.returncode
        if returncode is None:
            raise RuntimeError("the command runner returned before the child process exited")
        return CommandResult(returncode=returncode, stdout=stdout, stderr=stderr)

    async def _terminate_kill_reap(self, process: _ChildProcess) -> None:
        """Stop and reap a child, escalating from terminate to kill after a short grace."""
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.terminate()
        try:
            async with asyncio.timeout(self._termination_grace_s):
                await process.communicate()
                return
        except TimeoutError:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
        # ``communicate`` waits for process exit and drains both pipes, which reaps the
        # killed child and prevents a zombie or a pipe writer surviving cancellation.
        await process.communicate()


def _default_command_runner() -> CommandRunner:
    return AsyncioCommandRunner()


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _decode(payload: bytes) -> str:
    return payload.decode("utf-8", errors="replace")


def classify_ipmitool_failure(output: str) -> FailureClassification:
    """Classify a failed command from its text without returning or logging that text."""
    if _AUTHENTICATION_OUTPUT.search(output) is not None:
        return FailureClassification.AUTHENTICATION
    if _SENSOR_MISSING_OUTPUT.search(output) is not None:
        return FailureClassification.RESPONSE_FORMAT
    return FailureClassification.COMMUNICATION


class IpmiPlugin:
    """Read one named SDR sensor from one IPMI host."""

    plugin_id: ClassVar[str] = PLUGIN_ID
    config_model: ClassVar[type[BaseModel]] = IpmiParams

    __slots__ = ("_clock", "_device_id", "_executable", "_params", "_runner")

    def __init__(
        self,
        device_id: str,
        params: BaseModel,
        *,
        command_runner: CommandRunner | None = None,
        executable: str = DEFAULT_EXECUTABLE,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(params, IpmiParams):
            raise TypeError(
                f"{type(self).__name__} requires {IpmiParams.__name__} parameters, got {type(params).__name__}"
            )
        self._device_id = device_id
        self._params = params
        self._runner = command_runner if command_runner is not None else _default_command_runner()
        self._executable = executable
        self._clock = clock if clock is not None else _utc_now

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(device_id={self._device_id!r}, host={self._params.host!r}, "
            f"executable={_executable_name((self._executable,))!r})"
        )

    async def read_power(self, timeout_s: float) -> ReadResult:
        return await self._read_once(timeout_s)

    async def validate_connection(self, timeout_s: float) -> ValidationResult:
        outcome = await self._read_once(timeout_s)
        if isinstance(outcome, ReadFailure):
            return ValidationResult.failure(outcome.classification)
        return ValidationResult.success()

    def _effective_timeout(self, timeout_s: float) -> float:
        if math.isfinite(timeout_s) and timeout_s > 0:
            return min(float(timeout_s), MAX_REQUEST_TIMEOUT_S)
        return self._params.request_timeout_s

    @staticmethod
    def _network_timeout(timeout_s: float) -> int:
        return max(1, min(math.ceil(timeout_s), int(MAX_REQUEST_TIMEOUT_S)))

    def _argv(self, timeout_s: float) -> tuple[str, ...]:
        return (
            self._executable,
            "-I",
            self._params.interface,
            "-H",
            self._params.host,
            "-U",
            self._params.username.get_secret_value(),
            "-E",
            "-C",
            str(self._params.cipher_suite),
            "-N",
            str(self._network_timeout(timeout_s)),
            "-R",
            str(IPMITOOL_RETRY_COUNT),
            "sdr",
            "get",
            self._params.sensor_name,
        )

    def _environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment[IPMI_PASSWORD_ENV] = self._params.password.get_secret_value()
        return environment

    def _failure(self, classification: FailureClassification, detail: str) -> ReadFailure:
        return ReadFailure(device_id=self._device_id, classification=classification, detail=detail)

    async def _read_once(self, timeout_s: float) -> ReadResult:
        """One read-only command, shared by both interface operations."""
        effective_timeout = self._effective_timeout(timeout_s)
        try:
            result = await self._runner.run(
                self._argv(effective_timeout),
                self._environment(),
                effective_timeout,
            )
        except ExecutableUnavailableError:
            return self._failure(
                FailureClassification.COMMUNICATION,
                f"{_executable_name((self._executable,))} executable is unavailable",
            )
        except CommandLaunchError:
            return self._failure(
                FailureClassification.COMMUNICATION,
                f"{_executable_name((self._executable,))} executable could not be started",
            )
        except CommandTimeoutError:
            return self._failure(
                FailureClassification.TIMEOUT,
                f"{_executable_name((self._executable,))} did not complete within {effective_timeout:g}s",
            )
        except FileNotFoundError:
            # An injected runner may surface the operating-system error directly.
            return self._failure(
                FailureClassification.COMMUNICATION,
                f"{_executable_name((self._executable,))} executable is unavailable",
            )
        except OSError:
            return self._failure(
                FailureClassification.COMMUNICATION,
                f"{_executable_name((self._executable,))} executable could not be run",
            )

        received_at = _as_utc(self._clock())
        if result.returncode != 0:
            classification = classify_ipmitool_failure(f"{_decode(result.stdout)}\n{_decode(result.stderr)}")
            if classification is FailureClassification.AUTHENTICATION:
                detail = "the IPMI session or credentials were rejected"
            elif classification is FailureClassification.RESPONSE_FORMAT:
                detail = "the configured IPMI sensor was not found"
            else:
                detail = f"ipmitool could not read the configured host (exit status {result.returncode})"
            return self._failure(classification, detail)

        watts = parse_sensor_output(_decode(result.stdout), device_id=self._device_id)
        if isinstance(watts, ReadFailure):
            return watts
        return PowerReading(device_id=self._device_id, watts=watts, measured_at=received_at)
