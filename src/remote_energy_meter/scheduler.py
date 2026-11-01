"""The Poll_Scheduler.

This module is built in three sections, added by three tasks and kept apart on purpose:

1. **Schedule resolution** (below, task 11.1) — pure functions from data to data that decide how often a device is
   polled (Requirements 5.2, 5.7) and how long one attempt may take (Requirement 5.8).
2. **The poll cycle executor** (task 11.2) — attempts, timeouts, retry policy, and classification at the scheduler
   boundary.
3. **The per-device loop** (task 11.3) — one task per device under a ``TaskGroup``, with an injected clock.

Section 1 stays free of any clock, any I/O, and any logging. That is not tidiness: Properties 14 and 15 quantify over
the whole interval and timeout space, and a function that consulted a clock or emitted a record could not be run
100+ times per property in milliseconds. So a resolution that *rejects* a configured value returns the rejected value
for the caller to log, rather than logging it here. The record the requirements ask for (Requirement 5.7) names the
Device_Entry identifier as well, which these functions are not given and do not need.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Final, Protocol, runtime_checkable

from remote_energy_meter.config.models import MAX_POLL_INTERVAL_S, MIN_POLL_INTERVAL_S
from remote_energy_meter.diagnostics.logger import DEVICE_ID_FIELD, get_logger, log_read_failure
from remote_energy_meter.plugins.interface import (
    FailureClassification,
    PowerReading,
    ReadFailure,
    ReadResult,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    import logging
    from collections.abc import Sequence

    from remote_energy_meter.config.runtime import ResolvedDevice

__all__ = [
    "MAX_ATTEMPTS_PER_CYCLE",
    "MAX_FALLBACK_TIMEOUT_S",
    "RETRY_DELAYS_S",
    "TIMEOUT_INTERVAL_DIVISOR",
    "Clock",
    "CycleResult",
    "CycleSink",
    "IntervalResolution",
    "NullCycleSink",
    "PollAttempt",
    "PollCycleExecutor",
    "Sleeper",
    "TimeoutFactory",
    "next_retry_delay",
    "resolve_interval",
    "resolve_timeout",
    "whole_milliseconds",
]

# --------------------------------------------------------------------------------------------------
# Section 1: schedule resolution (Requirements 5.2, 5.7, 5.8)
# --------------------------------------------------------------------------------------------------

#: Requirement 5.8: a device that omits a request timeout gets one third of its poll interval, capped.
TIMEOUT_INTERVAL_DIVISOR: Final = 3
MAX_FALLBACK_TIMEOUT_S: Final = 10.0


@dataclass(frozen=True, slots=True)
class IntervalResolution:
    """Which poll interval a device got, and which declared value was refused to get there.

    Refines the design's ``(interval, was_rejected)`` tuple into named fields, because the caller needs the rejected
    *value* — not merely the fact of a rejection — to write the record Requirement 5.7 asks for, and reading it back
    out of the argument it just passed in is how call sites drift.

    Attributes:
        interval_s: The interval to poll this device at, in whole seconds.
        rejected_value: The device-specific interval that was present but not used, or ``None`` when no device-specific
            interval was present or the one present was used.
    """

    interval_s: int
    rejected_value: int | None = None

    @property
    def rejected(self) -> bool:
        """Whether a present device-specific interval was refused, so the caller owes one log record."""
        return self.rejected_value is not None


def resolve_interval(entry_interval_s: int | None, global_interval_s: int) -> IntervalResolution:
    """Resolve one device's poll interval (Requirements 5.2, 5.7).

    The device-specific interval wins if and only if it is present and within 5-3600 seconds inclusive. Otherwise the
    global interval is used, and a present-but-refused value comes back as
    :attr:`IntervalResolution.rejected_value` for the caller to name in one log record alongside the Device_Entry
    identifier.

    The rejection branch is a **second line of defence, not a production path**: the Configuration_Parser already
    excludes an entry whose ``poll_interval_s`` falls outside 5-3600, per the explicit clause in Requirement 2.2, and
    :class:`~remote_energy_meter.config.models.DeviceEntry` carries the same bounds, so a ``DeviceEntry`` holding an
    out-of-range interval cannot be constructed. The branch exists because Requirement 5.7 states the behaviour
    independently of how an entry reached the scheduler, and because a caller assembling a ``ResolvedDevice`` from
    somewhere other than the parser — a test, a future configuration source — should degrade to the global interval
    rather than poll a device every second.

    The global interval is used as given. It is validated by the Configuration_Object (Requirement 2.7, fatal to the
    whole configuration when out of range), so re-clamping it here would only mask a caller that skipped validation.

    :param entry_interval_s: the device-specific interval, or ``None`` when the entry omits one.
    :param global_interval_s: the Configuration_Object's global poll interval, in whole seconds.
    :returns: the resolved interval, plus the refused device value when there was one.
    """
    if entry_interval_s is None:
        return IntervalResolution(interval_s=global_interval_s)
    if MIN_POLL_INTERVAL_S <= entry_interval_s <= MAX_POLL_INTERVAL_S:
        return IntervalResolution(interval_s=entry_interval_s)
    return IntervalResolution(interval_s=global_interval_s, rejected_value=entry_interval_s)


def resolve_timeout(entry_timeout_s: float | None, interval_s: int) -> float:
    """Resolve how long one power reading attempt for a device may take (Requirement 5.8).

    The entry's request timeout when it is present; otherwise ``min(interval_s / 3, 10.0)`` seconds. A third of the
    interval leaves room for the two retries of Requirement 5.5 inside one cycle, and the 10 second cap keeps a device
    polled hourly from stalling for twenty minutes on one unreachable host.

    **Present means "the entry said so", not "the resolved parameter model has a value".** A plugin's schema may
    default its own timeout field — ``IdracParams.request_timeout_s`` defaults to 10.0 — so a validated parameter model
    always carries a number and cannot distinguish a configured 10 seconds from an omission. The caller must therefore
    pass ``None`` when the entry omitted the field, taking the distinction from the entry's raw parameter mapping,
    which the parser deliberately keeps unnormalized::

        declared = entry.params.get("request_timeout_s")

    or equivalently from the validated model's explicitly-set field names::

        declared = params.request_timeout_s if "request_timeout_s" in params.model_fields_set else None

    Which key holds the timeout is a plugin's own business — the core never learns a plugin's field names — so the
    caller that knows the plugin extracts the value and this function only applies the rule.

    The supplied timeout is used as given, and is not capped or compared against the interval: it came through the
    plugin's published schema, which is where that plugin's accepted range is declared (Requirement 4.1 bounds the
    iDRAC timeout to 1-60 seconds). The cycle budget of Requirement 5.5 is what stops an over-long timeout from
    bleeding into the next scheduled poll.

    :param entry_timeout_s: the timeout the entry declared, or ``None`` when it declared none.
    :param interval_s: the device's resolved poll interval, in whole seconds.
    :returns: the per-attempt request timeout in seconds.
    """
    if entry_timeout_s is not None:
        return entry_timeout_s
    return min(interval_s / TIMEOUT_INTERVAL_DIVISOR, MAX_FALLBACK_TIMEOUT_S)


# --------------------------------------------------------------------------------------------------
# Section 2: the poll cycle executor (Requirements 3.7, 5.4, 5.5, 5.6, 9.4)
# --------------------------------------------------------------------------------------------------
#
# One *cycle* is everything the scheduler does for one device at one scheduled time: up to three power reading
# attempts under a wall-clock budget, then exactly one downstream report and exactly one debug record. The per-device
# loop that decides *when* a cycle runs, and the lock that keeps two cycles for one device from overlapping, belong to
# Section 3 (task 11.3). Splitting it there keeps the interesting behaviour -- classification and the retry schedule --
# reachable from a test that calls one coroutine, rather than only from a running loop.
#
# Three deliberate boundaries.
#
# **The scheduler owns retry, timeout, and classification; the plugin owns one request.** A plugin receives a timeout
# and returns one result (see ``plugins.interface``). So the timeout is applied *here*, around the call, and an
# exception that escapes a plugin anyway is converted here into a plugin-classified failure (Requirement 3.7). A
# third-party plugin that raises, hangs, or returns nonsense degrades one device for one cycle; the loop and the
# process keep running.
#
# **The clock, the sleep function, and the timeout are injected.** Requirement 5.5 fixes retry delays of 1 s and 2 s
# under a budget equal to the poll interval, which is up to 3600 s. Properties 16, 17, and 18 quantify over that whole
# space, so a test has to be able to run a cycle with a 3600 s budget in microseconds. Reaching for
# ``time.monotonic``/``asyncio.sleep``/``asyncio.timeout`` directly would make those properties either slow or
# untestable, and a scheduler whose timing cannot be tested is a scheduler whose timing is not known to be right.
# Production wiring passes nothing and gets exactly the three the design names.
#
# **The publisher and the accumulator sit behind :class:`CycleSink`.** The executor never imports either one. That is
# what lets Requirement 5.6 be stated precisely -- a fully failed cycle calls :meth:`CycleSink.report_unavailable` and
# never calls :meth:`CycleSink.deliver_reading`, so "passes no Power_Reading to the Energy_Accumulator" is a property
# of the call sequence rather than a claim about a collaborator's internals.

#: Requirement 5.5: the delay before the first retry, then before the second. The length of this tuple *is* the retry
#: allowance, so :data:`MAX_ATTEMPTS_PER_CYCLE` is derived rather than restated.
RETRY_DELAYS_S: Final[tuple[float, ...]] = (1.0, 2.0)

#: One initial attempt plus one retry per entry in :data:`RETRY_DELAYS_S` (Requirement 5.5).
MAX_ATTEMPTS_PER_CYCLE: Final = 1 + len(RETRY_DELAYS_S)

type Clock = Callable[[], float]
"""A monotonic clock reading, in seconds. ``time.monotonic`` in production, a virtual clock in a test."""

type Sleeper = Callable[[float], Awaitable[None]]
"""Suspends for a number of seconds. ``asyncio.sleep`` in production, a virtual clock's advance in a test."""

