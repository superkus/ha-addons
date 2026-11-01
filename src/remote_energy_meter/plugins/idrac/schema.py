"""The iDRAC connection parameter schema (Requirement 4.1).

This module is the plugin's *published* configuration schema: the model the
Configuration_Parser validates a ``Device_Entry.params`` mapping against
(Requirement 2.2), and the value the plugin class exposes as its ``config_model``
ClassVar (Requirement 3.1). It is deliberately free of HTTP, TLS, and Redfish
mechanics, so the accepted parameter space can be exercised without a network.

Two conventions from the core are honoured here rather than reinvented:

* **Credential fields are ``SecretStr`` carrying the ``credential`` marker** from
  :mod:`remote_energy_meter.config.models`. Requirement 8.1 derives the credential value
  set from that marker, so the redaction registry and the parser's empty and oversized
  credential checks work against a plugin the core has never seen. Using any other
  marker would leave iDRAC passwords unredacted in the log stream.
* **The plugin does not decide its own scheduling.** ``request_timeout_s`` bounds one
  request (Requirements 4.7, 4.13); retry counts, retry delays, and poll intervals stay
  with the Poll_Scheduler (Requirement 5.5).

TLS verification defaults to ``True`` even though the target iDRAC hosts present
self-signed certificates. That is a decision, not an oversight: the insecure setting is
one an administrator makes explicitly per device, not one they inherit from a default.
``DOCS.md`` documents ``verify_tls: false`` for those hosts.
"""

from __future__ import annotations

from typing import Any, Final

from pydantic import BaseModel, Field, SecretStr

from remote_energy_meter.config.models import CREDENTIAL_MARKER

__all__ = [
    "DEFAULT_POWER_PATH",
    "DEFAULT_REQUEST_TIMEOUT_S",
    "MAX_PASSWORD_LENGTH",
    "MAX_POWER_PATH_LENGTH",
    "MAX_REQUEST_TIMEOUT_S",
    "MAX_USERNAME_LENGTH",
    "MIN_PASSWORD_LENGTH",
    "MIN_REQUEST_TIMEOUT_S",
    "MIN_USERNAME_LENGTH",
    "IdracParams",
]

# --------------------------------------------------------------------------------------
# Bounds and defaults
# --------------------------------------------------------------------------------------

#: Requirement 4.10: the Redfish chassis power resource used when ``power_path`` is
#: omitted. ``System.Embedded.1`` is the Dell chassis identifier, and the resource is
#: reachable at this path on iDRAC 7 and later. Named here rather than in the plugin
#: module so the read path and this schema cannot drift apart.
DEFAULT_POWER_PATH: Final = "/redfish/v1/Chassis/System.Embedded.1/Power"

#: Requirement 4.1: accepted iDRAC username length.
MIN_USERNAME_LENGTH: Final = 1
MAX_USERNAME_LENGTH: Final = 64

#: Requirement 4.1: accepted iDRAC password length. Note that Requirement 8.8 rejects a
#: credential value over 256 characters at the parser level for *any* plugin; this
#: tighter bound is what iDRAC itself accepts, so an over-long password is reported as a
#: schema violation naming the field rather than reaching the device.
MIN_PASSWORD_LENGTH: Final = 1
MAX_PASSWORD_LENGTH: Final = 128

#: Requirement 4.1: accepted Redfish power resource path length.
MAX_POWER_PATH_LENGTH: Final = 512

#: Requirement 4.1: accepted per-request timeout range and default, in seconds.
MIN_REQUEST_TIMEOUT_S: Final = 1.0
MAX_REQUEST_TIMEOUT_S: Final = 60.0
DEFAULT_REQUEST_TIMEOUT_S: Final = 10.0

_CREDENTIAL_FIELD: Final[dict[str, Any]] = {CREDENTIAL_MARKER: True}


# --------------------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------------------


class IdracParams(BaseModel):
    """Connection parameters for one Dell PowerEdge server reached through iDRAC.

    One instance describes one ``Device_Entry``. Requirement 4.2 has both target servers
    metered by the same plugin implementation with entries that differ only in these
    values, so nothing here is shared or global.

    Extra keys are ignored rather than rejected, matching pydantic's default: the
    parser's "first failing field" report (Requirement 2.3) orders errors by field
    declaration index, and an unknown key has no declaration index to order by.
    """

    #: Requirement 4.1: the iDRAC management interface address (the management address,
    #: not the host's own address). Kept a plain string so a hostname, an IPv4 literal,
    #: or an IPv6 literal are all accepted; reachability is the read operation's problem
    #: and is reported as a communication failure (Requirement 4.12).
    host: str = Field(min_length=1)

    #: Requirement 4.1 with Requirement 8.1: a credential field, so the redaction
    #: registry shields it from every log record and MQTT payload.
    username: SecretStr = Field(
        min_length=MIN_USERNAME_LENGTH,
        max_length=MAX_USERNAME_LENGTH,
        json_schema_extra=_CREDENTIAL_FIELD,
    )

    #: Requirement 4.1 with Requirement 8.1: a credential field. Reachable in plaintext
    #: only through an explicit ``get_secret_value()`` call, which appears once, where
    #: the HTTP Basic authentication header is built (Requirement 4.3).
    password: SecretStr = Field(
        min_length=MIN_PASSWORD_LENGTH,
        max_length=MAX_PASSWORD_LENGTH,
        json_schema_extra=_CREDENTIAL_FIELD,
    )

    #: Requirements 4.8, 4.9: ``False`` accepts a self-signed certificate, ``True``
    #: validates the chain and turns a failure into a TLS validation read failure.
    #: Defaults to ``True`` on purpose -- see the module docstring.
    verify_tls: bool = True

    #: Requirements 4.1, 4.10: the Redfish power resource to read. ``None`` means
    #: :data:`DEFAULT_POWER_PATH`; use :attr:`resolved_power_path` rather than
    #: re-deriving that fallback at each call site.
    power_path: str | None = Field(default=None, max_length=MAX_POWER_PATH_LENGTH)

    #: Requirement 4.1: how long one Redfish request may take before it is abandoned as
    #: a timeout failure (Requirements 4.7, 4.13). This bounds a single request only;
    #: the Poll_Scheduler owns the cycle budget and the retry delays (Requirement 5.5),
    #: and applies its own fallback when a device carries no request timeout at all
    #: (Requirement 5.8). ``model_fields_set`` distinguishes an explicitly configured
    #: 10 seconds from this default for callers that need to know.
    request_timeout_s: float = Field(
        default=DEFAULT_REQUEST_TIMEOUT_S,
        ge=MIN_REQUEST_TIMEOUT_S,
        le=MAX_REQUEST_TIMEOUT_S,
    )

    @property
    def resolved_power_path(self) -> str:
        """The Redfish power resource path to request, applying Requirement 4.10.

        Returns :data:`DEFAULT_POWER_PATH` when ``power_path`` is omitted, so the read
        operation and the connection validation operation cannot disagree about which
        resource an entry names.
        """
        return self.power_path if self.power_path is not None else DEFAULT_POWER_PATH
