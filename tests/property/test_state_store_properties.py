"""Properties 30 and 31: persisted energy state round trips, and corrupt entries stay isolated (tasks 7.6, 7.7).

Two statements about ``state_store.py``:

* **Property 30** — persisting a state and restoring it yields a state field-by-field equal to the original,
  including totals to 6 decimal places and timezone-aware UTC timestamps (Requirement 7.4);
* **Property 31** — when an arbitrary subset of device entries is unreadable, non-numeric, or negative, each
  affected device starts at 0 kWh with a log record naming it and the reason, and **every other device restores
  its persisted total unchanged** (Requirement 7.6).

The isolation half of Property 31 is the interesting one. A restore that gave up on the whole document at the
first bad entry would still satisfy "the affected device starts at 0 kWh", so the assertion that carries weight
is the one about the devices that were *not* corrupted: their totals must come back exactly as persisted, no
matter how many of their neighbours are broken or which shape of breakage they carry.

## Why most of this runs through ``parse_state`` and ``serialize_state``

Both are pure and total -- ``parse_state`` never raises and never touches the filesystem -- so the generated
half of each property runs over text and costs nothing per example. The genuinely file-level claims (an atomic
flush is readable by a restore, and a restore emits one log record per rejected device) are asserted through
:class:`EnergyStateStore` against ``tmp_path``, where the file itself is the subject rather than incidental.

## Two generation details worth naming

Totals are drawn as whole microkilowatt-hours mapped to floats, which is exactly the shape the accumulator
produces: Requirement 7.1 holds a total to 6 decimal places, and :func:`serialize_state` rounds to the same
place, so the round trip is asserted with **exact** equality rather than a tolerance. Drawing arbitrary floats
would have forced a tolerance that hid a genuine loss of precision.

Baseline timestamps are drawn at mixed UTC offsets, not only at UTC. The renderer converts to UTC, so the
restored value is asserted to be the same *instant* and to carry a zero offset -- which is the actual contract,
and stronger than asserting the original object came back.

Generators are local to this module rather than shared, so this file stands alone.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final, NamedTuple

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from remote_energy_meter.diagnostics.logger import DEVICE_ID_FIELD
from remote_energy_meter.energy import KWH_DECIMAL_PLACES
from remote_energy_meter.persistence import CURRENT_SCHEMA_VERSION, DeviceEnergyState
from remote_energy_meter.state_store import (
    STATE_FILE_NAME,
    EnergyStateStore,
    RestoreRejection,
    build_state_file,
    parse_state,
    serialize_state,
)

#: Whole microkilowatt-hours: the resolution Requirement 7.1 keeps, so a round trip is exact.
_TOTAL_UNITS_PER_KWH: Final = 10**KWH_DECIMAL_PLACES

#: Requirement 6.14's upper guard, and so the widest baseline value any plugin can have produced.
MAX_WATTS: Final = 100_000.0

#: Fixed offsets rather than named zones: the renderer's contract is "the same instant, expressed as UTC", and a
#: fixed offset exercises that without dragging in ambiguous local times.
_OFFSETS: Final = [UTC, timezone(timedelta(hours=5, minutes=30)), timezone(timedelta(hours=-8))]


# --------------------------------------------------------------------------------------------------
# generators
# --------------------------------------------------------------------------------------------------


def device_identifiers() -> st.SearchStrategy[str]:
    """Device identifiers in the form the Configuration_Parser accepts (Requirement 2.12)."""
    return st.from_regex(r"[A-Za-z0-9_-]{1,64}", fullmatch=True)


def good_totals() -> st.SearchStrategy[float]:
    """Cumulative totals as the accumulator produces them: non-negative and held to 6 decimal places."""
    return st.integers(min_value=0, max_value=10**12).map(
        lambda units: round(units / _TOTAL_UNITS_PER_KWH, KWH_DECIMAL_PLACES)
    )


def baseline_watts() -> st.SearchStrategy[float]:
    """Baseline power values, across the range the publisher's guard admits."""
    return st.one_of(
        st.floats(min_value=0.0, max_value=MAX_WATTS, allow_nan=False, allow_infinity=False),
        st.sampled_from([0.0, 1e-9, 214.0, MAX_WATTS]),
    )


