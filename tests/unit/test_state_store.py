"""Unit tests for the atomic energy state store (task 7.2).

Examples, boundaries, and the failure paths that do not vary with input. The universal statements over generated states
are Property 30 (persist-restore round trip, task 7.6) and Property 31 (corrupt state isolation, task 7.7).

Two things here are worth naming, because they are the tests that justify the design rather than describe it:

* the interrupted-write tests, which fail the rename and the ``fsync`` and then assert the *previous* document is still
  intact and no temp file is left behind — that is the whole point of writing through a temp file;
* the cadence tests, which drive :meth:`EnergyStateStore.run_periodic` with an injected sleep and monotonic clock, so
  the 300 second interval of Requirement 7.9 is asserted in milliseconds instead of waited out.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from remote_energy_meter.atomic_file import write_atomically
from remote_energy_meter.energy import EnergyAccumulator
from remote_energy_meter.persistence import CURRENT_SCHEMA_VERSION, DeviceEnergyState, EnergyStateFile
from remote_energy_meter.state_store import (
    PERIODIC_FLUSH_INTERVAL_S,
    SHUTDOWN_FLUSH_TIMEOUT_S,
    STATE_FILE_NAME,
    EnergyStateStore,
    FlushOutcome,
    RestoreFailure,
    RestoreRejection,
    build_state_file,
    parse_state,
    serialize_state,
)

DEVICE_A = "poweredge_a"
DEVICE_B = "poweredge_b"
T0 = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)


def store_at(tmp_path: Path, **kwargs: Any) -> EnergyStateStore:
    """A store over a temporary document, with a fixed wall clock unless overridden."""
    kwargs.setdefault("clock", lambda: T0)
    return EnergyStateStore(tmp_path / STATE_FILE_NAME, **kwargs)


def state(total: float, watts: float | None = None, at: datetime | None = None) -> DeviceEnergyState:
    return DeviceEnergyState(total_kwh=total, baseline_watts=watts, baseline_at=at)


def write_document(path: Path, document: object) -> None:
    """Write a document as text, bypassing the store, so a malformed shape can be planted."""
    path.write_text(json.dumps(document), encoding="utf-8")


def temp_files(directory: Path) -> list[Path]:
    """Every leftover temp file the store may have created next to the document."""
    return [entry for entry in directory.iterdir() if entry.name.endswith(".tmp")]


# --------------------------------------------------------------------------------------------------
# construction
# --------------------------------------------------------------------------------------------------


def test_default_path_is_the_state_file_inside_the_data_directory(tmp_path: Path) -> None:
    assert EnergyStateStore(data_dir=tmp_path).path == tmp_path / STATE_FILE_NAME


def test_default_flush_interval_is_the_requirement_bound(tmp_path: Path) -> None:
    """Requirement 7.9 allows at most 300 seconds between flushes."""
    assert store_at(tmp_path).flush_interval_s == PERIODIC_FLUSH_INTERVAL_S


@pytest.mark.parametrize("interval_s", [0.0, -1.0, PERIODIC_FLUSH_INTERVAL_S + 0.1, 3600.0])
def test_a_cadence_outside_the_requirement_bound_is_refused(tmp_path: Path, interval_s: float) -> None:
    """A cadence longer than 300 s would lose more energy than Requirement 7.9 permits, so it is a wiring error."""
    with pytest.raises(ValueError, match="flush interval"):
        store_at(tmp_path, flush_interval_s=interval_s)


# --------------------------------------------------------------------------------------------------
# serialization shape
# --------------------------------------------------------------------------------------------------


def test_serialized_document_carries_the_schema_version_and_explicit_fields() -> None:
    text = serialize_state(build_state_file({DEVICE_A: state(12.345678, 214.0, T0)}, T0))
    document = json.loads(text)

    assert document["schema_version"] == CURRENT_SCHEMA_VERSION
    assert document["updated_at"] == "2025-01-01T12:00:00Z"
    assert document["devices"][DEVICE_A] == {
        "total_kwh": 12.345678,
        "baseline_watts": 214.0,
        "baseline_at": "2025-01-01T12:00:00Z",
    }


def test_serialized_document_emits_an_absent_baseline_explicitly() -> None:
    document = json.loads(serialize_state(build_state_file({DEVICE_A: state(1.0)}, T0)))
    assert document["devices"][DEVICE_A] == {"total_kwh": 1.0, "baseline_watts": None, "baseline_at": None}


def test_serialization_is_byte_stable_regardless_of_snapshot_order() -> None:
    """Device keys are sorted, so two flushes of the same state produce identical text."""
    forward = serialize_state(build_state_file({DEVICE_A: state(1.0), DEVICE_B: state(2.0)}, T0))
    reverse = serialize_state(build_state_file({DEVICE_B: state(2.0), DEVICE_A: state(1.0)}, T0))
    assert forward == reverse
    assert forward.endswith("\n")


def test_serialization_refuses_a_non_finite_total() -> None:
    """`NaN` is not JSON. It cannot reach here through DeviceEnergyState, so the guard is against a hand-built file."""
    file = EnergyStateFile.__new__(EnergyStateFile)
    object.__setattr__(file, "schema_version", CURRENT_SCHEMA_VERSION)
    object.__setattr__(file, "updated_at", T0)
    object.__setattr__(file, "devices", {DEVICE_A: _unvalidated_state(float("nan"))})

    with pytest.raises(ValueError, match="Out of range float"):
        serialize_state(file)


def _unvalidated_state(total: float) -> DeviceEnergyState:
    """A state carrying a value its constructor would reject, for testing a downstream guard."""
    instance = DeviceEnergyState.__new__(DeviceEnergyState)
    object.__setattr__(instance, "total_kwh", total)
    object.__setattr__(instance, "baseline_watts", None)
    object.__setattr__(instance, "baseline_at", None)
    return instance


# --------------------------------------------------------------------------------------------------
# round trip (Requirements 7.3, 7.4)
# --------------------------------------------------------------------------------------------------


def test_flush_then_restore_returns_the_same_state(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    snapshot = {
        DEVICE_A: state(12.345678, 214.0, T0),
        DEVICE_B: state(0.0),
    }

    assert store.flush(snapshot).outcome is FlushOutcome.WRITTEN

    restored = store.restore()
    assert restored.restored
    assert dict(restored.devices) == snapshot
    assert restored.schema_version == CURRENT_SCHEMA_VERSION
    assert restored.updated_at == T0
    assert restored.rejections == ()


def test_round_trip_preserves_microseconds_and_normalizes_the_offset(tmp_path: Path) -> None:
    """A baseline stamped in a non-UTC offset restores as the same instant, to microsecond resolution."""
    east = timezone(timedelta(hours=2))
    measured = datetime(2025, 3, 4, 5, 6, 7, 891234, tzinfo=east)
    store = store_at(tmp_path)

    store.flush({DEVICE_A: state(1.5, 100.0, measured)})
    restored = store.restore()

    baseline_at = restored.devices[DEVICE_A].baseline_at
    assert baseline_at == measured
    assert baseline_at is not None
    assert baseline_at.utcoffset() == timedelta(0)


def test_round_trip_holds_a_total_to_six_decimal_places(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.flush({DEVICE_A: state(0.000001)})
    assert store.restore().devices[DEVICE_A].total_kwh == 0.000001


def test_flush_creates_the_data_directory(tmp_path: Path) -> None:
    """The container mounts /data, but a fresh volume may not have the parent of an injected path."""
    store = EnergyStateStore(tmp_path / "nested" / "deeper" / STATE_FILE_NAME, clock=lambda: T0)
    assert store.flush({DEVICE_A: state(1.0)}).written
    assert store.path.is_file()


def test_a_successful_flush_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.flush({DEVICE_A: state(1.0)})
    store.flush({DEVICE_A: state(2.0)})
    assert temp_files(tmp_path) == []


def test_flush_reports_the_device_count(tmp_path: Path) -> None:
    result = store_at(tmp_path).flush({DEVICE_A: state(1.0), DEVICE_B: state(2.0)})
    assert result.device_count == 2
    assert result.path == tmp_path / STATE_FILE_NAME


# --------------------------------------------------------------------------------------------------
# the interrupted write
# --------------------------------------------------------------------------------------------------


def test_a_failed_rename_leaves_the_previous_document_intact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The point of the temp file: a crash at the rename cannot truncate what is already on disk."""
    store = store_at(tmp_path)
    store.flush({DEVICE_A: state(7.5, 200.0, T0)})
    before = store.path.read_text(encoding="utf-8")

    def refuse(self: Path, target: Any) -> Path:
        raise OSError("simulated interruption at the rename")

    monkeypatch.setattr(Path, "replace", refuse)

    result = store.flush({DEVICE_A: state(9.75)})

    assert result.outcome is FlushOutcome.FAILED
    assert result.detail is not None
    assert "simulated interruption" in result.detail
    assert store.path.read_text(encoding="utf-8") == before
    assert temp_files(tmp_path) == []

    monkeypatch.undo()
    assert store.restore().devices[DEVICE_A] == state(7.5, 200.0, T0)


