"""Unit tests for the iDRAC connection parameter schema (task 10.1).

Covers Requirement 4.1 field for field: requiredness, each declared bound, each default,
and the credential marker that Requirement 8.1 keys redaction off. Requirement 4.10's
default resource path is covered through the fallback the schema exposes.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from remote_energy_meter.config.models import CREDENTIAL_MARKER, credential_field_names
from remote_energy_meter.plugins.idrac.schema import (
    DEFAULT_POWER_PATH,
    DEFAULT_REQUEST_TIMEOUT_S,
    MAX_PASSWORD_LENGTH,
    MAX_POWER_PATH_LENGTH,
    MAX_USERNAME_LENGTH,
    IdracParams,
)

VALID_PARAMS: dict[str, Any] = {"host": "192.0.2.9", "username": "root", "password": "calvin"}


def params(**overrides: Any) -> dict[str, Any]:
    """A minimal valid parameter mapping with the given fields replaced or added."""
    return VALID_PARAMS | overrides


def first_error_field(exc: ValidationError) -> str:
    return str(exc.errors()[0]["loc"][0])


# --------------------------------------------------------------------------------------------------
# requiredness
# --------------------------------------------------------------------------------------------------


def test_a_minimal_entry_needs_only_host_username_and_password() -> None:
    """Requirement 4.1: exactly three required fields; everything else carries a default."""
    parsed = IdracParams(**VALID_PARAMS)

    assert parsed.host == "192.0.2.9"
    assert parsed.username.get_secret_value() == "root"
    assert parsed.password.get_secret_value() == "calvin"


@pytest.mark.parametrize("missing", ["host", "username", "password"])
def test_omitting_a_required_field_is_a_validation_failure(missing: str) -> None:
    """Requirement 4.1: host, username, and password are required."""
    incomplete = {name: value for name, value in VALID_PARAMS.items() if name != missing}

    with pytest.raises(ValidationError) as caught:
        IdracParams(**incomplete)

    assert first_error_field(caught.value) == missing


@pytest.mark.parametrize("field", ["host", "username", "password"])
def test_an_empty_required_field_is_a_validation_failure(field: str) -> None:
    """Requirement 2.2 treats an empty required field as a validation failure, not a present value."""
    with pytest.raises(ValidationError) as caught:
        IdracParams(**params(**{field: ""}))

    assert first_error_field(caught.value) == field


# --------------------------------------------------------------------------------------------------
# bounds
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("length", [1, MAX_USERNAME_LENGTH])
def test_username_accepts_its_boundary_lengths(length: int) -> None:
    """Requirement 4.1: a username of 1 to 64 characters."""
    assert IdracParams(**params(username="u" * length)).username.get_secret_value() == "u" * length


def test_username_rejects_one_character_over_the_bound() -> None:
    with pytest.raises(ValidationError) as caught:
        IdracParams(**params(username="u" * (MAX_USERNAME_LENGTH + 1)))

    assert first_error_field(caught.value) == "username"


@pytest.mark.parametrize("length", [1, MAX_PASSWORD_LENGTH])
def test_password_accepts_its_boundary_lengths(length: int) -> None:
    """Requirement 4.1: a password of 1 to 128 characters."""
    assert IdracParams(**params(password="p" * length)).password.get_secret_value() == "p" * length


def test_password_rejects_one_character_over_the_bound() -> None:
    with pytest.raises(ValidationError) as caught:
        IdracParams(**params(password="p" * (MAX_PASSWORD_LENGTH + 1)))

    assert first_error_field(caught.value) == "password"


def test_power_path_accepts_the_maximum_length() -> None:
    """Requirement 4.1: a power path of at most 512 characters."""
    path = "/" + "p" * (MAX_POWER_PATH_LENGTH - 1)

    assert IdracParams(**params(power_path=path)).power_path == path


def test_power_path_rejects_one_character_over_the_bound() -> None:
    with pytest.raises(ValidationError) as caught:
        IdracParams(**params(power_path="p" * (MAX_POWER_PATH_LENGTH + 1)))

    assert first_error_field(caught.value) == "power_path"


@pytest.mark.parametrize("timeout", [1.0, 10.0, 60.0])
def test_request_timeout_accepts_its_range(timeout: float) -> None:
    """Requirement 4.1: a request timeout of 1 to 60 seconds inclusive."""
    assert IdracParams(**params(request_timeout_s=timeout)).request_timeout_s == timeout


@pytest.mark.parametrize("timeout", [0.999, 0, -1, 60.001, 61])
def test_request_timeout_rejects_values_outside_its_range(timeout: float) -> None:
    with pytest.raises(ValidationError) as caught:
        IdracParams(**params(request_timeout_s=timeout))

    assert first_error_field(caught.value) == "request_timeout_s"


# --------------------------------------------------------------------------------------------------
# defaults
# --------------------------------------------------------------------------------------------------


def test_optional_fields_carry_the_documented_defaults() -> None:
    """Requirement 4.1: verify_tls true, power_path absent, request timeout 10 seconds."""
    parsed = IdracParams(**VALID_PARAMS)

    assert parsed.verify_tls is True
    assert parsed.power_path is None
    assert parsed.request_timeout_s == DEFAULT_REQUEST_TIMEOUT_S


def test_tls_verification_defaults_on_and_is_opt_out_only() -> None:
    """The design's decision: the insecure setting is an explicit administrator choice per device."""
    assert IdracParams(**VALID_PARAMS).verify_tls is True
    assert IdracParams(**params(verify_tls=False)).verify_tls is False


