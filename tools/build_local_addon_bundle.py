#!/usr/bin/env python3
"""Build a self-contained Home Assistant Local add-on bundle on local storage."""

from __future__ import annotations

import argparse
import hashlib
import shutil
import stat
import tempfile
import zipfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from tools.build_local_integration_bundle import (
        DEFAULT_INTEGRATION_OUTPUT,
        IntegrationBundleError,
        build_integration_bundle,
    )
else:
    try:
        from tools.build_local_integration_bundle import (
            DEFAULT_INTEGRATION_OUTPUT,
            IntegrationBundleError,
            build_integration_bundle,
        )
    except ModuleNotFoundError:  # direct ``python3 tools/...`` execution
        from build_local_integration_bundle import (
            DEFAULT_INTEGRATION_OUTPUT,
            IntegrationBundleError,
            build_integration_bundle,
        )

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT: Final = Path("/private/tmp/remote-energy-meter-local-addon")
BUILDER_MARKER_NAME: Final = ".remote-energy-meter-local-addon-builder"
BUILDER_MARKER_CONTENT: Final = "remote-energy-meter-local-addon-builder\nformat-version: 1\n"
MANIFEST_NAME: Final = "SHA256SUMS"
ZIP_COMMENT: Final = b"remote-energy-meter-local-addon-builder:1"

_EXCLUDED_DIRECTORY_NAMES: Final = frozenset(
    {
        ".appledouble",
        ".git",
        ".github",
        ".hypothesis",
        ".kiro",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "tests",
    }
)
_EXCLUDED_FILE_NAMES: Final = frozenset(
    {
        ".ds_store",
        ".env",
        ".gitattributes",
        ".gitignore",
        "conftest.py",
        "credentials.json",
        "credentials.yaml",
        "credentials.yml",
        "idrac-probe.local.json",
        "secrets.yaml",
        "secrets.yml",
    }
)
_EXECUTABLE_PATHS: Final = (
    Path("rootfs/etc/s6-overlay/s6-rc.d/remote-energy-meter/run"),
    Path("rootfs/etc/s6-overlay/s6-rc.d/remote-energy-meter/finish"),
)
_FIXED_ZIP_TIMESTAMP: Final = (1980, 1, 1, 0, 0, 0)

_LOCAL_INSTALL = """# Install Remote Energy Meter as a Local add-on

This directory is a self-contained Home Assistant add-on build context. It was
created from the repository's allowlisted packaging inputs and contains no
credentials. The builder never reads or copies Home Assistant `secrets.yaml`.

`icon.png` and `logo.png` at this directory root are Supervisor presentation
metadata. They remain outside the add-on image filesystem because the Dockerfile
copies only the Python package and `rootfs`. The companion integration and its
Core 2026.3+ local brand remain a separately installed artifact. After replacing
this directory, reload Local add-ons and refresh the browser so Supervisor and the
frontend re-read the selected artwork.

## Prerequisites

- Use a Supervisor-managed Home Assistant installation that supports add-ons.
- Ensure the Mosquitto broker and Home Assistant MQTT integration are ready.
- Before starting the add-on, create the secret values referenced by
  `config.yaml` in Home Assistant `/config/secrets.yaml`, or replace the secret
  reference names in the add-on Configuration tab with names from your own
  secret store. The shipped device defaults reference `idrac_a_password` and
  `idrac_b_password`; no values for those references are in this bundle.
- Keep the Redfish and IPMI management endpoints on a trusted management
  network. See `DOCS.md` for protocol and least-privilege guidance.

## Install

1. First copy the separately generated
   `/private/tmp/remote-energy-meter-local-integration/custom_components/remote_energy_meter`
   directory to Home Assistant at `/config/custom_components/remote_energy_meter`.
2. Restart Home Assistant Core so the companion integration is loaded.
3. Copy the **contents of this directory** to the Home Assistant host at exactly
   `/addons/remote_energy_meter`. After copying,
   `/addons/remote_energy_meter/config.yaml` must exist.
4. In Home Assistant, open **Settings → Add-ons → Add-on Store**.
5. Open the store menu and select **Check for updates** (or **Reload** on
   versions that use that label) to reload **Local add-ons**.
6. Open **Remote Energy Meter** under **Local add-ons** and select **Install**
   or **Rebuild**, then review its Configuration tab and secret references.
7. Start the add-on. In **Settings → Devices & services**, open the discovered
   **Remote Energy Meter** card and confirm setup. Supervisor discovery failure
   is nonfatal; manual setup needs only the configured MQTT base topic.
8. Confirm the `idrac` and `ipmi` plugins and expected device count in the add-on log.

The add-on image does not contain or install the custom integration. The adjacent
zip is a transport copy of the same add-on files. Extract it so that `config.yaml`
is directly under `/addons/remote_energy_meter`, not inside an extra nested directory.
GitHub/HACS distribution is an optional later path.
"""