type TimeoutFactory = Callable[[float], AbstractAsyncContextManager[Any]]
"""Bounds the block it wraps, raising :class:`TimeoutError` when the deadline passes. ``asyncio.timeout`` in
production, a virtual deadline in a test."""


def whole_milliseconds(seconds: float) -> int:
    """Convert a duration in seconds to whole milliseconds (Requirements 9.4, 9.5).

    One conversion, used by the debug record and by the duration handed to :class:`CycleSink`, because Property 36
    requires the logged duration and the published diagnostic attribute to be the *same* number. Two call sites each
    doing their own arithmetic is how those two numbers come to differ in the last millisecond.

    A negative duration cannot arise from a monotonic clock but is clamped to zero rather than reported, because a
    negative millisecond count in a log record says nothing an administrator can act on.
    """
    return max(0, round(seconds * 1000.0))


def next_retry_delay(
    attempt_count: int,
    elapsed_s: float,
    interval_s: float,
    classification: FailureClassification,
) -> float | None:
    """How long to wait before the next attempt, or ``None`` when the cycle is over (Requirement 5.5).

    Pure, and the whole retry policy in one place:

    * an authentication failure is **never** retried. Repeating a credential that was just refused cannot succeed, and
      doing it twice more every poll interval walks an iDRAC service account toward a lockout -- which would take out
      the metering the retry was meant to protect;
    * every other classification is retried up to twice, waiting 1 s then 2 s (:data:`RETRY_DELAYS_S`);
    * no attempt is *started* once the elapsed cycle time for this device, retry delays included, would reach its poll
      interval. The check is made before sleeping, so the budget bounds the sleep as well as the attempt -- a cycle
      that stopped after sleeping into the next scheduled time would have spent the budget to learn nothing.

    The budget comparison is strict (``<``): Requirement 5.5 stops once the elapsed time *reaches* the interval, so an
    attempt that would begin exactly at the next scheduled time does not begin.

    :param attempt_count: how many attempts have already completed in this cycle; 1 or more.
    :param elapsed_s: seconds since the cycle began, measured on the scheduler's clock.
    :param interval_s: this device's resolved poll interval, which is also the cycle's wall-clock budget.
    :param classification: why the attempt just completed failed.
    :returns: the delay to wait before the next attempt, or ``None`` to end the cycle.
    """
    if attempt_count < 1:
        raise ValueError(f"a retry decision needs at least one completed attempt: {attempt_count!r}")
    if classification is FailureClassification.AUTHENTICATION:
        return None
    if attempt_count >= MAX_ATTEMPTS_PER_CYCLE:
        return None
    delay_s = RETRY_DELAYS_S[attempt_count - 1]
    if elapsed_s + delay_s >= interval_s:
        return None
    return delay_s


