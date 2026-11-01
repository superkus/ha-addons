"""Unit tests for the energy integration math and the accumulator (task 7.1).

Examples and boundaries only. The universal statements over generated reading sequences are Properties 27, 28, and 29
(tasks 7.3-7.5).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from remote_energy_meter.energy import (
    GAP_THRESHOLD_INTERVALS,
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

DEVICE = "poweredge_a"
INTERVAL_S = 30
T0 = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)


def reading(watts: float, at: datetime = T0, device_id: str = DEVICE) -> PowerReading:
    return PowerReading(device_id=device_id, watts=watts, measured_at=at)


def after(seconds: float, base: datetime = T0) -> datetime:
    return base + timedelta(seconds=seconds)


# --------------------------------------------------------------------------------------------------
# interval_kwh: trapezoidal, /1000, rounded to 6 decimals, clamped at zero (Requirements 7.1, 7.2)
# --------------------------------------------------------------------------------------------------


def test_interval_kwh_is_the_mean_power_over_the_elapsed_hours() -> None:
    """100 W and 300 W a full hour apart average 200 W, so 0.2 kWh."""
    assert interval_kwh(reading(100.0), reading(300.0, after(3600))) == pytest.approx(0.2)


def test_interval_kwh_scales_with_the_elapsed_time() -> None:
    """200 W held for 30 s is 200 * (30/3600) / 1000 kWh."""
    assert interval_kwh(reading(200.0), reading(200.0, after(30))) == pytest.approx(200 * 30 / 3600 / 1000, abs=5e-7)


def test_interval_kwh_rounds_to_six_decimal_places() -> None:
    """Requirement 7.1 fixes the resolution at 6 decimals of a kWh."""
    small = interval_kwh(reading(1.0), reading(1.0, after(1)))  # 1 W for 1 s = 2.777...e-7 kWh
    assert small == 0.0  # rounds away below the 6th decimal

    representable = interval_kwh(reading(3.6), reading(3.6, after(1)))  # 3.6 W for 1 s = 1e-6 kWh
    assert representable == 1e-6


def test_interval_kwh_of_a_zero_length_interval_is_zero() -> None:
    assert interval_kwh(reading(5000.0), reading(5000.0)) == 0.0


def test_interval_kwh_clamps_a_reversed_pair_at_zero() -> None:
    """A negative contribution contributes zero, so a total can never decrease (Requirement 7.2)."""
    assert interval_kwh(reading(200.0, after(3600)), reading(200.0, T0)) == 0.0


def test_interval_kwh_clamps_negative_power_at_zero() -> None:
    assert interval_kwh(reading(-500.0), reading(-500.0, after(3600))) == 0.0


def test_interval_kwh_rejects_naive_timestamps() -> None:
    naive = datetime(2025, 1, 1, 12, 0, 0)  # noqa: DTZ001 - the point of the test
    with pytest.raises(ValueError, match="timezone-aware"):
        interval_kwh(reading(100.0, naive), reading(100.0, after(30, naive)))


def test_interval_kwh_is_offset_correct_across_time_zones() -> None:
    """Aware timestamps in different offsets difference correctly; the elapsed span here is one hour."""
    berlin = timezone(timedelta(hours=1))
    later = datetime(2025, 1, 1, 14, 0, 0, tzinfo=berlin)  # 13:00 UTC
    assert interval_kwh(reading(100.0, T0), reading(100.0, later)) == pytest.approx(0.1)


def test_elapsed_hours_is_signed() -> None:
    assert elapsed_hours(T0, after(1800)) == pytest.approx(0.5)
    assert elapsed_hours(after(1800), T0) == pytest.approx(-0.5)


# --------------------------------------------------------------------------------------------------
# gap threshold (Requirement 7.5)
# --------------------------------------------------------------------------------------------------


def test_gap_threshold_is_four_poll_intervals() -> None:
    assert gap_threshold_s(INTERVAL_S) == GAP_THRESHOLD_INTERVALS * INTERVAL_S == 120.0


def test_a_gap_of_exactly_four_intervals_does_not_exceed_the_threshold() -> None:
    """Requirement 7.5 discards only what exceeds the threshold, so the boundary itself still integrates."""
    assert not exceeds_gap_threshold(T0, after(120), INTERVAL_S)
    assert exceeds_gap_threshold(T0, after(120.001), INTERVAL_S)


def test_the_exact_boundary_holds_for_an_interval_that_survives_no_float_round_trip() -> None:
    """57 s is the interval where converting the gap to hours and back misses the boundary.

    ``228 / 3600 * 3600`` is ``228.00000000000003``, so an implementation that measures the gap in hours reads a
    gap of exactly 4x this interval as exceeding its own threshold. Pinned by example because the generated
    boundary case of Property 29 only lands here sometimes and there is no example database to remember it.
    """
    interval_s = 57
    boundary = GAP_THRESHOLD_INTERVALS * interval_s  # 228 s
    assert not exceeds_gap_threshold(T0, after(boundary), interval_s)
    assert exceeds_gap_threshold(T0, T0 + timedelta(seconds=boundary, microseconds=1), interval_s)


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf")])
def test_gap_threshold_rejects_a_non_positive_or_non_finite_interval(bad: float) -> None:
    with pytest.raises(ValueError, match="poll interval"):
        gap_threshold_s(bad)


def test_gap_threshold_rejects_a_non_numeric_interval() -> None:
    with pytest.raises(TypeError, match="poll interval"):
        gap_threshold_s("30")  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------------------
# accumulate: state transitions
# --------------------------------------------------------------------------------------------------


def test_the_first_reading_baselines_and_leaves_the_total_unchanged() -> None:
    """Requirement 7.7."""
    result = accumulate(DeviceEnergyState(total_kwh=4.5), reading(200.0), INTERVAL_S)

    assert result.outcome is AccumulationOutcome.BASELINED
    assert result.added_kwh == 0.0
    assert result.state.total_kwh == 4.5
    assert (result.state.baseline_watts, result.state.baseline_at) == (200.0, T0)


def test_a_second_reading_accumulates_and_advances_the_baseline() -> None:
    """Requirement 7.1."""
    first = accumulate(DeviceEnergyState.zeroed(), reading(100.0), INTERVAL_S)
    second = accumulate(first.state, reading(300.0, after(3600)), 3600)

    assert second.outcome is AccumulationOutcome.ACCUMULATED
    assert second.added_kwh == pytest.approx(0.2)
    assert second.state.total_kwh == pytest.approx(0.2)
    assert (second.state.baseline_watts, second.state.baseline_at) == (300.0, after(3600))


def test_accumulation_adds_to_a_restored_total() -> None:
    """Requirement 7.4: a restored total is the starting point, not a reset."""
    baselined = accumulate(DeviceEnergyState(total_kwh=10.0), reading(100.0), 3600)
    accumulated = accumulate(baselined.state, reading(100.0, after(3600)), 3600)

    assert accumulated.state.total_kwh == pytest.approx(10.1)


def test_a_reading_at_the_baseline_timestamp_is_discarded_entirely() -> None:
    """Requirement 7.8: equal timestamps keep both the total and the baseline."""
    baselined = accumulate(DeviceEnergyState(total_kwh=2.0), reading(100.0), INTERVAL_S)
    same_time = accumulate(baselined.state, reading(900.0, T0), INTERVAL_S)

    assert same_time.outcome is AccumulationOutcome.DISCARDED_NOT_ADVANCING
    assert same_time.discarded
    assert same_time.state is baselined.state
    assert (same_time.state.baseline_watts, same_time.state.total_kwh) == (100.0, 2.0)


def test_a_reading_before_the_baseline_timestamp_is_discarded_entirely() -> None:
    """Requirement 7.8: an older reading must not walk the baseline backwards."""
    baselined = accumulate(DeviceEnergyState.zeroed(), reading(100.0, after(600)), INTERVAL_S)
    older = accumulate(baselined.state, reading(900.0, T0), INTERVAL_S)

    assert older.outcome is AccumulationOutcome.DISCARDED_NOT_ADVANCING
    assert older.state.baseline_at == after(600)
    assert older.state.total_kwh == 0.0


def test_a_gap_beyond_the_threshold_rebaselines_without_contributing() -> None:
    """Requirement 7.5: the interval is discarded, but the later reading becomes the baseline."""
    baselined = accumulate(DeviceEnergyState(total_kwh=1.0), reading(100.0), INTERVAL_S)
    after_gap = accumulate(baselined.state, reading(500.0, after(121)), INTERVAL_S)

    assert after_gap.outcome is AccumulationOutcome.REBASELINED_AFTER_GAP
    assert after_gap.added_kwh == 0.0
    assert after_gap.state.total_kwh == 1.0
    assert (after_gap.state.baseline_watts, after_gap.state.baseline_at) == (500.0, after(121))
    assert after_gap.detail is not None


def test_a_gap_at_exactly_the_threshold_still_contributes() -> None:
    baselined = accumulate(DeviceEnergyState.zeroed(), reading(360.0), INTERVAL_S)
    at_threshold = accumulate(baselined.state, reading(360.0, after(120)), INTERVAL_S)

    assert at_threshold.outcome is AccumulationOutcome.ACCUMULATED
    assert at_threshold.added_kwh == pytest.approx(360 * 120 / 3600 / 1000, abs=5e-7)


def test_the_gap_threshold_follows_the_devices_own_interval() -> None:
    """A device polled every 600 s tolerates a 600 s gap that a 30 s device would reject."""
    baselined = accumulate(DeviceEnergyState.zeroed(), reading(100.0), 600)

    assert accumulate(baselined.state, reading(100.0, after(600)), 600).outcome is AccumulationOutcome.ACCUMULATED
    assert (
        accumulate(baselined.state, reading(100.0, after(600)), INTERVAL_S).outcome
        is AccumulationOutcome.REBASELINED_AFTER_GAP
    )


@pytest.mark.parametrize("watts", [float("nan"), float("inf"), float("-inf"), -0.5])
def test_an_unusable_power_value_is_discarded_rather_than_raised(watts: float) -> None:
    """A plugin reporting a value outside its own contract degrades one cycle, not the poll loop."""
    baselined = accumulate(DeviceEnergyState(total_kwh=3.0), reading(100.0), INTERVAL_S)
    unusable = accumulate(baselined.state, reading(watts, after(30)), INTERVAL_S)

    assert unusable.outcome is AccumulationOutcome.DISCARDED_UNUSABLE_VALUE
    assert unusable.state is baselined.state
    assert unusable.detail is not None


def test_a_naive_timestamp_is_discarded_rather_than_raised() -> None:
    naive = datetime(2025, 1, 1, 12, 0, 30)  # noqa: DTZ001 - the point of the test
    baselined = accumulate(DeviceEnergyState.zeroed(), reading(100.0), INTERVAL_S)
    result = accumulate(baselined.state, reading(100.0, naive), INTERVAL_S)

    assert result.outcome is AccumulationOutcome.DISCARDED_UNUSABLE_VALUE
    assert result.state is baselined.state


def test_an_unusable_first_reading_does_not_baseline() -> None:
    result = accumulate(DeviceEnergyState.zeroed(), reading(float("nan")), INTERVAL_S)

    assert result.outcome is AccumulationOutcome.DISCARDED_UNUSABLE_VALUE
    assert result.state.baseline_at is None


def test_accumulate_rejects_a_non_positive_interval() -> None:
    with pytest.raises(ValueError, match="poll interval"):
        accumulate(DeviceEnergyState.zeroed(), reading(100.0), 0)


def test_a_zero_watt_pair_contributes_nothing_but_still_advances_the_baseline() -> None:
    baselined = accumulate(DeviceEnergyState(total_kwh=1.5), reading(0.0), INTERVAL_S)
    accumulated = accumulate(baselined.state, reading(0.0, after(30)), INTERVAL_S)

    assert accumulated.outcome is AccumulationOutcome.ACCUMULATED
    assert accumulated.state.total_kwh == 1.5
    assert accumulated.state.baseline_at == after(30)


# --------------------------------------------------------------------------------------------------
# baseline_reading adapter
# --------------------------------------------------------------------------------------------------


def test_baseline_reading_is_none_without_a_baseline() -> None:
    assert baseline_reading(DeviceEnergyState.zeroed(), DEVICE) is None


def test_baseline_reading_round_trips_the_persisted_pair() -> None:
    state = DeviceEnergyState(total_kwh=1.0, baseline_watts=214.0, baseline_at=T0)

    assert baseline_reading(state, DEVICE) == PowerReading(device_id=DEVICE, watts=214.0, measured_at=T0)


# --------------------------------------------------------------------------------------------------
# EnergyAccumulator
# --------------------------------------------------------------------------------------------------


def test_registration_starts_an_unknown_device_at_zero() -> None:
    acc = EnergyAccumulator()
    acc.register(DEVICE, INTERVAL_S)

    assert acc.total_kwh(DEVICE) == 0.0
    assert acc.device_ids == (DEVICE,)
    assert DEVICE in acc
    assert len(acc) == 1


def test_registration_restores_the_total_but_not_the_baseline() -> None:
    """Requirement 7.7: after a restart the gap is unknown, so the first reading re-baselines."""
    acc = EnergyAccumulator()
    persisted = DeviceEnergyState(total_kwh=12.345678, baseline_watts=214.0, baseline_at=T0)
    acc.register(DEVICE, INTERVAL_S, persisted)

    assert acc.total_kwh(DEVICE) == 12.345678
    assert acc.state_of(DEVICE).has_baseline is False

    first = acc.record(reading(500.0, after(86400)))
    assert first.outcome is AccumulationOutcome.BASELINED
    assert acc.total_kwh(DEVICE) == 12.345678


def test_register_all_registers_every_device_and_restores_where_present() -> None:
    acc = EnergyAccumulator()
    acc.register_all(
        {DEVICE: INTERVAL_S, "poweredge_b": 60},
        {DEVICE: DeviceEnergyState(total_kwh=7.5)},
    )

    assert acc.device_ids == (DEVICE, "poweredge_b")
    assert (acc.total_kwh(DEVICE), acc.total_kwh("poweredge_b")) == (7.5, 0.0)
    assert (acc.interval_s(DEVICE), acc.interval_s("poweredge_b")) == (30.0, 60.0)


def test_registering_a_device_twice_raises() -> None:
    acc = EnergyAccumulator()
    acc.register(DEVICE, INTERVAL_S)

    with pytest.raises(ValueError, match="already registered"):
        acc.register(DEVICE, INTERVAL_S)


def test_recording_for_an_unregistered_device_raises() -> None:
    acc = EnergyAccumulator()

    with pytest.raises(KeyError, match="not registered"):
        acc.record(reading(100.0))


def test_record_stores_the_new_state() -> None:
    acc = EnergyAccumulator()
    acc.register(DEVICE, 3600)

    acc.record(reading(100.0))
    result = acc.record(reading(300.0, after(3600)))

    assert result.accumulated
    assert acc.total_kwh(DEVICE) == pytest.approx(0.2)
    assert acc.state_of(DEVICE) is result.state


def test_devices_do_not_interact() -> None:
    """One device failing, stalling, or stepping its clock leaves every other total untouched."""
    acc = EnergyAccumulator()
    acc.register_all({DEVICE: 3600, "poweredge_b": 3600})

    acc.record(reading(100.0))
    acc.record(reading(100.0, T0, device_id="poweredge_b"))

    acc.record(reading(100.0, after(3600)))
    acc.record(reading(float("nan"), after(3600), device_id="poweredge_b"))

    assert acc.total_kwh(DEVICE) == pytest.approx(0.1)
    assert acc.total_kwh("poweredge_b") == 0.0


def test_snapshot_is_an_independent_mapping_of_persistable_states() -> None:
    acc = EnergyAccumulator()
    acc.register(DEVICE, 3600)
    acc.record(reading(100.0))

    snapshot = acc.snapshot()
    snapshot.pop(DEVICE)
    acc.record(reading(100.0, after(3600)))

    assert DEVICE in acc.snapshot()
    assert acc.snapshot()[DEVICE].total_kwh == pytest.approx(0.1)


def test_the_total_never_decreases_across_a_mixed_sequence() -> None:
    """Requirement 7.2, by example: discards, gaps, and reversed timestamps all leave the total non-decreasing."""
    acc = EnergyAccumulator()
    acc.register(DEVICE, INTERVAL_S)

    totals = []
    for offset, watts in [(0, 200.0), (30, 250.0), (60, -10.0), (90, 300.0), (45, 900.0), (1000, 400.0), (1030, 400.0)]:
        acc.record(reading(watts, after(offset)))
        totals.append(acc.total_kwh(DEVICE))

    assert totals == sorted(totals)
    assert totals[-1] > 0.0


def test_iterating_the_accumulator_yields_registered_identifiers() -> None:
    acc = EnergyAccumulator()
    acc.register_all({DEVICE: INTERVAL_S, "poweredge_b": INTERVAL_S})

    assert list(acc) == [DEVICE, "poweredge_b"]
