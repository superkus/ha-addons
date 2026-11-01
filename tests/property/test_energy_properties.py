"""Properties 27, 28, and 29: energy integration over generated reading sequences (tasks 7.3-7.5).

Three statements about ``energy.py``, each over an input space too large to enumerate:

* **Property 27** — a cumulative total is non-negative and never decreases, the first reading after start
  becomes the baseline without contributing, and a reading that does not advance the clock changes nothing
  (Requirements 7.2, 7.7, 7.8);
* **Property 28** — one interval's contribution is the trapezoidal closed form, and subdividing an interval
  at a linearly interpolated intermediate reading produces the same total (Requirement 7.1);
* **Property 29** — an interval contributes if and only if its gap is within ``4x`` the poll interval, and the
  later reading becomes the baseline either way (Requirement 7.5).

## Why the expectations are a model, not a restatement

Property 27 runs a five-case model of the accumulation rule alongside the accumulator and asserts the
transition the model predicts. That is what makes the generated sequences worth generating: an assertion that
only checked "the total did not go down" would pass against an accumulator that ignored every reading. The
model is written from the requirements table, not from the implementation's control flow, and it is the reason
the reading generator can afford to be hostile -- equal timestamps, backwards steps, gaps beyond the
threshold, zero and negative watts, non-finite watts, and the occasional naive timestamp all have a predicted
outcome rather than needing to be filtered out.

For the same reason Property 28 computes its expected contribution in exact rational arithmetic
(:class:`fractions.Fraction`) rather than repeating the module's float expression. Restating
``mean * hours / 1000`` in the test would assert only that Python is deterministic.

## Why the tolerances are what they are

Requirement 7.1 rounds every contribution to :data:`KWH_DECIMAL_PLACES` decimal places, so a contribution can
differ from the mathematically exact value by up to half of the last place, :data:`HALF_LAST_PLACE`. Additivity
under subdivision therefore holds only up to rounding: the whole interval carries one rounding and the two
halves carry one each, so the two sides can differ by up to three half-places. Both tolerances are named
constants rather than inline magic numbers, and :data:`FLOAT_SLACK` covers the binary floating point error in
the products themselves, which is around ``1e-11`` at the largest magnitude generated here (10 kW across the
4 hour gap threshold of a 3600 s interval).

Generators are local to this module rather than shared, so this file stands alone.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from typing import Final, NamedTuple

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from remote_energy_meter.config.models import MAX_POLL_INTERVAL_S, MIN_POLL_INTERVAL_S
from remote_energy_meter.energy import (
    GAP_THRESHOLD_INTERVALS,
    KWH_DECIMAL_PLACES,
    AccumulationOutcome,
    EnergyAccumulator,
    accumulate,
    baseline_reading,
    elapsed_hours,
    exceeds_gap_threshold,
    gap_threshold_s,
    interval_kwh,
)
from remote_energy_meter.persistence import DeviceEnergyState
from remote_energy_meter.plugins.interface import PowerReading

#: The one device every scenario meters. These properties are per-device statements; isolation between
#: devices is Property 7's subject.
DEVICE: Final = "poweredge_a"

#: The anchor every generated timestamp is expressed as an offset from.
T0: Final = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)

MICROSECONDS_PER_SECOND: Final = 1_000_000
SECONDS_PER_HOUR: Final = 3600
WATTS_PER_KILOWATT: Final = 1000

#: Requirement 4.4: the plausible range for one chassis, and so the range a plugin can hand the accumulator.
MAX_CHASSIS_WATTS: Final = 10_000.0

#: Half of the last decimal place Requirement 7.1 keeps: the most one rounded contribution can differ from the
#: exact value.
HALF_LAST_PLACE: Final = 0.5 * 10.0**-KWH_DECIMAL_PLACES

#: Slack for the binary floating point error in the products themselves, orders of magnitude below the rounding
#: term at every magnitude generated here.
FLOAT_SLACK: Final = 1e-9

#: One rounding: an interval contribution against its exact value.
ROUNDING_TOLERANCE: Final = HALF_LAST_PLACE + FLOAT_SLACK

#: Three roundings: the whole interval's contribution against the sum of the two halves' contributions, each of
#: which is rounded before it is added.
ADDITIVITY_TOLERANCE: Final = 3 * HALF_LAST_PLACE + FLOAT_SLACK


# --------------------------------------------------------------------------------------------------
# generators
# --------------------------------------------------------------------------------------------------

#: True one time in ten, and shrinks to False, so a rare shape is present in the sequence space without
#: dominating it.
_OCCASIONALLY: Final = st.sampled_from([False] * 9 + [True])


def poll_intervals() -> st.SearchStrategy[int]:
    """Resolved poll intervals, across the whole accepted range (Requirement 2.2)."""
    return st.integers(min_value=MIN_POLL_INTERVAL_S, max_value=MAX_POLL_INTERVAL_S)


def plausible_watts() -> st.SearchStrategy[float]:
    """Power values a plugin may report, including both ends of the accepted range."""
    return st.one_of(
        st.floats(min_value=0.0, max_value=MAX_CHASSIS_WATTS, allow_nan=False, allow_infinity=False),
        st.sampled_from([0.0, -0.0, 1e-9, 0.5, 214.0, MAX_CHASSIS_WATTS]),
    )


def unusable_watts() -> st.SearchStrategy[float]:
    """Power values the accumulator must refuse to integrate: negative, or not finite.

    Values adjacent to zero on the negative side are drawn explicitly, because that is where the clamp of
    Requirement 7.2 and the sign test of the unusable rule meet.
    """
    return st.sampled_from([-1e-9, -0.5, -214.0, -MAX_CHASSIS_WATTS, math.nan, math.inf, -math.inf])


@st.composite
def watt_values(draw: st.DrawFn) -> float:
    """Power values a plugin may hand the accumulator, usable one time in five.

    The shape is drawn as an index rather than through ``one_of`` for two reasons: ``one_of`` draws uniformly
    across its branches, and an unusable value short-circuits before the baseline is even consulted, so a
    uniform generator would spend a third of its budget outside every transition that carries arithmetic. An
    index also shrinks toward zero, so a failing example reduces to the plausible branch.
    """
    if draw(st.integers(min_value=0, max_value=4)) == 4:
        return draw(unusable_watts())
    return draw(plausible_watts())


def gap_threshold_us(interval_s: int) -> int:
    """The gap threshold of Requirement 7.5 in whole microseconds, as exact integer arithmetic.

    The threshold is the source of truth for what the tests expect, so it is computed in integers rather than
    by differencing timestamps: a float second count would make the assertions at the boundary itself depend on
    rounding rather than on the rule.
    """
    return GAP_THRESHOLD_INTERVALS * interval_s * MICROSECONDS_PER_SECOND


@st.composite
def timestamp_steps_us(draw: st.DrawFn, threshold_us: int) -> int:
    """One step of a generated timestamp walk, in microseconds.

    Four shapes, weighted so the branches that reach the integration arithmetic get most of the budget: advance
    within the gap threshold, advance beyond it, stand still, and go backwards. The last two are what drive
    Requirement 7.8, and one draw in eight each is plenty to establish it.
    """
    shape = draw(st.integers(min_value=0, max_value=7))
    if shape <= 2:
        return draw(st.integers(min_value=1, max_value=threshold_us))
    if shape == 3:
        # The boundary itself, which a uniform draw over a 4 hour span would never land on. A gap of exactly
        # 4x the interval still integrates, so this is the step that would expose an inclusive comparison.
        return draw(st.sampled_from([1, threshold_us - 1, threshold_us]))
    if shape <= 5:
        return draw(st.integers(min_value=threshold_us + 1, max_value=3 * threshold_us))
    if shape == 6:
        return 0
    return draw(st.integers(min_value=-threshold_us, max_value=-1))


class Scenario(NamedTuple):
    """One device's registration and the sequence of readings handed to it."""

    interval_s: int
    restored_total_kwh: float
    readings: tuple[PowerReading, ...]


