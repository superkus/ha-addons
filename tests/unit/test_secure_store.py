"""The fail-closed security store: owner-only directories and files, bounded reads, versioned envelopes."""

from __future__ import annotations

import errno
import os
import shutil
import stat
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from remote_energy_meter import secure_store
from remote_energy_meter.atomic_file import TEMP_SUFFIX
from remote_energy_meter.protocol_v1 import render_canonical_json
from remote_energy_meter.secure_store import (
    DIRECTORY_MODE,
    FILE_MODE,
    MAX_DOCUMENT_BYTES_CEILING,
    SecureStoreError,
    StoreFault,
    VersionedDocumentStore,
    ensure_private_directory,
    read_private_file,
    write_private_file,
)

STORE_ID = "test-store"
NAME = "document.json"


@pytest.fixture
def store_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "secure"
    ensure_private_directory(directory)
    return directory


def make_store(directory: Path, **overrides: Any) -> VersionedDocumentStore:
    options: dict[str, Any] = {"store_id": STORE_ID, "schema_version": 1, "max_bytes": 4096}
    options.update(overrides)
    return VersionedDocumentStore(directory, NAME, **options)


def plant(directory: Path, data: bytes, *, name: str = NAME, mode: int = 0o600) -> Path:
    """Write a file bypassing the store, so a hostile or damaged document can be planted."""
    path = directory / name
    path.write_bytes(data)
    path.chmod(mode)
    return path


def envelope(generation: int, record: Any = None, /, **overrides: Any) -> bytes:
    # Positional-only, so ``overrides`` can replace any envelope field, ``generation`` included.
    value: dict[str, Any] = {
        "generation": generation,
        "record": {} if record is None else record,
        "schema_version": 1,
        "store": STORE_ID,
    }
    value.update(overrides)
    # The store's own depth limit, so a planted envelope is rendered exactly as deep as the store accepts.
    return render_canonical_json(value, max_depth=secure_store._MAX_JSON_DEPTH)


def fault_of(operation: Callable[[], object]) -> StoreFault:
    with pytest.raises(SecureStoreError) as caught:
        operation()
    return caught.value.fault


def with_status(monkeypatch: pytest.MonkeyPatch, applies: Callable[[os.stat_result], bool], **fields: int) -> None:
    """Make os.fstat report altered fields (e.g. a foreign owner) for descriptors ``applies`` selects."""
    real_fstat = os.fstat
    order = ["st_mode", "st_ino", "st_dev", "st_nlink", "st_uid", "st_gid", "st_size"]

    def altered(descriptor: int) -> os.stat_result:
        status = real_fstat(descriptor)
        if not applies(status):
            return status
        values = list(status)
        for name, value in fields.items():
            values[order.index(name)] = value
        return os.stat_result(values)

    monkeypatch.setattr(os, "fstat", altered)


def is_regular(status: os.stat_result) -> bool:
    return stat.S_ISREG(status.st_mode)


def is_directory(status: os.stat_result) -> bool:
    return stat.S_ISDIR(status.st_mode)


def refuse_fsync(monkeypatch: pytest.MonkeyPatch, applies: Callable[[os.stat_result], bool], error: int) -> None:
    """Make os.fsync fail for descriptors ``applies`` selects, whichever helper issues the call.

    Selecting by the descriptor's file type keeps the seam meaningful however the writer is structured: the file
    fsync (regular file) always precedes the rename, the directory fsync always follows it.
    """
    real_fsync = os.fsync

    def refusing(descriptor: int) -> None:
        if applies(os.fstat(descriptor)):
            raise OSError(error, os.strerror(error))
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", refusing)


