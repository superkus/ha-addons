"""Unit tests for the poll cycle executor (task 11.2).

Covers, against a virtual clock, what one cycle does for one device: how each failure classification is retried, where
the wall-clock budget cuts a cycle short, what a fully failed cycle reports, how a timeout and an unhandled plugin
exception are classified at the scheduler boundary, and the one debug record a completed cycle owes.

Everything here runs on :class:`VirtualClock` rather than on ``asyncio.sleep``, for the same reason the executor takes
its clock by injection: the retry schedule of Requirement 5.5 is measured against a budget of up to 3600 seconds, and a
test that waited out real retry delays would take 3 seconds per cycle and would still not be able to reach the budget
cutoff at all. The same rules quantified over the whole input space are Properties 16, 17, and 18 (tasks 11.7-11.9);
wall-clock behaviour is verified once in the integration layer.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar, Final

import pytest
from pydantic import BaseModel, Field

from remote_energy_meter.config.models import DeviceEntry
from remote_energy_meter.config.runtime import ResolvedDevice
from remote_energy_meter.diagnostics.logger import DEVICE_ID_FIELD, LoggedFailureClassification
from remote_energy_meter.plugins.interface import (
    DevicePlugin,
    FailureClassification,
    PowerReading,
    ReadFailure,
    ValidationResult,
)
from remote_energy_meter.scheduler import (
    MAX_ATTEMPTS_PER_CYCLE,
    RETRY_DELAYS_S,
    CycleResult,
    NullCycleSink,
    PollAttempt,
    PollCycleExecutor,
    next_retry_delay,
    resolve_timeout,
    whole_milliseconds,
)

DEVICE_ID: Final = "poweredge_a"
MEASURED_AT: Final = datetime(2025, 1, 1, tzinfo=UTC)
DEFAULT_WATTS: Final = 214.0

#: A dedicated logger, so ``caplog`` sees this module's records and nothing another test left configured.
LOGGER_NAME: Final = "remote_energy_meter.tests.scheduler_cycle"

#: Every classification the retry policy of Requirement 5.5 *does* retry: all of them except authentication.
RETRYABLE = tuple(c for c in FailureClassification if c is not FailureClassification.AUTHENTICATION)


# --------------------------------------------------------------------------------------------------
# a virtual clock
# --------------------------------------------------------------------------------------------------


class _VirtualExpiryError(Exception):
    """Raised inside a virtually-slept block when the innermost virtual deadline passes.

    Stands in for the cancellation ``asyncio.timeout`` delivers at its deadline, and is rewritten to
    :class:`TimeoutError` by :meth:`VirtualClock.timeout` exactly as ``asyncio.timeout`` rewrites the cancellation it
    caused itself.
    """

    def __init__(self, at: float) -> None:
        super().__init__(f"virtual deadline at {at:g} s")
        self.at = at


@dataclass
class _Deadline:
    at: float


@dataclass
class VirtualClock:
    """A monotonic clock, a sleep, and a timeout that agree with each other and cost nothing to advance.

    ``sleep`` advances :attr:`now` instead of suspending, so a 3600 second retry budget is explored in microseconds.
    ``timeout`` registers a deadline; a sleep that would cross the nearest one advances to that deadline and raises,
    which is what makes a stalling fake plugin time out at the same instant a real one would.

    A single yield to the event loop per sleep keeps concurrently running cycles interleaving, so a test that gathers
    two devices still exercises two independent cycles rather than one after the other.
    """

    now: float = 0.0
    slept: list[float] = field(default_factory=list)
    _deadlines: list[_Deadline] = field(default_factory=list)

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.slept.append(delay)
        target = self.now + delay
        crossed = [deadline.at for deadline in self._deadlines if deadline.at <= target]
        if crossed:
            self.now = min(crossed)
            await asyncio.sleep(0)
            raise _VirtualExpiryError(self.now)
        self.now = target
        await asyncio.sleep(0)

    @asynccontextmanager
    async def timeout(self, delay: float) -> AsyncIterator[None]:
        deadline = _Deadline(at=self.now + delay)
        self._deadlines.append(deadline)
        try:
            yield
        except _VirtualExpiryError as expiry:
            if expiry.at <= deadline.at:
                raise TimeoutError(f"virtual timeout after {delay:g} s") from None
            raise
        finally:
            self._deadlines.remove(deadline)


# --------------------------------------------------------------------------------------------------
# a scripted plugin
# --------------------------------------------------------------------------------------------------


class Params(BaseModel):
    """A minimal plugin configuration schema."""

    host: str = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class Stall:
    """A scripted read that consumes ``seconds`` of virtual time, then behaves like ``then``."""

    seconds: float
    then: object = None


@dataclass(frozen=True, slots=True)
class Returns:
    """A scripted read that returns ``value`` verbatim, whatever it is -- including ``None``."""

    value: object


class ScriptedPlugin:
    """A conforming plugin whose reads follow a script, one step per attempt.

    A step is a :class:`PowerReading` or :class:`ReadFailure` to return, a :class:`BaseException` to raise, a
    :class:`Stall` to burn virtual time, a :class:`Returns` to hand back something arbitrary, or ``None`` for a default
    successful reading. The last step repeats for every further attempt, so "always times out" is a one-step script and
    "fails once then succeeds" is a two-step one.
    """

    plugin_id: ClassVar[str] = "scripted"
    config_model: ClassVar[type[BaseModel]] = Params

    def __init__(
        self,
        device_id: str = DEVICE_ID,
        params: BaseModel | None = None,
        *,
        script: Sequence[object] = (),
        clock: VirtualClock | None = None,
    ) -> None:
        self.device_id = device_id
        self.params = params
        self.clock = clock
        self.script: list[object] = list(script) or [None]
        self.timeouts: list[float] = []

    @property
    def read_calls(self) -> int:
        """How many reads have been requested of this instance."""
        return len(self.timeouts)

    async def validate_connection(self, timeout_s: float) -> ValidationResult:
        return ValidationResult.success()

    async def read_power(self, timeout_s: float) -> Any:
        step = self.script[min(self.read_calls, len(self.script) - 1)]
        self.timeouts.append(timeout_s)
        if isinstance(step, Stall):
            if self.clock is None:
                raise AssertionError("a stalling script needs a clock to stall on")
            await self.clock.sleep(step.seconds)
            step = step.then
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, Returns):
            return step.value
        if step is None:
            return PowerReading(device_id=self.device_id, watts=DEFAULT_WATTS, measured_at=MEASURED_AT)
        return step


class RecordingSink:
    """Records what the executor reported, so Requirement 5.6 is asserted on the call sequence itself."""

    def __init__(self, *, raises: BaseException | None = None) -> None:
        self.readings: list[tuple[str, float, int]] = []
        self.unavailable: list[tuple[str, FailureClassification]] = []
        self._raises = raises

    async def deliver_reading(self, device: ResolvedDevice, reading: PowerReading, duration_ms: int) -> None:
        if self._raises is not None:
            raise self._raises
        self.readings.append((device.device_id, reading.watts, duration_ms))

    async def report_unavailable(self, device: ResolvedDevice, failure: ReadFailure) -> None:
        if self._raises is not None:
            raise self._raises
        self.unavailable.append((device.device_id, failure.classification))


# --------------------------------------------------------------------------------------------------
# builders
# --------------------------------------------------------------------------------------------------


def make_device(
    plugin: DevicePlugin,
    *,
    device_id: str = DEVICE_ID,
    interval_s: int = 30,
    timeout_s: float = 10.0,
) -> ResolvedDevice:
    entry = DeviceEntry(device_id=device_id, plugin=ScriptedPlugin.plugin_id, params={"host": "192.0.2.10"})
    return ResolvedDevice(entry=entry, plugin=plugin, interval_s=interval_s, timeout_s=timeout_s)


def make_executor(
    clock: VirtualClock,
    sink: RecordingSink | None = None,
    logger: logging.Logger | None = None,
) -> PollCycleExecutor:
    return PollCycleExecutor(
        sink=sink,
        clock=clock.monotonic,
        sleep=clock.sleep,
        timeout=clock.timeout,
        logger=logger if logger is not None else logging.getLogger(LOGGER_NAME),
    )


def failure(classification: FailureClassification, device_id: str = DEVICE_ID) -> ReadFailure:
    return ReadFailure(device_id=device_id, classification=classification, detail="scripted")


def debug_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """The cycle-complete records of Requirement 9.4."""
    return [
        record
        for record in caplog.records
        if record.levelno == logging.DEBUG and str(record.msg).startswith("poll cycle complete")
    ]


def warning_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """The one-per-failed-operation records of Requirement 9.3."""
    return [record for record in caplog.records if record.levelno == logging.WARNING]


@pytest.fixture
def captured(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """Capture every severity from this module's logger, whatever a previous test configured."""
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
    return caplog