def moments() -> st.SearchStrategy[datetime]:
    """Timezone-aware timestamps at mixed offsets, spanning a century."""
    return st.datetimes(
        min_value=datetime(2000, 1, 1),  # noqa: DTZ001 - st.datetimes takes naive bounds
        max_value=datetime(2100, 1, 1),  # noqa: DTZ001 - st.datetimes takes naive bounds
        timezones=st.sampled_from(_OFFSETS),
    )


@st.composite
def device_states(draw: st.DrawFn) -> DeviceEnergyState:
    """One device's persisted state, with the baseline pair either wholly present or wholly absent."""
    total_kwh = draw(good_totals())
    if draw(st.booleans()):
        return DeviceEnergyState(
            total_kwh=total_kwh,
            baseline_watts=draw(baseline_watts()),
            baseline_at=draw(moments()),
        )
    return DeviceEnergyState(total_kwh=total_kwh)


def snapshots(min_size: int = 0, max_size: int = 8) -> st.SearchStrategy[dict[str, DeviceEnergyState]]:
    """An accumulator snapshot: device identifiers to states. Empty is included, and is a real case."""
    return st.dictionaries(
        keys=device_identifiers(),
        values=device_states(),
        min_size=min_size,
        max_size=max_size,
    )


@dataclass(frozen=True, slots=True)
class Corruption:
    """One device entry planted on disk, and what the restore must make of it.

    Attributes:
        entry: What sits in the document in place of a good entry.
        rejection: The reason the restore must report for it.
        kept_total_kwh: The total that must survive, or ``None`` when the device must reset to 0 kWh. Only
            :attr:`RestoreRejection.BASELINE_UNUSABLE` keeps a total -- the baseline is diagnostics and the
            total is real energy.
    """

    entry: object
    rejection: RestoreRejection
    kept_total_kwh: float | None


#: Baseline pairs that cannot be used: half present either way, a naive timestamp, text that is not a timestamp,
#: a timestamp that is not text, a watt value that is not a number, and a negative watt value.
_BROKEN_BASELINES: Final[list[tuple[object, object]]] = [
    (214.0, None),
    (None, "2025-01-01T00:00:00Z"),
    (214.0, "2025-01-01T00:00:00"),
    (214.0, "not-a-timestamp"),
    (214.0, ""),
    (214.0, 1_735_689_600),
    (214.0, True),
    ("214.0", "2025-01-01T00:00:00Z"),
    (-214.0, "2025-01-01T00:00:00Z"),
]


def malformed_entries() -> st.SearchStrategy[Corruption]:
    """Entries that are not objects at all, so they carry no fields to read."""
    return st.sampled_from([None, True, 0, 12.5, "12.5", "", [], [1, 2]]).map(
        lambda entry: Corruption(entry=entry, rejection=RestoreRejection.ENTRY_MALFORMED, kept_total_kwh=None)
    )


def total_missing_entries() -> st.SearchStrategy[Corruption]:
    """Objects that carry no ``total_kwh`` member, including one that carries only a baseline."""
    return st.sampled_from(
        [
            {},
            {"baseline_watts": 214.0, "baseline_at": "2025-01-01T00:00:00Z"},
            {"baseline_at": None},
            {"total": 1.5},
        ]
    ).map(lambda entry: Corruption(entry=entry, rejection=RestoreRejection.TOTAL_MISSING, kept_total_kwh=None))


def total_not_a_number_entries() -> st.SearchStrategy[Corruption]:
    """``total_kwh`` present but text, a boolean, a structure, or null."""
    return st.sampled_from(["1.5", "", None, True, False, [], {}, [1.0]]).map(
        lambda raw: Corruption(
            entry={"total_kwh": raw},
            rejection=RestoreRejection.TOTAL_NOT_A_NUMBER,
            kept_total_kwh=None,
        )
    )


