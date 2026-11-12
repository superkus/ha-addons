"""Fail-closed, owner-only storage for the add-on's security state.

The reporter and control stores (PKI, roster, tokens, revocations, control authority, replay and result records) keep
secrets and authority under ``/data``. Unlike the energy store, which resets a bad entry to zero so metering keeps
running, these stores **fail closed**: any doubt about a document's integrity stops the feature that depends on it.
Nothing here repairs, recreates, or silently ignores state.

Three layers:

* :func:`ensure_private_directory` creates or verifies a store directory: a real directory (not a symlink) owned by the
  effective user with mode exactly ``0700``. A directory with any other mode is reported, never ``chmod``-ed back,
  because a widened mode is evidence the contents can no longer be trusted.
* :func:`read_private_file` / :func:`write_private_file` read and replace one document inside such a directory. Reads
  open the name relative to the verified directory with ``O_NOFOLLOW | O_NONBLOCK`` and check the open descriptor, so
  a symlink, FIFO, device, foreign owner, or any mode other than ``0600`` is rejected, and the size is capped before
  the content is read. Writes use :func:`remote_energy_meter.atomic_file.write_atomically` with
  ``require_directory_sync=True``, so a rename that cannot be confirmed durable fails the write.
* :class:`VersionedDocumentStore` stores one canonical-JSON envelope ``{"generation", "record", "schema_version",
  "store"}``. Every successful save increments ``generation`` by exactly one. A store instance remembers the last
  generation it loaded or wrote and fails closed if the document on disk later moves backwards, jumps ahead, or
  disappears — a rollback or a second writer.

What a single document cannot do is detect the rollback of a *whole* restored backup: the restored file is internally
consistent. Restore checks against independently held state and the post-restore quarantine own that case. Nor does
this layer authenticate content: an edit that keeps a valid envelope (for example a changed digit in a record value)
loads as written. Stores whose records need tamper evidence add an integrity tag on top of this layer.

Errors are :class:`SecureStoreError` with a :class:`StoreFault`. Their ``detail`` names the store and file and the
violated rule only; it never includes document content, so it is safe to log.
"""

from __future__ import annotations

import errno
import os
import re
import stat
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from remote_energy_meter.atomic_file import FILE_MODE, TEMP_SUFFIX, write_atomically
from remote_energy_meter.protocol_v1 import JsonValue, ProtocolViolation, parse_canonical_json, render_canonical_json

__all__ = [
    "DIRECTORY_MODE",
    "FILE_MODE",
    "MAX_DOCUMENT_BYTES_CEILING",
    "SecureStoreError",
    "StoreFault",
    "StoredDocument",
    "VersionedDocumentStore",
    "ensure_private_directory",
    "read_private_file",
    "write_private_file",
]

DIRECTORY_MODE: Final = 0o700
"""Exact permission bits of a store directory."""

MAX_DOCUMENT_BYTES_CEILING: Final = 4 << 20
"""Largest per-document cap a caller may choose. The biggest planned store (1,000 roster identities) is far below."""

MAX_GENERATION: Final = 2**64 - 1
"""Generations are uint64, matching the protocol's generation fields."""

#: Envelope plus a record nested up to this depth. Records are flat or shallow; deep nesting is a mistake.
_MAX_JSON_DEPTH: Final = 16
_ENVELOPE_KEYS: Final = frozenset({"generation", "record", "schema_version", "store"})
_NAME: Final = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
_READ_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_DIRECTORY_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0)


class StoreFault(StrEnum):
    """Why a security document could not be trusted. Every member fails closed."""

    DIRECTORY_INSECURE = "directory_insecure"
    """The store directory is missing its parent, is a symlink or not a directory, has a foreign owner, or a mode
    other than ``0700``."""

    FILE_INSECURE = "file_insecure"
    """The document is a symlink, not a regular file, has a foreign owner, or a mode other than ``0600``."""

    OVERSIZE = "oversize"
    """The document is larger than the store's cap."""

    MALFORMED = "malformed"
    """The document is not canonical JSON, or not an envelope of exactly the expected shape and types."""

    FOREIGN_SCHEMA = "foreign_schema"
    """The envelope declares a schema version this release does not read."""

    WRONG_STORE = "wrong_store"
    """The envelope belongs to another store, e.g. a document copied into the wrong file."""

    GENERATION_ROLLBACK = "generation_rollback"
    """The document's generation is lower than one this process already saw, or the document vanished."""

    CONCURRENT_MODIFICATION = "concurrent_modification"
    """The document's generation is higher than any this process wrote: another writer exists."""

    GENERATION_EXHAUSTED = "generation_exhausted"
    """The uint64 generation cannot advance further."""

    IO_ERROR = "io_error"
    """The filesystem failed. A write with this fault may or may not have taken effect; see
    :meth:`VersionedDocumentStore.save`."""