# --------------------------------------------------------------------------------------------------
# the retry decision, as a pure function (Requirement 5.5)
# --------------------------------------------------------------------------------------------------


def test_the_retry_allowance_is_two_additional_attempts() -> None:
    """Requirement 5.5: one initial attempt and up to two retries, at 1 s then 2 s."""
    assert RETRY_DELAYS_S == (1.0, 2.0)
    assert MAX_ATTEMPTS_PER_CYCLE == 3


@pytest.mark.parametrize("attempt_count", [1, 2])
def test_authentication_is_never_retried(attempt_count: int) -> None:
    """Requirement 5.5: repeating a refused credential cannot succeed and walks toward an account lockout."""
    assert next_retry_delay(attempt_count, 0.0, 3600, FailureClassification.AUTHENTICATION) is None


@pytest.mark.parametrize("classification", RETRYABLE)
def test_every_other_classification_is_retried_at_one_then_two_seconds(
    classification: FailureClassification,
) -> None:
    assert next_retry_delay(1, 0.0, 3600, classification) == 1.0
    assert next_retry_delay(2, 1.0, 3600, classification) == 2.0


@pytest.mark.parametrize("classification", RETRYABLE)
def test_the_third_attempt_is_the_last(classification: FailureClassification) -> None:
    assert next_retry_delay(MAX_ATTEMPTS_PER_CYCLE, 0.0, 3600, classification) is None


