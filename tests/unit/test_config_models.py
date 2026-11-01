"""Unit tests for the configuration model field constraints (task 2.4).

These tests pin the bounds that Requirements 2.11, 2.12, 2.1, 2.6, 5.2, and 9.6 fix, and the
first redaction layer of Requirement 8.2: a credential field never renders plaintext.

Bounds are referenced through the module's named constants rather than restated as literals, so a
future bound change fails in `config/models.py` alone instead of here as well. Where a boundary is
exercised the constant is offset (``MAX_PORT + 1``), which keeps the test about the *edge* rather
than about a particular number.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from pydantic import BaseModel, Field, SecretStr, ValidationError

from remote_energy_meter.config.models import (
    CREDENTIAL_MARKER,
    DEFAULT_POLL_INTERVAL_S,
    DEVICE_ID_PATTERN,
    MAX_DEVICE_ID_LENGTH,
    MAX_DEVICES,
    MAX_PLUGIN_ID_LENGTH,
    MAX_POLL_INTERVAL_S,
    MAX_PORT,
    MIN_DEVICES,
    MIN_POLL_INTERVAL_S,
    MIN_PORT,
    ConfigurationObject,
    DeviceEntry,
    LogLevel,
    MqttSettings,
    credential_field_names,
)

#: A password distinctive enough that any leak into text output is unambiguous.
SECRET_PASSWORD = "s3cret-idrac-pw"


def make_device(device_id: str = "poweredge_a", **overrides: Any) -> dict[str, Any]:
    """A minimal valid Device_Entry payload, overridable field by field."""
    payload: dict[str, Any] = {
        "device_id": device_id,
        "plugin": "idrac",
        "params": {"host": "192.0.2.9"},
    }
    payload.update(overrides)
    return payload


def make_mqtt(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"host": "core-mosquitto", "port": 1883}
    payload.update(overrides)
    return payload


def make_config(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"mqtt": make_mqtt(), "devices": [make_device()]}
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------------------------------
# device identifier (Requirement 2.12)
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "device_id",
    [
        "a",  # shortest accepted form
        "poweredge_a",
        "PowerEdge-B",
        "srv-01_rack2",
        "0",  # digits only
        "-",  # hyphen only: the pattern constrains characters, not their arrangement
        "_",
        "A" * MAX_DEVICE_ID_LENGTH,  # upper length boundary
    ],
)
def test_device_entry_accepts_letters_digits_hyphen_underscore(device_id: str) -> None:
    """Requirement 2.12: letters, digits, hyphen, and underscore up to 64 characters are the accepted form."""
    assert DeviceEntry.model_validate(make_device(device_id)).device_id == device_id


@pytest.mark.parametrize(
    "device_id",
    [
        "",  # empty
        "poweredge.a",  # dot
        "poweredge a",  # space
        "poweredge/a",  # slash
        "poweredge:a",  # colon
        "poweredge+a",
        "poweredge#a",
        "powerédge",  # non-ASCII letter
        "power\nedge",  # newline, which a naive anchored regex would let pass
        "\tpoweredge",
    ],
)
def test_device_entry_rejects_identifiers_outside_the_accepted_character_set(device_id: str) -> None:
    """Requirement 2.12: an empty identifier, or any character outside the set, is a rejection."""
    with pytest.raises(ValidationError) as excinfo:
        DeviceEntry.model_validate(make_device(device_id))

    assert excinfo.value.errors()[0]["loc"] == ("device_id",)


def test_device_entry_rejects_an_identifier_one_character_over_the_limit() -> None:
    """Requirement 2.12: the 64/65 character boundary, from the far side."""
    with pytest.raises(ValidationError, match="device_id"):
        DeviceEntry.model_validate(make_device("A" * (MAX_DEVICE_ID_LENGTH + 1)))


def test_device_id_pattern_is_the_declared_character_set() -> None:
    """The exported pattern and the field constraint describe the same set, so the parser can reuse it.

    Requirement 2.12 names the accepted character set, and `config/parser.py` reports a rejected
    identifier against it; the pattern being anchored on both ends is what makes that report agree
    with the model's own verdict.
    """
    compiled = re.compile(DEVICE_ID_PATTERN)

    assert compiled.match("poweredge_a-1") is not None
    assert compiled.match("poweredge.a") is None
    assert compiled.match("poweredge\nbad") is None  # the trailing anchor rejects, rather than ignores, the tail


# --------------------------------------------------------------------------------------------------
# plugin identifier
# --------------------------------------------------------------------------------------------------


def test_device_entry_requires_a_non_empty_plugin_identifier() -> None:
    """Requirement 3.3 resolves on an exact plugin identifier, so an empty one can never resolve."""
    with pytest.raises(ValidationError, match="plugin"):
        DeviceEntry.model_validate(make_device(plugin=""))


def test_device_entry_accepts_a_plugin_identifier_on_the_length_boundary() -> None:
    """Requirement 3.1 bounds the plugin identifier at 64 characters, and the entry names one."""
    plugin_id = "p" * MAX_PLUGIN_ID_LENGTH

    assert DeviceEntry.model_validate(make_device(plugin=plugin_id)).plugin == plugin_id


def test_device_entry_rejects_a_plugin_identifier_one_character_over_the_limit() -> None:
    with pytest.raises(ValidationError) as excinfo:
        DeviceEntry.model_validate(make_device(plugin="p" * (MAX_PLUGIN_ID_LENGTH + 1)))

    assert excinfo.value.errors()[0]["loc"] == ("plugin",)


def test_device_entry_defaults_display_name_and_poll_interval_to_absent() -> None:
    """Both are optional: the display name falls back to the identifier, the interval to the global value."""
    entry = DeviceEntry.model_validate(make_device())

    assert entry.display_name is None
    assert entry.poll_interval_s is None


# --------------------------------------------------------------------------------------------------
# MQTT broker host and port (Requirement 2.11)
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("port", [MIN_PORT, 1883, MAX_PORT])
def test_mqtt_settings_accepts_ports_on_and_inside_the_bounds(port: int) -> None:
    assert MqttSettings.model_validate(make_mqtt(port=port)).port == port


@pytest.mark.parametrize("port", [MIN_PORT - 1, MIN_PORT - 2, MAX_PORT + 1, 100_000])
def test_mqtt_settings_rejects_ports_outside_the_bounds(port: int) -> None:
    """Requirement 2.11: a port outside 1-65535 names the offending field."""
    with pytest.raises(ValidationError) as excinfo:
        MqttSettings.model_validate(make_mqtt(port=port))

    assert excinfo.value.errors()[0]["loc"] == ("port",)


def test_mqtt_settings_requires_host_and_port() -> None:
    """Requirement 2.11: omitting either is a rejection, and both are reported."""
    with pytest.raises(ValidationError) as excinfo:
        MqttSettings.model_validate({})

    assert {error["loc"] for error in excinfo.value.errors()} == {("host",), ("port",)}


def test_mqtt_settings_rejects_an_empty_host() -> None:
    with pytest.raises(ValidationError, match="host"):
        MqttSettings.model_validate(make_mqtt(host=""))


def test_mqtt_settings_defaults_identity_topics_and_tls() -> None:
    """Defaults are resolved on the model so the Configuration_Printer can round-trip them (Requirement 2.8)."""
    mqtt = MqttSettings.model_validate(make_mqtt())

    assert (mqtt.client_id, mqtt.base_topic, mqtt.discovery_prefix, mqtt.tls) == (
        "remote_energy_meter",
        "remote_energy_meter",
        "homeassistant",
        False,
    )
    assert mqtt.username is None
    assert mqtt.password is None


# --------------------------------------------------------------------------------------------------
# device list bounds (Requirement 2.1)
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("count", [MIN_DEVICES, 2, MAX_DEVICES])
def test_configuration_object_accepts_device_lists_on_and_inside_the_bounds(count: int) -> None:
    devices = [make_device(f"dev{index}") for index in range(count)]

    assert len(ConfigurationObject.model_validate(make_config(devices=devices)).devices) == count


@pytest.mark.parametrize("count", [MIN_DEVICES - 1, MAX_DEVICES + 1])
def test_configuration_object_rejects_device_lists_outside_the_bounds(count: int) -> None:
    """Requirement 2.1: the device list holds 1 to 50 entries."""
    devices = [make_device(f"dev{index}") for index in range(count)]

    with pytest.raises(ValidationError) as excinfo:
        ConfigurationObject.model_validate(make_config(devices=devices))

    assert excinfo.value.errors()[0]["loc"] == ("devices",)


def test_configuration_object_preserves_device_list_order() -> None:
    """Requirement 2.5 resolves duplicates by first occurrence, which is only well defined if order survives."""
    devices = [make_device(f"dev{index}") for index in range(5)]

    config = ConfigurationObject.model_validate(make_config(devices=devices))

    assert [entry.device_id for entry in config.devices] == [f"dev{index}" for index in range(5)]


def test_configuration_object_requires_the_mqtt_section() -> None:
    """Requirement 2.1: exactly one MQTT connection section, so it cannot be omitted."""
    with pytest.raises(ValidationError, match="mqtt"):
        ConfigurationObject.model_validate({"devices": [make_device()]})


# --------------------------------------------------------------------------------------------------
# poll interval bounds and default (Requirements 2.6, 2.7, 5.2)
# --------------------------------------------------------------------------------------------------


def test_configuration_object_defaults_the_global_poll_interval_to_thirty_seconds() -> None:
    """Requirement 2.6: an omitted global poll interval resolves to 30 seconds."""
    assert DEFAULT_POLL_INTERVAL_S == 30
    assert ConfigurationObject.model_validate(make_config()).poll_interval_s == DEFAULT_POLL_INTERVAL_S


@pytest.mark.parametrize("interval", [MIN_POLL_INTERVAL_S, DEFAULT_POLL_INTERVAL_S, MAX_POLL_INTERVAL_S])
def test_configuration_object_accepts_global_intervals_on_and_inside_the_bounds(interval: int) -> None:
    assert ConfigurationObject.model_validate(make_config(poll_interval_s=interval)).poll_interval_s == interval


@pytest.mark.parametrize("interval", [MIN_POLL_INTERVAL_S - 1, 0, -1, MAX_POLL_INTERVAL_S + 1])
def test_configuration_object_rejects_global_intervals_outside_the_bounds(interval: int) -> None:
    """Requirement 2.7: an out-of-range global interval is a rejection naming the field."""
    with pytest.raises(ValidationError) as excinfo:
        ConfigurationObject.model_validate(make_config(poll_interval_s=interval))

    assert excinfo.value.errors()[0]["loc"] == ("poll_interval_s",)


@pytest.mark.parametrize("interval", [30.5, "30s"])
def test_configuration_object_rejects_a_global_interval_that_is_not_whole_seconds(interval: object) -> None:
    """Requirement 2.7: the interval is expressed in whole seconds, so a fractional value is rejected."""
    with pytest.raises(ValidationError, match="poll_interval_s"):
        ConfigurationObject.model_validate(make_config(poll_interval_s=interval))


@pytest.mark.parametrize("interval", [MIN_POLL_INTERVAL_S, 60, MAX_POLL_INTERVAL_S])
def test_device_entry_accepts_device_intervals_on_and_inside_the_bounds(interval: int) -> None:
    """Requirement 5.2: a device-specific interval within 5-3600 seconds is carried on the entry."""
    assert DeviceEntry.model_validate(make_device(poll_interval_s=interval)).poll_interval_s == interval


@pytest.mark.parametrize("interval", [MIN_POLL_INTERVAL_S - 1, 0, MAX_POLL_INTERVAL_S + 1])
def test_device_entry_rejects_device_intervals_outside_the_bounds(interval: int) -> None:
    with pytest.raises(ValidationError) as excinfo:
        DeviceEntry.model_validate(make_device(poll_interval_s=interval))

    assert excinfo.value.errors()[0]["loc"] == ("poll_interval_s",)


# --------------------------------------------------------------------------------------------------
# log level (Requirements 9.1, 9.2, 9.6)
# --------------------------------------------------------------------------------------------------


def test_log_level_members_are_the_four_accepted_severities() -> None:
    """Requirement 9.1: exactly one severity per record, drawn from this set."""
    assert [level.value for level in LogLevel] == ["debug", "info", "warning", "error"]


def test_log_level_declaration_order_is_the_severity_ranking() -> None:
    """Requirement 9.2 ranks debug < info < warning < error, and the logger derives its filter from this order."""
    order = list(LogLevel)

    assert order.index(LogLevel.DEBUG) < order.index(LogLevel.INFO)
    assert order.index(LogLevel.INFO) < order.index(LogLevel.WARNING)
    assert order.index(LogLevel.WARNING) < order.index(LogLevel.ERROR)


@pytest.mark.parametrize("level", ["debug", "info", "warning", "error"])
def test_configuration_object_accepts_every_declared_log_level(level: str) -> None:
    assert ConfigurationObject.model_validate(make_config(log_level=level)).log_level == LogLevel(level)


@pytest.mark.parametrize("level", ["INFO", "trace", "critical", "verbose", ""])
def test_configuration_object_rejects_a_log_level_outside_the_set(level: str) -> None:
    """An out-of-set log level is rejected here.

    Requirement 9.7's fallback to `info` with a warning belongs to the parser, not to the model.
    """
    with pytest.raises(ValidationError) as excinfo:
        ConfigurationObject.model_validate(make_config(log_level=level))

    assert excinfo.value.errors()[0]["loc"] == ("log_level",)


def test_configuration_object_defaults_the_log_level_to_info() -> None:
    """Requirement 9.6: an omitted log level resolves to `info`."""
    assert ConfigurationObject.model_validate(make_config()).log_level is LogLevel.INFO


# --------------------------------------------------------------------------------------------------
# credential fields (Requirements 8.1, 8.2)
# --------------------------------------------------------------------------------------------------


def test_credential_field_names_reports_the_marked_mqtt_fields_in_declaration_order() -> None:
    """Requirement 8.1: the credential value set is derived from markers, not from hard-coded field names."""
    assert credential_field_names(MqttSettings) == ("username", "password")


def test_credential_field_names_is_empty_for_a_model_without_credentials() -> None:
    """A Device_Entry carries no credential of its own; its credentials live in the plugin-validated params."""
    assert credential_field_names(DeviceEntry) == ()
    assert credential_field_names(ConfigurationObject) == ()


def test_credential_field_names_finds_a_marker_on_a_plugin_declared_schema() -> None:
    """The marker is the contract a plugin schema uses, so the core finds credentials in a model it has never seen."""

    class PluginParams(BaseModel):
        host: str
        token: SecretStr = Field(json_schema_extra={CREDENTIAL_MARKER: True})

    assert credential_field_names(PluginParams) == ("token",)


def test_credential_marker_is_the_documented_key() -> None:
    """Plugin authors write this key literally, so its value is part of the published contract."""
    assert CREDENTIAL_MARKER == "credential"


def test_mqtt_settings_repr_does_not_render_the_password_plaintext() -> None:
    """Requirement 8.2: the typed value is the first redaction layer, before the logging filter ever runs."""
    mqtt = MqttSettings.model_validate(make_mqtt(username="addons", password=SECRET_PASSWORD))

    assert SECRET_PASSWORD not in repr(mqtt)
    assert SECRET_PASSWORD not in str(mqtt)
    assert SECRET_PASSWORD not in repr(mqtt.password)
    assert SECRET_PASSWORD not in str(mqtt.password)


def test_configuration_object_repr_does_not_render_a_nested_password_plaintext() -> None:
    """Interpolating the whole configuration object -- the likeliest accident -- cannot leak a credential."""
    config = ConfigurationObject.model_validate(
        make_config(mqtt=make_mqtt(username="addons", password=SECRET_PASSWORD)),
    )

    assert SECRET_PASSWORD not in repr(config)
    assert SECRET_PASSWORD not in str(config)
    assert SECRET_PASSWORD not in config.model_dump_json()


def test_credential_value_is_still_reachable_through_an_explicit_call() -> None:
    """Redaction must not cost correctness: the broker client needs the real value on purpose."""
    mqtt = MqttSettings.model_validate(make_mqtt(password=SECRET_PASSWORD))

    assert mqtt.password is not None
    assert mqtt.password.get_secret_value() == SECRET_PASSWORD


def test_credential_redaction_marker_length_is_independent_of_the_credential_length() -> None:
    """Requirement 8.2: the rendered marker is fixed-length, so it reveals nothing about the credential."""
    short = MqttSettings.model_validate(make_mqtt(password="a"))
    long = MqttSettings.model_validate(make_mqtt(password="a" * 200))

    assert repr(short.password) == repr(long.password)
