"""Generic IPMI SDR sensor plugin implemented through bounded ``ipmitool`` processes.

Both public operations call :meth:`IpmiPlugin._read_once`; therefore validation and a
normal read have identical command, timeout, parsing, and classification behaviour.
The password leaves ``SecretStr`` only while constructing the child environment.  It
never appears in argv, a result representation, a failure detail, or a log record.

**Local SDR cache.** ``ipmitool sdr get NAME`` without ``-S`` walks the controller's whole
Sensor Data Repository to find the named sensor. On a legacy iDRAC 6 that is dozens of
request/response pairs and was measured at 6-37 s per read, routinely exceeding the
request timeout. Every timeout killed ``ipmitool`` mid-session, so the controller kept
the orphaned RMCP+ session until its own idle timeout; with only five session slots the
controller then refused new sessions. The plugin therefore dumps the repository once
(``sdr dump``) into a persistent per-device cache file, outside the per-read budget, and
reads with ``-S CACHE`` -- one sensor reading request, measured at 0.3-0.5 s.

**Short retransmission timer.** ``-N`` is the per-packet retransmission interval, not an
operation bound. A value equal to the whole request timeout meant a single lost UDP
datagram consumed the entire attempt, so a short interval with a few retransmissions is
used instead; the operation as a whole stays bounded by the request timeout.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import re
import tempfile
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
    "SDR_CACHE_DIR_ENV",
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
#: ``ipmitool -R``: retransmissions per request after the first send.
IPMITOOL_RETRY_COUNT: Final = 2
#: ``ipmitool -N`` upper bound: seconds between retransmissions of one request.
MAX_PACKET_TIMEOUT_S: Final = 2
#: The cache dump is many requests; it runs in the background with its own generous bound.
SDR_CACHE_BUILD_TIMEOUT_S: Final = 180.0
#: After a failed cache build, wait this long before contacting the controller for another one.
SDR_CACHE_RETRY_AFTER_S: Final = 60.0
#: Overrides the cache directory; otherwise ``/data/ipmi-sdr-cache`` in the add-on, a temp dir elsewhere.
SDR_CACHE_DIR_ENV: Final = "REMOTE_ENERGY_METER_SDR_CACHE_DIR"
_ADDON_DATA_DIR: Final = Path("/data")
#: Used only when the parent has no PATH of its own; the add-on image always sets one.
_DEFAULT_CHILD_PATH: Final = "/usr/sbin:/usr/bin:/sbin:/bin"
#: The plugin ends its own child this long before the caller's deadline, so a slow read is
#: classified and reaped here rather than cancelled from outside.
_DEADLINE_MARGIN_S: Final = 0.5
#: Upper bound on the share of an attempt held back for the cached read while a build is awaited.
_CACHED_READ_RESERVE_S: Final = 1.0
_TERMINATION_GRACE_S: Final = 0.25

# ``ipmitool`` prints the same "Unable to establish IPMI v2 / RMCP+ session" for a refused
# credential, an unreachable controller, and a controller out of session slots, so that line
# alone is *not* evidence of a credential problem. ``-v`` adds the RAKP detail that is.
_AUTHENTICATION_OUTPUT: Final = re.compile(
    r"(?:unauthori[sz]ed\s+name|invalid\s+user(?:\s*name)?|password\s+verification\s+failed|"
    r"authentication\s+(?:failed|type)|rakp\s+[24]\s+hmac\s+is\s+invalid|"
    r"invalid\s+integrity\s+check\s+value|unauthori[sz]ed\s+role\s+or\s+privilege)",
    re.IGNORECASE,
)
_SESSION_EXHAUSTED_OUTPUT: Final = re.compile(
    r"(?:insufficient\s+resources\s+for\s+session|no\s+session\s+slot\s+available|"
    r"session\s+limit\s+reached)",
    re.IGNORECASE,
)
_SESSION_OUTPUT: Final = re.compile(r"unable\s+to\s+establish\s+ipmi\s+v[12]", re.IGNORECASE)
_SENSOR_MISSING_OUTPUT: Final = re.compile(
    r"(?:sensor\s+data\s+record\s+not\s+found|sensor\s+not\s+found|unable\s+to\s+find\s+sensor)",
    re.IGNORECASE,
)
_CACHE_UNUSABLE_OUTPUT: Final = re.compile(
    r"(?:invalid\s+sdr\s+header|unable\s+to\s+open\s+sdr\s+cache|does\s+not\s+exist)",
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
    """Classify a failed command from its text without returning or logging that text.

    Only RAKP-level detail (printed under ``-v``) counts as a credential rejection. The bare
    "Unable to establish IPMI v2 / RMCP+ session" line is also what an unreachable
    controller and a controller with every session slot taken produce, so it classifies
    as ``communication``; calling those ``authentication`` sends an administrator to check a
    password that is fine and suppresses the retry that would have succeeded.
    """
    if _AUTHENTICATION_OUTPUT.search(output) is not None:
        return FailureClassification.AUTHENTICATION
    if _SENSOR_MISSING_OUTPUT.search(output) is not None:
        return FailureClassification.RESPONSE_FORMAT
    return FailureClassification.COMMUNICATION


def _failure_detail(classification: FailureClassification, output: str, returncode: int) -> str:
    """A credential-free, output-free description of a failed command."""
    if classification is FailureClassification.AUTHENTICATION:
        return "the IPMI credentials were rejected"
    if classification is FailureClassification.RESPONSE_FORMAT:
        return "the configured IPMI sensor was not found"
    if _SESSION_EXHAUSTED_OUTPUT.search(output) is not None:
        return "the controller has no free IPMI session slot"
    if _SESSION_OUTPUT.search(output) is not None:
        return "no IPMI session could be established (controller unreachable, not answering, or out of session slots)"
    return f"ipmitool could not read the configured host (exit status {returncode})"


def default_sdr_cache_dir() -> Path:
    """Where SDR cache files live: the environment override, else the add-on's persistent ``/data``."""
    configured = os.environ.get(SDR_CACHE_DIR_ENV)
    if configured:
        return Path(configured)
    if _ADDON_DATA_DIR.is_dir() and os.access(_ADDON_DATA_DIR, os.W_OK):
        return _ADDON_DATA_DIR / "ipmi-sdr-cache"
    return Path(tempfile.gettempdir()) / "remote-energy-meter-ipmi-sdr"