class BundleError(RuntimeError):
    """Base error for a bundle that cannot be built safely."""


class BundleSafetyError(BundleError):
    """The requested output cannot be created or replaced safely."""


@dataclass(frozen=True, slots=True)
class BundleResult:
    """Paths and counts for a completed bundle build."""

    output_dir: Path
    archive_path: Path
    manifest_path: Path
    file_count: int
    archive_size: int


def _allowed_local_bases() -> tuple[Path, ...]:
    candidates = (Path.home(), Path(tempfile.gettempdir()), Path("/private/tmp"), Path("/private/var/folders"))
    roots: list[Path] = []
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in roots:
            continue
        roots.append(resolved)
    return tuple(roots)


def validate_output_path(output: str | Path) -> Path:
    """Resolve and validate an output strictly below an allowlisted local base."""
    candidate = Path(output).expanduser()
    if not candidate.is_absolute():
        raise BundleSafetyError(f"output must be an absolute local path: {candidate}")
    if ".." in candidate.parts:
        raise BundleSafetyError(f"output must not contain '..' path segments: {candidate}")
    if candidate.is_symlink():
        raise BundleSafetyError(f"output must not be a symbolic link: {candidate}")

    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise BundleSafetyError(f"output cannot be resolved safely: {candidate}: {exc}") from exc

    allowed = _allowed_local_bases()
    if not any(root in resolved.parents for root in allowed):
        bases = ", ".join(str(root) for root in allowed)
        raise BundleSafetyError(f"output must be below an allowed local temp/home base ({bases}): {resolved}")
    return resolved


def archive_path_for(output_dir: Path) -> Path:
    """Return the zip path beside an output directory."""
    return output_dir.with_name(f"{output_dir.name}.zip")


def _path_entry_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _assert_output_replaceable(output_dir: Path) -> None:
    if not _path_entry_exists(output_dir):
        return
    if output_dir.is_symlink() or not output_dir.is_dir():
        raise BundleSafetyError(f"refusing to replace non-directory output: {output_dir}")

    marker = output_dir / BUILDER_MARKER_NAME
    if marker.is_symlink() or not marker.is_file():
        raise BundleSafetyError(
            f"refusing to replace unmarked directory {output_dir}; expected builder marker {marker.name}"
        )
    try:
        marker_content = marker.read_text(encoding="utf-8")
    except OSError as exc:
        raise BundleSafetyError(f"cannot verify builder marker {marker}: {exc}") from exc
    if marker_content != BUILDER_MARKER_CONTENT:
        raise BundleSafetyError(f"refusing to replace directory with an invalid builder marker: {output_dir}")


def _archive_has_builder_marker(archive_path: Path) -> bool:
    try:
        with zipfile.ZipFile(archive_path) as archive:
            return (
                archive.comment == ZIP_COMMENT
                and archive.read(BUILDER_MARKER_NAME).decode("utf-8") == BUILDER_MARKER_CONTENT
            )
    except (KeyError, OSError, UnicodeDecodeError, zipfile.BadZipFile):
        return False