def test_a_failed_fsync_leaves_the_previous_document_intact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An I/O error before the rename is the same story: the document on disk never changed."""
    store = store_at(tmp_path)
    store.flush({DEVICE_A: state(3.0)})
    before = store.path.read_text(encoding="utf-8")

    real_fsync = os.fsync

    def refuse(fd: int) -> None:
        raise OSError("simulated interruption at fsync")

    monkeypatch.setattr(os, "fsync", refuse)
    result = store.flush({DEVICE_A: state(4.0)})
    monkeypatch.setattr(os, "fsync", real_fsync)

    assert result.outcome is FlushOutcome.FAILED
    assert store.path.read_text(encoding="utf-8") == before
    assert temp_files(tmp_path) == []


def test_a_first_flush_that_fails_leaves_no_document_at_all(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = store_at(tmp_path)

    def refuse(self: Path, target: Any) -> Path:
        raise OSError("simulated interruption at the rename")

    monkeypatch.setattr(Path, "replace", refuse)

    assert store.flush({DEVICE_A: state(1.0)}).outcome is FlushOutcome.FAILED
    assert not store.path.exists()
    assert temp_files(tmp_path) == []


def test_a_truncated_document_is_reported_as_malformed_not_as_a_missing_file(tmp_path: Path) -> None:
    """What an interrupted write would have produced without the temp file, so the classification is pinned."""
    store = store_at(tmp_path)
    store.flush({DEVICE_A: state(5.0, 100.0, T0)})
    complete = store.path.read_text(encoding="utf-8")
    store.path.write_text(complete[: len(complete) // 2], encoding="utf-8")

    restored = store.restore()

    assert restored.failure is RestoreFailure.FILE_MALFORMED
    assert restored.devices == {}


def test_an_unsupported_directory_fsync_does_not_fail_the_flush(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Some network mounts refuse a directory fsync; the contents are durable and the rename is atomic regardless."""
    store = store_at(tmp_path)
    real_fsync = os.fsync

    def refuse_directories(fd: int) -> None:
        if os.fstat(fd).st_mode & 0o170000 == 0o040000:
            raise OSError("simulated unsupported directory fsync")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", refuse_directories)
    assert store.flush({DEVICE_A: state(1.0)}).written


