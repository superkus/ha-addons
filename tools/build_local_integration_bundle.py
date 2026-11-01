#!/usr/bin/env python3
"""Build the separately installed Home Assistant companion integration artifact."""

from __future__ import annotations

import argparse
import hashlib
import shutil
import stat
import tempfile
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from tools.validate_integration_package import IntegrationPackageError, validate_integration_package
else:
    try:
        from tools.validate_integration_package import IntegrationPackageError, validate_integration_package
    except ModuleNotFoundError:  # direct ``python3 tools/...`` execution
        from validate_integration_package import IntegrationPackageError, validate_integration_package

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
DEFAULT_INTEGRATION_OUTPUT: Final = Path("/private/tmp/remote-energy-meter-local-integration")
INTEGRATION_MARKER_NAME: Final = ".remote-energy-meter-local-integration-builder"
INTEGRATION_MARKER_CONTENT: Final = "remote-energy-meter-local-integration-builder\nformat-version: 1\n"
INTEGRATION_ZIP_COMMENT: Final = b"remote-energy-meter-local-integration-builder:1"
INTEGRATION_MANIFEST_NAME: Final = "SHA256SUMS"
_FIXED_ZIP_TIMESTAMP: Final = (1980, 1, 1, 0, 0, 0)

LOCAL_INTEGRATION_INSTALL = """# Install the Remote Energy Meter companion integration

This artifact is separate from the add-on image and contains no credentials. It
includes the selected local brand under
`custom_components/remote_energy_meter/brand/`, supported by Home Assistant Core 2026.3 and newer. After Core
restarts, refresh the browser if a cached integration tile still shows the previous image.

1. Copy `custom_components/remote_energy_meter` from this directory into Home
   Assistant at `/config/custom_components/remote_energy_meter`.
2. Restart Home Assistant Core so it loads the custom integration.
3. Install or rebuild the Local add-on from the adjacent add-on artifact, then
   start it. The add-on posts Supervisor discovery and publishes retained MQTT
   inventory; it never copies this integration into its container.
4. Open **Settings → Devices & services**. Select the discovered **Remote Energy
   Meter** card and confirm setup. Use manual setup with base topic
   `remote_energy_meter` only if Supervisor discovery is unavailable.

The adjacent zip has the same layout. Extract it under `/config` so the final
manifest path is `/config/custom_components/remote_energy_meter/manifest.json`.
GitHub/HACS distribution can be added later; it is not required for local use.
"""


class IntegrationBundleError(RuntimeError):
    """The integration artifact could not be built safely."""


@dataclass(frozen=True, slots=True)
class IntegrationBundleResult:
    output_dir: Path
    archive_path: Path
    manifest_path: Path
    package_path: Path
    file_count: int
    archive_size: int


def _validate_output(output: str | Path) -> Path:
    candidate = Path(output).expanduser()
    if not candidate.is_absolute() or ".." in candidate.parts or candidate.is_symlink():
        raise IntegrationBundleError("integration output must be an absolute, non-symlink local path")
    try:
        resolved = candidate.resolve()
    except OSError as error:
        raise IntegrationBundleError("integration output cannot be resolved") from error
    allowed = (Path.home().resolve(), Path(tempfile.gettempdir()).resolve(), Path("/private/tmp"))
    if not any(root in resolved.parents for root in allowed):
        raise IntegrationBundleError("integration output is outside approved local home/temp roots")
    return resolved


def _archive_path(output: Path) -> Path:
    return output.with_name(f"{output.name}.zip")


def _assert_replaceable(output: Path) -> None:
    if not (output.exists() or output.is_symlink()):
        return
    marker = output / INTEGRATION_MARKER_NAME
    if output.is_symlink() or not output.is_dir() or not marker.is_file() or marker.is_symlink():
        raise IntegrationBundleError("refusing to replace an unmarked integration directory")
    if marker.read_text(encoding="utf-8") != INTEGRATION_MARKER_CONTENT:
        raise IntegrationBundleError("refusing to replace an integration directory with an invalid marker")