def total_out_of_range_entries() -> st.SearchStrategy[Corruption]:
    """``total_kwh`` a number that cannot be a cumulative total: negative, or not finite."""
    return st.one_of(
        st.floats(min_value=-1e9, max_value=-1e-9, allow_nan=False, allow_infinity=False),
        st.integers(min_value=-(10**9), max_value=-1),
        st.sampled_from([-1e-6, math.nan, math.inf, -math.inf]),
    ).map(
        lambda raw: Corruption(
            entry={"total_kwh": raw},
            rejection=RestoreRejection.TOTAL_OUT_OF_RANGE,
            kept_total_kwh=None,
        )
    )


@st.composite
def baseline_unusable_entries(draw: st.DrawFn) -> Corruption:
    """A usable total alongside a baseline that is not, which must keep the total (Requirement 7.7)."""
    total_kwh = draw(good_totals())
    watts, at = draw(st.sampled_from(_BROKEN_BASELINES))
    return Corruption(
        entry={"total_kwh": total_kwh, "baseline_watts": watts, "baseline_at": at},
        rejection=RestoreRejection.BASELINE_UNUSABLE,
        kept_total_kwh=total_kwh,
    )


def corruptions() -> st.SearchStrategy[Corruption]:
    """Every shape of unusable entry Requirement 7.6 distinguishes."""
    return st.one_of(
        malformed_entries(),
        total_missing_entries(),
        total_not_a_number_entries(),
        total_out_of_range_entries(),
        baseline_unusable_entries(),
    )


class CorruptedDocument(NamedTuple):
    """A document text mixing good entries with planted bad ones, and what each half must produce."""

    text: str
    clean: dict[str, DeviceEnergyState]
    corrupt: dict[str, Corruption]


@st.composite
def corrupted_documents(draw: st.DrawFn) -> CorruptedDocument:
    """A well-formed document whose device entries are an arbitrary mix of good and unusable.

    The good half is rendered by the production serializer and the bad half is injected afterwards, so the clean
    entries are exactly what a real flush would have written. ``allow_nan`` is left on for the injection only,
    because a hand-edited or half-written document can contain the ``NaN`` and ``Infinity`` literals that
    :func:`serialize_state` refuses to produce.
    """
    identifiers = draw(st.lists(device_identifiers(), min_size=1, max_size=8, unique=True))

    clean: dict[str, DeviceEnergyState] = {}
    corrupt: dict[str, Corruption] = {}
    for device_id in identifiers:
        if draw(st.booleans()):
            corrupt[device_id] = draw(corruptions())
        else:
            clean[device_id] = draw(device_states())

    document: Any = json.loads(serialize_state(build_state_file(clean, draw(moments()))))
    for device_id, corruption in corrupt.items():
        document["devices"][device_id] = corruption.entry

    return CorruptedDocument(text=json.dumps(document, allow_nan=True), clean=clean, corrupt=corrupt)


# --------------------------------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------------------------------


class _CollectingHandler(logging.Handler):
    """Keeps every record, so the restore's own log records can be asserted over."""

    def __init__(self, records: list[logging.LogRecord]) -> None:
        super().__init__(level=logging.DEBUG)
        self._records = records

    def emit(self, record: logging.LogRecord) -> None:
        self._records.append(record)


@contextmanager
def capturing_logger(name: str) -> Iterator[tuple[logging.Logger, list[logging.LogRecord]]]:
    """An isolated logger whose records are collected rather than written anywhere."""
    records: list[logging.LogRecord] = []
    logger = logging.getLogger(name)
    saved_level, saved_propagate = logger.level, logger.propagate
    handler = _CollectingHandler(records)
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        yield logger, records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(saved_level)
        logger.propagate = saved_propagate


