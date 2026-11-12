"""The shared crash-safe, owner-only file writer every durable store uses."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path
from typing import Any

import pytest

from remote_energy_meter.atomic_file import (
    FILE_MODE,
    TEMP_SUFFIX,
    UnconfirmedReplaceError,
    fsync_directory,
    replace_in_directory,
    write_atomically,
)


def temp_files(directory: Path) -> list[Path]:
    return [entry for entry in directory.iterdir() if entry.name.endswith(TEMP_SUFFIX)]


def test_writes_exact_bytes_and_leaves_no_temporary_file(tmp_path: Path) -> None:
    target = tmp_path / "document.json"
    payload = b'{"a":1}\n\x00\xff'

    write_atomically(target, payload)

    assert target.read_bytes() == payload
    assert temp_files(tmp_path) == []


def test_creates_missing_parent_directories(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "deeper" / "document.json"

    write_atomically(target, b"x")

    assert target.read_bytes() == b"x"


def test_replaces_an_existing_document_completely(tmp_path: Path) -> None:
    target = tmp_path / "document.json"
    target.write_bytes(b"a much longer previous document")

    write_atomically(target, b"short")

    assert target.read_bytes() == b"short"


def test_an_empty_payload_produces_an_empty_document(tmp_path: Path) -> None:
    target = tmp_path / "document.json"
    target.write_bytes(b"previous")

    write_atomically(target, b"")

    assert target.read_bytes() == b""


@pytest.mark.parametrize("umask", [0o000, 0o022, 0o077, 0o277, 0o377, 0o777])
def test_the_document_is_exactly_0600_under_any_umask(tmp_path: Path, umask: int) -> None:
    """fchmod sets the mode after creation, so neither a permissive nor an owner-stripping umask changes it."""
    target = tmp_path / "document.json"
    previous = os.umask(umask)
    try:
        write_atomically(target, b"secret-free but private")
    finally:
        os.umask(previous)

    assert stat.S_IMODE(target.stat().st_mode) == FILE_MODE == 0o600
    assert target.read_bytes() == b"secret-free but private"


def test_the_mode_is_set_before_any_byte_is_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[int] = []
    real_fsync = os.fsync

    def recording_fsync(descriptor: int) -> None:
        status = os.fstat(descriptor)
        if stat.S_ISREG(status.st_mode):
            observed.append(stat.S_IMODE(status.st_mode))
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    previous = os.umask(0o377)
    try:
        write_atomically(tmp_path / "document.json", b"x")
    finally:
        os.umask(previous)

    assert observed == [0o600]


def test_a_previous_world_readable_document_becomes_owner_only(tmp_path: Path) -> None:
    """The rename installs the temporary file's inode, so the old document's mode is not inherited."""
    target = tmp_path / "document.json"
    target.write_bytes(b"old")
    target.chmod(0o644)

    write_atomically(target, b"new")

    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_the_temporary_file_is_created_exclusively_in_the_target_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    real_mkstemp = tempfile.mkstemp

    def recording_mkstemp(**kwargs: Any) -> tuple[int, str]:
        calls.append(kwargs)
        return real_mkstemp(**kwargs)

    monkeypatch.setattr(tempfile, "mkstemp", recording_mkstemp)
    target = tmp_path / "document.json"

    write_atomically(target, b"x")

    # mkstemp is the O_CREAT | O_EXCL, mode-0600 primitive; the same directory keeps the rename on one filesystem.
    assert calls == [{"dir": tmp_path, "prefix": "document.json.", "suffix": TEMP_SUFFIX}]


def test_a_failed_rename_keeps_the_previous_document_and_removes_the_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "document.json"
    target.write_bytes(b"previous")

    def refuse(self: Path, destination: Any) -> Path:
        raise OSError("simulated interruption at the rename")

    monkeypatch.setattr(Path, "replace", refuse)

    with pytest.raises(OSError, match="simulated interruption at the rename"):
        write_atomically(target, b"new")
    assert target.read_bytes() == b"previous"
    assert temp_files(tmp_path) == []


def test_a_failed_file_fsync_keeps_the_previous_document_and_removes_the_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "document.json"
    target.write_bytes(b"previous")

    def refuse(descriptor: int) -> None:
        raise OSError("simulated interruption at fsync")

    monkeypatch.setattr(os, "fsync", refuse)

    with pytest.raises(OSError, match="simulated interruption at fsync"):
        write_atomically(target, b"new")
    monkeypatch.undo()
    assert target.read_bytes() == b"previous"
    assert temp_files(tmp_path) == []


def test_a_first_write_that_fails_leaves_nothing_behind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "document.json"

    def refuse(self: Path, destination: Any) -> Path:
        raise OSError("simulated interruption at the rename")

    monkeypatch.setattr(Path, "replace", refuse)

    with pytest.raises(OSError):
        write_atomically(target, b"new")
    assert list(tmp_path.iterdir()) == []


def test_an_interrupt_during_the_write_still_removes_the_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cleanup covers BaseException, so Ctrl-C or task cancellation mid-write leaves no debris either."""
    target = tmp_path / "document.json"

    def interrupt(descriptor: int) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "fsync", interrupt)

    with pytest.raises(KeyboardInterrupt):
        write_atomically(target, b"new")
    monkeypatch.undo()
    assert list(tmp_path.iterdir()) == []