@dataclass(frozen=True, slots=True)
class PollAttempt:
    """One power reading attempt and what it cost.

    Attributes:
        index: 1 for the initial attempt, 2 and 3 for the retries.
        result: the reading, or the classified failure. Never an exception: an exception raised by a plugin is
            converted at the scheduler boundary (Requirement 3.7), so nothing downstream has to handle both shapes.
        duration_s: how long the attempt took, on the scheduler's clock. For a timeout this is the request timeout,
            because that is when the attempt was abandoned (Requirement 5.4).
    """

    index: int
    result: ReadResult
    duration_s: float

    @property
    def reading(self) -> PowerReading | None:
        """The reading this attempt produced, or ``None`` when it failed."""
        return self.result if isinstance(self.result, PowerReading) else None

    @property
    def failure(self) -> ReadFailure | None:
        """The classified failure this attempt produced, or ``None`` when it succeeded."""
        return self.result if isinstance(self.result, ReadFailure) else None

    @property
    def classification(self) -> FailureClassification | None:
        """Why this attempt failed, or ``None`` when it succeeded."""
        failure = self.failure
        return None if failure is None else failure.classification


@dataclass(frozen=True, slots=True)
class CycleResult:
    """Everything one poll cycle did, for the caller to act on and for a test to assert against.

    Returned rather than only acted upon, because Section 3's loop needs the outcome (to decide nothing, currently, but
    to log and to keep its schedule) and because the retry and duration properties are far easier to state over a value
    than over a sequence of observed side effects.

    Attributes:
        device_id: the Device_Entry identifier this cycle polled.
        attempts: every attempt made, in order. Never empty -- a cycle always makes its initial attempt.
        duration_s: the whole cycle, attempts and retry delays included.
        retry_delays_s: the delays actually waited, so a test can assert the 1 s then 2 s schedule of Requirement 5.5
            without reading the clock.
    """

    device_id: str
    attempts: tuple[PollAttempt, ...]
    duration_s: float
    retry_delays_s: tuple[float, ...] = ()

    def __post_init__(self) -> None:
        if not self.attempts:
            raise ValueError("a poll cycle always makes at least one attempt")

    @property
    def final(self) -> PollAttempt:
        """The attempt that ended the cycle, whose result is the cycle's outcome."""
        return self.attempts[-1]

    @property
    def reading(self) -> PowerReading | None:
        """The reading this cycle obtained, or ``None`` when every attempt failed (Requirement 5.6)."""
        return self.final.reading

    @property
    def failure(self) -> ReadFailure | None:
        """The failure that ended the cycle, or ``None`` when the cycle succeeded."""
        return self.final.failure

    @property
    def succeeded(self) -> bool:
        """Whether this cycle obtained a Power_Reading."""
        return self.reading is not None

    @property
    def attempt_count(self) -> int:
        """How many attempts this cycle made; 1 to :data:`MAX_ATTEMPTS_PER_CYCLE`."""
        return len(self.attempts)

    @property
    def classifications(self) -> tuple[FailureClassification, ...]:
        """The classification of every failed attempt, in order."""
        return tuple(attempt.classification for attempt in self.attempts if attempt.classification is not None)

    @property
    def operation_duration_ms(self) -> int:
        """The duration of the *final* attempt in whole milliseconds (Requirements 9.4, 9.5).

        The operation duration, not the cycle duration: Requirement 9.5 publishes "the duration of that operation" as
        a diagnostic attribute of a successful read, and reporting a number inflated by two retry sleeps would make
        that attribute useless for spotting a slow iDRAC.
        """
        return whole_milliseconds(self.final.duration_s)

    @property
    def duration_ms(self) -> int:
        """The whole cycle in whole milliseconds, attempts and retry delays included."""
        return whole_milliseconds(self.duration_s)


