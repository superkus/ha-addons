"""Properties of the fail-closed versioned store: exact round trips, and every partial write is detected."""

from __future__ import annotations

import tempfile
from decimal import Decimal
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from remote_energy_meter.protocol_v1 import JsonValue
from remote_energy_meter.secure_store import SecureStoreError, StoreFault, VersionedDocumentStore

_SCALARS: st.SearchStrategy[JsonValue] = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=0, max_value=2**64 - 1),
    st.builds(
        lambda whole, fraction: Decimal(f"{whole}.{str(fraction).zfill(6).rstrip('0')}"),
        st.integers(min_value=0, max_value=10**9),
        st.integers(min_value=1, max_value=999999),
    ),
    st.text(max_size=12),
)
_KEYS = st.text(alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x7E), max_size=8)
_RECORDS: st.SearchStrategy[dict[str, JsonValue]] = st.dictionaries(
    _KEYS,
    st.recursive(
        _SCALARS,
        lambda children: st.one_of(st.lists(children, max_size=3), st.dictionaries(_KEYS, children, max_size=3)),
        max_leaves=8,
    ),
    max_size=5,
)


def _store(directory: Path) -> VersionedDocumentStore:
    return VersionedDocumentStore(directory, "document.json", store_id="prop-store", schema_version=1, max_bytes=65536)


@given(st.lists(_RECORDS, min_size=1, max_size=4))
@settings(max_examples=100, deadline=None)
def test_every_saved_record_reloads_exactly_in_a_fresh_instance(records: list[dict[str, JsonValue]]) -> None:
    with tempfile.TemporaryDirectory() as root:
        directory = Path(root) / "secure"
        writer = _store(directory)
        for generation, record in enumerate(records, start=1):
            saved = writer.save(record)
            assert saved.generation == generation
            assert dict(saved.record) == record

        loaded = _store(directory).load()

        assert loaded is not None
        assert loaded.generation == len(records)
        assert dict(loaded.record) == records[-1]


@given(_RECORDS, st.data())
@settings(max_examples=100, deadline=None)
def test_every_truncation_of_a_saved_document_fails_closed(record: dict[str, JsonValue], data: st.DataObject) -> None:
    """A torn write can only ever leave a strict prefix; no prefix of the envelope is a valid envelope."""
    with tempfile.TemporaryDirectory() as root:
        directory = Path(root) / "secure"
        store = _store(directory)
        store.save(record)
        complete = store.path.read_bytes()
        cut = data.draw(st.integers(min_value=0, max_value=len(complete) - 1))
        store.path.write_bytes(complete[:cut])

        try:
            _store(directory).load()
        except SecureStoreError as error:
            assert error.fault is StoreFault.MALFORMED
        else:
            raise AssertionError(f"a {cut}-byte prefix of a {len(complete)}-byte document loaded")