def test_flush_writes_through_the_shared_atomic_writer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The store must not grow a parallel writer: its document goes through ``atomic_file.write_atomically``."""
    writes: list[tuple[Path, bytes]] = []

    def recording_write(path: Path, data: bytes) -> None:
        writes.append((path, data))
        write_atomically(path, data)

    monkeypatch.setattr("remote_energy_meter.state_store.write_atomically", recording_write)
    store = store_at(tmp_path)
    snapshot = {DEVICE_A: state(2.5, 120.0, T0)}

    assert store.flush(snapshot).written

    expected = serialize_state(build_state_file(snapshot, T0)).encode("utf-8")
    assert writes == [(store.path, expected)]
    assert store.path.read_bytes() == expected


def test_the_state_document_is_owner_only(tmp_path: Path) -> None:
    store = store_at(tmp_path)

    assert store.flush({DEVICE_A: state(1.0)}).written

    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


# --------------------------------------------------------------------------------------------------
# restore: document-level failures (Requirement 7.6)
# --------------------------------------------------------------------------------------------------


def test_a_missing_document_is_the_expected_first_start(tmp_path: Path) -> None:
    restored = store_at(tmp_path).restore()

    assert restored.failure is RestoreFailure.FILE_MISSING
    assert restored.failure.is_first_start
    assert restored.devices == {}
    assert restored.totals == {}


def test_an_unreadable_document_is_classified_not_raised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = store_at(tmp_path)
    store.flush({DEVICE_A: state(1.0)})

    def refuse(self: Path, **kwargs: Any) -> str:
        raise PermissionError("simulated permission denial")

    monkeypatch.setattr(Path, "read_text", refuse)

    restored = store.restore()
    assert restored.failure is RestoreFailure.FILE_UNREADABLE
    assert restored.detail is not None
    assert "PermissionError" in restored.detail


def test_a_document_that_is_not_utf8_is_malformed(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.path.write_bytes(b"\xff\xfe not utf-8")
    assert store.restore().failure is RestoreFailure.FILE_MALFORMED


def test_a_document_larger_than_the_bound_is_not_parsed(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.path.write_text("x" * (1 << 20) + "y", encoding="utf-8")

    restored = store.restore()
    assert restored.failure is RestoreFailure.FILE_UNREADABLE
    assert restored.detail is not None
    assert "larger than" in restored.detail


@pytest.mark.parametrize(
    "document",
    [
        [],
        "a string",
        42,
    ],
)
def test_a_document_that_is_not_an_object_is_malformed(tmp_path: Path, document: object) -> None:
    store = store_at(tmp_path)
    write_document(store.path, document)
    assert store.restore().failure is RestoreFailure.FILE_MALFORMED


@pytest.mark.parametrize("raw_version", [None, "1", 1.0, True])
def test_a_document_without_an_integer_schema_version_is_malformed(tmp_path: Path, raw_version: object) -> None:
    """A version that is not an integer cannot be compared, so it cannot be trusted to describe the entries."""
    store = store_at(tmp_path)
    write_document(store.path, {"schema_version": raw_version, "devices": {DEVICE_A: {"total_kwh": 5.0}}})

    restored = store.restore()
    assert restored.failure is RestoreFailure.FILE_MALFORMED
    assert restored.devices == {}


@pytest.mark.parametrize("foreign_version", [CURRENT_SCHEMA_VERSION + 1, CURRENT_SCHEMA_VERSION - 1, 99])
def test_a_foreign_schema_version_is_detected_rather_than_read_as_corruption(
    tmp_path: Path, foreign_version: int
) -> None:
    """Requirement 7.6: a format change must be a stated reason, not fifty corrupt entries."""
    store = store_at(tmp_path)
    write_document(
        store.path,
        {
            "schema_version": foreign_version,
            "updated_at": "2025-01-01T12:00:00Z",
            "devices": {DEVICE_A: {"total_kwh": 5.0, "kilojoules": 1}},
        },
    )

    restored = store.restore()

    assert restored.failure is RestoreFailure.FOREIGN_SCHEMA
    assert restored.schema_version == foreign_version
    assert restored.devices == {}
    assert restored.rejections == ()
    assert restored.detail is not None
    assert str(foreign_version) in restored.detail
    assert str(CURRENT_SCHEMA_VERSION) in restored.detail


def test_a_devices_member_that_is_not_an_object_is_malformed(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    write_document(store.path, {"schema_version": CURRENT_SCHEMA_VERSION, "devices": [DEVICE_A]})
    assert store.restore().failure is RestoreFailure.FILE_MALFORMED


def test_an_absent_devices_member_restores_nothing_without_a_failure(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    write_document(store.path, {"schema_version": CURRENT_SCHEMA_VERSION, "updated_at": "2025-01-01T12:00:00Z"})

    restored = store.restore()
    assert restored.restored
    assert restored.devices == {}


@pytest.mark.parametrize("raw_updated_at", [None, "not a timestamp", "2025-01-01T12:00:00", 17, []])
def test_an_unusable_document_timestamp_does_not_discard_the_totals(tmp_path: Path, raw_updated_at: object) -> None:
    """``updated_at`` is diagnostics. Throwing away real totals over it would be the wrong trade."""
    store = store_at(tmp_path)
    write_document(
        store.path,
        {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "updated_at": raw_updated_at,
            "devices": {DEVICE_A: {"total_kwh": 8.5}},
        },
    )

    restored = store.restore()

    assert restored.restored
    assert restored.updated_at is None
    assert restored.totals == {DEVICE_A: 8.5}


# --------------------------------------------------------------------------------------------------
# restore: per-device rejections (Requirement 7.6)
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        pytest.param([1, 2], RestoreRejection.ENTRY_MALFORMED, id="entry-is-a-list"),
        pytest.param("12.5", RestoreRejection.ENTRY_MALFORMED, id="entry-is-text"),
        pytest.param({}, RestoreRejection.TOTAL_MISSING, id="total-absent"),
        pytest.param({"baseline_watts": 1.0}, RestoreRejection.TOTAL_MISSING, id="baseline-only"),
        pytest.param({"total_kwh": "12.5"}, RestoreRejection.TOTAL_NOT_A_NUMBER, id="total-is-text"),
        pytest.param({"total_kwh": True}, RestoreRejection.TOTAL_NOT_A_NUMBER, id="total-is-a-boolean"),
        pytest.param({"total_kwh": None}, RestoreRejection.TOTAL_NOT_A_NUMBER, id="total-is-null"),
        pytest.param({"total_kwh": {"kwh": 1}}, RestoreRejection.TOTAL_NOT_A_NUMBER, id="total-is-an-object"),
        pytest.param({"total_kwh": -0.5}, RestoreRejection.TOTAL_OUT_OF_RANGE, id="total-is-negative"),
    ],
)
def test_a_corrupt_entry_starts_that_device_at_zero_with_a_stated_reason(
    tmp_path: Path, entry: object, expected: RestoreRejection
) -> None:
    store = store_at(tmp_path)
    write_document(
        store.path,
        {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "updated_at": "2025-01-01T12:00:00Z",
            "devices": {DEVICE_A: entry},
        },
    )

    restored = store.restore()

    assert restored.restored
    assert restored.totals == {DEVICE_A: 0.0}
    assert [(rejection.device_id, rejection.rejection) for rejection in restored.rejections] == [(DEVICE_A, expected)]
    assert restored.rejections[0].resets_total
    assert restored.rejections[0].detail
    assert DEVICE_A in restored.rejections[0].describe()


@pytest.mark.parametrize("raw_total", ["NaN", "Infinity", "-Infinity"])
def test_a_non_finite_total_starts_that_device_at_zero(tmp_path: Path, raw_total: str) -> None:
    """JSON's own grammar has no NaN, but ``json.loads`` accepts the literals, so the guard has to hold here."""
    store = store_at(tmp_path)
    store.path.write_text(
        f'{{"schema_version": {CURRENT_SCHEMA_VERSION}, "updated_at": "2025-01-01T12:00:00Z", '
        f'"devices": {{"{DEVICE_A}": {{"total_kwh": {raw_total}}}}}}}',
        encoding="utf-8",
    )

    restored = store.restore()

    assert restored.totals == {DEVICE_A: 0.0}
    assert restored.rejections[0].rejection is RestoreRejection.TOTAL_OUT_OF_RANGE


