"""Configuration data models: the validated in-memory shape of ``/data/options.json``.

These models are the single in-process declaration of the Addon_Configuration schema
(``remote_energy_meter/config.yaml`` is the Supervisor-facing declaration of the same
shape). They carry the field bounds that Requirements 2.1, 2.11, 2.12, and 5.2 fix:

* device identifier: 1-64 characters drawn from letters, digits, hyphen, underscore
* MQTT broker port: 1-65535
* device list: 1-50 entries, order preserved
* poll intervals (global and per-device): 5-3600 whole seconds
* log level: one of ``debug``, ``info``, ``warning``, ``error``, defaulting to ``info``

Credential fields are :class:`pydantic.SecretStr` carrying the ``credential`` schema
marker (Requirement 8.1). ``SecretStr`` keeps plaintext out of ``str`` and ``repr``
output, which is the first of the three redaction layers (Requirement 8.2); the marker
is what lets the redaction registry and the parser find credential fields without
knowing any particular plugin's parameter names.

Validation *policy* -- which failures are fatal to the whole configuration and which
merely exclude one Device_Entry -- lives in ``config/parser.py``, not here. These models
only declare the accepted shape.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Final

from pydantic import BaseModel, Field, SecretStr

__all__ = [
    "CREDENTIAL_MARKER",
    "ConfigurationObject",
    "DeviceEntry",
    "LogLevel",
    "MqttSettings",
    "credential_field_names",
]

# --------------------------------------------------------------------------------------
# Bounds, shared with the parser and the Supervisor options schema
# --------------------------------------------------------------------------------------

#: Requirements 2.2, 2.7, 5.2: accepted poll interval range, in whole seconds.
MIN_POLL_INTERVAL_S: Final = 5
MAX_POLL_INTERVAL_S: Final = 3600

#: Requirement 2.6: the global poll interval when the Addon_Configuration omits it.
DEFAULT_POLL_INTERVAL_S: Final = 30

#: Requirement 2.11: accepted MQTT broker port range.
MIN_PORT: Final = 1
MAX_PORT: Final = 65535

#: Requirement 2.12: accepted device identifier length and character set.
MAX_DEVICE_ID_LENGTH: Final = 64
DEVICE_ID_PATTERN: Final = r"^[A-Za-z0-9_-]+$"

#: Requirement 3.1: accepted plugin identifier length.
MAX_PLUGIN_ID_LENGTH: Final = 64

#: Requirement 2.1: accepted device list length.
MIN_DEVICES: Final = 1
MAX_DEVICES: Final = 50

# --------------------------------------------------------------------------------------
# Credential field marking
# --------------------------------------------------------------------------------------

#: JSON-schema extra key that marks a model field as holding a credential value.
#:
#: A field is declared as a credential by combining the ``SecretStr`` type with this
#: marker, both in these models and in every plugin-published configuration schema::
#:
#:     password: SecretStr = Field(json_schema_extra={CREDENTIAL_MARKER: True})
#:
#: Requirement 8.1 derives the credential value set from these markers, so the
#: redaction registry and the parser's empty/oversized credential checks work for a
#: plugin the core has never seen.
CREDENTIAL_MARKER: Final = "credential"

_CREDENTIAL_FIELD: Final[dict[str, Any]] = {CREDENTIAL_MARKER: True}


def credential_field_names(model_cls: type[BaseModel]) -> tuple[str, ...]:
    """Return the names of the fields of ``model_cls`` marked as credential fields.

    Names come back in field declaration order. Nested models are not walked: callers
    that need the credential fields of a nested model ask for them explicitly, so the
    traversal policy stays with the caller rather than being implied here.
    """
    names: list[str] = []
    for name, field in model_cls.model_fields.items():
        extra = field.json_schema_extra
        if isinstance(extra, dict) and extra.get(CREDENTIAL_MARKER) is True:
            names.append(name)
    return tuple(names)


# --------------------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------------------


class LogLevel(StrEnum):
    """The severity levels the Diagnostics_Logger accepts (Requirements 9.1, 9.2).

    Declaration order is the severity ranking ``debug < info < warning < error`` that
    Requirement 9.2 fixes; the logger derives its filter from that order rather than
    restating it.
    """

    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class MqttSettings(BaseModel):
    """The MQTT connection section of the Addon_Configuration (Requirement 2.11).

    ``host`` and ``port`` are required and the port is range-bounded, because omitting
    either or supplying a port outside 1-65535 rejects the whole configuration.
    """

    host: str = Field(min_length=1)
    port: int = Field(ge=MIN_PORT, le=MAX_PORT)
    username: SecretStr | None = Field(default=None, json_schema_extra=_CREDENTIAL_FIELD)
    password: SecretStr | None = Field(default=None, json_schema_extra=_CREDENTIAL_FIELD)
    client_id: str = "remote_energy_meter"
    base_topic: str = "remote_energy_meter"
    discovery_prefix: str = "homeassistant"
    tls: bool = False


class DeviceEntry(BaseModel):
    """One element of the device list: a device, its plugin, and its parameters.

    ``params`` stays an unvalidated mapping at this level on purpose. Requirement 2.2
    validates it against the configuration schema published by the plugin named in
    ``plugin``, which is knowable only once the Plugin_Registry has resolved the entry.
    """

    device_id: str = Field(min_length=1, max_length=MAX_DEVICE_ID_LENGTH, pattern=DEVICE_ID_PATTERN)
    plugin: str = Field(min_length=1, max_length=MAX_PLUGIN_ID_LENGTH)
    display_name: str | None = None
    poll_interval_s: int | None = Field(default=None, ge=MIN_POLL_INTERVAL_S, le=MAX_POLL_INTERVAL_S)
    params: dict[str, Any]


class ConfigurationObject(BaseModel):
    """The fully resolved Addon_Configuration (Requirement 2.1).

    Defaults are applied here rather than at each point of use, so a
    ``ConfigurationObject`` is complete and the Configuration_Printer can round-trip it
    field for field (Requirements 2.6, 2.8, 2.9, 9.6). ``devices`` keeps the order of
    the Addon_Configuration device list, which is what makes "the first occurrence wins"
    in Requirement 2.5 well defined.
    """

    poll_interval_s: int = Field(default=DEFAULT_POLL_INTERVAL_S, ge=MIN_POLL_INTERVAL_S, le=MAX_POLL_INTERVAL_S)
    log_level: LogLevel = LogLevel.INFO
    mqtt: MqttSettings
    devices: list[DeviceEntry] = Field(min_length=MIN_DEVICES, max_length=MAX_DEVICES)
