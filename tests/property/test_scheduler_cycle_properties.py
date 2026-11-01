"""Poll cycle properties: what one cycle costs, how it retries, and what a failed one reports (tasks 11.7-11.9).

**Property 16** -- *for any* set of accepted Device_Entry values and any assignment of read latencies to
them, the elapsed duration of the poll cycle is at most the largest single-read latency plus 1 second, and
no device has two power reading operations in flight simultaneously (Requirement 5.3).

**Property 17** -- *for any* failure classification and any poll interval, the number of power reading
attempts in a cycle is exactly 1 when the classification is authentication failure, and otherwise the
largest count up to 3 whose cumulative retry delays of 1 second then 2 seconds keep the elapsed cycle time
for that device below its poll interval, with no attempt started after that budget is reached
(Requirement 5.5).

**Property 18** -- *for any* per-device sequence of poll cycle outcomes, every cycle in which no attempt
succeeded reports that device to the MQTT_Publisher as unavailable, passes no Power_Reading to the
Energy_Accumulator, leaves that device's cumulative energy total unchanged, and is followed by a further
cycle at the next scheduled time (Requirement 5.6).

**Property 7** -- *for any* set of devices of which an arbitrary subset misbehaves -- returning classified
failures, raising exceptions of any type, returning garbage, or hanging -- the registry holds one plugin
instance per device, every raised exception is converted into a plugin-classified read failure, every
non-failing device still obtains a reading in every cycle and still accumulates energy, and the process
keeps running (Requirements 3.6, 3.7, 4.2).

## What this module covers, and what belongs elsewhere

This module is scoped to the **poll cycle** -- what the scheduler does at one scheduled time, and what
it costs. Properties 16, 17, 18, and 7 live here.

Its sibling ``test_scheduler_loop_properties.py`` is scoped to the **per-device loop** -- the part that
decides *when* a cycle runs. Property 13 (drift) is there, and Property 19 (task 11.10, overrunning
cycles skipping exactly the elapsed scheduled times) belongs there too, per that module's own docstring.
Properties 14 and 15, the pure resolution functions, are in ``test_scheduler_resolution_properties.py``.
The split mirrors the unit layer's ``test_scheduler_loop.py`` / ``test_scheduler_cycle.py`` divide.

## The virtual clock, and why this one is not the loop module's clock

The spec's testing strategy requires the timing-sensitive properties to run against a virtual clock, and
the scheduler takes its clock and sleep function by injection for exactly that reason. Nothing here
sleeps for a real duration: a 3600 second interval across six devices costs a few hundred event-loop
turns and no waiting.

But the clock in ``test_scheduler_loop_properties.py`` (and in the unit modules) advances by *whichever
task last slept* -- ``now += delay`` inside ``sleep``. With one device that is faithful. With two it is
not: each device's sleep shoves the shared clock forward under the other, so two loops interfere and any
duration measured across them would be an artefact of the fake. Those modules say as much, and drive one
device only. Property 16 is inherently multi-device -- a single device cannot tell a concurrent
implementation from a sequential one -- so this module needs a clock that models concurrency.

:class:`ConcurrentVirtualClock` is that clock: a small discrete-event scheduler. A sleep does not move
time at all. It parks the caller at a deadline, and when *every* participant is parked -- one participant
per device task, which is where every sleep in this harness comes from -- time jumps to the **earliest**
pending deadline and exactly the waiters due at that instant are released. So two devices that sleep 10 s
starting together both wake 10 s later, rather than 20 s later as the simpler clock would have it. Under
that clock the simulated read *is* a sleep, not a shove: the read holds its device's task parked for the
## Four properties, four harnesses, one clockoncurrent reads overlap in simulated time.

A cap on the number of advances (:data:`MAX_CLOCK_ADVANCES`) turns a harness that stops making progress
into a fast, named failure instead of a test run that hangs until a timeout kills it.

## Three properties, three harnesses, one clock

Property 16 is about the **loop that issues cycles**, so it is driven through :class:`PollScheduler` with a
recorder standing in for the cycle executor: what is measured is when reads start and end, and a real
executor would only add attempts the duration bound does not quantify over.

Property 17 is about the **cycle executor itself** -- the retry schedule is its behaviour, not the loop's
-- so it is driven by calling :meth:`PollCycleExecutor.run_cycle` directly, with a stub plugin whose reads
return a generated classification sequence. No loop, no scheduled times: one cycle, start to finish.

Property 18 spans both, because Requirement 5.6 does: what a failed cycle reports is the executor's
Property 7 is about **one device's fault and its neighbours**, so it is inherently multi-device and runs
the whole stack the way Property 18 does -- the real loop, the real executor, the real accumulator behind
the real sink -- with a plugin per device that misbehaves as generated. What it asserts is a comparison
rather than a bound: the same run is driven again with the faulty devices never configured, and the
healthy devices' reads and totals must come out equal.

All four run on the same :class:`ConcurrentVirtualClock`; Properties 17 and 18 give it one participant
instead of one per device. With a single participant an advance happens the moment the cycle parks, which
is exactly the advance-on-sleep behaviour the unit modules' clock has -- and reusing this one keeps the
module to a single notion of simulated time rather than several fakes that could disagree.

All three run on the same :class:`ConcurrentVirtualClock`; Properties 17 and 18 give it one participant
instead of one per device. With a single participant an advance happens the moment the cycle parks, which
is exactly the advance-on-sleep behaviour the unit modules' clock has -- and reusing this one keeps the
module to a single notion of simulated time rather than several fakes that could disagree.

## How the harness is stopped from faking the result

A timing harness that measures its own arithmetic is the real hazard here: a clock that never advanced,
or a measurement taken across the wrong instants, would report a small elapsed duration for a sequential
implementation just as happily as for a concurrent one, and the property would pass while proving
nothing. Three things guard against that, all asserted rather than assumed:

* **the reads really spend their latency.** Every observed read is checked to have ended exactly its
  generated latency after it began, on the clock the property then measures;
* **the reads of one cycle really do overlap.** The recorder counts operations in flight as they happen,
  and the property asserts the peak reached the full device count -- Requirement 5.3's "concurrently"
  read as an observation rather than as an inference from a duration;
* **a sequential control violates the bound.** Every example is run a second time against the *same*
  scheduler, the *same* clock, and the *same* measurement, with only the recorder changed: reads queue on
  one virtual server instead of running side by side. Wherever the latencies sum to more than the slowest
  plus a second -- which the generated shapes make common -- the control's measured cycle is asserted to
  *breach* the bound the concurrent run must satisfy. So the bound is known to be falsifiable by this
  harness, not merely satisfied by it.

## The generated space

* **the devices** -- two to six, each with its own read latency. Two is the floor because one device
  cannot distinguish concurrent from sequential; six keeps a run small while giving five reads to
  overlap. Explicit shapes are sampled alongside the free-form lists, and they are the interesting ones:
  one slow read among fast ones (the case a sequential implementation survives by luck when the others
  are quick), several slow reads (where sequential cost is a multiple of the bound), all-equal latencies,
  and all-instant reads as the degenerate control;
* **the interval** -- the whole 5-3600 second range of Requirement 5.1, shared by every device in an
  example. One shared interval is what makes "the poll cycle" of Requirement 5.3 a well-defined set of
  reads to measure across; per-device intervals are Requirement 5.2's subject and Property 14's;
* **the latencies** -- fractions of the interval, so a read is always somewhere between instant and
  nearly a whole interval. Strictly below 1.0: a read that outlasts its interval makes the loop drop
  scheduled times (Requirement 5.9), and the k-th read of one device would then no longer belong to the
  same cycle as the k-th read of another, leaving nothing well defined to measure. That regime is
  Property 19's. Here it is excluded by construction and the exclusion is *checked* -- the property
  asserts no skip record was written;
* **the cycles** -- one to four consecutive cycles per run. One is all the duration bound needs; more
  exist for the second clause, since "no device has two reads in flight" is a claim about what a repeated
  schedule does, and a cycle costing nearly a whole interval is where an implementation that let one
  overlap the next would be caught;
* **the start reading** -- ``time.monotonic`` is boot-relative, so a real start reads anywhere from
  seconds to months of uptime. Generated rather than fixed, so nothing can pass by assuming zero.

## The generated space of Property 17

* **the classification of each attempt** -- all seven :class:`FailureClassification` values, drawn both as
  a repeated sequence (a device usually fails the same way twice running) and as freely mixed ones (a
  timeout that becomes a communication failure on the retry);
* **where the cycle succeeds** -- at the first, second, or third attempt, or nowhere at all, so
  "a success on an earlier attempt means no further attempts" is quantified over rather than sampled;
* **how long each attempt takes** -- because Requirement 5.5's budget covers attempts *as well as* retry
  delays, a read that spends most of an interval is what makes the budget bite before the retry allowance
  runs out;
* **the interval** -- the whole 5-3600 second range, plus a band of tight intervals sampled on purpose:
  at 5 seconds a 4 second read leaves no room for even the 1 second first retry, and a 1 second read
  plus the 1 s and 2 s delays reaches the budget exactly, which is the boundary the requirement resolves
  exclusively;
* **the start reading** -- shared with Property 16, for the same reason.

## The generated space of Property 18

* **the sequence of outcomes** -- two or three consecutive cycles, each of which either fails throughout or
  succeeds at a named attempt, drawn free-form and from sampled shapes: all failing, all succeeding, a
  failure between two successes, and a success between two failures. Requirement 5.6 is a claim about a
  *sequence*, so a run of one cycle could not observe the clause about the next scheduled time at all;
* **how each cycle fails** -- the same seven-classification scripts Property 17 draws, so the report a
  failed cycle makes is quantified over every mix of classifications, including the authentication failure
  that ends a cycle after one attempt;
* **the interval and the start reading** -- shared with Properties 16 and 17;
* **how long each attempt takes** -- as a share of the cycle's *read budget*, the poll interval less the
  3 seconds of retry delays Requirement 5.5 allows. Capped so that three attempts plus both delays stay
  strictly inside one interval: a cycle that outran its interval would drop the next scheduled time
  (Requirement 5.9, Property 19's subject) and there would be no "next scheduled time" left to observe.
  The exclusion is *checked* -- the property asserts no skip record was written.

## The generated space of Property 7

* **which devices misbehave, and how** -- two to six devices, of which at least one is healthy and at least
  one is faulty, in a permuted order so a faulty device lands anywhere in the order the scheduler creates
  its tasks. Each fault draws one of four kinds, one of the seven classifications, and one of seven
  exception types; mixes worth having are sampled on purpose, including all four kinds at once;
* **how long each device reads** -- as a share of the cycle's read budget, capped so three attempts and both
  retry delays stay strictly inside one interval, exactly as Property 18 does and for the same reason: a
  cycle that outran its interval would drop scheduled times and the healthy schedule being compared across
  two runs would no longer be the same schedule;
* **the interval, the start reading, and the cycle count** -- shared with Properties 17 and 18. Two cycles
  is the floor, because "keeps being polled" needs a second cycle to observe.

Every generated duration is an exact binary fraction, and so is every start reading. That is deliberate:
the expected schedule is computed by summing the same values in the same order the clock does, so the
comparison against the requirement's ``elapsed + delay >= interval`` is exact rather than approximate, and
the boundary examples test the boundary instead of testing float noise.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from itertools import pairwise
from typing import TYPE_CHECKING, Any, Final, cast

from hypothesis import given, settings
from hypothesis import strategies as st

from remote_energy_meter.app import AccumulateThenPublish
from remote_energy_meter.energy import EnergyAccumulator
from remote_energy_meter.persistence import DeviceEnergyState
from remote_energy_meter.plugins.interface import FailureClassification, PowerReading, ReadFailure
from remote_energy_meter.scheduler import PollCycleExecutor, PollScheduler

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import AsyncIterator, Iterator, Mapping, Sequence

    from remote_energy_meter.config.runtime import ResolvedDevice
    from remote_energy_meter.plugins.interface import ReadResult
    from remote_energy_meter.scheduler import CycleResult

#: The poll interval range Requirement 5.1's cycle rate is stated over, in whole seconds. Written out
#: rather than imported from the module under test, so a bound changed on one side is a disagreement
#: rather than a generator and an expectation moving together.
ACCEPTED_MIN_INTERVAL_S: Final = 5
ACCEPTED_MAX_INTERVAL_S: Final = 3600

#: The global interval when the Addon_Configuration omits it (Requirement 2.6), sampled as a notable
#: value only -- nothing in the expectation depends on it.
DEFAULT_GLOBAL_INTERVAL_S: Final = 30

#: Requirement 5.3's tolerance: a cycle may cost its slowest read plus this much, and no more.
TOLERANCE_S: Final = 1.0

#: The tighter bound the virtual clock earns. Real scheduling jitter is what Requirement 5.3's second of
#: slack is for, and none of it exists here: with concurrent reads modelled exactly, a cycle costs its
#: slowest read to within a float rounding artefact well under a nanosecond. A microsecond is three
#: orders of magnitude above that noise and six below the requirement's slack, so a cycle that cost even
#: one extra millisecond of simulated time fails here long before it would fail the requirement.
VIRTUAL_CLOCK_SLACK_S: Final = 1e-6

#: How many devices one example polls. Two is the floor: with one device, a sequential implementation and
#: a concurrent one cost the same and the property would be untestable.
MIN_DEVICES: Final = 2
MAX_DEVICES: Final = 6

#: How many consecutive cycles one run is driven for.
MIN_CYCLES: Final = 1
MAX_CYCLES: Final = 4

#: The largest share of its interval a simulated read may take. Strictly below 1.0: a read that outlasts
#: its interval drops scheduled times (Requirement 5.9), which is Property 19's subject.
MAX_LATENCY_FRACTION: Final = 0.99

#: Latency shapes worth drawing on purpose, as fractions of the interval. One slow read among fast ones
#: is where a sequential implementation nearly passes; several slow reads is where its cost is a multiple
#: of the bound; all-equal and all-instant are the symmetric and degenerate cases.
LATENCY_SHAPES: Final[tuple[tuple[float, ...], ...]] = (
    (MAX_LATENCY_FRACTION, 0.001, 0.001, 0.001, 0.001, 0.001),
    (0.9, 0.01, 0.01),
    (0.9, 0.9, 0.9),
    (MAX_LATENCY_FRACTION, MAX_LATENCY_FRACTION, MAX_LATENCY_FRACTION, MAX_LATENCY_FRACTION),
    (0.5, 0.5),
    (0.5, 0.0, 0.5, 0.0),
    (0.0, 0.0),
)

#: Monotonic clock readings a real start could plausibly produce: a fresh boot, a few minutes, a day,
#: and a hundred days of uptime. Generated rather than fixed, so a measurement anchored at zero fails.
START_READINGS_S: Final[tuple[float, ...]] = (0.0, 1000.0, 86_400.0, 8_640_000.0)

#: Device identifiers are built from this, so they are shaped like real ones (Requirement 2.12).
DEVICE_ID_PREFIX: Final = "poweredge_"

#: An isolated logger name, so configuring it cannot disturb the root logger or pytest's capture.
RECORDING_LOGGER_NAME: Final = "remote_energy_meter_property_scheduler_cycle"

#: The message fragment the loop's skip record carries (Requirements 5.3, 5.9).
SKIP_MESSAGE_FRAGMENT: Final = "poll cycle skipped"

#: How many times one run may advance the virtual clock before the harness is declared stuck. Four
#: cycles across six devices need on the order of a hundred advances, so this is generous by two orders
#: of magnitude; its purpose is to turn a harness that stops making progress into a named failure rather
#: than a run that hangs until a timeout kills it.
MAX_CLOCK_ADVANCES: Final = 10_000


# --------------------------------------------------------------------------------------------------
# log capture
# --------------------------------------------------------------------------------------------------


class _Recorder(logging.Handler):
    """Collects records rather than formatting them, so an assertion reads the record's own fields.

    ``caplog`` would be the obvious choice and cannot be used here: it is a function-scoped fixture, and
    Hypothesis reuses a function-scoped fixture across every example of one test, so the records of
    example *n* would still be present when example *n+1* counted them.
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
# the concurrent virtual clock
# --------------------------------------------------------------------------------------------------