@st.composite
def scenarios(draw: st.DrawFn) -> Scenario:
    """A restored total and a hostile reading sequence for one device.

    Timestamps are built as a cumulative walk whose steps are drawn from four shapes -- stand still, go
    backwards, advance within the gap threshold, and advance beyond it -- so a single sequence exercises
    baselining, accumulation, non-advancing discards, and gap re-baselining in an order the generator did not
    plan. Timestamps are occasionally naive, which ``PowerReading`` permits structurally and the accumulator
    must report as an unusable reading rather than raise on.
    """
    interval_s = draw(poll_intervals())
    threshold_us = gap_threshold_us(interval_s)

    count = draw(st.integers(min_value=1, max_value=10))
    offsets_us = [0]
    for _ in range(count - 1):
        offsets_us.append(offsets_us[-1] + draw(timestamp_steps_us(threshold_us)))

    readings: list[PowerReading] = []
    for offset_us in offsets_us:
        moment = T0 + timedelta(microseconds=offset_us)
        if draw(_OCCASIONALLY):
            moment = moment.replace(tzinfo=None)
        readings.append(PowerReading(device_id=DEVICE, watts=draw(watt_values()), measured_at=moment))

    return Scenario(
        interval_s=interval_s,
        restored_total_kwh=draw(restored_totals()),
        readings=tuple(readings),
    )


