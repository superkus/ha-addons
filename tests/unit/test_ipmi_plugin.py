"""Async operation, command secrecy, classification, and child-cleanup tests for IPMI."""

from __future__ import annotations

import asyncio
import os
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Final

import pytest
from pydantic import BaseModel

from remote_energy_meter.plugins.interface import (
    FailureClassification,
    PowerReading,
    ReadFailure,
    ValidationResult,
)
from remote_energy_meter.plugins.ipmi.plugin import (
    IPMI_PASSWORD_ENV,
    AsyncioCommandRunner,
    CommandLaunchError,
    CommandResult,
    CommandTimeoutError,
    ExecutableUnavailableError,
    IpmiPlugin,
)
from remote_energy_meter.plugins.ipmi.schema import IpmiParams

DEVICE_ID: Final = "poweredge_b"
HOST: Final = "192.0.2.20"
USERNAME: Final = "root"
PASSWORD: Final = "not-on-the-command-line"
RECEIVED_AT: Final = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)
VALID_PARAMS: Final[dict[str, Any]] = {"host": HOST, "username": USERNAME, "password": PASSWORD}
SUCCESS_OUTPUT: Final = b"Sensor ID              : System Level (0x1)\nSensor Reading         : 112 (+/- 0) Watts\n"


@dataclass(slots=True)
class RecordedCommand:
    argv: tuple[str, ...]
    env: dict[str, str] = field(repr=False)
    timeout_s: float = 0.0


type RunnerOutcome = CommandResult | BaseException


@dataclass(slots=True)
class ScriptedRunner:
    outcomes: deque[RunnerOutcome]
    calls: list[RecordedCommand] = field(default_factory=list)

    async def run(
        self,
        argv: tuple[str, ...],
        env: Mapping[str, str],
        timeout_s: float,
    ) -> CommandResult:
        self.calls.append(RecordedCommand(argv=argv, env=dict(env), timeout_s=timeout_s))
        if not self.outcomes:
            raise AssertionError("no command outcome configured")
        outcome = self.outcomes.popleft()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def completed(
    returncode: int = 0,
    *,
    stdout: bytes = SUCCESS_OUTPUT,
    stderr: bytes = b"",
) -> CommandResult:
    return CommandResult(returncode=returncode, stdout=stdout, stderr=stderr)


def runner_with(*outcomes: RunnerOutcome) -> ScriptedRunner:
    return ScriptedRunner(deque(outcomes))


def make_plugin(
    runner: ScriptedRunner | AsyncioCommandRunner,
    *,
    device_id: str = DEVICE_ID,
    executable: str = "ipmitool",
    clock: Any = None,
    **overrides: Any,
) -> IpmiPlugin:
    parsed = IpmiParams(**(VALID_PARAMS | overrides))
    return IpmiPlugin(
        device_id,
        parsed,
        command_runner=runner,
        executable=executable,
        clock=clock if clock is not None else lambda: RECEIVED_AT,
    )


async def read(plugin: IpmiPlugin, timeout_s: float = 5.0) -> PowerReading | ReadFailure:
    return await plugin.read_power(timeout_s)


async def reading(plugin: IpmiPlugin, timeout_s: float = 5.0) -> PowerReading:
    result = await read(plugin, timeout_s)
    assert isinstance(result, PowerReading), result
    return result


async def failure(plugin: IpmiPlugin, timeout_s: float = 5.0) -> ReadFailure:
    result = await read(plugin, timeout_s)
    assert isinstance(result, ReadFailure), result
    return result


def test_parameters_from_another_schema_are_refused() -> None:
    class OtherParams(BaseModel):
        host: str

    with pytest.raises(TypeError, match="IpmiParams"):
        IpmiPlugin(DEVICE_ID, OtherParams(host=HOST))


async def test_successful_read_uses_exact_safe_argv_and_child_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REMOTE_ENERGY_METER_TEST_PARENT", "preserved")
    runner = runner_with(completed())
    plugin = make_plugin(runner, executable="/opt/bin/ipmitool-test")

    result = await reading(plugin, timeout_s=4.2)

    assert (result.device_id, result.watts, result.measured_at) == (DEVICE_ID, 112.0, RECEIVED_AT)
    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call.argv == (
        "/opt/bin/ipmitool-test",
        "-I",
        "lanplus",
        "-H",
        HOST,
        "-U",
        USERNAME,
        "-E",
        "-C",
        "3",
        "-N",
        "5",
        "-R",
        "1",
        "sdr",
        "get",
        "System Level",
    )
    assert call.timeout_s == 4.2
    assert call.env == (dict(os.environ) | {IPMI_PASSWORD_ENV: PASSWORD})
    assert PASSWORD not in call.argv
    assert "-P" not in call.argv


async def test_custom_sensor_interface_and_cipher_reach_the_command() -> None:
    runner = runner_with(completed())

    await reading(
        make_plugin(
            runner,
            sensor_name="PS1 Input Power",
            interface="lan",
            cipher_suite=8,
        )
    )

    argv = runner.calls[0].argv
    assert argv[argv.index("-I") + 1] == "lan"
    assert argv[argv.index("-C") + 1] == "8"
    assert argv[-3:] == ("sdr", "get", "PS1 Input Power")


