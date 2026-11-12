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
"""

from __future__ import annotations

import contextlib
import os
import secrets
import tempfile
from pathlib import Path
from typing import Final

TEMP_SUFFIX: Final = ".tmp"
"""Suffix of the in-flight temporary file, ``<target name>.<random>.tmp`` in the target's directory."""

FILE_MODE: Final = 0o600
"""Exact permission bits of every document this module writes."""

#: Exclusive creation that never follows or reuses an existing name, like ``tempfile.mkstemp``.
_TEMP_FLAGS: Final = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC


class UnconfirmedReplaceError(OSError):
    """The rename succeeded but the directory sync after it failed.

    The new document is already visible under its name, but not yet durable: a power cut could still bring the
    previous one back. Every other failure of :func:`replace_in_directory` happens before the rename and leaves the
    previous document in place.
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
    temp_name = f"{name}.{secrets.token_hex(8)}{TEMP_SUFFIX}"
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
        os.replace(temp_name, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp_name, dir_fd=directory_fd)
        raise
    try:
        os.fsync(directory_fd)
    except OSError as error:
        raise UnconfirmedReplaceError(error.errno, error.strerror) from error


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
