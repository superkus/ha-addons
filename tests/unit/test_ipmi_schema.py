"""Schema, defaults, bounds, and credential-secrecy tests for the IPMI plugin."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from remote_energy_meter.config.models import CREDENTIAL_MARKER, credential_field_names
from remote_energy_meter.plugins.ipmi.schema import (
    DEFAULT_CIPHER_SUITE,
    DEFAULT_INTERFACE,
    DEFAULT_REQUEST_TIMEOUT_S,
    DEFAULT_SENSOR_NAME,
    MAX_CIPHER_SUITE,
    MIN_CIPHER_SUITE,
    IpmiParams,
)

VALID_PARAMS: dict[str, Any] = {"host": "192.0.2.20", "username": "root", "password": "legacy-secret"}


def params(**overrides: Any) -> dict[str, Any]:
    return VALID_PARAMS | overrides


@pytest.mark.parametrize("missing", ["host", "username", "password"])
def test_host_username_and_password_are_required(missing: str) -> None:
    incomplete = {name: value for name, value in VALID_PARAMS.items() if name != missing}

    with pytest.raises(ValidationError):
        IpmiParams(**incomplete)


@pytest.mark.parametrize("field", ["host", "username", "password"])
def test_required_fields_reject_empty_values(field: str) -> None:
    with pytest.raises(ValidationError):
        IpmiParams(**params(**{field: ""}))


def test_documented_defaults_match_the_live_validated_path() -> None:
    parsed = IpmiParams(**VALID_PARAMS)

    assert parsed.sensor_name == DEFAULT_SENSOR_NAME == "System Level"
    assert parsed.interface == DEFAULT_INTERFACE == "lanplus"
    assert parsed.cipher_suite == DEFAULT_CIPHER_SUITE == 3
    assert parsed.request_timeout_s == DEFAULT_REQUEST_TIMEOUT_S == 10.0


@pytest.mark.parametrize("cipher_suite", [MIN_CIPHER_SUITE, DEFAULT_CIPHER_SUITE, MAX_CIPHER_SUITE])
def test_cipher_suite_accepts_its_sane_ipmitool_range(cipher_suite: int) -> None:
    assert IpmiParams(**params(cipher_suite=cipher_suite)).cipher_suite == cipher_suite


@pytest.mark.parametrize("cipher_suite", [MIN_CIPHER_SUITE - 1, MAX_CIPHER_SUITE + 1])
def test_cipher_suite_rejects_values_outside_its_range(cipher_suite: int) -> None:
    with pytest.raises(ValidationError):
        IpmiParams(**params(cipher_suite=cipher_suite))


@pytest.mark.parametrize("timeout_s", [1.0, 10.0, 60.0])
def test_request_timeout_accepts_one_through_sixty_seconds(timeout_s: float) -> None:
    assert IpmiParams(**params(request_timeout_s=timeout_s)).request_timeout_s == timeout_s


@pytest.mark.parametrize("timeout_s", [0, 0.99, 60.01, 61])
def test_request_timeout_rejects_values_outside_one_through_sixty(timeout_s: float) -> None:
    with pytest.raises(ValidationError):
        IpmiParams(**params(request_timeout_s=timeout_s))


def test_username_and_password_are_credential_marked_secret_strings() -> None:
    parsed = IpmiParams(**VALID_PARAMS)

    assert credential_field_names(IpmiParams) == ("username", "password")
    for field_name in ("username", "password"):
        extra = IpmiParams.model_fields[field_name].json_schema_extra
        assert isinstance(extra, dict)
        assert extra[CREDENTIAL_MARKER] is True
    assert parsed.username.get_secret_value() == "root"
    assert parsed.password.get_secret_value() == "legacy-secret"


def test_credentials_never_render_in_the_schema_representation() -> None:
    rendered = repr(IpmiParams(**VALID_PARAMS))

    assert "root" not in rendered
    assert "legacy-secret" not in rendered
    assert "192.0.2.20" in rendered


def test_interface_rejects_whitespace_or_option_like_delimiters() -> None:
    with pytest.raises(ValidationError):
        IpmiParams(**params(interface="lanplus --help"))


# --------------------------------------------------------------------------------------------------
# option-injection and URL-smuggling defences on free-text fields
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["192.0.2.20", "bmc.example", "bmc.example:623", "::1", "[fd00::9]", "x_1-2.y"])
def test_host_accepts_hostnames_and_ip_literals(host: str) -> None:
    assert IpmiParams(**params(host=host)).host == host


@pytest.mark.parametrize(
    "host",
    [
        "-H",  # would read as an ipmitool option under a permuting getopt
        "-f/etc/passwd",
        "bmc example",  # whitespace
        "bmc.example\t",
        "user:pw@bmc.example",  # userinfo smuggled into a URL authority
        "bmc.example/redfish",  # interior path
        "bmc.example?x=1",
        "bmc.example#frag",
        "bmc\x00.example",
        "a" * 254,  # over the 253-octet DNS limit
    ],
)
def test_host_rejects_option_like_and_url_smuggling_values(host: str) -> None:
    with pytest.raises(ValidationError):
        IpmiParams(**params(host=host))


@pytest.mark.parametrize("name", ["System Level", "PS1 Input Power", "Pwr Consumption", "x", "Temp (C)", "a-b"])
def test_sensor_name_accepts_ordinary_sdr_record_names(name: str) -> None:
    assert IpmiParams(**params(sensor_name=name)).sensor_name == name


@pytest.mark.parametrize(
    "name",
    [
        "-f/etc/passwd",  # GNU getopt would permute this trailing positional into a global option
        "-S/tmp/other.sdr",
        "--help",
        " System Level",  # leading whitespace hides a following dash
        "System\nLevel",
        "System\x00Level",
    ],
)
def test_sensor_name_rejects_option_like_values(name: str) -> None:
    with pytest.raises(ValidationError):
        IpmiParams(**params(sensor_name=name))