@pytest.mark.parametrize(
    ("attempt_count", "elapsed_s", "interval_s", "expected"),
    [
        (1, 3.9, 5, 1.0),  # 3.9 + 1 < 5: the retry still fits
        (1, 4.0, 5, None),  # 4.0 + 1 == 5: reaching the interval stops the cycle
        (1, 4.5, 5, None),
        (2, 2.9, 5, 2.0),
        (2, 3.0, 5, None),  # 3.0 + 2 == 5: the boundary is exclusive
        (2, 3.5, 5, None),
    ],
)
def test_no_attempt_starts_once_the_budget_is_reached(
    attempt_count: int,
    elapsed_s: float,
    interval_s: int,
    expected: float | None,
) -> None:
    """Requirement 5.5: the budget covers attempts *and* retry delays, and is reached exclusively."""
    assert next_retry_delay(attempt_count, elapsed_s, interval_s, FailureClassification.TIMEOUT) == expected


def test_a_retry_decision_needs_a_completed_attempt() -> None:
    """A wiring error in the caller, not data from a device, so it raises rather than being absorbed."""
    with pytest.raises(ValueError, match="at least one completed attempt"):
        next_retry_delay(0, 0.0, 30, FailureClassification.TIMEOUT)


@pytest.mark.parametrize(
    ("attempt_count", "elapsed_s", "expected"),
    [
        (1, 10.0, 1.0),  # 10 + 1 + 10 = 21 < 30: the second attempt can finish inside the interval
        (1, 18.9, 1.0),  # 18.9 + 1 + 10 = 29.9 < 30
        (1, 19.0, None),  # 19 + 1 + 10 == 30: it could still be running at the next poll
        (2, 17.9, 2.0),  # 17.9 + 2 + 10 = 29.9 < 30
        (2, 21.0, None),  # the production overrun: a third attempt at ~21 s ran to ~33 s
    ],
)
def test_a_retry_only_starts_when_its_own_timeout_also_fits(
    attempt_count: int, elapsed_s: float, expected: float | None
) -> None:
    """The retry's worst case counts against the budget, so a cycle never runs into its next scheduled poll."""
    assert (
        next_retry_delay(attempt_count, elapsed_s, 30, FailureClassification.TIMEOUT, attempt_timeout_s=10.0)
        == expected
    )