def _archive_owned(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as archive:
            return (
                archive.comment == INTEGRATION_ZIP_COMMENT
                and archive.read(INTEGRATION_MARKER_NAME).decode("utf-8") == INTEGRATION_MARKER_CONTENT
            )
    except (OSError, KeyError, UnicodeDecodeError, zipfile.BadZipFile):
        return False


def _assert_archive_replaceable(path: Path) -> None:
    if not (path.exists() or path.is_symlink()):
        return
    if path.is_symlink() or not path.is_file() or not _archive_owned(path):
        raise IntegrationBundleError("refusing to replace an unowned integration archive")


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def _copy_package(source: Path, destination: Path) -> None:
    validate_integration_package(source)
    destination.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.rglob("*"), key=lambda item: item.relative_to(source).as_posix()):
        relative = path.relative_to(source)
        if any(part.startswith("._") for part in relative.parts):
            continue
        target = destination / relative
        if path.is_symlink():
            raise IntegrationBundleError("integration source contains a symbolic link")
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target, follow_symlinks=False)
        else:
            raise IntegrationBundleError("integration source contains a special file")


def _regular_files(root: Path) -> Iterator[Path]:
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_symlink():
            raise IntegrationBundleError("staged integration contains a symbolic link")
        if path.is_file():
            yield path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_manifest(staging: Path) -> Path:
    manifest = staging / INTEGRATION_MANIFEST_NAME
    lines = [
        f"{_sha256(path)}  {path.relative_to(staging).as_posix()}"
        for path in _regular_files(staging)
        if path != manifest
    ]
    _write_text(manifest, "\n".join(lines) + "\n")
    return manifest


def _write_zip(staging: Path, archive_path: Path) -> None:
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.comment = INTEGRATION_ZIP_COMMENT
        for path in _regular_files(staging):
            relative = path.relative_to(staging).as_posix()
            info = zipfile.ZipInfo(relative, date_time=_FIXED_ZIP_TIMESTAMP)
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | stat.S_IMODE(path.stat().st_mode)) << 16
            with path.open("rb") as source, archive.open(info, "w", force_zip64=True) as destination:
                shutil.copyfileobj(source, destination, length=1024 * 1024)


def build_integration_bundle(
    output: str | Path = DEFAULT_INTEGRATION_OUTPUT,
    *,
    repository_root: Path = REPOSITORY_ROOT,
) -> IntegrationBundleResult:
    """Build and atomically publish the custom integration directory and adjacent zip."""
    output_dir = _validate_output(output)
    archive_path = _archive_path(output_dir)
    _assert_replaceable(output_dir)
    _assert_archive_replaceable(archive_path)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.staging-", dir=output_dir.parent))
    temporary_archive: Path | None = None
    published = False
    try:
        _write_text(staging / INTEGRATION_MARKER_NAME, INTEGRATION_MARKER_CONTENT)
        package = staging / "custom_components/remote_energy_meter"
        _copy_package(repository_root.resolve() / "custom_components/remote_energy_meter", package)
        _write_text(staging / "LOCAL_INSTALL.md", LOCAL_INTEGRATION_INSTALL)
        _write_manifest(staging)
        validate_integration_package(package)

        with tempfile.NamedTemporaryFile(
            prefix=f".{output_dir.name}.archive-",
            suffix=".zip",
            dir=output_dir.parent,
            delete=False,
        ) as stream:
            temporary_archive = Path(stream.name)
        _write_zip(staging, temporary_archive)
        file_count = sum(1 for _ in _regular_files(staging))
        if output_dir.exists():
            shutil.rmtree(output_dir)
        staging.replace(output_dir)
        published = True
        temporary_archive.replace(archive_path)
        temporary_archive = None
        return IntegrationBundleResult(
            output_dir=output_dir,
            archive_path=archive_path,
            manifest_path=output_dir / INTEGRATION_MANIFEST_NAME,
            package_path=output_dir / "custom_components/remote_energy_meter",
            file_count=file_count,
            archive_size=archive_path.stat().st_size,
        )
    except IntegrationPackageError as error:
        raise IntegrationBundleError(str(error)) from error
    finally:
        if not published and staging.exists():
            shutil.rmtree(staging)
        if temporary_archive is not None and temporary_archive.exists():
            temporary_archive.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_INTEGRATION_OUTPUT)
    options = parser.parse_args()
    try:
        result = build_integration_bundle(options.output)
    except IntegrationBundleError as error:
        parser.exit(2, f"local integration bundle refused: {error}\n")
    print(f"Integration directory: {result.output_dir}")
    print(f"Integration archive: {result.archive_path}")
    print(f"Regular files: {result.file_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
