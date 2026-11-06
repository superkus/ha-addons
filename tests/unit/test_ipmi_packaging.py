"""Addon packaging and shipped-default checks for the IPMI plugin."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import yaml

from remote_energy_meter.plugins.ipmi.plugin import IpmiPlugin
from remote_energy_meter.plugins.registry import discover

ROOT: Final = Path(__file__).resolve().parents[2]
DOCKERFILE: Final = ROOT / "remote_energy_meter" / "Dockerfile"
MANIFEST: Final = ROOT / "remote_energy_meter" / "config.yaml"


def _mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, Mapping)
    return {str(key): item for key, item in value.items()}


def test_dockerfile_installs_ipmitool_before_python_dependencies() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    install = dockerfile.index("apk add --no-cache ipmitool=")
    pip = dockerfile.index("pip3 install --no-cache-dir --require-hashes -r requirements.txt")
    assert install < pip


def test_dockerfile_pins_ipmitool_to_an_exact_alpine_package_version() -> None:
    """A floating `apk add ipmitool` would silently change the parser that handles untrusted controller bytes."""
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    assert re.search(r"apk add --no-cache ipmitool=\d+\.\d+\.\d+-r\d+\b", dockerfile)


def test_dockerfile_installs_only_hash_locked_dependencies() -> None:
    """The image is built on the user's host; the lock is what makes CI's audit and the shipped set identical."""
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    assert "pip3 install --no-cache-dir --require-hashes -r requirements.txt" in dockerfile
    assert "pip3 install --no-cache-dir --no-deps ." in dockerfile, "the package itself must not resolve anything"
    assert "pip3 install --no-cache-dir .\n" not in dockerfile and "pip3 install --no-cache-dir . " not in dockerfile
    assert "requirements.txt" in dockerfile.split("COPY pyproject.toml", 1)[1].split("\n", 1)[0]


def test_shipped_defaults_keep_redfish_first_and_add_ipmi_second() -> None:
    manifest = _mapping(yaml.safe_load(MANIFEST.read_text(encoding="utf-8")))
    options = _mapping(manifest["options"])
    devices = options["devices"]
    assert isinstance(devices, list)
    assert len(devices) == 2

    first = _mapping(devices[0])
    first_params = _mapping(first["params"])
    assert (first["plugin"], first_params["host"]) == ("idrac", "192.0.2.9")

    second = _mapping(devices[1])
    second_params = _mapping(second["params"])
    assert (second["plugin"], second_params["host"]) == ("ipmi", "192.0.2.20")
    assert second_params["username"] == "root"
    assert second_params["password"] == "!secret idrac_b_password"
    assert second_params["sensor_name"] == "System Level"
    assert second_params["interface"] == "lanplus"
    assert second_params["cipher_suite"] == 3
    assert {"verify_tls", "power_path"}.isdisjoint(second_params)


def test_manifest_union_declares_all_ipmi_specific_parameters_optional() -> None:
    manifest = _mapping(yaml.safe_load(MANIFEST.read_text(encoding="utf-8")))
    schema = _mapping(manifest["schema"])
    devices = schema["devices"]
    assert isinstance(devices, list) and len(devices) == 1
    params = _mapping(_mapping(devices[0])["params"])

    assert params["sensor_name"] == "str?"
    assert params["interface"] == "str?"
    assert params["cipher_suite"] == "int?"


def test_ipmi_plugin_is_discovered_from_the_shipped_package() -> None:
    result = discover()

    assert result.ok
    assert result.plugins["ipmi"] is IpmiPlugin
