"""Crash-safe, owner-only replacement of a single file.

Two writers share one set of guarantees:

* :func:`write_atomically` replaces a file by path. It writes the energy state file. The IPMI SDR cache is the
  exception: ``ipmitool`` stages it and the plugin installs it separately.
* :func:`replace_in_directory` replaces an entry relative to an already opened, already verified directory descriptor.
  It is the writer of the security-store foundation (:mod:`remote_energy_meter.secure_store`), which nothing uses yet.

For :func:`write_atomically`:
* the temporary file is created in the target's own directory with ``tempfile.mkstemp``, i.e. ``O_CREAT | O_EXCL``,
  so it cannot cross a filesystem or follow or reuse an existing name, and its mode is then set to exactly ``0600``
  with ``fchmod`` before any byte is written, so no process umask can leave it readable by group or others or strip
  the owner's own access — and because the rename installs this inode, the previous document's mode is not kept;
* the payload is written, flushed, and ``fsync``-ed before the rename;
* the rename is ``os.replace``: a crash at any point leaves either the previous document or the new one, never a
  truncated one;
* the temporary file is removed on any failure, so a failing mount does not accumulate debris;
* the directory entry is ``fsync``-ed afterwards where the platform supports it (best effort, see
  :func:`fsync_directory`).

:func:`replace_in_directory` gives the same guarantees relative to an already opened, already verified directory
descriptor instead of a path, for the security stores: every step (create, rename, unlink, sync) names the entry
relative to that descriptor, so a directory removed or swapped for a symlink after it was verified is never recreated
or followed, and the directory sync is strict. It raises :class:`UnconfirmedReplaceError` only when that sync fails
after the rename, so a caller can tell "the new document may already be in place" from "nothing changed".
:func:`create_in_directory` is its no-replace variant: it installs the same temporary file with a hard link, so an
existing entry is never overwritten (``FileExistsError``) even when two writers race. It raises
:class:`UnconfirmedReplaceError` when a step after the link fails (removing the temporary name or the directory sync).
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import tempfile
from pathlib import Path
from typing import Final

TEMP_SUFFIX: Final = ".tmp"
"""Suffix of the in-flight temporary file, ``<target name>.<random>.tmp`` in the target's directory."""

_TEMP_TOKEN_BYTES: Final = 8
"""Random bytes in a :func:`replace_in_directory` temporary name, written as twice as many lowercase hex digits."""

FILE_MODE: Final = 0o600
"""Exact permission bits of every document this module writes."""

#: Exclusive creation that never follows or reuses an existing name, like ``tempfile.mkstemp``.
_TEMP_FLAGS: Final = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC


class UnconfirmedReplaceError(OSError):
    """The rename (or, for :func:`create_in_directory`, the link) succeeded but a step after it failed.

    The new document is already visible under its name, but not yet durable: a power cut could still bring the
    previous state back (the previous document, or for :func:`create_in_directory` no entry). Every other failure of
    :func:`replace_in_directory` and :func:`create_in_directory` happens before the document is installed and leaves
    the previous state in place.
    """


def write_atomically(path: Path, data: bytes) -> None:
    """Replace ``path`` with exactly ``data``, atomically and with mode exactly ``0600``.

    Args:
        path: Target file. Missing parent directories are created.
        data: Exact bytes the file contains afterwards.

    Raises:
        OSError: Creating, writing, syncing, or renaming the temporary file failed. The previous document, if any, is
            unchanged and the temporary file has been removed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    descriptor, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=TEMP_SUFFIX)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), FILE_MODE)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # Path.replace is os.replace: the atomic, same-filesystem rename this whole function exists for.
        temp_path.replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            temp_path.unlink(missing_ok=True)
        raise

    fsync_directory(path.parent)


def replace_in_directory(directory_fd: int, name: str, data: bytes) -> None:
    """Replace the entry ``name`` of the directory open as ``directory_fd`` with exactly ``data``, mode ``0600``.

    The caller opens and verifies the directory; nothing here resolves a path or creates a directory. The temporary
    file is ``<name>.<random hex>.tmp`` in the same directory, created exclusively without following a symlink.

    Raises:
        UnconfirmedReplaceError: The rename happened, but the directory sync after it failed.
        OSError: Any earlier step failed. The previous document, if any, is unchanged and the temporary file has been
            removed.
    """
    temp_name = _write_temporary(directory_fd, name, data)
    try:
        os.replace(temp_name, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp_name, dir_fd=directory_fd)
        raise
    try:
        os.fsync(directory_fd)
    except OSError as error:
        raise UnconfirmedReplaceError(error.errno, error.strerror) from error


def create_in_directory(directory_fd: int, name: str, data: bytes) -> None:
    """Create the entry ``name`` of the directory open as ``directory_fd`` with exactly ``data``, mode ``0600``.

    The same temporary file as :func:`replace_in_directory`, but it is installed with a hard link instead of a rename,
    so an existing entry is never replaced: whoever links first wins, and every later creator gets
    :class:`FileExistsError`. The temporary name is then unlinked and the directory synced.

    Raises:
        FileExistsError: ``name`` already exists; it is unchanged and the temporary file has been removed.
        UnconfirmedReplaceError: The link happened, but removing the temporary name or the directory sync after it
            failed. The new document is visible; a leftover temporary name is another link to the same bytes.
        OSError: Any earlier step failed. Nothing was installed and the temporary file has been removed.
    """
    temp_name = _write_temporary(directory_fd, name, data)
    try:
        os.link(temp_name, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd, follow_symlinks=False)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp_name, dir_fd=directory_fd)
        raise
    try:
        os.unlink(temp_name, dir_fd=directory_fd)
        os.fsync(directory_fd)
    except OSError as error:
        raise UnconfirmedReplaceError(error.errno, error.strerror) from error


def _write_temporary(directory_fd: int, name: str, data: bytes) -> str:
    """Write ``data`` to a new, synced ``0600`` temporary file for ``name`` and return its name.

    The file is created exclusively without following a symlink; on any failure it is removed before the error
    propagates.
    """
    temp_name = f"{name}.{secrets.token_hex(_TEMP_TOKEN_BYTES)}{TEMP_SUFFIX}"
    descriptor = os.open(temp_name, _TEMP_FLAGS, FILE_MODE, dir_fd=directory_fd)
    try:
        try:
            os.fchmod(descriptor, FILE_MODE)
            view = memoryview(data)
            while view:
                view = view[os.write(descriptor, view) :]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp_name, dir_fd=directory_fd)
        raise
    return temp_name


def temporary_name_pattern(name: str) -> re.Pattern[str]:
    """Return the pattern whose ``fullmatch`` accepts exactly the temporary names :func:`replace_in_directory` makes.

    It lets a caller recognise and remove a temporary file left by a crash for ``name`` without matching any other
    entry, such as a sibling document whose name merely starts with ``name``.
    """
    return re.compile(rf"{re.escape(name)}\.[0-9a-f]{{{2 * _TEMP_TOKEN_BYTES}}}{re.escape(TEMP_SUFFIX)}")


def fsync_directory(directory: Path) -> None:
    """Flush a directory entry so a rename survives a power cut. Best effort by design.

    Filesystems that reject a directory ``fsync``, some network mounts among them, are tolerated: the document contents
    are already durable and the rename is atomic regardless, so this is a durability nicety, not a correctness
    requirement.
    """
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)
