"""Property 13: the per-device poll loop keeps a drift-free schedule (task 11.4).

*For any* poll interval in 5-3600 seconds and any number of cycles k, the k-th power reading request
for a device begins within 1 second of ``start + k x interval``, so scheduling error does not
accumulate across cycles (Requirement 5.1).

## What this module covers, and what belongs elsewhere

This module is scoped to the **per-device loop** of ``scheduler.py`` Section 3 -- the part that decides
*when* a cycle runs. Property 13 lives here, and so does Property 19 (task 11.10, overrunning cycles
skipping exactly the elapsed scheduled times), which is the same subject seen from the other side: a
read longer than the interval.

The remaining scheduler properties are about the **cycle executor** -- what happens inside one cycle,
not when it starts -- and belong in a sibling cycle module: Property 16 (cycle duration, task 11.7),
Property 17 (the retry schedule, task 11.8), Property 18 (a fully failed cycle's report, task 11.9),
and Property 7 (device fault isolation, task 11.11). Properties 14 and 15, the pure resolution
functions, are already in ``test_scheduler_resolution_properties.py``. The split mirrors the unit
layer's ``test_scheduler_loop.py`` / ``test_scheduler_cycle.py`` divide, and for the same reason: a
loop property needs a clock and a driven event loop, and a cycle property does not.

## The virtual clock, and why there is no real sleeping

The spec's testing strategy requires the timing-sensitive properties to run against a virtual clock,
and the scheduler takes its clock and sleep function by injection for exactly that reason. This module
uses that seam: :class:`VirtualClock` returns a number that advances *only* when the loop sleeps or a
simulated read holds it, so a 3600 second interval over forty cycles is 41 hours of simulated time and
a handful of event-loop turns of real time. Nothing here calls :func:`asyncio.sleep` with a non-zero
delay. The harness is the one already used by ``tests/unit/test_scheduler_loop.py``, kept local to this
module so it stands alone, with two changes for the property setting:

* **termination is by sentinel, not by turn budget.** The unit harness spins the event loop a fixed
  number of turns and then cancels. A generated scenario can ask for forty cycles, and a turn budget
  large enough for that would be an invitation to a test that passes because it gave up early. The
  simulated read raises :class:`_EnoughCycles` once it has recorded the requested number of cycles, so
  the run ends when the property has its data and at no other point;
* **the clock's start is generated.** ``time.monotonic`` is boot-relative, so a real start reads
  anywhere from seconds to months of uptime. Anchoring every example at zero would let an
  implementation that scheduled from zero rather than from the start reading pass.

Only one device is driven per example. This clock advances by whichever task last slept, so two
concurrent loops interfere and any observed interval would measure the fake rather than the scheduler
-- the same limitation the unit module records. One device is all Property 13 quantifies over; per
device tasks are covered structurally at the unit layer.

## The generated space

* **the interval** -- the whole 5-3600 second range Requirement 5.1's cycle rate is stated over, with
  both bounds and the 30 second default sampled explicitly;
* **the number of cycles** -- 1 to 40. Drift is a property of a *run*, not of a call, so a single
  cycle is the degenerate case (only the first-request clause bites) and forty is enough that a
  per-cycle error of 25 ms would break the 1 second bound;
* **the read durations** -- a *list* of fractions of the interval, cycled through, so successive reads
  in one run take different amounts of simulated time. Fractions reach 0.99, which is what makes
  accumulation visible: a loop that scheduled the next cycle at "now plus one interval" instead of
  "the previous deadline plus one interval" falls behind by the whole read duration every cycle, so a
  read taking most of a 5 second interval puts it more than a second late by its second cycle. A run
  of all-zero fractions is the control case -- instantaneous reads, where the two schedules agree.

Fractions stay strictly below 1.0 on purpose. A read that outlasts its interval makes the loop *drop*
the scheduled times it slept through (Requirement 5.9), so the k-th executed cycle is no longer the
k-th scheduled one and Property 13's ``start + k x interval`` carries no expectation. That regime is
Property 19's subject. Here it is excluded by construction and the exclusion is checked, not assumed:
the property asserts that no skip record was written, which is what makes indexing the observed cycles
against the scheduled grid meaningful.

## The oracle

The expected schedule is ``start + k x interval`` computed by multiplication from the generated start,
straight out of Requirement 5.1 and the property statement. The implementation reaches the same grid
by repeated addition to the previous deadline; deriving the expectation the same way would assert only
that addition equals itself, and would agree with a drifting loop at every point.

Two bounds are asserted against it:

* **1 second**, the requirement's own tolerance, applied to the first request relative to the start
  and to every subsequent request relative to its scheduled time;
* **1 microsecond**, which the virtual clock earns. Real scheduling jitter is what Requirement 5.1's
  second of slack is for, and none of it exists here: the deadlines are integer sums of an integer
  interval, so the only error a drift-free loop can show is the last bit or two of a float addition --
  under 2 nanoseconds even at a hundred days of uptime. Tightening to a microsecond is what lets a
  three-cycle run catch a drift of a millisecond per cycle, rather than needing a thousand cycles for
  it to breach the requirement's bound.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import pairwise
from typing import TYPE_CHECKING, Any, Final, cast

from hypothesis import given, settings
from hypothesis import strategies as st

from remote_energy_meter.scheduler import PollScheduler

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterator, Sequence

    from remote_energy_meter.config.runtime import ResolvedDevice

#: The poll interval range Requirement 5.1's cycle rate is stated over, in whole seconds. Written out
#: rather than imported from the module under test, so a bound changed on one side is a disagreement
#: rather than a generator and an expectation moving together.
ACCEPTED_MIN_INTERVAL_S: Final = 5
ACCEPTED_MAX_INTERVAL_S: Final = 3600

#: The global interval when the Addon_Configuration omits it (Requirement 2.6), sampled as a notable
#: value only -- nothing in the expectation depends on it.
DEFAULT_GLOBAL_INTERVAL_S: Final = 30

#: Requirement 5.1's tolerance: the first request starts within this of Poll_Scheduler start, and every
#: subsequent request within this of its scheduled time.
TOLERANCE_S: Final = 1.0

#: The tighter bound the virtual clock earns. See the module docstring: with integer deadlines and no
#: real scheduling, a drift-free loop's error is a float rounding artefact under 2e-9 s, so a
#: microsecond is three orders of magnitude above the noise and six below the requirement's slack.
VIRTUAL_CLOCK_SLACK_S: Final = 1e-6

#: How many cycles one run is driven for. Forty is enough that an error of 25 ms per cycle breaches
#: :data:`TOLERANCE_S`; one is the degenerate run where only the first-request clause applies.
MIN_CYCLES: Final = 1
MAX_CYCLES: Final = 40

#: How many distinct read durations one run cycles through, so successive reads differ.
MAX_DISTINCT_HOLDS: Final = 8

#: The largest share of its interval a simulated read may take. Strictly below 1.0: a read that
#: outlasts its interval drops scheduled times (Requirement 5.9), which is Property 19's subject.
MAX_HOLD_FRACTION: Final = 0.99

#: Monotonic clock readings a real start could plausibly produce: a fresh boot, a few minutes, a day,
#: and a hundred days of uptime. Generated rather than fixed, so a schedule anchored at zero fails.
START_READINGS_S: Final[tuple[float, ...]] = (0.0, 1000.0, 86_400.0, 8_640_000.0)

#: One device per example, so its identifier is a constant. Shaped like a real one (Requirement 2.12).
DEVICE_ID: Final = "poweredge_a"

#: An isolated logger name, so configuring it cannot disturb the root logger or pytest's capture.
RECORDING_LOGGER_NAME: Final = "remote_energy_meter_property_scheduler_loop"

#: The message fragment the loop's skip record carries (Requirement 5.9).
SKIP_MESSAGE_FRAGMENT: Final = "poll cycle skipped"


# --------------------------------------------------------------------------------------------------
# log capture
# --------------------------------------------------------------------------------------------------


class _Recorder(logging.Handler):
    """Collects records rather than formatting them, so an assertion reads the record's own fields.

    ``caplog`` would be the obvious choice and cannot be used here: it is a function-scoped fixture,
    and Hypothesis reuses a function-scoped fixture across every example of one test, so the records
    of example *n* would still be present when example *n+1* counted them.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextmanager
def recording_logger() -> Iterator[tuple[logging.Logger, list[logging.LogRecord]]]:
    """A logger that captures every record handed to it, and nothing else."""
    logger = logging.getLogger(RECORDING_LOGGER_NAME)
    saved_level, saved_propagate = logger.level, logger.propagate
    logger.handlers.clear()
    logger.filters.clear()
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    recorder = _Recorder()
    logger.addHandler(recorder)
    try:
        yield logger, recorder.records
    finally:
        logger.removeHandler(recorder)
        logger.setLevel(saved_level)
        logger.propagate = saved_propagate


# --------------------------------------------------------------------------------------------------
# the virtual clock and the simulated device
# --------------------------------------------------------------------------------------------------


class _EnoughCycles(Exception):  # noqa: N818 - a control-flow sentinel that ends the run, not an error
    """Ends a run once the requested number of cycles has been observed.

    Raised from inside a simulated read, so it reaches the ``TaskGroup`` in
    :meth:`PollScheduler.run` and is re-raised there as an exception group. A sentinel rather than an
    outside cancellation after so many event-loop turns, because a turn budget that turned out too
    small would end the run early and the property would pass on the cycles it happened to get.
    """


class VirtualClock:
    """A monotonic clock that advances only when the loop sleeps, or when a simulated read holds it.

    The injected pair the scheduler needs: the instance itself is the clock, and :meth:`sleep` is the
    sleep function. A sleep of any length costs one event-loop turn, which is what lets an hour-long
    interval be waited out in microseconds of real time.
    """

    def __init__(self, start_s: float) -> None:
        self.now = start_s
        self.slept_s: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay_s: float) -> None:
        self.slept_s.append(delay_s)
        self.now += delay_s
        await asyncio.sleep(0)