@runtime_checkable
class CycleSink(Protocol):
    """Where a completed cycle goes: the Energy_Accumulator and the MQTT_Publisher, behind two method names.

    The executor imports neither collaborator. It calls exactly one of these two methods per cycle, which is precisely
    the shape Requirement 5.6 asks for: a cycle that obtained no reading reports the device unavailable and passes no
    Power_Reading on, so nothing reaches the accumulator to be integrated and nothing reaches the publisher to be
    published as a state.

    Both methods are coroutines because the publisher's are; a synchronous accumulator is wrapped by whichever
    composite the application wires up. Neither may raise as part of normal operation -- a raise is contained by the
    executor and reported, because a broker hiccup or an unregistered device must not end a device's poll loop.
    """

    async def deliver_reading(self, device: ResolvedDevice, reading: PowerReading, duration_ms: int) -> None:
        """Hand a successful reading to the accumulator and the publisher.

        ``duration_ms`` is the operation duration Requirement 9.5 publishes as a diagnostic attribute, and is the same
        number the cycle's debug record carries (Property 36).
        """
        ...

    async def report_unavailable(self, device: ResolvedDevice, failure: ReadFailure) -> None:
        """Report that every attempt in this cycle failed (Requirements 5.6, 6.8).

        ``failure`` is the classified failure that ended the cycle, so the publisher can name it without re-deriving
        it.
        """
        ...