def assert_same_state(restored: DeviceEnergyState, original: DeviceEnergyState) -> None:
    """Assert a restored device state is field-by-field equal to the persisted one."""
    # Exact, not approximate: both sides are held to 6 decimal places, so a tolerance here would hide a
    # genuine loss of precision.
    assert restored.total_kwh == original.total_kwh
    assert restored.baseline_watts == original.baseline_watts
    assert restored.has_baseline is original.has_baseline

    if original.baseline_at is None:
        assert restored.baseline_at is None
    else:
        assert restored.baseline_at is not None
        # The same instant, expressed as UTC -- which is the contract, whatever offset it went in as.
        assert restored.baseline_at == original.baseline_at
        assert restored.baseline_at.utcoffset() == timedelta(0)
        assert restored.baseline_at.tzinfo is not None


# --------------------------------------------------------------------------------------------------
# the properties
# --------------------------------------------------------------------------------------------------


# Feature: remote-energy-meter, Property 30: For any mapping of device identifiers to cumulative energy totals
# and baseline timestamps, persisting the state and restoring it yields a state field-by-field equal to the
# original, including cumulative totals to 6 decimal places and timezone-aware UTC timestamps.
# Validates: Requirements 7.4
@settings(max_examples=300, deadline=None)
@given(snapshot=snapshots(), updated_at=moments())
def test_energy_state_survives_a_serialize_parse_round_trip(
    snapshot: dict[str, DeviceEnergyState],
    updated_at: datetime,
) -> None:
    """Requirement 7.4 over the pure halves of persistence, where an example costs nothing."""
    text = serialize_state(build_state_file(snapshot, updated_at))
    restored = parse_state(text)

    # --- the document itself came back whole ---
    assert restored.restored
    assert restored.failure is None
    assert restored.detail is None
    assert restored.rejections == ()
    assert restored.schema_version == CURRENT_SCHEMA_VERSION

    assert restored.updated_at == updated_at
    assert restored.updated_at is not None
    assert restored.updated_at.utcoffset() == timedelta(0)

    # --- every device, and only those devices, field-by-field equal ---
    assert set(restored.devices) == set(snapshot)
    for device_id, original in snapshot.items():
        assert_same_state(restored.devices[device_id], original)

    assert restored.totals == {device_id: state.total_kwh for device_id, state in snapshot.items()}
    assert restored.baselines == {
        device_id: state.baseline_at for device_id, state in snapshot.items() if state.baseline_at is not None
    }
    for device_id, original in snapshot.items():
        assert restored.baseline_at(device_id) == original.baseline_at

    # --- and the round trip is a fixed point: re-persisting the restored state is byte-identical ---
    assert serialize_state(build_state_file(dict(restored.devices), updated_at)) == text