class _FakeDevice:
    """Stands in for a ``ResolvedDevice``: the loop reads ``device_id``, ``interval_s``, and ``lock``.

    A real ``ResolvedDevice`` would need a validated entry and a plugin instance, neither of which the
    loop touches -- it hands the device straight to its cycle runner, which here is a recorder.
    """

    def __init__(self, interval_s: int) -> None:
        self.device_id = DEVICE_ID
        self.interval_s = interval_s
        self.lock = asyncio.Lock()


@dataclass(frozen=True, slots=True)
class ObservedCycle:
    """One cycle the loop actually ran, on the virtual clock.

    Attributes:
        device_id: which device was polled, so a cycle cannot be credited to the wrong loop.
        started_at: the clock reading when the request began -- the quantity Property 13 bounds.
        ended_at: the clock reading when the simulated read finished.
    """

    device_id: str
    started_at: float
    ended_at: float

    @property
    def duration_s(self) -> float:
        """How much simulated time this cycle spent."""
        return self.ended_at - self.started_at


class SimulatedReads:
    """A cycle runner that records when each cycle began and holds the clock for a generated duration.

    Holding the clock inside the cycle is what a slow read does to the schedule, and it is the whole
    mechanism by which drift would become observable: a loop that re-anchored its next deadline to the
    clock would fall behind by exactly the duration recorded here, every cycle.
    """

    def __init__(self, clock: VirtualClock, holds_s: Sequence[float], wanted: int) -> None:
        self._clock = clock
        self._holds_s = holds_s
        self._wanted = wanted
        self.cycles: list[ObservedCycle] = []

    async def run_cycle(self, device: Any) -> None:
        """Record this cycle's start, spend its generated duration, and stop the run when done."""
        started_at = self._clock.now
        # One turn before the clock moves, so the cycle is a genuine suspension point rather than a
        # straight-line call: a real cycle awaits a request here.
        await asyncio.sleep(0)
        self._clock.now += self._holds_s[len(self.cycles) % len(self._holds_s)]
        self.cycles.append(ObservedCycle(device_id=device.device_id, started_at=started_at, ended_at=self._clock.now))
        if len(self.cycles) >= self._wanted:
            raise _EnoughCycles