async def test_password_is_absent_from_argv_representations_details_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    echoed = f"authentication failed; supplied password was {PASSWORD}".encode()
    result = completed(1, stdout=b"", stderr=echoed)
    runner = runner_with(result)
    plugin = make_plugin(runner)

    outcome = await failure(plugin)
    rendered = "\n".join((repr(plugin), repr(result), repr(runner.calls[0]), outcome.detail, caplog.text))

    assert outcome.classification is FailureClassification.AUTHENTICATION
    assert PASSWORD not in rendered
    assert PASSWORD not in runner.calls[0].argv


@pytest.mark.parametrize(
    "stderr",
    [
        b"RAKP 2 message indicates an error : unauthorized name",
        b"Error: Unable to establish IPMI v2 / RMCP+ session",
        b"password verification failed",
    ],
)
async def test_session_or_authentication_rejection_is_authentication(stderr: bytes) -> None:
    result = await failure(make_plugin(runner_with(completed(1, stdout=b"", stderr=stderr))))

    assert result.classification is FailureClassification.AUTHENTICATION


async def test_network_or_tool_failure_is_communication() -> None:
    result = await failure(
        make_plugin(runner_with(completed(1, stdout=b"", stderr=b"Error: Unable to establish LAN session")))
    )

    assert result.classification is FailureClassification.COMMUNICATION
    assert "exit status 1" in result.detail


async def test_missing_sensor_is_response_format() -> None:
    result = await failure(make_plugin(runner_with(completed(1, stdout=b"", stderr=b"Sensor data record not found!"))))

    assert result.classification is FailureClassification.RESPONSE_FORMAT


async def test_successful_command_with_missing_or_malformed_output_is_response_format() -> None:
    for output in (b"", b"Sensor Reading : n/a Watts", b"Sensor Reading : 12 Volts"):
        result = await failure(make_plugin(runner_with(completed(stdout=output))))
        assert result.classification is FailureClassification.RESPONSE_FORMAT


@pytest.mark.parametrize("output", [b"Sensor Reading : nan Watts", b"Sensor Reading : 100001 Watts"])
async def test_nonfinite_or_out_of_range_output_is_invalid_measurement(output: bytes) -> None:
    result = await failure(make_plugin(runner_with(completed(stdout=output))))

    assert result.classification is FailureClassification.INVALID_MEASUREMENT


@pytest.mark.parametrize(
    ("error", "classification", "phrase"),
    [
        (CommandTimeoutError("timeout"), FailureClassification.TIMEOUT, "did not complete"),
        (ExecutableUnavailableError("missing"), FailureClassification.COMMUNICATION, "executable is unavailable"),
        (CommandLaunchError("launch"), FailureClassification.COMMUNICATION, "could not be started"),
        (FileNotFoundError(), FailureClassification.COMMUNICATION, "executable is unavailable"),
        (OSError(), FailureClassification.COMMUNICATION, "could not be run"),
    ],
)
async def test_expected_runner_failures_are_classified_without_exception_text(
    error: BaseException,
    classification: FailureClassification,
    phrase: str,
) -> None:
    result = await failure(make_plugin(runner_with(error)))

    assert result.classification is classification
    assert phrase in result.detail
    if str(error):
        assert str(error) not in result.detail


async def test_unexpected_code_defects_escape_for_scheduler_boundary_classification() -> None:
    plugin = make_plugin(runner_with(RuntimeError("implementation defect")))

    with pytest.raises(RuntimeError, match="implementation defect"):
        await plugin.read_power(5.0)


async def test_validation_and_reading_share_the_same_single_command_path() -> None:
    validation_runner = runner_with(completed())
    read_runner = runner_with(completed())

    validation = await make_plugin(validation_runner).validate_connection(5.0)
    read_result = await reading(make_plugin(read_runner))

    assert validation == ValidationResult.success()
    assert isinstance(read_result, PowerReading)
    assert len(validation_runner.calls) == len(read_runner.calls) == 1
    assert validation_runner.calls[0].argv == read_runner.calls[0].argv


async def test_validation_returns_the_read_classification() -> None:
    runner = runner_with(completed(1, stderr=b"unauthorized name"))

    result = await make_plugin(runner).validate_connection(5.0)

    assert result == ValidationResult.failure(FailureClassification.AUTHENTICATION)
    assert len(runner.calls) == 1


async def test_receipt_timestamp_is_normalized_to_utc() -> None:
    elsewhere = RECEIVED_AT.astimezone(timezone(timedelta(hours=5, minutes=30)))
    result = await reading(make_plugin(runner_with(completed()), clock=lambda: elsewhere))

    assert result.measured_at == RECEIVED_AT
    assert result.measured_at.tzinfo is UTC


