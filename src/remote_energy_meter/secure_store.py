"""Fail-closed, owner-only storage for the add-on's security state.

The reporter and control stores (PKI, roster, tokens, revocations, control authority, replay and result records) keep
secrets and authority under ``/data``. Unlike the energy store, which resets a bad entry to zero so metering keeps
running, these stores **fail closed**: any doubt about a document's integrity stops the feature that depends on it.
Nothing here repairs or silently ignores state, and nothing is recreated once a generation is known; the only thing
ever created is a missing store directory before any generation is known (see below).

Three layers:

* :func:`ensure_private_directory` creates or verifies a store directory: a real directory (not a symlink) owned by the
  effective user with mode exactly ``0700``. An existing directory with any other mode is reported, never
  ``chmod``-ed back, because a widened mode is evidence the contents can no longer be trusted. A directory it creates
  gets its mode set only through a no-follow descriptor, never by path.
* :func:`read_private_file` / :func:`write_private_file` read and replace one document inside such a directory. Reads
  open the name relative to the verified directory with ``O_NOFOLLOW | O_NONBLOCK`` and check the open descriptor, so
  a symlink, FIFO, device, foreign owner, or any mode other than ``0600`` is rejected, and the size is capped before
  the content is read. Writes go through :func:`remote_energy_meter.atomic_file.replace_in_directory` relative to the
  verified directory's descriptor, refuse to replace an insecure existing entry, and fail when the rename cannot be
  confirmed durable.
* :class:`VersionedDocumentStore` stores one canonical-JSON envelope ``{"generation", "record", "schema_version",
  "store"}``. Every successful save increments ``generation`` by exactly one. A store instance remembers the last
  generation it loaded or wrote and fails closed if the document on disk later moves backwards, jumps ahead, or
  disappears while it is running.

Preconditions and assumptions:

* The store directory's parent (for example ``/data``) must be writable only by the effective user or root. The
  directory itself is created and verified by path before it is opened; whoever can rename entries in the parent can
  race that step.
* Each document has exactly one writer: one :class:`VersionedDocumentStore` instance in one running process. Detection
  of other writers is by generation number only. A second process that writes a higher generation is reported
  (``CONCURRENT_MODIFICATION``) on this instance's next load or save, but a write interleaved between this instance's
  check and its rename, or a substituted document that carries the same generation, is not detected.

What this layer does not detect:

* State lost while no instance is running. A store instance starts with no known generation, so a document deleted
  while the add-on was stopped loads as never written and the next save restarts at generation 1; a deleted store
  directory is recreated empty. Detecting that needs the later marker and quarantine layer.
* The rollback of a *whole* restored backup: the restored file is internally consistent. Restore checks against
  independently held state and the post-restore quarantine own that case.
* Content edits. This layer does not authenticate content: an edit that keeps a valid envelope (for example a changed
  digit in a record value) loads as written. Stores whose records need tamper evidence use
  :class:`remote_energy_meter.authenticated_store.AuthenticatedDocumentStore`, which adds an integrity tag on top of
  this layer.

A crash during a write can leave a temporary ``<name>.<random>.tmp`` file in the store directory. It is never read as
the document, but it can hold a newer record's bytes, so :class:`VersionedDocumentStore` deletes its own document's
temporary files after every load that verifies the document, including the load at the start of each save. A load that
fails closed leaves them in place on purpose, as evidence for recovery.

Errors are :class:`SecureStoreError` with a :class:`StoreFault`. Their ``detail`` may name paths, the store id,
generation and ``schema_version`` numbers, size caps, file modes, and the type name of an underlying exception; it never
contains record content, so it is safe to log.
"""

from __future__ import annotations

import errno
import os
import re
import stat
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from remote_energy_meter.atomic_file import (
    FILE_MODE,
    TEMP_SUFFIX,
    UnconfirmedReplaceError,
    replace_in_directory,
    temporary_name_pattern,
)
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