# --------------------------------------------------------------------------------------------------
# generators
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DriftScenario:
    """One run of one device's loop: how often, from when, for how many cycles, and how slow the reads.

    Attributes:
        interval_s: the device's resolved poll interval in whole seconds, as ``ResolvedDevice`` carries
            it.
        cycles: how many cycles the run is driven for.
        start_s: the clock reading at Poll_Scheduler start.
        hold_fractions: the share of the interval each read takes, cycled through across the run.
    """

    interval_s: int
    cycles: int
    start_s: float
    hold_fractions: tuple[float, ...]

    def holds_s(self) -> tuple[float, ...]:
        """The read durations in seconds, each strictly below one interval."""
        return tuple(fraction * self.interval_s for fraction in self.hold_fractions)

    def hold_for(self, index: int) -> float:
        """The duration of the read in cycle ``index``, counting from zero."""
        holds = self.holds_s()
        return holds[index % len(holds)]


def intervals() -> st.SearchStrategy[int]:
    """A poll interval across the whole 5-3600 second range, with both bounds and the default sampled.

    Whole seconds, because that is what ``ResolvedDevice.interval_s`` is: the interval reaches the loop
    from ``resolve_interval``, which returns an ``int``.
    """
    return st.one_of(
        st.integers(min_value=ACCEPTED_MIN_INTERVAL_S, max_value=ACCEPTED_MAX_INTERVAL_S),
        st.sampled_from([ACCEPTED_MIN_INTERVAL_S, DEFAULT_GLOBAL_INTERVAL_S, ACCEPTED_MAX_INTERVAL_S]),
    )


def cycle_counts() -> st.SearchStrategy[int]:
    """How many cycles one run covers: the whole 1-40 band, with the short runs sampled explicitly.

    One and two are worth drawing on their own. Drift is invisible in a single cycle and only just
    visible in two, so they are where a property that quietly measured nothing would still pass.
    """
    return st.one_of(
        st.integers(min_value=MIN_CYCLES, max_value=MAX_CYCLES),
        st.sampled_from([MIN_CYCLES, 2, MAX_CYCLES]),
    )


def start_readings() -> st.SearchStrategy[float]:
    """The clock reading at Poll_Scheduler start, from a fresh boot to a hundred days of uptime."""
    return st.sampled_from(START_READINGS_S)


def hold_fractions() -> st.SearchStrategy[tuple[float, ...]]:
    """Read durations as shares of the interval, one to eight of them, cycled through across a run.

    Drawn as a list rather than a single value so successive reads differ: a schedule that re-anchored
    to the clock would then fall behind by a *different* amount each cycle, which is exactly the shape
    a fixed increment is there to prevent. 0.9 and 0.99 are sampled explicitly -- a read taking nearly
    its whole interval is where accumulated error is largest without any scheduled time being dropped
    -- and 0.0 is the control case where a drifting loop and a fixed one agree.
    """
    fraction = st.one_of(
        st.floats(min_value=0.0, max_value=MAX_HOLD_FRACTION, allow_nan=False, allow_infinity=False),
        st.sampled_from([0.0, 0.5, 0.9, MAX_HOLD_FRACTION]),
    )
    return st.lists(fraction, min_size=1, max_size=MAX_DISTINCT_HOLDS).map(tuple)


def drift_scenarios() -> st.SearchStrategy[DriftScenario]:
    """A whole run: an interval, a cycle count, a start reading, and the read durations."""
    return st.builds(
        DriftScenario,
        interval_s=intervals(),
        cycles=cycle_counts(),
        start_s=start_readings(),
        hold_fractions=hold_fractions(),
    )