def test_the_directory_entry_is_fsynced_after_the_rename(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    synced: list[bool] = []
    real_fsync = os.fsync
    target = tmp_path / "document.json"

    def recording_fsync(descriptor: int) -> None:
        is_directory = stat.S_ISDIR(os.fstat(descriptor).st_mode)
        # The directory sync must observe the completed rename.
        if is_directory:
            assert target.read_bytes() == b"x"
        synced.append(is_directory)
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", recording_fsync)

    write_atomically(target, b"x")

    assert synced == [False, True]


def test_an_unsupported_directory_fsync_is_tolerated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_fsync = os.fsync

    def refuse_directories(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("simulated unsupported directory fsync")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", refuse_directories)
    target = tmp_path / "document.json"

    write_atomically(target, b"x")

    assert target.read_bytes() == b"x"


def test_fsync_directory_ignores_a_directory_that_cannot_be_opened(tmp_path: Path) -> None:
    fsync_directory(tmp_path / "missing")


def test_fsync_directory_closes_its_descriptor_even_when_fsync_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[int] = []
    opened: list[int] = []
    real_open = os.open
    real_close = os.close

    def recording_open(path: Any, flags: int, *args: Any) -> int:
        descriptor = real_open(path, flags, *args)
        opened.append(descriptor)
        return descriptor

    def recording_close(descriptor: int) -> None:
        closed.append(descriptor)
        real_close(descriptor)

    def refuse(descriptor: int) -> None:
        raise OSError("simulated unsupported directory fsync")

    monkeypatch.setattr(os, "open", recording_open)
    monkeypatch.setattr(os, "close", recording_close)
    monkeypatch.setattr(os, "fsync", refuse)

    fsync_directory(tmp_path)

    assert opened and closed == opened


# --------------------------------------------------------------------------------------------------
# replace_in_directory: the descriptor-relative writer of the security stores
# --------------------------------------------------------------------------------------------------


def replace_via_descriptor(directory: Path, name: str, data: bytes) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        replace_in_directory(descriptor, name, data)
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("umask", [0o000, 0o022, 0o777])
def test_replace_in_directory_writes_exact_bytes_with_mode_0600_and_no_debris(tmp_path: Path, umask: int) -> None:
    target = tmp_path / "document.json"
    target.write_bytes(b"previous")
    target.chmod(0o644)
    previous = os.umask(umask)
    try:
        replace_via_descriptor(tmp_path, "document.json", b"\x00new\xff")
    finally:
        os.umask(previous)

    assert target.read_bytes() == b"\x00new\xff"
    assert stat.S_IMODE(target.stat().st_mode) == FILE_MODE
    assert temp_files(tmp_path) == []


def test_replace_in_directory_writes_everything_despite_short_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_write = os.write

    def one_byte(descriptor: int, data: Any) -> int:
        return real_write(descriptor, bytes(data[:1]))

    monkeypatch.setattr(os, "write", one_byte)

    replace_via_descriptor(tmp_path, "document.json", b"several bytes")

    assert (tmp_path / "document.json").read_bytes() == b"several bytes"


def test_replace_in_directory_uses_an_unpredictable_temporary_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[str] = []
    real_open = os.open

    def recording_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if flags & os.O_CREAT:
            created.append(str(path))
            assert flags & os.O_EXCL and flags & os.O_NOFOLLOW
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", recording_open)

    replace_via_descriptor(tmp_path, "document.json", b"a")
    replace_via_descriptor(tmp_path, "document.json", b"b")

    assert len(created) == 2 and created[0] != created[1]
    for name in created:
        assert name.startswith("document.json.") and name.endswith(TEMP_SUFFIX)
        assert len(name) >= len("document.json.") + 16 + len(TEMP_SUFFIX)


def test_replace_in_directory_does_not_follow_a_symlink_at_the_temporary_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even a planted symlink at the exact temporary name is refused, not written through."""
    outside = tmp_path / "outside"
    outside.write_bytes(b"untouched")
    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setattr("remote_energy_meter.atomic_file.secrets.token_hex", lambda size: "0" * (2 * size))
    (store / f"document.json.{'0' * 16}{TEMP_SUFFIX}").symlink_to(outside)

    with pytest.raises(OSError):
        replace_via_descriptor(store, "document.json", b"new")

    assert outside.read_bytes() == b"untouched"
    assert not (store / "document.json").exists()


@pytest.mark.parametrize("failing", ["fsync_file", "replace"])
def test_replace_in_directory_failing_before_the_rename_keeps_the_previous_document_and_removes_debris(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    target = tmp_path / "document.json"
    target.write_bytes(b"previous")
    real_fsync = os.fsync

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise OSError(5, "Input/output error")

    def refuse_files(descriptor: int) -> None:
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            refuse()
        real_fsync(descriptor)

    if failing == "replace":
        monkeypatch.setattr(os, "replace", refuse)
    else:
        monkeypatch.setattr(os, "fsync", refuse_files)

    with pytest.raises(OSError) as caught:
        replace_via_descriptor(tmp_path, "document.json", b"new")

    assert not isinstance(caught.value, UnconfirmedReplaceError)
    monkeypatch.undo()
    assert target.read_bytes() == b"previous"
    assert temp_files(tmp_path) == []


def test_replace_in_directory_reports_only_a_post_rename_sync_failure_as_unconfirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "document.json"
    target.write_bytes(b"previous")
    real_fsync = os.fsync

    def refuse_directories(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(22, "Invalid argument")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", refuse_directories)

    with pytest.raises(UnconfirmedReplaceError) as caught:
        replace_via_descriptor(tmp_path, "document.json", b"new")

    assert caught.value.errno == 22
    monkeypatch.undo()
    assert target.read_bytes() == b"new"
    assert temp_files(tmp_path) == []
