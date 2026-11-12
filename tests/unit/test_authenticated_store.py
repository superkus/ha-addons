"""Integrity-protected documents under a separate, never-replaced state-authentication key."""

from __future__ import annotations

import errno
import json
import os
import pickle
import secrets
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from remote_energy_meter import authenticated_store
from remote_energy_meter.authenticated_store import (
    KEY_BYTES,
    KEY_DOCUMENT_NAME,
    AuthenticatedDocumentStore,
    StateAuthenticationKey,
    StateAuthenticationKeyStore,
)
from remote_energy_meter.protocol_v1 import JsonValue, encode_base64url, render_canonical_json
from remote_energy_meter.secure_store import SecureStoreError, StoreFault, ensure_private_directory

STORE_ID = "control-authority"
NAME = "authority.json"


@pytest.fixture
def key_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "keys"
    ensure_private_directory(directory)
    return directory


@pytest.fixture
def record_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "records"
    ensure_private_directory(directory)
    return directory


@pytest.fixture
def keys(key_dir: Path) -> StateAuthenticationKeyStore:
    store = StateAuthenticationKeyStore(key_dir)
    store.create()
    return store


def make_store(record_dir: Path, keys: StateAuthenticationKeyStore, **overrides: Any) -> AuthenticatedDocumentStore:
    options: dict[str, Any] = {"store_id": STORE_ID, "schema_version": 1, "max_bytes": 8192, "keys": keys}
    options.update(overrides)
    return AuthenticatedDocumentStore(record_dir, NAME, **options)


def fault_of(operation: Callable[[], object]) -> StoreFault:
    with pytest.raises(SecureStoreError) as caught:
        operation()
    return caught.value.fault


def read_envelope(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_bytes())
    assert isinstance(value, dict)
    return value


def write_envelope(path: Path, envelope: dict[str, Any]) -> None:
    """Replace a document with a canonical envelope of the attacker's choice, keeping mode 0600."""
    path.write_bytes(render_canonical_json(envelope, max_depth=16))
    path.chmod(0o600)


def rewrite_key_document(key_dir: Path, **changes: Any) -> None:
    path = key_dir / KEY_DOCUMENT_NAME
    envelope = read_envelope(path)
    envelope.update(changes)
    write_envelope(path, envelope)