_UINT64_MAX: Final = 2**64 - 1
#: Generations are uint64, matching the protocol's generation fields.
_MAX_GENERATION: Final = _UINT64_MAX

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
    """The document's generation is lower than one this process already saw, or the document or its directory
    vanished."""

    CONCURRENT_MODIFICATION = "concurrent_modification"
    """The document's generation is higher than any this process wrote: another writer exists."""

    GENERATION_EXHAUSTED = "generation_exhausted"
    """The uint64 generation cannot advance further."""

    IO_ERROR = "io_error"
    """The filesystem failed. A write with this fault may or may not have taken effect; see
    :meth:`VersionedDocumentStore.save`."""

    KEY_MISSING = "key_missing"
    """Authenticated state exists or is being written, but its state-authentication key is absent."""

    KEY_EXISTS = "key_exists"
    """A new state-authentication key was to be created, but one already exists; it is never replaced."""

    AUTHENTICATION_FAILED = "authentication_failed"
    """A record's integrity tag does not verify under the state-authentication key: the record was altered, moved to
    another store or generation, or written under a different key."""


class SecureStoreError(Exception):
    """A security document failed a fail-closed check.

    ``detail`` is safe to log: it may name paths, the store id, generation and schema numbers, modes, and exception type
    names, but never record content.
    """

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


def _check_file_status(path: Path, status: os.stat_result) -> None:
    if stat.S_ISLNK(status.st_mode):
        raise SecureStoreError(StoreFault.FILE_INSECURE, f"{path} is a symlink")
    if not stat.S_ISREG(status.st_mode):
        raise SecureStoreError(StoreFault.FILE_INSECURE, f"{path} is not a regular file")
    if status.st_uid != os.geteuid():
        raise SecureStoreError(StoreFault.FILE_INSECURE, f"{path} is not owned by the add-on user")
    if stat.S_IMODE(status.st_mode) != FILE_MODE:
        raise SecureStoreError(
            StoreFault.FILE_INSECURE,
            f"{path} has mode {stat.S_IMODE(status.st_mode):04o}, expected {FILE_MODE:04o}",
        )


def ensure_private_directory(directory: Path) -> None:
    """Create ``directory`` with mode ``0700`` if it is absent, then verify it.

    Only the last path component is created; its parent (for example ``/data``) must already exist. An existing
    directory is verified exactly as found and never modified. A directory this call creates gets its mode set to
    ``0700`` through a no-follow descriptor, because ``mkdir`` applies the process umask. If the umask leaves it
    unopenable by its owner, it is refused and left in place, never ``chmod``-ed by path; to recover, fix the umask and
    remove the empty directory.

    Raises:
        SecureStoreError: ``DIRECTORY_INSECURE`` when the parent is missing, a directory this call created cannot be
            opened by its owner (umask), or the directory fails verification; ``IO_ERROR`` when the filesystem fails,
            including setting the new directory's mode.
    """
    try:
        directory.mkdir(mode=DIRECTORY_MODE)
    except FileExistsError:
        pass
    except FileNotFoundError:
        raise SecureStoreError(StoreFault.DIRECTORY_INSECURE, f"the parent of {directory} does not exist") from None
    except OSError as error:
        raise SecureStoreError(StoreFault.IO_ERROR, f"cannot create {directory}: {type(error).__name__}") from None
    else:
        _set_new_directory_mode(directory)
    _verify_directory(directory)


def _set_new_directory_mode(directory: Path) -> None:
    """Set exactly ``0700`` on a directory this process just created, through a no-follow descriptor.

    mkdir applies the umask, so the mode is set explicitly. A umask that removes the owner's read bit (e.g. ``0o777``)
    leaves a directory its owner cannot open; setting its mode by path could follow a symlink swapped in, so that case
    fails closed instead. The no-follow verification afterwards rejects anything swapped in.
    """
    try:
        descriptor = os.open(directory, _DIRECTORY_FLAGS)
    except PermissionError:
        raise SecureStoreError(
            StoreFault.DIRECTORY_INSECURE,
            f"{directory} was created unreadable by its owner; the process umask removes owner permissions",
        ) from None
    except FileNotFoundError:
        raise SecureStoreError(StoreFault.DIRECTORY_INSECURE, f"{directory} does not exist") from None
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            raise SecureStoreError(StoreFault.DIRECTORY_INSECURE, f"{directory} is not a real directory") from None
        raise SecureStoreError(StoreFault.IO_ERROR, f"cannot open {directory}: {type(error).__name__}") from None
    try:
        os.fchmod(descriptor, DIRECTORY_MODE)
    except OSError as error:
        raise SecureStoreError(
            StoreFault.IO_ERROR, f"cannot set the mode of {directory}: {type(error).__name__}"
        ) from None
    finally:
        os.close(descriptor)