def test_one_corrupt_entry_does_not_disturb_its_neighbours(tmp_path: Path) -> None:
    """Requirement 7.6 is per device: forty-nine good totals survive one bad one."""
    store = store_at(tmp_path)
    write_document(
        store.path,
        {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "updated_at": "2025-01-01T12:00:00Z",
            "devices": {
                DEVICE_A: {"total_kwh": 12.345678, "baseline_watts": 214.0, "baseline_at": "2025-01-01T11:59:30Z"},
                DEVICE_B: {"total_kwh": -3.0},
                "poweredge_c": {"total_kwh": 4.5},
            },
        },
    )

    restored = store.restore()

    assert restored.totals == {DEVICE_A: 12.345678, DEVICE_B: 0.0, "poweredge_c": 4.5}
    assert [rejection.device_id for rejection in restored.rejections] == [DEVICE_B]
    assert restored.devices[DEVICE_A].baseline_at == T0 - timedelta(seconds=30)


@pytest.mark.parametrize(
    "entry",
    [
        pytest.param({"total_kwh": 6.25, "baseline_watts": 100.0}, id="timestamp-absent"),
        pytest.param({"total_kwh": 6.25, "baseline_at": "2025-01-01T12:00:00Z"}, id="watts-absent"),
        pytest.param({"total_kwh": 6.25, "baseline_watts": 100.0, "baseline_at": "2025-01-01T12:00:00"}, id="naive"),
        pytest.param({"total_kwh": 6.25, "baseline_watts": 100.0, "baseline_at": "yesterday"}, id="unparseable"),
        pytest.param({"total_kwh": 6.25, "baseline_watts": 100.0, "baseline_at": 1735732800}, id="epoch-seconds"),
        pytest.param({"total_kwh": 6.25, "baseline_watts": "100", "baseline_at": "2025-01-01T12:00:00Z"}, id="text-w"),
        pytest.param({"total_kwh": 6.25, "baseline_watts": -1.0, "baseline_at": "2025-01-01T12:00:00Z"}, id="neg-w"),
    ],
)
def test_an_unusable_baseline_keeps_the_total(tmp_path: Path, entry: object) -> None:
    """The baseline is diagnostics and is never restored as an active baseline, so it cannot cost a real total."""
    store = store_at(tmp_path)
    write_document(
        store.path,
        {"schema_version": CURRENT_SCHEMA_VERSION, "updated_at": "2025-01-01T12:00:00Z", "devices": {DEVICE_A: entry}},
    )

    restored = store.restore()

    assert restored.totals == {DEVICE_A: 6.25}
    assert restored.devices[DEVICE_A].baseline_at is None
    assert restored.rejections[0].rejection is RestoreRejection.BASELINE_UNUSABLE
    assert not restored.rejections[0].resets_total