def refuse_file_creation(monkeypatch: pytest.MonkeyPatch, error: int) -> None:
    """Make every os.open that would create a file fail (tempfile.mkstemp and a dir_fd-relative open alike)."""
    real_open = os.open

    def refusing(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if flags & os.O_CREAT:
            raise OSError(error, os.strerror(error))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", refusing)


def refuse_rename(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the rename that installs a new document fail (os.replace, which Path.replace also uses)."""

    def refusing(*args: Any, **kwargs: Any) -> None:
        raise OSError(errno.EIO, os.strerror(errno.EIO))

    monkeypatch.setattr(os, "replace", refusing)


def swap_directory_once_opened(
    monkeypatch: pytest.MonkeyPatch, directory: Path, swap: Callable[[], None]
) -> list[bool]:
    """Run ``swap`` once, right after the first no-follow descriptor of ``directory`` has been opened.

    That is the window between verifying the store directory and writing into it: a writer that goes back to the path
    afterwards sees whatever ``swap`` left there. Returns a list that records that the swap happened.
    """
    real_open = os.open
    swapped: list[bool] = []

    def opening(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        descriptor = real_open(path, flags, *args, **kwargs)
        if not swapped and kwargs.get("dir_fd") is None and Path(path) == directory:
            swapped.append(True)
            swap()
        return descriptor

    monkeypatch.setattr(os, "open", opening)
    return swapped


def entry_snapshot(path: Path) -> tuple[int, int, int, str | None]:
    """The directory entry's identity as seen without following a symlink: inode, type and mode, link target."""
    status = path.lstat()
    target = str(path.readlink()) if stat.S_ISLNK(status.st_mode) else None
    return status.st_ino, stat.S_IFMT(status.st_mode), stat.S_IMODE(status.st_mode), target


needs_permission_checks = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root bypasses directory permission bits"
)


# --------------------------------------------------------------------------------------------------
# directories
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("umask", [0o000, 0o022, 0o077, 0o277, 0o377])
def test_a_new_store_directory_is_0700_under_any_umask_keeping_owner_read(tmp_path: Path, umask: int) -> None:
    directory = tmp_path / "secure"
    previous = os.umask(umask)
    try:
        ensure_private_directory(directory)
    finally:
        os.umask(previous)

    assert stat.S_IMODE(directory.stat().st_mode) == DIRECTORY_MODE == 0o700


@needs_permission_checks
def test_a_new_store_directory_its_owner_cannot_open_fails_closed(tmp_path: Path) -> None:
    """Under a umask that removes owner read, the mode could only be fixed by path, which may follow a swapped link."""
    directory = tmp_path / "secure"
    previous = os.umask(0o777)
    try:
        assert fault_of(lambda: ensure_private_directory(directory)) is StoreFault.DIRECTORY_INSECURE
    finally:
        os.umask(previous)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o000


def test_an_existing_valid_directory_is_accepted_unchanged(store_dir: Path) -> None:
    before = store_dir.stat()

    ensure_private_directory(store_dir)

    assert store_dir.stat().st_ino == before.st_ino
    assert stat.S_IMODE(store_dir.stat().st_mode) == 0o700


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o701, 0o500, 0o600, 0o2700, 0o1700])
def test_an_existing_directory_with_any_other_mode_fails_closed_and_is_not_repaired(tmp_path: Path, mode: int) -> None:
    directory = tmp_path / "secure"
    directory.mkdir()
    directory.chmod(mode)
    try:
        assert fault_of(lambda: ensure_private_directory(directory)) is StoreFault.DIRECTORY_INSECURE
        assert stat.S_IMODE(directory.stat().st_mode) == mode
    finally:
        directory.chmod(0o700)


def test_a_symlinked_store_directory_fails_closed(tmp_path: Path) -> None:
    real = tmp_path / "real"
    ensure_private_directory(real)
    link = tmp_path / "secure"
    link.symlink_to(real, target_is_directory=True)

    assert fault_of(lambda: ensure_private_directory(link)) is StoreFault.DIRECTORY_INSECURE
    assert fault_of(lambda: read_private_file(link, NAME, max_bytes=16)) is StoreFault.DIRECTORY_INSECURE
    assert fault_of(lambda: write_private_file(link, NAME, b"x")) is StoreFault.DIRECTORY_INSECURE
    assert list(real.iterdir()) == []


def test_a_file_in_place_of_the_store_directory_fails_closed(tmp_path: Path) -> None:
    impostor = tmp_path / "secure"
    impostor.write_bytes(b"")

    assert fault_of(lambda: ensure_private_directory(impostor)) is StoreFault.DIRECTORY_INSECURE
    assert fault_of(lambda: read_private_file(impostor, NAME, max_bytes=16)) is StoreFault.DIRECTORY_INSECURE


def test_a_missing_parent_is_not_created(tmp_path: Path) -> None:
    directory = tmp_path / "absent" / "secure"

    assert fault_of(lambda: ensure_private_directory(directory)) is StoreFault.DIRECTORY_INSECURE
    assert not (tmp_path / "absent").exists()


def test_a_missing_directory_fails_reads_and_writes(tmp_path: Path) -> None:
    directory = tmp_path / "secure"

    assert fault_of(lambda: read_private_file(directory, NAME, max_bytes=16)) is StoreFault.DIRECTORY_INSECURE
    assert fault_of(lambda: write_private_file(directory, NAME, b"x")) is StoreFault.DIRECTORY_INSECURE