def restored_totals() -> st.SearchStrategy[float]:
    """Cumulative totals a restore may hand to registration, held to 6 decimal places."""
    return st.one_of(
        st.just(0.0),
        st.integers(min_value=0, max_value=10**10).map(lambda units: round(units / 10**6, KWH_DECIMAL_PLACES)),
    )


class Subdivision(NamedTuple):
    """A reading pair and an intermediate reading on the straight line between them."""

    interval_s: int
    first: PowerReading
    middle: PowerReading
    last: PowerReading


@st.composite
def subdivisions(draw: st.DrawFn) -> Subdivision:
    """A pair within the gap threshold, plus its linearly interpolated midpoint.

    The span is at least 2 microseconds and the split is strictly inside it, so the intermediate reading
    advances the clock past the first and stays behind the last: a coincident timestamp would take the
    non-advancing path of Requirement 7.8 and test something else. Both sub-intervals are shorter than the
    whole, so if the whole is within the gap threshold then so are they.
    """
    interval_s = draw(poll_intervals())
    span_us = draw(st.integers(min_value=2, max_value=gap_threshold_us(interval_s)))
    split_us = draw(st.integers(min_value=1, max_value=span_us - 1))

    first_watts = draw(plausible_watts())
    last_watts = draw(plausible_watts())
    # Linear interpolation at the split point. Power that ramps linearly is exactly what the trapezoidal rule
    # integrates without error, which is why subdividing here must not change the total.
    middle_watts = first_watts + (last_watts - first_watts) * (split_us / span_us)

    return Subdivision(
        interval_s=interval_s,
        first=PowerReading(device_id=DEVICE, watts=first_watts, measured_at=T0),
        middle=PowerReading(
            device_id=DEVICE,
            watts=middle_watts,
            measured_at=T0 + timedelta(microseconds=split_us),
        ),
        last=PowerReading(
            device_id=DEVICE,
            watts=last_watts,
            measured_at=T0 + timedelta(microseconds=span_us),
        ),
    )


class GapCase(NamedTuple):
    """A baselined device and a later reading at a generated distance from the gap threshold."""

    interval_s: int
    gap_us: int
    starting_total_kwh: float
    earlier: PowerReading
    later: PowerReading