def test_a_negative_attempt_timeout_does_not_extend_the_budget() -> None:
    """29 + 1 reaches the interval; an unclamped negative timeout would wrongly let the retry start."""
    assert next_retry_delay(1, 29.0, 30, FailureClassification.TIMEOUT, attempt_timeout_s=-100.0) is None


# --------------------------------------------------------------------------------------------------
# duration conversion (Requirements 9.4, 9.5)
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0.0, 0), (0.0004, 0), (0.0006, 1), (0.25, 250), (1.2345, 1234), (12.0, 12000), (-1.0, 0)],
)
def test_durations_convert_to_whole_milliseconds(seconds: float, expected: int) -> None:
    assert whole_milliseconds(seconds) == expected


# --------------------------------------------------------------------------------------------------
# a successful cycle
# --------------------------------------------------------------------------------------------------


async def test_a_successful_first_attempt_ends_the_cycle(captured: pytest.LogCaptureFixture) -> None:
    clock = VirtualClock()
    plugin = ScriptedPlugin(script=[Stall(0.25)], clock=clock)
    sink = RecordingSink()
    cycle = await make_executor(clock, sink).run_cycle(make_device(plugin))

    assert cycle.succeeded
    assert cycle.attempt_count == 1
    assert cycle.retry_delays_s == ()
    assert plugin.read_calls == 1
    assert cycle.reading is not None
    assert cycle.reading.watts == DEFAULT_WATTS
    assert sink.readings == [(DEVICE_ID, DEFAULT_WATTS, 250)]
    assert sink.unavailable == []
    assert warning_records(captured) == []


async def test_the_attempt_is_given_the_devices_resolved_timeout() -> None:
    """The plugin never chooses its own timeout; the scheduler hands it the resolved one (Requirement 5.8)."""
    clock = VirtualClock()
    plugin = ScriptedPlugin(clock=clock)
    await make_executor(clock).run_cycle(make_device(plugin, timeout_s=4.0))

    assert plugin.timeouts == [4.0]


async def test_a_retry_can_succeed_and_the_cycle_reports_only_the_reading() -> None:
    """One failure then a reading: the device is not reported unavailable at any point in the cycle."""
    clock = VirtualClock()
    plugin = ScriptedPlugin(script=[failure(FailureClassification.COMMUNICATION), Stall(0.4)], clock=clock)
    sink = RecordingSink()
    cycle = await make_executor(clock, sink).run_cycle(make_device(plugin))

    assert cycle.succeeded
    assert cycle.attempt_count == 2
    assert cycle.retry_delays_s == (1.0,)
    assert sink.unavailable == []
    assert sink.readings == [(DEVICE_ID, DEFAULT_WATTS, 400)]


async def test_the_reported_duration_is_the_operation_not_the_cycle() -> None:
    """Requirement 9.5 publishes the duration of the *operation*; two retry sleeps must not inflate it."""
    clock = VirtualClock()
    plugin = ScriptedPlugin(script=[failure(FailureClassification.TIMEOUT), Stall(0.4)], clock=clock)
    sink = RecordingSink()
    cycle = await make_executor(clock, sink).run_cycle(make_device(plugin))

    assert cycle.operation_duration_ms == 400
    assert cycle.duration_ms == 1400
    assert sink.readings == [(DEVICE_ID, DEFAULT_WATTS, 400)]


