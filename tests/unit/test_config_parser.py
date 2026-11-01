"""Unit tests for the Configuration_Parser (task 4.2).

The parser has two jobs that pull in opposite directions, and most of these tests exist to
pin the boundary between them: a defect in the global sections rejects the whole
Addon_Configuration and exits non-zero (Requirements 2.7, 2.10, 2.11, 2.4), while a defect
in one ``Device_Entry`` excludes only that entry and keeps every other one
(Requirements 2.3, 2.5, 2.12, 8.5, 8.6, 8.8).

Three things are checked more heavily than the rest, because they are the parts a future
change is most likely to break silently:

* **"The first failing field" is deterministic.** An entry with several defects must always
  report the same one, chosen by declaration order across the ``DeviceEntry`` envelope and
  then the plugin-published schema. A synthetic schema with a known field order is used so
  the expectation does not depend on the iDRAC schema's shape.
* **``params`` is not normalized.** The Poll_Scheduler needs ``model_fields_set`` to tell an
  omitted request timeout from one explicitly set to the same value as the schema default
  (Requirement 5.8), so the parser must carry the substituted raw mapping rather than a
  dumped model.
* **An unknown plugin is not the parser's rejection.** Requirement 3.4 puts that record in
  the Plugin_Registry, so the parser passes the entry through untouched.

Every test supplies its own :class:`CredentialRegistry` and its own logger, so the
process-wide registry and the root logger are never mutated by the suite.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import BaseModel, Field, SecretStr

from remote_energy_meter.config.models import (
    CREDENTIAL_MARKER,
    DEFAULT_POLL_INTERVAL_S,
    MAX_DEVICE_ID_LENGTH,
    MAX_DEVICES,
    LogLevel,
)
from remote_energy_meter.config.parser import (
    MAX_OPTIONS_BYTES,
    FatalReason,
    ParseOutcome,
    PluginConfigSchema,
    load_options,
    parse_options,
    read_options_text,
)
from remote_energy_meter.config.runtime import RejectionReason
from remote_energy_meter.config.secrets import SecretResolver
from remote_energy_meter.diagnostics.redaction import MAX_CREDENTIAL_LENGTH, CredentialRegistry
from remote_energy_meter.plugins.idrac.schema import IdracParams

LOGGER_NAME: Final = "tests.config_parser"

#: A password distinctive enough that its appearance anywhere is unambiguous.
IDRAC_PASSWORD: Final = "hunter2-idrac-pw"
MQTT_PASSWORD: Final = "broker-secret-pw"


class SyntheticParams(BaseModel):
    """A plugin schema with a known field order, for the ordering tests.

    ``alpha`` before ``token`` before ``omega``: a rejection report naming ``token`` when
    ``alpha`` also failed would mean the ordering rule has broken.
    """

    alpha: str = Field(min_length=1)
    token: SecretStr = Field(json_schema_extra={CREDENTIAL_MARKER: True})
    omega: int = Field(default=1, ge=0, le=10)


SCHEMAS: Final[Mapping[str, PluginConfigSchema]] = {
    "idrac": IdracParams,
    "synthetic": SyntheticParams,
}


def idrac_params(**overrides: Any) -> dict[str, Any]:
    """Valid iDRAC connection parameters, with the given fields replaced."""
    params: dict[str, Any] = {
        "host": "192.0.2.9",
        "username": "root",
        "password": IDRAC_PASSWORD,
        "verify_tls": False,
    }
    params.update(overrides)
    return params


def device(device_id: str = "poweredge_a", **overrides: Any) -> dict[str, Any]:
    """A valid device list element, with the given fields replaced."""
    entry: dict[str, Any] = {"device_id": device_id, "plugin": "idrac", "params": idrac_params()}
    entry.update(overrides)
    return entry


def options(**overrides: Any) -> dict[str, Any]:
    """A valid Addon_Configuration document, with the given top-level fields replaced."""
    document: dict[str, Any] = {
        "poll_interval_s": 30,
        "log_level": "info",
        "mqtt": {"host": "core-mosquitto", "port": 1883},
        "devices": [device()],
    }
    document.update(overrides)
    return document


@pytest.fixture
def registry() -> CredentialRegistry:
    """A registry private to one test, so the process-wide one is never touched."""
    return CredentialRegistry()


@pytest.fixture
def resolver(registry: CredentialRegistry) -> SecretResolver:
    return SecretResolver.from_mapping(
        {
            "idrac_a_password": IDRAC_PASSWORD,
            "mqtt_password": MQTT_PASSWORD,
            "blank_password": "   ",
        },
        registry=registry,
    )


@pytest.fixture
def log(caplog: pytest.LogCaptureFixture) -> logging.Logger:
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
    return logging.getLogger(LOGGER_NAME)


def parse(
    document: object,
    *,
    log: logging.Logger,
    registry: CredentialRegistry,
    resolver: SecretResolver | None = None,
    schemas: Mapping[str, PluginConfigSchema] = SCHEMAS,
) -> ParseOutcome:
    """Serialize ``document`` the way Supervisor would and parse it."""
    return parse_options(
        json.dumps(document),
        schemas=schemas,
        secrets=resolver if resolver is not None else SecretResolver.from_mapping({}, registry=registry),
        logger=log,
        registry=registry,
    )


def errors(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.levelno == logging.ERROR]


def messages(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(record.getMessage() for record in caplog.records)


# --------------------------------------------------------------------------------------------------
# the happy path (Requirement 2.1)
# --------------------------------------------------------------------------------------------------


def test_valid_configuration_is_fully_resolved(
    log: logging.Logger,
    registry: CredentialRegistry,
    resolver: SecretResolver,
    caplog: pytest.LogCaptureFixture,
) -> None:
    document = options(
        poll_interval_s=45,
        log_level="debug",
        mqtt={"host": "core-mosquitto", "port": 1883, "username": "addons", "password": "!secret mqtt_password"},
        devices=[
            device("poweredge_a", display_name="PowerEdge A"),
            device("poweredge_b", poll_interval_s=60, params=idrac_params(host="192.0.2.20")),
        ],
    )

    outcome = parse(document, log=log, registry=registry, resolver=resolver)

    assert outcome.fatal is None
    assert outcome.ok
    assert outcome.rejections == ()
    assert errors(caplog) == []

    config = outcome.config
    assert config is not None
    assert config.poll_interval_s == 45
    assert config.log_level is LogLevel.DEBUG
    assert config.mqtt.host == "core-mosquitto"
    assert config.mqtt.port == 1883
    assert config.mqtt.password is not None
    assert config.mqtt.password.get_secret_value() == MQTT_PASSWORD
    assert [entry.device_id for entry in config.devices] == ["poweredge_a", "poweredge_b"]
    assert config.devices[0].display_name == "PowerEdge A"
    assert config.devices[1].poll_interval_s == 60


def test_device_order_is_preserved(log: logging.Logger, registry: CredentialRegistry) -> None:
    ids = ["a", "b", "c", "d", "e"]
    outcome = parse(options(devices=[device(name) for name in ids]), log=log, registry=registry)

    assert outcome.config is not None
    assert [entry.device_id for entry in outcome.config.devices] == ids


def test_params_are_carried_unnormalized(log: logging.Logger, registry: CredentialRegistry) -> None:
    """Requirement 5.8: an omitted request timeout must stay distinguishable from a set one."""
    outcome = parse(
        options(devices=[device("omitted"), device("explicit", params=idrac_params(request_timeout_s=10.0))]),
        log=log,
        registry=registry,
    )

    assert outcome.config is not None
    omitted, explicit = outcome.config.devices

    assert "request_timeout_s" not in omitted.params
    assert "request_timeout_s" not in IdracParams.model_validate(omitted.params).model_fields_set
    assert "request_timeout_s" in IdracParams.model_validate(explicit.params).model_fields_set


def test_secret_references_in_credential_fields_are_substituted(
    log: logging.Logger,
    registry: CredentialRegistry,
    resolver: SecretResolver,
) -> None:
    outcome = parse(
        options(devices=[device(params=idrac_params(password="!secret idrac_a_password"))]),
        log=log,
        registry=registry,
        resolver=resolver,
    )

    assert outcome.config is not None
    assert outcome.config.devices[0].params["password"] == IDRAC_PASSWORD


def test_credential_values_are_registered_for_redaction(
    log: logging.Logger,
    registry: CredentialRegistry,
    resolver: SecretResolver,
) -> None:
    """Requirement 8.1: credential fields found through the schema marker are registered."""
    document = options(
        mqtt={"host": "core-mosquitto", "port": 1883, "password": MQTT_PASSWORD},
        devices=[device()],
    )

    assert parse(document, log=log, registry=registry, resolver=resolver).ok

    assert IDRAC_PASSWORD not in registry.redact(f"idrac said {IDRAC_PASSWORD}")
    assert MQTT_PASSWORD not in registry.redact(f"broker said {MQTT_PASSWORD}")


# --------------------------------------------------------------------------------------------------
# global poll interval (Requirements 2.6, 2.7)
# --------------------------------------------------------------------------------------------------


def test_global_interval_defaults_to_30_seconds(log: logging.Logger, registry: CredentialRegistry) -> None:
    document = options()
    del document["poll_interval_s"]

    outcome = parse(document, log=log, registry=registry)

    assert outcome.config is not None
    assert outcome.config.poll_interval_s == DEFAULT_POLL_INTERVAL_S == 30


def test_null_global_interval_is_treated_as_omitted(log: logging.Logger, registry: CredentialRegistry) -> None:
    """Supervisor writes an unset optional field as ``null``, which means "use the default"."""
    outcome = parse(options(poll_interval_s=None), log=log, registry=registry)

    assert outcome.config is not None
    assert outcome.config.poll_interval_s == DEFAULT_POLL_INTERVAL_S


@pytest.mark.parametrize("supplied", [5, 30, 3600, 30.0])
def test_global_interval_within_range_is_accepted(
    supplied: object,
    log: logging.Logger,
    registry: CredentialRegistry,
) -> None:
    outcome = parse(options(poll_interval_s=supplied), log=log, registry=registry)

    assert outcome.config is not None
    assert outcome.config.poll_interval_s == int(supplied)  # type: ignore[call-overload]


@pytest.mark.parametrize("supplied", [4, 0, -1, 3601, 30.5, "30", True, [30]])
def test_global_interval_outside_range_rejects_the_configuration(
    supplied: object,
    log: logging.Logger,
    registry: CredentialRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    outcome = parse(options(poll_interval_s=supplied), log=log, registry=registry)

    assert outcome.config is None
    assert outcome.fatal is not None
    assert outcome.fatal.reason is FatalReason.INVALID_GLOBAL_INTERVAL
    assert outcome.fatal.field_name == "poll_interval_s"
    # Requirement 2.7: the record names the supplied value and the accepted range.
    assert "5 to 3600" in messages(caplog)
    assert len(errors(caplog)) == 1


# --------------------------------------------------------------------------------------------------
# MQTT connection section (Requirement 2.11)
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("section", "expected_field"),
    [
        ({"port": 1883}, "mqtt.host"),
        ({"host": "", "port": 1883}, "mqtt.host"),
        ({"host": "core-mosquitto"}, "mqtt.port"),
        ({"host": "core-mosquitto", "port": 0}, "mqtt.port"),
        ({"host": "core-mosquitto", "port": 65536}, "mqtt.port"),
        ({"host": "core-mosquitto", "port": -1}, "mqtt.port"),
    ],
)
def test_bad_mqtt_section_rejects_the_configuration(
    section: dict[str, Any],
    expected_field: str,
    log: logging.Logger,
    registry: CredentialRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    outcome = parse(options(mqtt=section), log=log, registry=registry)

    assert outcome.config is None
    assert outcome.fatal is not None
    assert outcome.fatal.reason is FatalReason.INVALID_MQTT_SETTINGS
    assert outcome.fatal.field_name == expected_field
    assert len(errors(caplog)) == 1


def test_absent_mqtt_section_rejects_the_configuration(
    log: logging.Logger,
    registry: CredentialRegistry,
) -> None:
    document = options()
    del document["mqtt"]

    outcome = parse(document, log=log, registry=registry)

    assert outcome.fatal is not None
    assert outcome.fatal.reason is FatalReason.INVALID_MQTT_SETTINGS
    assert outcome.fatal.field_name == "mqtt"


def test_unresolvable_mqtt_credential_rejects_the_configuration(
    log: logging.Logger,
    registry: CredentialRegistry,
    resolver: SecretResolver,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """There is no per-field fallback for the broker credentials, so this is fatal.

    Dropping the credential or authenticating with the literal reference text would both
    surface much later as an unexplained broker rejection.
    """
    document = options(mqtt={"host": "core-mosquitto", "port": 1883, "password": "!secret absent_name"})

    outcome = parse(document, log=log, registry=registry, resolver=resolver)

    assert outcome.fatal is not None
    assert outcome.fatal.reason is FatalReason.INVALID_MQTT_SETTINGS
    assert outcome.fatal.field_name == "mqtt.password"
    assert "absent_name" in messages(caplog)
    assert MQTT_PASSWORD not in messages(caplog)


# --------------------------------------------------------------------------------------------------
# log level (Requirements 9.6, 9.7)
# --------------------------------------------------------------------------------------------------


def test_omitted_log_level_resolves_to_info(log: logging.Logger, registry: CredentialRegistry) -> None:
    document = options()
    del document["log_level"]

    outcome = parse(document, log=log, registry=registry)

    assert outcome.config is not None
    assert outcome.config.log_level is LogLevel.INFO
    assert outcome.level_resolution.rejected_value is None
    assert not outcome.level_resolution.fell_back


def test_out_of_set_log_level_falls_back_without_a_parser_record(
    log: logging.Logger,
    registry: CredentialRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The Requirement 9.7 warning is written by ``configure_logging``, not here.

    The parser only resolves the value and reports what it rejected, so exactly one warning
    reaches the log stream rather than one per component that notices.
    """
    outcome = parse(options(log_level="verbose"), log=log, registry=registry)

    assert outcome.config is not None
    assert outcome.config.log_level is LogLevel.INFO
    assert outcome.level_resolution.rejected_value == "verbose"
    assert caplog.records == []