@st.composite
def gap_cases(draw: st.DrawFn) -> GapCase:
    """A reading pair whose gap straddles the ``4x`` threshold, including the boundary itself.

    The boundary is drawn explicitly and from both sides, because Requirement 7.5 discards only what *exceeds*
    the threshold: a gap of exactly ``4x`` the poll interval still contributes, and a comparison written as
    ``>=`` would pass every randomly drawn example and fail only here.
    """
    interval_s = draw(poll_intervals())
    threshold_us = gap_threshold_us(interval_s)

    # The boundary and its two neighbours are their own branches rather than values inside a sampled set, so
    # each of them gets a sixth of the budget instead of a few examples per run. Shrinking prefers the first
    # branch, so a failure minimizes to the boundary itself -- the most informative example there is.
    shape = draw(st.integers(min_value=0, max_value=5))
    if shape == 0:
        gap_us = threshold_us
    elif shape == 1:
        gap_us = threshold_us + 1
    elif shape == 2:
        gap_us = threshold_us - 1
    elif shape == 3:
        gap_us = draw(st.integers(min_value=1, max_value=threshold_us))
    elif shape == 4:
        gap_us = draw(st.integers(min_value=threshold_us + 1, max_value=4 * threshold_us))
    else:
        gap_us = draw(st.sampled_from([1, 2 * threshold_us]))

    return GapCase(
        interval_s=interval_s,
        gap_us=gap_us,
        starting_total_kwh=draw(restored_totals()),
        earlier=PowerReading(device_id=DEVICE, watts=draw(plausible_watts()), measured_at=T0),
        later=PowerReading(
            device_id=DEVICE,
            watts=draw(plausible_watts()),
            measured_at=T0 + timedelta(microseconds=gap_us),
        ),
    )


# --------------------------------------------------------------------------------------------------
# the model
# --------------------------------------------------------------------------------------------------


def is_usable(reading: PowerReading) -> bool:
    """Whether a reading can be integrated at all: a finite, non-negative value at an aware instant."""
    return math.isfinite(reading.watts) and reading.watts >= 0.0 and reading.measured_at.tzinfo is not None


def exact_interval_kwh(prev: PowerReading, curr: PowerReading) -> Fraction:
    """The contribution of one interval, in exact rational arithmetic and before rounding.

    Independent of the implementation: the elapsed time comes from the ``timedelta`` components as integers
    rather than from a float second count, and the mean and the division are exact. Clamped at zero, because
    Requirement 7.2 is part of the closed form rather than a separate step.
    """
    span = curr.measured_at - prev.measured_at
    seconds = Fraction(span.days * 86_400 + span.seconds) + Fraction(span.microseconds, MICROSECONDS_PER_SECOND)
    hours = seconds / SECONDS_PER_HOUR
    mean_watts = (Fraction(prev.watts) + Fraction(curr.watts)) / 2
    return max(Fraction(0), mean_watts * hours / WATTS_PER_KILOWATT)


# --------------------------------------------------------------------------------------------------
# the properties
# --------------------------------------------------------------------------------------------------


