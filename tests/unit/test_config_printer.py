"""Unit tests for the Configuration_Printer (task 4.3).

Requirement 2.8 is one sentence -- the printed text is text the parser accepts -- and it
hides three ways to get it wrong, one per group of tests below:

* **Omitting a field that holds its default.** The parser would re-derive that field from
  its own default, which agrees today and stops agreeing the moment a default changes. The
  tests assert the printed document carries *every* declared field of every model, keyed
  off ``model_fields`` rather than a list written here, so a field added to a model without
  being printed fails a test instead of quietly breaking the round trip.
* **Normalizing ``params``.** Dumping a validated plugin model would write every schema
  default in explicitly and destroy the ``model_fields_set`` distinction the Poll_Scheduler
  needs to tell an omitted per-device request timeout from an explicitly configured one
  (Requirement 5.8).
* **Printing the credential mask instead of the credential.** ``SecretStr`` renders as
  ``**********`` under ``str`` and under ``model_dump``, so the obvious implementation
  prints a configuration that cannot authenticate -- and round-trips the mask itself into a
  password. One test therefore asserts the plaintext *is* present, which is the documented,
  intended behaviour, and a companion test pins that the printer reaches no log record with
  it (Requirement 8.2).

The round-trip assertion goes through the real :func:`parse_options` rather than a stub,
because "text the parser accepts" is a statement about that function. Property 1 (task 4.4)
generalizes the same claim over generated configurations; these tests fix the examples that
say *which* fields matter.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any, Final

import pytest
from pydantic import SecretStr

from remote_energy_meter.config.models import (
    ConfigurationObject,
    DeviceEntry,
    LogLevel,
    MqttSettings,
)
from remote_energy_meter.config.parser import PluginConfigSchema, parse_options
from remote_energy_meter.config.printer import as_options_document, print_config
from remote_energy_meter.config.secrets import SECRET_TAG, SecretResolver
from remote_energy_meter.diagnostics.redaction import CredentialRegistry
from remote_energy_meter.plugins.idrac.schema import DEFAULT_REQUEST_TIMEOUT_S, IdracParams

LOGGER_NAME: Final = "tests.config_printer"

#: Distinctive enough that finding them anywhere is unambiguous.
IDRAC_PASSWORD: Final = "hunter2-idrac-pw"
IDRAC_USERNAME: Final = "root-idrac-user"
MQTT_PASSWORD: Final = "broker-secret-pw"
MQTT_USERNAME: Final = "addons-broker-user"

#: What ``SecretStr`` renders instead of its value. Printing this would be the bug.
SECRET_MASK: Final = "**********"

SCHEMAS: Final[Mapping[str, PluginConfigSchema]] = {"idrac": IdracParams}


# --------------------------------------------------------------------------------------------------
# fixtures and helpers
# --------------------------------------------------------------------------------------------------


@pytest.fixture
def registry() -> CredentialRegistry:
    """A registry private to one test, so the process-wide one is never touched."""
    return CredentialRegistry()


@pytest.fixture
def log(caplog: pytest.LogCaptureFixture) -> logging.Logger:
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
    return logging.getLogger(LOGGER_NAME)


def idrac_params(**overrides: Any) -> dict[str, Any]:
    """iDRAC parameters that omit ``request_timeout_s`` and ``power_path`` on purpose.

    The omissions are what Requirement 5.8 rests on: the scheduler must be able to see that
    the administrator said nothing about the timeout.
    """
    params: dict[str, Any] = {
        "host": "192.0.2.9",
        "username": IDRAC_USERNAME,
        "password": IDRAC_PASSWORD,
        "verify_tls": False,
    }
    params.update(overrides)
    return params


def configuration(**overrides: Any) -> ConfigurationObject:
    """A Configuration_Object covering both devices of the target environment."""
    fields: dict[str, Any] = {
        "poll_interval_s": 45,
        "log_level": LogLevel.DEBUG,
        "mqtt": MqttSettings(
            host="core-mosquitto",
            port=1883,
            username=SecretStr(MQTT_USERNAME),
            password=SecretStr(MQTT_PASSWORD),
        ),
        "devices": [
            DeviceEntry(
                device_id="poweredge_a",
                plugin="idrac",
                display_name="PowerEdge A",
                params=idrac_params(),
            ),
            DeviceEntry(
                device_id="poweredge_b",
                plugin="idrac",
                poll_interval_s=60,
                params=idrac_params(host="192.0.2.20", request_timeout_s=7.5),
            ),
        ],
    }
    fields.update(overrides)
    return ConfigurationObject(**fields)


def reparse(
    config: ConfigurationObject,
    *,
    log: logging.Logger,
    registry: CredentialRegistry,
) -> ConfigurationObject:
    """Print ``config``, parse the text back, and assert the parse recorded nothing.

    Requirement 2.8 is exactly this: accepted, with no validation failure. The secret
    resolver is backed by an empty in-memory store, so a printed literal that the parser
    tried to resolve as a reference would fail loudly here rather than silently succeed.
    """
    outcome = parse_options(
        print_config(config),
        schemas=SCHEMAS,
        secrets=SecretResolver.from_mapping({}, registry=registry),
        logger=log,
        registry=registry,
    )
    assert outcome.fatal is None
    assert outcome.rejections == ()
    assert outcome.config is not None
    return outcome.config


def errors(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.levelno == logging.ERROR]


# --------------------------------------------------------------------------------------------------
# every field is emitted, including the ones holding defaults
# --------------------------------------------------------------------------------------------------


def test_every_declared_field_is_emitted() -> None:
    document = as_options_document(configuration())

    assert list(document) == list(ConfigurationObject.model_fields)
    assert list(document["mqtt"]) == list(MqttSettings.model_fields)
    for entry in document["devices"]:
        assert list(entry) == list(DeviceEntry.model_fields)


def test_fields_holding_defaults_are_emitted_explicitly() -> None:
    """A default-valued field is printed, not omitted for the parser to re-derive."""
    config = ConfigurationObject(
        mqtt=MqttSettings(host="core-mosquitto", port=1883),
        devices=[DeviceEntry(device_id="poweredge_a", plugin="idrac", params=idrac_params())],
    )

    document = as_options_document(config)

    # Global defaults (Requirements 2.6, 9.6), written out rather than left implicit.
    assert document["poll_interval_s"] == 30
    assert document["log_level"] == "info"
    # MQTT defaults, including the ones whose value is falsy.
    assert document["mqtt"]["client_id"] == "remote_energy_meter"
    assert document["mqtt"]["base_topic"] == "remote_energy_meter"
    assert document["mqtt"]["discovery_prefix"] == "homeassistant"
    assert document["mqtt"]["tls"] is False
    # Optional fields that are genuinely unset are emitted as null, not dropped.
    assert document["mqtt"]["username"] is None
    assert document["mqtt"]["password"] is None
    assert document["devices"][0]["display_name"] is None
    assert document["devices"][0]["poll_interval_s"] is None


def test_default_valued_configuration_round_trips() -> None:
    """The abbreviated case is where an omitted default would bite; it must survive too."""
    config = ConfigurationObject(
        mqtt=MqttSettings(host="core-mosquitto", port=1883),
        devices=[DeviceEntry(device_id="poweredge_a", plugin="idrac", params=idrac_params())],
    )
    registry = CredentialRegistry()

    restored = reparse(config, log=logging.getLogger(LOGGER_NAME), registry=registry)

    assert as_options_document(restored) == as_options_document(config)


# --------------------------------------------------------------------------------------------------
# the round trip (Requirements 2.8, 2.9)
# --------------------------------------------------------------------------------------------------


def test_printed_text_round_trips_field_by_field(
    log: logging.Logger,
    registry: CredentialRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = configuration()

    restored = reparse(config, log=log, registry=registry)

    assert errors(caplog) == []
    assert restored.poll_interval_s == config.poll_interval_s
    assert restored.log_level is config.log_level

    assert restored.mqtt.host == config.mqtt.host
    assert restored.mqtt.port == config.mqtt.port
    assert restored.mqtt.client_id == config.mqtt.client_id
    assert restored.mqtt.base_topic == config.mqtt.base_topic
    assert restored.mqtt.discovery_prefix == config.mqtt.discovery_prefix
    assert restored.mqtt.tls == config.mqtt.tls
    # Credentials survive as values, not as masks.
    assert restored.mqtt.username is not None
    assert restored.mqtt.username.get_secret_value() == MQTT_USERNAME
    assert restored.mqtt.password is not None
    assert restored.mqtt.password.get_secret_value() == MQTT_PASSWORD

    assert [entry.device_id for entry in restored.devices] == ["poweredge_a", "poweredge_b"]
    for original, seen in zip(config.devices, restored.devices, strict=True):
        assert seen.device_id == original.device_id
        assert seen.plugin == original.plugin
        assert seen.display_name == original.display_name
        assert seen.poll_interval_s == original.poll_interval_s
        assert seen.params == original.params


def test_printing_is_a_fixed_point_after_one_parse(
    log: logging.Logger,
    registry: CredentialRegistry,
) -> None:
    """print-parse-print is byte-identical, so the first pass already normalized."""
    config = configuration()
    once = print_config(config)

    restored = reparse(config, log=log, registry=registry)

    assert print_config(restored) == once


def test_printed_text_is_json_with_unescaped_non_ascii(
    log: logging.Logger,
    registry: CredentialRegistry,
) -> None:
    """Non-ASCII text stays readable: the options file is read by administrators too.

    The parser reads the options file as UTF-8, so an unescaped character round-trips; the
    display name carries it because a device identifier is restricted to
    ``[A-Za-z0-9_-]`` (Requirement 2.12).
    """
    config = configuration(
        devices=[
            DeviceEntry(
                device_id="poweredge_a",
                plugin="idrac",
                display_name="PowerEdge Ä (Küche)",
                params=idrac_params(),
            )
        ]
    )

    text = print_config(config)

    assert text.endswith("\n")
    assert "PowerEdge Ä (Küche)" in text
    assert json.loads(text) == as_options_document(config)

    restored = reparse(config, log=log, registry=registry)
    assert restored.devices[0].display_name == "PowerEdge Ä (Küche)"


def test_device_order_is_preserved(log: logging.Logger, registry: CredentialRegistry) -> None:
    """Requirement 2.1 keeps the device list ordered, which is what makes 2.5 well defined."""
    ordered = [f"device_{index:02d}" for index in range(6)]
    config = configuration(
        devices=[DeviceEntry(device_id=device_id, plugin="idrac", params=idrac_params()) for device_id in ordered]
    )

    restored = reparse(config, log=log, registry=registry)

    assert [entry.device_id for entry in restored.devices] == ordered


# --------------------------------------------------------------------------------------------------
# params stay unnormalized (Requirement 5.8)
# --------------------------------------------------------------------------------------------------


def test_params_are_emitted_key_for_key_without_schema_defaults() -> None:
    document = as_options_document(configuration())

    first = document["devices"][0]["params"]
    assert first == idrac_params()
    # Nothing the administrator omitted appears; a dumped plugin model would add both.
    assert "request_timeout_s" not in first
    assert "power_path" not in first


def test_round_trip_preserves_an_omitted_request_timeout(
    log: logging.Logger,
    registry: CredentialRegistry,
) -> None:
    """The scheduler distinguishes omitted from explicit through ``model_fields_set``."""
    config = configuration()

    restored = reparse(config, log=log, registry=registry)

    omitted = IdracParams.model_validate(restored.devices[0].params)
    assert "request_timeout_s" not in omitted.model_fields_set
    assert omitted.request_timeout_s == DEFAULT_REQUEST_TIMEOUT_S

    explicit = IdracParams.model_validate(restored.devices[1].params)
    assert "request_timeout_s" in explicit.model_fields_set
    assert explicit.request_timeout_s == 7.5


def test_an_explicit_value_equal_to_the_schema_default_stays_explicit(
    log: logging.Logger,
    registry: CredentialRegistry,
) -> None:
    """The case where normalizing would be undetectable by value alone."""
    config = configuration(
        devices=[
            DeviceEntry(
                device_id="poweredge_a",
                plugin="idrac",
                params=idrac_params(request_timeout_s=DEFAULT_REQUEST_TIMEOUT_S),
            )
        ]
    )

    restored = reparse(config, log=log, registry=registry)

    params = IdracParams.model_validate(restored.devices[0].params)
    assert "request_timeout_s" in params.model_fields_set


def test_params_mapping_is_copied_not_aliased() -> None:
    """Mutating the printed document must not reach back into the Configuration_Object."""
    config = configuration()
    document = as_options_document(config)

    document["devices"][0]["params"]["host"] = "198.51.100.1"

    assert config.devices[0].params["host"] == "192.0.2.9"


# --------------------------------------------------------------------------------------------------
# credentials: plaintext in the return value, nowhere else
# --------------------------------------------------------------------------------------------------


def test_printed_text_carries_credential_plaintext() -> None:
    """The documented, intended behaviour: literal values, never the ``SecretStr`` mask.

    This is asserted rather than assumed so the security constraint is explicit. The text
    is a credential-bearing value, which is why the module docstring forbids routing it to a
    log record (Requirement 8.2) or an MQTT payload (Requirement 8.3), and why nothing here
    hands it to either.
    """
    text = print_config(configuration())

    assert MQTT_USERNAME in text
    assert MQTT_PASSWORD in text
    assert IDRAC_USERNAME in text
    assert IDRAC_PASSWORD in text
    assert SECRET_MASK not in text


def test_printing_emits_no_log_record(caplog: pytest.LogCaptureFixture) -> None:
    """The printer holds plaintext, so it must reach no logging sink at all."""
    caplog.set_level(logging.DEBUG)

    print_config(configuration())

    assert caplog.records == []


def test_a_reference_shaped_credential_is_printed_verbatim() -> None:
    """A literal that looks like a ``!secret`` reference is not quoted, escaped, or rewritten.

    The reference syntax has no escape, so a password whose own text begins with the tag is
    the one value this pair cannot round-trip: the parser reads the printed text as a
    reference again. That belongs to the syntax rather than to the printer, so the printer
    prints what it was given and the limitation stays visible here rather than being papered
    over with a rewrite that would change the credential.
    """
    shaped = f"{SECRET_TAG} hunter2"
    config = configuration(
        mqtt=MqttSettings(host="core-mosquitto", port=1883, password=SecretStr(shaped)),
    )

    document = as_options_document(config)

    assert document["mqtt"]["password"] == shaped