class _EnoughCycles(Exception):  # noqa: N818 - a control-flow sentinel that ends the run, not an error
    """Ends a run once every device has completed the requested number of cycles.

    Raised from inside a simulated read (Property 16) or from the runner wrapping the real executor
    (Property 18), so it reaches the ``TaskGroup`` in :meth:`PollScheduler.run` and is re-raised there as
    an exception group, which cancels the devices still parked on their next deadline. A sentinel rather
    than an outside cancellation after so many event-loop turns, because a turn budget that turned out too
    small would end the run early and the property would pass on whichever cycles it happened to get.

    Never raised from inside a *real* read: :meth:`PollCycleExecutor.run_cycle` catches every exception a
    plugin lets escape and classifies it as a plugin failure (Requirement 3.7), so a sentinel raised there
    would be swallowed and the run would never end.
    """


@dataclass(frozen=True, slots=True)
class _Parked:
    """One task waiting for the virtual clock to reach ``deadline``."""

    deadline: float
    waiter: asyncio.Future[None]


class ConcurrentVirtualClock:
    """A virtual monotonic clock that models concurrent waiting: a discrete-event scheduler.

    The injected pair the scheduler needs: the instance itself is the clock, and :meth:`sleep` is the
    sleep function. Unlike the single-device clock in the loop module and the unit modules, a sleep here
    does **not** move time. It parks the caller and returns time control to the harness; time advances
    only when every participant is parked, and then only to the earliest pending deadline. So N devices
    that each sleep for 10 s starting together all wake 10 s later, which is the whole point: sequential
    and concurrent execution have different costs on this clock, and the same cost on the simpler one.

    One participant per device task. Every sleep in this harness -- the loop's wait for the next
    deadline, and a simulated read holding its device -- is issued from a device's task, so "every
    participant is parked" is exactly "no device can make progress until time moves". A device task never
    finishes before the run ends, so the count is fixed for the life of the clock.
    """

    __slots__ = ("_parked", "_participants", "advances", "now")

    def __init__(self, start_s: float, participants: int) -> None:
        """Start at ``start_s`` with ``participants`` tasks whose sleeps this clock arbitrates."""
        self.now = start_s
        self.advances = 0
        self._participants = participants
        self._parked: list[_Parked] = []

    def __call__(self) -> float:
        """The current reading, which is what the scheduler's injected clock returns."""
        return self.now

    async def sleep(self, delay_s: float) -> None:
        """Park the calling task until this clock reaches ``now + delay_s``.

        A non-positive delay is a plain yield: it costs an event-loop turn and no simulated time, which is
        what ``asyncio.sleep(0)`` does in production and keeps a zero-latency read from having to be
        woken by a clock advance.
        """
        if delay_s <= 0:
            await asyncio.sleep(0)
            return
        parked = _Parked(deadline=self.now + delay_s, waiter=asyncio.get_running_loop().create_future())
        self._parked.append(parked)
        self._advance_if_all_parked()
        await parked.waiter

    def _advance_if_all_parked(self) -> None:
        """Move to the earliest pending deadline once no participant can make progress without it.

        Called from :meth:`sleep` by whichever task parks last, so the advance happens at the one moment
        it is safe: every device is waiting, either for its next scheduled time or for a read to finish.
        Releasing *all* waiters due at the new reading, rather than only the earliest, is what lets reads
        that started together finish together.
        """
        if len(self._parked) < self._participants:
            return
        self.advances += 1
        if self.advances > MAX_CLOCK_ADVANCES:
            message = f"the virtual clock advanced more than {MAX_CLOCK_ADVANCES} times: the harness is stuck"
            raise RuntimeError(message)
        # Every pending deadline is strictly ahead of the current reading: a waiter due at or before it is
        # released here rather than left parked, and the reading only ever moves forward.
        self.now = min(parked.deadline for parked in self._parked)
        due = [parked for parked in self._parked if parked.deadline <= self.now]
        self._parked = [parked for parked in self._parked if parked.deadline > self.now]
        for parked in due:
            parked.waiter.set_result(None)


# --------------------------------------------------------------------------------------------------
# the simulated devices and their reads
# --------------------------------------------------------------------------------------------------


class _FakeDevice:
    """Stands in for a ``ResolvedDevice``, carrying only the fields the code under test reads.

    The loop (Property 16) reads ``device_id``, ``interval_s``, and ``lock``, and hands the device straight
    to its cycle runner. The cycle executor (Property 17) reads ``timeout_s``, ``plugin``, and
    ``plugin_id`` as well, so those are optional here rather than living in a second stand-in. A real
    ``ResolvedDevice`` would need a validated entry, which neither the loop nor the executor touches.
    """

    def __init__(
        self,
        device_id: str,
        interval_s: int,
        *,
        timeout_s: float = 0.0,
        plugin: object | None = None,
        plugin_id: str = "stub",
    ) -> None:
        self.device_id = device_id
        self.interval_s = interval_s
        self.timeout_s = timeout_s
        self.plugin = plugin
        self.plugin_id = plugin_id
        self.lock = asyncio.Lock()


@dataclass(frozen=True, slots=True)
class ObservedRead:
    """One power reading operation that actually ran, on the virtual clock.

    Attributes:
        device_id: which device was read, so a read cannot be credited to the wrong loop.
        index: which cycle of that device this was, counting from zero. With every read finishing inside
            its own interval and every device sharing one interval, the reads that share an index are
            exactly the reads of one poll cycle.
        started_at: the clock reading when the operation began.
        ended_at: the clock reading when it finished.
    """

    device_id: str
    index: int
    started_at: float
    ended_at: float

    @property
    def duration_s(self) -> float:
        """How much simulated time this read spent."""
        return self.ended_at - self.started_at


class SimulatedReads:
    """A cycle runner whose reads hold their device for a generated latency, and which records overlap.

    Two modes, and the second is what gives the property teeth:

    * **concurrent** (the default) -- a read holds only its own device's task, so reads issued in the
      same cycle overlap in simulated time and the cycle costs the slowest of them. This is the mode the
      scheduler is measured in;
    * **sequential** -- a deliberately wrong control. Each read claims the next free slot on a single
      virtual server, so no two reads ever overlap and a cycle costs the *sum* of its reads. The claim is
      taken synchronously on arrival and waited out on the clock, rather than by contending on a lock,
      because a task blocked on a lock is neither running nor parked and would stall the clock.

    Both modes share every other part of the harness, so a bound that the concurrent run satisfies and
    the sequential control breaks is a bound about the scheduler rather than about the measurement.
    """

    def __init__(
        self,
        clock: ConcurrentVirtualClock,
        latencies_s: Mapping[str, float],
        wanted: int,
        *,
        sequential: bool = False,
    ) -> None:
        """Read ``latencies_s`` per device, until every device has completed ``wanted`` cycles."""
        self._clock = clock
        self._latencies_s = latencies_s
        self._wanted = wanted
        self._sequential = sequential
        self._sequential_free_at = 0.0
        self._in_flight: dict[str, int] = dict.fromkeys(latencies_s, 0)
        self._completed: dict[str, int] = dict.fromkeys(latencies_s, 0)
        self.reads: list[ObservedRead] = []
        self.peak_in_flight_per_device = 0
        self.peak_in_flight_overall = 0

    async def run_cycle(self, device: Any) -> None:
        """Perform one simulated power reading operation for ``device``, and record what it cost."""
        device_id = str(device.device_id)
        latency_s = self._latencies_s[device_id]
        queued_s = self._claim_slot(latency_s) if self._sequential else 0.0

        # One turn before anything is measured, so a cycle is a genuine suspension point rather than a
        # straight-line call: a real cycle awaits a request here.
        await asyncio.sleep(0)
        if queued_s > 0:
            await self._clock.sleep(queued_s)

        started_at = self._clock.now
        self._entered(device_id)
        await self._clock.sleep(latency_s)
        self._left(device_id)

        index = self._completed[device_id]
        self._completed[device_id] = index + 1
        self.reads.append(
            ObservedRead(device_id=device_id, index=index, started_at=started_at, ended_at=self._clock.now)
        )
        if all(count >= self._wanted for count in self._completed.values()):
            raise _EnoughCycles

    def _claim_slot(self, latency_s: float) -> float:
        """Reserve the next free slot on the sequential control's single virtual server.

        Returns how long this read must wait before it may begin. Taken synchronously, before any await,
        so the queue order is the order the device loops arrived rather than a race.
        """
        claim_at = max(self._clock.now, self._sequential_free_at)
        self._sequential_free_at = claim_at + latency_s
        return claim_at - self._clock.now

    def _entered(self, device_id: str) -> None:
        """Record one more operation in flight, for both clauses of Requirement 5.3."""
        self._in_flight[device_id] += 1
        self.peak_in_flight_per_device = max(self.peak_in_flight_per_device, self._in_flight[device_id])
        self.peak_in_flight_overall = max(self.peak_in_flight_overall, sum(self._in_flight.values()))

    def _left(self, device_id: str) -> None:
        """Record that an operation finished."""
        self._in_flight[device_id] -= 1


# --------------------------------------------------------------------------------------------------
# generators
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CycleScenario:
    """One run: how many devices, how slow each one reads, how often, from when, and for how long.

    Attributes:
        interval_s: the poll interval in whole seconds, as ``ResolvedDevice`` carries it, shared by every
            device so that "one poll cycle" is a well-defined set of reads.
        cycles: how many consecutive cycles every device is driven for.
        start_s: the clock reading at Poll_Scheduler start.
        latency_fractions: one read latency per device, as a share of the interval.
    """

    interval_s: int
    cycles: int
    start_s: float
    latency_fractions: tuple[float, ...]

    @property
    def device_count(self) -> int:
        """How many devices this run polls."""
        return len(self.latency_fractions)

    def device_ids(self) -> tuple[str, ...]:
        """The identifiers, shaped like real ones and distinct (Requirements 2.5, 2.12)."""
        return tuple(f"{DEVICE_ID_PREFIX}{index}" for index in range(self.device_count))

    def latencies_s(self) -> tuple[float, ...]:
        """The read latencies in seconds, each strictly below one interval."""
        return tuple(fraction * self.interval_s for fraction in self.latency_fractions)

    def latency_by_device(self) -> dict[str, float]:
        """The assignment of read latencies to devices that Property 16 quantifies over."""
        return dict(zip(self.device_ids(), self.latencies_s(), strict=True))

    @property
    def slowest_s(self) -> float:
        """The largest single-read latency: the quantity Requirement 5.3 bounds a cycle by."""
        return max(self.latencies_s())

    @property
    def sequential_cost_s(self) -> float:
        """What one cycle would cost if the reads ran one after another."""
        return sum(self.latencies_s())


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
    """How many consecutive cycles one run covers: one to four, with the extremes sampled.

    One cycle is all the duration bound needs. More exist for the second clause of Property 16: "no
    device has two reads in flight" is a claim about a repeated schedule, and a read taking nearly a whole
    interval is where an implementation that let one cycle overlap the next would be caught.
    """
    return st.one_of(
        st.integers(min_value=MIN_CYCLES, max_value=MAX_CYCLES),
        st.sampled_from([MIN_CYCLES, MAX_CYCLES]),
    )


def start_readings() -> st.SearchStrategy[float]:
    """The clock reading at Poll_Scheduler start, from a fresh boot to a hundred days of uptime."""
    return st.sampled_from(START_READINGS_S)


def latency_fractions() -> st.SearchStrategy[tuple[float, ...]]:
    """One read latency per device, as a share of its interval, for two to six devices.

    Free-form lists across the whole 0 to 0.99 band, plus the shapes of :data:`LATENCY_SHAPES` drawn
    explicitly -- widely differing latencies are what separate a concurrent cycle from a sequential one,
    and a generator left to itself would rarely produce one 0.99 among five 0.001s.
    """
    fraction = st.floats(min_value=0.0, max_value=MAX_LATENCY_FRACTION, allow_nan=False, allow_infinity=False)
    return st.one_of(
        st.lists(fraction, min_size=MIN_DEVICES, max_size=MAX_DEVICES).map(tuple),
        st.sampled_from(LATENCY_SHAPES),
    )


def cycle_scenarios() -> st.SearchStrategy[CycleScenario]:
    """A whole run: an interval, a cycle count, a start reading, and one latency per device."""
    return st.builds(
        CycleScenario,
        interval_s=intervals(),
        cycles=cycle_counts(),
        start_s=start_readings(),
        latency_fractions=latency_fractions(),
    )


# --------------------------------------------------------------------------------------------------
# the driver
# --------------------------------------------------------------------------------------------------


async def _observe(
    scenario: CycleScenario,
    logger: logging.Logger,
    *,
    cycles: int,
    sequential: bool = False,
) -> SimulatedReads:
    """Run ``scenario`` on a concurrent virtual clock and return the recorder holding what happened.

    Returns once every device has completed ``cycles`` cycles and the last one to get there raised
    :class:`_EnoughCycles`, which the scheduler's ``TaskGroup`` re-raises as an exception group.
    """
    clock = ConcurrentVirtualClock(scenario.start_s, participants=scenario.device_count)
    devices: Sequence[ResolvedDevice] = [
        cast("ResolvedDevice", _FakeDevice(device_id, scenario.interval_s)) for device_id in scenario.device_ids()
    ]
    reads = SimulatedReads(clock, scenario.latency_by_device(), wanted=cycles, sequential=sequential)
    scheduler = PollScheduler(
        devices,
        executor=cast("Any", reads),
        clock=clock,
        sleep=clock.sleep,
        logger=logger,
    )
    try:  # noqa: SIM105 - contextlib.suppress does not catch except*, the TaskGroup's exception group
        await scheduler.run()
    except* _EnoughCycles:
        pass
    return reads


def _reads_of_cycle(reads: Sequence[ObservedRead], index: int) -> tuple[ObservedRead, ...]:
    """The reads belonging to cycle ``index``, one per device."""
    return tuple(read for read in reads if read.index == index)


def _elapsed_s(cycle_reads: Sequence[ObservedRead]) -> float:
    """How long one poll cycle took: from its first read beginning to its last read finishing."""
    return max(read.ended_at for read in cycle_reads) - min(read.started_at for read in cycle_reads)


# --------------------------------------------------------------------------------------------------
# the property
# --------------------------------------------------------------------------------------------------