# Feature: remote-energy-meter, Property 27: For any sequence of Power_Reading values for one device,
# including sequences with equal, out-of-order, zero, and negative-adjacent values, the cumulative energy
# total is non-negative and never smaller after processing a reading than before it; the first reading after
# start leaves the total unchanged and becomes the baseline; and any reading whose timestamp is earlier than
# or equal to the baseline timestamp leaves both the total and the baseline unchanged.
# Validates: Requirements 7.2, 7.7, 7.8
@settings(max_examples=300, deadline=None)
@given(scenario=scenarios())
def test_cumulative_energy_never_decreases_and_baselines_advance(scenario: Scenario) -> None:
    """Requirements 7.2, 7.7, and 7.8, over hostile reading sequences."""
    accumulator = EnergyAccumulator()
    accumulator.register(DEVICE, scenario.interval_s, DeviceEnergyState(total_kwh=scenario.restored_total_kwh))

    # Requirement 7.7: registration keeps the restored total and drops the persisted baseline pair, so the
    # first reading after start re-baselines instead of integrating across an unknown gap.
    assert accumulator.total_kwh(DEVICE) == scenario.restored_total_kwh
    assert accumulator.state_of(DEVICE).baseline_at is None
    assert accumulator.state_of(DEVICE).baseline_watts is None
    assert baseline_reading(accumulator.state_of(DEVICE), DEVICE) is None

    baselined = False
    for reading in scenario.readings:
        before = accumulator.state_of(DEVICE)
        result = accumulator.record(reading)
        after = result.state

        # --- what the accumulator holds is what it reported ---
        assert after is accumulator.state_of(DEVICE)

        # --- the total is non-negative and never smaller than before this reading (Requirement 7.2) ---
        assert after.total_kwh >= 0.0
        assert after.total_kwh >= before.total_kwh
        assert result.added_kwh >= 0.0
        # The stored total is rounded after the addition, so it tracks the reported contribution to within one
        # half of the last place kept.
        assert after.total_kwh == pytest.approx(before.total_kwh + result.added_kwh, abs=ROUNDING_TOLERANCE)

        if not is_usable(reading):
            # Reported, never raised: a misbehaving plugin degrades one cycle, not the poll loop.
            assert result.outcome is AccumulationOutcome.DISCARDED_UNUSABLE_VALUE
            assert result.discarded
            assert after is before
            assert result.added_kwh == 0.0
            assert result.detail is not None
            continue

        if not baselined:
            # --- the first reading after start becomes the baseline and contributes nothing (Req. 7.7) ---
            assert result.outcome is AccumulationOutcome.BASELINED
            assert after.total_kwh == before.total_kwh
            assert after.baseline_watts == reading.watts
            assert after.baseline_at == reading.measured_at
            assert result.added_kwh == 0.0
            baselined = True
            continue

        baseline = baseline_reading(before, DEVICE)
        assert baseline is not None
        # Measured in whole microseconds rather than through exceeds_gap_threshold, so the model stays
        # independent of the module it is checking.
        gap_us = (reading.measured_at - baseline.measured_at) // timedelta(microseconds=1)

        if reading.measured_at <= baseline.measured_at:
            # --- a reading that does not advance the clock changes nothing at all (Requirement 7.8) ---
            assert result.outcome is AccumulationOutcome.DISCARDED_NOT_ADVANCING
            assert result.discarded
            assert after is before
            assert after.total_kwh == before.total_kwh
            assert after.baseline_at == baseline.measured_at
            assert after.baseline_watts == baseline.watts
            assert result.added_kwh == 0.0
        elif gap_us > gap_threshold_us(scenario.interval_s):
            # Requirement 7.5, asserted in full by Property 29; here it only has to not contribute.
            assert result.outcome is AccumulationOutcome.REBASELINED_AFTER_GAP
            assert after.total_kwh == before.total_kwh
            assert after.baseline_at == reading.measured_at
            assert result.added_kwh == 0.0
        else:
            assert result.outcome is AccumulationOutcome.ACCUMULATED
            assert result.accumulated
            assert result.added_kwh == interval_kwh(baseline, reading)
            assert after.baseline_watts == reading.watts
            assert after.baseline_at == reading.measured_at

    # --- across the whole sequence the total only ever grew ---
    assert accumulator.total_kwh(DEVICE) >= scenario.restored_total_kwh