# --------------------------------------------------------------------------------------------------
# retry behaviour per classification (Requirement 5.5)
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("classification", RETRYABLE)
async def test_a_retryable_classification_is_attempted_three_times(
    classification: FailureClassification,
) -> None:
    clock = VirtualClock()
    plugin = ScriptedPlugin(script=[failure(classification)], clock=clock)
    cycle = await make_executor(clock).run_cycle(make_device(plugin))

    assert plugin.read_calls == MAX_ATTEMPTS_PER_CYCLE
    assert cycle.attempt_count == MAX_ATTEMPTS_PER_CYCLE
    assert cycle.retry_delays_s == RETRY_DELAYS_S
    assert clock.now == pytest.approx(sum(RETRY_DELAYS_S))
    assert cycle.classifications == (classification,) * MAX_ATTEMPTS_PER_CYCLE


async def test_an_authentication_failure_is_attempted_once(captured: pytest.LogCaptureFixture) -> None:
    clock = VirtualClock()
    plugin = ScriptedPlugin(script=[failure(FailureClassification.AUTHENTICATION)], clock=clock)
    sink = RecordingSink()
    cycle = await make_executor(clock, sink).run_cycle(make_device(plugin))

    assert plugin.read_calls == 1
    assert cycle.retry_delays_s == ()
    assert clock.slept == []
    assert sink.unavailable == [(DEVICE_ID, FailureClassification.AUTHENTICATION)]
    assert len(warning_records(captured)) == 1


async def test_the_budget_stops_a_cycle_before_the_next_scheduled_poll() -> None:
    """Requirement 5.5: at a 5 s interval two timeouts and one retry delay exhaust the budget.

    The arithmetic the executor has to get right: the first attempt is abandoned at ``5/3`` s, a 1 s retry delay still
    fits inside 5 s, and after the second attempt is abandoned at ``3 x 5/3`` s a further 2 s delay would reach the
    next scheduled poll -- so no third attempt starts, even though the retry allowance has one left.
    """
    clock = VirtualClock()
    timeout_s = resolve_timeout(None, 5)
    plugin = ScriptedPlugin(script=[Stall(3600.0)], clock=clock)
    cycle = await make_executor(clock).run_cycle(make_device(plugin, interval_s=5, timeout_s=timeout_s))

    assert plugin.read_calls == 2
    assert cycle.retry_delays_s == (1.0,)
    assert cycle.classifications == (FailureClassification.TIMEOUT, FailureClassification.TIMEOUT)
    assert clock.now == pytest.approx(2 * timeout_s + 1.0)
    assert clock.now < 5


async def test_a_hung_controller_never_makes_the_cycle_overrun_its_interval() -> None:
    """The production failure: 30 s interval, 10 s timeout, a controller that never answers.

    Before the fix the cycle ran 10 + 1 + 10 + 2 + 10 = 33 s, so the poll scheduled at 30 s was skipped. The third
    attempt must not start, because 21 + 2 + 10 would reach the interval.
    """
    clock = VirtualClock()
    plugin = ScriptedPlugin(script=[Stall(3600.0)], clock=clock)
    cycle = await make_executor(clock).run_cycle(make_device(plugin, interval_s=30, timeout_s=10.0))

    assert plugin.read_calls == 2
    assert cycle.retry_delays_s == (1.0,)
    assert cycle.classifications == (FailureClassification.TIMEOUT, FailureClassification.TIMEOUT)
    assert clock.now == pytest.approx(21.0)
    assert clock.now < 30


