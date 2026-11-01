"""Unit tests for the runtime, rejection, and persistence models (task 2.3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from remote_energy_meter.config.runtime import EntryRejection, RejectionReason
from remote_energy_meter.persistence import (
    CURRENT_SCHEMA_VERSION,
    DeviceEnergyState,
    EnergyStateFile,
)

# --------------------------------------------------------------------------------------------------
# rejection reporting
# --------------------------------------------------------------------------------------------------


def test_rejection_reason_covers_every_per_entry_rejection_path() -> None:
    """Every non-fatal exclusion the parser or the registry can produce has a member (Tier 2 of Error Handling)."""
    assert {reason.value for reason in RejectionReason} == {
        "schema_violation",  # 2.2, 2.3
        "invalid_identifier",  # 2.12
        "duplicate_identifier",  # 2.5
        "unknown_plugin",  # 3.4
        "empty_credential",  # 8.5
        "oversized_credential",  # 8.8
        "unresolved_secret",  # 8.6
    }


def test_entry_rejection_defaults_field_name_to_none() -> None:
    """A duplicate identifier or unknown plugin does not concern a single field."""
    rejection = EntryRejection(device_id="poweredge_a", reason=RejectionReason.DUPLICATE_IDENTIFIER)

    assert rejection.field_name is None


def test_entry_rejection_names_the_first_failing_field() -> None:
    rejection = EntryRejection(
        device_id="poweredge_a",
        reason=RejectionReason.SCHEMA_VIOLATION,
        field_name="host",
    )

    assert (rejection.device_id, rejection.reason, rejection.field_name) == (
        "poweredge_a",
        RejectionReason.SCHEMA_VIOLATION,
        "host",
    )


# --------------------------------------------------------------------------------------------------
# persisted device state
# --------------------------------------------------------------------------------------------------


def test_device_energy_state_accepts_a_baseline_pair() -> None:
    measured_at = datetime(2025, 1, 1, tzinfo=UTC)
    state = DeviceEnergyState(total_kwh=12.345678, baseline_watts=214.0, baseline_at=measured_at)

    assert state.total_kwh == pytest.approx(12.345678)
    assert state.has_baseline


def test_device_energy_state_zeroed_is_the_fallback_start() -> None:
    """Requirement 7.6: a device with no usable persisted total starts at 0 kWh with no baseline."""
    state = DeviceEnergyState.zeroed()

    assert state.total_kwh == 0.0
    assert not state.has_baseline


@pytest.mark.parametrize("total", [-0.000001, -1.0, float("nan"), float("inf")])
def test_device_energy_state_rejects_unusable_totals(total: float) -> None:
    """Requirement 7.6: a negative or non-finite total is detectable per device, not silently accepted."""
    with pytest.raises(ValueError, match="total_kwh"):
        DeviceEnergyState(total_kwh=total)


def test_device_energy_state_rejects_a_non_numeric_total() -> None:
    """A structural problem in the persisted document is distinguishable from an out-of-range total."""
    with pytest.raises(TypeError, match="total_kwh"):
        DeviceEnergyState(total_kwh="12.3")  # type: ignore[arg-type]


def test_device_energy_state_rejects_a_negative_baseline_watts() -> None:
    with pytest.raises(ValueError, match="baseline_watts"):
        DeviceEnergyState(total_kwh=1.0, baseline_watts=-1.0, baseline_at=datetime(2025, 1, 1, tzinfo=UTC))


def test_device_energy_state_rejects_a_naive_baseline_timestamp() -> None:
    """Requirement 7.3: persisted baselines are timezone-aware UTC."""
    with pytest.raises(ValueError, match="timezone-aware"):
        DeviceEnergyState(total_kwh=1.0, baseline_watts=100.0, baseline_at=datetime(2025, 1, 1))  # noqa: DTZ001


@pytest.mark.parametrize(
    ("watts", "moment"),
    [
        (100.0, None),
        (None, datetime(2025, 1, 1, tzinfo=UTC)),
    ],
)
def test_device_energy_state_rejects_a_half_present_baseline(watts: float | None, moment: datetime | None) -> None:
    with pytest.raises(ValueError, match="both be present or both be absent"):
        DeviceEnergyState(total_kwh=1.0, baseline_watts=watts, baseline_at=moment)


# --------------------------------------------------------------------------------------------------
# persisted state document
# --------------------------------------------------------------------------------------------------


def test_energy_state_file_defaults_to_no_devices() -> None:
    document = EnergyStateFile(schema_version=CURRENT_SCHEMA_VERSION, updated_at=datetime(2025, 1, 1, tzinfo=UTC))

    assert document.devices == {}
    assert document.is_current_schema


def test_energy_state_file_flags_a_foreign_schema_version() -> None:
    """Requirement 7.6: a future format is detectable rather than read as corruption."""
    document = EnergyStateFile(
        schema_version=CURRENT_SCHEMA_VERSION + 1,
        updated_at=datetime(2025, 1, 1, tzinfo=UTC),
    )

    assert not document.is_current_schema


def test_energy_state_file_accepts_a_non_utc_aware_timestamp() -> None:
    """Any aware offset is readable; only a naive timestamp is ambiguous."""
    document = EnergyStateFile(
        schema_version=CURRENT_SCHEMA_VERSION,
        updated_at=datetime(2025, 1, 1, tzinfo=timezone(timedelta(hours=2))),
    )

    assert document.updated_at.tzinfo is not None


def test_energy_state_file_rejects_a_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        EnergyStateFile(schema_version=CURRENT_SCHEMA_VERSION, updated_at=datetime(2025, 1, 1))  # noqa: DTZ001