# Feature: remote-energy-meter, Property 28: For any pair of consecutive Power_Reading values within the gap
# threshold, the interval contribution equals the mean of the two power values in watts multiplied by the
# elapsed hours and divided by 1000 rounded to 6 decimal places; and inserting an intermediate reading whose
# power is the linear interpolation of the pair at its timestamp produces the same cumulative total within
# rounding tolerance.
# Validates: Requirements 7.1
@settings(max_examples=200, deadline=None)
@given(case=subdivisions())
def test_energy_integration_is_additive_under_subdivision(case: Subdivision) -> None:
    """Requirement 7.1: the trapezoidal closed form, and additivity up to rounding."""
    whole = interval_kwh(case.first, case.last)

    # --- the closed form, against exact rational arithmetic rather than a repeat of the float expression ---
    exact = exact_interval_kwh(case.first, case.last)
    assert whole == pytest.approx(float(exact), abs=ROUNDING_TOLERANCE)
    assert whole >= 0.0
    assert round(whole, KWH_DECIMAL_PLACES) == whole

    # --- additivity under subdivision, at the level of the interval math ---
    # Only up to rounding: each of the three contributions is rounded to 6 decimal places independently, so
    # the two sides can differ by at most three half-places.
    parts = interval_kwh(case.first, case.middle) + interval_kwh(case.middle, case.last)
    assert parts == pytest.approx(whole, abs=ADDITIVITY_TOLERANCE)

    # --- and at the level of the accumulated total, which is the quantity Home Assistant sees ---
    start = DeviceEnergyState(total_kwh=0.0)
    baselined = accumulate(start, case.first, case.interval_s)
    assert baselined.outcome is AccumulationOutcome.BASELINED

    direct = accumulate(baselined.state, case.last, case.interval_s)
    subdivided_middle = accumulate(baselined.state, case.middle, case.interval_s)
    subdivided = accumulate(subdivided_middle.state, case.last, case.interval_s)

    assert direct.outcome is AccumulationOutcome.ACCUMULATED
    assert subdivided_middle.outcome is AccumulationOutcome.ACCUMULATED
    assert subdivided.outcome is AccumulationOutcome.ACCUMULATED
    assert subdivided.state.total_kwh == pytest.approx(direct.state.total_kwh, abs=ADDITIVITY_TOLERANCE)
    assert direct.state.total_kwh == pytest.approx(float(exact), abs=ROUNDING_TOLERANCE)

    # --- the baseline advances to the last reading either way ---
    assert direct.state.baseline_at == case.last.measured_at
    assert subdivided.state.baseline_at == case.last.measured_at


# Feature: remote-energy-meter, Property 29: For any pair of consecutive Power_Reading values for a device,
# the interval contributes to the cumulative energy total if and only if the elapsed time between their
# timestamps is at most 4 times that device's poll interval; when it exceeds the threshold the total is
# unchanged; and in both cases the later reading becomes the accumulation baseline.
# Validates: Requirements 7.5
@settings(max_examples=200, deadline=None)
@given(case=gap_cases())
def test_gaps_beyond_the_threshold_are_discarded_and_rebaselined(case: GapCase) -> None:
    """Requirement 7.5, including the boundary the strict comparison turns on."""
    threshold_us = gap_threshold_us(case.interval_s)
    # Exact integer arithmetic decides what the test expects, so the boundary example does not inherit the
    # implementation's float rounding.
    beyond_threshold = case.gap_us > threshold_us

    assert gap_threshold_s(case.interval_s) == GAP_THRESHOLD_INTERVALS * case.interval_s
    assert exceeds_gap_threshold(case.earlier.measured_at, case.later.measured_at, case.interval_s) is beyond_threshold
    assert elapsed_hours(case.earlier.measured_at, case.later.measured_at) > 0.0

    baselined = DeviceEnergyState(
        total_kwh=case.starting_total_kwh,
        baseline_watts=case.earlier.watts,
        baseline_at=case.earlier.measured_at,
    )
    result = accumulate(baselined, case.later, case.interval_s)

    if beyond_threshold:
        # --- the interval is discarded and the total stands still ---
        assert result.outcome is AccumulationOutcome.REBASELINED_AFTER_GAP
        assert not result.accumulated
        assert result.added_kwh == 0.0
        assert result.state.total_kwh == case.starting_total_kwh
        assert result.detail is not None
    else:
        # --- a gap of exactly 4x the interval still contributes: the comparison is strict ---
        assert result.outcome is AccumulationOutcome.ACCUMULATED
        assert result.accumulated
        assert result.added_kwh == interval_kwh(case.earlier, case.later)
        assert result.state.total_kwh == pytest.approx(
            case.starting_total_kwh + result.added_kwh, abs=ROUNDING_TOLERANCE
        )
        assert result.state.total_kwh >= case.starting_total_kwh

    # --- either way the later reading becomes the accumulation baseline ---
    assert result.state.baseline_watts == case.later.watts
    assert result.state.baseline_at == case.later.measured_at
    assert baseline_reading(result.state, DEVICE) == case.later