async def test_fast_failures_still_receive_the_whole_retry_allowance() -> None:
    """The attempt-timeout budget only trims retries that could overrun; quick failures keep all three attempts."""
    clock = VirtualClock()
    plugin = ScriptedPlugin(script=[Stall(0.3, failure(FailureClassification.COMMUNICATION))], clock=clock)
    cycle = await make_executor(clock).run_cycle(make_device(plugin, interval_s=30, timeout_s=10.0))

    assert plugin.read_calls == MAX_ATTEMPTS_PER_CYCLE
    assert cycle.retry_delays_s == RETRY_DELAYS_S


# --------------------------------------------------------------------------------------------------
# timeouts (Requirements 4.7, 5.4)
# --------------------------------------------------------------------------------------------------


async def test_an_attempt_that_overruns_its_timeout_is_abandoned_and_classified() -> None:
    clock = VirtualClock()
    plugin = ScriptedPlugin(script=[Stall(3600.0)], clock=clock)
    cycle = await make_executor(clock).run_cycle(make_device(plugin, interval_s=3600, timeout_s=2.0))

    assert cycle.classifications == (FailureClassification.TIMEOUT,) * MAX_ATTEMPTS_PER_CYCLE
    assert [attempt.duration_s for attempt in cycle.attempts] == [2.0, 2.0, 2.0]
    assert cycle.attempts[0].duration_s == 2.0, "the attempt is abandoned at the deadline, not at the plugin's leisure"


async def test_a_timeout_raised_by_the_plugin_itself_is_also_a_timeout() -> None:
    """A plugin that enforces its own deadline and raises reaches the same classification (Requirement 4.7)."""
    clock = VirtualClock()
    plugin = ScriptedPlugin(script=[TimeoutError("read timed out")], clock=clock)
    cycle = await make_executor(clock).run_cycle(make_device(plugin))

    assert cycle.classifications == (FailureClassification.TIMEOUT,) * MAX_ATTEMPTS_PER_CYCLE


# --------------------------------------------------------------------------------------------------
# unhandled plugin exceptions (Requirement 3.7)
# --------------------------------------------------------------------------------------------------


async def test_an_unhandled_plugin_exception_becomes_a_plugin_failure(captured: pytest.LogCaptureFixture) -> None:
    clock = VirtualClock()
    plugin = ScriptedPlugin(script=[ZeroDivisionError("division by zero")], clock=clock)
    sink = RecordingSink()
    cycle = await make_executor(clock, sink).run_cycle(make_device(plugin))

    assert cycle.classifications == (FailureClassification.PLUGIN,) * MAX_ATTEMPTS_PER_CYCLE
    assert cycle.failure is not None
    assert cycle.failure.detail == "ZeroDivisionError: division by zero"
    assert sink.unavailable == [(DEVICE_ID, FailureClassification.PLUGIN)]

    errors = [record for record in captured.records if record.levelno == logging.ERROR]
    assert len(errors) == MAX_ATTEMPTS_PER_CYCLE
    assert all(record.exc_info is not None for record in errors), "a plugin defect needs its traceback to be fixable"


async def test_a_plugin_returning_something_that_is_not_a_result_is_a_plugin_failure() -> None:
    clock = VirtualClock()
    plugin = ScriptedPlugin(script=[Returns(None)], clock=clock)
    cycle = await make_executor(clock).run_cycle(make_device(plugin))

    assert cycle.classifications == (FailureClassification.PLUGIN,) * MAX_ATTEMPTS_PER_CYCLE
    assert cycle.failure is not None
    assert "NoneType" in cycle.failure.detail


async def test_a_reading_for_another_device_is_refused_rather_than_accumulated() -> None:
    """Accepting it would add another device's energy to this device's total (Requirement 3.6)."""
    clock = VirtualClock()
    stranger = PowerReading(device_id="poweredge_b", watts=900.0, measured_at=MEASURED_AT)
    plugin = ScriptedPlugin(script=[stranger], clock=clock)
    sink = RecordingSink()
    cycle = await make_executor(clock, sink).run_cycle(make_device(plugin))

    assert not cycle.succeeded
    assert cycle.classifications == (FailureClassification.PLUGIN,) * MAX_ATTEMPTS_PER_CYCLE
    assert sink.readings == []
    assert sink.unavailable == [(DEVICE_ID, FailureClassification.PLUGIN)]