class NullCycleSink:
    """A sink that accepts everything and does nothing.

    The default, so an executor can be constructed and exercised before the publisher exists, and so a test that cares
    only about retry behaviour does not have to supply a collaborator it will not inspect.
    """

    async def deliver_reading(self, device: ResolvedDevice, reading: PowerReading, duration_ms: int) -> None:
        """Discard the reading."""

    async def report_unavailable(self, device: ResolvedDevice, failure: ReadFailure) -> None:
        """Discard the report."""


class PollCycleExecutor:
    """Runs one poll cycle for one device: attempts, timeouts, retries, classification, report, and record.

    Stateless with respect to devices -- every device's cycle goes through the same instance, and nothing about one
    device is remembered between cycles. Per-device state that does persist (the cumulative energy total, the last
    published availability) lives with the collaborator that owns it, behind :class:`CycleSink`.
    """

    __slots__ = ("_clock", "_log", "_sink", "_sleep", "_timeout")

    def __init__(
        self,
        *,
        sink: CycleSink | None = None,
        clock: Clock = time.monotonic,
        sleep: Sleeper = asyncio.sleep,
        timeout: TimeoutFactory = asyncio.timeout,
        logger: logging.Logger | None = None,
    ) -> None:
        """Wire the executor.

        :param sink: where completed cycles are reported; a :class:`NullCycleSink` by default.
        :param clock: the monotonic clock used for the cycle budget and for durations. ``time.monotonic`` by default,
            never ``time.time``: a clock that can step backwards would produce negative durations and a budget that
            never expires.
        :param sleep: how a retry delay is waited out. ``asyncio.sleep`` by default.
        :param timeout: how one attempt is bounded. ``asyncio.timeout`` by default, which cancels the attempt at the
            deadline and surfaces :class:`TimeoutError` (Requirement 5.4).
        :param logger: where the debug and failure records go; this module's logger by default.
        """
        self._sink: CycleSink = sink if sink is not None else NullCycleSink()
        self._clock = clock
        self._sleep = sleep
        self._timeout = timeout
        self._log = logger if logger is not None else get_logger(__name__)

    async def run_cycle(self, device: ResolvedDevice) -> CycleResult:
        """Run one complete poll cycle for ``device`` and return what it did.

        Attempts run under this device's request timeout until one succeeds or :func:`next_retry_delay` ends the cycle.
        Each failed attempt gets exactly one warning record naming the device and one of the four logged
        classifications (Requirement 9.3). The completed cycle is then reported once through the sink and gets exactly
        one debug record (Requirement 9.4).

        Never raises for anything a device or a plugin did: a failure is classified, reported, and logged.
        :class:`asyncio.CancelledError` *does* propagate, so a SIGTERM arriving mid-read cancels the read and lets
        shutdown keep to the 10 second budget of Requirement 1.5 instead of waiting out two retries first.
        """
        started_at = self._clock()
        attempts: list[PollAttempt] = []
        delays: list[float] = []

        while True:
            result, duration_s = await self._attempt(device)
            attempts.append(PollAttempt(index=len(attempts) + 1, result=result, duration_s=duration_s))
            if isinstance(result, PowerReading):
                break

            log_read_failure(self._log, device.device_id, result.classification, result.detail)
            delay_s = next_retry_delay(
                attempt_count=len(attempts),
                elapsed_s=self._clock() - started_at,
                interval_s=device.interval_s,
                classification=result.classification,
            )
            if delay_s is None:
                break
            delays.append(delay_s)
            await self._sleep(delay_s)

        cycle = CycleResult(
            device_id=device.device_id,
            attempts=tuple(attempts),
            duration_s=self._clock() - started_at,
            retry_delays_s=tuple(delays),
        )

        # Reported before the record is written: the publication deadlines are the tight ones (2 s in Requirements
        # 6.4, 6.7, 6.8), while Requirement 9.4 sets none for the cycle record.
        await self._report(device, cycle)
        self._log_cycle(device, cycle)
        return cycle

    # -- one attempt -------------------------------------------------------------------------------

    async def _attempt(self, device: ResolvedDevice) -> tuple[ReadResult, float]:
        """Make one power reading attempt, returning its result and its duration in seconds.

        Every way an attempt can end is turned into a :class:`ReadResult` here, at the boundary, so the retry loop
        above reads one classification rather than juggling three exception shapes:

        * the deadline passed: the attempt is abandoned and classified as a timeout (Requirements 4.7, 5.4);
        * the plugin raised: classified as a plugin failure, with the exception recorded (Requirement 3.7);
        * the plugin returned: taken as given, after the identity check in :meth:`_classified`.

        :class:`asyncio.CancelledError` is re-raised rather than classified. It is not a device failure -- it is the
        process being asked to stop -- and converting it would swallow a shutdown request and leave the cycle running.
        The re-raise must come before ``except Exception`` in source order to survive a future refactor that widens
        the latter; it also has to come before ``except TimeoutError``, because ``asyncio.timeout`` only rewrites a
        cancellation caused by *its own* deadline.
        """
        started_at = self._clock()
        try:
            async with self._timeout(device.timeout_s):
                returned = await device.plugin.read_power(device.timeout_s)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            result: ReadResult = ReadFailure(
                device_id=device.device_id,
                classification=FailureClassification.TIMEOUT,
                detail=f"no response within {device.timeout_s:g} s",
            )
        except Exception as error:
            # At error severity with the traceback attached, because a raising plugin is a defect that needs a stack
            # to fix. The warning record Requirement 9.3 asks for is written once by the caller, from the classified
            # failure, so this does not double-report at warning severity.
            self._log.exception(
                "plugin %s raised during a power reading",
                device.plugin_id,
                extra={DEVICE_ID_FIELD: device.device_id},
            )
            result = ReadFailure(
                device_id=device.device_id,
                classification=FailureClassification.PLUGIN,
                detail=f"{type(error).__name__}: {error}",
            )
        else:
            result = self._classified(device, returned)
        return result, self._clock() - started_at

    def _classified(self, device: ResolvedDevice, returned: object) -> ReadResult:
        """Accept what a plugin returned, or classify it as a plugin failure.

        ``returned`` is typed ``object`` on purpose. The annotation on
        :meth:`~remote_energy_meter.plugins.interface.DevicePlugin.read_power` is a promise a third-party plugin makes,
        not one the core can rely on, and a plugin that returns ``None`` would otherwise fail somewhere further
        downstream with no indication of which plugin caused it.

        A reading carrying a *different* device identifier is refused rather than passed on: the accumulator keys
        cumulative totals by ``reading.device_id``, so accepting it would let a buggy plugin add another device's
        energy to this one's total -- exactly the cross-device contamination Requirement 3.6 exists to prevent. A
        *failure* carrying the wrong identifier is harmless by comparison and is simply relabelled, so the entry that
        actually failed is the one reported unavailable.
        """
        if isinstance(returned, PowerReading):
            if returned.device_id != device.device_id:
                return ReadFailure(
                    device_id=device.device_id,
                    classification=FailureClassification.PLUGIN,
                    detail=f"plugin {device.plugin_id} returned a reading for {returned.device_id!r}",
                )
            return returned
        if isinstance(returned, ReadFailure):
            if returned.device_id != device.device_id:
                return replace(returned, device_id=device.device_id)
            return returned
        return ReadFailure(
            device_id=device.device_id,
            classification=FailureClassification.PLUGIN,
            detail=f"plugin {device.plugin_id} returned {type(returned).__name__}, not a reading or a failure",
        )

    # -- reporting ---------------------------------------------------------------------------------

    async def _report(self, device: ResolvedDevice, cycle: CycleResult) -> None:
        """Report a completed cycle downstream exactly once (Requirement 5.6)."""
        reading = cycle.reading
        failure = cycle.failure
        if reading is not None:
            await self._guarded(
                device,
                "reading",
                self._sink.deliver_reading(device, reading, cycle.operation_duration_ms),
            )
        elif failure is not None:
            await self._guarded(device, "unavailability", self._sink.report_unavailable(device, failure))

    async def _guarded(self, device: ResolvedDevice, subject: str, call: Awaitable[None]) -> None:
        """Await a sink call, containing any failure of it to this one cycle.

        A sink failure is a collaborator's problem, not this device's: the publisher owns its own reconnection
        (Requirement 6.9), and an accumulator asked about a device it never registered raises. Letting either escape
        would end the device's poll loop permanently over something a later cycle would very likely get right, so it is
        recorded with its traceback and the cycle completes.
        """
        try:
            await call
        except asyncio.CancelledError:
            raise
        except Exception:
            self._log.exception("failed to report %s downstream", subject, extra={DEVICE_ID_FIELD: device.device_id})

    def _log_cycle(self, device: ResolvedDevice, cycle: CycleResult) -> None:
        """Write the one debug record a completed cycle owes (Requirement 9.4).

        Exactly one record per polled device per cycle, naming the identifier, the power value in watts, and the
        operation duration in whole milliseconds. The values go in ``extra`` as well as into the message so a log
        consumer can read them as fields; the message carries them too, because an administrator reading the addon log
        in Supervisor is the primary audience.

        ``watts`` is present but ``None`` for a failed cycle rather than omitted, so a consumer counting records per
        cycle sees the same field set either way.
        """
        reading = cycle.reading
        self._log.debug(
            "poll cycle complete: %s in %d ms over %d attempt(s)",
            "no reading" if reading is None else f"{reading.watts:.1f} W",
            cycle.operation_duration_ms,
            cycle.attempt_count,
            extra={
                DEVICE_ID_FIELD: device.device_id,
                "watts": None if reading is None else reading.watts,
                "duration_ms": cycle.operation_duration_ms,
                "cycle_duration_ms": cycle.duration_ms,
                "attempts": cycle.attempt_count,
            },
        )