def test_a_directory_owned_by_another_user_fails_closed(store_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with_status(monkeypatch, is_directory, st_uid=os.geteuid() + 1)

    assert fault_of(lambda: ensure_private_directory(store_dir)) is StoreFault.DIRECTORY_INSECURE
    assert fault_of(lambda: read_private_file(store_dir, NAME, max_bytes=16)) is StoreFault.DIRECTORY_INSECURE
    assert fault_of(lambda: write_private_file(store_dir, NAME, b"x")) is StoreFault.DIRECTORY_INSECURE


def test_a_failure_to_create_the_directory_is_an_io_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(self: Path, *args: Any, **kwargs: Any) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "mkdir", refuse)

    assert fault_of(lambda: ensure_private_directory(tmp_path / "secure")) is StoreFault.IO_ERROR


def test_a_failure_to_set_the_new_directory_mode_is_an_io_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise PermissionError(1, "Operation not permitted")

    # Every way of setting the mode: through a descriptor, without following a symlink, or by path.
    monkeypatch.setattr(os, "fchmod", refuse)
    monkeypatch.setattr(os, "chmod", refuse)

    assert fault_of(lambda: ensure_private_directory(tmp_path / "secure")) is StoreFault.IO_ERROR


def test_an_uninspectable_directory_is_an_io_error(store_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_open = os.open

    def refuse_directory(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if Path(path) == store_dir:
            raise PermissionError(13, "Permission denied")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", refuse_directory)

    assert fault_of(lambda: ensure_private_directory(store_dir)) is StoreFault.IO_ERROR
    assert fault_of(lambda: read_private_file(store_dir, NAME, max_bytes=16)) is StoreFault.IO_ERROR


@needs_permission_checks
@pytest.mark.parametrize("mode", [0o000, 0o100, 0o200, 0o300], ids=lambda mode: f"{mode:04o}")
def test_a_directory_its_owner_cannot_read_is_insecure_not_an_io_error(tmp_path: Path, mode: int) -> None:
    """Without owner read the directory cannot be opened; that is a mode mismatch, the same as any other wrong mode."""
    directory = tmp_path / "secure"
    ensure_private_directory(directory)
    write_private_file(directory, NAME, envelope(1))
    store = make_store(directory)
    directory.chmod(mode)
    try:
        assert fault_of(lambda: ensure_private_directory(directory)) is StoreFault.DIRECTORY_INSECURE
        assert fault_of(lambda: read_private_file(directory, NAME, max_bytes=4096)) is StoreFault.DIRECTORY_INSECURE
        assert fault_of(lambda: write_private_file(directory, NAME, b"x")) is StoreFault.DIRECTORY_INSECURE
        assert fault_of(store.load) is StoreFault.DIRECTORY_INSECURE
        assert stat.S_IMODE(directory.lstat().st_mode) == mode
    finally:
        directory.chmod(0o700)
    assert read_private_file(directory, NAME, max_bytes=4096) == envelope(1)


# --------------------------------------------------------------------------------------------------
# files
# --------------------------------------------------------------------------------------------------


def test_write_then_read_round_trips_exact_bytes_with_mode_0600(store_dir: Path) -> None:
    write_private_file(store_dir, NAME, b"\x00exact\xff")

    assert read_private_file(store_dir, NAME, max_bytes=16) == b"\x00exact\xff"
    assert stat.S_IMODE((store_dir / NAME).stat().st_mode) == FILE_MODE == 0o600
    assert [entry.name for entry in store_dir.iterdir()] == [NAME]


def test_a_missing_document_reads_as_none(store_dir: Path) -> None:
    assert read_private_file(store_dir, NAME, max_bytes=16) is None


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o400, 0o700, 0o4600, 0o000])
def test_a_document_with_any_other_mode_fails_closed(store_dir: Path, mode: int) -> None:
    plant(store_dir, b"x", mode=mode)

    assert fault_of(lambda: read_private_file(store_dir, NAME, max_bytes=16)) is StoreFault.FILE_INSECURE


