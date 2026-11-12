"""Property: with an integrity tag, no single-byte change anywhere in a stored document loads."""

from __future__ import annotations

import tempfile
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from remote_energy_meter.authenticated_store import AuthenticatedDocumentStore, StateAuthenticationKeyStore
from remote_energy_meter.protocol_v1 import JsonValue
from remote_energy_meter.secure_store import SecureStoreError

_KEYS = st.text(alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x7E), max_size=6)
_BODIES: st.SearchStrategy[dict[str, JsonValue]] = st.dictionaries(
    _KEYS,
    st.one_of(st.none(), st.booleans(), st.integers(min_value=0, max_value=2**64 - 1), st.text(max_size=8)),
    max_size=3,
)


def _store(root: Path) -> AuthenticatedDocumentStore:
    return AuthenticatedDocumentStore(
        root / "records",
        "document.json",
        store_id="prop-store",
        schema_version=1,
        max_bytes=4096,
        keys=StateAuthenticationKeyStore(root / "keys"),
    )


@given(_BODIES, st.integers(min_value=1, max_value=3), st.sampled_from([0x01, 0x20, 0x80]))
@settings(max_examples=40, deadline=None)
def test_every_single_byte_change_of_a_stored_document_fails_closed(
    body: dict[str, JsonValue], saves: int, flip: int
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        StateAuthenticationKeyStore(root / "keys").create()
        writer = _store(root)
        for _ in range(saves):
            writer.save(body)
        original = writer.path.read_bytes()

        accepted: list[int] = []
        for index in range(len(original)):
            mutated = bytearray(original)
            mutated[index] ^= flip
            writer.path.write_bytes(bytes(mutated))
            try:
                _store(root).load()
            except SecureStoreError:
                continue
            accepted.append(index)
        writer.path.write_bytes(original)

        assert accepted == [], f"single-byte changes at {accepted} loaded"
        loaded = _store(root).load()
        assert loaded is not None
        assert dict(loaded.record) == body