async def test_a_failure_labelled_with_another_device_is_relabelled() -> None:
    """The entry that actually failed is the one reported, and the classification is left alone."""
    clock = VirtualClock()
    plugin = ScriptedPlugin(script=[failure(FailureClassification.COMMUNICATION, device_id="somewhere_else")])
    cycle = await make_executor(clock).run_cycle(make_device(plugin))

    assert cycle.failure is not None
    assert cycle.failure.device_id == DEVICE_ID
    assert cycle.failure.classification is FailureClassification.COMMUNICATION


async def test_cancellation_propagates_instead_of_being_classified() -> None:
    """A SIGTERM arriving mid-read must not be absorbed as a device failure and retried for 3 more seconds."""
    clock = VirtualClock()
    plugin = ScriptedPlugin(script=[asyncio.CancelledError()], clock=clock)
    sink = RecordingSink()

    with pytest.raises(asyncio.CancelledError):
        await make_executor(clock, sink).run_cycle(make_device(plugin))

    assert plugin.read_calls == 1
    assert sink.readings == []
    assert sink.unavailable == []


# --------------------------------------------------------------------------------------------------
# a fully failed cycle (Requirement 5.6)
# --------------------------------------------------------------------------------------------------


async def test_a_fully_failed_cycle_reports_unavailable_and_delivers_nothing(
    captured: pytest.LogCaptureFixture,
) -> None:
    clock = VirtualClock()
    plugin = ScriptedPlugin(script=[failure(FailureClassification.COMMUNICATION)], clock=clock)
    sink = RecordingSink()
    cycle = await make_executor(clock, sink).run_cycle(make_device(plugin))

    assert not cycle.succeeded
    assert cycle.reading is None
    assert sink.readings == [], "no Power_Reading reaches the accumulator or the publisher from a failed cycle"
    assert sink.unavailable == [(DEVICE_ID, FailureClassification.COMMUNICATION)]
    assert len(debug_records(captured)) == 1


async def test_a_failed_cycle_does_not_prevent_the_next_one() -> None:
    """Requirement 5.6: the cycle ends, the device stays polled, and a later cycle can succeed."""
    clock = VirtualClock()
    plugin = ScriptedPlugin(script=[failure(FailureClassification.TIMEOUT)], clock=clock)
    device = make_device(plugin)
    executor = make_executor(clock)

    first = await executor.run_cycle(device)
    plugin.script = [None]
    second = await executor.run_cycle(device)

    assert not first.succeeded
    assert second.succeeded


async def test_one_devices_failure_leaves_another_devices_cycle_intact() -> None:
    """Requirement 3.7: a raising plugin degrades its own device and nothing else."""
    clock = VirtualClock()
    healthy = make_device(ScriptedPlugin(clock=clock), device_id="poweredge_a")
    broken_plugin = ScriptedPlugin(device_id="poweredge_b", script=[RuntimeError("bad plugin")], clock=clock)
    broken = make_device(broken_plugin, device_id="poweredge_b")
    executor = make_executor(clock)

    results = await asyncio.gather(executor.run_cycle(healthy), executor.run_cycle(broken))

    assert results[0].succeeded
    assert not results[1].succeeded
    assert results[1].classifications == (FailureClassification.PLUGIN,) * MAX_ATTEMPTS_PER_CYCLE


async def test_a_sink_failure_is_contained_to_the_cycle(captured: pytest.LogCaptureFixture) -> None:
    """A broker hiccup or an unregistered device must not end a device's poll loop."""
    clock = VirtualClock()
    sink = RecordingSink(raises=RuntimeError("broker is gone"))
    cycle = await make_executor(clock, sink).run_cycle(make_device(ScriptedPlugin(clock=clock)))

    assert cycle.succeeded
    assert [record.levelno for record in captured.records if record.levelno == logging.ERROR] == [logging.ERROR]
    assert len(debug_records(captured)) == 1