# Feature: remote-energy-meter, Property 30: For any mapping of device identifiers to cumulative energy totals
# and baseline timestamps, persisting the state and restoring it yields a state field-by-field equal to the
# original, including cumulative totals to 6 decimal places and timezone-aware UTC timestamps.
# Validates: Requirements 7.4
@settings(
    max_examples=100,
    deadline=None,
    # The document is the subject of this property, so the temporary directory has to be real. Each example
    # replaces the same document, which is exactly what a running addon does every 300 seconds.
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(snapshot=snapshots(), updated_at=moments())
def test_energy_state_survives_a_flush_restore_round_trip(
    snapshot: dict[str, DeviceEnergyState],
    updated_at: datetime,
    tmp_path: Path,
) -> None:
    """Requirement 7.4 through the atomic write and the file read, not only through the text."""
    with capturing_logger("remote_energy_meter_property_30") as (logger, _records):
        store = EnergyStateStore(tmp_path / STATE_FILE_NAME, clock=lambda: updated_at, logger=logger)

        flushed = store.flush(snapshot)
        assert flushed.written
        assert flushed.device_count == len(snapshot)
        assert flushed.path == store.path

        restored = store.restore()

    assert restored.restored
    assert restored.failure is None
    assert restored.rejections == ()
    assert restored.schema_version == CURRENT_SCHEMA_VERSION
    assert restored.updated_at == updated_at

    assert set(restored.devices) == set(snapshot)
    for device_id, original in snapshot.items():
        assert_same_state(restored.devices[device_id], original)


# Feature: remote-energy-meter, Property 31: For any persisted state in which an arbitrary subset of device
# entries is unreadable, non-numeric, or negative, each affected device starts at 0 kWh with a log record
# naming that device identifier and the rejection reason, and every unaffected device restores its persisted
# cumulative total unchanged.
# Validates: Requirements 7.6
@settings(max_examples=300, deadline=None)
@given(case=corrupted_documents())
def test_corrupt_persisted_state_is_isolated_to_the_affected_devices(case: CorruptedDocument) -> None:
    """Requirement 7.6: one bad entry costs one device its total, and costs its neighbours nothing."""
    restored = parse_state(case.text)

    # --- a document with bad *entries* is still a readable document ---
    assert restored.restored
    assert restored.failure is None
    assert restored.schema_version == CURRENT_SCHEMA_VERSION
    assert set(restored.devices) == set(case.clean) | set(case.corrupt)

    # --- the isolation statement: every unaffected device restores exactly as persisted ---
    for device_id, original in case.clean.items():
        assert_same_state(restored.devices[device_id], original)
        assert restored.totals[device_id] == original.total_kwh

    # --- exactly one rejection per affected device, and none for any other ---
    rejected = {rejection.device_id: rejection for rejection in restored.rejections}
    assert len(restored.rejections) == len(case.corrupt)
    assert set(rejected) == set(case.corrupt)

    for device_id, corruption in case.corrupt.items():
        rejection = rejected[device_id]
        state = restored.devices[device_id]

        # --- named, with a stated reason ---
        assert rejection.rejection is corruption.rejection
        assert rejection.detail
        assert device_id in rejection.describe()
        assert corruption.rejection.value in rejection.describe()

        if corruption.kept_total_kwh is None:
            # --- an unreadable, non-numeric, or negative total starts that device at 0 kWh ---
            assert rejection.resets_total
            assert corruption.rejection.resets_total
            assert state == DeviceEnergyState.zeroed()
            assert state.total_kwh == 0.0
        else:
            # --- a total that is usable survives a baseline that is not: the baseline is diagnostics ---
            assert not rejection.resets_total
            assert state.total_kwh == corruption.kept_total_kwh
            assert state.baseline_at is None
            assert state.baseline_watts is None


# Feature: remote-energy-meter, Property 31: For any persisted state in which an arbitrary subset of device
# entries is unreadable, non-numeric, or negative, each affected device starts at 0 kWh with a log record
# naming that device identifier and the rejection reason, and every unaffected device restores its persisted
# cumulative total unchanged.
# Validates: Requirements 7.6
@settings(
    max_examples=100,
    deadline=None,
    # The log records come from EnergyStateStore.restore, which reads a real document.
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(case=corrupted_documents())
def test_every_rejected_device_is_named_in_exactly_one_log_record(case: CorruptedDocument, tmp_path: Path) -> None:
    """Requirement 7.6: the reason reaches the addon log naming the device it concerns."""
    path = tmp_path / STATE_FILE_NAME
    path.write_text(case.text, encoding="utf-8")

    with capturing_logger("remote_energy_meter_property_31") as (logger, records):
        restored = EnergyStateStore(path, logger=logger).restore()

    assert {rejection.device_id for rejection in restored.rejections} == set(case.corrupt)

    for rejection in restored.rejections:
        naming_it = [record for record in records if record.__dict__.get(DEVICE_ID_FIELD) == rejection.device_id]
        assert len(naming_it) == 1

        record = naming_it[0]
        assert record.levelno == logging.WARNING
        assert record.__dict__["reason"] == rejection.rejection.value
        assert record.__dict__["total_reset"] is rejection.resets_total
        assert rejection.detail in record.getMessage()

    # --- a clean device is never the subject of a rejection record ---
    named = {record.__dict__.get(DEVICE_ID_FIELD) for record in records}
    assert not (named & set(case.clean))
