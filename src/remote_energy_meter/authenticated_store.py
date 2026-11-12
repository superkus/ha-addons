"""Integrity-protected security documents under a separate state-authentication key.

:class:`remote_energy_meter.secure_store.VersionedDocumentStore` rejects insecure, malformed, and rolled-back
documents, but it accepts any well-formed envelope, so an edit that keeps a valid envelope loads as written. Stores
whose records carry authority (the completed bootstrap-confirm result, exact-retry results, consumed-credential
tombstones) need tamper evidence as well. This module adds it:

* :class:`StateAuthenticationKeyStore` keeps one random 32-byte key in its own owner-only document, apart from the
  records it protects (requirement 7.5a: "a dedicated random core state-authentication key stored separately"). The key
  is created only by an explicit :meth:`~StateAuthenticationKeyStore.create`, exclusively, so an existing key is never
  replaced, even by a concurrent creator. ``create`` checks only the key document: calling it only while no protected
  state exists is the caller's obligation, which the later bootstrap layer enforces. A missing key is
  :attr:`~remote_energy_meter.secure_store.StoreFault.KEY_MISSING`, never silently regenerated, because a fresh key
  would make every existing record unverifiable and hide what happened: if a key is lost while records exist and a new
  one is created anyway, those records fail as ``AUTHENTICATION_FAILED``. A key that one key store instance has already
  read and that then vanishes, or is rewritten at a higher generation, fails as ``GENERATION_ROLLBACK`` or
  ``CONCURRENT_MODIFICATION`` through that instance, not as ``KEY_MISSING``; a fresh instance reports ``KEY_MISSING``
  or ``MALFORMED`` for the same states. Likewise, a key another writer creates after an instance saw none fails as
  ``CONCURRENT_MODIFICATION`` on that instance's ``load`` (``KEY_EXISTS`` on its ``create``); a fresh instance loads it.
* :class:`AuthenticatedDocumentStore` stores ``{"body": <record>, "tag": <HMAC>}`` as the record of a versioned
  envelope. The tag is HMAC-SHA-256 under that key over a ``REM-TRANSCRIPT`` binding the store id, schema version,
  generation, and the canonical body bytes, so a record cannot be altered, copied into another store, or relabelled
  with another generation without the key. Before a save overwrites the current document, its tag is verified, so a
  save never erases evidence of tampering.

What the tag cannot prove is freshness. It gives authenticity and binding to the store, schema version, and
generation, and the key never rotates, so every record it ever tagged stays authentic. An older record restored on its
own, or a record deleted, while no instance is running therefore loads after a restart as that older generation, or as
never written (``None``); so does a coordinated restore of both the records and the key from one backup. Restore checks
against independently held state and the post-restore quarantine own that case (see
:mod:`remote_energy_meter.secure_store`).

The key is ordinary process memory: Python cannot guarantee it is erased. It never appears in ``repr``, logs, or
error details.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from remote_energy_meter.protocol_v1 import (
    JsonValue,
    ProtocolViolation,
    decode_base64url,
    encode_base64url,
    encode_transcript,
    render_canonical_json,
)
from remote_energy_meter.secure_store import (
    MAX_DOCUMENT_BYTES_CEILING,
    SecureStoreError,
    StoredDocument,
    StoreFault,
    VersionedDocumentStore,
)

__all__ = [
    "KEY_BYTES",
    "KEY_DOCUMENT_NAME",
    "TAG_BYTES",
    "AuthenticatedDocumentStore",
    "StateAuthenticationKey",
    "StateAuthenticationKeyStore",
]

KEY_BYTES: Final = 32
"""Length of the state-authentication key: 256 bits, the SHA-256 output size recommended for HMAC-SHA-256 keys."""

TAG_BYTES: Final = 32
"""Length of an HMAC-SHA-256 tag."""

KEY_DOCUMENT_NAME: Final = "state-authentication-key.json"
"""File name of the key document inside its directory."""

_KEY_STORE_ID: Final = "state-authentication-key"
_KEY_SCHEMA_VERSION: Final = 1
_KEY_DOCUMENT_MAX_BYTES: Final = 256
_TAG_PURPOSE: Final = "rem-store-record-v1"
#: The body sits two levels below the envelope root (envelope -> record -> body), inside the store's 16-level bound.
_BODY_MAX_DEPTH: Final = 14
#: Fixed transcript overhead beyond the body: prefix, purpose, four field headers, store id, and two uint64 values.
_TRANSCRIPT_OVERHEAD: Final = 256
_RECORD_KEYS: Final = frozenset({"body", "tag"})


class StateAuthenticationKey:
    """The 32-byte key that tags authenticated records. Its value never appears in ``repr`` or ``str``."""

    __slots__ = ("_key",)

    def __init__(self, key: bytes) -> None:
        if type(key) is not bytes or len(key) != KEY_BYTES:
            raise ValueError(f"a state-authentication key is exactly {KEY_BYTES} bytes")
        self._key = key

    def tag(self, message: bytes) -> bytes:
        """Return the HMAC-SHA-256 tag of ``message``."""
        return hmac.new(self._key, message, hashlib.sha256).digest()

    def verify(self, message: bytes, tag: bytes) -> bool:
        """Return whether ``tag`` is the tag of ``message``, compared in constant time."""
        return hmac.compare_digest(self.tag(message), tag)

    def __repr__(self) -> str:
        return "StateAuthenticationKey(<redacted>)"

    __str__ = __repr__

    def __reduce__(self) -> str | tuple[object, ...]:
        # Pickling would serialize the key; nothing in the add-on needs it.
        raise TypeError("a state-authentication key cannot be pickled")


class StateAuthenticationKeyStore:
    """The owner-only document holding the state-authentication key.

    Args:
        directory: Its store directory. Keep it apart from the records it protects where possible; at minimum it is a
            separate document.
    """

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._document = VersionedDocumentStore(
            directory,
            KEY_DOCUMENT_NAME,
            store_id=_KEY_STORE_ID,
            schema_version=_KEY_SCHEMA_VERSION,
            max_bytes=_KEY_DOCUMENT_MAX_BYTES,
        )
        self._lock = threading.Lock()

    @property
    def directory(self) -> Path:
        """The key's store directory."""
        return self._directory

    @property
    def path(self) -> Path:
        """The key document's path."""
        return self._document.path

    def load(self) -> StateAuthenticationKey:
        """Read and verify the key.

        The document is re-read on every call, so a key removed, rolled back, or replaced while the add-on runs is
        detected rather than masked by a cached copy.

        Raises:
            SecureStoreError: ``KEY_MISSING`` when no key exists and this instance has not read one before;
                ``GENERATION_ROLLBACK`` when a key this instance already read has disappeared;
                ``CONCURRENT_MODIFICATION`` when it was rewritten at a higher generation since (a fresh instance reports
                that as ``MALFORMED``) or when a key appeared after this instance saw none (a fresh instance loads it);
                ``MALFORMED`` when the document is not exactly one canonical 32-byte key written once; any other fault
                of the underlying document.
        """
        with self._lock:
            document = self._document.load()
            if document is None:
                raise SecureStoreError(StoreFault.KEY_MISSING, f"{self.path} does not exist")
            return self._decode(document)

    def create(self) -> StateAuthenticationKey:
        """Generate, store, and return a new key, exclusively: an existing key is never replaced.

        This checks only the key document. Call it only when no state protected by a key exists yet; that is the
        caller's obligation (records left from a lost key would then fail as ``AUTHENTICATION_FAILED``).

        An instance that saw no key, or whose creation lost the race to another writer, keeps reporting
        ``CONCURRENT_MODIFICATION`` on :meth:`load`, because it never read the other writer's key; read the key through
        a fresh instance.

        Raises:
            SecureStoreError: ``KEY_EXISTS`` when a key document already exists, including one another writer created
                after this instance last saw none or while this call runs (it is never replaced); any fault of the
                underlying document, including ``IO_ERROR`` from the write.
        """
        with self._lock:
            # A key this instance has not read (another writer's, or one rewritten after this instance read it), whether
            # the pre-check sees it or the exclusive create finds it, is an existing key: report it, replace nothing.
            try:
                existing = self._document.load()
            except SecureStoreError as error:
                raise self._existing_key_or(error) from None
            if existing is not None:
                raise SecureStoreError(StoreFault.KEY_EXISTS, f"{self.path} already exists and is never replaced")
            key = secrets.token_bytes(KEY_BYTES)
            try:
                # Exclusive: a key another writer installed after the check above is never overwritten.
                self._document.create({"key": encode_base64url(key, exact_length=KEY_BYTES)})
            except SecureStoreError as error:
                raise self._existing_key_or(error) from None
            return StateAuthenticationKey(key)

    def _existing_key_or(self, error: SecureStoreError) -> SecureStoreError:
        """Map another writer's key (``CONCURRENT_MODIFICATION``) to ``KEY_EXISTS``; return any other fault as is."""
        if error.fault is not StoreFault.CONCURRENT_MODIFICATION:
            return error
        detail = f"{self.path} was created by another writer and is never replaced"
        return SecureStoreError(StoreFault.KEY_EXISTS, detail)

    def _decode(self, document: StoredDocument) -> StateAuthenticationKey:
        # Written exactly once by create(): a later generation means it was rewritten, which never happens legitimately.
        if document.generation != 1:
            detail = f"{self.path} was rewritten (generation {document.generation})"
            raise SecureStoreError(StoreFault.MALFORMED, detail)
        record = document.record
        encoded = record.get("key")
        if set(record) != {"key"} or type(encoded) is not str:
            raise SecureStoreError(StoreFault.MALFORMED, f"{self.path} is not a key record")
        try:
            return StateAuthenticationKey(decode_base64url(encoded, exact_length=KEY_BYTES))
        except ProtocolViolation:
            raise SecureStoreError(
                StoreFault.MALFORMED, f"{self.path} does not hold a canonical {KEY_BYTES}-byte key"
            ) from None