def test_log_level_is_resolved_even_when_the_configuration_is_fatal(
    log: logging.Logger,
    registry: CredentialRegistry,
) -> None:
    outcome = parse(options(log_level="debug", poll_interval_s=1), log=log, registry=registry)

    assert outcome.fatal is not None
    assert outcome.level_resolution.level is LogLevel.DEBUG


# --------------------------------------------------------------------------------------------------
# device identifiers (Requirements 2.12, 2.5)
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "identifier",
    [
        "",
        "a" * (MAX_DEVICE_ID_LENGTH + 1),
        "has space",
        "has.dot",
        "has/slash",
        "unicodé",
        None,
        42,
    ],
)
def test_malformed_identifier_excludes_only_that_entry(
    identifier: object,
    log: logging.Logger,
    registry: CredentialRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    document = options(devices=[device(), {"device_id": identifier, "plugin": "idrac", "params": idrac_params()}])

    outcome = parse(document, log=log, registry=registry)

    assert outcome.config is not None
    assert [entry.device_id for entry in outcome.config.devices] == ["poweredge_a"]
    assert [rejection.reason for rejection in outcome.rejections] == [RejectionReason.INVALID_IDENTIFIER]
    assert len(errors(caplog)) == 1
    # Requirement 2.12: the record names the accepted identifier form.
    assert "letters, digits, hyphen, and underscore" in messages(caplog)


def test_identifier_at_the_length_boundary_is_accepted(log: logging.Logger, registry: CredentialRegistry) -> None:
    longest = "d" * MAX_DEVICE_ID_LENGTH

    outcome = parse(options(devices=[device(longest)]), log=log, registry=registry)

    assert outcome.config is not None
    assert [entry.device_id for entry in outcome.config.devices] == [longest]


def test_duplicate_identifiers_keep_the_first_occurrence(
    log: logging.Logger,
    registry: CredentialRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    document = options(
        devices=[
            device("PowerEdge_A", display_name="first"),
            device("poweredge_a", display_name="second"),
            device("POWEREDGE_A", display_name="third"),
            device("poweredge_b", display_name="distinct"),
        ]
    )

    outcome = parse(document, log=log, registry=registry)

    assert outcome.config is not None
    assert [entry.display_name for entry in outcome.config.devices] == ["first", "distinct"]
    assert [rejection.reason for rejection in outcome.rejections] == [
        RejectionReason.DUPLICATE_IDENTIFIER,
        RejectionReason.DUPLICATE_IDENTIFIER,
    ]
    assert len(errors(caplog)) == 2


def test_an_identifier_is_claimed_even_when_its_entry_is_rejected(
    log: logging.Logger,
    registry: CredentialRegistry,
) -> None:
    """Requirement 2.5 compares *declared* identifiers, not accepted ones.

    Letting a later entry inherit an identifier whose first holder was rejected would make
    which entry gets metered depend on an unrelated defect elsewhere in the file.
    """
    document = options(
        devices=[
            device("shared", params=idrac_params(host="")),  # rejected: schema violation
            device("SHARED"),  # still a duplicate of a declared identifier
            device("kept"),
        ]
    )

    outcome = parse(document, log=log, registry=registry)

    assert outcome.config is not None
    assert [entry.device_id for entry in outcome.config.devices] == ["kept"]
    assert [rejection.reason for rejection in outcome.rejections] == [
        RejectionReason.SCHEMA_VIOLATION,
        RejectionReason.DUPLICATE_IDENTIFIER,
    ]


def test_non_mapping_entry_is_excluded_without_an_identifier(
    log: logging.Logger,
    registry: CredentialRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    outcome = parse(options(devices=[device(), "poweredge_b", 7]), log=log, registry=registry)

    assert outcome.config is not None
    assert len(outcome.config.devices) == 1
    assert [(rejection.device_id, rejection.reason) for rejection in outcome.rejections] == [
        (None, RejectionReason.SCHEMA_VIOLATION),
        (None, RejectionReason.SCHEMA_VIOLATION),
    ]
    assert len(errors(caplog)) == 2


# --------------------------------------------------------------------------------------------------
# plugin schema violations (Requirements 2.2, 2.3)
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("params", "expected_field"),
    [
        ({"username": "root", "password": IDRAC_PASSWORD}, "params.host"),
        (idrac_params(host=""), "params.host"),
        (idrac_params(host=1234), "params.host"),
        (idrac_params(verify_tls="maybe"), "params.verify_tls"),
        (idrac_params(request_timeout_s=0.5), "params.request_timeout_s"),
        (idrac_params(request_timeout_s=61), "params.request_timeout_s"),
        (idrac_params(power_path="p" * 513), "params.power_path"),
    ],
)
def test_schema_violation_names_the_failing_field(
    params: dict[str, Any],
    expected_field: str,
    log: logging.Logger,
    registry: CredentialRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    outcome = parse(options(devices=[device(params=params)]), log=log, registry=registry)

    assert outcome.fatal is not None  # the only entry was excluded, so nothing is left to meter
    assert outcome.fatal.reason is FatalReason.NO_ACCEPTED_DEVICES
    assert [(rejection.reason, rejection.field_name) for rejection in outcome.rejections] == [
        (RejectionReason.SCHEMA_VIOLATION, expected_field)
    ]
    assert expected_field in messages(caplog)


def test_out_of_range_device_interval_excludes_the_entry(
    log: logging.Logger,
    registry: CredentialRegistry,
) -> None:
    """Requirement 2.2 lists a device interval outside 5-3600 s as a validation failure.

    The Supervisor options schema leaves the bound unconstrained on purpose, because a
    Supervisor schema failure refuses the whole options blob, so the parser is the only
    place it is enforced.
    """
    document = options(devices=[device("fast", poll_interval_s=4), device("ok", poll_interval_s=3600)])

    outcome = parse(document, log=log, registry=registry)

    assert outcome.config is not None
    assert [entry.device_id for entry in outcome.config.devices] == ["ok"]
    assert [(rejection.device_id, rejection.field_name) for rejection in outcome.rejections] == [
        ("fast", "poll_interval_s")
    ]


@pytest.mark.parametrize(
    ("overrides", "expected_field"),
    [
        ({"plugin": None}, "plugin"),
        ({"plugin": ""}, "plugin"),
        ({"plugin": "p" * 65}, "plugin"),
        ({"params": "not-a-mapping"}, "params"),
        ({"display_name": 5}, "display_name"),
    ],
)
def test_malformed_envelope_field_excludes_the_entry(
    overrides: dict[str, Any],
    expected_field: str,
    log: logging.Logger,
    registry: CredentialRegistry,
) -> None:
    outcome = parse(options(devices=[device(), device("second", **overrides)]), log=log, registry=registry)

    assert outcome.config is not None
    assert [entry.device_id for entry in outcome.config.devices] == ["poweredge_a"]
    assert [(rejection.reason, rejection.field_name) for rejection in outcome.rejections] == [
        (RejectionReason.SCHEMA_VIOLATION, expected_field)
    ]


def test_absent_params_reports_the_first_missing_plugin_field(
    log: logging.Logger,
    registry: CredentialRegistry,
) -> None:
    entry = device()
    del entry["params"]

    outcome = parse(options(devices=[entry]), log=log, registry=registry)

    assert [(rejection.reason, rejection.field_name) for rejection in outcome.rejections] == [
        (RejectionReason.SCHEMA_VIOLATION, "params.host")
    ]


# --------------------------------------------------------------------------------------------------
# credential fields (Requirements 8.5, 8.6, 8.8)
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("supplied", ["", "   ", "\t\n"])
def test_empty_credential_excludes_the_entry(
    supplied: str,
    log: logging.Logger,
    registry: CredentialRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    outcome = parse(
        options(devices=[device(), device("second", params=idrac_params(password=supplied))]),
        log=log,
        registry=registry,
    )

    assert outcome.config is not None
    assert len(outcome.config.devices) == 1
    assert [(rejection.reason, rejection.field_name) for rejection in outcome.rejections] == [
        (RejectionReason.EMPTY_CREDENTIAL, "params.password")
    ]
    assert "second" in messages(caplog)


def test_absent_required_credential_is_reported_as_empty(
    log: logging.Logger,
    registry: CredentialRegistry,
) -> None:
    """Requirement 8.5 is the more specific rule, so it wins over "missing required field"."""
    params = idrac_params()
    del params["password"]

    outcome = parse(options(devices=[device(params=params)]), log=log, registry=registry)

    assert [(rejection.reason, rejection.field_name) for rejection in outcome.rejections] == [
        (RejectionReason.EMPTY_CREDENTIAL, "params.password")
    ]


def test_secret_resolving_to_whitespace_is_reported_as_empty(
    log: logging.Logger,
    registry: CredentialRegistry,
    resolver: SecretResolver,
) -> None:
    """Requirement 8.4 resolves before validating, which is what makes this reachable."""
    outcome = parse(
        options(devices=[device(params=idrac_params(password="!secret blank_password"))]),
        log=log,
        registry=registry,
        resolver=resolver,
    )

    assert [(rejection.reason, rejection.field_name) for rejection in outcome.rejections] == [
        (RejectionReason.EMPTY_CREDENTIAL, "params.password")
    ]


def test_oversized_credential_excludes_the_entry(
    log: logging.Logger,
    registry: CredentialRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Requirement 8.8's 256-character limit is reported ahead of the plugin's own bound.

    ``IdracParams.password`` also caps at 128 characters, so both rules fire; the record
    cites the one that applies to every plugin.
    """
    document = options(
        devices=[
            device(params=idrac_params(password="x" * (MAX_CREDENTIAL_LENGTH + 1))),
            device("synth", plugin="synthetic", params={"alpha": "a", "token": "t" * 300}),
        ]
    )

    outcome = parse(document, log=log, registry=registry)

    assert [(rejection.reason, rejection.field_name) for rejection in outcome.rejections] == [
        (RejectionReason.OVERSIZED_CREDENTIAL, "params.password"),
        (RejectionReason.OVERSIZED_CREDENTIAL, "params.token"),
    ]
    assert "256" in messages(caplog)


def test_credential_at_the_length_boundary_is_accepted(log: logging.Logger, registry: CredentialRegistry) -> None:
    outcome = parse(
        options(devices=[device("synth", plugin="synthetic", params={"alpha": "a", "token": "t" * 256})]),
        log=log,
        registry=registry,
    )

    assert outcome.config is not None
    assert len(outcome.config.devices) == 1


def test_unresolvable_secret_excludes_the_entry_without_naming_a_value(
    log: logging.Logger,
    registry: CredentialRegistry,
    resolver: SecretResolver,
    caplog: pytest.LogCaptureFixture,
) -> None:
    document = options(
        devices=[device(), device("second", params=idrac_params(password="!secret absent_name"))],
    )

    outcome = parse(document, log=log, registry=registry, resolver=resolver)

    assert outcome.config is not None
    assert len(outcome.config.devices) == 1
    assert [(rejection.reason, rejection.field_name) for rejection in outcome.rejections] == [
        (RejectionReason.UNRESOLVED_SECRET, "params.password")
    ]
    # Requirement 8.6: the record names the reference, never a value.
    assert "absent_name" in messages(caplog)
    assert IDRAC_PASSWORD not in messages(caplog)


def test_bare_secret_tag_is_reported_as_unresolved(
    log: logging.Logger,
    registry: CredentialRegistry,
    resolver: SecretResolver,
) -> None:
    outcome = parse(
        options(devices=[device(params=idrac_params(password="!secret"))]),
        log=log,
        registry=registry,
        resolver=resolver,
    )

    assert [rejection.reason for rejection in outcome.rejections] == [RejectionReason.UNRESOLVED_SECRET]


def test_secret_references_in_non_credential_fields_stay_literal(
    log: logging.Logger,
    registry: CredentialRegistry,
    resolver: SecretResolver,
) -> None:
    """Requirement 8.4 scopes resolution to credential fields."""
    outcome = parse(
        options(devices=[device(params=idrac_params(host="!secret idrac_a_password"))]),
        log=log,
        registry=registry,
        resolver=resolver,
    )

    assert outcome.config is not None
    assert outcome.config.devices[0].params["host"] == "!secret idrac_a_password"


# --------------------------------------------------------------------------------------------------
# "the first failing field" is deterministic (Requirement 2.3)
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("params", "expected_field", "expected_reason"),
    [
        # alpha (index 0) fails alongside an empty token (index 1): alpha wins.
        ({"alpha": "", "token": "  "}, "params.alpha", RejectionReason.SCHEMA_VIOLATION),
        # token (index 1) fails alongside an out-of-range omega (index 2): token wins.
        ({"alpha": "a", "token": "  ", "omega": 99}, "params.token", RejectionReason.EMPTY_CREDENTIAL),
        # only omega (index 2) fails.
        ({"alpha": "a", "token": "t", "omega": 99}, "params.omega", RejectionReason.SCHEMA_VIOLATION),
        # an unresolved reference and an out-of-range omega: the reference field is earlier.
        (
            {"alpha": "a", "token": "!secret absent_name", "omega": 99},
            "params.token",
            RejectionReason.UNRESOLVED_SECRET,
        ),
    ],
)
def test_the_earliest_declared_failing_field_is_reported(
    params: dict[str, Any],
    expected_field: str,
    expected_reason: RejectionReason,
    log: logging.Logger,
    registry: CredentialRegistry,
    resolver: SecretResolver,
) -> None:
    outcome = parse(
        options(devices=[device("synth", plugin="synthetic", params=params)]),
        log=log,
        registry=registry,
        resolver=resolver,
    )

    assert [(rejection.reason, rejection.field_name) for rejection in outcome.rejections] == [
        (expected_reason, expected_field)
    ]


def test_envelope_defects_outrank_params_defects(log: logging.Logger, registry: CredentialRegistry) -> None:
    """``poll_interval_s`` is declared before ``params``, so it is the earlier field."""
    outcome = parse(
        options(devices=[device("synth", plugin="synthetic", poll_interval_s=4, params={"alpha": "", "token": "t"})]),
        log=log,
        registry=registry,
    )

    assert [rejection.field_name for rejection in outcome.rejections] == ["poll_interval_s"]


def test_one_entry_produces_exactly_one_rejection_and_one_record(
    log: logging.Logger,
    registry: CredentialRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    everything_wrong = {"alpha": "", "token": "", "omega": 99}
    document = options(devices=[device(), device("synth", plugin="synthetic", params=everything_wrong)])

    outcome = parse(document, log=log, registry=registry)

    assert len(outcome.rejections) == 1
    assert len(errors(caplog)) == 1


def test_rejection_reporting_is_reproducible(log: logging.Logger, registry: CredentialRegistry) -> None:
    document = options(devices=[device("synth", plugin="synthetic", params={"alpha": "", "token": "", "omega": 99})])

    first = parse(document, log=log, registry=registry).rejections
    second = parse(document, log=log, registry=registry).rejections

    assert first == second


# --------------------------------------------------------------------------------------------------
# an unknown plugin is the registry's rejection, not the parser's (Requirement 3.4)
# --------------------------------------------------------------------------------------------------


def test_unknown_plugin_entry_passes_through_untouched(
    log: logging.Logger,
    registry: CredentialRegistry,
    resolver: SecretResolver,
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw_params = {"anything": "at all", "password": "!secret idrac_a_password"}
    document = options(devices=[device("mystery", plugin="not_installed", params=raw_params)])

    outcome = parse(document, log=log, registry=registry, resolver=resolver)

    assert outcome.config is not None
    assert [entry.plugin for entry in outcome.config.devices] == ["not_installed"]
    # Untouched: without a schema there is no way to know which fields are credentials.
    assert outcome.config.devices[0].params == raw_params
    assert outcome.rejections == ()
    assert errors(caplog) == []


def test_unknown_plugin_entry_still_obeys_the_identifier_rules(
    log: logging.Logger,
    registry: CredentialRegistry,
) -> None:
    document = options(devices=[device("mystery", plugin="not_installed"), device("MYSTERY", plugin="not_installed")])

    outcome = parse(document, log=log, registry=registry)

    assert outcome.config is not None
    assert len(outcome.config.devices) == 1
    assert [rejection.reason for rejection in outcome.rejections] == [RejectionReason.DUPLICATE_IDENTIFIER]


# --------------------------------------------------------------------------------------------------
# the device list as a whole (Requirements 2.1, 2.4)
# --------------------------------------------------------------------------------------------------


def test_zero_accepted_entries_rejects_the_configuration(
    log: logging.Logger,
    registry: CredentialRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    document = options(devices=[device("bad one"), device("also bad")])

    outcome = parse(document, log=log, registry=registry)

    assert outcome.config is None
    assert outcome.fatal is not None
    assert outcome.fatal.reason is FatalReason.NO_ACCEPTED_DEVICES
    # The per-entry reasons survive onto the fatal path, so the caller can report them.
    assert len(outcome.rejections) == 2
    assert len(errors(caplog)) == 3  # two entries, then the fatal record


@pytest.mark.parametrize("devices", [[], None, {}, "poweredge_a", 5])
def test_missing_or_malformed_device_list_rejects_the_configuration(
    devices: object,
    log: logging.Logger,
    registry: CredentialRegistry,
) -> None:
    outcome = parse(options(devices=devices), log=log, registry=registry)

    assert outcome.config is None
    assert outcome.fatal is not None
    assert outcome.fatal.reason is FatalReason.NO_ACCEPTED_DEVICES


def test_absent_device_list_rejects_the_configuration(log: logging.Logger, registry: CredentialRegistry) -> None:
    document = options()
    del document["devices"]

    outcome = parse(document, log=log, registry=registry)

    assert outcome.fatal is not None
    assert outcome.fatal.reason is FatalReason.NO_ACCEPTED_DEVICES


def test_surplus_devices_are_excluded_one_by_one(
    log: logging.Logger,
    registry: CredentialRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Requirement 2.1 caps the list at 50; the surplus is excluded per entry.

    Rejecting the whole configuration instead would meter nothing rather than fifty.
    """
    document = options(devices=[device(f"dev_{index:03d}") for index in range(MAX_DEVICES + 3)])

    outcome = parse(document, log=log, registry=registry)

    assert outcome.config is not None
    assert len(outcome.config.devices) == MAX_DEVICES
    assert [entry.device_id for entry in outcome.config.devices] == [f"dev_{index:03d}" for index in range(MAX_DEVICES)]
    assert len(outcome.rejections) == 3
    assert len(errors(caplog)) == 3


# --------------------------------------------------------------------------------------------------
# reading versus parsing (Requirements 1.7, 2.10)
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("raw_text", ["", "{", "not json", '{"poll_interval_s": 30,}'])
def test_unparseable_text_is_reported_as_a_parse_failure(
    raw_text: str,
    log: logging.Logger,
    registry: CredentialRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    outcome = parse_options(raw_text, schemas=SCHEMAS, logger=log, registry=registry)

    assert outcome.config is None
    assert outcome.fatal is not None
    assert outcome.fatal.reason is FatalReason.OPTIONS_UNPARSEABLE
    assert len(errors(caplog)) == 1


@pytest.mark.parametrize("raw_text", ["[]", '"text"', "42", "null"])
def test_text_that_is_not_an_options_object_is_reported_as_a_parse_failure(
    raw_text: str,
    log: logging.Logger,
    registry: CredentialRegistry,
) -> None:
    outcome = parse_options(raw_text, schemas=SCHEMAS, logger=log, registry=registry)

    assert outcome.fatal is not None
    assert outcome.fatal.reason is FatalReason.OPTIONS_UNPARSEABLE


def test_absent_options_file_is_reported_as_a_read_failure(
    tmp_path: Path,
    log: logging.Logger,
    registry: CredentialRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    outcome = load_options(tmp_path / "options.json", schemas=SCHEMAS, logger=log, registry=registry)

    assert outcome.config is None
    assert outcome.fatal is not None
    assert outcome.fatal.reason is FatalReason.OPTIONS_UNREADABLE
    assert len(errors(caplog)) == 1


def test_unreadable_and_unparseable_are_distinct_reasons(
    tmp_path: Path,
    log: logging.Logger,
    registry: CredentialRegistry,
) -> None:
    """Requirement 2.10: the record has to say whether the read or the parse failed."""
    unparseable = tmp_path / "options.json"
    unparseable.write_text("{ not json", encoding="utf-8")

    read_failure = load_options(tmp_path / "missing.json", schemas=SCHEMAS, logger=log, registry=registry)
    parse_failure = load_options(unparseable, schemas=SCHEMAS, logger=log, registry=registry)

    assert read_failure.fatal is not None
    assert parse_failure.fatal is not None
    assert read_failure.fatal.reason is not parse_failure.fatal.reason


def test_options_file_that_is_not_utf8_is_a_read_failure(tmp_path: Path) -> None:
    path = tmp_path / "options.json"
    path.write_bytes(b'{"poll_interval_s": \xff\xfe}')

    outcome = read_options_text(path)

    assert not isinstance(outcome, str)
    assert outcome.reason is FatalReason.OPTIONS_UNREADABLE
    assert "UTF-8" in outcome.detail


def test_oversized_options_file_is_a_read_failure(tmp_path: Path) -> None:
    path = tmp_path / "options.json"
    path.write_text("x" * (MAX_OPTIONS_BYTES + 1), encoding="utf-8")

    outcome = read_options_text(path)

    assert not isinstance(outcome, str)
    assert outcome.reason is FatalReason.OPTIONS_UNREADABLE


def test_a_directory_in_place_of_the_options_file_is_a_read_failure(tmp_path: Path) -> None:
    outcome = read_options_text(tmp_path)

    assert not isinstance(outcome, str)
    assert outcome.reason is FatalReason.OPTIONS_UNREADABLE


def test_load_options_reads_and_parses_a_real_file(
    tmp_path: Path,
    log: logging.Logger,
    registry: CredentialRegistry,
) -> None:
    path = tmp_path / "options.json"
    path.write_text(json.dumps(options()), encoding="utf-8")

    outcome = load_options(path, schemas=SCHEMAS, logger=log, registry=registry)

    assert outcome.ok
    assert outcome.config is not None
    assert outcome.config.poll_interval_s == 30