def test_a_symlinked_document_fails_closed_and_is_not_followed(store_dir: Path, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.write_bytes(b"secret")
    target.chmod(0o600)
    (store_dir / NAME).symlink_to(target)

    assert fault_of(lambda: read_private_file(store_dir, NAME, max_bytes=16)) is StoreFault.FILE_INSECURE


def test_a_fifo_in_place_of_a_document_fails_closed_without_blocking(store_dir: Path) -> None:
    os.mkfifo(store_dir / NAME, 0o600)

    assert fault_of(lambda: read_private_file(store_dir, NAME, max_bytes=16)) is StoreFault.FILE_INSECURE


def test_a_directory_in_place_of_a_document_fails_closed(store_dir: Path) -> None:
    (store_dir / NAME).mkdir(mode=0o600)

    assert fault_of(lambda: read_private_file(store_dir, NAME, max_bytes=16)) is StoreFault.FILE_INSECURE


def test_a_document_owned_by_another_user_fails_closed(store_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plant(store_dir, b"x")
    with_status(monkeypatch, is_regular, st_uid=os.geteuid() + 1)

    assert fault_of(lambda: read_private_file(store_dir, NAME, max_bytes=16)) is StoreFault.FILE_INSECURE


def test_a_document_at_the_cap_is_read_and_one_byte_over_is_rejected(store_dir: Path) -> None:
    plant(store_dir, b"a" * 16)
    assert read_private_file(store_dir, NAME, max_bytes=16) == b"a" * 16

    plant(store_dir, b"a" * 17)
    assert fault_of(lambda: read_private_file(store_dir, NAME, max_bytes=16)) is StoreFault.OVERSIZE


def test_a_document_that_grows_after_fstat_is_still_capped(store_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The read stops one byte past the cap even when fstat reported a smaller size."""
    plant(store_dir, b"a" * 64)
    with_status(monkeypatch, is_regular, st_size=1)

    assert fault_of(lambda: read_private_file(store_dir, NAME, max_bytes=16)) is StoreFault.OVERSIZE


def test_a_read_failure_is_an_io_error(store_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plant(store_dir, b"x")

    def refuse(descriptor: int, size: int) -> bytes:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(os, "read", refuse)

    assert fault_of(lambda: read_private_file(store_dir, NAME, max_bytes=16)) is StoreFault.IO_ERROR


def test_an_unopenable_document_is_an_io_error(store_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plant(store_dir, b"x")
    real_open = os.open

    def refuse_document(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if path == NAME:
            raise OSError(5, "Input/output error")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", refuse_document)

    assert fault_of(lambda: read_private_file(store_dir, NAME, max_bytes=16)) is StoreFault.IO_ERROR


def test_an_io_error_detail_names_the_error_type_even_without_an_errno(
    store_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Like the other stores' details, an IO_ERROR names the exception type, so an errno-less OSError never reads
    as ``None``."""
    plant(store_dir, b"x")

    def refuse(descriptor: int, size: int) -> bytes:
        raise OSError("no errno")

    monkeypatch.setattr(os, "read", refuse)

    with pytest.raises(SecureStoreError) as caught:
        read_private_file(store_dir, NAME, max_bytes=16)
    assert caught.value.fault is StoreFault.IO_ERROR
    assert caught.value.detail == f"cannot read {store_dir / NAME}: OSError"


def test_an_unconfirmed_write_detail_names_the_failed_sync_not_the_wrapper(
    store_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    refuse_fsync(monkeypatch, is_directory, errno.EINVAL)

    with pytest.raises(SecureStoreError) as caught:
        write_private_file(store_dir, NAME, b"x")
    assert caught.value.fault is StoreFault.IO_ERROR
    assert caught.value.detail == f"replaced {store_dir / NAME} but cannot sync {store_dir}: OSError"


@pytest.mark.parametrize("error_number", [13, 1])
def test_a_document_its_owner_cannot_open_is_insecure(
    store_dir: Path, monkeypatch: pytest.MonkeyPatch, error_number: int
) -> None:
    """EACCES/EPERM on the owner's own document means its mode is wrong, even when running as root hides that."""
    plant(store_dir, b"x")
    real_open = os.open

    def refuse_document(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if path == NAME:
            raise OSError(error_number, os.strerror(error_number))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", refuse_document)

    assert fault_of(lambda: read_private_file(store_dir, NAME, max_bytes=16)) is StoreFault.FILE_INSECURE


def test_a_write_failure_is_an_io_error_and_keeps_the_previous_document(
    store_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_private_file(store_dir, NAME, b"previous")

    refuse_rename(monkeypatch)

    assert fault_of(lambda: write_private_file(store_dir, NAME, b"new")) is StoreFault.IO_ERROR
    monkeypatch.undo()
    assert read_private_file(store_dir, NAME, max_bytes=16) == b"previous"
    assert [entry.name for entry in store_dir.iterdir()] == [NAME]


def test_a_failed_directory_sync_fails_the_write(store_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_fsync = os.fsync

    def refuse_directories(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(22, "Invalid argument")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", refuse_directories)

    assert fault_of(lambda: write_private_file(store_dir, NAME, b"x")) is StoreFault.IO_ERROR


@pytest.mark.parametrize("kind", ["mode_0644", "fifo", "symlink"])
def test_a_write_does_not_replace_an_insecure_existing_entry(store_dir: Path, tmp_path: Path, kind: str) -> None:
    """An insecure document is evidence of tampering; a write must fail closed instead of silently replacing it."""
    target = tmp_path / "elsewhere"
    target.write_bytes(b"outside the store")
    target.chmod(0o600)
    path = store_dir / NAME
    if kind == "mode_0644":
        plant(store_dir, b"widened", mode=0o644)
    elif kind == "fifo":
        os.mkfifo(path, 0o600)
    else:
        path.symlink_to(target)
    before = entry_snapshot(path)

    assert fault_of(lambda: write_private_file(store_dir, NAME, b"new")) is StoreFault.FILE_INSECURE

    assert entry_snapshot(path) == before
    if kind == "mode_0644":
        assert path.read_bytes() == b"widened"
    assert target.read_bytes() == b"outside the store"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert sorted(entry.name for entry in store_dir.iterdir()) == [NAME]


def test_a_store_directory_removed_after_verification_is_not_recreated_by_a_write(
    store_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write goes through the verified descriptor; it never recreates a directory (with the umask's mode)."""
    swapped = swap_directory_once_opened(monkeypatch, store_dir, lambda: shutil.rmtree(store_dir))

    with pytest.raises(SecureStoreError):
        write_private_file(store_dir, NAME, b"new")

    assert swapped
    assert not store_dir.exists()


def test_a_store_directory_swapped_for_a_symlink_after_verification_is_not_followed_by_a_write(
    store_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Swapping the verified directory for a symlink to another private directory cannot redirect the write."""
    other = tmp_path / "other"
    ensure_private_directory(other)
    moved = tmp_path / "moved"

    def swap() -> None:
        store_dir.rename(moved)
        store_dir.symlink_to(other, target_is_directory=True)

    swapped = swap_directory_once_opened(monkeypatch, store_dir, swap)

    write_private_file(store_dir, NAME, b"new")

    assert swapped
    assert list(other.iterdir()) == []
    # The write landed in the directory that was verified, now at ``moved``.
    assert (moved / NAME).read_bytes() == b"new"
    assert [entry.name for entry in moved.iterdir()] == [NAME]


def test_a_new_directory_swapped_for_a_symlink_before_its_mode_is_set_is_not_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Setting the new directory's mode must not chmod the target of a symlink swapped in after mkdir."""
    directory = tmp_path / "secure"
    target = tmp_path / "shared"
    target.mkdir()
    target.chmod(0o755)
    real_mkdir = Path.mkdir

    def mkdir_then_swap(self: Path, *args: Any, **kwargs: Any) -> None:
        real_mkdir(self, *args, **kwargs)
        if self == directory:
            self.rmdir()
            self.symlink_to(target, target_is_directory=True)

    monkeypatch.setattr(Path, "mkdir", mkdir_then_swap)

    assert fault_of(lambda: ensure_private_directory(directory)) is StoreFault.DIRECTORY_INSECURE
    assert stat.S_IMODE(target.stat().st_mode) == 0o755


@pytest.mark.parametrize(
    "name",
    ["", ".", "..", "a/b", "../x", "/abs", "Upper.json", "-dash", "x" * 65, "doc.tmp", "with space", "nul\x00"],
)
def test_document_names_must_be_one_plain_component(store_dir: Path, name: str) -> None:
    with pytest.raises(ValueError, match="plain lowercase"):
        read_private_file(store_dir, name, max_bytes=16)
    with pytest.raises(ValueError, match="plain lowercase"):
        write_private_file(store_dir, name, b"x")


@pytest.mark.parametrize("max_bytes", [0, -1, MAX_DOCUMENT_BYTES_CEILING + 1])
def test_read_caps_must_be_in_range(store_dir: Path, max_bytes: int) -> None:
    with pytest.raises(ValueError, match="max_bytes"):
        read_private_file(store_dir, NAME, max_bytes=max_bytes)


def test_error_details_never_contain_document_content(store_dir: Path) -> None:
    plant(store_dir, b"TOP-SECRET-MARKER" * 4)

    with pytest.raises(SecureStoreError) as caught:
        read_private_file(store_dir, NAME, max_bytes=16)

    assert "TOP-SECRET-MARKER" not in str(caught.value)
    assert caught.value.fault is StoreFault.OVERSIZE


SECRET_MARKER = "TOP-SECRET-MARKER"


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        (SECRET_MARKER.encode() * 4, StoreFault.MALFORMED),
        (b'{"secret":"' + SECRET_MARKER.encode() + b'"', StoreFault.MALFORMED),
        (envelope(1, {"token": SECRET_MARKER}, extra=SECRET_MARKER), StoreFault.MALFORMED),
        (envelope(1, {"token": SECRET_MARKER}, generation=SECRET_MARKER), StoreFault.MALFORMED),
        (envelope(1, {"token": SECRET_MARKER}, schema_version=SECRET_MARKER), StoreFault.MALFORMED),
        (envelope(1, SECRET_MARKER), StoreFault.MALFORMED),
        (envelope(1, {"token": SECRET_MARKER}, schema_version=2), StoreFault.FOREIGN_SCHEMA),
        (envelope(1, {"token": SECRET_MARKER}, store="other-store"), StoreFault.WRONG_STORE),
        (envelope(1, {"token": SECRET_MARKER}, store=SECRET_MARKER.lower()), StoreFault.WRONG_STORE),
    ],
)
def test_load_error_details_never_contain_document_content(
    store_dir: Path, document: bytes, expected: StoreFault
) -> None:
    plant(store_dir, document)

    with pytest.raises(SecureStoreError) as caught:
        make_store(store_dir).load()

    error = caught.value
    assert error.fault is expected
    for rendered in (str(error), repr(error), error.detail, *map(str, error.args)):
        assert SECRET_MARKER not in rendered
        assert SECRET_MARKER.lower() not in rendered


def test_save_error_details_never_contain_record_content(store_dir: Path) -> None:
    store = make_store(store_dir, max_bytes=128)

    for record in ({"token": SECRET_MARKER, "bad": 1.5}, {"token": SECRET_MARKER * 10}):
        with pytest.raises(SecureStoreError) as caught:
            store.save(cast(Any, record))
        error = caught.value
        assert error.fault is StoreFault.MALFORMED
        for rendered in (str(error), repr(error), error.detail, *map(str, error.args)):
            assert SECRET_MARKER not in rendered


# --------------------------------------------------------------------------------------------------
# versioned documents
# --------------------------------------------------------------------------------------------------


def test_a_store_that_was_never_written_loads_as_none_and_creates_its_directory(tmp_path: Path) -> None:
    directory = tmp_path / "secure"
    store = make_store(directory)

    assert store.load() is None
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert store.path == directory / NAME


def test_saves_increment_the_generation_by_exactly_one(store_dir: Path) -> None:
    store = make_store(store_dir)

    first = store.save({"value": 1})
    second = store.save({"value": 2})

    assert (first.generation, dict(first.record)) == (1, {"value": 1})
    assert (second.generation, dict(second.record)) == (2, {"value": 2})
    assert store.path.read_bytes() == envelope(2, {"value": 2})


def test_a_new_instance_reads_what_an_earlier_one_saved(store_dir: Path) -> None:
    make_store(store_dir).save({"name": "server_a", "count": 3})
    make_store(store_dir).save({"name": "server_a", "count": 4})

    loaded = make_store(store_dir).load()

    assert loaded is not None
    assert loaded.generation == 2
    assert dict(loaded.record) == {"name": "server_a", "count": 4}


def test_a_save_without_a_prior_load_continues_from_the_disk_generation(store_dir: Path) -> None:
    plant(store_dir, envelope(41))

    assert make_store(store_dir).save({}).generation == 42


def test_an_older_document_restored_under_a_running_store_is_a_rollback(store_dir: Path) -> None:
    store = make_store(store_dir)
    store.save({"value": 1})
    old = store.path.read_bytes()
    store.save({"value": 2})

    plant(store_dir, old)

    assert fault_of(store.load) is StoreFault.GENERATION_ROLLBACK
    assert fault_of(lambda: store.save({"value": 3})) is StoreFault.GENERATION_ROLLBACK
    assert store.path.read_bytes() == old


def test_a_document_that_disappears_under_a_running_store_is_a_rollback(store_dir: Path) -> None:
    store = make_store(store_dir)
    store.save({})
    store.path.unlink()

    assert fault_of(store.load) is StoreFault.GENERATION_ROLLBACK


def test_a_second_writer_is_detected_before_anything_is_overwritten(store_dir: Path) -> None:
    first = make_store(store_dir)
    second = make_store(store_dir)
    first.save({"writer": "first"})
    second.save({"writer": "second"})

    assert fault_of(lambda: first.save({"writer": "first again"})) is StoreFault.CONCURRENT_MODIFICATION
    loaded = make_store(store_dir).load()
    assert loaded is not None
    assert dict(loaded.record) == {"writer": "second"}


def test_an_unconfirmed_save_accepts_either_outcome_on_the_next_load(
    store_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed directory sync after the rename leaves the new generation on disk; that is not a second writer.

    The rename is not durable until a directory sync succeeds, so a load while the sync still fails cannot confirm
    generation 2 (a power cut could still bring generation 1 back). Once the sync succeeds, generation 2 is accepted.
    """
    store = make_store(store_dir)
    store.save({"value": 1})
    refuse_fsync(monkeypatch, is_directory, errno.EINVAL)
    assert fault_of(lambda: store.save({"value": 2})) is StoreFault.IO_ERROR

    assert fault_of(store.load) is StoreFault.IO_ERROR
    assert fault_of(store.load) is StoreFault.IO_ERROR
    monkeypatch.undo()

    loaded = store.load()
    assert loaded is not None
    assert (loaded.generation, dict(loaded.record)) == (2, {"value": 2})
    assert store.save({"value": 3}).generation == 3
    assert store.path.read_bytes() == envelope(3, {"value": 3})


@pytest.mark.parametrize("seam", ["temp_file_creation", "file_fsync"])
def test_a_save_that_fails_before_the_rename_does_not_accept_a_foreign_next_generation(
    store_dir: Path, monkeypatch: pytest.MonkeyPatch, seam: str
) -> None:
    """Only a failure after the rename may leave the new generation on disk; an earlier one leaves nothing to accept."""
    first = make_store(store_dir)
    first.save({"writer": "first"})
    if seam == "temp_file_creation":
        refuse_file_creation(monkeypatch, errno.ENOSPC)
    else:
        refuse_fsync(monkeypatch, is_regular, errno.ENOSPC)
    assert fault_of(lambda: first.save({"writer": "first again"})) is StoreFault.IO_ERROR
    monkeypatch.undo()
    assert store_dir.joinpath(NAME).read_bytes() == envelope(1, {"writer": "first"})

    assert make_store(store_dir).save({"writer": "second"}).generation == 2

    assert fault_of(first.load) is StoreFault.CONCURRENT_MODIFICATION
    assert fault_of(lambda: first.save({"writer": "first"})) is StoreFault.CONCURRENT_MODIFICATION
    assert store_dir.joinpath(NAME).read_bytes() == envelope(2, {"writer": "second"})


def test_a_store_directory_removed_under_a_running_store_is_a_rollback_and_is_not_recreated(store_dir: Path) -> None:
    store = make_store(store_dir)
    store.save({"value": 1})
    shutil.rmtree(store_dir)

    assert fault_of(store.load) is StoreFault.GENERATION_ROLLBACK
    assert not store_dir.exists()
    assert fault_of(lambda: store.save({"value": 2})) is StoreFault.GENERATION_ROLLBACK
    assert not store_dir.exists()


def test_an_unconfirmed_save_whose_rename_did_not_happen_keeps_the_previous_generation(
    store_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(store_dir)
    store.save({"value": 1})

    refuse_rename(monkeypatch)
    assert fault_of(lambda: store.save({"value": 2})) is StoreFault.IO_ERROR
    monkeypatch.undo()

    loaded = store.load()
    assert loaded is not None
    assert (loaded.generation, dict(loaded.record)) == (1, {"value": 1})
    assert store.save({"value": 2}).generation == 2


def test_a_non_io_save_failure_does_not_relax_the_generation_check(store_dir: Path, tmp_path: Path) -> None:
    store = make_store(store_dir)
    store.save({})
    # A save refused before writing (unrenderable record) must not let a foreign generation 2 through afterwards.
    assert fault_of(lambda: store.save(cast(Any, {"bad": 1.5}))) is StoreFault.MALFORMED
    plant(store_dir, envelope(2))

    assert fault_of(store.load) is StoreFault.CONCURRENT_MODIFICATION


@pytest.mark.parametrize(
    "document",
    [
        b"",
        b"not json",
        b'{"generation":1}',
        b'{ "generation":1,"record":{},"schema_version":1,"store":"test-store"}',
        b'{"record":{},"generation":1,"schema_version":1,"store":"test-store"}',
        b"[]",
        b'"string"',
    ],
)
def test_a_document_that_is_not_a_canonical_envelope_is_malformed(store_dir: Path, document: bytes) -> None:
    plant(store_dir, document)

    assert fault_of(make_store(store_dir).load) is StoreFault.MALFORMED


@pytest.mark.parametrize(
    "overrides",
    [
        {"generation": 0},
        {"generation": "1"},
        {"generation": True},
        {"schema_version": "1"},
        {"store": 5},
        {"record": []},
        {"record": "x"},
        {"extra": 1},
    ],
)
def test_an_envelope_with_a_wrong_field_type_or_value_is_malformed(store_dir: Path, overrides: dict[str, Any]) -> None:
    plant(store_dir, envelope(1, **overrides))

    assert fault_of(make_store(store_dir).load) is StoreFault.MALFORMED


def test_a_foreign_schema_version_is_reported_as_such(store_dir: Path) -> None:
    plant(store_dir, envelope(1, schema_version=2))

    assert fault_of(make_store(store_dir).load) is StoreFault.FOREIGN_SCHEMA


def test_a_document_from_another_store_is_rejected(store_dir: Path) -> None:
    plant(store_dir, envelope(1, store="other-store"))

    assert fault_of(make_store(store_dir).load) is StoreFault.WRONG_STORE


def test_an_unrenderable_or_oversize_record_is_refused_before_writing(store_dir: Path) -> None:
    store = make_store(store_dir, max_bytes=128)

    assert fault_of(lambda: store.save(cast(Any, {"float": 1.5}))) is StoreFault.MALFORMED
    assert fault_of(lambda: store.save({"negative": -1})) is StoreFault.MALFORMED
    assert fault_of(lambda: store.save({"long": "x" * 200})) is StoreFault.MALFORMED
    assert not store.path.exists()


def test_the_generation_cannot_wrap(store_dir: Path) -> None:
    plant(store_dir, envelope(2**64 - 1))
    store = make_store(store_dir)

    assert fault_of(lambda: store.save({})) is StoreFault.GENERATION_EXHAUSTED
    loaded = store.load()
    assert loaded is not None
    assert loaded.generation == 2**64 - 1


def test_an_insecure_document_fails_the_store_closed(store_dir: Path) -> None:
    store = make_store(store_dir)
    store.save({})
    store.path.chmod(0o644)

    assert fault_of(store.load) is StoreFault.FILE_INSECURE
    assert fault_of(lambda: store.save({})) is StoreFault.FILE_INSECURE
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o644


def test_leftover_temporary_files_of_the_document_are_removed_on_load(store_dir: Path) -> None:
    """Debris from an interrupted write can hold newer record bytes; it is never read, and it is deleted."""
    store = make_store(store_dir)
    store.save({"value": 1})
    leftover = plant(store_dir, b"garbage from an interrupted write", name=f"{NAME}.0123456789abcdef{TEMP_SUFFIX}")
    unrelated = [
        plant(store_dir, b"x", name=f"other.json.0123456789abcdef{TEMP_SUFFIX}"),
        plant(store_dir, b"x", name=f"{NAME}.short{TEMP_SUFFIX}"),
        plant(store_dir, b"x", name="notes.txt"),
    ]

    loaded = make_store(store_dir).load()

    assert loaded is not None
    assert dict(loaded.record) == {"value": 1}
    assert not leftover.exists()
    assert all(path.exists() for path in unrelated)


def test_a_leftover_symlink_with_a_temporary_name_is_unlinked_not_followed(store_dir: Path, tmp_path: Path) -> None:
    target = tmp_path / "outside"
    target.write_bytes(b"must survive")
    (store_dir / f"{NAME}.0123456789abcdef{TEMP_SUFFIX}").symlink_to(target)

    assert make_store(store_dir).load() is None

    assert not (store_dir / f"{NAME}.0123456789abcdef{TEMP_SUFFIX}").is_symlink()
    assert target.read_bytes() == b"must survive"


def test_a_leftover_that_cannot_be_removed_fails_the_load(store_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plant(store_dir, b"x", name=f"{NAME}.0123456789abcdef{TEMP_SUFFIX}")

    def refuse(path: Any, *args: Any, **kwargs: Any) -> None:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(os, "unlink", refuse)

    assert fault_of(make_store(store_dir).load) is StoreFault.IO_ERROR


def test_a_directory_that_vanished_before_anything_was_written_is_recreated(tmp_path: Path) -> None:
    """Without a known generation, a missing directory is still first use, not a rollback."""
    directory = tmp_path / "secure"
    store = make_store(directory)
    assert store.load() is None
    directory.rmdir()

    saved = store.save({"value": 1})

    assert saved.generation == 1
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_concurrent_saves_on_one_instance_are_serialized(store_dir: Path) -> None:
    store = make_store(store_dir)
    generations: list[int] = []
    lock = threading.Lock()

    def save_many() -> None:
        for _ in range(10):
            saved = store.save({})
            with lock:
                generations.append(saved.generation)

    threads = [threading.Thread(target=save_many) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(generations) == list(range(1, 41))


@pytest.mark.parametrize(
    "overrides",
    [
        {"store_id": "Bad Id"},
        {"schema_version": 0},
        {"max_bytes": 0},
        {"max_bytes": MAX_DOCUMENT_BYTES_CEILING + 1},
    ],
)
def test_store_construction_validates_its_parameters(store_dir: Path, overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        make_store(store_dir, **overrides)


def test_store_construction_validates_the_document_name(store_dir: Path) -> None:
    with pytest.raises(ValueError, match="plain lowercase"):
        VersionedDocumentStore(store_dir, "../escape", store_id=STORE_ID, schema_version=1, max_bytes=64)


def test_the_module_exports_its_public_names() -> None:
    for name in secure_store.__all__:
        assert hasattr(secure_store, name)