# --------------------------------------------------------------------------------------------------
# the expectation
# --------------------------------------------------------------------------------------------------


def expected_schedule(scenario: DriftScenario) -> tuple[float, ...]:
    """Requirement 5.1's scheduled times: ``start + k x interval`` for k from zero.

    Multiplication from the start, not repeated addition. The loop advances its deadline by adding one
    interval to the previous one, so an expectation built the same way would agree with it by
    construction -- including when the value being added had been contaminated by the last cycle's
    duration, which is the failure this property exists to catch.
    """
    return tuple(scenario.start_s + index * scenario.interval_s for index in range(scenario.cycles))


# --------------------------------------------------------------------------------------------------
# the driver
# --------------------------------------------------------------------------------------------------


async def _observe(scenario: DriftScenario, logger: logging.Logger) -> tuple[ObservedCycle, ...]:
    """Run ``scenario`` on a virtual clock and return the cycles the loop actually ran.

    Returns when the simulated read has recorded the requested number of cycles and raised
    :class:`_EnoughCycles`, which the scheduler's ``TaskGroup`` re-raises as an exception group.
    """
    clock = VirtualClock(scenario.start_s)
    device = cast("ResolvedDevice", _FakeDevice(scenario.interval_s))
    reads = SimulatedReads(clock, scenario.holds_s(), scenario.cycles)
    scheduler = PollScheduler(
        [device],
        executor=cast("Any", reads),
        clock=clock,
        sleep=clock.sleep,
        logger=logger,
    )
    try:  # noqa: SIM105 - contextlib.suppress does not catch except*, the TaskGroup's exception group
        await scheduler.run()
    except* _EnoughCycles:
        pass
    return tuple(reads.cycles)


# --------------------------------------------------------------------------------------------------
# the property
# --------------------------------------------------------------------------------------------------


# Feature: remote-energy-meter, Property 13: The poll schedule is drift-free
# Validates: Requirements 5.1
@settings(max_examples=150, deadline=None)
@given(scenario=drift_scenarios())
def test_the_poll_schedule_is_drift_free(scenario: DriftScenario) -> None:
    """Requirement 5.1, over the whole interval range and every run length up to forty cycles.

    Driven through ``asyncio.run`` from a synchronous test rather than as an async test, so each
    example gets its own event loop and the property does not depend on how a loop is shared with
    Hypothesis. Every second of the simulated schedule is virtual: an hour-long interval over forty
    cycles costs a few hundred event-loop turns and no waiting.
    """
    with recording_logger() as (logger, records):
        observed = asyncio.run(_observe(scenario, logger))

    scheduled = expected_schedule(scenario)

    # --- the run produced what it was driven for, on the one device it was given ---
    # Asserted first, because every bound below is vacuous over an empty or truncated run.
    assert len(observed) == scenario.cycles
    assert {cycle.device_id for cycle in observed} == {DEVICE_ID}

    # --- no scheduled time was dropped, so the k-th cycle observed is the k-th cycle scheduled ---
    # The generated reads all finish inside their interval, so Requirement 5.9's skip path must not
    # fire. Checked rather than assumed: if it did fire, the loop would have moved on to a later
    # deadline and indexing the observations against the grid below would compare the wrong pairs.
    skips = [record for record in records if SKIP_MESSAGE_FRAGMENT in record.getMessage()]
    assert skips == [], f"a read inside its interval dropped a scheduled time: {len(skips)} skip(s)"

    # --- the simulated reads really did spend time, so a drifting loop would have been seen ---
    # The guard against a vacuous property: if the clock did not move inside the cycles, "now plus one
    # interval" and "the previous deadline plus one interval" would be the same schedule and every
    # bound below would hold for a loop that drifts.
    #
    # Stated as an end reading rather than as a duration: ``ended_at - started_at`` is a float
    # subtraction that need not return the value that was added, so 4.95 s of simulated time comes back
    # as 4.950000000000045 and an exact comparison against the generated duration would fail on the
    # test's own arithmetic. Comparing the reading the harness produced against the same addition is
    # exact, and is the thing that matters: the clock advanced by the read duration.
    for index, cycle in enumerate(observed):
        hold_s = scenario.hold_for(index)
        assert cycle.ended_at == cycle.started_at + hold_s, (
            f"cycle {index} spent {cycle.duration_s:g} s of simulated time, not its {hold_s:g} s read"
        )

    # --- the first request starts within 1 second of Poll_Scheduler start (Requirement 5.1) ---
    assert abs(observed[0].started_at - scenario.start_s) <= TOLERANCE_S

    # --- and the k-th request within 1 second of start + k x interval, with no accumulation ---
    for index, (cycle, scheduled_at) in enumerate(zip(observed, scheduled, strict=True)):
        error_s = abs(cycle.started_at - scheduled_at)
        assert error_s <= TOLERANCE_S, f"cycle {index} began {error_s:g} s from its scheduled time"
        # Tightened to the virtual clock's noise floor. A loop whose error grows by the read duration
        # each cycle fails the requirement's bound too, but only once the accumulation exceeds a
        # second; here it fails at the first cycle that follows a read of a microsecond or more.
        assert error_s <= VIRTUAL_CLOCK_SLACK_S, f"cycle {index} drifted by {error_s:g} s"

    # --- one reading per poll interval, whatever the reads cost (Requirement 5.1) ---
    # The same statement read as a rate rather than as a grid: consecutive requests stay one interval
    # apart, so a read that took most of its interval does not shorten or stretch the next gap.
    gaps_s = [later.started_at - earlier.started_at for earlier, later in pairwise(observed)]
    for index, gap_s in enumerate(gaps_s):
        assert abs(gap_s - scenario.interval_s) <= TOLERANCE_S, f"gap {index} was {gap_s:g} s"