class SecureStoreError(Exception):
    """A security document failed a fail-closed check. ``detail`` is safe to log: it never contains content."""

    def __init__(self, fault: StoreFault, detail: str) -> None:
        super().__init__(f"{fault.value}: {detail}")
        self.fault = fault
        self.detail = detail


# --------------------------------------------------------------------------------------------------
# directories and files
# --------------------------------------------------------------------------------------------------


def _check_name(name: str) -> None:
    # One plain path component: no separators, no dot names, never a temporary-file name.
    if not _NAME.fullmatch(name) or name.endswith(TEMP_SUFFIX):
        raise ValueError(f"store document name {name!r} is not a plain lowercase file name")


def _check_directory_status(directory: Path, status: os.stat_result) -> None:
    if not stat.S_ISDIR(status.st_mode):
        raise SecureStoreError(StoreFault.DIRECTORY_INSECURE, f"{directory} is not a directory")
    if status.st_uid != os.geteuid():
        raise SecureStoreError(StoreFault.DIRECTORY_INSECURE, f"{directory} is not owned by the add-on user")
    if stat.S_IMODE(status.st_mode) != DIRECTORY_MODE:
        raise SecureStoreError(
            StoreFault.DIRECTORY_INSECURE,
            f"{directory} has mode {stat.S_IMODE(status.st_mode):04o}, expected {DIRECTORY_MODE:04o}",
        )


def ensure_private_directory(directory: Path) -> None:
    """Create ``directory`` with mode ``0700`` if it is absent, then verify it.

    Only the last path component is created; its parent (for example ``/data``) must already exist. An existing
    directory is verified exactly as found and never modified.

    Raises:
        SecureStoreError: ``DIRECTORY_INSECURE`` when the parent is missing or the directory fails verification;
            ``IO_ERROR`` when the filesystem fails.
    """
    try:
        directory.mkdir(mode=DIRECTORY_MODE)
    except FileExistsError:
        pass
    except FileNotFoundError:
        raise SecureStoreError(StoreFault.DIRECTORY_INSECURE, f"the parent of {directory} does not exist") from None
    except OSError as error:
        raise SecureStoreError(StoreFault.IO_ERROR, f"cannot create {directory}: {error.strerror}") from None
    else:
        # mkdir applies the umask; this process just created the directory, so setting the exact mode is safe. A path
        # chmod is used because a umask such as 0o777 leaves a directory that cannot be opened to fchmod it; the
        # no-follow verification below still rejects a symlink swapped in between.
        try:
            directory.chmod(DIRECTORY_MODE)
        except OSError as error:
            detail = f"cannot set the mode of {directory}: {error.strerror}"
            raise SecureStoreError(StoreFault.IO_ERROR, detail) from None
    _verify_directory(directory)


def _verify_directory(directory: Path) -> None:
    """Verify the store directory through a no-follow descriptor, so a symlink is rejected rather than followed."""
    try:
        descriptor = os.open(directory, _DIRECTORY_FLAGS)
    except FileNotFoundError:
        raise SecureStoreError(StoreFault.DIRECTORY_INSECURE, f"{directory} does not exist") from None
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            raise SecureStoreError(StoreFault.DIRECTORY_INSECURE, f"{directory} is not a real directory") from None
        raise SecureStoreError(StoreFault.IO_ERROR, f"cannot inspect {directory}: {error.strerror}") from None
    try:
        _check_directory_status(directory, os.fstat(descriptor))
    finally:
        os.close(descriptor)