def test_restore_logs_one_record_per_rejected_device_naming_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Requirement 7.6: the record names the device identifier and the reason."""
    store = store_at(tmp_path)
    write_document(
        store.path,
        {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "updated_at": "2025-01-01T12:00:00Z",
            "devices": {DEVICE_A: {"total_kwh": -1.0}, DEVICE_B: {"total_kwh": 2.0}},
        },
    )

    with caplog.at_level("DEBUG"):
        store.restore()

    warnings = [record for record in caplog.records if record.levelname == "WARNING"]
    assert len(warnings) == 1
    assert getattr(warnings[0], "device_id", None) == DEVICE_A
    assert getattr(warnings[0], "reason", None) == RestoreRejection.TOTAL_OUT_OF_RANGE.value


def test_a_missing_document_is_logged_at_info_not_as_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A first start has nothing to restore. Reporting that as a warning would train operators to ignore warnings."""
    with caplog.at_level("DEBUG"):
        store_at(tmp_path).restore()

    assert [record.levelname for record in caplog.records] == ["INFO"]


def test_a_foreign_schema_is_logged_as_a_document_level_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = store_at(tmp_path)
    write_document(store.path, {"schema_version": 99, "devices": {DEVICE_A: {"total_kwh": 1.0}}})

    with caplog.at_level("DEBUG"):
        store.restore()

    warnings = [record for record in caplog.records if record.levelname == "WARNING"]
    assert len(warnings) == 1
    assert getattr(warnings[0], "reason", None) == RestoreFailure.FOREIGN_SCHEMA.value
    assert not hasattr(warnings[0], "device_id")


