"""Crash-safe, owner-only replacement of a single file.

Every durable document the add-on owns is written through :func:`write_atomically`, so each store gets the same
guarantees from one implementation instead of a parallel writer:

* the temporary file is created in the target's own directory with ``tempfile.mkstemp``, i.e. ``O_CREAT | O_EXCL``
  and mode ``0600``, so it cannot cross a filesystem, cannot follow or reuse an existing name, and is never readable by
  another user — and because the rename keeps the temporary file's inode, the replaced document is ``0600`` too;
* the payload is written, flushed, and ``fsync``-ed before the rename;
* the rename is ``os.replace``: a crash at any point leaves either the previous document or the new one, never a
  truncated one;
* the temporary file is removed on any failure, so a failing mount does not accumulate debris;
* the directory entry is ``fsync``-ed afterwards where the platform supports it (best effort, see
  :func:`fsync_directory`).
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path
from typing import Final

TEMP_SUFFIX: Final = ".tmp"
"""Suffix of the in-flight temporary file, ``<target name>.<random>.tmp`` in the target's directory."""


def write_atomically(path: Path, data: bytes) -> None:
    """Replace ``path`` with exactly ``data``, atomically and owner-only.

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