# --------------------------------------------------------------------------------------------------
# Property 19: overrunning cycles
# --------------------------------------------------------------------------------------------------

#: A read in the "just over one interval" partition exceeds its interval by between 0.01% and 10%.
#: The lower bound remains visible at the largest generated clock reading, rather than rounding back
#: onto the interval boundary and turning an intended overrun into an exact-boundary case.
MIN_OVERRUN_FRACTION: Final = 0.0001
MAX_JUST_OVER_FRACTION: Final = 0.1

#: The multi-interval partition crosses between two and eight complete scheduled intervals, then a
#: positive fractional remainder. The oracle scans one further grid point to prove it found the first
#: scheduled time after completion rather than merely all candidates it happened to enumerate.
MIN_MULTIPLE_INTERVALS: Final = 2
MAX_MULTIPLE_INTERVALS: Final = 8


@dataclass(frozen=True, slots=True)
class OverrunScenario:
    """One mixed run covering both controls and the two overrun partitions.

    Every scenario contains, in order, a non-overrun, a read just over one interval, a read spanning
    multiple intervals, and another non-overrun. A final zero-duration probe is added by the driver;
    observing where that probe starts proves that the last overrun resumed on the original grid.
    """

    interval_s: int
    start_s: float
    non_overrun_before_fraction: float
    just_over_extra_fraction: float
    multiple_intervals: int
    multiple_extra_fraction: float
    non_overrun_after_fraction: float

    def holds_s(self) -> tuple[float, ...]:
        """The four generated read durations, in seconds."""
        interval = self.interval_s
        return (
            self.non_overrun_before_fraction * interval,
            (1.0 + self.just_over_extra_fraction) * interval,
            (self.multiple_intervals + self.multiple_extra_fraction) * interval,
            self.non_overrun_after_fraction * interval,
        )

    def grid_time(self, index: int) -> float:
        """Scheduled grid point ``index``, derived directly as ``start + index * interval``."""
        return self.start_s + index * self.interval_s


