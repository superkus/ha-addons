#!/usr/bin/env python3
"""Validate the distributable Home Assistant custom-integration package."""

from __future__ import annotations

import argparse
import json
import re
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Final

REQUIRED_FILES: Final = (
    Path("manifest.json"),
    Path("strings.json"),
    Path("translations/en.json"),
)
BRAND_PNG_SPECS: Final = {
    Path("brand/icon.png"): (256, 256),
    Path("brand/icon@2x.png"): (512, 512),
    Path("brand/logo.png"): (640, 256),
    Path("brand/logo@2x.png"): (1280, 512),
}
_FORBIDDEN_FILE_NAMES: Final = frozenset(
    {
        ".env",
        "credentials.json",
        "credentials.yaml",
        "credentials.yml",
        "secrets.yaml",
        "secrets.yml",
    }
)
_CREDENTIAL_KEY_RE: Final = re.compile(r"(?i)[\"'](?:username|password|token|secret|api[_-]?key)[\"']\s*[:=]")
_FORBIDDEN_LITERAL_MARKERS: Final = ("!secret ", "SUPERVISOR_TOKEN", "Authorization: Bearer")
_PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
_PNG_IHDR: Final = b"IHDR"
_PNG_IDAT: Final = b"IDAT"
_PNG_IEND: Final = b"IEND"
_PNG_PLTE: Final = b"PLTE"
_PNG_RGBA_BIT_DEPTH: Final = 8
_PNG_RGBA_COLOR_TYPE: Final = 6
_PNG_BYTES_PER_PIXEL: Final = 4


class IntegrationPackageError(RuntimeError):
    """The integration package is incomplete or credential-bearing."""


@dataclass(frozen=True, slots=True)
class IntegrationPackageValidation:
    """Summary of one successful package validation."""

    package_path: Path
    file_count: int
    python_file_count: int


def _json_object(path: Path) -> dict[str, object]:
    try:
        decoded: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise IntegrationPackageError(f"required JSON file is unreadable or invalid: {path.name}") from error
    if not isinstance(decoded, dict) or not all(isinstance(key, str) for key in decoded):
        raise IntegrationPackageError(f"required JSON file is not an object: {path.name}")
    return decoded


def _png_error(relative: Path, detail: str) -> IntegrationPackageError:
    return IntegrationPackageError(f"invalid integration brand PNG {relative.as_posix()}: {detail}")


def _validate_brand_png(path: Path, relative: Path, expected_size: tuple[int, int]) -> None:
    """Validate one exact local-brand PNG without treating arbitrary binary files as trusted."""
    try:
        content = path.read_bytes()
    except OSError as error:
        raise _png_error(relative, "file is unreadable") from error
    if not content.startswith(_PNG_SIGNATURE):
        raise _png_error(relative, "PNG signature is missing or invalid")

    offset = len(_PNG_SIGNATURE)
    saw_header = False
    saw_data = False
    data_closed = False
    saw_end = False
    width = height = 0
    compressed_parts: list[bytes] = []

    while offset < len(content):
        if len(content) - offset < 12:
            raise _png_error(relative, "chunk header or checksum is truncated")
        length = struct.unpack(">I", content[offset : offset + 4])[0]
        chunk_type = content[offset + 4 : offset + 8]
        chunk_end = offset + 12 + length
        if chunk_end > len(content):
            raise _png_error(relative, "chunk payload is truncated")
        if len(chunk_type) != 4 or not all(
            ord("A") <= value <= ord("Z") or ord("a") <= value <= ord("z") for value in chunk_type
        ):
            raise _png_error(relative, "chunk type is invalid")

        chunk_data = content[offset + 8 : offset + 8 + length]
        recorded_crc = struct.unpack(">I", content[offset + 8 + length : chunk_end])[0]
        computed_crc = zlib.crc32(chunk_data, zlib.crc32(chunk_type)) & 0xFFFFFFFF
        if recorded_crc != computed_crc:
            raise _png_error(relative, f"{chunk_type.decode('ascii')} checksum is invalid")

        if not saw_header:
            if chunk_type != _PNG_IHDR or length != 13:
                raise _png_error(relative, "IHDR must be the first chunk and exactly 13 bytes")
            width, height, bit_depth, color_type, compression, filter_method, interlace = struct.unpack(
                ">IIBBBBB", chunk_data
            )
            if (width, height) != expected_size:
                raise _png_error(relative, f"dimensions must be {expected_size[0]}x{expected_size[1]}")
            if bit_depth != _PNG_RGBA_BIT_DEPTH or color_type != _PNG_RGBA_COLOR_TYPE:
                raise _png_error(relative, "color format must be 8-bit RGBA")
            if compression != 0 or filter_method != 0 or interlace != 0:
                raise _png_error(relative, "compression, filter, and interlace methods must be PNG defaults")
            saw_header = True
        elif chunk_type == _PNG_IHDR:
            raise _png_error(relative, "IHDR appears more than once")
        elif chunk_type == _PNG_IDAT:
            if data_closed:
                raise _png_error(relative, "IDAT chunks must be consecutive")
            saw_data = True
            compressed_parts.append(chunk_data)
        elif chunk_type == _PNG_IEND:
            if length != 0 or not saw_data:
                raise _png_error(relative, "IEND must follow image data and have no payload")
            saw_end = True
            offset = chunk_end
            if offset != len(content):
                raise _png_error(relative, "data appears after IEND")
            break
        else:
            if saw_data:
                data_closed = True
            if chunk_type[0] & 0x20 == 0 and chunk_type != _PNG_PLTE:
                raise _png_error(relative, f"unsupported critical chunk {chunk_type.decode('ascii')}")
        offset = chunk_end

    if not saw_header or not saw_end:
        raise _png_error(relative, "required IHDR, IDAT, or IEND chunk is missing")

    decompressor = zlib.decompressobj()
    try:
        pixels = decompressor.decompress(b"".join(compressed_parts)) + decompressor.flush()
    except zlib.error as error:
        raise _png_error(relative, "image data cannot be decompressed") from error
    if not decompressor.eof or decompressor.unused_data:
        raise _png_error(relative, "image data stream is truncated or has trailing data")

    row_size = 1 + width * _PNG_BYTES_PER_PIXEL
    if len(pixels) != height * row_size:
        raise _png_error(relative, "decompressed image data has the wrong length")
    if any(pixels[row * row_size] > 4 for row in range(height)):
        raise _png_error(relative, "scanline uses an invalid PNG filter")