# Feature: remote-energy-meter, Property 16: A poll cycle costs no more than its slowest read
# Validates: Requirements 5.3
@settings(max_examples=150, deadline=None)
@given(scenario=cycle_scenarios())
def test_a_poll_cycle_costs_no_more_than_its_slowest_read(scenario: CycleScenario) -> None:
    """Requirement 5.3, over two to six devices with any assignment of read latencies to them.

    **Validates: Requirements 5.3**

    Driven through ``asyncio.run`` from a synchronous test rather than as an async test, so each example
    gets its own event loop and the property does not depend on how a loop is shared with Hypothesis.
    Every second of the simulated schedule is virtual: six devices reading for an hour apiece cost a few
    hundred event-loop turns and no waiting.
    """
    with recording_logger() as (logger, records):
        observed = asyncio.run(_observe(scenario, logger, cycles=scenario.cycles))
    with recording_logger() as (control_logger, _control_records):
        control = asyncio.run(_observe(scenario, control_logger, cycles=1, sequential=True))

    device_ids = set(scenario.device_ids())
    latency_by_device = scenario.latency_by_device()

    # --- the run polled the devices it was given, for the cycles it was driven for ---
    # Asserted first, because every bound below is vacuous over a run that never happened.
    assert {read.device_id for read in observed.reads} == device_ids
    for device_id in device_ids:
        completed = len([read for read in observed.reads if read.device_id == device_id])
        assert completed >= scenario.cycles, f"{device_id} completed {completed} of {scenario.cycles} cycles"

    # --- no scheduled time was dropped, so the reads sharing an index are the reads of one cycle ---
    # Every generated read finishes inside its interval, so neither skip path of Requirement 5.3 or 5.9
    # may fire. Checked rather than assumed: a skip would leave one device's k-th read in a different
    # cycle from another's, and grouping by index below would then measure across unrelated reads.
    skips = [record for record in records if SKIP_MESSAGE_FRAGMENT in record.getMessage()]
    assert skips == [], f"a read inside its interval dropped a scheduled time: {len(skips)} skip(s)"

    # --- the reads really did spend their latency on the clock the property then measures ---
    # The guard against a vacuous measurement: if the clock did not move inside the reads, a sequential
    # implementation would cost the same as a concurrent one and every bound below would hold anyway.
    #
    # Stated as an end reading rather than as a duration: ``ended_at - started_at`` is a float
    # subtraction that need not return the value that was added, so an exact comparison against the
    # generated latency would fail on the test's own arithmetic. Comparing the reading the clock produced
    # against the same addition is exact, and is the thing that matters.
    for read in observed.reads:
        latency_s = latency_by_device[read.device_id]
        assert read.ended_at == read.started_at + latency_s, (
            f"{read.device_id} cycle {read.index} spent {read.duration_s:g} s, not its {latency_s:g} s read"
        )

    # --- no device has two power reading operations in flight simultaneously (Requirement 5.3) ---
    assert observed.peak_in_flight_per_device == 1, (
        f"{observed.peak_in_flight_per_device} reads were in flight for one device at once"
    )

    # --- the operations of one cycle are issued concurrently (Requirement 5.3) ---
    # Observed as overlap while it happens, not inferred from a duration: every device's read was in
    # flight at the same instant as every other's.
    assert observed.peak_in_flight_overall == scenario.device_count, (
        f"at most {observed.peak_in_flight_overall} of {scenario.device_count} reads overlapped"
    )

    # --- and so a cycle costs its slowest read, not the sum of its reads (Requirement 5.3) ---
    for index in range(scenario.cycles):
        cycle_reads = _reads_of_cycle(observed.reads, index)
        assert len(cycle_reads) == scenario.device_count
        assert {read.device_id for read in cycle_reads} == device_ids

        # The reads of one cycle all begin together, which is the requirement's "concurrently" read as an
        # instant rather than as a duration.
        spread_s = max(read.started_at for read in cycle_reads) - min(read.started_at for read in cycle_reads)
        assert spread_s <= VIRTUAL_CLOCK_SLACK_S, f"cycle {index} began over a {spread_s:g} s spread"

        elapsed_s = _elapsed_s(cycle_reads)
        assert elapsed_s <= scenario.slowest_s + TOLERANCE_S, (
            f"cycle {index} took {elapsed_s:g} s for a slowest read of {scenario.slowest_s:g} s"
            f" (sequentially it would have cost {scenario.sequential_cost_s:g} s)"
        )
        # Tightened to the virtual clock's noise floor: with concurrency modelled exactly, a cycle costs
        # its slowest read and nothing more, so this catches a millisecond of serialisation that the
        # requirement's second of slack would hide until several devices had accumulated it.
        assert elapsed_s <= scenario.slowest_s + VIRTUAL_CLOCK_SLACK_S, (
            f"cycle {index} took {elapsed_s - scenario.slowest_s:g} s longer than its slowest read"
        )

    # --- the bound has teeth: the same measurement fails a sequential implementation ---
    # Same scheduler, same clock, same measurement; only the recorder differs, queueing the reads on one
    # virtual server instead of running them side by side. If the harness could not tell the two apart,
    # the bounds above would be satisfied by arithmetic rather than by behaviour.
    control_reads = _reads_of_cycle(control.reads, 0)
    assert len(control_reads) == scenario.device_count
    control_elapsed_s = _elapsed_s(control_reads)
    assert abs(control_elapsed_s - scenario.sequential_cost_s) <= VIRTUAL_CLOCK_SLACK_S, (
        f"the sequential control took {control_elapsed_s:g} s, not the {scenario.sequential_cost_s:g} s"
        " sum of its reads, so it did not serialise and proves nothing"
    )
    if scenario.sequential_cost_s > scenario.slowest_s + TOLERANCE_S:
        assert control_elapsed_s > scenario.slowest_s + TOLERANCE_S, (
            f"a sequential cycle costing {control_elapsed_s:g} s passed a bound of"
            f" {scenario.slowest_s + TOLERANCE_S:g} s: this measurement cannot detect serialisation"
        )


# --------------------------------------------------------------------------------------------------
# Property 17: the retry rule, written out from the requirement
# --------------------------------------------------------------------------------------------------
#
# The expectation is derived from Requirement 5.5 and from nothing else. ``scheduler.RETRY_DELAYS_S`` and
# ``scheduler.MAX_ATTEMPTS_PER_CYCLE`` are deliberately *not* imported here: an oracle built from the
# implementation's own constants moves with them, so a delay schedule silently changed to 5 s and 10 s
# would still pass. Written out, the same change is a disagreement between two sources.

#: Requirement 5.5: 1 second before the first retry, 2 seconds before the second.
EXPECTED_RETRY_DELAYS_S: Final[tuple[float, ...]] = (1.0, 2.0)

#: Requirement 5.5: the initial attempt plus the 2 additional attempts it allows.
EXPECTED_MAX_ATTEMPTS: Final = 3

#: How long a single attempt may take, in seconds. Every value is an exact binary fraction, so summing
#: them in the clock's order reproduces the clock's reading exactly and the budget comparison is not
#: decided by rounding. The range spans an instant read, reads that fit inside a tight interval alongside
#: one or both retry delays, and reads that exhaust a 5 second interval on their own.
READ_DURATIONS_S: Final[tuple[float, ...]] = (0.0, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 8.0)

#: Poll intervals tight enough that the budget clause of Requirement 5.5, rather than the allowance of two
#: retries, is what ends the cycle. Sampled on purpose: a generator ranging over 5-3600 s would almost
#: always draw an interval with room for every retry, and the truncating cases are the interesting ones.
TIGHT_INTERVALS_S: Final[tuple[int, ...]] = (5, 6, 7, 8, 10)

#: How far above the slowest generated read the request timeout is set. Requirement 5.4's cancellation is
#: the unit layer's subject; here every attempt runs to completion, so what the retry schedule does with a
#: *classified* failure is not entangled with how that classification was produced.
TIMEOUT_HEADROOM_S: Final = 1.0

#: The single device Property 17 polls: the retry schedule is a per-device rule (Requirement 5.5).
RETRY_DEVICE_ID: Final = f"{DEVICE_ID_PREFIX}retry"

#: The reading a scripted successful attempt returns. Neither value matters to the retry schedule; both
#: are fixed so a failing example shrinks toward the timing, which does.
READING_WATTS: Final = 214.0
MEASURED_AT: Final = datetime(2025, 1, 1, tzinfo=UTC)

#: The detail every scripted failure carries.
FAILURE_DETAIL: Final = "generated"


@asynccontextmanager
async def _no_deadline(timeout_s: float) -> AsyncIterator[None]:
    """The injected timeout factory, standing in for ``asyncio.timeout`` and never firing.

    ``asyncio.timeout`` measures the event loop's own clock, which this harness does not advance: the
    simulated seconds a read spends are the virtual clock's, so a real deadline would be decided by how
    long the *test* took rather than by the scenario. Generated reads are kept strictly inside their
    request timeout instead -- ``timeout_s`` is set above the slowest of them -- and the property asserts
    that they were, so a deadline could not have fired in production either.
    """
    del timeout_s  # accepted to match the TimeoutFactory signature; no deadline is imposed
    yield


# --------------------------------------------------------------------------------------------------
# the scripted plugin and what it observed
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ObservedAttempt:
    """One power reading attempt that actually reached the plugin, on the virtual clock.

    Attributes:
        index: 1 for the initial attempt, 2 and 3 for the retries.
        started_at: the clock reading when the read began.
        ended_at: the clock reading when it returned.
        timeout_s: the request timeout the executor passed in, so the property can check the read stayed
            inside it rather than assuming so.
    """

    index: int
    started_at: float
    ended_at: float
    timeout_s: float


class ScriptedReads:
    """A plugin stand-in whose reads spend generated time and return generated outcomes.

    Only ``read_power`` is implemented, which is all :meth:`PollCycleExecutor.run_cycle` calls -- a plugin's
    validation operation belongs to startup (Requirement 3.4), not to a poll cycle.

    Every read is recorded, including one the requirement does not allow. The script's last step repeats
    rather than running out, because a read that raised would be caught by the executor and classified as a
    plugin failure: an implementation that retried a fourth time would then look like an ordinary retry
    instead of the defect it is. Recorded, it shows up as an attempt count the property fails on.
    """

    def __init__(self, clock: ConcurrentVirtualClock, scenario: RetryScenario) -> None:
        self._clock = clock
        self._scenario = scenario
        self.attempts: list[ObservedAttempt] = []

    async def read_power(self, timeout_s: float) -> ReadResult:
        """Spend this attempt's generated duration on the virtual clock, then return its outcome."""
        index = len(self.attempts) + 1
        started_at = self._clock.now
        await self._clock.sleep(self._scenario.duration_of(index))
        self.attempts.append(
            ObservedAttempt(index=index, started_at=started_at, ended_at=self._clock.now, timeout_s=timeout_s)
        )
        outcome = self._scenario.outcome_of(index)
        if outcome is None:
            return PowerReading(device_id=RETRY_DEVICE_ID, watts=READING_WATTS, measured_at=MEASURED_AT)
        return ReadFailure(device_id=RETRY_DEVICE_ID, classification=outcome, detail=FAILURE_DETAIL)


# --------------------------------------------------------------------------------------------------
# the scenario, and the schedule the requirement demands of it
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExpectedSchedule:
    """What Requirement 5.5 says one cycle of a scenario must do.

    Attributes:
        attempts: how many power reading attempts the cycle makes.
        delays_s: the delay waited before each retry, in order.
        elapsed_s: what the whole cycle costs -- every attempt plus every retry delay.
    """

    attempts: int
    delays_s: tuple[float, ...]
    elapsed_s: float


@dataclass(frozen=True, slots=True)
class RetryScenario:
    """One cycle: how each attempt ends, how long each takes, and the interval that bounds them.

    Attributes:
        interval_s: the device's poll interval in whole seconds, which Requirement 5.5 also makes the
            cycle's wall-clock budget.
        start_s: the clock reading when the cycle begins.
        failures: the classification each attempt fails with, unless that attempt is the one that succeeds.
        succeeds_at: the 1-based attempt that returns a reading, or ``None`` when every attempt fails. A
            position beyond the attempts the cycle actually makes is simply never reached, which is how a
            success scheduled after the budget cutoff is generated.
        read_durations_s: how long each attempt spends before returning.
    """

    interval_s: int
    start_s: float
    failures: tuple[FailureClassification, ...]
    succeeds_at: int | None
    read_durations_s: tuple[float, ...]

    @property
    def timeout_s(self) -> float:
        """A request timeout above every generated read, so no attempt is cut short by its deadline."""
        return max(self.read_durations_s) + TIMEOUT_HEADROOM_S

    def duration_of(self, index: int) -> float:
        """How long attempt ``index`` spends; the last script step repeats for any attempt beyond it."""
        return self.read_durations_s[min(index, len(self.read_durations_s)) - 1]

    def outcome_of(self, index: int) -> FailureClassification | None:
        """How attempt ``index`` ends: ``None`` for a reading, otherwise the classification it fails with."""
        if index == self.succeeds_at:
            return None
        return self.failures[min(index, len(self.failures)) - 1]

    def expected_schedule(self) -> ExpectedSchedule:
        """The attempts and delays Requirement 5.5 demands, computed from the requirement alone.

        One pass over the attempts, ending at the first of the four things the requirement says ends a
        cycle: a successful reading, an authentication failure, the third attempt, or a retry whose delay
        plus its own attempt timeout would carry the elapsed cycle time to the poll interval. The budget
        comparison is ``>=`` because the requirement stops once the elapsed time *reaches* the interval, so
        an attempt that could still be running at the next scheduled time does not begin -- otherwise the
        cycle overruns and the next scheduled poll is skipped.
        """
        attempts = 0
        elapsed_s = 0.0
        delays_s: list[float] = []
        for index in range(1, EXPECTED_MAX_ATTEMPTS + 1):
            elapsed_s += self.duration_of(index)
            attempts = index
            outcome = self.outcome_of(index)
            if outcome is None or outcome is FailureClassification.AUTHENTICATION:
                break
            if attempts >= EXPECTED_MAX_ATTEMPTS:
                break
            delay_s = EXPECTED_RETRY_DELAYS_S[attempts - 1]
            if elapsed_s + delay_s + self.timeout_s >= self.interval_s:
                break
            delays_s.append(delay_s)
            elapsed_s += delay_s
        return ExpectedSchedule(attempts=attempts, delays_s=tuple(delays_s), elapsed_s=elapsed_s)


# --------------------------------------------------------------------------------------------------
# generators
# --------------------------------------------------------------------------------------------------


def failure_scripts() -> st.SearchStrategy[tuple[FailureClassification, ...]]:
    """One classification per attempt, over all seven values of the enum.

    Repeated sequences are drawn as their own shape because they are the realistic ones -- an unreachable
    iDRAC times out three times running -- and a free-form list would rarely produce one.
    """
    classification = st.sampled_from(list(FailureClassification))
    return st.one_of(
        classification.map(lambda value: (value,) * EXPECTED_MAX_ATTEMPTS),
        st.lists(classification, min_size=EXPECTED_MAX_ATTEMPTS, max_size=EXPECTED_MAX_ATTEMPTS).map(tuple),
    )


def success_positions() -> st.SearchStrategy[int | None]:
    """Which attempt succeeds: the first, second, or third, or none of them."""
    return st.one_of(st.none(), st.integers(min_value=1, max_value=EXPECTED_MAX_ATTEMPTS))


def attempt_durations() -> st.SearchStrategy[tuple[float, ...]]:
    """How long each attempt spends, drawn from :data:`READ_DURATIONS_S`.

    Equal durations are drawn as their own shape: attempts that cost the same are what make the arithmetic
    of the budget cutoff land on its boundary, and they are also what a device that consistently times out
    actually produces.
    """
    duration = st.sampled_from(READ_DURATIONS_S)
    return st.one_of(
        duration.map(lambda value: (value,) * EXPECTED_MAX_ATTEMPTS),
        st.lists(duration, min_size=EXPECTED_MAX_ATTEMPTS, max_size=EXPECTED_MAX_ATTEMPTS).map(tuple),
    )


