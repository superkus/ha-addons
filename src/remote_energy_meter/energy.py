"""Energy integration: instantaneous watts in, cumulative kilowatt-hours out.

Home Assistant's Energy Dashboard wants a monotonic ``total_increasing`` meter, but a plugin reports instantaneous
power. This module bridges the two by integrating successive
:class:`~remote_energy_meter.plugins.interface.PowerReading` values of one device into a cumulative total
(Requirement 7.1).

Everything here is a pure function over plain data: no clock, no file, no network, no logging. The accumulator holds
state, but every transition of that state is computed by :func:`accumulate`, which takes the current state and a reading
and returns the next state. That is what makes the correctness properties of this module testable by generating
adversarial reading sequences rather than by orchestrating a poll loop, and it is why persistence lives in
``state_store`` instead of here.

Four decisions carry weight.

**Trapezoidal, not rectangular.** The contribution of an interval is the *mean* of the two power values times the
elapsed hours (Requirement 7.1), not the last value times the interval. Power ramps between polls; at a 30 second
interval the trapezoid halves the systematic error of a rectangular rule at no cost.

**The total never decreases.** A computed contribution below zero contributes zero (Requirement 7.2). This is not
defensive dressing: a ``total_increasing`` sensor that goes backwards makes Home Assistant treat the drop as a meter
reset and corrupts long-term statistics in a way that is tedious to repair.

**A reading that does not advance the clock is discarded outright.** Requirement 7.8 keeps both the total and the
baseline unchanged when a timestamp is earlier than or equal to the baseline's. Re-baselining onto an older reading
would let a device with a stepping clock walk its baseline backwards and then integrate the same span twice.

**A long gap re-baselines instead of guessing.** When more than ``4x`` the device's poll interval has elapsed
(Requirement 7.5), the interval is discarded, but the new reading still becomes the baseline. After an outage there is
no basis to claim what was drawn during the gap, yet the current power is known, so integration resumes from now rather
than stalling until two fresh readings happen to land close together.

A reading whose power value is unusable -- not a finite number, or negative -- is discarded the same way, reported as
:data:`AccumulationOutcome.DISCARDED_UNUSABLE_VALUE` rather than raised. The plugin already applies its own plausibility
range (Requirement 4.4) and the publisher applies a wider one (Requirement 6.14), so a value arriving here outside both
means a plugin is misbehaving; degrading one device's accumulation for one cycle is the proportionate response, and it
keeps a third-party plugin from taking down the poll loop through this path.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final

from remote_energy_meter.persistence import DeviceEnergyState
from remote_energy_meter.plugins.interface import PowerReading

__all__ = [
    "GAP_THRESHOLD_INTERVALS",
    "KWH_DECIMAL_PLACES",
    "AccumulationOutcome",
    "AccumulationResult",
    "EnergyAccumulator",
    "accumulate",
    "baseline_reading",
    "elapsed_hours",
    "exceeds_gap_threshold",
    "gap_threshold_s",
    "interval_kwh",
]

# --------------------------------------------------------------------------------------------------
# Fixed quantities
# --------------------------------------------------------------------------------------------------

#: Requirement 7.1: cumulative totals and interval contributions are held to 6 decimal places of a kilowatt-hour.
#: At the 0-3600 s interval range that is finer than any plausible reading resolution, and it keeps the persisted
#: value short enough to compare exactly across a persist-restore round trip.
KWH_DECIMAL_PLACES: Final = 6

#: Requirement 7.5: how many poll intervals may elapse between two readings before the interval between them is
#: discarded. Shared with the publisher's retained-reading freshness check, so a reading too old to publish is also
#: too old to integrate.
GAP_THRESHOLD_INTERVALS: Final = 4

_SECONDS_PER_HOUR: Final = 3600.0
_WATTS_PER_KILOWATT: Final = 1000.0
_MICROSECONDS_PER_SECOND: Final = 1_000_000


class AccumulationOutcome(StrEnum):
    """What one reading did to a device's cumulative total.

    Returned rather than logged, so the caller writes the log record with its own device identifier and severity and
    this module stays free of a logger dependency. The set is closed and total: every reading produces exactly one
    member, which is what lets a caller ``match`` over it under ``mypy --strict``.
    """

    BASELINED = "baselined"
    """The first reading for this device since start. Retained as the accumulation baseline, total unchanged
    (Requirement 7.7)."""

    ACCUMULATED = "accumulated"
    """The interval since the baseline contributed to the total, and the reading became the new baseline
    (Requirement 7.1)."""

    REBASELINED_AFTER_GAP = "rebaselined_after_gap"
    """More than :data:`GAP_THRESHOLD_INTERVALS` poll intervals had elapsed: the interval was discarded and the reading
    became the new baseline (Requirement 7.5)."""

    DISCARDED_NOT_ADVANCING = "discarded_not_advancing"
    """The timestamp was not later than the baseline's. Total and baseline both unchanged (Requirement 7.8)."""

    DISCARDED_UNUSABLE_VALUE = "discarded_unusable_value"
    """The power value was not a finite, non-negative number, or the timestamp was not timezone-aware. Total and
    baseline both unchanged."""