def _assert_archive_replaceable(archive_path: Path) -> None:
    if not _path_entry_exists(archive_path):
        return
    if archive_path.is_symlink() or not archive_path.is_file() or not _archive_has_builder_marker(archive_path):
        raise BundleSafetyError(f"refusing to replace archive not created by this builder: {archive_path}")


def _is_excluded(path: Path) -> bool:
    name = path.name
    lowered = name.casefold()
    if name.startswith("._") or lowered in _EXCLUDED_DIRECTORY_NAMES or lowered in _EXCLUDED_FILE_NAMES:
        return True
    if lowered.endswith((".pyc", ".pyo")):
        return True
    if path.is_file() and (lowered.startswith("test_") or lowered.endswith("_test.py")):
        return True
    return lowered.startswith(".env.")


def _copy_file(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise BundleError(f"bundle source must be a regular file, not a link or special file: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination, follow_symlinks=False)


def _copy_tree(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_dir():
        raise BundleError(f"bundle source must be a directory, not a link: {source}")
    destination.mkdir(parents=True, exist_ok=True)
    for child in sorted(source.iterdir(), key=lambda item: item.name):
        if _is_excluded(child):
            continue
        target = destination / child.name
        if child.is_symlink():
            raise BundleError(f"symbolic links are not allowed in bundle inputs: {child}")
        if child.is_dir():
            _copy_tree(child, target)
        elif child.is_file():
            _copy_file(child, target)
        else:
            raise BundleError(f"special files are not allowed in bundle inputs: {child}")
    shutil.copystat(source, destination, follow_symlinks=False)


def _replace_once(document: str, old: str, new: str, *, description: str) -> str:
    count = document.count(old)
    if count != 1:
        raise BundleError(f"expected exactly one {description} in the source Dockerfile, found {count}")
    return document.replace(old, new, 1)


def _generate_dockerfile(source: Path) -> str:
    try:
        document = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise BundleError(f"cannot read source Dockerfile {source}: {exc}") from exc
    document = _replace_once(
        document,
        "# BUILD CONTEXT: the repository root, not this directory.",
        "# BUILD CONTEXT: this local add-on bundle directory.",
        description="build-context heading",
    )
    document = _replace_once(
        document,
        "#   docker buildx build -f remote_energy_meter/Dockerfile \\",
        "#   docker buildx build -f Dockerfile \\",
        description="build-context command",
    )
    return _replace_once(
        document,
        "COPY remote_energy_meter/rootfs /",
        "COPY rootfs /",
        description="rootfs COPY instruction",
    )


def _write_text(path: Path, content: str, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    path.chmod(mode)


def _stage_bundle(staging_dir: Path, repository_root: Path) -> None:
    addon_source = repository_root / "remote_energy_meter"
    source_files = (
        (addon_source / "config.yaml", staging_dir / "config.yaml"),
        (addon_source / "build.yaml", staging_dir / "build.yaml"),
        (addon_source / "DOCS.md", staging_dir / "DOCS.md"),
        (addon_source / "icon.png", staging_dir / "icon.png"),
        (addon_source / "logo.png", staging_dir / "logo.png"),
        (repository_root / "pyproject.toml", staging_dir / "pyproject.toml"),
        (repository_root / "README.md", staging_dir / "README.md"),
        (repository_root / "CHANGELOG.md", staging_dir / "CHANGELOG.md"),
    )
    for source, destination in source_files:
        _copy_file(source, destination)

    _copy_tree(addon_source / "rootfs", staging_dir / "rootfs")
    _copy_tree(repository_root / "src", staging_dir / "src")
    _write_text(staging_dir / "Dockerfile", _generate_dockerfile(addon_source / "Dockerfile"))
    _write_text(staging_dir / "LOCAL_INSTALL.md", _LOCAL_INSTALL)

    for relative_path in _EXECUTABLE_PATHS:
        executable = staging_dir / relative_path
        if not executable.is_file():
            raise BundleError(f"required s6 executable is missing from staged rootfs: {relative_path.as_posix()}")
        executable.chmod(0o755)


def _regular_files(root: Path) -> Iterator[Path]:
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_symlink():
            raise BundleError(f"staged bundle contains an unexpected symbolic link: {path}")
        if path.is_file():
            yield path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_manifest(staging_dir: Path) -> Path:
    manifest = staging_dir / MANIFEST_NAME
    lines = [
        f"{_sha256(path)}  {path.relative_to(staging_dir).as_posix()}"
        for path in _regular_files(staging_dir)
        if path != manifest
    ]
    _write_text(manifest, "\n".join(lines) + "\n")
    return manifest


def _write_zip(staging_dir: Path, archive_path: Path) -> None:
    with zipfile.ZipFile(
        archive_path,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        strict_timestamps=True,
    ) as archive:
        archive.comment = ZIP_COMMENT
        for path in _regular_files(staging_dir):
            relative = path.relative_to(staging_dir).as_posix()
            mode = stat.S_IFREG | stat.S_IMODE(path.stat().st_mode)
            info = zipfile.ZipInfo(relative, date_time=_FIXED_ZIP_TIMESTAMP)
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = mode << 16
            with path.open("rb") as source, archive.open(info, mode="w", force_zip64=True) as destination:
                shutil.copyfileobj(source, destination, length=1024 * 1024)


def build_bundle(
    output: str | Path = DEFAULT_OUTPUT,
    *,
    repository_root: Path = REPOSITORY_ROOT,
) -> BundleResult:
    """Build and atomically publish the local add-on directory and adjacent zip."""
    output_dir = validate_output_path(output)
    archive_path = archive_path_for(output_dir)
    _assert_output_replaceable(output_dir)
    _assert_archive_replaceable(archive_path)

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.staging-", dir=output_dir.parent))
    temporary_archive: Path | None = None
    published = False
    try:
        _write_text(staging_dir / BUILDER_MARKER_NAME, BUILDER_MARKER_CONTENT)
        _stage_bundle(staging_dir, repository_root.resolve())
        _write_manifest(staging_dir)

        with tempfile.NamedTemporaryFile(
            prefix=f".{output_dir.name}.archive-",
            suffix=".zip",
            dir=output_dir.parent,
            delete=False,
        ) as temporary_file:
            temporary_archive = Path(temporary_file.name)
        _write_zip(staging_dir, temporary_archive)

        file_count = sum(1 for _ in _regular_files(staging_dir))
        if output_dir.exists():
            shutil.rmtree(output_dir)
        staging_dir.replace(output_dir)
        published = True
        temporary_archive.replace(archive_path)
        temporary_archive = None
        return BundleResult(
            output_dir=output_dir,
            archive_path=archive_path,
            manifest_path=output_dir / MANIFEST_NAME,
            file_count=file_count,
            archive_size=archive_path.stat().st_size,
        )
    finally:
        if not published and staging_dir.exists():
            shutil.rmtree(staging_dir)
        if temporary_archive is not None and temporary_archive.exists():
            temporary_archive.unlink()


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"local output directory (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--integration-output",
        type=Path,
        default=DEFAULT_INTEGRATION_OUTPUT,
        help=f"separate companion integration output (default: {DEFAULT_INTEGRATION_OUTPUT})",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _argument_parser()
    options = parser.parse_args(argv)
    try:
        result = build_bundle(options.output)
        integration = build_integration_bundle(options.integration_output)
    except (BundleError, IntegrationBundleError) as exc:
        parser.exit(2, f"local artifact build refused: {exc}\n")

    print(f"Bundle directory: {result.output_dir}")
    print(f"Archive: {result.archive_path}")
    print(f"Regular files: {result.file_count}")
    print(f"Archive size: {result.archive_size} bytes")
    print(f"Integration directory: {integration.output_dir}")
    print(f"Integration archive: {integration.archive_path}")
    print(f"Integration files: {integration.file_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