def retry_intervals() -> st.SearchStrategy[int]:
    """A poll interval over the whole accepted range, with the budget-truncating band sampled on purpose."""
    return st.one_of(intervals(), st.sampled_from(TIGHT_INTERVALS_S))


def retry_scenarios() -> st.SearchStrategy[RetryScenario]:
    """A whole cycle: an interval, a start reading, an outcome per attempt, and a duration per attempt."""
    return st.builds(
        RetryScenario,
        interval_s=retry_intervals(),
        start_s=start_readings(),
        failures=failure_scripts(),
        succeeds_at=success_positions(),
        read_durations_s=attempt_durations(),
    )


# --------------------------------------------------------------------------------------------------
# the driver
# --------------------------------------------------------------------------------------------------


async def _observe_cycle(
    scenario: RetryScenario,
    logger: logging.Logger,
) -> tuple[CycleResult, ScriptedReads]:
    """Run one real cycle for ``scenario`` on the virtual clock, and return it with what the plugin saw.

    The real :class:`PollCycleExecutor`, called directly: the retry schedule is the executor's behaviour,
    and driving it through the loop would add scheduled times the property does not quantify over. One
    participant on the clock, because one cycle is one task -- every sleep here is the executor's retry
    delay or the plugin's read, and both are issued from this coroutine.
    """
    clock = ConcurrentVirtualClock(scenario.start_s, participants=1)
    plugin = ScriptedReads(clock, scenario)
    device = cast(
        "ResolvedDevice",
        _FakeDevice(
            RETRY_DEVICE_ID,
            scenario.interval_s,
            timeout_s=scenario.timeout_s,
            plugin=plugin,
        ),
    )
    executor = PollCycleExecutor(clock=clock, sleep=clock.sleep, timeout=_no_deadline, logger=logger)
    cycle = await executor.run_cycle(device)
    return cycle, plugin


# --------------------------------------------------------------------------------------------------
# the property
# --------------------------------------------------------------------------------------------------


# Feature: remote-energy-meter, Property 17: The retry schedule follows classification and budget
# Validates: Requirements 5.5
@settings(max_examples=150, deadline=None)
@given(scenario=retry_scenarios())
def test_the_retry_schedule_follows_classification_and_budget(scenario: RetryScenario) -> None:
    """Requirement 5.5, over every classification, every success position, and the whole interval range.

    **Validates: Requirements 5.5**

    The attempts are counted and timed from the plugin's side rather than read out of the executor's own
    bookkeeping, and the two are then required to agree: a cycle that reported one retry schedule while
    performing another would satisfy either check alone.
    """
    with recording_logger() as (logger, _records):
        cycle, plugin = asyncio.run(_observe_cycle(scenario, logger))

    expected = scenario.expected_schedule()
    attempts = plugin.attempts
    observed_delays_s = tuple(later.started_at - earlier.ended_at for earlier, later in pairwise(attempts))

    # --- the attempts really ran, and really spent their time on the clock the budget is measured on ---
    # Asserted first: if the clock did not move inside a read, every attempt would appear instantaneous and
    # the budget clause below could never truncate anything, so the property would pass on arithmetic.
    assert attempts, "the cycle made no power reading attempt at all"
    for attempt in attempts:
        duration_s = scenario.duration_of(attempt.index)
        assert attempt.ended_at == attempt.started_at + duration_s, (
            f"attempt {attempt.index} spent {attempt.ended_at - attempt.started_at:g} s, not its {duration_s:g} s read"
        )
        # And it was never cut short by its deadline, so this is the retry schedule after a *classified*
        # failure rather than a mixture of that and Requirement 5.4's cancellation.
        assert duration_s < attempt.timeout_s, (
            f"attempt {attempt.index} read for {duration_s:g} s under a {attempt.timeout_s:g} s timeout"
        )

    # --- the number of attempts is the one Requirement 5.5 allows ---
    assert len(attempts) == expected.attempts, (
        f"{len(attempts)} attempts for {scenario.failures} at a {scenario.interval_s} s interval"
        f" with {scenario.read_durations_s} s reads, expected {expected.attempts}"
    )
    assert cycle.attempt_count == len(attempts), (
        f"the cycle reported {cycle.attempt_count} attempts and the plugin saw {len(attempts)}"
    )
    assert cycle.classifications == tuple(
        outcome for outcome in (scenario.outcome_of(attempt.index) for attempt in attempts) if outcome is not None
    )

    # --- and the gaps between them are 1 s then 2 s, as far as the cycle got ---
    assert observed_delays_s == expected.delays_s, (
        f"waited {observed_delays_s} s between attempts, expected {expected.delays_s} s"
    )
    assert cycle.retry_delays_s == observed_delays_s, (
        f"the cycle reported delays of {cycle.retry_delays_s} s and the plugin observed {observed_delays_s} s"
    )
    # The whole cycle cost exactly its attempts plus its delays: nothing waited on anything else, and in
    # particular nothing waited on a real clock.
    assert cycle.duration_s == expected.elapsed_s, (
        f"the cycle cost {cycle.duration_s:g} s of simulated time, not the {expected.elapsed_s:g} s"
        " of its attempts and delays"
    )

    # --- an authentication failure is never retried (Requirement 5.5) ---
    # Stated over every attempt, not only the first: a credential refused on the second attempt must end
    # the cycle there just as surely.
    retried = [scenario.outcome_of(attempt.index) for attempt in attempts[:-1]]
    assert FailureClassification.AUTHENTICATION not in retried, (
        f"a refused credential was retried: outcomes {retried} were followed by another attempt"
    )
    if scenario.outcome_of(1) is FailureClassification.AUTHENTICATION:
        assert len(attempts) == 1, f"an authentication failure took {len(attempts)} attempts"
        assert observed_delays_s == ()

    # --- a success ends the cycle, and nothing is attempted after it ---
    if cycle.succeeded:
        assert len(attempts) == scenario.succeeds_at, (
            f"the reading came on attempt {scenario.succeeds_at} but {len(attempts)} attempts were made"
        )
    else:
        assert scenario.succeeds_at is None or scenario.succeeds_at > len(attempts), (
            f"the cycle failed although attempt {scenario.succeeds_at} of {len(attempts)} returned a reading"
        )

    # --- no attempt starts once the elapsed cycle time reaches the poll interval (Requirement 5.5) ---
    # Stronger than "starts before the interval": a retry must also be able to *finish* within it, so the
    # cycle never runs into the next scheduled poll and causes it to be skipped.
    for attempt in attempts:
        offset_s = attempt.started_at - scenario.start_s
        assert offset_s < scenario.interval_s, (
            f"attempt {attempt.index} started {offset_s:g} s into a {scenario.interval_s} s interval,"
            " at or after the next scheduled poll"
        )
        if attempt.index > 1:
            assert offset_s + scenario.timeout_s < scenario.interval_s, (
                f"retry {attempt.index} started {offset_s:g} s in with a {scenario.timeout_s:g} s timeout,"
                f" so it could run past the {scenario.interval_s} s interval"
            )

    # --- and the cycle stopped short only when the budget made it: the count is maximal ---
    # The other side of the clause above. Without it, an implementation that gave up after one retry would
    # satisfy every bound here; with it, a cycle that ended early while its classification was retryable,
    # its allowance unspent, and its budget intact is a failure.
    final_outcome = scenario.outcome_of(len(attempts))
    ended_early = len(attempts) < EXPECTED_MAX_ATTEMPTS
    retryable = final_outcome is not None and final_outcome is not FailureClassification.AUTHENTICATION
    if ended_early and retryable:
        elapsed_s = attempts[-1].ended_at - scenario.start_s
        next_delay_s = EXPECTED_RETRY_DELAYS_S[len(attempts) - 1]
        assert elapsed_s + next_delay_s + scenario.timeout_s >= scenario.interval_s, (
            f"the cycle stopped after {len(attempts)} attempts on a retryable {final_outcome} with"
            f" {scenario.interval_s - elapsed_s:g} s of its budget left, enough for a {next_delay_s:g} s delay"
            f" and a {scenario.timeout_s:g} s attempt"
        )


# --------------------------------------------------------------------------------------------------
# Property 18: what a fully failed cycle reports, and what it must not contribute
# --------------------------------------------------------------------------------------------------
#
# Requirement 5.6 makes three claims about a cycle in which no attempt succeeded, and each one is checked
# against a real collaborator rather than against a recorder that agrees with the executor by construction.
#
# * **it reports the device unavailable.** The executor calls exactly one of two sink methods per cycle, so
#   this is a claim about a call sequence: ``report_unavailable`` once, ``deliver_reading`` never.
# * **it passes no Power_Reading to the Energy_Accumulator.** Observed on the accumulator rather than on the
#   sink: the real :class:`EnergyAccumulator` sits behind the real :class:`AccumulateThenPublish` -- the sink
#   the application wires up in production -- seeded with a nonzero total and a live baseline, and a failed
#   cycle must leave the total at exactly the value it went in at. The seeding is what makes that assertion
#   mean something: against a fresh accumulator "unchanged" is zero compared against zero, which a sink that
#   dropped every reading it was ever handed would satisfy just as happily.
# * **it continues at the next scheduled time.** So the sequence runs through the real :class:`PollScheduler`
#   loop rather than by calling :meth:`PollCycleExecutor.run_cycle` several times in a row: what the next
#   scheduled time is, and whether a failed cycle costs a device its place in the schedule, is the loop's
#   business and not the harness's to decide.
#
# The positive contrast is what stops all three from being satisfied by an implementation that never reports
# a device available and never accumulates anything. Every example is run a second time through the same
# harness -- same executor, same sink, same accumulator, same schedule -- with only the script changed so
# that every cycle succeeds on its first attempt, and that run is required to deliver each reading and to
# move the total.

#: The single device the sequence polls. Requirement 5.6 is a per-device rule, and device isolation across a
#: fault is Property 7's subject (task 11.11).
SEQUENCE_DEVICE_ID: Final = f"{DEVICE_ID_PREFIX}sequence"

#: How many consecutive cycles one sequence covers. Two is the floor: "is followed by a further cycle at the
#: next scheduled time" needs a further cycle to observe. Three is the ceiling, and the reason is the
#: accumulator's -- Requirement 7.5 discards the interval between two readings more than 4 poll intervals
#: apart, and in a run where only the last cycle succeeds the gap from the seeded baseline to that reading is
#: as many intervals as there are cycles. Three leaves a whole interval of margin under that threshold, so
#: nothing here is decided by where Requirement 7.5's boundary falls.
MIN_SEQUENCE_CYCLES: Final = 2
MAX_SEQUENCE_CYCLES: Final = 3

#: Sequences of success positions worth drawing on purpose: ``None`` is a cycle that fails throughout, an
#: integer is the attempt that returns a reading. All failing is the case Requirement 5.6 is written for; all
#: succeeding is its complement; the mixed shapes are where an implementation that latched a device offline,
#: or that never left it offline again, would be caught. A free-form generator draws these rarely.
SUCCESS_SEQUENCES: Final[tuple[tuple[int | None, ...], ...]] = (
    (None, None, None),
    (None, None),
    (1, 1, 1),
    (None, 1, None),
    (1, None, 1),
    (None, 3),
    (3, None, 2),
)

#: The retry delays Requirement 5.5 allows one cycle, in seconds. Subtracted from the poll interval to get
#: the time a cycle's *reads* may spend without the cycle outrunning its interval.
TOTAL_RETRY_DELAY_S: Final = sum(EXPECTED_RETRY_DELAYS_S)

#: What share of that read budget one attempt may spend. Three attempts at the largest share come to 0.75 of
#: the budget, so a whole cycle -- reads and both delays -- stays strictly inside one poll interval and no
#: scheduled time is dropped. Every share is an exact binary fraction, and the budget is a whole number of
#: seconds less 3, so an attempt's duration is exact and the clock's arithmetic is not what decides whether a
#: cycle fitted.
ATTEMPT_SHARES: Final[tuple[float, ...]] = (0.0, 0.0625, 0.125, 0.25)

#: The cumulative total the accumulator is seeded with, in kilowatt-hours, so that "unchanged" is a value a
#: wrongly delivered reading would visibly move off of rather than zero compared against zero.
SEED_TOTAL_KWH: Final = 12.5

#: The live baseline seeded alongside that total. A first reading is retained as the baseline and contributes
#: nothing (Requirement 7.7), so without a baseline already in place the contrast run's first cycle could not
#: contribute either, and "a successful cycle moves the total" would prove nothing.
BASELINE_WATTS: Final = 200.0

#: The two downstream reports a completed cycle can make, as the recording publisher tags them.
READING_CALL: Final = "reading"
UNAVAILABLE_CALL: Final = "unavailable"

#: The message fragment the executor writes when a downstream report raised and was contained
#: (Requirement 5.6's report may not end a device's loop).
SINK_FAILURE_FRAGMENT: Final = "failed to report"


# --------------------------------------------------------------------------------------------------
# the recording publisher, and the reports it saw
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SinkCall:
    """One downstream report a cycle made, as the publisher received it.

    Attributes:
        kind: :data:`READING_CALL` or :data:`UNAVAILABLE_CALL`, which of the two sink methods was called.
        device_id: which device the report named, so a report cannot be credited to the wrong entry.
        watts: the power value of a delivered reading, or ``None`` for an unavailability report.
        classification: why the cycle failed, or ``None`` for a delivered reading.
        duration_ms: the operation duration published with a reading (Requirement 9.5), or ``None``.
    """

    kind: str
    device_id: str
    watts: float | None = None
    classification: FailureClassification | None = None
    duration_ms: int | None = None


class RecordingPublisher:
    """A publisher stand-in that records what it is handed and publishes nothing.

    The whole ``app.PublisherPort`` surface is implemented, so the real :class:`AccumulateThenPublish` can be
    wired to it without a cast; the three lifecycle methods belong to startup and shutdown and are never
    reached by a poll cycle.

    Records rather than asserts, on purpose. The executor contains a raising sink and carries on
    (Requirement 5.6's report must not end a device's loop), so an assertion raised in here would be
    swallowed and reported as a log record instead of failing the property.
    """

    def __init__(self) -> None:
        self.calls: list[SinkCall] = []

    async def start(self) -> None:
        """Startup only; never reached by a poll cycle."""

    async def stop(self) -> None:
        """Shutdown only; never reached by a poll cycle."""

    async def publish_all_offline(self) -> None:
        """Shutdown only; never reached by a poll cycle."""

    async def deliver_reading(self, device: ResolvedDevice, reading: PowerReading, duration_ms: int) -> None:
        """Record one successful cycle's publication."""
        self.calls.append(
            SinkCall(
                kind=READING_CALL,
                device_id=device.device_id,
                watts=reading.watts,
                duration_ms=duration_ms,
            )
        )

    async def report_unavailable(self, device: ResolvedDevice, failure: ReadFailure) -> None:
        """Record one fully failed cycle's unavailability report (Requirements 5.6, 6.8)."""
        self.calls.append(
            SinkCall(
                kind=UNAVAILABLE_CALL,
                device_id=device.device_id,
                classification=failure.classification,
            )
        )