@dataclass(frozen=True, slots=True)
class AccumulationResult:
    """The next state of one device, plus what happened to get there.

    Attributes:
        state: The device state after the reading. The same object as the input state when nothing changed, so an
            unchanged total is observable by identity as well as by value.
        outcome: Which of the five transitions applied.
        added_kwh: What the reading contributed, in kilowatt-hours. Zero for every outcome other than
            :data:`AccumulationOutcome.ACCUMULATED`, and never negative (Requirement 7.2).
        detail: Human-readable context for the discard outcomes, for the caller's log record. ``None`` otherwise.
    """

    state: DeviceEnergyState
    outcome: AccumulationOutcome
    added_kwh: float = 0.0
    detail: str | None = None

    @property
    def accumulated(self) -> bool:
        """Whether the reading contributed to the cumulative total."""
        return self.outcome is AccumulationOutcome.ACCUMULATED

    @property
    def discarded(self) -> bool:
        """Whether the reading left both the total and the baseline unchanged."""
        return self.outcome in {
            AccumulationOutcome.DISCARDED_NOT_ADVANCING,
            AccumulationOutcome.DISCARDED_UNUSABLE_VALUE,
        }


# --------------------------------------------------------------------------------------------------
# Integration math (Requirements 7.1, 7.2)
# --------------------------------------------------------------------------------------------------


def elapsed_hours(earlier: datetime, later: datetime) -> float:
    """Return the elapsed time from ``earlier`` to ``later`` in hours.

    Both timestamps must be timezone-aware; ``PowerReading.measured_at`` is contractually UTC-aware, and differencing a
    naive timestamp against an aware one raises rather than silently assuming an offset. The result is negative when
    ``later`` precedes ``earlier``, which the callers handle before it can reach the total.
    """
    if earlier.tzinfo is None or later.tzinfo is None:
        raise ValueError("elapsed_hours requires timezone-aware timestamps")
    return (later - earlier).total_seconds() / _SECONDS_PER_HOUR


def interval_kwh(prev: PowerReading, curr: PowerReading) -> float:
    """Return the energy drawn between two consecutive readings, in kilowatt-hours.

    The mean of the two power values in watts, multiplied by the elapsed hours, divided by 1000, rounded to
    :data:`KWH_DECIMAL_PLACES` decimal places (Requirement 7.1), and clamped at zero so it can never pull a cumulative
    total backwards (Requirement 7.2). The clamp absorbs both a negative power value and a reading pair in reverse
    order; :func:`accumulate` rejects the latter before it gets here, but this function is also used directly and must
    hold on its own.

    Rounding is applied to the contribution rather than only to the running total, so the same pair of readings
    contributes the same amount regardless of how large the total already is.
    """
    hours = elapsed_hours(prev.measured_at, curr.measured_at)
    mean_watts = (prev.watts + curr.watts) / 2.0
    kwh = mean_watts * hours / _WATTS_PER_KILOWATT
    if not math.isfinite(kwh):
        return 0.0
    return round(max(0.0, kwh), KWH_DECIMAL_PLACES)


def gap_threshold_s(interval_s: float) -> float:
    """Return the longest gap between two readings that still contributes energy, in seconds (Requirement 7.5)."""
    _check_interval(interval_s)
    return GAP_THRESHOLD_INTERVALS * float(interval_s)


def exceeds_gap_threshold(earlier: datetime, later: datetime, interval_s: float) -> bool:
    """Whether the gap between two readings is too long to integrate across (Requirement 7.5).

    The comparison is strict: a gap of exactly ``4x`` the poll interval still contributes, because Requirement 7.5
    discards only what *exceeds* the threshold.

    The gap is taken in whole microseconds straight off the ``timedelta``, which is what makes that boundary exact.
    Routing it through :func:`elapsed_hours` and multiplying back by 3600 was not a round trip: ``228 / 3600 * 3600``
    is ``228.00000000000003``, so a gap of exactly 4x a 57 s interval compared as greater than its own threshold and
    the boundary interval was discarded. Only the threshold stays a float here, in the units
    :func:`gap_threshold_s` reports it in.
    """
    if earlier.tzinfo is None or later.tzinfo is None:
        raise ValueError("exceeds_gap_threshold requires timezone-aware timestamps")
    gap_us = (later - earlier) // timedelta(microseconds=1)
    return gap_us > gap_threshold_s(interval_s) * _MICROSECONDS_PER_SECOND


