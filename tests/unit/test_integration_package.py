"""Custom integration package metadata, validation, branding, and local distribution."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
import zipfile
from pathlib import Path
from typing import Final

import pytest
import yaml
from tools.build_local_integration_bundle import (
    INTEGRATION_MARKER_NAME,
    INTEGRATION_ZIP_COMMENT,
    IntegrationBundleResult,
    build_integration_bundle,
)
from tools.validate_integration_package import BRAND_PNG_SPECS, IntegrationPackageError, validate_integration_package

ROOT: Final = Path(__file__).resolve().parents[2]
PACKAGE: Final = ROOT / "custom_components/remote_energy_meter"
SELECTED_BRAND_ROOT: Final = ROOT / "docs/icon-candidates/01-pulse-dial/integration/brand"
SELECTED_BRAND_ASSETS: Final = {
    "icon.png": ((256, 256), "d64f0f961e6f0557a81e18a7f2192acf5244a34931f1758cc8408a3a157fd92e"),
    "icon@2x.png": ((512, 512), "b60b96af69728f784a255f605fc5a7b7ed5f227b4dfdda8e8747b43ac41b5fe7"),
    "logo.png": ((640, 256), "ed3cee60abf08506141c9d4830098e68699b7c74ffd2590231382858aa96291b"),
    "logo@2x.png": ((1280, 512), "1a59505be5ba71689fdfd0696e65fe9cba75839d126aa21ac956b7f8ea2e2a67"),
}
PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"


def _png_dimensions(path: Path) -> tuple[int, int]:
    content = path.read_bytes()
    assert content.startswith(PNG_SIGNATURE)
    assert content[12:16] == b"IHDR"
    return struct.unpack(">II", content[16:24])


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _copy_package(tmp_path: Path) -> Path:
    destination = tmp_path / "remote_energy_meter"
    shutil.copytree(PACKAGE, destination, ignore=shutil.ignore_patterns("._*", "__pycache__"))
    return destination


@pytest.fixture(scope="module")
def integration_bundle(tmp_path_factory: pytest.TempPathFactory) -> IntegrationBundleResult:
    return build_integration_bundle(tmp_path_factory.mktemp("integration-bundle") / "remote-energy-meter")


def test_source_integration_has_required_manifest_strings_translations_brand_and_no_credentials() -> None:
    result = validate_integration_package(PACKAGE)
    manifest = json.loads((PACKAGE / "manifest.json").read_text(encoding="utf-8"))

    assert result.python_file_count >= 6
    assert manifest["version"]
    assert manifest["config_flow"] is True
    assert manifest["dependencies"] == ["mqtt"]
    assert manifest["iot_class"] == "local_push"
    assert manifest["integration_type"] == "hub"
    assert "icon" not in manifest


def test_integration_brand_matches_only_the_selected_pulse_dial_assets() -> None:
    brand = PACKAGE / "brand"
    assert {path.name for path in brand.iterdir() if path.is_file() and not path.name.startswith("._")} == set(
        SELECTED_BRAND_ASSETS
    )
    assert {
        Path(f"brand/{name}"): dimensions for name, (dimensions, _digest) in SELECTED_BRAND_ASSETS.items()
    } == BRAND_PNG_SPECS

    for name, (dimensions, digest) in SELECTED_BRAND_ASSETS.items():
        promoted = brand / name
        selected = SELECTED_BRAND_ROOT / name
        assert promoted.read_bytes() == selected.read_bytes()
        assert _png_dimensions(promoted) == dimensions
        assert _sha256(promoted) == digest


def test_integration_bundle_has_copy_ready_layout_exact_brand_and_matching_zip(
    integration_bundle: IntegrationBundleResult,
) -> None:
    package = integration_bundle.output_dir / "custom_components/remote_energy_meter"
    assert package == integration_bundle.package_path
    assert (package / "manifest.json").is_file()
    assert (package / "strings.json").is_file()
    assert (package / "translations/en.json").is_file()
    assert (integration_bundle.output_dir / "LOCAL_INSTALL.md").is_file()
    assert (integration_bundle.output_dir / INTEGRATION_MARKER_NAME).is_file()
    assert {path.name for path in (package / "brand").iterdir() if path.is_file()} == set(SELECTED_BRAND_ASSETS)
    assert not (integration_bundle.output_dir / "config.yaml").exists()
    assert not (integration_bundle.output_dir / "icon.png").exists()
    assert not (integration_bundle.output_dir / "logo.png").exists()
    validate_integration_package(package)

    for name, (dimensions, digest) in SELECTED_BRAND_ASSETS.items():
        staged = package / "brand" / name
        assert staged.read_bytes() == (SELECTED_BRAND_ROOT / name).read_bytes()
        assert _png_dimensions(staged) == dimensions
        assert _sha256(staged) == digest

    files = {
        path.relative_to(integration_bundle.output_dir).as_posix(): path.read_bytes()
        for path in integration_bundle.output_dir.rglob("*")
        if path.is_file()
    }
    with zipfile.ZipFile(integration_bundle.archive_path) as archive:
        assert archive.comment == INTEGRATION_ZIP_COMMENT
        assert set(archive.namelist()) == set(files)
        assert {
            name.removeprefix("custom_components/remote_energy_meter/brand/")
            for name in archive.namelist()
            if name.startswith("custom_components/remote_energy_meter/brand/")
        } == set(SELECTED_BRAND_ASSETS)
        for relative, content in files.items():
            assert archive.read(relative) == content


def test_local_install_guide_requires_core_restart_and_brand_cache_refresh(
    integration_bundle: IntegrationBundleResult,
) -> None:
    guide = (integration_bundle.output_dir / "LOCAL_INSTALL.md").read_text(encoding="utf-8")
    assert "/config/custom_components/remote_energy_meter" in guide
    assert guide.index("Restart Home Assistant Core") < guide.index("start it")
    assert "Devices & services" in guide
    assert "confirm" in guide.casefold()
    assert "Core 2026.3" in guide
    assert "brand/" in guide
    assert "refresh the browser" in guide.casefold()
    assert "GitHub/HACS" in guide


def test_validator_rejects_a_credential_key(tmp_path: Path) -> None:
    package = _copy_package(tmp_path)
    (package / "__init__.py").write_text('CONFIG = {"password": "embedded"}\n', encoding="utf-8")

    with pytest.raises(IntegrationPackageError, match="credential material"):
        validate_integration_package(package)


def test_validator_rejects_malformed_brand_png(tmp_path: Path) -> None:
    package = _copy_package(tmp_path)
    (package / "brand/icon.png").write_bytes(PNG_SIGNATURE + b"truncated")

    with pytest.raises(
        IntegrationPackageError,
        match=re.escape("invalid integration brand PNG brand/icon.png"),
    ):
        validate_integration_package(package)


def test_validator_rejects_wrong_sized_brand_png(tmp_path: Path) -> None:
    package = _copy_package(tmp_path)
    (package / "brand/icon.png").write_bytes((SELECTED_BRAND_ROOT / "logo.png").read_bytes())

    with pytest.raises(IntegrationPackageError, match="dimensions must be 256x256"):
        validate_integration_package(package)


def test_validator_rejects_unexpected_brand_asset_even_when_it_is_a_valid_png(tmp_path: Path) -> None:
    package = _copy_package(tmp_path)
    (package / "brand/dark_icon.png").write_bytes((SELECTED_BRAND_ROOT / "icon.png").read_bytes())

    with pytest.raises(
        IntegrationPackageError,
        match=re.escape("unexpected brand asset brand/dark_icon.png"),
    ):
        validate_integration_package(package)


def test_validator_rejects_unexpected_binary_outside_the_brand_allowlist(tmp_path: Path) -> None:
    package = _copy_package(tmp_path)
    (package / "unexpected.bin").write_bytes((SELECTED_BRAND_ROOT / "icon.png").read_bytes())

    with pytest.raises(
        IntegrationPackageError,
        match=re.escape("not UTF-8 text: unexpected.bin"),
    ):
        validate_integration_package(package)


def test_validator_requires_every_exact_brand_asset(tmp_path: Path) -> None:
    package = _copy_package(tmp_path)
    (package / "brand/icon@2x.png").unlink()

    with pytest.raises(
        IntegrationPackageError,
        match=re.escape("missing required brand assets: brand/icon@2x.png"),
    ):
        validate_integration_package(package)


def test_validator_rejects_symlinks_and_special_files(tmp_path: Path) -> None:
    linked_package = _copy_package(tmp_path / "linked")
    (linked_package / "linked-file").symlink_to(linked_package / "manifest.json")
    with pytest.raises(IntegrationPackageError, match="symbolic link"):
        validate_integration_package(linked_package)

    special_package = _copy_package(tmp_path / "special")
    os.mkfifo(special_package / "special.pipe")
    with pytest.raises(IntegrationPackageError, match="special file"):
        validate_integration_package(special_package)


def test_addon_manifest_declares_only_the_companion_discovery_service() -> None:
    manifest = yaml.safe_load((ROOT / "remote_energy_meter/config.yaml").read_text(encoding="utf-8"))
    assert manifest["discovery"] == ["remote_energy_meter"]