def validate_integration_package(package_path: str | Path) -> IntegrationPackageValidation:
    """Require metadata, exact brand PNGs, compilable Python, and no credentials."""
    package = Path(package_path)
    if package.is_symlink() or not package.is_dir():
        raise IntegrationPackageError(f"integration package is not a regular directory: {package}")

    for relative in REQUIRED_FILES:
        required = package / relative
        if required.is_symlink() or not required.is_file():
            raise IntegrationPackageError(f"integration package is missing {relative.as_posix()}")

    manifest = _json_object(package / "manifest.json")
    strings = _json_object(package / "strings.json")
    translation = _json_object(package / "translations/en.json")
    expected_manifest = {
        "domain": "remote_energy_meter",
        "config_flow": True,
        "dependencies": ["mqtt"],
        "iot_class": "local_push",
        "integration_type": "hub",
    }
    for key, expected in expected_manifest.items():
        if manifest.get(key) != expected:
            raise IntegrationPackageError(f"manifest field {key!r} does not declare {expected!r}")
    if strings != translation:
        raise IntegrationPackageError("strings.json and translations/en.json are not synchronized")

    files: list[Path] = []
    python_count = 0
    for path in sorted(package.rglob("*"), key=lambda item: item.relative_to(package).as_posix()):
        relative = path.relative_to(package)
        # AppleDouble sidecars are metadata that macOS writes on filesystems without extended
        # attributes (network shares, FAT volumes), not integration files. Ignore them during
        # source validation; the builder also skips them, so they cannot enter the package or zip.
        if any(part.startswith("._") for part in relative.parts):
            continue
        if path.is_symlink():
            raise IntegrationPackageError("integration package contains a symbolic link")
        if path.is_dir():
            if path.name == "__pycache__":
                raise IntegrationPackageError("integration package contains generated metadata")
            continue
        if not path.is_file():
            raise IntegrationPackageError("integration package contains a special file")
        if path.name.casefold() in _FORBIDDEN_FILE_NAMES:
            raise IntegrationPackageError(f"integration package contains forbidden file {relative.as_posix()}")
        if path.suffix.casefold() in {".pyc", ".pyo"}:
            raise IntegrationPackageError("integration package contains compiled cache files")
        files.append(path)

        brand_spec = BRAND_PNG_SPECS.get(relative)
        if brand_spec is not None:
            _validate_brand_png(path, relative, brand_spec)
            continue
        if relative.parts and relative.parts[0] == "brand":
            raise IntegrationPackageError(f"integration package contains unexpected brand asset {relative.as_posix()}")
        if path.suffix.casefold() == ".png":
            raise IntegrationPackageError(f"integration package contains an unexpected PNG file {relative.as_posix()}")

        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise IntegrationPackageError(f"integration file is not UTF-8 text: {relative.as_posix()}") from error
        if "\x00" in text:
            raise IntegrationPackageError(f"integration file is not plain UTF-8 text: {relative.as_posix()}")
        if _CREDENTIAL_KEY_RE.search(text) or any(marker in text for marker in _FORBIDDEN_LITERAL_MARKERS):
            raise IntegrationPackageError(f"integration package contains credential material in {relative.as_posix()}")
        if path.suffix == ".py":
            try:
                compile(text, str(relative), "exec")
            except SyntaxError as error:
                raise IntegrationPackageError(f"integration Python does not compile: {relative.as_posix()}") from error
            python_count += 1

    missing_brand_assets = set(BRAND_PNG_SPECS) - {path.relative_to(package) for path in files}
    if missing_brand_assets:
        rendered = ", ".join(path.as_posix() for path in sorted(missing_brand_assets))
        raise IntegrationPackageError(f"integration package is missing required brand assets: {rendered}")
    if not files or python_count == 0:
        raise IntegrationPackageError("integration package contains no Python implementation")
    return IntegrationPackageValidation(package.resolve(), len(files), python_count)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    options = parser.parse_args()
    try:
        result = validate_integration_package(options.package)
    except IntegrationPackageError as error:
        parser.exit(2, f"integration package invalid: {error}\n")
    print(f"Validated integration: {result.package_path}")
    print(f"Files: {result.file_count} ({result.python_file_count} Python)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