# --------------------------------------------------------------------------------------------------
# State transition (Requirements 7.1, 7.2, 7.5, 7.7, 7.8)
# --------------------------------------------------------------------------------------------------


def baseline_reading(state: DeviceEnergyState, device_id: str) -> PowerReading | None:
    """Return the retained accumulation baseline as a reading, or ``None`` when there is none.

    ``DeviceEnergyState`` stores the baseline as a watt value and a timestamp because that is the persisted shape;
    the integration math works on reading pairs. This is the one adapter between them.
    """
    if state.baseline_watts is None or state.baseline_at is None:
        return None
    return PowerReading(device_id=device_id, watts=state.baseline_watts, measured_at=state.baseline_at)


def accumulate(state: DeviceEnergyState, reading: PowerReading, interval_s: float) -> AccumulationResult:
    """Apply one reading to one device's state and return the next state.

    The whole per-device accumulation rule, as one total function over the five cases:

    ========================================  =========================  ==========================  ===============
    Condition                                 Total                      Baseline                    Requirement
    ========================================  =========================  ==========================  ===============
    power value or timestamp unusable         unchanged                  unchanged                   --
    no baseline retained                      unchanged                  becomes this reading        7.7
    timestamp at or before the baseline's     unchanged                  unchanged                   7.8
    gap exceeds ``4x`` the poll interval      unchanged                  becomes this reading        7.5
    otherwise                                 increased by the interval  becomes this reading        7.1, 7.2
    ========================================  =========================  ==========================  ===============

    ``interval_s`` is the *resolved* poll interval of this device (``ResolvedDevice.interval_s``), not the global one,
    so a device polled every 600 s tolerates a proportionally longer gap than one polled every 5 s.

    Never raises for any :class:`PowerReading`: an unusable reading is reported as a discard outcome instead, so one
    misbehaving plugin cannot interrupt the poll loop through this path. A non-positive ``interval_s`` *does* raise,
    because that is a wiring error in the caller rather than data from a device.
    """
    _check_interval(interval_s)

    unusable = _unusable_reason(reading)
    if unusable is not None:
        return AccumulationResult(state=state, outcome=AccumulationOutcome.DISCARDED_UNUSABLE_VALUE, detail=unusable)

    baseline = baseline_reading(state, reading.device_id)

    if baseline is None:
        return AccumulationResult(state=_rebaselined(state, reading), outcome=AccumulationOutcome.BASELINED)

    if reading.measured_at <= baseline.measured_at:
        return AccumulationResult(
            state=state,
            outcome=AccumulationOutcome.DISCARDED_NOT_ADVANCING,
            detail=(
                f"timestamp {reading.measured_at.isoformat()} does not advance the "
                f"baseline at {baseline.measured_at.isoformat()}"
            ),
        )

    if exceeds_gap_threshold(baseline.measured_at, reading.measured_at, interval_s):
        gap_s = elapsed_hours(baseline.measured_at, reading.measured_at) * _SECONDS_PER_HOUR
        return AccumulationResult(
            state=_rebaselined(state, reading),
            outcome=AccumulationOutcome.REBASELINED_AFTER_GAP,
            detail=f"gap of {gap_s:.3f} s exceeds {gap_threshold_s(interval_s):.3f} s",
        )

    contribution = interval_kwh(baseline, reading)
    return AccumulationResult(
        state=_rebaselined(state, reading, total_kwh=round(state.total_kwh + contribution, KWH_DECIMAL_PLACES)),
        outcome=AccumulationOutcome.ACCUMULATED,
        added_kwh=contribution,
    )


# --------------------------------------------------------------------------------------------------
# Per-device holder
# --------------------------------------------------------------------------------------------------