# --------------------------------------------------------------------------------------------------
# the baseline is retained for diagnostics, not restored as a baseline (Requirements 7.3, 7.7)
# --------------------------------------------------------------------------------------------------


def test_restore_exposes_the_persisted_baseline_timestamps(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.flush({DEVICE_A: state(1.0, 214.0, T0), DEVICE_B: state(2.0)})

    restored = store.restore()

    assert restored.baselines == {DEVICE_A: T0}
    assert restored.baseline_at(DEVICE_A) == T0
    assert restored.baseline_at(DEVICE_B) is None
    assert restored.baseline_at("never_configured") is None


def test_registering_a_restored_state_keeps_the_total_and_drops_the_baseline(tmp_path: Path) -> None:
    """The accumulator does the dropping (Requirement 7.7); the store keeps the timestamp for the log record."""
    store = store_at(tmp_path)
    store.flush({DEVICE_A: state(12.345678, 214.0, T0)})
    restored = store.restore()

    accumulator = EnergyAccumulator()
    accumulator.register_all({DEVICE_A: 30.0}, restored.devices)

    assert accumulator.total_kwh(DEVICE_A) == 12.345678
    assert not accumulator.state_of(DEVICE_A).has_baseline
    assert restored.baseline_at(DEVICE_A) == T0


def test_a_snapshot_of_a_restored_accumulator_round_trips(tmp_path: Path) -> None:
    """The full startup-to-shutdown path: restore, register, snapshot, flush, restore again."""
    store = store_at(tmp_path)
    store.flush({DEVICE_A: state(4.0, 100.0, T0)})

    accumulator = EnergyAccumulator()
    accumulator.register_all({DEVICE_A: 30.0}, store.restore().devices)
    store.flush(accumulator.snapshot())

    assert store.restore().devices == {DEVICE_A: state(4.0)}


# --------------------------------------------------------------------------------------------------
# cadence (Requirement 7.9)
# --------------------------------------------------------------------------------------------------


class VirtualClock:
    """A monotonic clock and a sleep function that advance only when asked.

    Injected instead of waiting, so the 300 second cadence is asserted in milliseconds. ``max_sleeps`` ends the loop
    under test the way cancellation would, which is the only way ``run_periodic`` ever exits.
    """

    def __init__(self, max_sleeps: int) -> None:
        self.now = 1000.0
        self.max_sleeps = max_sleeps
        self.requested: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        if len(self.requested) >= self.max_sleeps:
            raise asyncio.CancelledError
        self.requested.append(delay)
        self.now += delay
        await asyncio.sleep(0)


async def test_run_periodic_flushes_once_per_interval(tmp_path: Path) -> None:
    """Requirement 7.9: at most 300 seconds of accumulated energy is at risk between flushes."""
    clock = VirtualClock(max_sleeps=3)
    store = store_at(tmp_path, monotonic=clock.monotonic)
    totals = iter([1.0, 2.0, 3.0])
    flushed: list[float] = []

    def source() -> dict[str, DeviceEnergyState]:
        total = next(totals)
        flushed.append(total)
        return {DEVICE_A: state(total)}

    with pytest.raises(asyncio.CancelledError):
        await store.run_periodic(source, sleep=clock.sleep)

    assert clock.requested == [PERIODIC_FLUSH_INTERVAL_S] * 3
    assert flushed == [1.0, 2.0, 3.0]
    assert store.restore().totals == {DEVICE_A: 3.0}


async def test_run_periodic_sleeps_before_its_first_flush(tmp_path: Path) -> None:
    """Nothing has accumulated at start, and the restored document is already current."""
    clock = VirtualClock(max_sleeps=0)
    store = store_at(tmp_path, monotonic=clock.monotonic)

    with pytest.raises(asyncio.CancelledError):
        await store.run_periodic(lambda: {DEVICE_A: state(1.0)}, sleep=clock.sleep)

    assert not store.path.exists()


async def test_run_periodic_honours_a_shorter_configured_cadence(tmp_path: Path) -> None:
    clock = VirtualClock(max_sleeps=2)
    store = store_at(tmp_path, flush_interval_s=60.0, monotonic=clock.monotonic)

    with pytest.raises(asyncio.CancelledError):
        await store.run_periodic(lambda: {DEVICE_A: state(1.0)}, sleep=clock.sleep)

    assert clock.requested == [60.0, 60.0]


def test_a_flush_is_due_until_one_succeeds(tmp_path: Path) -> None:
    now = [1000.0]
    store = store_at(tmp_path, monotonic=lambda: now[0])

    # Each reading is bound to its own local: asserting on the property directly would have mypy carry the narrowing
    # from one assertion into the next and call the rest of the test unreachable.
    before_any_flush = (store.is_due, store.seconds_until_due())
    assert before_any_flush == (True, 0.0)

    store.flush({DEVICE_A: state(1.0)})
    just_flushed = (store.is_due, store.seconds_until_due())
    assert just_flushed == (False, PERIODIC_FLUSH_INTERVAL_S)

    now[0] += PERIODIC_FLUSH_INTERVAL_S - 1
    one_second_short = (store.is_due, store.seconds_until_due())
    assert one_second_short == (False, 1.0)

    now[0] += 1
    at_the_cadence = (store.is_due, store.seconds_until_due())
    assert at_the_cadence == (True, 0.0)


def test_a_failed_flush_leaves_the_next_one_due(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The cadence measures actual persistence, so a failure retries at the next opportunity."""
    now = [1000.0]
    store = store_at(tmp_path, monotonic=lambda: now[0])
    store.flush({DEVICE_A: state(1.0)})
    now[0] += PERIODIC_FLUSH_INTERVAL_S

    def refuse(self: Path, target: Any) -> Path:
        raise OSError("simulated interruption at the rename")

    monkeypatch.setattr(Path, "replace", refuse)
    assert store.flush({DEVICE_A: state(2.0)}).outcome is FlushOutcome.FAILED
    assert store.is_due


async def test_flush_if_due_skips_when_the_cadence_has_not_elapsed(tmp_path: Path) -> None:
    now = [1000.0]
    store = store_at(tmp_path, monotonic=lambda: now[0])
    calls = [0]

    def source() -> dict[str, DeviceEnergyState]:
        calls[0] += 1
        return {DEVICE_A: state(float(calls[0]))}

    first = await store.flush_if_due(source)
    assert first is not None
    assert first.written

    assert await store.flush_if_due(source) is None
    assert calls[0] == 1

    now[0] += PERIODIC_FLUSH_INTERVAL_S
    second = await store.flush_if_due(source)
    assert second is not None
    assert second.written
    assert store.restore().totals == {DEVICE_A: 2.0}


# --------------------------------------------------------------------------------------------------
# the bounded shutdown flush (Requirement 7.3)
# --------------------------------------------------------------------------------------------------


def test_the_shutdown_bound_is_five_seconds() -> None:
    assert SHUTDOWN_FLUSH_TIMEOUT_S == 5.0


async def test_flush_async_writes_the_document(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    result = await store.flush_async({DEVICE_A: state(2.5)})

    assert result.written
    assert store.restore().totals == {DEVICE_A: 2.5}


async def test_flush_within_returns_the_result_when_the_write_completes(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    result = await store.flush_within({DEVICE_A: state(2.5)}, timeout_s=SHUTDOWN_FLUSH_TIMEOUT_S)

    assert result.written
    assert store.restore().totals == {DEVICE_A: 2.5}


async def test_flush_within_reports_a_timeout_instead_of_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Requirement 7.3 bounds the wait; the shutdown sequence still has to reach its remaining steps."""
    store = store_at(tmp_path)
    real_flush = EnergyStateStore.flush

    def stall(self: EnergyStateStore, snapshot: Any) -> Any:
        time.sleep(0.4)
        return real_flush(self, snapshot)

    monkeypatch.setattr(EnergyStateStore, "flush", stall)

    result = await store.flush_within({DEVICE_A: state(1.0)}, timeout_s=0.05)

    assert result.outcome is FlushOutcome.TIMED_OUT
    assert result.detail is not None
    assert "0.05 seconds" in result.detail

    # The worker thread was shielded, not abandoned, so a stalled filesystem yields a late flush rather than none.
    await asyncio.sleep(0.6)
    monkeypatch.undo()
    assert store.restore().totals == {DEVICE_A: 1.0}


async def test_flush_within_can_abandon_a_stalled_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = store_at(tmp_path)

    def stall(self: EnergyStateStore, snapshot: Any) -> Any:
        time.sleep(0.2)
        raise AssertionError("unreachable in this test")

    monkeypatch.setattr(EnergyStateStore, "flush", stall)

    result = await store.flush_within({DEVICE_A: state(1.0)}, timeout_s=0.02, shielded=False)
    assert result.outcome is FlushOutcome.TIMED_OUT


# --------------------------------------------------------------------------------------------------
# parse_state is pure
# --------------------------------------------------------------------------------------------------


def test_parse_state_never_touches_the_filesystem(tmp_path: Path) -> None:
    result = parse_state(serialize_state(build_state_file({DEVICE_A: state(1.0, 100.0, T0)}, T0)))

    assert result.restored
    assert result.devices == {DEVICE_A: state(1.0, 100.0, T0)}
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("text", ["", "   ", "{", "not json at all", "[1, 2, 3]"])
def test_parse_state_classifies_unparseable_text_without_raising(text: str) -> None:
    result = parse_state(text)
    assert result.failure is RestoreFailure.FILE_MALFORMED
    assert result.devices == {}