# --------------------------------------------------------------------------------------------------
# the scenario: a sequence of cycles, each with its own outcome
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CycleScript:
    """What one cycle of the sequence does.

    Attributes:
        succeeds_at: the 1-based attempt that returns a reading, or ``None`` when no attempt does. A position
            beyond the attempts the cycle actually makes is simply never reached, which is how a cycle that
            fails throughout *because its budget ran out* is generated.
        failures: the classification each attempt fails with, unless that attempt is the one that succeeds.
    """

    succeeds_at: int | None
    failures: tuple[FailureClassification, ...]

    @property
    def fails_throughout(self) -> bool:
        """Whether no attempt of this cycle returns a reading, however many attempts it gets to make."""
        return self.succeeds_at is None

    def outcome_of(self, attempt: int) -> FailureClassification | None:
        """How attempt ``attempt`` ends: ``None`` for a reading, otherwise the classification it fails with."""
        if attempt == self.succeeds_at:
            return None
        return self.failures[min(attempt, len(self.failures)) - 1]


@dataclass(frozen=True, slots=True)
class SequenceScenario:
    """One run: consecutive cycles for a single device, and what each of them does.

    Attributes:
        interval_s: the device's poll interval in whole seconds, which is both the schedule and the cycle
            budget of Requirement 5.5.
        start_s: the clock reading at Poll_Scheduler start.
        scripts: one script per cycle, in the order the cycles run.
        attempt_shares: how long each attempt within any cycle spends, as a share of the read budget.
    """

    interval_s: int
    start_s: float
    scripts: tuple[CycleScript, ...]
    attempt_shares: tuple[float, ...]

    @property
    def cycles(self) -> int:
        """How many consecutive cycles this run covers."""
        return len(self.scripts)

    @property
    def read_budget_s(self) -> float:
        """How long one cycle's reads may take in total without the cycle outrunning its poll interval."""
        return self.interval_s - TOTAL_RETRY_DELAY_S

    def duration_of(self, attempt: int) -> float:
        """How long attempt ``attempt`` spends; the last script step repeats for any attempt beyond it."""
        share = self.attempt_shares[min(attempt, len(self.attempt_shares)) - 1]
        return share * self.read_budget_s

    @property
    def timeout_s(self) -> float:
        """A request timeout above every generated read, so no attempt is cut short by its deadline."""
        longest_s = max(self.duration_of(attempt) for attempt in range(1, EXPECTED_MAX_ATTEMPTS + 1))
        return longest_s + TIMEOUT_HEADROOM_S

    def scheduled_at(self, cycle: int) -> float:
        """The clock reading cycle ``cycle`` is due at: the start, plus one interval per cycle before it."""
        return self.start_s + cycle * self.interval_s

    def measured_at(self, cycle: int) -> datetime:
        """The measurement timestamp a reading from cycle ``cycle`` carries, one interval apart per cycle."""
        return MEASURED_AT + timedelta(seconds=cycle * self.interval_s)

    @property
    def baseline_at(self) -> datetime:
        """When the seeded baseline was measured: one interval before the first cycle, as a real one would be."""
        return MEASURED_AT - timedelta(seconds=self.interval_s)

    def all_succeeding(self) -> SequenceScenario:
        """The same run with every cycle succeeding on its first attempt: the positive contrast."""
        return replace(self, scripts=tuple(replace(script, succeeds_at=1) for script in self.scripts))


# --------------------------------------------------------------------------------------------------
# the scripted plugin, and what one driven sequence produced
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SequencedRead:
    """One power reading attempt the plugin served, tagged with the cycle it belonged to.

    Attributes:
        cycle: which cycle of the sequence this attempt was part of, counting from zero.
        attempt: 1 for that cycle's initial attempt, 2 and 3 for its retries.
        started_at: the clock reading when the read began.
        ended_at: the clock reading when it returned.
        timeout_s: the request timeout the executor passed in, so the property can check the read stayed
            inside it rather than assuming so.
    """

    cycle: int
    attempt: int
    started_at: float
    ended_at: float
    timeout_s: float


class SequencedReads:
    """A plugin stand-in whose reads follow the script of whichever cycle is currently running.

    Only ``read_power`` is implemented, which is all :meth:`PollCycleExecutor.run_cycle` calls. The cycle
    index is set by the runner that brackets each cycle rather than inferred from the attempt count, because
    how many attempts a cycle makes is exactly what the implementation decides and the harness must not
    assume.

    A successful cycle's reading carries a measurement timestamp one interval later than the previous
    cycle's, which is what a device polled on schedule reports and what lets the accumulator integrate
    across two successive readings instead of re-baselining after a gap (Requirement 7.5).
    """

    def __init__(self, clock: ConcurrentVirtualClock, scenario: SequenceScenario) -> None:
        self._clock = clock
        self._scenario = scenario
        self._cycle = 0
        self._attempts = 0
        self.reads: list[SequencedRead] = []

    def begin_cycle(self, cycle: int) -> None:
        """Switch to cycle ``cycle``'s script, from its first attempt."""
        self._cycle = cycle
        self._attempts = 0

    async def read_power(self, timeout_s: float) -> ReadResult:
        """Spend this attempt's generated duration on the virtual clock, then return its scripted outcome."""
        attempt = self._attempts + 1
        self._attempts = attempt
        started_at = self._clock.now
        await self._clock.sleep(self._scenario.duration_of(attempt))
        self.reads.append(
            SequencedRead(
                cycle=self._cycle,
                attempt=attempt,
                started_at=started_at,
                ended_at=self._clock.now,
                timeout_s=timeout_s,
            )
        )
        outcome = self._scenario.scripts[self._cycle].outcome_of(attempt)
        if outcome is None:
            return PowerReading(
                device_id=SEQUENCE_DEVICE_ID,
                watts=READING_WATTS,
                measured_at=self._scenario.measured_at(self._cycle),
            )
        return ReadFailure(device_id=SEQUENCE_DEVICE_ID, classification=outcome, detail=FAILURE_DETAIL)


@dataclass(frozen=True, slots=True)
class ObservedCycle:
    """One cycle that ran: what it returned, what it reported, and what it did to the cumulative total.

    Attributes:
        index: which cycle of the sequence this was, counting from zero.
        started_at: the clock reading when the cycle began, for the schedule clause of Requirement 5.6.
        result: the :class:`CycleResult` the real executor returned.
        state_before: the device's accumulator state immediately before the cycle.
        state_after: the same immediately after it, so "unchanged" covers the retained baseline as well as
            the total.
        reports: the downstream reports this cycle made, in order.
    """

    index: int
    started_at: float
    result: CycleResult
    state_before: DeviceEnergyState
    state_after: DeviceEnergyState
    reports: tuple[SinkCall, ...]

    @property
    def kinds(self) -> tuple[str, ...]:
        """Which downstream reports this cycle made, in order."""
        return tuple(call.kind for call in self.reports)

    @property
    def contributed_kwh(self) -> float:
        """What this cycle added to the device's cumulative energy total, in kilowatt-hours."""
        return self.state_after.total_kwh - self.state_before.total_kwh


@dataclass(frozen=True, slots=True)
class SequenceObservation:
    """Everything one driven sequence produced, for the property to assert over.

    Attributes:
        cycles: one entry per cycle that ran, in order.
        reads: every attempt the plugin served across the whole run.
        reports: every downstream report the publisher received across the whole run.
        initial_total_kwh: the cumulative total after seeding, before the first cycle.
        final_total_kwh: the cumulative total after the last cycle.
    """

    cycles: tuple[ObservedCycle, ...]
    reads: tuple[SequencedRead, ...]
    reports: tuple[SinkCall, ...]
    initial_total_kwh: float
    final_total_kwh: float

    def reads_of(self, cycle: int) -> tuple[SequencedRead, ...]:
        """The attempts served during cycle ``cycle``."""
        return tuple(read for read in self.reads if read.cycle == cycle)


class SequencedCycles:
    """Runs each cycle through the real executor, and records what it did to the sink and the accumulator.

    A wrapper, not a substitute: every cycle here is a real :meth:`PollCycleExecutor.run_cycle`, called by
    the real per-device loop. The wrapper exists for two reasons. It brackets each cycle, so the cumulative
    total and the reports of *one* cycle are observable without asking either collaborator to keep a history;
    and it ends the run, raising :class:`_EnoughCycles` once the requested cycles are done -- raised here,
    outside the executor, because an exception raised inside a read would be caught and classified as a
    plugin failure (Requirement 3.7) and would never reach the task group.
    """

    def __init__(
        self,
        executor: PollCycleExecutor,
        plugin: SequencedReads,
        accumulator: EnergyAccumulator,
        publisher: RecordingPublisher,
        clock: ConcurrentVirtualClock,
        wanted: int,
    ) -> None:
        """Run ``wanted`` cycles through ``executor``, then end the run."""
        self._executor = executor
        self._plugin = plugin
        self._accumulator = accumulator
        self._publisher = publisher
        self._clock = clock
        self._wanted = wanted
        self.observed: list[ObservedCycle] = []

    async def run_cycle(self, device: ResolvedDevice) -> CycleResult:
        """Run one real cycle for ``device`` and record it, ending the run once enough have run."""
        index = len(self.observed)
        self._plugin.begin_cycle(index)
        reported = len(self._publisher.calls)
        state_before = self._accumulator.state_of(device.device_id)
        started_at = self._clock.now

        result = await self._executor.run_cycle(device)

        self.observed.append(
            ObservedCycle(
                index=index,
                started_at=started_at,
                result=result,
                state_before=state_before,
                state_after=self._accumulator.state_of(device.device_id),
                reports=tuple(self._publisher.calls[reported:]),
            )
        )
        if len(self.observed) >= self._wanted:
            raise _EnoughCycles
        return result


# --------------------------------------------------------------------------------------------------
# generators
# --------------------------------------------------------------------------------------------------


def success_sequences() -> st.SearchStrategy[tuple[int | None, ...]]:
    """Where each cycle of the sequence succeeds, if anywhere, with the notable shapes sampled."""
    return st.one_of(
        st.lists(
            success_positions(),
            min_size=MIN_SEQUENCE_CYCLES,
            max_size=MAX_SEQUENCE_CYCLES,
        ).map(tuple),
        st.sampled_from(SUCCESS_SEQUENCES),
    )


@st.composite
def cycle_sequences(draw: st.DrawFn) -> tuple[CycleScript, ...]:
    """One script per cycle: where that cycle succeeds, if anywhere, and how each of its attempts fails."""
    positions = draw(success_sequences())
    return tuple(CycleScript(succeeds_at=position, failures=draw(failure_scripts())) for position in positions)


def attempt_share_scripts() -> st.SearchStrategy[tuple[float, ...]]:
    """How long each attempt spends, as a share of the read budget, equal shares drawn as their own shape."""
    share = st.sampled_from(ATTEMPT_SHARES)
    return st.one_of(
        share.map(lambda value: (value,) * EXPECTED_MAX_ATTEMPTS),
        st.lists(share, min_size=EXPECTED_MAX_ATTEMPTS, max_size=EXPECTED_MAX_ATTEMPTS).map(tuple),
    )


def sequence_scenarios() -> st.SearchStrategy[SequenceScenario]:
    """A whole run: an interval, a start reading, a script per cycle, and a duration per attempt."""
    return st.builds(
        SequenceScenario,
        interval_s=retry_intervals(),
        start_s=start_readings(),
        scripts=cycle_sequences(),
        attempt_shares=attempt_share_scripts(),
    )


# --------------------------------------------------------------------------------------------------
# the driver
# --------------------------------------------------------------------------------------------------


async def _observe_sequence(scenario: SequenceScenario, logger: logging.Logger) -> SequenceObservation:
    """Drive ``scenario`` through the real loop, executor, sink, and accumulator, and return what happened.

    Returns once the requested cycles have run and the last of them raised :class:`_EnoughCycles`, which the
    scheduler's ``TaskGroup`` re-raises as an exception group. One participant on the clock: one device, and
    every sleep in the run -- the loop's wait for the next scheduled time, the executor's retry delays, and
    the plugin's reads -- is issued from that device's task.
    """
    clock = ConcurrentVirtualClock(scenario.start_s, participants=1)

    accumulator = EnergyAccumulator()
    accumulator.register(
        SEQUENCE_DEVICE_ID,
        scenario.interval_s,
        restored=DeviceEnergyState(total_kwh=SEED_TOTAL_KWH),
    )
    accumulator.record(
        PowerReading(device_id=SEQUENCE_DEVICE_ID, watts=BASELINE_WATTS, measured_at=scenario.baseline_at)
    )
    initial_total_kwh = accumulator.total_kwh(SEQUENCE_DEVICE_ID)

    publisher = RecordingPublisher()
    plugin = SequencedReads(clock, scenario)
    device = cast(
        "ResolvedDevice",
        _FakeDevice(
            SEQUENCE_DEVICE_ID,
            scenario.interval_s,
            timeout_s=scenario.timeout_s,
            plugin=plugin,
        ),
    )
    executor = PollCycleExecutor(
        sink=AccumulateThenPublish(accumulator, publisher),
        clock=clock,
        sleep=clock.sleep,
        timeout=_no_deadline,
        logger=logger,
    )
    runner = SequencedCycles(executor, plugin, accumulator, publisher, clock, wanted=scenario.cycles)
    scheduler = PollScheduler([device], executor=runner, clock=clock, sleep=clock.sleep, logger=logger)

    try:  # noqa: SIM105 - contextlib.suppress does not catch except*, the TaskGroup's exception group
        await scheduler.run()
    except* _EnoughCycles:
        pass

    return SequenceObservation(
        cycles=tuple(runner.observed),
        reads=tuple(plugin.reads),
        reports=tuple(publisher.calls),
        initial_total_kwh=initial_total_kwh,
        final_total_kwh=accumulator.total_kwh(SEQUENCE_DEVICE_ID),
    )


# --------------------------------------------------------------------------------------------------
# the property
# --------------------------------------------------------------------------------------------------