class EnergyAccumulator:
    """Cumulative energy totals for every metered device.

    One instance serves the whole process. Each device is registered once at startup with its resolved poll interval
    and its restored total, and every subsequent reading goes through :meth:`record`. Devices do not interact: a device
    that stops reporting, reports garbage, or steps its clock backwards affects only its own total.

    The persisted baseline is deliberately **not** restored as an active baseline. After a restart the time since that
    timestamp is unknown and usually exceeds the gap threshold anyway, so the first reading after start re-baselines
    and contributes nothing (Requirement 7.7). :meth:`register` therefore keeps the restored ``total_kwh`` and drops the
    restored baseline pair; ``state_store`` keeps the persisted timestamp for diagnostics.
    """

    __slots__ = ("_intervals", "_states")

    def __init__(self) -> None:
        self._states: dict[str, DeviceEnergyState] = {}
        self._intervals: dict[str, float] = {}

    # -- registration ------------------------------------------------------------------------------

    def register(self, device_id: str, interval_s: float, restored: DeviceEnergyState | None = None) -> None:
        """Register a device with its resolved poll interval and its restored total.

        ``restored`` is the persisted state for this device, or ``None`` for a device with no usable persisted total,
        which starts at 0 kWh (Requirements 7.4, 7.6). Its baseline pair is dropped, per Requirement 7.7.

        Registering an already registered identifier raises: the registry admits each accepted entry exactly once, so a
        second registration would silently reset a total that is still accumulating.
        """
        if device_id in self._states:
            raise ValueError(f"device {device_id!r} is already registered")
        _check_interval(interval_s)
        self._states[device_id] = DeviceEnergyState(total_kwh=restored.total_kwh if restored else 0.0)
        self._intervals[device_id] = float(interval_s)

    def register_all(
        self,
        intervals: Mapping[str, float],
        restored: Mapping[str, DeviceEnergyState] | None = None,
    ) -> None:
        """Register every device in ``intervals``, restoring totals from ``restored`` where present."""
        for device_id, interval_s in intervals.items():
            self.register(device_id, interval_s, (restored or {}).get(device_id))

    # -- accumulation ------------------------------------------------------------------------------

    def record(self, reading: PowerReading) -> AccumulationResult:
        """Apply one reading to its device and return what it did.

        The returned result is also stored, so :meth:`state_of` and :meth:`snapshot` reflect it immediately. Raises
        :class:`KeyError` for an unregistered device, because a reading for a device the accumulator never saw means
        the scheduler is polling something the registry did not admit.
        """
        state = self.state_of(reading.device_id)
        result = accumulate(state, reading, self._intervals[reading.device_id])
        self._states[reading.device_id] = result.state
        return result

    # -- inspection --------------------------------------------------------------------------------

    def state_of(self, device_id: str) -> DeviceEnergyState:
        """Return the current state of one registered device."""
        try:
            return self._states[device_id]
        except KeyError:
            raise KeyError(f"device {device_id!r} is not registered with the accumulator") from None

    def total_kwh(self, device_id: str) -> float:
        """Return the cumulative energy total of one registered device, in kilowatt-hours."""
        return self.state_of(device_id).total_kwh

    def interval_s(self, device_id: str) -> float:
        """Return the resolved poll interval of one registered device, in seconds."""
        self.state_of(device_id)
        return self._intervals[device_id]

    def snapshot(self) -> dict[str, DeviceEnergyState]:
        """Return every device's current state, for ``state_store`` to persist (Requirements 7.3, 7.9).

        A new mapping of immutable states, so a flush in progress cannot observe a half-updated view and a caller
        cannot mutate accumulator state through the returned object.
        """
        return dict(self._states)

    @property
    def device_ids(self) -> tuple[str, ...]:
        """The registered device identifiers, in registration order."""
        return tuple(self._states)

    def __contains__(self, device_id: object) -> bool:
        return device_id in self._states

    def __len__(self) -> int:
        return len(self._states)

    def __iter__(self) -> Iterator[str]:
        return iter(self._states)


# --------------------------------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------------------------------


def _check_interval(interval_s: float) -> None:
    """Raise unless ``interval_s`` is a positive, finite number of seconds.

    The configuration layer already bounds poll intervals to 5-3600 s (Requirement 2.2), so a bad value here is a
    wiring error in the caller. It raises rather than being classified because there is no per-device degradation that
    would make sense: without an interval there is no gap threshold at all.
    """
    if isinstance(interval_s, bool) or not isinstance(interval_s, int | float):
        raise TypeError(f"poll interval is not a number: {interval_s!r}")
    if not math.isfinite(interval_s) or interval_s <= 0:
        raise ValueError(f"poll interval must be a positive, finite number of seconds: {interval_s!r}")


def _unusable_reason(reading: PowerReading) -> str | None:
    """Return why ``reading`` cannot be integrated, or ``None`` when it can."""
    if isinstance(reading.watts, bool) or not isinstance(reading.watts, int | float):
        return f"power value is not a number: {reading.watts!r}"
    if not math.isfinite(reading.watts):
        return f"power value is not finite: {reading.watts!r}"
    if reading.watts < 0:
        return f"power value is negative: {reading.watts!r}"
    if reading.measured_at.tzinfo is None:
        return "measurement timestamp is not timezone-aware"
    return None


def _rebaselined(
    state: DeviceEnergyState,
    reading: PowerReading,
    total_kwh: float | None = None,
) -> DeviceEnergyState:
    """Return ``state`` with ``reading`` as the baseline, and optionally a new total."""
    return DeviceEnergyState(
        total_kwh=state.total_kwh if total_kwh is None else total_kwh,
        baseline_watts=float(reading.watts),
        baseline_at=reading.measured_at,
    )