@dataclass(frozen=True, slots=True)
class ExpectedOverrunTrace:
    """The independently modelled starts and skipped grid points for one scenario."""

    cycle_starts: tuple[float, ...]
    skipped_by_cycle: tuple[tuple[float, ...], ...]

    @property
    def skipped_times(self) -> tuple[float, ...]:
        """All skipped times in the order their cycles encounter them."""
        return tuple(skipped_at for cycle_skips in self.skipped_by_cycle for skipped_at in cycle_skips)


def non_overrun_fractions() -> st.SearchStrategy[float]:
    """Fractions from an instantaneous read through one taking 99% of its interval."""
    return st.one_of(
        st.floats(min_value=0.0, max_value=MAX_HOLD_FRACTION, allow_nan=False, allow_infinity=False),
        st.sampled_from([0.0, 0.5, 0.9, MAX_HOLD_FRACTION]),
    )


def positive_overrun_fractions(max_value: float) -> st.SearchStrategy[float]:
    """A positive fractional interval, kept away from the exact scheduled-time boundary."""
    return st.one_of(
        st.floats(
            min_value=MIN_OVERRUN_FRACTION,
            max_value=max_value,
            allow_nan=False,
            allow_infinity=False,
        ),
        st.sampled_from([MIN_OVERRUN_FRACTION, 0.01, max_value]),
    )


def overrun_scenarios() -> st.SearchStrategy[OverrunScenario]:
    """Runs containing non-overruns, one elapsed interval, and multiple elapsed intervals."""
    return st.builds(
        OverrunScenario,
        interval_s=intervals(),
        start_s=start_readings(),
        non_overrun_before_fraction=non_overrun_fractions(),
        just_over_extra_fraction=positive_overrun_fractions(MAX_JUST_OVER_FRACTION),
        multiple_intervals=st.one_of(
            st.integers(min_value=MIN_MULTIPLE_INTERVALS, max_value=MAX_MULTIPLE_INTERVALS),
            st.sampled_from([MIN_MULTIPLE_INTERVALS, 3, MAX_MULTIPLE_INTERVALS]),
        ),
        multiple_extra_fraction=positive_overrun_fractions(MAX_HOLD_FRACTION),
        non_overrun_after_fraction=non_overrun_fractions(),
    )


def expected_overrun_trace(scenario: OverrunScenario) -> ExpectedOverrunTrace:
    """Model elapsed deadlines solely from the original ``start + n * interval`` grid.

    The scheduler under test advances a mutable deadline by repeated addition. This oracle never
    does: every candidate is reconstructed by multiplication from the generated start. For each read
    it enumerates all bounded candidate grid points, selects exactly those at or before completion,
    and chooses the immediately following grid point as the next executed cycle.
    """
    grid_index = 0
    cycle_starts: list[float] = []
    skipped_by_cycle: list[tuple[float, ...]] = []

    for hold_s in scenario.holds_s():
        cycle_start = scenario.grid_time(grid_index)
        completed_at = cycle_start + hold_s
        cycle_starts.append(cycle_start)

        # One more candidate than the largest generated overrun can cross. That final candidate must
        # be after completion; retaining it in the model guards against a truncated expectation.
        candidates = tuple(
            scenario.grid_time(index) for index in range(grid_index + 1, grid_index + MAX_MULTIPLE_INTERVALS + 2)
        )
        elapsed = tuple(scheduled_at for scheduled_at in candidates if scheduled_at <= completed_at)
        assert candidates[-1] > completed_at, "the oracle did not scan beyond cycle completion"
        skipped_by_cycle.append(elapsed)

        # The next run is the first grid point after every elapsed one. This index arithmetic chooses
        # a grid point; it does not derive a deadline from completion, so overruns cannot re-anchor it.
        grid_index += len(elapsed) + 1

    # A probe cycle lets the test observe resumption after the final generated read without asking the
    # scheduler to process another duration that could itself add skip records.
    cycle_starts.append(scenario.grid_time(grid_index))
    return ExpectedOverrunTrace(tuple(cycle_starts), tuple(skipped_by_cycle))