# --------------------------------------------------------------------------------------------------
# the per-device loop (Requirements 5.1, 5.3, 5.9)
# --------------------------------------------------------------------------------------------------


@runtime_checkable
class CycleRunner(Protocol):
    """What the scheduler needs from whatever runs a cycle.

    A protocol so the loop's timing behaviour can be tested without the executor, and so the
    application can substitute a recorder rather than starting real polling inside a unit test.
    """

    async def run_cycle(self, device: ResolvedDevice) -> CycleResult:
        """Run one complete poll cycle for ``device``."""
        ...


class PollScheduler:
    """One task per accepted device, all supervised by a single :class:`asyncio.TaskGroup`.

    Per-device tasks rather than one global tick: Requirement 5.2 allows each device its own interval,
    and Requirement 5.9 makes overrun a per-device decision. A device that takes longer than its
    interval skips its own next cycle without delaying anyone else's.

    The scheduled times advance by a fixed increment rather than by "now plus interval". The
    difference matters over a long run: restarting the clock from each wake-up adds every cycle's
    scheduling error into the next deadline, so a device polled every 30 s slowly drifts. Adding the
    interval to the previous deadline keeps the schedule anchored to the start of the run.
    """

    __slots__ = ("_clock", "_devices", "_executor", "_log", "_sleep")

    def __init__(
        self,
        devices: Sequence[ResolvedDevice],
        *,
        executor: CycleRunner | None = None,
        clock: Clock = time.monotonic,
        sleep: Sleeper = asyncio.sleep,
        logger: logging.Logger | None = None,
    ) -> None:
        """Wire the scheduler.

        :param devices: the accepted, resolved devices. One task each.
        :param executor: runs a single cycle; a default :class:`PollCycleExecutor` when omitted.
        :param clock: monotonic clock for the schedule. Never ``time.time``: a clock that steps
            backwards would make a deadline unreachable.
        :param sleep: how the wait until the next deadline is spent. Injected so a test drives the
            schedule on a virtual clock instead of waiting real seconds.
        :param logger: where the skip records go; this module's logger by default.
        """
        self._devices = tuple(devices)
        self._executor: CycleRunner = executor if executor is not None else PollCycleExecutor(logger=logger)
        self._clock = clock
        self._sleep = sleep
        self._log = logger if logger is not None else get_logger(__name__)

    async def run(self) -> None:
        """Run every device's loop until cancelled.

        Returns only when cancelled, or immediately when there are no devices — a configuration with
        no accepted entries is a fatal condition the caller has already handled, so there is nothing
        to supervise here.

        :class:`asyncio.CancelledError` propagates, which is what lets shutdown keep to the 10 second
        budget of Requirement 1.5: the task group cancels every device loop, and a loop parked in
        ``sleep`` stops immediately rather than finishing its interval.
        """
        if not self._devices:
            return
        async with asyncio.TaskGroup() as group:
            for device in self._devices:
                group.create_task(self._device_loop(device), name=f"poll-{device.device_id}")

    async def _device_loop(self, device: ResolvedDevice) -> None:
        """Poll one device forever on a fixed schedule (Requirements 5.1, 5.3, 5.9)."""
        # Requirement 5.1: the first deadline is now, so the first request goes out immediately
        # rather than after one interval.
        next_at = self._clock()
        while True:
            await self._sleep_until(next_at)
            scheduled_at = next_at
            next_at += device.interval_s

            # Requirement 5.3. Checked rather than awaited: waiting would run the skipped cycle
            # late instead of skipping it, which is how a slow device turns an interval into a
            # queue. This loop is the only routine holder, so a contended lock means another
            # caller -- a manual trigger or a shutdown step -- is mid-cycle for this device.
            if device.lock.locked():
                self._skipped(device, scheduled_at)
                continue

            async with device.lock:
                await self._executor.run_cycle(device)

            # Requirement 5.9. The cycle is awaited inline, so an overrun cannot overlap the next
            # one -- but it does leave deadlines behind, and those are dropped rather than run
            # back to back. Each is recorded, so an administrator can see the interval is not
            # being met instead of inferring it from gaps in the data.
            now = self._clock()
            while next_at <= now:
                self._skipped(device, next_at)
                next_at += device.interval_s

    def _skipped(self, device: ResolvedDevice, scheduled_at: float) -> None:
        """One record naming the device and the scheduled time that was not run."""
        self._log.info(
            "poll cycle skipped, previous cycle still running: %s",
            device.device_id,
            extra={DEVICE_ID_FIELD: device.device_id, "skipped_at": scheduled_at},
        )

    async def _sleep_until(self, deadline: float) -> None:
        """Wait until ``deadline`` on the injected clock, or return at once if it has passed."""
        remaining = deadline - self._clock()
        if remaining > 0:
            await self._sleep(remaining)