# Feature: remote-energy-meter, Property 18: A fully failed cycle reports unavailable and contributes no energy
# Validates: Requirements 5.6
@settings(max_examples=150, deadline=None)
@given(scenario=sequence_scenarios())
def test_a_fully_failed_cycle_reports_unavailable_and_contributes_no_energy(scenario: SequenceScenario) -> None:
    """Requirement 5.6, over any sequence of per-cycle outcomes and any mix of the seven classifications.

    **Validates: Requirements 5.6**

    Two runs of the same harness per example: the generated sequence, which says what a failed cycle must not
    do, and the same sequence with every cycle succeeding, which says the harness can tell the difference.
    """
    with recording_logger() as (logger, records):
        observed = asyncio.run(_observe_sequence(scenario, logger))
    with recording_logger() as (contrast_logger, contrast_records):
        contrast = asyncio.run(_observe_sequence(scenario.all_succeeding(), contrast_logger))

    # --- the whole sequence ran, and every cycle of it really reached the device ---
    # Asserted first, because every clause below is vacuous over a run that stopped early or over a cycle
    # whose result the executor produced without asking the plugin for anything.
    assert len(observed.cycles) == scenario.cycles, (
        f"{len(observed.cycles)} of {scenario.cycles} cycles ran: the sequence did not continue"
    )
    for cycle in observed.cycles:
        served = observed.reads_of(cycle.index)
        assert served, f"cycle {cycle.index} produced a result without reading the device"
        assert len(served) == cycle.result.attempt_count, (
            f"cycle {cycle.index} reported {cycle.result.attempt_count} attempts, the plugin served {len(served)}"
        )

    # --- the reads really spent their time on the clock the schedule is measured on, inside their timeout ---
    for read in observed.reads:
        duration_s = scenario.duration_of(read.attempt)
        assert read.ended_at == read.started_at + duration_s, (
            f"cycle {read.cycle} attempt {read.attempt} spent {read.ended_at - read.started_at:g} s,"
            f" not its {duration_s:g} s read"
        )
        assert duration_s < read.timeout_s, (
            f"cycle {read.cycle} attempt {read.attempt} read for {duration_s:g} s under a {read.timeout_s:g} s timeout"
        )

    # --- the seeded total survived baselining, so "unchanged" is a nonzero value throughout ---
    assert observed.initial_total_kwh == SEED_TOTAL_KWH, (
        f"the run began at {observed.initial_total_kwh:g} kWh, not the seeded {SEED_TOTAL_KWH:g} kWh"
    )

    # --- no downstream report failed, so what the publisher recorded is what the accumulator saw ---
    # A raising accumulator is contained by the executor and logged, because Requirement 5.6's report must
    # not end a device's loop -- which would leave a cycle looking as though it had reported nothing at all.
    contained = [record for record in records if SINK_FAILURE_FRAGMENT in record.getMessage()]
    assert contained == [], f"a downstream report failed: {[record.getMessage() for record in contained]}"

    # --- and no scheduled time was dropped, so the cycle that followed is the one that was next due ---
    # Every generated cycle fits inside its interval by construction; a skip would mean it did not, and the
    # schedule clause below would then be measured against times Requirement 5.9 had already abandoned.
    skips = [record for record in records if SKIP_MESSAGE_FRAGMENT in record.getMessage()]
    assert skips == [], f"a cycle inside its interval dropped a scheduled time: {len(skips)} skip(s)"

    for cycle in observed.cycles:
        script = scenario.scripts[cycle.index]

        # --- the harness and the executor agree on which cycles obtained a reading ---
        if script.fails_throughout:
            assert not cycle.result.succeeded, (
                f"cycle {cycle.index} obtained a reading although every attempt was scripted to fail"
            )
        if not cycle.result.succeeded:
            assert script.succeeds_at is None or script.succeeds_at > cycle.result.attempt_count, (
                f"cycle {cycle.index} failed although attempt {script.succeeds_at}"
                f" of its {cycle.result.attempt_count} returned a reading"
            )

        if cycle.result.succeeded:
            # --- the contrast, cycle by cycle: a reading is reported and does move the total ---
            reading = cycle.result.reading
            assert reading is not None
            assert cycle.kinds == (READING_CALL,), (
                f"cycle {cycle.index} obtained {reading.watts:g} W and reported {cycle.kinds}"
            )
            assert cycle.reports[0].watts == reading.watts
            assert cycle.contributed_kwh > 0.0, (
                f"cycle {cycle.index} delivered {reading.watts:g} W and added nothing to the total"
            )
            continue

        # --- Requirement 5.6: a fully failed cycle reports the device unavailable, and nothing else ---
        failure = cycle.result.failure
        assert failure is not None
        assert cycle.kinds == (UNAVAILABLE_CALL,), (
            f"cycle {cycle.index} failed on {cycle.result.classifications} and reported {cycle.kinds}"
        )
        assert cycle.reports[0].device_id == SEQUENCE_DEVICE_ID
        assert cycle.reports[0].classification is failure.classification

        # --- Requirement 5.6: and it passes no Power_Reading to the Energy_Accumulator ---
        # The whole state, not only the total: a failed cycle that re-baselined would leave the total
        # untouched now and silently change what the next successful cycle integrates.
        assert cycle.state_after == cycle.state_before, (
            f"cycle {cycle.index} failed on {cycle.result.classifications} and moved the accumulator from"
            f" {cycle.state_before} to {cycle.state_after}"
        )
        assert cycle.contributed_kwh == 0.0

    # --- Requirement 5.6: a failed cycle is followed by a further cycle at the next scheduled time ---
    # A tolerance rather than an equality, because the loop reaches its deadline by adding a remaining span
    # to the current reading, and ``now + (deadline - now)`` need not return the deadline exactly. A
    # microsecond is six orders of magnitude below the shortest interval the requirements allow.
    for cycle in observed.cycles:
        drift_s = cycle.started_at - scenario.scheduled_at(cycle.index)
        assert abs(drift_s) <= VIRTUAL_CLOCK_SLACK_S, f"cycle {cycle.index} began {drift_s:g} s off its scheduled time"
    for earlier, later in pairwise(observed.cycles):
        assert later.index == earlier.index + 1
        gap_s = later.started_at - earlier.started_at
        assert abs(gap_s - scenario.interval_s) <= VIRTUAL_CLOCK_SLACK_S, (
            f"the cycle after {'a failed' if not earlier.result.succeeded else 'a successful'} cycle"
            f" {earlier.index} began {gap_s:g} s later, not one {scenario.interval_s} s interval"
        )

    # --- and over a run in which nothing succeeded, the cumulative total is exactly where it started ---
    if all(not cycle.result.succeeded for cycle in observed.cycles):
        assert [call.kind for call in observed.reports] == [UNAVAILABLE_CALL] * scenario.cycles, (
            f"a run of {scenario.cycles} failed cycles reported {[call.kind for call in observed.reports]}"
        )
        assert observed.final_total_kwh == observed.initial_total_kwh, (
            f"a run in which every cycle failed moved the total from {observed.initial_total_kwh:g}"
            f" to {observed.final_total_kwh:g} kWh"
        )

    # --- the contrast: the same harness does report a device available, and does accumulate ---
    # Same executor, same sink, same accumulator, same schedule; only the script differs. Without this, every
    # clause above would be satisfied by an implementation that reported nothing but unavailability and never
    # accumulated anything at all.
    contained_contrast = [record for record in contrast_records if SINK_FAILURE_FRAGMENT in record.getMessage()]
    assert contained_contrast == [], f"a report of the contrast run failed: {len(contained_contrast)} contained"
    assert len(contrast.cycles) == scenario.cycles
    assert [call.kind for call in contrast.reports] == [READING_CALL] * scenario.cycles, (
        f"a run of successful cycles reported {[call.kind for call in contrast.reports]}"
    )
    for cycle in contrast.cycles:
        assert cycle.result.succeeded, f"contrast cycle {cycle.index} failed although its first attempt succeeded"
        assert cycle.result.attempt_count == 1
        assert cycle.contributed_kwh > 0.0, (
            f"contrast cycle {cycle.index} delivered a reading that added nothing to the cumulative total"
        )
    assert contrast.final_total_kwh > contrast.initial_total_kwh, (
        "a run of successful cycles left the cumulative total where it was: this harness cannot tell a"
        " delivered reading from a withheld one"
    )


# --------------------------------------------------------------------------------------------------
# Property 7: one device's fault, and the devices either side of it
# --------------------------------------------------------------------------------------------------
#
# Requirements 3.6, 3.7, and 4.2 together make a device a bulkhead: one entry's plugin may fail however it
# likes, and what the other entries do must be indistinguishable from a run in which the faulty entry had
# never been configured. So that is what this property asserts, literally. Every example is driven twice --
# once with the faulty entries present, once with them removed -- and the healthy entries' reads, cycles,
# reports, and cumulative totals are required to be *equal* across the two, not merely similar.
#
# Four ways to misbehave are generated, drawn per faulty device:
#
# * **a classified failure** -- the plugin returns a :class:`ReadFailure`, which is the contract working;
# * **a raised exception** -- one of seven types, six built in and one the plugin declares itself and the
#   core has never heard of. Requirement 3.7 makes the scheduler convert it into a plugin-classified read
#   failure, and the property checks the conversion *names the exception that caused it*, so arriving at
#   some plugin failure by another route does not satisfy it;
# * **a garbage return** -- neither a reading nor a failure, so the ``read_power`` annotation is a promise
#   this plugin broke rather than a guarantee the core may lean on;
# * **a hang** -- a read that never returns and never raises, modelled by parking that device's task past
#   every deadline the run can reach. No request deadline is imposed at all, which is the worst case on
#   purpose: Requirement 5.4's timeout is what bounds a hung read in production, and switching it off here
#   means the isolation being observed is the scheduler's per-device task structure rather than its timeout.
#
# ``asyncio.TaskGroup`` is why the raising case needs a property rather than a unit test. A child task that
# raises causes the group to cancel its siblings, so an exception the executor let through does not degrade
# one device -- it ends every device's loop at once, and the process with it. The interesting claim is
# therefore about *where* the exception is caught, and it is pinned down from both sides:
#
# * the exception group :meth:`PollScheduler.run` raises is required to carry the harness's own end-of-run
#   sentinel and nothing else, so an escaping plugin exception fails the property by its mere presence;
# * every example is run a third time with the same exception raised from *outside* the executor -- by the
#   runner that calls it -- and that run is required to cost the healthy devices their cycles. So this
#   harness is known to be able to tell an escape from a containment, rather than assumed to be.

#: How many devices one fault run polls, healthy and faulty together. The design states Property 7 over 1 to
#: 50 entries; six keeps an example small while leaving room for all four fault kinds beside a healthy
#: witness, and nothing in the reasoning below is sensitive to the count.
MAX_FAULT_DEVICES: Final = MAX_DEVICES

#: How many cycles the healthy devices are driven for. Two is the floor -- "keeps being polled" needs a
#: second cycle to observe -- and three the ceiling, for the reason Property 18's ceiling exists: the gap
#: from the seeded baseline to a reading stays well inside Requirement 7.5's four-interval threshold, so no
#: assertion here is decided by where that boundary falls.
MIN_FAULT_CYCLES: Final = 2
MAX_FAULT_CYCLES: Final = 3

#: How long a hung read parks its device, in seconds. Beyond every deadline a run can reach -- the longest
#: is three 3600 second intervals from a start reading of at most a hundred days -- so the read never
#: returns, which is what distinguishes a hang from a slow read.
HANG_S: Final = 1e9

#: The plugin identifier every device in a fault run shares. One implementation serving entries that differ
#: only in their connection parameters is Requirement 4.2; one *instance* per entry is Requirement 3.6.
FAULT_PLUGIN_ID: Final = "idrac"

#: What a garbage-returning plugin hands back: neither a reading nor a failure. ``None`` because that is the
#: shape a real plugin bug takes -- a code path that forgot its ``return``.
GARBAGE_RETURN: Final = None

#: The message a raised fault carries, and the message the negative control raises outside the executor.
RAISED_MESSAGE: Final = "the plugin blew up mid-read"
ESCAPED_MESSAGE: Final = "the plugin blew up outside the executor"

#: The message fragment the executor writes when a plugin let an exception escape (Requirement 3.7).
PLUGIN_RAISED_FRAGMENT: Final = "raised during a power reading"


class PluginBlewUpError(Exception):
    """An exception type a plugin declares for itself, which the metering core has never heard of.

    Generated alongside the six built-in types, because Requirement 3.7 is stated over *any* unhandled
    exception: an implementation that enumerated the exceptions it knew how to convert would pass over
    ``RuntimeError`` and ``OSError`` and still let this one through.
    """


class FaultKind(StrEnum):
    """How one device misbehaves for the whole of a run."""

    #: Every attempt returns a classified :class:`ReadFailure`: the plugin contract working as intended.
    CLASSIFIED = "classified"
    #: Every attempt raises, which Requirement 3.7 makes the scheduler convert rather than propagate.
    RAISING = "raising"
    #: Every attempt returns something that is neither a reading nor a failure.
    GARBAGE = "garbage"
    #: The first attempt never returns and never raises.
    HANGING = "hanging"


#: The exception types a raising plugin is drawn from. Six built-in ones and the plugin's own, because
#: "any exception" is the requirement and a conversion written against one type is the defect to catch.
#: :class:`asyncio.CancelledError` is deliberately absent: it is not a device failure but the process being
#: asked to stop, and the executor re-raises it on purpose.
FAULT_ERRORS: Final[tuple[type[Exception], ...]] = (
    RuntimeError,
    ValueError,
    KeyError,
    TypeError,
    OSError,
    ZeroDivisionError,
    PluginBlewUpError,
)

#: Fault mixes worth drawing on purpose. A single fault of each kind is the minimal case; a hang beside a
#: raise is where a harness that let the hung device stall the clock would be caught; all four kinds at once
#: is the case a free-form generator draws rarely; three raises is a whole subset failing the way that
#: cancels siblings.
FAULT_SHAPES: Final[tuple[tuple[FaultKind, ...], ...]] = (
    (FaultKind.CLASSIFIED,),
    (FaultKind.RAISING,),
    (FaultKind.GARBAGE,),
    (FaultKind.HANGING,),
    (FaultKind.RAISING, FaultKind.HANGING),
    (FaultKind.CLASSIFIED, FaultKind.RAISING, FaultKind.GARBAGE, FaultKind.HANGING),
    (FaultKind.RAISING, FaultKind.RAISING, FaultKind.RAISING),
)


# --------------------------------------------------------------------------------------------------
# the scenario: which devices misbehave, how, and what the healthy ones are owed
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeviceFault:
    """How one misbehaving device's plugin fails, for the whole of a run.

    All three fields are drawn for every fault, whatever its kind: ``classification`` is what a
    :attr:`FaultKind.CLASSIFIED` fault returns, and ``error`` is what a :attr:`FaultKind.RAISING` fault
    raises *and* what the negative control raises from outside the executor, so the control quantifies over
    the same seven types regardless of which kind the example drew.

    Attributes:
        kind: which of the four ways to misbehave this device takes.
        classification: the failure a classified fault returns from every attempt.
        error: the exception type a raising fault raises from every attempt.
    """

    kind: FaultKind
    classification: FailureClassification
    error: type[Exception]


@dataclass(frozen=True, slots=True)
class FaultEntry:
    """One device in a fault run: what it is called, whether it misbehaves, and how slowly it reads.

    Attributes:
        device_id: the Device_Entry identifier, carrying its fault kind so a failing example names the
            device that mattered rather than an index.
        fault: how this device misbehaves, or ``None`` for a healthy one.
        share: how long each of this device's attempts spends, as a share of the cycle's read budget.
        watts: the power value this device's successful reads report. Distinct per device, so a reading
            credited to the wrong entry is visible in the total it moved rather than invisible.
    """

    device_id: str
    fault: DeviceFault | None
    share: float
    watts: float

    @property
    def healthy(self) -> bool:
        """Whether this device's plugin behaves."""
        return self.fault is None

    @property
    def escape_error(self) -> type[Exception]:
        """The exception type the negative control raises from outside the executor for this entry."""
        return PluginBlewUpError if self.fault is None else self.fault.error