def _cache_file_name(device_id: str, params: IpmiParams) -> str:
    """One file per device and controller endpoint, so re-pointing a device never reuses a foreign repository."""
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", device_id)[:64] or "device"
    endpoint = f"{params.host}|{params.interface}|{params.cipher_suite}".encode()
    return f"{safe_id}-{hashlib.sha256(endpoint).hexdigest()[:12]}.sdr"


def _create_staging_file(cache_path: Path) -> Path:
    """Make the file ``ipmitool sdr dump`` will write into, next to the cache, with ``O_EXCL`` and mode 0600.

    Created by this process rather than by the child: a pre-planted symlink at a predictable name could
    otherwise redirect a root ``ipmitool`` into overwriting an arbitrary file.
    """
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f"{cache_path.name}.", suffix=".tmp", dir=cache_path.parent)
    os.close(descriptor)
    return Path(name)


def _install_staged_cache(staging: Path, cache_path: Path) -> bool:
    """Atomically move a non-empty, regular staging file into place; ``False`` when there is nothing usable."""
    if staging.is_symlink() or not staging.is_file() or staging.stat().st_size == 0:
        return False
    staging.replace(cache_path)
    return True


def _discard(path: Path) -> None:
    with suppress(OSError):
        path.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class _CacheFailure:
    """Why the last SDR cache build failed, and when (loop time) another may be attempted."""

    classification: FailureClassification
    detail: str
    retry_at: float


