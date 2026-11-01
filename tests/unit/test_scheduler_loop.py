"""Unit tests for the per-device poll loop and concurrency control (task 11.3).

Covers the fixed behaviours: one task per device, the first request going out immediately, the fixed
schedule increment, and deadlines that come due during an overrunning cycle being dropped with a
record rather than run back to back. Drift across arbitrary timings is Property 13 (task 11.4).

Everything runs on an injected clock and sleep, so no test waits a real interval. An overrunning
cycle is simulated by advancing the virtual clock inside the cycle, which is what a slow read does.

One deliberate gap: nothing here asserts that two devices with *different* intervals are polled at
different rates. This clock advances by whichever task last slept, so with two tasks the timelines
interfere and such an assertion would measure the fake rather than the scheduler. Doing it honestly
needs an event-driven virtual scheduler that advances to the earliest pending deadline. Per-device
intervals are covered structurally by one task per device, and numerically by interval resolution
(Property 15, task 11.5) and schedule drift (Property 13, task 11.4).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

import pytest

from remote_energy_meter.scheduler import PollScheduler

if TYPE_CHECKING:
    from remote_energy_meter.config.runtime import ResolvedDevice

DEVICE_ID = "poweredge_a"
OTHER_DEVICE_ID = "poweredge_b"
INTERVAL_S = 30.0
START = 1000.0


class VirtualClock:
    """A monotonic clock that advances only when a sleep is awaited, or when a cycle holds it."""

    def __init__(self) -> None:
        self.now = START
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay_s: float) -> None:
        self.slept.append(delay_s)
        self.now += delay_s
        await asyncio.sleep(0)


class FakeDevice:
    """Stands in for ResolvedDevice: the loop reads device_id, interval_s, and lock."""

    def __init__(self, device_id: str = DEVICE_ID, interval_s: float = INTERVAL_S) -> None:
        self.device_id = device_id
        self.interval_s = interval_s
        self.lock = asyncio.Lock()


def as_device(device_id: str = DEVICE_ID, interval_s: float = INTERVAL_S) -> ResolvedDevice:
    return cast("ResolvedDevice", FakeDevice(device_id, interval_s))


class RecordingExecutor:
    """Records each cycle and how long the clock read when it began.

    ``hold_s`` advances the clock inside the cycle, which is how a read that outlasts its interval is
    simulated without any real waiting.
    """

    def __init__(self, clock: VirtualClock | None = None, hold_s: float = 0.0) -> None:
        self.cycles: list[str] = []
        self.started_at: list[float] = []
        self._clock = clock
        self._hold_s = hold_s

    async def run_cycle(self, device: Any) -> None:
        self.cycles.append(device.device_id)
        if self._clock is not None:
            self.started_at.append(self._clock.now)
            self._clock.now += self._hold_s
        await asyncio.sleep(0)


def scheduler_for(devices: list[ResolvedDevice], executor: RecordingExecutor, clock: VirtualClock) -> PollScheduler:
    return PollScheduler(devices, executor=cast("Any", executor), clock=clock, sleep=clock.sleep)


async def run_until(scheduler: PollScheduler, until: Any, turns: int = 600) -> None:
    """Run the scheduler until ``until()`` holds, then cancel it."""
    task = asyncio.create_task(scheduler.run())
    for _ in range(turns):
        if until():
            break
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_no_devices_returns_immediately() -> None:
    """Zero accepted entries is a fatal condition the caller has already handled."""
    scheduler = PollScheduler([], executor=cast("Any", RecordingExecutor()))
    await asyncio.wait_for(scheduler.run(), timeout=1.0)


async def test_the_first_request_goes_out_without_waiting_an_interval() -> None:
    """Requirement 5.1: the first deadline is now, not now plus one interval."""
    clock = VirtualClock()
    executor = RecordingExecutor(clock)
    scheduler = scheduler_for([as_device()], executor, clock)

    await run_until(scheduler, lambda: len(executor.cycles) >= 1)

    assert executor.started_at[0] == START, "the first cycle must not wait"


async def test_the_schedule_advances_by_a_fixed_increment() -> None:
    """Every deadline is one interval after the previous one, so error cannot accumulate."""
    clock = VirtualClock()
    executor = RecordingExecutor(clock)
    scheduler = scheduler_for([as_device()], executor, clock)

    await run_until(scheduler, lambda: len(executor.cycles) >= 4)

    assert executor.started_at[:4] == [START, START + 30.0, START + 60.0, START + 90.0]


async def test_one_task_per_device_polls_every_device() -> None:
    """Requirement 5.2: separate tasks are what make per-device intervals possible."""
    clock = VirtualClock()
    executor = RecordingExecutor(clock)
    scheduler = scheduler_for([as_device(DEVICE_ID), as_device(OTHER_DEVICE_ID)], executor, clock)

    await run_until(scheduler, lambda: set(executor.cycles) >= {DEVICE_ID, OTHER_DEVICE_ID})

    assert set(executor.cycles) == {DEVICE_ID, OTHER_DEVICE_ID}


async def test_deadlines_missed_during_an_overrun_are_skipped_and_recorded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Requirement 5.9: dropped rather than run back to back, and each one is recorded."""
    clock = VirtualClock()
    # A cycle that takes two and a half intervals leaves two deadlines behind.
    executor = RecordingExecutor(clock, hold_s=2.5 * INTERVAL_S)
    scheduler = scheduler_for([as_device()], executor, clock)

    with caplog.at_level("INFO"):
        await run_until(scheduler, lambda: len(executor.cycles) >= 2)

    skips = [r for r in caplog.records if "poll cycle skipped" in r.getMessage()]
    assert len(skips) >= 2, "both missed deadlines are recorded"
    assert getattr(skips[0], "device_id", None) == DEVICE_ID
    assert isinstance(getattr(skips[0], "skipped_at", None), float)


async def test_an_overrun_does_not_run_missed_cycles_back_to_back() -> None:
    """The point of skipping: cycles stay one interval apart, not queued up."""
    clock = VirtualClock()
    executor = RecordingExecutor(clock, hold_s=2.5 * INTERVAL_S)
    scheduler = scheduler_for([as_device()], executor, clock)

    await run_until(scheduler, lambda: len(executor.cycles) >= 3)

    gaps = [b - a for a, b in zip(executor.started_at, executor.started_at[1:], strict=False)]
    assert all(gap >= 2.5 * INTERVAL_S for gap in gaps), f"cycles ran back to back: {gaps}"


async def test_polling_resumes_on_schedule_after_an_overrun() -> None:
    """The skip is per-deadline, not a latch that stops the device."""
    clock = VirtualClock()
    executor = RecordingExecutor(clock, hold_s=2.5 * INTERVAL_S)
    scheduler = scheduler_for([as_device()], executor, clock)

    await run_until(scheduler, lambda: len(executor.cycles) >= 4)

    assert len(executor.cycles) >= 4, "the device keeps polling after an overrun"


async def test_a_lock_held_elsewhere_skips_rather_than_waits(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Requirement 5.3: at most one read in flight per device, and the loop does not queue behind it."""
    clock = VirtualClock()
    executor = RecordingExecutor(clock)
    device = as_device()
    scheduler = scheduler_for([device], executor, clock)

    await device.lock.acquire()
    with caplog.at_level("INFO"):
        await run_until(scheduler, lambda: len(clock.slept) >= 2)
    device.lock.release()

    assert executor.cycles == [], "no cycle may run while the lock is held elsewhere"
    assert [r for r in caplog.records if "poll cycle skipped" in r.getMessage()]