def _open_verified_directory(directory: Path) -> int:
    """Open the store directory with a no-follow descriptor and verify it through that descriptor.

    The caller names every entry relative to the returned descriptor, so a directory swapped after the check cannot
    redirect the operation. The caller closes it.
    """
    try:
        descriptor = os.open(directory, _DIRECTORY_FLAGS)
    except FileNotFoundError:
        raise SecureStoreError(StoreFault.DIRECTORY_INSECURE, f"{directory} does not exist") from None
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            raise SecureStoreError(StoreFault.DIRECTORY_INSECURE, f"{directory} is not a real directory") from None
        if error.errno in (errno.EACCES, errno.EPERM):
            _reject_unreadable_directory(directory)
        raise SecureStoreError(StoreFault.IO_ERROR, f"cannot open {directory}: {type(error).__name__}") from None
    try:
        _check_directory_status(directory, os.fstat(descriptor))
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _reject_unreadable_directory(directory: Path) -> None:
    """Report a directory its owner cannot open because its mode lacks owner read as ``DIRECTORY_INSECURE``.

    A wrong mode is a permission mismatch however it shows up. Any other cause (a real ``0700`` directory that still
    cannot be opened, a foreign owner, a failing ``lstat``) returns, and the caller reports ``IO_ERROR``.
    """
    try:
        status = os.lstat(directory)
    except OSError:
        return
    if (
        stat.S_ISDIR(status.st_mode)
        and status.st_uid == os.geteuid()
        and stat.S_IMODE(status.st_mode) != DIRECTORY_MODE
    ):
        _check_directory_status(directory, status)


def _verify_directory(directory: Path) -> None:
    """Verify the store directory through a no-follow descriptor, so a symlink is rejected rather than followed."""
    os.close(_open_verified_directory(directory))


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
    directory_fd = _open_verified_directory(directory)
    try:
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
            raise SecureStoreError(StoreFault.IO_ERROR, f"cannot open {path}: {type(error).__name__}") from None
    finally:
        os.close(directory_fd)
    try:
        status = os.fstat(file_fd)
        _check_file_status(path, status)
        if status.st_size > max_bytes:
            raise SecureStoreError(StoreFault.OVERSIZE, f"{path} is larger than {max_bytes} bytes")
        # Read one byte past the cap so a file that grows after fstat is still caught.
        data = _read_at_most(file_fd, max_bytes + 1)
    except OSError as error:
        raise SecureStoreError(StoreFault.IO_ERROR, f"cannot read {path}: {type(error).__name__}") from None
    finally:
        os.close(file_fd)
    if len(data) > max_bytes:
        raise SecureStoreError(StoreFault.OVERSIZE, f"{path} is larger than {max_bytes} bytes")
    return data


def _read_at_most(descriptor: int, limit: int) -> bytes:
    """Read from ``descriptor`` until end of file or ``limit`` bytes, whichever comes first."""
    chunks: list[bytes] = []
    remaining = limit
    while remaining > 0:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def write_private_file(directory: Path, name: str, data: bytes) -> None:
    """Atomically replace one document in a verified store directory with mode exactly ``0600``.

    Every step names the document relative to the descriptor of the verified directory
    (:func:`remote_energy_meter.atomic_file.replace_in_directory`), so a directory removed or swapped for a symlink
    after the check is neither recreated nor followed. An existing entry that is not a regular ``0600`` file owned by
    the add-on user is evidence of tampering and is left untouched.

    Raises:
        ValueError: ``name`` is not a plain document name.
        SecureStoreError: ``DIRECTORY_INSECURE``, ``FILE_INSECURE`` for an insecure existing entry, or ``IO_ERROR``
            when the write or the directory sync fails. After an ``IO_ERROR`` the document is either unchanged or
            already replaced, never torn.
    """
    _check_name(name)
    try:
        _replace_private_file(directory, name, data)
    except UnconfirmedReplaceError as error:
        raise _unconfirmed_replace(directory, name, error) from None