# --------------------------------------------------------------------------------------------------
# log records (Requirements 9.3, 9.4)
# --------------------------------------------------------------------------------------------------


async def test_one_debug_record_names_the_device_the_watts_and_the_duration(
    captured: pytest.LogCaptureFixture,
) -> None:
    clock = VirtualClock()
    plugin = ScriptedPlugin(script=[Stall(0.25)], clock=clock)
    await make_executor(clock).run_cycle(make_device(plugin))

    records = debug_records(captured)
    assert len(records) == 1
    fields = records[0].__dict__
    assert fields[DEVICE_ID_FIELD] == DEVICE_ID
    assert fields["watts"] == DEFAULT_WATTS
    assert fields["duration_ms"] == 250
    assert isinstance(fields["duration_ms"], int)
    assert "214.0 W" in records[0].getMessage()


async def test_the_debug_record_of_a_failed_cycle_carries_no_watts(captured: pytest.LogCaptureFixture) -> None:
    clock = VirtualClock()
    plugin = ScriptedPlugin(script=[failure(FailureClassification.RESPONSE_FORMAT)], clock=clock)
    await make_executor(clock).run_cycle(make_device(plugin))

    records = debug_records(captured)
    assert len(records) == 1
    assert records[0].__dict__["watts"] is None
    assert records[0].__dict__["attempts"] == MAX_ATTEMPTS_PER_CYCLE


async def test_every_failed_attempt_gets_exactly_one_warning_record(captured: pytest.LogCaptureFixture) -> None:
    """Requirement 9.3: one warning per failed power reading operation, naming the device and one classification."""
    clock = VirtualClock()
    plugin = ScriptedPlugin(script=[failure(FailureClassification.TLS_VALIDATION)], clock=clock)
    await make_executor(clock).run_cycle(make_device(plugin))

    records = warning_records(captured)
    assert len(records) == MAX_ATTEMPTS_PER_CYCLE
    for record in records:
        assert record.__dict__[DEVICE_ID_FIELD] == DEVICE_ID
        assert record.__dict__["classification"] == LoggedFailureClassification.TLS_VALIDATION.value


async def test_a_successful_cycle_writes_no_warning_record(captured: pytest.LogCaptureFixture) -> None:
    clock = VirtualClock()
    await make_executor(clock).run_cycle(make_device(ScriptedPlugin(clock=clock)))

    assert warning_records(captured) == []
    assert len(debug_records(captured)) == 1


# --------------------------------------------------------------------------------------------------
# the result value
# --------------------------------------------------------------------------------------------------


def test_a_cycle_always_has_at_least_one_attempt() -> None:
    with pytest.raises(ValueError, match="at least one attempt"):
        CycleResult(device_id=DEVICE_ID, attempts=(), duration_s=0.0)


def test_an_attempt_exposes_its_outcome_both_ways() -> None:
    reading = PowerReading(device_id=DEVICE_ID, watts=1.0, measured_at=MEASURED_AT)
    succeeded = PollAttempt(index=1, result=reading, duration_s=0.1)
    failed = PollAttempt(index=2, result=failure(FailureClassification.TIMEOUT), duration_s=0.2)

    assert succeeded.reading is reading
    assert succeeded.failure is None
    assert succeeded.classification is None
    assert failed.reading is None
    assert failed.failure is not None
    assert failed.classification is FailureClassification.TIMEOUT


async def test_the_default_sink_accepts_everything() -> None:
    """An executor is usable before the publisher exists, which is how this task ships before task 12."""
    clock = VirtualClock()
    executor = PollCycleExecutor(clock=clock.monotonic, sleep=clock.sleep, timeout=clock.timeout)

    assert isinstance(NullCycleSink(), NullCycleSink)
    assert (await executor.run_cycle(make_device(ScriptedPlugin(clock=clock)))).succeeded