def test_an_omitted_power_path_resolves_to_the_default_chassis_resource() -> None:
    """Requirement 4.10: an omitted path means the default Redfish chassis power resource."""
    assert IdracParams(**VALID_PARAMS).resolved_power_path == DEFAULT_POWER_PATH
    assert DEFAULT_POWER_PATH == "/redfish/v1/Chassis/System.Embedded.1/Power"


def test_a_supplied_power_path_overrides_the_default() -> None:
    override = "/redfish/v1/Chassis/1/Power"

    assert IdracParams(**params(power_path=override)).resolved_power_path == override


def test_an_explicit_request_timeout_is_distinguishable_from_the_default() -> None:
    """Requirement 5.8 needs to know whether a device carries a request timeout at all."""
    assert "request_timeout_s" not in IdracParams(**VALID_PARAMS).model_fields_set
    assert "request_timeout_s" in IdracParams(**params(request_timeout_s=10.0)).model_fields_set


# --------------------------------------------------------------------------------------------------
# credential marking
# --------------------------------------------------------------------------------------------------


def test_username_and_password_are_the_credential_fields() -> None:
    """Requirement 8.1: the core derives the credential set from the marker, so it must be present."""
    assert credential_field_names(IdracParams) == ("username", "password")


@pytest.mark.parametrize("field", ["username", "password"])
def test_each_credential_field_carries_the_shared_marker(field: str) -> None:
    """The marker key must be the core's, not a plugin-local spelling, or redaction misses it."""
    extra = IdracParams.model_fields[field].json_schema_extra

    assert isinstance(extra, dict)
    assert extra[CREDENTIAL_MARKER] is True


@pytest.mark.parametrize("field", ["host", "verify_tls", "power_path", "request_timeout_s"])
def test_non_credential_fields_are_not_marked(field: str) -> None:
    assert field not in credential_field_names(IdracParams)


def test_credential_values_do_not_render_in_repr() -> None:
    """Requirement 8.2's first layer: an accidental interpolation of the params cannot leak."""
    rendered = repr(IdracParams(**params(username="root", password="s3cr3t-p4ssw0rd")))

    assert "s3cr3t-p4ssw0rd" not in rendered
    assert "root" not in rendered
    assert "192.0.2.9" in rendered


# --------------------------------------------------------------------------------------------------
# the host becomes a URL authority: nothing may smuggle a path, userinfo, or query into it
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    ["192.0.2.9", "idrac.example", "idrac.example:8443", "[fd00::9]:8443", "::1", "https://192.0.2.9", "192.0.2.9/"],
)
def test_host_accepts_hostnames_and_ip_literals_and_the_documented_scheme_and_slash_forms(host: str) -> None:
    assert IdracParams(**params(host=host)).host == host


@pytest.mark.parametrize(
    "host",
    [
        "-idrac.example",
        "user:pw@idrac.example",
        "idrac.example/redfish/v1",  # an interior path would change the resource read
        "https://idrac.example/redfish/v1",
        "ftp://idrac.example",
        "idrac.example?x=1",
        "idrac.example#x",
        "idrac example",
        "idrac\x00.example",
    ],
)
def test_host_rejects_values_that_would_change_the_request_target(host: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        IdracParams(**params(host=host))
    assert first_error_field(excinfo.value) == "host"