def _replace_private_file(directory: Path, name: str, data: bytes) -> None:
    """:func:`write_private_file` without the final translation: a failed post-rename sync stays an
    :class:`~remote_energy_meter.atomic_file.UnconfirmedReplaceError`, so :meth:`VersionedDocumentStore.save` can tell
    "the new document may be in place" from "nothing changed". Every other failure is a :class:`SecureStoreError`."""
    path = directory / name
    directory_fd = _open_verified_directory(directory)
    try:
        try:
            existing = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        except OSError as error:
            raise SecureStoreError(StoreFault.IO_ERROR, f"cannot inspect {path}: {type(error).__name__}") from None
        else:
            _check_file_status(path, existing)
        try:
            replace_in_directory(directory_fd, name, data)
        except UnconfirmedReplaceError:
            # An OSError subclass, so it must pass through before the generic handler turns it into "nothing changed".
            raise
        except OSError as error:
            raise SecureStoreError(StoreFault.IO_ERROR, f"cannot write {path}: {type(error).__name__}") from None
    finally:
        os.close(directory_fd)


def _unconfirmed_replace(directory: Path, name: str, error: UnconfirmedReplaceError) -> SecureStoreError:
    # The rename happened; only its durability is unconfirmed. Name the underlying sync failure, not the wrapper.
    detail = f"replaced {directory / name} but cannot sync {directory}: {type(error.__cause__ or error).__name__}"
    return SecureStoreError(StoreFault.IO_ERROR, detail)


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

    One instance per document, in one process; it must be the only writer (see the module docstring for what a second
    writer can get past). Methods are thread-safe, so a caller may run them on a worker thread.

    Args:
        directory: Store directory; created with mode ``0700`` if absent while this instance knows no written
            generation. Once it does, a vanished directory is a ``GENERATION_ROLLBACK`` and is never recreated.
        name: Document file name, a plain lowercase name.
        store_id: Identifier written into and required from the envelope, e.g. ``"control-authority"``.
        schema_version: The only envelope ``schema_version`` this release reads and writes.
        max_bytes: Cap on the document size, at most :data:`MAX_DOCUMENT_BYTES_CEILING`.
    """

    def __init__(self, directory: Path, name: str, *, store_id: str, schema_version: int, max_bytes: int) -> None:
        _check_name(name)
        if not _NAME.fullmatch(store_id):
            raise ValueError(f"store_id {store_id!r} is not a plain lowercase identifier")
        if not 0 < schema_version <= _UINT64_MAX:
            raise ValueError("schema_version must be a positive uint64")
        if not 0 < max_bytes <= MAX_DOCUMENT_BYTES_CEILING:
            raise ValueError(f"max_bytes must be between 1 and {MAX_DOCUMENT_BYTES_CEILING}")
        self._directory = directory
        self._name = name
        self._store_id = store_id
        self._schema_version = schema_version
        self._max_bytes = max_bytes
        self._temporary_name = temporary_name_pattern(name)
        self._lock = threading.Lock()
        # Highest generation this instance has confirmed on disk: None before the first load, 0 for "no document".
        self._known_generation: int | None = None
        # Set while a save's outcome is unconfirmed: the next load accepts either the known or this generation.
        self._unconfirmed_generation: int | None = None

    @property
    def path(self) -> Path:
        """The document's path."""
        return self._directory / self._name

    def load(self) -> StoredDocument | None:
        """Read and verify the document; ``None`` when it has never been written.

        Once the document verifies, this document's leftover temporary files are deleted (see the module docstring).

        Raises:
            SecureStoreError: Any fault. In particular ``GENERATION_ROLLBACK`` if the document is older than, or has
                disappeared since, one this instance saw, ``CONCURRENT_MODIFICATION`` if it is newer than any this
                instance wrote, and ``IO_ERROR`` if the leftover temporary files cannot be removed.
        """
        with self._lock:
            return self._load_locked()

    def save(self, record: Mapping[str, JsonValue]) -> StoredDocument:
        """Write ``record`` as the next generation and return it; :meth:`save_with` for a generation-bound record."""
        return self.save_with(lambda _generation: record)

    def save_with(self, build: Callable[[int], Mapping[str, JsonValue]]) -> StoredDocument:
        """Write ``build(generation)`` as the next generation and return it.

        ``build`` receives the generation the record will be stored under, so a layer above can bind it (for example in
        an integrity tag). It runs under the store lock after the document on disk was re-verified; an exception from it
        writes nothing.

        The document on disk is re-verified first, so a rollback or a second writer is detected before anything is
        written. A save whose rename happened but whose directory sync failed raises ``IO_ERROR`` and leaves its outcome
        unconfirmed: the next :meth:`load` accepts either the previous generation or this one (this one only once a
        directory sync succeeds; until then the load raises ``IO_ERROR``). Any ``IO_ERROR`` before the rename leaves the
        previous generation in place and relaxes nothing. Either way a caller must not report the save as successful.

        Raises:
            SecureStoreError: Any fault from :meth:`load`, ``MALFORMED`` if the record is not canonical-JSON
                renderable within the size cap, ``GENERATION_EXHAUSTED``, or ``IO_ERROR``.
        """
        with self._lock:
            current = self._load_locked()
            previous = 0 if current is None else current.generation
            if previous >= _MAX_GENERATION:
                raise SecureStoreError(StoreFault.GENERATION_EXHAUSTED, f"{self.path} cannot advance its generation")
            generation = previous + 1
            data = self._render(generation, build(generation))
            try:
                _replace_private_file(self._directory, self._name, data)
            except UnconfirmedReplaceError as error:
                # Only a failure after the rename may leave this generation on disk: accept either on the next load.
                self._unconfirmed_generation = generation
                raise _unconfirmed_replace(self._directory, self._name, error) from None
            self._known_generation = generation
            self._unconfirmed_generation = None
            # Return a fresh parse of what was written, not the caller's mapping, which the caller may still mutate.
            return StoredDocument(generation=generation, record=self._decode(data).record)

    # -- internals --------------------------------------------------------------------------------

    def _load_locked(self) -> StoredDocument | None:
        known = self._known_generation
        if not known:
            # Nothing confirmed written yet (first use, or a load saw no document): creating the directory is first
            # use, and a directory that vanished since is still consistent with "never written".
            ensure_private_directory(self._directory)
        try:
            data = read_private_file(self._directory, self._name, max_bytes=self._max_bytes)
        except SecureStoreError as error:
            # Once a generation is known, a vanished directory is a vanished document: never recreate it.
            if known and error.fault is StoreFault.DIRECTORY_INSECURE and not os.path.lexists(self._directory):
                raise SecureStoreError(
                    StoreFault.GENERATION_ROLLBACK,
                    f"{self._directory} disappeared after this process saw generation {known} of {self.path}",
                ) from None
            raise
        document = None if data is None else self._decode(data)
        found = 0 if document is None else document.generation
        self._check_generation(found)
        self._remove_leftover_temporaries()
        self._known_generation = found
        self._unconfirmed_generation = None
        return document

    def _remove_leftover_temporaries(self) -> None:
        """Delete this document's temporary files left by an interrupted write.

        They are never read as the document, but one can hold the bytes of a newer record, and later stores keep keys,
        so they are removed rather than left on disk. The instance lock serializes this with its own saves, so any
        such file present here is debris. Only names of exactly this document's temporary pattern are touched, through
        the verified directory descriptor; ``unlink`` never follows a symlink.
        """
        pattern = self._temporary_name
        directory_fd = _open_verified_directory(self._directory)
        try:
            with os.scandir(directory_fd) as entries:
                leftovers = [entry.name for entry in entries if pattern.fullmatch(entry.name)]
            for leftover in leftovers:
                os.unlink(leftover, dir_fd=directory_fd)
            if leftovers:
                os.fsync(directory_fd)
        except OSError as error:
            raise SecureStoreError(
                StoreFault.IO_ERROR, f"cannot remove temporary files of {self.path}: {type(error).__name__}"
            ) from None
        finally:
            os.close(directory_fd)

    def _check_generation(self, found: int) -> None:
        known = self._known_generation
        if known is None:
            return
        if found == known:
            return
        if self._unconfirmed_generation is not None and found == self._unconfirmed_generation:
            # The unconfirmed rename is visible but not yet durable: a power cut could still bring the previous
            # generation back. Accept it only once a directory sync succeeds; until then keep it unconfirmed. The sync
            # goes through a verified no-follow descriptor, so a directory swapped for a symlink confirms nothing.
            directory_fd = _open_verified_directory(self._directory)
            try:
                os.fsync(directory_fd)
            except OSError as error:
                raise SecureStoreError(
                    StoreFault.IO_ERROR, f"cannot confirm generation {found} of {self.path}: {type(error).__name__}"
                ) from None
            finally:
                os.close(directory_fd)
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
        if (
            type(version) is not int
            or type(generation) is not int
            or type(store) is not str
            or not isinstance(record, dict)
        ):
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