@dataclass(frozen=True, slots=True)
class FaultScenario:
    """One run: a set of devices of which an arbitrary subset misbehaves, polled on a shared schedule.

    Attributes:
        interval_s: the poll interval in whole seconds, shared by every device so that the healthy devices'
            schedule is the same schedule with the faulty entries present and absent.
        start_s: the clock reading at Poll_Scheduler start.
        cycles: how many cycles every *healthy* device is driven for. A faulty device's cycle count is not
            fixed by the harness -- how many cycles a failing device gets through is the implementation's
            business, and asserting a count here would be asserting the retry schedule again.
        entries: the devices, in the order the scheduler creates their tasks. At least one healthy and at
            least one faulty, in an arbitrary order.
    """

    interval_s: int
    start_s: float
    cycles: int
    entries: tuple[FaultEntry, ...]

    @property
    def device_count(self) -> int:
        """How many devices this run polls."""
        return len(self.entries)

    @property
    def healthy_entries(self) -> tuple[FaultEntry, ...]:
        """The devices whose plugins behave: the witnesses that a fault was isolated."""
        return tuple(entry for entry in self.entries if entry.healthy)

    @property
    def faulty_entries(self) -> tuple[FaultEntry, ...]:
        """The devices whose plugins misbehave."""
        return tuple(entry for entry in self.entries if not entry.healthy)

    @property
    def healthy_ids(self) -> frozenset[str]:
        """The identifiers of the healthy devices, which is what ends the run once all have their cycles."""
        return frozenset(entry.device_id for entry in self.healthy_entries)

    def entry_of(self, device_id: str) -> FaultEntry:
        """The entry with this identifier."""
        return next(entry for entry in self.entries if entry.device_id == device_id)

    @property
    def read_budget_s(self) -> float:
        """How long one cycle's reads may take in total without the cycle outrunning its poll interval."""
        return self.interval_s - TOTAL_RETRY_DELAY_S

    def duration_of(self, entry: FaultEntry) -> float:
        """How long each of ``entry``'s attempts spends, in seconds."""
        return entry.share * self.read_budget_s

    @property
    def timeout_s(self) -> float:
        """The request timeout every device is given, above every generated read.

        Derived from the largest share any device *could* have drawn rather than from the shares this
        example did draw, so the timeout is the same number with the faulty entries present and absent and
        the two runs' recorded reads can be compared field for field.
        """
        return max(ATTEMPT_SHARES) * self.read_budget_s + TIMEOUT_HEADROOM_S

    def scheduled_at(self, cycle: int) -> float:
        """The clock reading cycle ``cycle`` is due at: the start, plus one interval per cycle before it."""
        return self.start_s + cycle * self.interval_s

    def measured_at(self, cycle: int) -> datetime:
        """The measurement timestamp a reading from cycle ``cycle`` carries, one interval apart per cycle."""
        return MEASURED_AT + timedelta(seconds=cycle * self.interval_s)

    @property
    def baseline_at(self) -> datetime:
        """When the seeded baseline was measured: one interval before the first cycle, as a real one would be."""
        return MEASURED_AT - timedelta(seconds=self.interval_s)

    def without_faults(self) -> FaultScenario:
        """The same run with the misbehaving devices never configured: what isolation has to reproduce.

        The healthy entries keep their identifiers, their read shares, and their power values, so every
        observation of the two runs is comparable device by device.
        """
        return replace(self, entries=self.healthy_entries)


# --------------------------------------------------------------------------------------------------
# the fault-injecting plugin, and the reads it served
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FaultRead:
    """One power reading attempt that reached a plugin, on the virtual clock.

    Attributes:
        device_id: which device's plugin served it, so a read cannot be credited to the wrong entry.
        cycle: which cycle of that device this attempt belonged to, counting from zero.
        attempt: 1 for that cycle's initial attempt, 2 and 3 for its retries.
        outcome: how the read ended, or ``None`` for a healthy device's reading.
        started_at: the clock reading when the read began.
        ended_at: the clock reading when it returned or raised, or ``None`` for a read that never did --
            which is exactly what a hung plugin produces, and is why this field is recorded on entry and
            completed on the way out rather than written once at the end.
        timeout_s: the request timeout the executor passed in.
    """

    device_id: str
    cycle: int
    attempt: int
    outcome: FaultKind | None
    started_at: float
    ended_at: float | None
    timeout_s: float


class FaultInjectingReads:
    """The one plugin implementation every device in a fault run uses, one instance per device.

    That shape is Requirements 3.6 and 4.2 as the scheduler sees them: entries differing only in their
    parameters -- here, in a fault and a read duration -- served by a single implementation, with per-device
    state held per instance so nothing one device does can reach another. An instance knows its own entry
    and nothing about its siblings, so a read it serves for another device would be a scheduler defect, and
    the property checks that none was.

    Only ``read_power`` is implemented, which is all :meth:`PollCycleExecutor.run_cycle` calls.
    """

    def __init__(self, clock: ConcurrentVirtualClock, scenario: FaultScenario, entry: FaultEntry) -> None:
        self._clock = clock
        self._scenario = scenario
        self._entry = entry
        self._cycle = 0
        self._attempts = 0
        self.reads: list[FaultRead] = []

    def begin_cycle(self, cycle: int) -> None:
        """Switch to cycle ``cycle``, from its first attempt."""
        self._cycle = cycle
        self._attempts = 0

    async def read_power(self, timeout_s: float) -> ReadResult:
        """Spend this attempt's generated duration on the virtual clock, then misbehave as scripted."""
        fault = self._entry.fault
        attempt = self._attempts + 1
        self._attempts = attempt
        index = len(self.reads)
        self.reads.append(
            FaultRead(
                device_id=self._entry.device_id,
                cycle=self._cycle,
                attempt=attempt,
                outcome=None if fault is None else fault.kind,
                started_at=self._clock.now,
                ended_at=None,
                timeout_s=timeout_s,
            )
        )

        if fault is not None and fault.kind is FaultKind.HANGING:
            # Parked past every deadline this run can reach, so this read does not return and does not
            # raise. Its device is held here for the rest of the run, which is the point.
            await self._clock.sleep(HANG_S)
        else:
            await self._clock.sleep(self._scenario.duration_of(self._entry))

        # Completed before the outcome is produced, so a raised fault is recorded as a read that ran rather
        # than disappearing along with the exception.
        self.reads[index] = replace(self.reads[index], ended_at=self._clock.now)
        return self._outcome(fault)

    def _outcome(self, fault: DeviceFault | None) -> ReadResult:
        """What this read returns once it has spent its time, or the exception it raises instead."""
        if fault is None:
            return PowerReading(
                device_id=self._entry.device_id,
                watts=self._entry.watts,
                measured_at=self._scenario.measured_at(self._cycle),
            )
        if fault.kind is FaultKind.CLASSIFIED:
            return ReadFailure(
                device_id=self._entry.device_id,
                classification=fault.classification,
                detail=FAILURE_DETAIL,
            )
        if fault.kind is FaultKind.RAISING:
            raise fault.error(RAISED_MESSAGE)
        if fault.kind is FaultKind.GARBAGE:
            # The cast is the defect being injected: ``read_power`` promises a ``ReadResult`` and this
            # plugin does not keep the promise, which is why the executor re-checks what it was handed.
            return cast("ReadResult", GARBAGE_RETURN)
        # A hung read never reaches here. Reported as a timeout rather than pretended away, so a clock that
        # somehow released it fails the property's assertion that a hung read never ended, instead of
        # raising something inscrutable from inside the harness.
        return ReadFailure(
            device_id=self._entry.device_id,
            classification=FailureClassification.TIMEOUT,
            detail="a hung read was released",
        )


# --------------------------------------------------------------------------------------------------
# what one driven run produced
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ObservedFaultCycle:
    """One cycle that ran to completion: what it returned, what it reported, and what it accumulated.

    Attributes:
        device_id: which device this cycle polled.
        index: which cycle of that device it was, counting from zero.
        started_at: the clock reading when the cycle began.
        result: the :class:`CycleResult` the real executor returned.
        state_before: that device's accumulator state immediately before the cycle.
        state_after: the same immediately after it.
        reports: the downstream reports this cycle made for this device, in order.
    """

    device_id: str
    index: int
    started_at: float
    result: CycleResult
    state_before: DeviceEnergyState
    state_after: DeviceEnergyState
    reports: tuple[SinkCall, ...]

    @property
    def kinds(self) -> tuple[str, ...]:
        """Which downstream reports this cycle made, in order."""
        return tuple(call.kind for call in self.reports)

    @property
    def contributed_kwh(self) -> float:
        """What this cycle added to its device's cumulative energy total, in kilowatt-hours."""
        return self.state_after.total_kwh - self.state_before.total_kwh

    @property
    def signature(self) -> tuple[object, ...]:
        """Everything about this cycle a faultless run has to reproduce exactly.

        A projection rather than the whole object: these are the fields Requirements 3.6 and 4.2 make
        claims about, and comparing them names the one that moved instead of printing two cycle results.
        """
        reading = self.result.reading
        return (
            self.index,
            self.started_at,
            self.result.attempt_count,
            self.result.succeeded,
            None if reading is None else reading.watts,
            self.state_after.total_kwh,
            self.kinds,
        )


@dataclass(frozen=True, slots=True)
class FaultObservation:
    """Everything one driven run produced, for the property to assert over.

    Attributes:
        cycles: every cycle that ran to completion, in the order they completed.
        reads: every attempt every plugin served, in per-device order.
        reports: every downstream report the publisher received.
        initial_totals_kwh: each device's cumulative total after seeding, before the first cycle.
        final_totals_kwh: each device's cumulative total after the run.
        plugins: the plugin instance each device was given, so "one instance per entry, serving only that
            entry" is checked against the instances themselves.
        escaped: every non-group exception that reached the scheduler's ``TaskGroup``. The harness's own
            end-of-run sentinel belongs here; anything else means a device's fault ended the process.
    """

    cycles: tuple[ObservedFaultCycle, ...]
    reads: tuple[FaultRead, ...]
    reports: tuple[SinkCall, ...]
    initial_totals_kwh: Mapping[str, float]
    final_totals_kwh: Mapping[str, float]
    plugins: Mapping[str, FaultInjectingReads]
    escaped: tuple[BaseException, ...]

    def cycles_of(self, device_id: str) -> tuple[ObservedFaultCycle, ...]:
        """The completed cycles of one device, in order."""
        return tuple(cycle for cycle in self.cycles if cycle.device_id == device_id)

    def reads_of(self, device_id: str) -> tuple[FaultRead, ...]:
        """The attempts one device's plugin served, in order."""
        return tuple(read for read in self.reads if read.device_id == device_id)

    def reports_of(self, device_id: str) -> tuple[SinkCall, ...]:
        """The downstream reports made about one device, in order."""
        return tuple(call for call in self.reports if call.device_id == device_id)


class FaultInjectedCycles:
    """Runs every device's cycle through the real executor, and records what each one did to whom.

    A wrapper, not a substitute, for the same two reasons Property 18's is: it brackets each cycle so one
    cycle's reports and one device's cumulative total are observable, and it ends the run by raising
    :class:`_EnoughCycles` once every *healthy* device has had its cycles -- raised out here, where the
    executor cannot catch it and classify it as a plugin failure (Requirement 3.7).

    It is also where the negative control lives. Given an ``escape``, the named device's cycle raises the
    given exception instead of running: the same fault, one boundary further out, so the run shows what
    happens when a plugin exception is *not* contained.
    """

    def __init__(
        self,
        executor: PollCycleExecutor,
        plugins: Mapping[str, FaultInjectingReads],
        accumulator: EnergyAccumulator,
        publisher: RecordingPublisher,
        clock: ConcurrentVirtualClock,
        *,
        wanted: int,
        healthy_ids: frozenset[str],
        escape: tuple[str, type[Exception]] | None = None,
    ) -> None:
        """Run cycles through ``executor`` until every device in ``healthy_ids`` has had ``wanted`` of them."""
        self._executor = executor
        self._plugins = plugins
        self._accumulator = accumulator
        self._publisher = publisher
        self._clock = clock
        self._wanted = wanted
        self._healthy_ids = healthy_ids
        self._escape = escape
        self._counts: dict[str, int] = dict.fromkeys(plugins, 0)
        self.observed: list[ObservedFaultCycle] = []

    async def run_cycle(self, device: ResolvedDevice) -> CycleResult:
        """Run one real cycle for ``device`` and record it, ending the run once the healthy devices are done."""
        device_id = device.device_id
        if self._escape is not None and device_id == self._escape[0]:
            escaping_error = self._escape[1]
            raise escaping_error(ESCAPED_MESSAGE)

        index = self._counts[device_id]
        self._plugins[device_id].begin_cycle(index)
        reported = len(self._publisher.calls)
        state_before = self._accumulator.state_of(device_id)
        started_at = self._clock.now

        result = await self._executor.run_cycle(device)

        self._counts[device_id] = index + 1
        self.observed.append(
            ObservedFaultCycle(
                device_id=device_id,
                index=index,
                started_at=started_at,
                result=result,
                state_before=state_before,
                state_after=self._accumulator.state_of(device_id),
                # Filtered by device, not merely sliced: several devices report during one cycle of one of
                # them, so a slice of the shared list would credit a sibling's report to this cycle.
                reports=tuple(call for call in self._publisher.calls[reported:] if call.device_id == device_id),
            )
        )
        if all(self._counts[healthy_id] >= self._wanted for healthy_id in self._healthy_ids):
            raise _EnoughCycles
        return result


# --------------------------------------------------------------------------------------------------
# generators
# --------------------------------------------------------------------------------------------------


@st.composite
def fault_assignments(draw: st.DrawFn) -> tuple[DeviceFault | None, ...]:
    """One fault per misbehaving device and ``None`` per healthy one, in an arbitrary order.

    At least one healthy device, so "isolated" has a witness, and at least one faulty one, so every example
    observes a fault -- the empty subset is exactly the faultless run each example is already compared
    against. The order is permuted rather than healthy-first, so the faulty devices land at arbitrary
    positions in the order the scheduler creates its tasks.
    """
    healthy = draw(st.integers(min_value=1, max_value=MAX_FAULT_DEVICES - 1))
    kinds = draw(
        st.one_of(
            st.lists(st.sampled_from(list(FaultKind)), min_size=1, max_size=MAX_FAULT_DEVICES - 1),
            st.sampled_from(FAULT_SHAPES).map(list),
        )
    )
    assignment: list[DeviceFault | None] = [
        DeviceFault(
            kind=kind,
            classification=draw(st.sampled_from(list(FailureClassification))),
            error=draw(st.sampled_from(FAULT_ERRORS)),
        )
        for kind in kinds[: MAX_FAULT_DEVICES - healthy]
    ]
    assignment.extend([None] * healthy)
    return tuple(draw(st.permutations(assignment)))


def fault_cycle_counts() -> st.SearchStrategy[int]:
    """How many cycles the healthy devices are driven for: two or three."""
    return st.integers(min_value=MIN_FAULT_CYCLES, max_value=MAX_FAULT_CYCLES)


@st.composite
def fault_scenarios(draw: st.DrawFn) -> FaultScenario:
    """A whole run: an interval, a start reading, a cycle count, and a device per drawn fault assignment."""
    assignment = draw(fault_assignments())
    entries = tuple(
        FaultEntry(
            device_id=f"{DEVICE_ID_PREFIX}{index}_{'ok' if fault is None else fault.kind.value}",
            fault=fault,
            share=draw(st.sampled_from(ATTEMPT_SHARES)),
            watts=READING_WATTS + index,
        )
        for index, fault in enumerate(assignment)
    )
    return FaultScenario(
        interval_s=draw(retry_intervals()),
        start_s=draw(start_readings()),
        cycles=draw(fault_cycle_counts()),
        entries=entries,
    )


# --------------------------------------------------------------------------------------------------
# the driver
# --------------------------------------------------------------------------------------------------


def _leaf_exceptions(error: BaseException) -> tuple[BaseException, ...]:
    """Every non-group exception inside ``error``, flattening nested exception groups."""
    if isinstance(error, BaseExceptionGroup):
        return tuple(leaf for inner in error.exceptions for leaf in _leaf_exceptions(inner))
    return (error,)


