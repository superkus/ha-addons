"""Configuration schema for the generic IPMI sensor plugin.

The schema is intentionally device-agnostic: one instance identifies an IPMI host and
one SDR sensor whose reading is expressed in watts.  Credentials use the same
``SecretStr`` plus schema marker convention as every other plugin, so configuration
validation and log redaction work without IPMI-specific core code.
"""

from __future__ import annotations

from typing import Any, Final

from pydantic import BaseModel, Field, SecretStr

from remote_energy_meter.config.models import CREDENTIAL_MARKER

__all__ = [
    "DEFAULT_CIPHER_SUITE",
    "DEFAULT_INTERFACE",
    "DEFAULT_REQUEST_TIMEOUT_S",
    "DEFAULT_SENSOR_NAME",
    "MAX_CIPHER_SUITE",
    "MAX_PASSWORD_LENGTH",
    "MAX_REQUEST_TIMEOUT_S",
    "MAX_USERNAME_LENGTH",
    "MIN_CIPHER_SUITE",
    "MIN_REQUEST_TIMEOUT_S",
    "IpmiParams",
]

DEFAULT_SENSOR_NAME: Final = "System Level"
DEFAULT_INTERFACE: Final = "lanplus"
DEFAULT_CIPHER_SUITE: Final = 3
DEFAULT_REQUEST_TIMEOUT_S: Final = 10.0

MIN_CIPHER_SUITE: Final = 0
MAX_CIPHER_SUITE: Final = 17
MIN_REQUEST_TIMEOUT_S: Final = 1.0
MAX_REQUEST_TIMEOUT_S: Final = 60.0
MAX_USERNAME_LENGTH: Final = 64
MAX_PASSWORD_LENGTH: Final = 128
MAX_SENSOR_NAME_LENGTH: Final = 255
MAX_INTERFACE_LENGTH: Final = 32

_CREDENTIAL_FIELD: Final[dict[str, Any]] = {CREDENTIAL_MARKER: True}


class IpmiParams(BaseModel):
    """Connection parameters for one IPMI sensor-backed power meter."""

    host: str = Field(min_length=1)
    username: SecretStr = Field(
        min_length=1,
        max_length=MAX_USERNAME_LENGTH,
        json_schema_extra=_CREDENTIAL_FIELD,
    )
    password: SecretStr = Field(
        min_length=1,
        max_length=MAX_PASSWORD_LENGTH,
        json_schema_extra=_CREDENTIAL_FIELD,
    )
    sensor_name: str = Field(default=DEFAULT_SENSOR_NAME, min_length=1, max_length=MAX_SENSOR_NAME_LENGTH)
    interface: str = Field(
        default=DEFAULT_INTERFACE,
        min_length=1,
        max_length=MAX_INTERFACE_LENGTH,
        pattern=r"^[A-Za-z0-9_.+-]+$",
    )
    cipher_suite: int = Field(default=DEFAULT_CIPHER_SUITE, ge=MIN_CIPHER_SUITE, le=MAX_CIPHER_SUITE)
    request_timeout_s: float = Field(
        default=DEFAULT_REQUEST_TIMEOUT_S,
        ge=MIN_REQUEST_TIMEOUT_S,
        le=MAX_REQUEST_TIMEOUT_S,
    )
