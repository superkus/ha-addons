"""Local Home Assistant add-on bundle construction and safety checks."""

from __future__ import annotations

import hashlib
import shlex
import stat
import struct
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import pytest
import yaml
from tools.build_local_addon_bundle import (
    BUILDER_MARKER_NAME,
    MANIFEST_NAME,
    ZIP_COMMENT,
    BundleResult,
    BundleSafetyError,
    archive_path_for,
    build_bundle,
    validate_output_path,
)

ROOT: Final = Path(__file__).resolve().parents[2]
EXECUTABLES: Final = (
    Path("rootfs/etc/s6-overlay/s6-rc.d/remote-energy-meter/run"),
    Path("rootfs/etc/s6-overlay/s6-rc.d/remote-energy-meter/finish"),
)
SELECTED_ADDON_BRAND_ASSETS: Final = {
    "icon.png": ((128, 128), "2bc9dc07f35b365ead6555396af8fea6d4dd1b4339724dccc5b3ffe33070ea4d"),
    "logo.png": ((250, 100), "d96d368ec086dd8b7876256a56e54dbccce7d5338c8fea3463dc77036b90035a"),
}
SELECTED_ADDON_BRAND_ROOT: Final = ROOT / "docs/icon-candidates/01-pulse-dial/addon"
PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"


def _mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, Mapping)
    return {str(key): item for key, item in value.items()}