def refuse_directory_fsync(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every directory fsync fail: the sync after the rename that makes a new document durable.

    The seam is the descriptor's file type, not a helper name, so it holds however the writer installs the document.
    """
    real_fsync = os.fsync

    def refusing(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", refusing)


def nested_body(depth: int) -> dict[str, JsonValue]:
    """An object nested exactly ``depth`` containers deep (the body itself counts as one)."""
    body: dict[str, JsonValue] = {}
    for _ in range(depth - 1):
        body = {"n": body}
    return body


# --------------------------------------------------------------------------------------------------
# the key
# --------------------------------------------------------------------------------------------------


def test_a_created_key_is_stored_owner_only_and_reloads_identically(key_dir: Path) -> None:
    store = StateAuthenticationKeyStore(key_dir)

    created = store.create()
    loaded = StateAuthenticationKeyStore(key_dir).load()

    assert stat.S_IMODE((key_dir / KEY_DOCUMENT_NAME).stat().st_mode) == 0o600
    assert store.path == key_dir / KEY_DOCUMENT_NAME
    assert created.tag(b"message") == loaded.tag(b"message")
    assert len(created.tag(b"message")) == 32


def test_two_created_keys_differ(tmp_path: Path) -> None:
    first_dir, second_dir = tmp_path / "a", tmp_path / "b"
    first = StateAuthenticationKeyStore(first_dir).create()
    second = StateAuthenticationKeyStore(second_dir).create()

    assert first.tag(b"m") != second.tag(b"m")


def test_a_missing_key_is_reported_and_never_regenerated(key_dir: Path) -> None:
    store = StateAuthenticationKeyStore(key_dir)

    assert fault_of(store.load) is StoreFault.KEY_MISSING
    assert not (key_dir / KEY_DOCUMENT_NAME).exists()


def test_an_existing_key_is_never_replaced(keys: StateAuthenticationKeyStore) -> None:
    before = keys.path.read_bytes()

    assert fault_of(keys.create) is StoreFault.KEY_EXISTS
    assert fault_of(StateAuthenticationKeyStore(keys.directory).create) is StoreFault.KEY_EXISTS
    assert keys.path.read_bytes() == before


def test_a_key_created_by_another_instance_while_generating_is_never_replaced(
    key_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SEC-1: a key that appears after the existence check but before the write must survive, not be overwritten."""
    first, second = StateAuthenticationKeyStore(key_dir), StateAuthenticationKeyStore(key_dir)
    real_token_bytes = secrets.token_bytes
    winner: list[StateAuthenticationKey] = []

    def race_then_generate(nbytes: int | None = None) -> bytes:
        if nbytes == KEY_BYTES and not winner:
            winner.append(StateAuthenticationKey(b"\x00" * KEY_BYTES))  # recursion guard, replaced below
            winner[0] = second.create()
        return real_token_bytes(nbytes)

    monkeypatch.setattr(secrets, "token_bytes", race_then_generate)
    fault = fault_of(first.create)
    monkeypatch.undo()

    assert fault is StoreFault.KEY_EXISTS
    assert StateAuthenticationKeyStore(key_dir).load().tag(b"m") == winner[0].tag(b"m")


def test_a_key_created_by_another_instance_while_writing_is_never_replaced(
    key_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SEC-1: the other key lands after the last existence check, just before the new key file is created."""
    first, second = StateAuthenticationKeyStore(key_dir), StateAuthenticationKeyStore(key_dir)
    real_open = os.open
    winner: list[StateAuthenticationKey] = []
    winner_bytes: list[bytes] = []

    def race_before_file_creation(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if flags & os.O_CREAT and not winner:
            winner.append(StateAuthenticationKey(b"\x00" * KEY_BYTES))  # recursion guard, replaced below
            winner[0] = second.create()
            winner_bytes.append(second.path.read_bytes())
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", race_before_file_creation)
    fault: StoreFault | None = None
    try:
        first.create()
    except SecureStoreError as error:
        fault = error.fault
    monkeypatch.undo()

    assert winner_bytes, "the competing key was never created"
    assert second.path.read_bytes() == winner_bytes[0], "the competing key was replaced"
    assert fault is StoreFault.KEY_EXISTS
    assert StateAuthenticationKeyStore(key_dir).load().tag(b"m") == winner[0].tag(b"m")


def test_a_key_whose_temporary_file_another_instance_removes_is_never_returned(
    key_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another instance's load deletes the in-flight temporary file before the link: the create fails closed."""
    first, second = StateAuthenticationKeyStore(key_dir), StateAuthenticationKeyStore(key_dir)
    real_link = os.link

    def cleanup_then_link(*args: Any, **kwargs: Any) -> None:
        assert fault_of(second.load) is StoreFault.KEY_MISSING  # its load removes the temporary file
        real_link(*args, **kwargs)

    monkeypatch.setattr(os, "link", cleanup_then_link)
    assert fault_of(first.create) is StoreFault.IO_ERROR
    monkeypatch.undo()

    assert list(key_dir.iterdir()) == []
    assert fault_of(StateAuthenticationKeyStore(key_dir).load) is StoreFault.KEY_MISSING


def test_an_unconfirmed_key_creation_is_never_followed_by_a_second_key(
    key_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DATA-3(a): after a create whose rename could not be synced, a retry finds the key; it never makes another."""
    store = StateAuthenticationKeyStore(key_dir)
    real_token_bytes = secrets.token_bytes
    generated: list[bytes] = []

    def recording(nbytes: int | None = None) -> bytes:
        value = real_token_bytes(nbytes)
        if nbytes == KEY_BYTES:
            generated.append(value)
        return value

    monkeypatch.setattr(secrets, "token_bytes", recording)
    refuse_directory_fsync(monkeypatch)
    assert fault_of(store.create) is StoreFault.IO_ERROR
    assert fault_of(store.create) is StoreFault.IO_ERROR  # still unconfirmed while the directory cannot sync
    monkeypatch.undo()
    monkeypatch.setattr(secrets, "token_bytes", recording)

    assert fault_of(store.create) is StoreFault.KEY_EXISTS
    assert fault_of(StateAuthenticationKeyStore(key_dir).create) is StoreFault.KEY_EXISTS
    assert len(generated) == 1
    expected = StateAuthenticationKey(generated[0]).tag(b"m")
    assert store.load().tag(b"m") == expected
    assert StateAuthenticationKeyStore(key_dir).load().tag(b"m") == expected


def test_a_key_removed_while_running_is_a_rollback_not_a_missing_first_key(keys: StateAuthenticationKeyStore) -> None:
    keys.load()
    keys.path.unlink()

    assert fault_of(keys.load) is StoreFault.GENERATION_ROLLBACK


@pytest.mark.parametrize(
    "record",
    [
        {"key": encode_base64url(b"\x01" * 31, exact_length=31)},
        {"key": encode_base64url(b"\x01" * 33, exact_length=33)},
        {"key": encode_base64url(b"\x01" * 32, exact_length=32) + "="},
        {"key": 1},
        {"key": encode_base64url(b"\x01" * 32, exact_length=32), "extra": True},
        {},
    ],
)
def test_a_malformed_key_document_fails_closed(key_dir: Path, record: dict[str, Any]) -> None:
    StateAuthenticationKeyStore(key_dir).create()
    rewrite_key_document(key_dir, record=record)

    assert fault_of(StateAuthenticationKeyStore(key_dir).load) is StoreFault.MALFORMED


def test_a_rewritten_key_document_fails_closed(key_dir: Path) -> None:
    """The key is written exactly once; any later generation means someone replaced it."""
    StateAuthenticationKeyStore(key_dir).create()
    rewrite_key_document(key_dir, generation=2)

    assert fault_of(StateAuthenticationKeyStore(key_dir).load) is StoreFault.MALFORMED


def test_an_insecure_key_document_fails_closed(keys: StateAuthenticationKeyStore) -> None:
    keys.path.chmod(0o644)

    assert fault_of(StateAuthenticationKeyStore(keys.directory).load) is StoreFault.FILE_INSECURE


def test_the_key_never_appears_in_repr_str_or_pickle() -> None:
    raw = b"\xab" * KEY_BYTES
    key = StateAuthenticationKey(raw)

    assert repr(key) == str(key) == "StateAuthenticationKey(<redacted>)"
    assert raw.hex() not in repr(key)
    with pytest.raises(TypeError, match="cannot be pickled"):
        pickle.dumps(key)


@pytest.mark.parametrize("raw", [b"", b"\x00" * 31, b"\x00" * 33, bytearray(32), "x" * 32])
def test_a_key_must_be_exactly_32_bytes(raw: Any) -> None:
    with pytest.raises(ValueError, match="exactly 32 bytes"):
        StateAuthenticationKey(raw)


def test_verify_accepts_only_the_exact_tag() -> None:
    key = StateAuthenticationKey(b"\x07" * KEY_BYTES)
    tag = key.tag(b"message")

    assert key.verify(b"message", tag)
    assert not key.verify(b"messagE", tag)
    assert not key.verify(b"message", tag[:-1] + bytes([tag[-1] ^ 1]))
    assert not key.verify(b"message", tag[:-1])


# --------------------------------------------------------------------------------------------------
# authenticated documents
# --------------------------------------------------------------------------------------------------


def test_a_document_that_was_never_written_loads_without_needing_a_key(record_dir: Path, key_dir: Path) -> None:
    assert make_store(record_dir, StateAuthenticationKeyStore(key_dir)).load() is None


def test_saving_requires_the_key_and_writes_nothing_without_it(record_dir: Path, key_dir: Path) -> None:
    store = make_store(record_dir, StateAuthenticationKeyStore(key_dir))

    assert fault_of(lambda: store.save({"generation_of_authority": 1})) is StoreFault.KEY_MISSING
    assert not store.path.exists()


def test_a_saved_body_reloads_exactly_in_a_fresh_instance(record_dir: Path, keys: StateAuthenticationKeyStore) -> None:
    body: dict[str, JsonValue] = {
        "integration_thumbprint": "eK92MFIAjsOk8dCHImhsEZ8vFwnfFsNB_8JOG9OXx2w",
        "generation": 1,
        "pins": [],
    }

    saved = make_store(record_dir, keys).save(body)
    loaded = make_store(record_dir, StateAuthenticationKeyStore(keys.directory)).load()

    assert (saved.generation, dict(saved.record)) == (1, body)
    assert loaded is not None
    assert (loaded.generation, dict(loaded.record)) == (1, body)
    assert set(read_envelope(saved_path(record_dir))["record"]) == {"body", "tag"}


def saved_path(record_dir: Path) -> Path:
    return record_dir / NAME


def test_generations_advance_by_one_and_each_is_tagged(record_dir: Path, keys: StateAuthenticationKeyStore) -> None:
    store = make_store(record_dir, keys)

    tags = [read_tag_after(store, {"value": value}) for value in range(3)]

    assert store.load() is not None
    assert len(set(tags)) == 3


def read_tag_after(store: AuthenticatedDocumentStore, body: dict[str, Any]) -> str:
    store.save(body)
    tag = read_envelope(store.path)["record"]["tag"]
    assert isinstance(tag, str)
    return tag


def test_an_existing_document_without_its_key_fails_closed(
    record_dir: Path, keys: StateAuthenticationKeyStore, tmp_path: Path
) -> None:
    make_store(record_dir, keys).save({"value": 1})

    other_keys = StateAuthenticationKeyStore(tmp_path / "absent-keys")

    assert fault_of(make_store(record_dir, other_keys).load) is StoreFault.KEY_MISSING


def test_a_body_altered_inside_a_valid_envelope_fails_authentication(
    record_dir: Path, keys: StateAuthenticationKeyStore
) -> None:
    """The case the bare versioned store cannot catch: a well-formed edit of a record value."""
    store = make_store(record_dir, keys)
    store.save({"control_key_generation": 1})
    envelope = read_envelope(store.path)
    envelope["record"]["body"]["control_key_generation"] = 2
    write_envelope(store.path, envelope)

    assert fault_of(make_store(record_dir, keys).load) is StoreFault.AUTHENTICATION_FAILED


def test_an_old_record_replayed_under_the_current_generation_fails_authentication(
    record_dir: Path, keys: StateAuthenticationKeyStore
) -> None:
    store = make_store(record_dir, keys)
    store.save({"revoked": False})
    old = read_envelope(store.path)
    store.save({"revoked": True})

    old["generation"] = 2  # the attacker bumps the old record to the current generation
    write_envelope(store.path, old)

    assert fault_of(make_store(record_dir, keys).load) is StoreFault.AUTHENTICATION_FAILED


def test_a_record_copied_from_another_store_fails_authentication(
    tmp_path: Path, keys: StateAuthenticationKeyStore
) -> None:
    """Moved into another store and relabelled with that store's id, the tag still names its origin."""
    source = AuthenticatedDocumentStore(
        tmp_path / "source", NAME, store_id="replay-records", schema_version=1, max_bytes=8192, keys=keys
    )
    source.save({"value": 1})
    envelope = read_envelope(source.path)
    target = make_store(tmp_path / "target", keys)
    assert target.load() is None
    envelope["store"] = STORE_ID
    write_envelope(target.path, envelope)

    assert fault_of(make_store(tmp_path / "target", keys).load) is StoreFault.AUTHENTICATION_FAILED


def test_a_record_written_under_another_key_fails_authentication(
    record_dir: Path, keys: StateAuthenticationKeyStore, tmp_path: Path
) -> None:
    make_store(record_dir, keys).save({"value": 1})
    other_keys = StateAuthenticationKeyStore(tmp_path / "other-keys")
    other_keys.create()

    assert fault_of(make_store(record_dir, other_keys).load) is StoreFault.AUTHENTICATION_FAILED


def test_an_altered_tag_fails_authentication(record_dir: Path, keys: StateAuthenticationKeyStore) -> None:
    store = make_store(record_dir, keys)
    store.save({"value": 1})
    envelope = read_envelope(store.path)
    envelope["record"]["tag"] = encode_base64url(b"\x00" * 32, exact_length=32)
    write_envelope(store.path, envelope)

    assert fault_of(make_store(record_dir, keys).load) is StoreFault.AUTHENTICATION_FAILED


@pytest.mark.parametrize(
    "record",
    [
        {"body": {}},
        {"tag": "x"},
        {"body": {}, "tag": "x", "extra": 1},
        {"body": [], "tag": encode_base64url(b"\x00" * 32, exact_length=32)},
        {"body": {}, "tag": 1},
        {"body": {}, "tag": "not-canonical-base64url"},
        {"body": {}, "tag": encode_base64url(b"\x00" * 31, exact_length=31)},
    ],
)
def test_a_record_that_is_not_a_body_and_a_canonical_tag_is_malformed(
    record_dir: Path, keys: StateAuthenticationKeyStore, record: dict[str, Any]
) -> None:
    store = make_store(record_dir, keys)
    store.save({"value": 1})
    envelope = read_envelope(store.path)
    envelope["record"] = record
    write_envelope(store.path, envelope)

    assert fault_of(make_store(record_dir, keys).load) is StoreFault.MALFORMED


def test_a_save_never_overwrites_a_tampered_document(record_dir: Path, keys: StateAuthenticationKeyStore) -> None:
    store = make_store(record_dir, keys)
    store.save({"value": 1})
    envelope = read_envelope(store.path)
    envelope["record"]["body"]["value"] = 2
    write_envelope(store.path, envelope)
    tampered = store.path.read_bytes()

    assert fault_of(lambda: make_store(record_dir, keys).save({"value": 3})) is StoreFault.AUTHENTICATION_FAILED
    assert store.path.read_bytes() == tampered


def test_an_unrenderable_body_is_refused_before_writing(record_dir: Path, keys: StateAuthenticationKeyStore) -> None:
    store = make_store(record_dir, keys, max_bytes=512)

    assert fault_of(lambda: store.save({"value": -1})) is StoreFault.MALFORMED
    assert fault_of(lambda: store.save({"value": "x" * 600})) is StoreFault.MALFORMED
    assert not store.path.exists()


def test_error_details_never_contain_the_body_or_the_key(record_dir: Path, keys: StateAuthenticationKeyStore) -> None:
    store = make_store(record_dir, keys)
    store.save({"secret_marker": "TOP-SECRET-MARKER"})
    envelope = read_envelope(store.path)
    envelope["record"]["body"]["secret_marker"] = "TOP-SECRET-MARKER-ALTERED"
    write_envelope(store.path, envelope)
    key_record = read_envelope(keys.path)["record"]["key"]

    with pytest.raises(SecureStoreError) as caught:
        make_store(record_dir, keys).load()

    message = str(caught.value)
    assert "TOP-SECRET-MARKER" not in message
    assert key_record not in message


def test_a_save_never_overwrites_a_record_tampered_with_during_the_save(
    record_dir: Path, keys: StateAuthenticationKeyStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """COR-1: a same-generation edit landing between the verifying read and the write is still caught."""
    store = make_store(record_dir, keys)
    store.save({"value": 1})
    tampered = read_envelope(store.path)
    tampered["record"]["body"]["value"] = 2  # generation 1 and the old tag kept: a valid envelope, a forged body
    tampered_bytes = render_canonical_json(tampered, max_depth=16)
    real_load = keys.load

    def load_then_tamper() -> StateAuthenticationKey:
        key = real_load()
        write_envelope(store.path, tampered)
        return key

    monkeypatch.setattr(keys, "load", load_then_tamper)

    assert fault_of(lambda: store.save({"value": 3})) is StoreFault.AUTHENTICATION_FAILED
    assert store.path.read_bytes() == tampered_bytes


def test_an_unconfirmed_save_is_accepted_and_verified_once_the_directory_syncs(
    record_dir: Path, keys: StateAuthenticationKeyStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """COR-2(a)/DATA-3(b): the VersionedDocumentStore unconfirmed-outcome rule holds through the authenticated layer."""
    store = make_store(record_dir, keys)
    store.save({"value": 1})
    refuse_directory_fsync(monkeypatch)

    assert fault_of(lambda: store.save({"value": 2})) is StoreFault.IO_ERROR
    assert fault_of(store.load) is StoreFault.IO_ERROR  # generation 2 is visible but not yet durable
    monkeypatch.undo()

    loaded = store.load()
    assert loaded is not None
    assert (loaded.generation, dict(loaded.record)) == (2, {"value": 2})
    assert store.save({"value": 3}).generation == 3
    reloaded = make_store(record_dir, StateAuthenticationKeyStore(keys.directory)).load()
    assert reloaded is not None
    assert (reloaded.generation, dict(reloaded.record)) == (3, {"value": 3})


def test_a_save_over_an_existing_record_whose_key_vanished_writes_nothing(
    record_dir: Path, keys: StateAuthenticationKeyStore
) -> None:
    """COR-2(b): a fresh process with the record but without its key must not overwrite the record."""
    make_store(record_dir, keys).save({"value": 1})
    keys.path.unlink()
    before = saved_path(record_dir).read_bytes()

    store = make_store(record_dir, StateAuthenticationKeyStore(keys.directory))

    assert fault_of(lambda: store.save({"value": 2})) is StoreFault.KEY_MISSING
    assert saved_path(record_dir).read_bytes() == before


def test_a_body_at_the_nesting_bound_round_trips_and_one_level_deeper_is_refused(
    record_dir: Path, keys: StateAuthenticationKeyStore, tmp_path: Path
) -> None:
    """COR-2(c): the body bound plus the two enclosing levels (envelope, record) fits the store's own bound."""
    deepest = nested_body(authenticated_store._BODY_MAX_DEPTH)

    saved = make_store(record_dir, keys).save(deepest)
    loaded = make_store(record_dir, StateAuthenticationKeyStore(keys.directory)).load()

    assert saved.generation == 1
    assert loaded is not None
    assert dict(loaded.record) == deepest

    too_deep_store = make_store(tmp_path / "too-deep", keys)
    too_deep = nested_body(authenticated_store._BODY_MAX_DEPTH + 1)
    assert fault_of(lambda: too_deep_store.save(too_deep)) is StoreFault.MALFORMED
    assert not too_deep_store.path.exists()


def test_a_record_at_the_last_generation_cannot_be_advanced_and_is_kept(
    record_dir: Path, keys: StateAuthenticationKeyStore
) -> None:
    """COR-2(d): GENERATION_EXHAUSTED from the versioned layer reaches the caller of save()."""
    last = 2**64 - 1
    body: dict[str, JsonValue] = {"value": 1}
    store = make_store(record_dir, keys)
    store.save(body)
    envelope = read_envelope(store.path)
    tag = keys.load().tag(store._message(last, body))
    envelope["generation"] = last
    envelope["record"]["tag"] = encode_base64url(tag, exact_length=len(tag))
    write_envelope(store.path, envelope)
    planted = store.path.read_bytes()

    fresh = make_store(record_dir, StateAuthenticationKeyStore(keys.directory))
    loaded = fresh.load()
    assert loaded is not None
    assert (loaded.generation, dict(loaded.record)) == (last, body)  # the planted record is authentic

    assert fault_of(lambda: fresh.save({"value": 2})) is StoreFault.GENERATION_EXHAUSTED
    assert store.path.read_bytes() == planted


def test_a_document_cannot_share_the_key_documents_path(key_dir: Path) -> None:
    keys = StateAuthenticationKeyStore(key_dir)

    with pytest.raises(ValueError, match="key document"):
        AuthenticatedDocumentStore(
            key_dir, KEY_DOCUMENT_NAME, store_id=STORE_ID, schema_version=1, max_bytes=512, keys=keys
        )


@pytest.mark.parametrize("max_bytes", [0, 4 << 20 | 1])
def test_the_size_cap_is_validated(record_dir: Path, keys: StateAuthenticationKeyStore, max_bytes: int) -> None:
    with pytest.raises(ValueError, match="max_bytes"):
        make_store(record_dir, keys, max_bytes=max_bytes)


def test_the_module_exports_its_public_names() -> None:
    for name in authenticated_store.__all__:
        assert hasattr(authenticated_store, name)


def test_create_after_seeing_no_key_reports_a_key_created_meanwhile_as_existing(key_dir: Path) -> None:
    """An instance that saw "no key", then another writer creates one: create() reports KEY_EXISTS, replaces nothing."""
    late = StateAuthenticationKeyStore(key_dir)
    assert fault_of(late.load) is StoreFault.KEY_MISSING
    StateAuthenticationKeyStore(key_dir).create()
    winner = (key_dir / KEY_DOCUMENT_NAME).read_bytes()

    assert fault_of(late.create) is StoreFault.KEY_EXISTS
    assert (key_dir / KEY_DOCUMENT_NAME).read_bytes() == winner