class AuthenticatedDocumentStore:
    """One integrity-protected, versioned security document.

    The same contract as :class:`~remote_energy_meter.secure_store.VersionedDocumentStore` (one writer, generation +1
    per save, fail closed), with every record tagged under the key in ``keys``. A document that does not exist yet
    needs no key to load; reading an existing document or writing any document requires the key.

    ``store_id`` must be unique among the stores that share one key: the tag binds the store id, not the file name, so a
    record copied between two stores with the same id and schema version would verify in either.

    Args:
        directory: Store directory of the document.
        name: Document file name.
        store_id: Identifier bound into the envelope and the tag.
        schema_version: The only schema version this release reads and writes, bound into the tag.
        max_bytes: Cap on the document size.
        keys: Where the state-authentication key lives.
    """

    def __init__(
        self,
        directory: Path,
        name: str,
        *,
        store_id: str,
        schema_version: int,
        max_bytes: int,
        keys: StateAuthenticationKeyStore,
    ) -> None:
        if name == KEY_DOCUMENT_NAME and directory == keys.directory:
            raise ValueError("an authenticated document cannot share the key document's path")
        if not 0 < max_bytes <= MAX_DOCUMENT_BYTES_CEILING:
            raise ValueError(f"max_bytes must be between 1 and {MAX_DOCUMENT_BYTES_CEILING}")
        self._document = VersionedDocumentStore(
            directory, name, store_id=store_id, schema_version=schema_version, max_bytes=max_bytes
        )
        self._store_id = store_id
        self._schema_version = schema_version
        self._max_bytes = max_bytes
        self._keys = keys
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        """The document's path."""
        return self._document.path

    def load(self) -> StoredDocument | None:
        """Read and verify the document and its tag; ``None`` when it has never been written.

        Raises:
            SecureStoreError: Any fault of the underlying document; any fault of
                :meth:`StateAuthenticationKeyStore.load` when the document exists (``KEY_MISSING`` when its key does
                not, ``GENERATION_ROLLBACK`` or ``CONCURRENT_MODIFICATION`` when a key already read through that key
                store has vanished or been rewritten); ``MALFORMED`` when the record is not exactly a body and a tag;
                ``AUTHENTICATION_FAILED`` when the tag does not verify.
        """
        with self._lock:
            return self._load_locked()

    def save(self, body: Mapping[str, JsonValue]) -> StoredDocument:
        """Tag ``body`` and write it as the next generation; return the stored body.

        The document this save replaces, if any, is verified under the store lock as it is re-read for the write, so a
        save never overwrites a tampered record. The key is loaded first, so a missing key fails as ``KEY_MISSING``
        with nothing written, whether or not a record exists. The same unconfirmed-outcome rule as
        :meth:`VersionedDocumentStore.save` applies after an ``IO_ERROR``.

        Raises:
            SecureStoreError: Any fault of :meth:`StateAuthenticationKeyStore.load` (``KEY_MISSING`` when the key does
                not exist, ``GENERATION_ROLLBACK`` or ``CONCURRENT_MODIFICATION`` when a key already read through that
                key store has vanished or been rewritten); any other fault from :meth:`load`; ``MALFORMED`` when
                ``body`` is not canonical-JSON renderable within the cap; ``GENERATION_EXHAUSTED``; ``IO_ERROR`` from
                the write.
        """
        with self._lock:
            key = self._keys.load()

            def build(generation: int, current: StoredDocument | None) -> Mapping[str, JsonValue]:
                if current is not None:
                    self._verify(current, key)
                tag = key.tag(self._message(generation, body))
                return {"body": dict(body), "tag": encode_base64url(tag, exact_length=TAG_BYTES)}

            saved = self._document.save_with(build)
            stored_body, _tag = self._split(saved)
            return StoredDocument(generation=saved.generation, record=stored_body)

    # -- internals --------------------------------------------------------------------------------

    def _load_locked(self) -> StoredDocument | None:
        document = self._document.load()
        if document is None:
            return None
        return self._verify(document, self._keys.load())

    def _verify(self, document: StoredDocument, key: StateAuthenticationKey) -> StoredDocument:
        """Return the body of ``document`` once its tag verifies under ``key``, or fail closed."""
        body, encoded_tag = self._split(document)
        try:
            tag = decode_base64url(encoded_tag, exact_length=TAG_BYTES)
        except ProtocolViolation:
            raise SecureStoreError(StoreFault.MALFORMED, f"{self.path} has a non-canonical tag") from None
        if not key.verify(self._message(document.generation, body), tag):
            raise SecureStoreError(
                StoreFault.AUTHENTICATION_FAILED,
                f"{self.path} generation {document.generation} does not verify under the state-authentication key",
            )
        return StoredDocument(generation=document.generation, record=body)

    def _split(self, document: StoredDocument) -> tuple[Mapping[str, JsonValue], str]:
        """Return the body and the encoded tag of an authenticated record, or fail as ``MALFORMED``."""
        record = document.record
        body, tag = record.get("body"), record.get("tag")
        if set(record) != _RECORD_KEYS or type(body) is not dict or type(tag) is not str:
            raise SecureStoreError(StoreFault.MALFORMED, f"{self.path} is not an authenticated record")
        return body, tag

    def _message(self, generation: int, body: Mapping[str, JsonValue]) -> bytes:
        """The exact bytes the tag covers: store, schema version, generation, and canonical body, domain-separated."""
        try:
            body_bytes = render_canonical_json(dict(body), max_depth=_BODY_MAX_DEPTH, max_length=self._max_bytes)
        except ProtocolViolation:
            raise SecureStoreError(
                StoreFault.MALFORMED, f"the record for {self.path} is not canonical JSON within {self._max_bytes} bytes"
            ) from None
        return encode_transcript(
            _TAG_PURPOSE,
            [
                ("store", self._store_id.encode("ascii")),
                ("schema_version", self._schema_version.to_bytes(8, "big")),
                ("generation", generation.to_bytes(8, "big")),
                ("body", body_bytes),
            ],
            max_length=len(body_bytes) + _TRANSCRIPT_OVERHEAD,
        )