async def _observe_faults(
    scenario: FaultScenario,
    logger: logging.Logger,
    *,
    escape: tuple[str, type[Exception]] | None = None,
) -> FaultObservation:
    """Drive ``scenario`` through the real loop, executor, sink, and accumulator, and return what happened.

    One participant on the clock per device, as Property 16 does: this is a multi-device run, and the clock
    that advances on whichever task slept last would let one device's schedule shove another's. A hung
    device stays *parked* rather than leaving, so it still counts as a participant and the devices either
    side of it keep making progress -- which is the behaviour under test, not an artefact of the fake.

    Every device is registered with the accumulator, seeded with a nonzero total and a live baseline, so
    "contributed nothing" is a value a wrongly credited reading would visibly move off of.
    """
    clock = ConcurrentVirtualClock(scenario.start_s, participants=scenario.device_count)

    accumulator = EnergyAccumulator()
    for entry in scenario.entries:
        accumulator.register(
            entry.device_id,
            scenario.interval_s,
            restored=DeviceEnergyState(total_kwh=SEED_TOTAL_KWH),
        )
        accumulator.record(
            PowerReading(device_id=entry.device_id, watts=BASELINE_WATTS, measured_at=scenario.baseline_at)
        )
    initial_totals_kwh = {entry.device_id: accumulator.total_kwh(entry.device_id) for entry in scenario.entries}

    publisher = RecordingPublisher()
    plugins = {entry.device_id: FaultInjectingReads(clock, scenario, entry) for entry in scenario.entries}
    devices: Sequence[ResolvedDevice] = [
        cast(
            "ResolvedDevice",
            _FakeDevice(
                entry.device_id,
                scenario.interval_s,
                timeout_s=scenario.timeout_s,
                plugin=plugins[entry.device_id],
                plugin_id=FAULT_PLUGIN_ID,
            ),
        )
        for entry in scenario.entries
    ]
    executor = PollCycleExecutor(
        sink=AccumulateThenPublish(accumulator, publisher),
        clock=clock,
        sleep=clock.sleep,
        timeout=_no_deadline,
        logger=logger,
    )
    runner = FaultInjectedCycles(
        executor,
        plugins,
        accumulator,
        publisher,
        clock,
        wanted=scenario.cycles,
        healthy_ids=scenario.healthy_ids,
        escape=escape,
    )
    scheduler = PollScheduler(devices, executor=runner, clock=clock, sleep=clock.sleep, logger=logger)

    # Collected rather than suppressed: which exceptions reached the task group *is* the claim of
    # Requirement 3.7, so the property asserts over them instead of the harness swallowing them.
    escaped: tuple[BaseException, ...] = ()
    try:
        await scheduler.run()
    except BaseExceptionGroup as group:
        escaped = _leaf_exceptions(group)

    return FaultObservation(
        cycles=tuple(runner.observed),
        reads=tuple(read for plugin in plugins.values() for read in plugin.reads),
        reports=tuple(publisher.calls),
        initial_totals_kwh=initial_totals_kwh,
        final_totals_kwh={entry.device_id: accumulator.total_kwh(entry.device_id) for entry in scenario.entries},
        plugins=plugins,
        escaped=escaped,
    )


def _escaped_names(observation: FaultObservation) -> list[str]:
    """The type names of everything that reached the task group, for a failure message to name."""
    return sorted({type(error).__name__ for error in observation.escaped})


# --------------------------------------------------------------------------------------------------
# the property
# --------------------------------------------------------------------------------------------------


# Feature: remote-energy-meter, Property 7: Device failures are isolated
# Validates: Requirements 3.6, 3.7, 4.2
@settings(max_examples=150, deadline=None)
@given(scenario=fault_scenarios())
def test_device_failures_are_isolated(scenario: FaultScenario) -> None:
    """Requirements 3.6, 3.7, and 4.2, over any mix of healthy and misbehaving devices.

    **Validates: Requirements 3.6, 3.7, 4.2**

    Three runs of the same harness per example: the generated mix; the same run with the faulty entries
    never configured, which is what the healthy entries are required to reproduce exactly; and the same
    exception raised one boundary further out, which is what shows this harness would notice an exception
    the executor let through.
    """
    with recording_logger() as (logger, records):
        observed = asyncio.run(_observe_faults(scenario, logger))
    with recording_logger() as (faultless_logger, faultless_records):
        faultless = asyncio.run(_observe_faults(scenario.without_faults(), faultless_logger))
    escaping = scenario.faulty_entries[0]
    with recording_logger() as (control_logger, _control_records):
        control = asyncio.run(
            _observe_faults(scenario, control_logger, escape=(escaping.device_id, escaping.escape_error))
        )

    # --- Requirement 3.7: the process is still running, because nothing but the sentinel got out ---
    # Asserted first, and it is the clause ``asyncio.TaskGroup`` makes load-bearing: a child task that
    # raises causes the group to cancel every sibling, so a plugin exception reaching here would have ended
    # every device's loop rather than one device's cycle. Anything beyond the harness's own end-of-run
    # sentinel fails, whatever it is.
    assert {type(error) for error in observed.escaped} == {_EnoughCycles}, (
        f"the run ended on {_escaped_names(observed)} rather than the harness sentinel alone:"
        " a device's fault reached the task group, which cancels every other device's loop"
    )
    assert {type(error) for error in faultless.escaped} == {_EnoughCycles}, (
        f"the faultless reference run ended on {_escaped_names(faultless)}"
    )

    # --- every fault really fired, so nothing below is vacuous ---
    assert {read.device_id for read in observed.reads} == {entry.device_id for entry in scenario.entries}, (
        "a configured device was never read at all"
    )
    for entry in scenario.faulty_entries:
        assert observed.reads_of(entry.device_id), f"{entry.device_id} was never read, so its fault never fired"

    # --- every raised exception was seen and recorded at the boundary, with its traceback ---
    # The count is exact: one record per read that raised. A conversion that quietly dropped the exception
    # would leave the classification below satisfied and this unsatisfied. Counted over the reads that
    # *completed*, because the run ends as soon as the healthy devices have had their cycles, which can
    # leave a faulty device parked mid-read -- and a read cancelled before it got there never raised.
    raised_reads = [read for read in observed.reads if read.outcome is FaultKind.RAISING and read.ended_at is not None]
    raised_records = [record for record in records if PLUGIN_RAISED_FRAGMENT in record.getMessage()]
    assert len(raised_records) == len(raised_reads), (
        f"{len(raised_reads)} reads raised and {len(raised_records)} were recorded at the scheduler boundary"
    )
    assert all(record.exc_info is not None for record in raised_records), (
        "a raising plugin was recorded without its traceback, which is what a defect needs to be fixed"
    )

    # --- no downstream report failed, and no scheduled time was dropped ---
    # Both are guards on the comparison that follows: a contained sink failure would leave a cycle looking
    # as though it reported nothing, and a skip would mean a generated cycle outran its interval and the
    # healthy devices' schedules were not the fixed ones the two runs are compared over.
    for label, run_records in (("the run", records), ("the faultless reference run", faultless_records)):
        contained = [record for record in run_records if SINK_FAILURE_FRAGMENT in record.getMessage()]
        assert contained == [], f"a downstream report failed during {label}: {len(contained)} contained"
        skips = [record for record in run_records if SKIP_MESSAGE_FRAGMENT in record.getMessage()]
        assert skips == [], f"{label} dropped a scheduled time: {len(skips)} skip(s)"

    # --- the reads really spent their time on the clock, and only the ones that could not finish did not ---
    for read in observed.reads:
        if read.ended_at is None:
            if read.outcome is FaultKind.HANGING:
                assert read.timeout_s < HANG_S, "a hung read parked inside its timeout, so it is a slow read"
                continue
            # The run ends the instant the healthy devices have had their cycles, which can leave a faulty
            # device parked mid-read. Only a faulty device's *last* read may be cut short that way: a
            # healthy device that lost a read here would have lost a cycle the comparison below rests on.
            assert not scenario.entry_of(read.device_id).healthy, (
                f"{read.device_id} is healthy and its cycle {read.cycle} read never finished"
            )
            assert read == observed.reads_of(read.device_id)[-1], (
                f"{read.device_id} cycle {read.cycle} attempt {read.attempt} never finished"
                " and was not the last read of that device"
            )
            continue
        assert read.outcome is not FaultKind.HANGING, (
            f"{read.device_id} was scripted to hang and its read ended at {read.ended_at:g}"
        )
        duration_s = scenario.duration_of(scenario.entry_of(read.device_id))
        assert read.ended_at == read.started_at + duration_s, (
            f"{read.device_id} cycle {read.cycle} attempt {read.attempt} spent"
            f" {read.ended_at - read.started_at:g} s, not its {duration_s:g} s read"
        )
        assert duration_s < read.timeout_s, (
            f"{read.device_id} read for {duration_s:g} s under a {read.timeout_s:g} s timeout"
        )

    # --- Requirements 3.6 and 4.2: one implementation, one instance per entry, each serving only its own ---
    assert {type(plugin) for plugin in observed.plugins.values()} == {FaultInjectingReads}, (
        "the devices were not all served by one plugin implementation"
    )
    assert len({id(plugin) for plugin in observed.plugins.values()}) == scenario.device_count, (
        "two devices shared a plugin instance, so per-device connection state is shared"
    )
    for device_id, plugin in observed.plugins.items():
        served = {read.device_id for read in plugin.reads}
        assert served <= {device_id}, f"the plugin instance of {device_id} was asked to read {served - {device_id}}"

    # --- Requirement 3.7: each faulty device is failed for its cycle, and contributes nothing ---
    for entry in scenario.faulty_entries:
        fault = entry.fault
        assert fault is not None
        cycles = observed.cycles_of(entry.device_id)

        if fault.kind is FaultKind.HANGING:
            # A hung read is never completed, so this device has no cycle at all: nothing to report, and
            # nothing to accumulate. What matters is that it is *this* device that stalled and no other.
            assert cycles == (), f"{entry.device_id} completed {len(cycles)} cycles although its read hung"
            assert len(observed.reads_of(entry.device_id)) == 1, "a hung device was read a second time"
            assert observed.reports_of(entry.device_id) == (), "a hung device was reported downstream"
        else:
            assert cycles, f"{entry.device_id} was read but no cycle of it completed"

        expected = fault.classification if fault.kind is FaultKind.CLASSIFIED else FailureClassification.PLUGIN
        for cycle in cycles:
            assert not cycle.result.succeeded, f"{entry.device_id} cycle {cycle.index} obtained a reading"
            assert cycle.kinds == (UNAVAILABLE_CALL,), (
                f"{entry.device_id} cycle {cycle.index} failed on {cycle.result.classifications}"
                f" and reported {cycle.kinds}"
            )
            assert cycle.contributed_kwh == 0.0, f"{entry.device_id} cycle {cycle.index} accumulated energy"
            for attempt in cycle.result.attempts:
                failure = attempt.failure
                assert failure is not None
                assert failure.classification is expected, (
                    f"{entry.device_id} attempt {attempt.index} of a {fault.kind} fault was classified"
                    f" {failure.classification}, expected {expected}"
                )
                assert failure.device_id == entry.device_id
                if fault.kind is FaultKind.RAISING:
                    # The conversion names the exception that caused it, so arriving at a plugin failure by
                    # some other route does not satisfy Requirement 3.7.
                    assert failure.detail.startswith(f"{fault.error.__name__}:"), (
                        f"a raised {fault.error.__name__} was converted to a failure detailed {failure.detail!r}"
                    )
                elif fault.kind is FaultKind.GARBAGE:
                    assert type(GARBAGE_RETURN).__name__ in failure.detail, (
                        f"a garbage return was converted to a failure detailed {failure.detail!r}"
                    )

        assert observed.final_totals_kwh[entry.device_id] == observed.initial_totals_kwh[entry.device_id], (
            f"{entry.device_id} failed throughout and moved its total from"
            f" {observed.initial_totals_kwh[entry.device_id]:g} to {observed.final_totals_kwh[entry.device_id]:g} kWh"
        )
        assert [call.kind for call in observed.reports_of(entry.device_id)] == [UNAVAILABLE_CALL] * len(cycles)

    # --- Requirements 3.6 and 4.2: every healthy device ran as though the faults had never been there ---
    for entry in scenario.healthy_entries:
        cycles = observed.cycles_of(entry.device_id)
        assert len(cycles) == scenario.cycles, (
            f"{entry.device_id} completed {len(cycles)} of {scenario.cycles} cycles beside"
            f" {len(scenario.faulty_entries)} faulty device(s)"
        )
        for cycle in cycles:
            assert cycle.result.succeeded, f"{entry.device_id} cycle {cycle.index} failed although it was healthy"
            assert cycle.result.attempt_count == 1, (
                f"{entry.device_id} cycle {cycle.index} took {cycle.result.attempt_count} attempts to read a"
                " device that answers first time"
            )
            assert cycle.kinds == (READING_CALL,), f"{entry.device_id} cycle {cycle.index} reported {cycle.kinds}"
            assert cycle.reports[0].watts == entry.watts, (
                f"{entry.device_id} published {cycle.reports[0].watts} W, not its own {entry.watts} W"
            )
            assert cycle.contributed_kwh > 0.0, (
                f"{entry.device_id} cycle {cycle.index} delivered a reading that added nothing to its total"
            )
            drift_s = cycle.started_at - scenario.scheduled_at(cycle.index)
            assert abs(drift_s) <= VIRTUAL_CLOCK_SLACK_S, (
                f"{entry.device_id} cycle {cycle.index} began {drift_s:g} s off its scheduled time"
            )

        # And the comparison the requirement really makes: the same reads, at the same instants, for the
        # same attempts, producing the same cumulative total as a run without the faulty entries in it.
        assert observed.reads_of(entry.device_id) == faultless.reads_of(entry.device_id), (
            f"{entry.device_id} was read differently beside a fault than it was without one"
        )
        assert [cycle.signature for cycle in cycles] == [
            cycle.signature for cycle in faultless.cycles_of(entry.device_id)
        ], f"{entry.device_id} polled differently beside a fault than it did without one"
        assert observed.final_totals_kwh[entry.device_id] == faultless.final_totals_kwh[entry.device_id], (
            f"{entry.device_id} ended at {observed.final_totals_kwh[entry.device_id]:g} kWh beside a fault and"
            f" {faultless.final_totals_kwh[entry.device_id]:g} kWh without one"
        )
        assert observed.final_totals_kwh[entry.device_id] > observed.initial_totals_kwh[entry.device_id], (
            f"{entry.device_id} was polled throughout and accumulated nothing"
        )

    # --- the escape has teeth: the same exception, uncontained, does cost the healthy devices their run ---
    # Same scheduler, same clock, same devices; only where the exception is raised differs. Without this,
    # the assertion that nothing but the sentinel escaped would be satisfied by a harness that could not
    # observe an escape at all.
    assert escaping.escape_error in {type(error) for error in control.escaped}, (
        f"a {escaping.escape_error.__name__} raised outside the executor did not reach the task group:"
        f" {_escaped_names(control)}"
    )
    for entry in scenario.healthy_entries:
        completed = len(control.cycles_of(entry.device_id))
        assert completed < scenario.cycles, (
            f"{entry.device_id} completed all {scenario.cycles} cycles although a sibling's exception reached"
            " the task group: this harness cannot tell an escaping plugin exception from a contained one"
        )