def read_private_file(directory: Path, name: str, *, max_bytes: int) -> bytes | None:
    """Read one document from a verified store directory, or return ``None`` if it does not exist.

    Raises:
        ValueError: ``name`` is not a plain document name, or ``max_bytes`` is out of range.
        SecureStoreError: ``DIRECTORY_INSECURE``, ``FILE_INSECURE``, ``OVERSIZE``, or ``IO_ERROR``.
    """
    _check_name(name)
    if not 0 < max_bytes <= MAX_DOCUMENT_BYTES_CEILING:
        raise ValueError(f"max_bytes must be between 1 and {MAX_DOCUMENT_BYTES_CEILING}")
    path = directory / name
    try:
        directory_fd = os.open(directory, _DIRECTORY_FLAGS)
    except FileNotFoundError:
        raise SecureStoreError(StoreFault.DIRECTORY_INSECURE, f"{directory} does not exist") from None
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            raise SecureStoreError(StoreFault.DIRECTORY_INSECURE, f"{directory} is not a real directory") from None
        raise SecureStoreError(StoreFault.IO_ERROR, f"cannot open {directory}: {error.strerror}") from None
    try:
        # Verify the directory through the descriptor actually used, so a swap after the check cannot redirect reads.
        _check_directory_status(directory, os.fstat(directory_fd))
        try:
            file_fd = os.open(name, _READ_FLAGS, dir_fd=directory_fd)
        except FileNotFoundError:
            return None
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise SecureStoreError(StoreFault.FILE_INSECURE, f"{path} is a symlink") from None
            if error.errno in (errno.EACCES, errno.EPERM):
                # The owner cannot open its own document: its mode is wrong, which is a permission mismatch.
                raise SecureStoreError(StoreFault.FILE_INSECURE, f"{path} cannot be opened by its owner") from None
            raise SecureStoreError(StoreFault.IO_ERROR, f"cannot open {path}: {error.strerror}") from None
    finally:
        os.close(directory_fd)
    try:
        status = os.fstat(file_fd)
        if not stat.S_ISREG(status.st_mode):
            raise SecureStoreError(StoreFault.FILE_INSECURE, f"{path} is not a regular file")
        if status.st_uid != os.geteuid():
            raise SecureStoreError(StoreFault.FILE_INSECURE, f"{path} is not owned by the add-on user")
        if stat.S_IMODE(status.st_mode) != FILE_MODE:
            raise SecureStoreError(
                StoreFault.FILE_INSECURE,
                f"{path} has mode {stat.S_IMODE(status.st_mode):04o}, expected {FILE_MODE:04o}",
            )
        if status.st_size > max_bytes:
            raise SecureStoreError(StoreFault.OVERSIZE, f"{path} is larger than {max_bytes} bytes")
        # Read one byte past the cap so a file that grows after fstat is still caught.
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(file_fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
    except OSError as error:
        raise SecureStoreError(StoreFault.IO_ERROR, f"cannot read {path}: {error.strerror}") from None
    finally:
        os.close(file_fd)
    if len(data) > max_bytes:
        raise SecureStoreError(StoreFault.OVERSIZE, f"{path} is larger than {max_bytes} bytes")
    return data


def write_private_file(directory: Path, name: str, data: bytes) -> None:
    """Atomically replace one document in a verified store directory with mode exactly ``0600``.

    Raises:
        ValueError: ``name`` is not a plain document name.
        SecureStoreError: ``DIRECTORY_INSECURE``, or ``IO_ERROR`` when the write or the directory sync fails. After an
            ``IO_ERROR`` the document is either unchanged or already replaced, never torn.
    """
    _check_name(name)
    _verify_directory(directory)
    try:
        write_atomically(directory / name, data, require_directory_sync=True)
    except OSError as error:
        raise SecureStoreError(StoreFault.IO_ERROR, f"cannot write {directory / name}: {error.strerror}") from None


# --------------------------------------------------------------------------------------------------
# versioned documents
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StoredDocument:
    """One loaded or saved envelope: its generation and its record."""

    generation: int
    record: Mapping[str, JsonValue]


class VersionedDocumentStore:
    """One canonical-JSON document whose generation increases by exactly one per save.

    One instance per document per process; it is the only writer. Methods are thread-safe, so a caller may run them
    on a worker thread.

    Args:
        directory: Store directory; created with mode ``0700`` on first use if absent.
        name: Document file name, a plain lowercase name.
        store_id: Identifier written into and required from the envelope, e.g. ``"control-authority"``.
        schema_version: The only envelope ``schema_version`` this release reads and writes.
        max_bytes: Cap on the document size, at most :data:`MAX_DOCUMENT_BYTES_CEILING`.
    """

    def __init__(self, directory: Path, name: str, *, store_id: str, schema_version: int, max_bytes: int) -> None:
        _check_name(name)
        if not _NAME.fullmatch(store_id):
            raise ValueError(f"store_id {store_id!r} is not a plain lowercase identifier")
        if not 0 < schema_version <= MAX_GENERATION:
            raise ValueError("schema_version must be a positive uint64")
        if not 0 < max_bytes <= MAX_DOCUMENT_BYTES_CEILING:
            raise ValueError(f"max_bytes must be between 1 and {MAX_DOCUMENT_BYTES_CEILING}")
        self._directory = directory
        self._name = name
        self._store_id = store_id
        self._schema_version = schema_version
        self._max_bytes = max_bytes
        self._lock = threading.Lock()
        # Highest generation this instance has confirmed on disk: None before the first load, 0 for "no document".
        self._known: int | None = None
        # Set while a save's outcome is unconfirmed: the next load accepts either the known or this generation.
        self._unconfirmed: int | None = None

    @property
    def path(self) -> Path:
        """The document's path."""
        return self._directory / self._name

    def load(self) -> StoredDocument | None:
        """Read and verify the document; ``None`` when it has never been written.

        Raises:
            SecureStoreError: Any fault. In particular ``GENERATION_ROLLBACK`` if the document is older than, or has
                disappeared since, one this instance saw, and ``CONCURRENT_MODIFICATION`` if it is newer than any this
                instance wrote.
        """
        with self._lock:
            return self._load_locked()

    def save(self, record: Mapping[str, JsonValue]) -> StoredDocument:
        """Write ``record`` as the next generation and return it.

        The document on disk is re-verified first, so a rollback or a second writer is detected before anything is
        written. A save whose write raises ``IO_ERROR`` leaves its outcome unconfirmed: the next :meth:`load` accepts
        either the previous generation or this one, and a caller must not report the save as successful.

        Raises:
            SecureStoreError: Any fault from :meth:`load`, ``MALFORMED`` if the record is not canonical-JSON
                renderable within the size cap, ``GENERATION_EXHAUSTED``, or ``IO_ERROR``.
        """
        with self._lock:
            current = self._load_locked()
            previous = 0 if current is None else current.generation
            if previous >= MAX_GENERATION:
                raise SecureStoreError(StoreFault.GENERATION_EXHAUSTED, f"{self.path} cannot advance its generation")
            generation = previous + 1
            data = self._render(generation, record)
            try:
                write_private_file(self._directory, self._name, data)
            except SecureStoreError as error:
                if error.fault is StoreFault.IO_ERROR:
                    # The rename may have happened: accept either generation on the next load.
                    self._unconfirmed = generation
                raise
            self._known = generation
            self._unconfirmed = None
            return StoredDocument(generation=generation, record=self._decode(data).record)

    # -- internals --------------------------------------------------------------------------------

    def _load_locked(self) -> StoredDocument | None:
        ensure_private_directory(self._directory)
        data = read_private_file(self._directory, self._name, max_bytes=self._max_bytes)
        document = None if data is None else self._decode(data)
        found = 0 if document is None else document.generation
        self._check_generation(found)
        self._known = found
        self._unconfirmed = None
        return document

    def _check_generation(self, found: int) -> None:
        known = self._known
        if known is None:
            return
        if found == known or (self._unconfirmed is not None and found == self._unconfirmed):
            return
        if found < known:
            what = "disappeared" if found == 0 else f"went back to generation {found}"
            raise SecureStoreError(
                StoreFault.GENERATION_ROLLBACK, f"{self.path} {what} after this process saw generation {known}"
            )
        raise SecureStoreError(
            StoreFault.CONCURRENT_MODIFICATION,
            f"{self.path} is at generation {found}, which this process did not write (last known {known})",
        )

    def _render(self, generation: int, record: Mapping[str, JsonValue]) -> bytes:
        envelope: dict[str, JsonValue] = {
            "generation": generation,
            "record": dict(record),
            "schema_version": self._schema_version,
            "store": self._store_id,
        }
        try:
            return render_canonical_json(envelope, max_depth=_MAX_JSON_DEPTH, max_length=self._max_bytes)
        except ProtocolViolation:
            raise SecureStoreError(
                StoreFault.MALFORMED, f"the record for {self.path} is not canonical JSON within {self._max_bytes} bytes"
            ) from None

    def _decode(self, data: bytes) -> StoredDocument:
        try:
            value = parse_canonical_json(data, max_depth=_MAX_JSON_DEPTH, max_length=self._max_bytes)
        except ProtocolViolation:
            raise SecureStoreError(StoreFault.MALFORMED, f"{self.path} is not canonical JSON") from None
        if not isinstance(value, dict) or set(value) != _ENVELOPE_KEYS:
            raise SecureStoreError(StoreFault.MALFORMED, f"{self.path} is not a store envelope")
        version, store, generation, record = (
            value["schema_version"],
            value["store"],
            value["generation"],
            value["record"],
        )
        # bool is an int subclass; the canonical parser never yields one for a number, but check the type exactly.
        if type(version) is not int or type(generation) is not int or not isinstance(record, dict):
            raise SecureStoreError(StoreFault.MALFORMED, f"{self.path} has envelope fields of the wrong type")
        if version != self._schema_version:
            raise SecureStoreError(
                StoreFault.FOREIGN_SCHEMA,
                f"{self.path} declares schema_version {version}; this release reads {self._schema_version}",
            )
        if store != self._store_id:
            raise SecureStoreError(StoreFault.WRONG_STORE, f"{self.path} does not belong to store {self._store_id!r}")
        if generation < 1:
            raise SecureStoreError(StoreFault.MALFORMED, f"{self.path} has generation 0")
        return StoredDocument(generation=generation, record=record)