async def test_two_instances_keep_commands_credentials_and_results_isolated() -> None:
    runner_a = runner_with(completed(1, stderr=b"Unable to establish LAN session"))
    runner_b = runner_with(completed(stdout=b"Sensor Reading : 112 (+/- 0) Watts"))
    plugin_a = make_plugin(runner_a, device_id="legacy_a", host="192.0.2.1", password="secret-a")
    plugin_b = make_plugin(runner_b, device_id="legacy_b", host="192.0.2.2", password="secret-b")

    result_a, result_b = await asyncio.gather(plugin_a.read_power(5.0), plugin_b.read_power(5.0))

    assert isinstance(result_a, ReadFailure)
    assert isinstance(result_b, PowerReading)
    assert (result_b.device_id, result_b.watts) == ("legacy_b", 112.0)
    assert runner_a.calls[0].env[IPMI_PASSWORD_ENV] == "secret-a"
    assert runner_b.calls[0].env[IPMI_PASSWORD_ENV] == "secret-b"
    assert runner_a.calls[0].argv[runner_a.calls[0].argv.index("-H") + 1] == "192.0.2.1"
    assert runner_b.calls[0].argv[runner_b.calls[0].argv.index("-H") + 1] == "192.0.2.2"


class ControlledProcess:
    """A child that exits on terminate, or ignores it until kill, for cleanup tests."""

    __slots__ = (
        "_done",
        "_ignore_terminate",
        "_returncode",
        "_stderr",
        "_stdout",
        "communicate_calls",
        "communicate_started",
        "kill_calls",
        "terminate_calls",
    )

    def __init__(
        self,
        *,
        ignore_terminate: bool = False,
        completed_at_start: bool = False,
        stdout: bytes = SUCCESS_OUTPUT,
        stderr: bytes = b"",
    ) -> None:
        self._done = asyncio.Event()
        self._ignore_terminate = ignore_terminate
        self._returncode: int | None = 0 if completed_at_start else None
        self._stdout = stdout
        self._stderr = stderr
        self.communicate_calls = 0
        self.communicate_started = asyncio.Event()
        self.terminate_calls = 0
        self.kill_calls = 0
        if completed_at_start:
            self._done.set()

    @property
    def returncode(self) -> int | None:
        return self._returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        self.communicate_calls += 1
        self.communicate_started.set()
        await self._done.wait()
        return self._stdout, self._stderr

    def terminate(self) -> None:
        self.terminate_calls += 1
        if not self._ignore_terminate:
            self._returncode = -15
            self._done.set()

    def kill(self) -> None:
        self.kill_calls += 1
        self._returncode = -9
        self._done.set()


@dataclass(slots=True)
class RecordingProcessFactory:
    process: ControlledProcess
    calls: int = 0
    argv: tuple[str, ...] | None = None
    env: dict[str, str] = field(default_factory=dict, repr=False)

    async def __call__(self, argv: tuple[str, ...], env: Mapping[str, str]) -> ControlledProcess:
        self.calls += 1
        self.argv = argv
        self.env = dict(env)
        return self.process


async def test_asyncio_runner_creates_exactly_one_subprocess_for_a_successful_operation() -> None:
    process = ControlledProcess(completed_at_start=True)
    factory = RecordingProcessFactory(process)
    runner = AsyncioCommandRunner(process_factory=factory)

    result = await reading(make_plugin(runner))

    assert result.watts == 112.0
    assert factory.calls == 1
    assert process.communicate_calls == 1
    assert factory.env[IPMI_PASSWORD_ENV] == PASSWORD
    assert factory.argv is not None and PASSWORD not in factory.argv


async def test_operation_timeout_terminates_and_reaps_the_child() -> None:
    process = ControlledProcess()
    factory = RecordingProcessFactory(process)
    runner = AsyncioCommandRunner(process_factory=factory, termination_grace_s=0.01)

    result = await failure(make_plugin(runner), timeout_s=0.01)

    assert result.classification is FailureClassification.TIMEOUT
    assert factory.calls == 1
    assert process.terminate_calls == 1
    assert process.kill_calls == 0
    assert process.returncode == -15
    assert process.communicate_calls == 2, "the timed-out communicate and cleanup reap"


async def test_cancellation_terminates_kills_and_reaps_an_uncooperative_child() -> None:
    process = ControlledProcess(ignore_terminate=True)
    factory = RecordingProcessFactory(process)
    runner = AsyncioCommandRunner(process_factory=factory, termination_grace_s=0.01)
    plugin = make_plugin(runner)
    task = asyncio.create_task(plugin.read_power(5.0))
    await asyncio.wait_for(process.communicate_started.wait(), timeout=1.0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert factory.calls == 1
    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.returncode == -9
    assert process.communicate_calls == 3, "cancelled communicate, terminate grace, and post-kill reap"


async def test_absent_executable_is_a_safe_communication_failure() -> None:
    async def missing(argv: tuple[str, ...], env: Mapping[str, str]) -> ControlledProcess:
        raise FileNotFoundError

    runner = AsyncioCommandRunner(process_factory=missing)
    result = await failure(make_plugin(runner, executable="missing-ipmitool"))

    assert result.classification is FailureClassification.COMMUNICATION
    assert result.detail == "missing-ipmitool executable is unavailable"
    assert PASSWORD not in result.detail
