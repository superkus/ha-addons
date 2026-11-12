"""Crash-safe, owner-only replacement of a single file.

The add-on's state stores (currently the energy state file) are written through :func:`write_atomically`, so each
store gets the same guarantees from one implementation instead of a parallel writer. The IPMI SDR cache is the
exception: ``ipmitool`` stages it and the plugin installs it separately.

* the temporary file is created in the target's own directory with ``tempfile.mkstemp``, i.e. ``O_CREAT | O_EXCL``,
  so it cannot cross a filesystem or follow or reuse an existing name, and its mode is then set to exactly ``0600``
  with ``fchmod`` before any byte is written, so no process umask can leave it readable by group or others or strip
  the owner's own access — and because the rename installs this inode, the previous document's mode is not kept;
* the payload is written, flushed, and ``fsync``-ed before the rename;
* the rename is ``os.replace``: a crash at any point leaves either the previous document or the new one, never a
  truncated one;
* the temporary file is removed on any failure, so a failing mount does not accumulate debris;
* the directory entry is ``fsync``-ed afterwards. By default that is best effort (see :func:`fsync_directory`); a
  security store passes ``require_directory_sync=True`` so an unconfirmed rename fails the write.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path
from typing import Final

TEMP_SUFFIX: Final = ".tmp"
"""Suffix of the in-flight temporary file, ``<target name>.<random>.tmp`` in the target's directory."""

FILE_MODE: Final = 0o600
"""Exact permission bits of every document this module writes."""


def write_atomically(path: Path, data: bytes, *, require_directory_sync: bool = False) -> None:
    """Replace ``path`` with exactly ``data``, atomically and with mode exactly ``0600``.

    Args:
        path: Target file. Missing parent directories are created.
        data: Exact bytes the file contains afterwards.
        require_directory_sync: Raise when the directory entry cannot be ``fsync``-ed after the rename, instead of
            tolerating it. The new document is already in place when that happens, so the caller must treat the
            outcome as unconfirmed: a later read sees either the previous or the new document, never a torn one.

    Raises:
        OSError: Creating, writing, syncing, or renaming the temporary file failed. The previous document, if any, is
            unchanged and the temporary file has been removed. With ``require_directory_sync``, also raised when the
            post-rename directory sync fails.
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

    if require_directory_sync:
        sync_directory_strictly(path.parent)
    else:
        fsync_directory(path.parent)


def sync_directory_strictly(directory: Path) -> None:
    """Flush a directory entry, raising ``OSError`` when the directory cannot be opened or synced."""
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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