def _regular_files(root: Path) -> dict[str, Path]:
    return {
        path.relative_to(root).as_posix(): path
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _png_dimensions(path: Path) -> tuple[int, int]:
    content = path.read_bytes()
    assert content.startswith(PNG_SIGNATURE)
    assert content[12:16] == b"IHDR"
    return struct.unpack(">II", content[16:24])


def _manifest_entries(result: BundleResult) -> dict[str, str]:
    entries: dict[str, str] = {}
    for line in result.manifest_path.read_text(encoding="utf-8").splitlines():
        digest, separator, relative = line.partition("  ")
        assert separator == "  "
        assert len(digest) == 64
        assert relative not in entries
        entries[relative] = digest
    return entries


@pytest.fixture(scope="module")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> BundleResult:
    return build_bundle(tmp_path_factory.mktemp("local-addon-bundle") / "remote_energy_meter")


def test_bundle_has_only_the_required_top_level_layout_and_parseable_yaml(bundle: BundleResult) -> None:
    assert {path.name for path in bundle.output_dir.iterdir()} == {
        BUILDER_MARKER_NAME,
        MANIFEST_NAME,
        "CHANGELOG.md",
        "DOCS.md",
        "Dockerfile",
        "LOCAL_INSTALL.md",
        "README.md",
        "build.yaml",
        "config.yaml",
        "icon.png",
        "logo.png",
        "pyproject.toml",
        "requirements.txt",
        "rootfs",
        "src",
    }
    assert _mapping(yaml.safe_load((bundle.output_dir / "config.yaml").read_text(encoding="utf-8")))
    assert _mapping(yaml.safe_load((bundle.output_dir / "build.yaml").read_text(encoding="utf-8")))
    assert (bundle.output_dir / "src/remote_energy_meter/__init__.py").is_file()
    assert (bundle.output_dir / "src/remote_energy_meter/plugins/idrac/plugin.py").is_file()
    assert (bundle.output_dir / "src/remote_energy_meter/plugins/ipmi/plugin.py").is_file()
    assert not (bundle.output_dir / "custom_components").exists(), (
        "the companion integration is a separate Core artifact, never part of the add-on image"
    )

    copied_files = {
        "config.yaml": ROOT / "remote_energy_meter/config.yaml",
        "build.yaml": ROOT / "remote_energy_meter/build.yaml",
        "DOCS.md": ROOT / "remote_energy_meter/DOCS.md",
        "icon.png": ROOT / "remote_energy_meter/icon.png",
        "logo.png": ROOT / "remote_energy_meter/logo.png",
        "pyproject.toml": ROOT / "pyproject.toml",
        "requirements.txt": ROOT / "requirements.txt",
        "README.md": ROOT / "README.md",
        "CHANGELOG.md": ROOT / "CHANGELOG.md",
    }
    for relative, source in copied_files.items():
        assert (bundle.output_dir / relative).read_bytes() == source.read_bytes()


def test_addon_branding_matches_only_the_selected_pulse_dial_assets(bundle: BundleResult) -> None:
    for relative, (dimensions, digest) in SELECTED_ADDON_BRAND_ASSETS.items():
        promoted = ROOT / "remote_energy_meter" / relative
        selected = SELECTED_ADDON_BRAND_ROOT / relative
        staged = bundle.output_dir / relative

        assert promoted.read_bytes() == selected.read_bytes()
        assert staged.read_bytes() == selected.read_bytes()
        assert _png_dimensions(promoted) == dimensions
        assert _png_dimensions(staged) == dimensions
        assert _sha256(promoted) == digest
        assert _sha256(staged) == digest


def test_generated_install_guide_covers_the_local_addon_flow_without_credentials(bundle: BundleResult) -> None:
    guide = (bundle.output_dir / "LOCAL_INSTALL.md").read_text(encoding="utf-8")
    lowered = guide.casefold()

    assert "/addons/remote_energy_meter" in guide
    assert "local add-ons" in lowered
    assert "reload" in lowered
    assert "install" in lowered
    assert "start" in lowered
    assert "/config/secrets.yaml" in guide
    assert "/config/custom_components/remote_energy_meter" in guide
    assert "restart home assistant core" in lowered
    assert "devices & services" in lowered
    assert "icon.png" in guide and "logo.png" in guide
    assert "presentation" in lowered and "outside the add-on image filesystem" in lowered
    assert "reload local add-ons" in lowered and "refresh the browser" in lowered
    assert "contains no\ncredentials" in lowered
    assert "no values for those references" in lowered


@pytest.mark.parametrize("document", [ROOT / "README.md", ROOT / "remote_energy_meter/DOCS.md"])
def test_operator_docs_make_local_install_primary_and_keep_github_optional(document: Path) -> None:
    content = document.read_text(encoding="utf-8")
    lowered = content.casefold()

    assert "tools/build_local_addon_bundle.py" in content
    assert "/private/tmp/remote-energy-meter-local-addon" in content
    assert "/private/tmp/remote-energy-meter-local-integration" in content
    assert "/config/custom_components/remote_energy_meter" in content
    assert "/addons/remote_energy_meter" in content
    assert "restart" in lowered and "home assistant core" in lowered
    assert "devices & services" in lowered and "confirm" in lowered
    assert "reload" in lowered and "local add-ons" in lowered
    assert "github" in lowered and "optional later path" in lowered
    assert lowered.index("local add-on") < lowered.index("optional later path")


def test_manifest_is_complete_sorted_and_hash_correct(bundle: BundleResult) -> None:
    files = _regular_files(bundle.output_dir)
    entries = _manifest_entries(bundle)
    expected_paths = set(files) - {MANIFEST_NAME}

    assert list(entries) == sorted(entries)
    assert set(entries) == expected_paths
    assert bundle.file_count == len(files)
    for relative, digest in entries.items():
        assert digest == _sha256(files[relative])


def test_generated_dockerfile_changes_only_the_local_build_context_paths(bundle: BundleResult) -> None:
    source = (ROOT / "remote_energy_meter/Dockerfile").read_text(encoding="utf-8")
    expected = source.replace(
        "# BUILD CONTEXT: the repository root, not this directory.",
        "# BUILD CONTEXT: this local add-on bundle directory.",
        1,
    )
    expected = expected.replace(
        "#   docker buildx build -f remote_energy_meter/Dockerfile \\",
        "#   docker buildx build -f Dockerfile \\",
        1,
    )
    expected = expected.replace("COPY remote_energy_meter/rootfs /", "COPY rootfs /", 1)

    generated = (bundle.output_dir / "Dockerfile").read_text(encoding="utf-8")
    assert generated == expected


def test_every_generated_dockerfile_copy_source_exists(bundle: BundleResult) -> None:
    dockerfile = (bundle.output_dir / "Dockerfile").read_text(encoding="utf-8")
    copy_sources: list[str] = []
    for line in dockerfile.splitlines():
        if not line.lstrip().startswith("COPY "):
            continue
        tokens = shlex.split(line)
        copy_sources.extend(tokens[1:-1])

    assert copy_sources
    assert "rootfs" in {source.rstrip("/") for source in copy_sources}
    assert "remote_energy_meter/rootfs" not in {source.rstrip("/") for source in copy_sources}
    for source in copy_sources:
        assert (bundle.output_dir / source.rstrip("/")).exists(), source


def test_supervisor_branding_and_companion_integration_stay_out_of_the_addon_image(
    bundle: BundleResult,
) -> None:
    dockerfile = (bundle.output_dir / "Dockerfile").read_text(encoding="utf-8")
    copy_sources: list[str] = []
    for line in dockerfile.splitlines():
        if line.lstrip().startswith("COPY "):
            tokens = shlex.split(line)
            copy_sources.extend(source.rstrip("/") for source in tokens[1:-1])

    assert "icon.png" not in copy_sources
    assert "logo.png" not in copy_sources
    assert not any(source == "." or source.startswith("custom_components") for source in copy_sources)
    assert "icon.png" not in dockerfile
    assert "logo.png" not in dockerfile
    assert not (bundle.output_dir / "rootfs/icon.png").exists()
    assert not (bundle.output_dir / "rootfs/logo.png").exists()
    assert not (bundle.output_dir / "custom_components").exists()


def test_s6_run_and_finish_are_executable_in_the_staged_directory(bundle: BundleResult) -> None:
    for relative in EXECUTABLES:
        mode = stat.S_IMODE((bundle.output_dir / relative).stat().st_mode)
        assert mode == 0o755


def test_bundle_excludes_caches_tests_metadata_and_sensitive_local_files(bundle: BundleResult) -> None:
    forbidden_names = {
        ".git",
        ".github",
        ".hypothesis",
        ".kiro",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "idrac-probe.local.json",
        "secrets.yaml",
        "secrets.yml",
        "tests",
    }
    for relative in _regular_files(bundle.output_dir):
        path = Path(relative)
        lowered_parts = {part.casefold() for part in path.parts}
        assert forbidden_names.isdisjoint(lowered_parts)
        assert not any(part.startswith("._") for part in path.parts)
        assert path.suffix.casefold() not in {".pyc", ".pyo"}
        assert not path.name.casefold().startswith("test_")
        assert not path.name.casefold().endswith("_test.py")


def test_unmarked_output_directory_is_never_replaced(tmp_path: Path) -> None:
    output = tmp_path / "existing-addon"
    output.mkdir()
    sentinel = output / "keep-me.txt"
    sentinel.write_text("do not delete", encoding="utf-8")

    with pytest.raises(BundleSafetyError, match="unmarked directory"):
        build_bundle(output)

    assert sentinel.read_text(encoding="utf-8") == "do not delete"
    assert not archive_path_for(output).exists()


def test_builder_marked_output_can_be_replaced(tmp_path: Path) -> None:
    output = tmp_path / "replaceable-addon"
    first = build_bundle(output)
    stale = first.output_dir / "stale.txt"
    stale.write_text("stale", encoding="utf-8")

    second = build_bundle(output)

    assert second.output_dir == first.output_dir
    assert not stale.exists()
    assert second.archive_path.is_file()


def test_arbitrary_existing_archive_is_never_replaced(tmp_path: Path) -> None:
    output = tmp_path / "addon-with-unowned-archive"
    archive = archive_path_for(output)
    archive.write_bytes(b"not created by the bundle builder")

    with pytest.raises(BundleSafetyError, match="archive not created by this builder"):
        build_bundle(output)

    assert archive.read_bytes() == b"not created by the bundle builder"
    assert not output.exists()


@pytest.mark.parametrize(
    "unsafe",
    [
        Path("relative-addon"),
        Path("/mnt/shared/remote-energy-meter-local-addon"),
        Path("/etc/remote-energy-meter-local-addon"),
    ],
)
def test_output_must_be_below_an_allowed_local_home_or_temp_base(unsafe: Path) -> None:
    with pytest.raises(BundleSafetyError):
        validate_output_path(unsafe)


def test_zip_matches_the_directory_and_preserves_unix_modes(bundle: BundleResult) -> None:
    files = _regular_files(bundle.output_dir)
    with zipfile.ZipFile(bundle.archive_path) as archive:
        infos = archive.infolist()
        assert archive.comment == ZIP_COMMENT
        assert [info.filename for info in infos] == sorted(files)
        assert not any(info.is_dir() for info in infos)
        assert not any(info.filename.startswith("custom_components/") for info in infos)
        for info in infos:
            staged = files[info.filename]
            assert archive.read(info) == staged.read_bytes()
            assert stat.S_IMODE(info.external_attr >> 16) == stat.S_IMODE(staged.stat().st_mode)

        for relative in EXECUTABLES:
            mode = stat.S_IMODE(archive.getinfo(relative.as_posix()).external_attr >> 16)
            assert mode == 0o755


def test_local_packaging_keeps_idrac_and_ipmi_defaults_and_installs_ipmitool(bundle: BundleResult) -> None:
    manifest = _mapping(yaml.safe_load((bundle.output_dir / "config.yaml").read_text(encoding="utf-8")))
    options = _mapping(manifest["options"])
    devices = options["devices"]
    assert isinstance(devices, list)
    assert len(devices) == 2

    first = _mapping(devices[0])
    first_params = _mapping(first["params"])
    assert (first["plugin"], first_params["host"]) == ("idrac", "192.0.2.9")
    assert isinstance(first_params["password"], str) and first_params["password"].startswith("!secret ")

    second = _mapping(devices[1])
    second_params = _mapping(second["params"])
    assert (second["plugin"], second_params["host"]) == ("ipmi", "192.0.2.20")
    assert isinstance(second_params["password"], str) and second_params["password"].startswith("!secret ")
    assert {"verify_tls", "power_path"}.isdisjoint(second_params)

    dockerfile = (bundle.output_dir / "Dockerfile").read_text(encoding="utf-8")
    pip = dockerfile.index("pip3 install --no-cache-dir --require-hashes -r requirements.txt")
    assert dockerfile.index("apk add --no-cache ipmitool=") < pip
    # The bundle keeps the flat layout the generated COPY lines expect: the lock sits beside pyproject.toml.
    assert "COPY pyproject.toml README.md requirements.txt ./" in dockerfile