async def _observe_overruns(
    scenario: OverrunScenario,
    logger: logging.Logger,
) -> tuple[ObservedCycle, ...]:
    """Drive the shared virtual-clock harness through all reads and one resumption probe."""
    clock = VirtualClock(scenario.start_s)
    device = cast("ResolvedDevice", _FakeDevice(scenario.interval_s))
    holds = (*scenario.holds_s(), 0.0)
    reads = SimulatedReads(clock, holds, len(holds))
    scheduler = PollScheduler(
        [device],
        executor=cast("Any", reads),
        clock=clock,
        sleep=clock.sleep,
        logger=logger,
    )
    try:  # noqa: SIM105 - contextlib.suppress does not catch except*, the TaskGroup's exception group
        await scheduler.run()
    except* _EnoughCycles:
        pass
    return tuple(reads.cycles)


# Feature: remote-energy-meter, Property 19: Overrunning cycles skip exactly the elapsed scheduled times
# Validates: Requirements 5.9
@settings(max_examples=150, deadline=None)
@given(scenario=overrun_scenarios())
def test_overrunning_cycles_skip_exactly_the_elapsed_scheduled_times(scenario: OverrunScenario) -> None:
    """Every elapsed grid time is skipped once, with no backlog, drift, or extra skip."""
    expected = expected_overrun_trace(scenario)
    holds = scenario.holds_s()

    with recording_logger() as (logger, records):
        observed = asyncio.run(_observe_overruns(scenario, logger))

    # The generated partitions are load-bearing: every example exercises controls on both sides of
    # an overrun, exactly one elapsed deadline, and a generated number of elapsed deadlines.
    expected_skip_counts = (0, 1, scenario.multiple_intervals, 0)
    assert tuple(len(cycle_skips) for cycle_skips in expected.skipped_by_cycle) == expected_skip_counts

    # The sentinel fires only from the extra probe, so a missing resumption cannot pass vacuously.
    assert len(observed) == len(holds) + 1
    assert len(observed) == len(expected.cycle_starts)
    assert {cycle.device_id for cycle in observed} == {DEVICE_ID}

    # Confirm the harness applied every generated duration and made the probe instantaneous. No real
    # sleep supplies any of this elapsed time; only the injected clock moves.
    for index, (cycle, hold_s) in enumerate(zip(observed[:-1], holds, strict=True)):
        assert cycle.ended_at == cycle.started_at + hold_s, f"cycle {index} did not spend its generated read duration"
    assert observed[-1].duration_s == 0.0

    # Each skip record must name exactly the one device and the independently enumerated scheduled
    # time. Ordered equality proves there is neither a missing record nor an extra skip.
    skip_records = tuple(record for record in records if SKIP_MESSAGE_FRAGMENT in record.getMessage())
    expected_record_fields = tuple((DEVICE_ID, skipped_at) for skipped_at in expected.skipped_times)
    observed_record_fields = tuple(
        (getattr(record, "device_id", None), getattr(record, "skipped_at", None)) for record in skip_records
    )
    assert observed_record_fields == expected_record_fields

    # Executed cycles resume at the first unelapsed point on the original multiplication-derived
    # grid. This rules out both a backlog at skipped times and re-anchoring at completion + interval.
    for index, (cycle, scheduled_at) in enumerate(zip(observed, expected.cycle_starts, strict=True)):
        error_s = abs(cycle.started_at - scheduled_at)
        assert error_s <= VIRTUAL_CLOCK_SLACK_S, f"cycle {index} resumed {error_s:g} s off the original grid"

    for index, (cycle, resumed, cycle_skips) in enumerate(
        zip(observed[:-1], observed[1:], expected.skipped_by_cycle, strict=True)
    ):
        assert all(skipped_at <= cycle.ended_at for skipped_at in cycle_skips)
        assert resumed.started_at > cycle.ended_at, f"cycle {index} left a backlog instead of waiting for the grid"

    for skipped_at in expected.skipped_times:
        assert all(abs(cycle.started_at - skipped_at) > VIRTUAL_CLOCK_SLACK_S for cycle in observed), (
            f"a cycle ran at skipped grid time {skipped_at:g}"
        )