class IpmiPlugin:
    """Read one named SDR sensor from one IPMI host."""

    plugin_id: ClassVar[str] = PLUGIN_ID
    config_model: ClassVar[type[BaseModel]] = IpmiParams

    #: A cache younger than this is not discarded merely because the sensor is missing from it:
    #: that is a configuration error, and re-dumping the repository every poll would not fix it.
    CACHE_MIN_AGE_FOR_INVALIDATION_S: ClassVar[float] = 600.0

    __slots__ = (
        "_cache_build",
        "_cache_failure",
        "_cache_path",
        "_clock",
        "_device_id",
        "_executable",
        "_params",
        "_runner",
    )

    def __init__(
        self,
        device_id: str,
        params: BaseModel,
        *,
        command_runner: CommandRunner | None = None,
        executable: str = DEFAULT_EXECUTABLE,
        clock: Callable[[], datetime] | None = None,
        sdr_cache_dir: Path | None = None,
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
        directory = sdr_cache_dir if sdr_cache_dir is not None else default_sdr_cache_dir()
        self._cache_path = directory / _cache_file_name(device_id, params)
        self._cache_build: asyncio.Task[_CacheFailure | None] | None = None
        self._cache_failure: _CacheFailure | None = None

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(device_id={self._device_id!r}, host={self._params.host!r}, "
            f"executable={_executable_name((self._executable,))!r})"
        )

    @property
    def sdr_cache_path(self) -> Path:
        """The local SDR cache file this instance reads with ``-S``."""
        return self._cache_path

    async def read_power(self, timeout_s: float) -> ReadResult:
        return await self._read_once(timeout_s)

    async def validate_connection(self, timeout_s: float) -> ValidationResult:
        outcome = await self._read_once(timeout_s)
        if isinstance(outcome, ReadFailure):
            return ValidationResult.failure(outcome.classification)
        return ValidationResult.success()

    # -- command construction ----------------------------------------------------------------------

    def _effective_timeout(self, timeout_s: float) -> float:
        if math.isfinite(timeout_s) and timeout_s > 0:
            return min(float(timeout_s), MAX_REQUEST_TIMEOUT_S)
        return self._params.request_timeout_s

    @staticmethod
    def _packet_timeout(timeout_s: float) -> int:
        """``-N``: short enough that a lost datagram costs a fraction of the attempt, never under 1 s."""
        return max(1, min(MAX_PACKET_TIMEOUT_S, math.floor(timeout_s / 4)))

    def _base_argv(self, packet_timeout_s: int) -> tuple[str, ...]:
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
            str(packet_timeout_s),
            "-R",
            str(IPMITOOL_RETRY_COUNT),
            # Verbose output carries the RAKP detail that separates a refused credential from an
            # unreachable or saturated controller; it never echoes the password.
            "-v",
        )

    def _read_argv(self, timeout_s: float) -> tuple[str, ...]:
        return (
            *self._base_argv(self._packet_timeout(timeout_s)),
            "-S",
            str(self._cache_path),
            "sdr",
            "get",
            self._params.sensor_name,
        )

    def _dump_argv(self, target: Path) -> tuple[str, ...]:
        return (*self._base_argv(MAX_PACKET_TIMEOUT_S), "sdr", "dump", str(target))

    def _environment(self) -> dict[str, str]:
        """The child's whole environment: the password, plus only what ipmitool needs to run.

        Deliberately *not* a copy of this process's environment. Supervisor injects ``SUPERVISOR_TOKEN``
        (and the operator may set more), and ``ipmitool`` parses attacker-influenced bytes from the
        management network with a CVE history for exactly that; a compromised child must not find a
        token, or any other device's secrets, lying in its environment.
        """
        return {
            "PATH": os.environ.get("PATH", _DEFAULT_CHILD_PATH),
            "LC_ALL": "C",
            IPMI_PASSWORD_ENV: self._params.password.get_secret_value(),
        }

    def _failure(self, classification: FailureClassification, detail: str) -> ReadFailure:
        return ReadFailure(device_id=self._device_id, classification=classification, detail=detail)

    # -- one child process -------------------------------------------------------------------------

    async def _run(self, argv: tuple[str, ...], timeout_s: float) -> CommandResult | ReadFailure:
        """Run one command, mapping every launch and timeout failure to a classified failure."""
        name = _executable_name((self._executable,))
        try:
            return await self._runner.run(argv, self._environment(), timeout_s)
        except ExecutableUnavailableError:
            return self._failure(FailureClassification.COMMUNICATION, f"{name} executable is unavailable")
        except CommandLaunchError:
            return self._failure(FailureClassification.COMMUNICATION, f"{name} executable could not be started")
        except CommandTimeoutError:
            return self._failure(FailureClassification.TIMEOUT, f"{name} did not complete within {timeout_s:.1f}s")
        except FileNotFoundError:
            # An injected runner may surface the operating-system error directly.
            return self._failure(FailureClassification.COMMUNICATION, f"{name} executable is unavailable")
        except OSError:
            return self._failure(FailureClassification.COMMUNICATION, f"{name} executable could not be run")

    # -- SDR cache ---------------------------------------------------------------------------------

    async def _ready_cache(self, deadline: float) -> Path | ReadFailure:
        """Return the cache file, starting or awaiting its background build when it is missing.

        The build is a separate task, so cancelling this attempt -- the scheduler's own timeout,
        or shutdown -- never cancels a dump that the next attempt can use. ``asyncio.wait`` does
        not cancel what it waits for.
        """
        if self._cache_path.is_file():
            return self._cache_path

        loop = asyncio.get_running_loop()
        task = self._cache_build
        if task is None:
            failure = self._cache_failure
            if failure is not None and loop.time() < failure.retry_at:
                return self._failure(
                    failure.classification,
                    f"IPMI SDR cache unavailable ({failure.detail}); next build in "
                    f"{failure.retry_at - loop.time():.0f} s",
                )
            task = asyncio.create_task(self._build_cache(), name=f"ipmi-sdr-cache-{self._device_id}")
            self._cache_build = task
            task.add_done_callback(self._on_cache_built)

        # Keep part of the attempt for the cached read itself: up to 1 s, but never the whole of a
        # short attempt, or a sub-second timeout could never wait for even an instant build.
        remaining_s = deadline - loop.time()
        wait_s = remaining_s - min(_CACHED_READ_RESERVE_S, remaining_s / 4)
        if wait_s > 0 and not task.done():
            await asyncio.wait({task}, timeout=wait_s)
        if not task.done():
            return self._failure(
                FailureClassification.COMMUNICATION,
                "building the local IPMI SDR cache (one-time); readings start once it completes",
            )
        # The done callback has run by now only if the loop got a turn; apply it directly too.
        self._on_cache_built(task)
        if self._cache_path.is_file():
            return self._cache_path
        failure = self._cache_failure
        if failure is None:
            return self._failure(FailureClassification.COMMUNICATION, "the IPMI SDR cache could not be built")
        return self._failure(failure.classification, f"IPMI SDR cache unavailable ({failure.detail})")

    def _on_cache_built(self, task: asyncio.Task[_CacheFailure | None]) -> None:
        if self._cache_build is not task:
            return
        self._cache_build = None
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self._cache_failure = _CacheFailure(
                FailureClassification.PLUGIN,
                f"{type(error).__name__} while building the cache",
                asyncio.get_running_loop().time() + SDR_CACHE_RETRY_AFTER_S,
            )
            return
        self._cache_failure = task.result()

    async def _build_cache(self) -> _CacheFailure | None:
        """Dump the controller's SDR repository to the cache file, atomically. ``None`` on success."""
        loop = asyncio.get_running_loop()

        def failed(classification: FailureClassification, detail: str) -> _CacheFailure:
            return _CacheFailure(classification, detail, loop.time() + SDR_CACHE_RETRY_AFTER_S)

        try:
            staging = await asyncio.to_thread(_create_staging_file, self._cache_path)
        except OSError:
            return failed(FailureClassification.COMMUNICATION, "the SDR cache directory is not writable")

        try:
            outcome = await self._run(self._dump_argv(staging), SDR_CACHE_BUILD_TIMEOUT_S)
            if isinstance(outcome, ReadFailure):
                return failed(outcome.classification, outcome.detail)
            output = f"{_decode(outcome.stdout)}\n{_decode(outcome.stderr)}"
            if outcome.returncode != 0:
                classification = classify_ipmitool_failure(output)
                return failed(classification, _failure_detail(classification, output, outcome.returncode))
            if not await asyncio.to_thread(_install_staged_cache, staging, self._cache_path):
                return failed(FailureClassification.RESPONSE_FORMAT, "ipmitool sdr dump wrote no repository")
            return None
        finally:
            await asyncio.to_thread(_discard, staging)

    def _invalidate_cache(self, *, force: bool) -> None:
        """Drop the cache so the next attempt rebuilds it.

        ``force`` is for a cache ipmitool itself reports as unreadable. A merely *missing sensor*
        only invalidates a cache old enough that the repository may have changed since.
        """
        try:
            if not force:
                age_s = datetime.now(tz=UTC).timestamp() - self._cache_path.stat().st_mtime
                if age_s < self.CACHE_MIN_AGE_FOR_INVALIDATION_S:
                    return
            self._cache_path.unlink(missing_ok=True)
        except OSError:
            return

    # -- one read ----------------------------------------------------------------------------------

    async def _read_once(self, timeout_s: float) -> ReadResult:
        """One cached, read-only sensor request, shared by both interface operations."""
        effective_timeout = self._effective_timeout(timeout_s)
        loop = asyncio.get_running_loop()
        # Finish inside the caller's bound so the child is reaped and the outcome classified here.
        deadline = loop.time() + max(effective_timeout - _DEADLINE_MARGIN_S, effective_timeout / 2)

        cache = await self._ready_cache(deadline)
        if isinstance(cache, ReadFailure):
            return cache
        remaining_s = deadline - loop.time()
        if remaining_s <= 0:
            return self._failure(
                FailureClassification.TIMEOUT,
                "the IPMI SDR cache became ready too late for this attempt",
            )

        outcome = await self._run(self._read_argv(effective_timeout), remaining_s)
        if isinstance(outcome, ReadFailure):
            return outcome

        received_at = _as_utc(self._clock())
        output = f"{_decode(outcome.stdout)}\n{_decode(outcome.stderr)}"
        # ipmitool exits 0 even when the cache file is corrupt and the sensor cannot be found in it.
        if _CACHE_UNUSABLE_OUTPUT.search(output) is not None:
            self._invalidate_cache(force=True)
            return self._failure(
                FailureClassification.RESPONSE_FORMAT,
                "the local IPMI SDR cache was unreadable and will be rebuilt",
            )
        if outcome.returncode != 0:
            classification = classify_ipmitool_failure(output)
            if classification is FailureClassification.RESPONSE_FORMAT:
                self._invalidate_cache(force=False)
            return self._failure(classification, _failure_detail(classification, output, outcome.returncode))

        watts = parse_sensor_output(_decode(outcome.stdout), device_id=self._device_id)
        if isinstance(watts, ReadFailure):
            if _SENSOR_MISSING_OUTPUT.search(output) is not None:
                self._invalidate_cache(force=False)
                return self._failure(FailureClassification.RESPONSE_FORMAT, "the configured IPMI sensor was not found")
            return watts
        return PowerReading(device_id=self._device_id, watts=watts, measured_at=received_at)
