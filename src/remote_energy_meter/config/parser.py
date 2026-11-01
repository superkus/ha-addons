"""The Configuration_Parser: ``/data/options.json`` text in, a validated configuration out.

Two severities of failure, and the split between them is the whole design of this module.

**Fatal, whole-configuration** failures mean the addon cannot do its job at all, so the
caller logs the failed startup step and exits non-zero (Requirement 1.4). They are
reported as a :class:`FatalConfigError` with a :class:`FatalReason`:

=================================================  ============
Condition                                          Requirement
=================================================  ============
options text absent, unreadable, or not UTF-8      1.7, 2.10
options text not parseable as an options object    2.10
global poll interval not whole seconds, or out of  2.7
the range 5-3600 s
MQTT host or port missing, or port out of range    2.11
zero accepted ``Device_Entry`` values              2.4
=================================================  ============

**Per-entry** failures exclude one ``Device_Entry``, emit exactly one error record, and
leave every other entry alone: a typo in one of seventeen configured devices must not
turn into a total outage. They are reported as :class:`~remote_energy_meter.config.runtime.EntryRejection`
values covering a malformed identifier (2.12), a case-insensitive duplicate identifier
where the first occurrence wins (2.5), a plugin-schema violation (2.2, 2.3), an empty
credential (8.5), a credential over 256 characters (8.8), and an unresolvable secret
reference (8.6).

## "The first failing field" is a total order, not whichever error surfaced first

Requirement 2.3 has the rejection record name *the first* failing field, so the record
has to be reproducible. pydantic reports errors in its own order, and the credential
checks of Requirements 8.5 and 8.8 are not pydantic errors at all, so neither source can
be trusted to say which field is first.

Instead every candidate failure for an entry — pydantic error, empty credential,
oversized credential, unresolved secret reference — is tagged with its position in the
*declaration* order of the models it came from: the ``DeviceEntry`` field index, then the
field index within the plugin-published schema for anything under ``params``. The
candidate with the lowest position wins, ties broken by a fixed reason precedence that
prefers the more specific requirement (an unresolved reference over an empty value, an
oversized credential over the schema's own length bound). One entry therefore always
produces exactly one rejection, and the same options text always produces the same one.

## Ordering inside an entry

Secret references are substituted **before** validation (Requirement 8.4), so a reference
that resolves to whitespace is caught by the same empty-credential rule as a literal
empty value. Only credential-marked fields are substituted: Requirement 8.4 scopes
resolution to credential fields, and a non-credential field whose literal value happens
to begin with ``!secret`` must stay literal.

The device identifier is checked first, and is claimed the moment it is well formed, even
if the entry is rejected for some later defect. That is Requirement 2.5 read literally —
*two entries declaring the same identifier*, not two accepted entries — and it falls out
of declaration order anyway, ``device_id`` being the first field of ``DeviceEntry``.

## What the parser deliberately does not do

*An entry naming a plugin the parser has no schema for is accepted with its ``params``
untouched.* Requirement 3.4 puts that rejection in the Plugin_Registry, whose record
names every discovered plugin identifier, and duplicating it here would either produce
two records for one entry or hide the entry from the component that owes the record.

*``params`` is carried as the substituted raw mapping, not as a dumped model.* Normalizing
it would write every schema default in explicitly, and the Poll_Scheduler needs
``model_fields_set`` to tell an omitted request timeout from one explicitly configured to
the same value as the default (Requirement 5.8). The plugin's own model is rebuilt from
this mapping by the registry.

*The Requirement 9.7 warning for a rejected log level is not emitted here.* The parser
resolves the level so the ``Configuration_Object`` is complete (Requirements 9.6, 9.7) and
returns the resolution; the record is written by ``configure_logging``, which is the only
component that knows the logging stream is ready to carry it.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, NamedTuple

from pydantic import BaseModel, ValidationError

from remote_energy_meter.config.models import (
    DEFAULT_POLL_INTERVAL_S,
    DEVICE_ID_PATTERN,
    MAX_DEVICE_ID_LENGTH,
    MAX_DEVICES,
    MAX_PLUGIN_ID_LENGTH,
    MAX_POLL_INTERVAL_S,
    MIN_POLL_INTERVAL_S,
    ConfigurationObject,
    DeviceEntry,
    LogLevel,
    MqttSettings,
    credential_field_names,
)
from remote_energy_meter.config.runtime import EntryRejection, RejectionReason
from remote_energy_meter.config.secrets import SecretResolver, UnresolvedSecret
from remote_energy_meter.diagnostics.logger import (
    DEVICE_ID_FIELD,
    LevelResolution,
    get_logger,
    resolve_log_level,
)
from remote_energy_meter.diagnostics.redaction import (
    CREDENTIAL_REGISTRY,
    MAX_CREDENTIAL_LENGTH,
    CredentialRegistry,
)

__all__ = [
    "DEFAULT_OPTIONS_PATH",
    "MAX_ECHOED_VALUE_LENGTH",
    "MAX_OPTIONS_BYTES",
    "FatalConfigError",
    "FatalReason",
    "ParseOutcome",
    "PluginConfigSchema",
    "load_options",
    "parse_options",
    "read_options_text",
]

#: Where the Home Assistant Supervisor writes the Addon_Configuration (Requirement 2.1).
DEFAULT_OPTIONS_PATH: Final = Path("/data/options.json")

#: Largest options file this module will parse. The Addon_Configuration is a short object
#: with at most 50 device entries; a file orders of magnitude larger is a mistake, and
#: parsing it would spend the 30 second startup budget of Requirement 1.3 on nothing.
MAX_OPTIONS_BYTES: Final = 1 << 20

#: How much of a rejected value is echoed into a log record. Requirement 9.1 caps a
#: message at 512 characters, so an absurd value is bounded here rather than allowed to
#: crowd out the rest of the record. Only non-credential values are ever echoed: an
#: identifier, an interval, a plugin identifier, a type name.
MAX_ECHOED_VALUE_LENGTH: Final = 80

#: A plugin's published configuration schema: the pydantic model class a
#: ``Device_Entry``'s ``params`` mapping is validated against (Requirements 2.2, 3.1).
type PluginConfigSchema = type[BaseModel]

_DEVICE_ID_RE: Final = re.compile(DEVICE_ID_PATTERN)

#: Declaration order of the ``DeviceEntry`` envelope fields, which is the outer half of
#: the "first failing field" ordering (Requirement 2.3).
_ENVELOPE_ORDER: Final[Mapping[str, int]] = MappingProxyType(
    {name: index for index, name in enumerate(DeviceEntry.model_fields)}
)
_ENVELOPE_FIELD_COUNT: Final = len(_ENVELOPE_ORDER)
_PARAMS_ORDER_INDEX: Final = _ENVELOPE_ORDER["params"]

#: Sorts below any field index, so an envelope field precedes every ``params`` field that
#: shares its outer position.
_ENVELOPE_INNER_INDEX: Final = -1

#: Tie-break when two candidates name the same field, lowest first. The more specific
#: requirement wins: an unresolved reference over an empty value (there is no value to
#: call empty), and Requirement 8.8's 256-character limit over a plugin's own tighter
#: length bound, so the record cites the rule the administrator can act on.
_REASON_PRECEDENCE: Final[Mapping[RejectionReason, int]] = MappingProxyType(
    {
        RejectionReason.UNRESOLVED_SECRET: 0,
        RejectionReason.EMPTY_CREDENTIAL: 1,
        RejectionReason.OVERSIZED_CREDENTIAL: 2,
        RejectionReason.SCHEMA_VIOLATION: 3,
    }
)

_ACCEPTED_IDENTIFIER_FORM: Final = (
    f"1 to {MAX_DEVICE_ID_LENGTH} characters drawn from letters, digits, hyphen, and underscore"
)
_ACCEPTED_INTERVAL_RANGE: Final = (
    f"a whole number of seconds from {MIN_POLL_INTERVAL_S} to {MAX_POLL_INTERVAL_S} inclusive"
)


# --------------------------------------------------------------------------------------------------
# outcome types
# --------------------------------------------------------------------------------------------------


class FatalReason(StrEnum):
    """Why the whole Addon_Configuration was rejected.

    Every member ends the process with a non-zero status before the Poll_Scheduler starts,
    and is reported by the caller as the failure classification of the configuration
    startup step (Requirement 1.4).
    """

    OPTIONS_UNREADABLE = "options_unreadable"
    """The options text is absent, could not be read, or is not valid UTF-8
    (Requirements 1.7, 2.10). Distinct from :attr:`OPTIONS_UNPARSEABLE` because
    Requirement 2.10 has the record say whether the read or the parse failed."""

    OPTIONS_UNPARSEABLE = "options_unparseable"
    """The options text was read but is not an options object: invalid JSON, or valid JSON
    that is not a mapping of fields (Requirement 2.10)."""

    INVALID_GLOBAL_INTERVAL = "invalid_global_interval"
    """The global poll interval is not a whole number of seconds, or falls outside
    5-3600 seconds inclusive (Requirement 2.7)."""

    INVALID_MQTT_SETTINGS = "invalid_mqtt_settings"
    """The MQTT connection section is absent, omits the broker host or port, declares a
    port outside 1-65535, names an unresolvable secret, or otherwise does not validate
    (Requirement 2.11)."""

    NO_ACCEPTED_DEVICES = "no_accepted_devices"
    """No ``Device_Entry`` survived validation, so there is nothing to meter
    (Requirement 2.4)."""


@dataclass(frozen=True, slots=True)
class FatalConfigError:
    """A whole-configuration rejection, described well enough to act on.

    Attributes:
        reason: The classification the caller reports as the startup failure
            classification (Requirement 1.4).
        detail: One sentence naming the offending value and the accepted form, which is
            what Requirements 2.7 and 2.11 require the error record to carry.
        field_name: The offending field in options-file terms, dotted for a nested field
            as in ``mqtt.port``. ``None`` when the failure concerns the whole document.
    """

    reason: FatalReason
    detail: str
    field_name: str | None = None

    def describe(self) -> str:
        """One line naming the reason, the field when there is one, and the detail."""
        if self.field_name is None:
            return f"{self.reason.value}: {self.detail}"
        return f"{self.reason.value} at {self.field_name}: {self.detail}"


class ParseOutcome(NamedTuple):
    """Everything one parse produced.

    ``config`` and ``fatal`` are mutually exclusive: exactly one of them is set. Both
    ``rejections`` and ``level_resolution`` are populated on either path, because a
    configuration that ends up fatal for having zero accepted entries still owes the
    caller the per-entry reasons, and the log level is resolved before any of the fatal
    checks so that whatever records the caller writes are written at the configured level.

    Attributes:
        config: The fully resolved Configuration_Object, or ``None`` when the
            configuration was rejected outright.
        fatal: The whole-configuration rejection, or ``None`` when parsing succeeded.
        rejections: One entry per excluded ``Device_Entry``, in device-list order. The
            parser has already written one error record for each; these are for the
            caller's own accounting.
        level_resolution: The effective log level and the value rejected to reach it, if
            any (Requirements 9.2, 9.6, 9.7). Passed on to ``configure_logging``, which
            writes the Requirement 9.7 warning.
    """

    config: ConfigurationObject | None
    fatal: FatalConfigError | None
    rejections: tuple[EntryRejection, ...] = ()
    level_resolution: LevelResolution = LevelResolution(LogLevel.INFO)

    @property
    def ok(self) -> bool:
        """True when a Configuration_Object was produced."""
        return self.config is not None


# --------------------------------------------------------------------------------------------------
# internal candidate machinery
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Candidate:
    """One possible reason to reject an entry, positioned in declaration order.

    ``order`` is ``(envelope field index, field index within the plugin schema)``; an
    envelope field carries :data:`_ENVELOPE_INNER_INDEX` as its inner index so it sorts
    ahead of every ``params`` field at the same outer position.
    """

    order: tuple[int, int]
    reason: RejectionReason
    field_name: str
    detail: str

    @property
    def sort_key(self) -> tuple[int, int, int, str]:
        """Total order over candidates: position, then reason precedence, then name."""
        precedence = _REASON_PRECEDENCE.get(self.reason, len(_REASON_PRECEDENCE))
        return (self.order[0], self.order[1], precedence, self.field_name)


@dataclass(frozen=True, slots=True)
class _Rejected:
    """An excluded entry together with the record the parser writes for it."""

    rejection: EntryRejection
    message: str
    extra: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------------------------------


def _echo(value: object) -> str:
    """Render ``value`` for a log record, bounded in length.

    Never called on a credential value: rejection records name credential *fields*, never
    their contents (Requirements 8.2, 8.6).
    """
    rendered = repr(value)
    if len(rendered) <= MAX_ECHOED_VALUE_LENGTH:
        return rendered
    return rendered[: MAX_ECHOED_VALUE_LENGTH - 3] + "..."


def _field_order(model_cls: type[BaseModel]) -> Mapping[str, int]:
    """Declaration index of every field of ``model_cls``."""
    return {name: index for index, name in enumerate(model_cls.model_fields)}


def _first_error_field(error: ValidationError, order: Mapping[str, int]) -> tuple[str, str]:
    """The first failing field of ``error`` by declaration index, with its message.

    An error whose location names no declared field — a whole-model error, or a key the
    model does not declare — sorts last, because it has no declaration index to order by.
    """
    entries = error.errors()
    ranked = sorted(
        entries,
        key=lambda item: (
            order.get(str(item["loc"][0]) if item["loc"] else "", len(order)),
            ".".join(str(part) for part in item["loc"]),
        ),
    )
    first = ranked[0]
    name = ".".join(str(part) for part in first["loc"]) if first["loc"] else "(model)"
    return name, str(first["msg"])


def _candidates_from_error(error: ValidationError, order: Mapping[str, int]) -> list[_Candidate]:
    """Turn every plugin-schema error into a candidate positioned under ``params``.

    Field names are reported as ``params.<name>`` so a plugin free to name a parameter
    ``plugin`` or ``display_name`` cannot produce a rejection that reads as though the
    envelope field failed.
    """
    candidates: list[_Candidate] = []
    for item in error.errors():
        location = item["loc"]
        head = str(location[0]) if location else ""
        dotted = ".".join(str(part) for part in location) if location else "(model)"
        candidates.append(
            _Candidate(
                order=(_PARAMS_ORDER_INDEX, order.get(head, len(order))),
                reason=RejectionReason.SCHEMA_VIOLATION,
                field_name=f"params.{dotted}",
                detail=str(item["msg"]),
            )
        )
    return candidates


# --------------------------------------------------------------------------------------------------
# reading and parsing the document
# --------------------------------------------------------------------------------------------------


def read_options_text(path: Path | None = None) -> str | FatalConfigError:
    """Read the Addon_Configuration text, or describe why it could not be read.

    Every failure here is a *read* failure rather than a parse failure, which is the
    discriminator Requirement 2.10 requires the error record to carry. A file that is not
    valid UTF-8 counts as unreadable: no text was produced, so there was nothing to parse.
    """
    target = path if path is not None else DEFAULT_OPTIONS_PATH

    try:
        size = target.stat().st_size
    except FileNotFoundError:
        return FatalConfigError(
            FatalReason.OPTIONS_UNREADABLE,
            f"no addon options file exists at {target}",
        )
    except OSError as error:
        return FatalConfigError(
            FatalReason.OPTIONS_UNREADABLE,
            f"the addon options file at {target} could not be inspected: {type(error).__name__}",
        )

    if size > MAX_OPTIONS_BYTES:
        return FatalConfigError(
            FatalReason.OPTIONS_UNREADABLE,
            f"the addon options file at {target} is larger than {MAX_OPTIONS_BYTES} bytes",
        )

    try:
        return target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return FatalConfigError(
            FatalReason.OPTIONS_UNREADABLE,
            f"no addon options file exists at {target}",
        )
    except UnicodeDecodeError:
        return FatalConfigError(
            FatalReason.OPTIONS_UNREADABLE,
            f"the addon options file at {target} is not valid UTF-8",
        )
    except OSError as error:
        return FatalConfigError(
            FatalReason.OPTIONS_UNREADABLE,
            f"the addon options file at {target} could not be read: {type(error).__name__}",
        )


def _parse_document(raw_text: str) -> dict[str, Any] | FatalConfigError:
    """Parse the options text into a mapping of fields (Requirement 2.10).

    JSON, because that is what Supervisor writes to ``/data/options.json``. Valid JSON
    that is not an object is reported as unparseable rather than as a missing field: a
    list or a bare scalar is not an Addon_Configuration at all, and naming one absent
    field would misdirect the administrator.
    """
    try:
        document = json.loads(raw_text)
    except json.JSONDecodeError as error:
        return FatalConfigError(
            FatalReason.OPTIONS_UNPARSEABLE,
            f"the addon options text is not valid JSON: {error.msg} at line {error.lineno}, column {error.colno}",
        )

    if not isinstance(document, Mapping):
        return FatalConfigError(
            FatalReason.OPTIONS_UNPARSEABLE,
            f"the addon options text holds a {type(document).__name__}, not a mapping of configuration fields",
        )

    return {str(key): value for key, value in document.items()}


# --------------------------------------------------------------------------------------------------
# global settings
# --------------------------------------------------------------------------------------------------


def _resolve_global_interval(raw: object) -> int | FatalConfigError:
    """Resolve the global poll interval (Requirements 2.6, 2.7).

    An omitted value defaults to 30 seconds, and so does an explicit ``null``: Supervisor
    writes an unset optional option as ``null``, so the two are the same statement about
    intent. A present value must be a whole number of seconds within 5-3600 inclusive;
    anything else rejects the whole configuration with a record naming the supplied value
    and the accepted range.

    ``30.0`` is accepted and ``30.5`` is not, because Requirement 2.7 constrains the
    *value* rather than how it was written. A boolean is rejected even though it is an
    ``int`` subclass, and so is a numeric string: reading ``true`` or ``"30"`` as a poll
    interval would be a silent guess about what the administrator meant.
    """
    if raw is None:
        return DEFAULT_POLL_INTERVAL_S

    whole: int
    if isinstance(raw, bool):
        return _interval_rejected(raw)
    if isinstance(raw, int):
        whole = raw
    elif isinstance(raw, float):
        if not raw.is_integer():
            return _interval_rejected(raw)
        whole = int(raw)
    else:
        return _interval_rejected(raw)

    if not MIN_POLL_INTERVAL_S <= whole <= MAX_POLL_INTERVAL_S:
        return _interval_rejected(raw)
    return whole


def _interval_rejected(raw: object) -> FatalConfigError:
    return FatalConfigError(
        FatalReason.INVALID_GLOBAL_INTERVAL,
        f"the global poll interval {_echo(raw)} is not accepted; the accepted range is {_ACCEPTED_INTERVAL_RANGE}",
        field_name="poll_interval_s",
    )


def _build_mqtt(raw: object, secrets: SecretResolver) -> MqttSettings | FatalConfigError:
    """Validate the MQTT connection section (Requirement 2.11).

    Requirement 2.11 names three fatal conditions — host omitted, port omitted, port
    outside 1-65535 — and every other defect in this section takes the same path, because
    there is no defined per-field fallback for it. That includes an unresolvable secret
    reference for the broker username or password: the alternatives are to drop the
    credential or to authenticate with the literal text ``!secret name``, and both are
    silently wrong in a way that surfaces only as a broker rejection much later.
    """
    if raw is None:
        return FatalConfigError(
            FatalReason.INVALID_MQTT_SETTINGS,
            "the MQTT connection section is absent; a broker host address and port are required",
            field_name="mqtt",
        )
    if not isinstance(raw, Mapping):
        return FatalConfigError(
            FatalReason.INVALID_MQTT_SETTINGS,
            f"the MQTT connection section holds a {type(raw).__name__}, not a mapping of fields",
            field_name="mqtt",
        )

    payload = {str(key): value for key, value in raw.items()}
    for name in credential_field_names(MqttSettings):
        if name not in payload:
            continue
        substituted, unresolved = secrets.substitute(payload[name])
        if unresolved is not None:
            return FatalConfigError(
                FatalReason.INVALID_MQTT_SETTINGS,
                unresolved.describe(),
                field_name=f"mqtt.{name}",
            )
        payload[name] = substituted

    try:
        return MqttSettings.model_validate(payload)
    except ValidationError as error:
        name, message = _first_error_field(error, _field_order(MqttSettings))
        return FatalConfigError(
            FatalReason.INVALID_MQTT_SETTINGS,
            f"the MQTT connection field {name!r} is not accepted: {message}",
            field_name=f"mqtt.{name}",
        )


# --------------------------------------------------------------------------------------------------
# device entries
# --------------------------------------------------------------------------------------------------


def _reject_identifier(index: int, raw_id: object) -> _Rejected:
    """Requirement 2.12: name the rejected identifier and the accepted identifier form."""
    return _Rejected(
        rejection=EntryRejection(
            device_id=raw_id if isinstance(raw_id, str) else None,
            reason=RejectionReason.INVALID_IDENTIFIER,
            field_name="device_id",
        ),
        message=(
            f"device entry at index {index} declares the identifier {_echo(raw_id)}, which is not accepted; "
            f"an accepted identifier is {_ACCEPTED_IDENTIFIER_FORM}"
        ),
        extra={"entry_index": index, "rejected_identifier": _echo(raw_id)},
    )


def _check_credentials(
    params: Mapping[str, Any],
    schema: PluginConfigSchema,
    order: Mapping[str, int],
) -> list[_Candidate]:
    """Requirements 8.5 and 8.8, over whatever fields the plugin marked as credentials.

    A *required* credential field that is absent, or holds only whitespace, is reported as
    empty rather than as a missing required field: Requirement 8.5 is the more specific
    rule and its record names the credential field. An *optional* credential field may be
    absent or null — that is the plugin declaring a default — but if it is present as
    text it must still be non-empty and within the length limit.
    """
    candidates: list[_Candidate] = []
    for name in credential_field_names(schema):
        model_field = schema.model_fields[name]
        present = name in params
        value = params.get(name)

        if (not present or value is None) and model_field.is_required():
            candidates.append(
                _Candidate(
                    order=(_PARAMS_ORDER_INDEX, order.get(name, len(order))),
                    reason=RejectionReason.EMPTY_CREDENTIAL,
                    field_name=f"params.{name}",
                    detail="the credential field is absent",
                )
            )
            continue

        if not isinstance(value, str):
            # A non-string credential is a type mismatch, which the plugin schema reports
            # with its own message and position (Requirement 2.2).
            continue

        if not value.strip():
            candidates.append(
                _Candidate(
                    order=(_PARAMS_ORDER_INDEX, order.get(name, len(order))),
                    reason=RejectionReason.EMPTY_CREDENTIAL,
                    field_name=f"params.{name}",
                    detail="the credential field holds no characters once leading and trailing whitespace is removed",
                )
            )
        elif len(value) > MAX_CREDENTIAL_LENGTH:
            candidates.append(
                _Candidate(
                    order=(_PARAMS_ORDER_INDEX, order.get(name, len(order))),
                    reason=RejectionReason.OVERSIZED_CREDENTIAL,
                    field_name=f"params.{name}",
                    detail=f"the credential field holds more than {MAX_CREDENTIAL_LENGTH} characters",
                )
            )
    return candidates


def _substitute_credentials(
    params: Mapping[str, Any],
    schema: PluginConfigSchema,
    order: Mapping[str, int],
    secrets: SecretResolver,
) -> tuple[dict[str, Any], list[_Candidate]]:
    """Resolve ``!secret`` references in credential fields before validation (Req. 8.4).

    A reference that does not resolve leaves the original value in place and contributes a
    candidate rejection, so an *earlier* failing field can still win the report. The
    candidate's detail names the reference and never the referenced value (Req. 8.6).
    """
    resolved = dict(params)
    candidates: list[_Candidate] = []
    for name in credential_field_names(schema):
        if name not in resolved:
            continue
        substituted, unresolved = secrets.substitute(resolved[name])
        if unresolved is not None:
            candidates.append(_unresolved_candidate(name, unresolved, order))
            continue
        resolved[name] = substituted
    return resolved, candidates


def _unresolved_candidate(
    name: str,
    unresolved: UnresolvedSecret,
    order: Mapping[str, int],
) -> _Candidate:
    return _Candidate(
        order=(_PARAMS_ORDER_INDEX, order.get(name, len(order))),
        reason=RejectionReason.UNRESOLVED_SECRET,
        field_name=f"params.{name}",
        detail=unresolved.describe(),
    )


def _validate_entry(
    index: int,
    raw: object,
    *,
    schemas: Mapping[str, PluginConfigSchema],
    secrets: SecretResolver,
    claimed: dict[str, str],
) -> DeviceEntry | _Rejected:
    """Validate one raw device list element into a ``DeviceEntry``, or reject it.

    Never raises: every defect becomes a :class:`_Rejected` so the remaining entries carry
    on (Requirements 2.3, 2.5, 2.12, 8.5, 8.6, 8.8).
    """
    if not isinstance(raw, Mapping):
        return _Rejected(
            rejection=EntryRejection(device_id=None, reason=RejectionReason.SCHEMA_VIOLATION, field_name=None),
            message=(
                f"device entry at index {index} holds a {type(raw).__name__}, not a mapping of fields; "
                "the entry is excluded"
            ),
            extra={"entry_index": index},
        )

    payload = {str(key): value for key, value in raw.items()}

    # -- device_id (Requirements 2.12, 2.5): the first declared field, so the first checked
    raw_id = payload.get("device_id")
    if not isinstance(raw_id, str) or not _DEVICE_ID_RE.fullmatch(raw_id) or len(raw_id) > MAX_DEVICE_ID_LENGTH:
        return _reject_identifier(index, raw_id)

    folded = raw_id.lower()
    if folded in claimed:
        first = claimed[folded]
        return _Rejected(
            rejection=EntryRejection(
                device_id=raw_id,
                reason=RejectionReason.DUPLICATE_IDENTIFIER,
                field_name="device_id",
            ),
            message=(
                f"device identifier {raw_id!r} duplicates {first!r} declared earlier, compared without regard to "
                "letter case; the first occurrence is kept and this entry is excluded"
            ),
            extra={DEVICE_ID_FIELD: raw_id, "entry_index": index, "duplicate_of": first},
        )
    # Claimed as soon as it is well formed, even if this entry is rejected below:
    # Requirement 2.5 compares *declared* identifiers, not accepted ones.
    claimed[folded] = raw_id

    # -- plugin identifier: needed to find the schema, so checked before the schema is used
    raw_plugin = payload.get("plugin")
    if not isinstance(raw_plugin, str) or not 1 <= len(raw_plugin) <= MAX_PLUGIN_ID_LENGTH:
        return _rejection_from(
            raw_id,
            _envelope_candidate(
                "plugin",
                f"is not a string of 1 to {MAX_PLUGIN_ID_LENGTH} characters (found {_echo(raw_plugin)})",
            ),
            index,
        )

    raw_params = payload.get("params")
    if raw_params is None:
        # An absent parameter mapping is treated as an empty one, so the plugin schema
        # reports the specific required field that is missing (Requirement 2.3) rather
        # than the parser reporting the container.
        raw_params = {}
    if not isinstance(raw_params, Mapping):
        return _rejection_from(
            raw_id,
            _envelope_candidate(
                "params",
                f"holds a {type(raw_params).__name__}, not a mapping of connection parameters",
            ),
            index,
        )
    params = {str(key): value for key, value in raw_params.items()}

    candidates: list[_Candidate] = []
    schema = schemas.get(raw_plugin)
    if schema is not None:
        order = _field_order(schema)
        params, candidates = _substitute_credentials(params, schema, order, secrets)
        candidates.extend(_check_credentials(params, schema, order))
        try:
            schema.model_validate(params)
        except ValidationError as error:
            candidates.extend(_candidates_from_error(error, order))
    # Otherwise Requirement 3.4 applies and the rejection belongs to the Plugin_Registry,
    # whose record names every discovered plugin identifier. The entry passes through with
    # its parameters untouched -- unvalidated, and with no secret substituted, because
    # without a schema there is no way to know which fields are credentials.

    built = _build_entry(raw_id, raw_plugin, payload, params)
    if isinstance(built, _Candidate):
        # An envelope defect (display name, per-device poll interval) competes on
        # declaration order with everything found above.
        candidates.append(built)
    elif not candidates:
        return built

    return _rejection_from(raw_id, min(candidates, key=lambda item: item.sort_key), index)


def _build_entry(
    device_id: str,
    plugin: str,
    payload: Mapping[str, Any],
    params: Mapping[str, Any],
) -> DeviceEntry | _Candidate:
    """Validate the ``DeviceEntry`` envelope: display name and per-device poll interval.

    Requirement 2.2 makes a device-specific poll interval outside 5-3600 seconds a
    validation failure, and Requirement 2.3 excludes the entry for it. The Supervisor
    options schema deliberately leaves that bound unconstrained, because a Supervisor
    schema failure would refuse the whole options blob rather than one entry, so this is
    the only place the bound is enforced.
    """
    try:
        return DeviceEntry(
            device_id=device_id,
            plugin=plugin,
            display_name=payload.get("display_name"),
            poll_interval_s=payload.get("poll_interval_s"),
            params=dict(params),
        )
    except ValidationError as error:
        name, message = _first_error_field(error, _ENVELOPE_ORDER)
        return _envelope_candidate(name, message)


def _envelope_candidate(field_name: str, detail: str) -> _Candidate:
    """A schema-violation candidate positioned on a ``DeviceEntry`` envelope field."""
    head = field_name.split(".")[0]
    return _Candidate(
        order=(_ENVELOPE_ORDER.get(head, _ENVELOPE_FIELD_COUNT), _ENVELOPE_INNER_INDEX),
        reason=RejectionReason.SCHEMA_VIOLATION,
        field_name=field_name,
        detail=detail,
    )


def _rejection_from(device_id: str, candidate: _Candidate, index: int) -> _Rejected:
    """One entry, one rejection, naming the entry identifier and the first failing field."""
    return _Rejected(
        rejection=EntryRejection(
            device_id=device_id,
            reason=candidate.reason,
            field_name=candidate.field_name,
        ),
        message=(
            f"device entry {device_id!r} is excluded ({candidate.reason.value}): "
            f"field {candidate.field_name!r}: {candidate.detail}"
        ),
        extra={
            DEVICE_ID_FIELD: device_id,
            "entry_index": index,
            "reason": candidate.reason.value,
            "field": candidate.field_name,
        },
    )


def _surplus_rejection(device_id: str | None, index: int) -> _Rejected:
    """Requirement 2.1 caps the device list at 50 entries.

    The surplus is excluded per entry rather than rejecting the whole configuration, which
    keeps 50 devices metered instead of none. Reported as a schema violation of the device
    list, because that is the bound the entry violates.
    """
    return _Rejected(
        rejection=EntryRejection(
            device_id=device_id,
            reason=RejectionReason.SCHEMA_VIOLATION,
            field_name="devices",
        ),
        message=f"device entry at index {index} exceeds the limit of {MAX_DEVICES} accepted devices, so it is excluded",
        extra={"entry_index": index},
    )


# --------------------------------------------------------------------------------------------------
# logging
# --------------------------------------------------------------------------------------------------


def _log_fatal(log: logging.Logger, fatal: FatalConfigError) -> None:
    extra: dict[str, Any] = {"reason": fatal.reason.value}
    if fatal.field_name is not None:
        extra["field"] = fatal.field_name
    log.error("%s", fatal.detail, extra=extra)


def _log_rejection(log: logging.Logger, rejected: _Rejected) -> None:
    log.error("%s", rejected.message, extra=dict(rejected.extra))


# --------------------------------------------------------------------------------------------------
# the parse
# --------------------------------------------------------------------------------------------------


def parse_options(
    raw_text: str,
    *,
    schemas: Mapping[str, PluginConfigSchema],
    secrets: SecretResolver | None = None,
    logger: logging.Logger | None = None,
    registry: CredentialRegistry | None = CREDENTIAL_REGISTRY,
) -> ParseOutcome:
    """Parse Addon_Configuration text into a Configuration_Object.

    Args:
        raw_text: The options text, as written by Supervisor.
        schemas: The configuration schema published by each discovered plugin, keyed by
            plugin identifier. An entry naming a key that is absent here is passed through
            with unvalidated parameters for the Plugin_Registry to reject under
            Requirement 3.4.
        secrets: Resolves ``!secret`` references. Defaults to a resolver over the mounted
            Home Assistant configuration directory, which reads nothing until the first
            reference is met.
        logger: Where the error records of Requirements 2.3, 2.5, 2.7, 2.10, 2.11, 2.12,
            8.5, 8.6, and 8.8 are written. Defaults to this module's logger.
        registry: Credential values of accepted entries and of the MQTT section are
            registered here, so they are redacted from every later record
            (Requirements 8.1, 8.2). Pass ``None`` to opt out.

    Returns:
        A :class:`ParseOutcome` carrying either a Configuration_Object or a
        :class:`FatalConfigError`, plus every per-entry rejection and the resolved log
        level.
    """
    log = logger if logger is not None else get_logger(__name__)
    resolver = secrets if secrets is not None else SecretResolver()

    document = _parse_document(raw_text)
    if isinstance(document, FatalConfigError):
        _log_fatal(log, document)
        return ParseOutcome(config=None, fatal=document, level_resolution=resolve_log_level(None))

    # Resolved before any fatal check, so whatever the caller logs next is logged at the
    # configured level (Requirements 9.2, 9.6, 9.7).
    level = resolve_log_level(document.get("log_level"))

    interval = _resolve_global_interval(document.get("poll_interval_s"))
    if isinstance(interval, FatalConfigError):
        _log_fatal(log, interval)
        return ParseOutcome(config=None, fatal=interval, level_resolution=level)

    mqtt = _build_mqtt(document.get("mqtt"), resolver)
    if isinstance(mqtt, FatalConfigError):
        _log_fatal(log, mqtt)
        return ParseOutcome(config=None, fatal=mqtt, level_resolution=level)

    accepted, rejections = _validate_devices(document.get("devices"), schemas=schemas, secrets=resolver, log=log)

    if not accepted:
        fatal = FatalConfigError(
            FatalReason.NO_ACCEPTED_DEVICES,
            f"no device entry was accepted; {len(rejections)} were excluded during validation",
            field_name="devices",
        )
        _log_fatal(log, fatal)
        return ParseOutcome(config=None, fatal=fatal, rejections=rejections, level_resolution=level)

    try:
        config = ConfigurationObject(
            poll_interval_s=interval,
            log_level=level.level,
            mqtt=mqtt,
            devices=accepted,
        )
    except ValidationError as error:  # pragma: no cover - the parser pre-checks every bound
        name, message = _first_error_field(error, _field_order(ConfigurationObject))
        fatal = FatalConfigError(
            FatalReason.OPTIONS_UNPARSEABLE,
            f"the configuration field {name!r} is not accepted: {message}",
            field_name=name,
        )
        _log_fatal(log, fatal)
        return ParseOutcome(config=None, fatal=fatal, rejections=rejections, level_resolution=level)

    if registry is not None:
        # Registered here rather than only at startup, so the window in which a credential
        # could reach a record unredacted is as short as the parse itself.
        registry.register_model(config.mqtt)
        for entry in config.devices:
            schema = schemas.get(entry.plugin)
            if schema is None:
                continue
            for name in credential_field_names(schema):
                value = entry.params.get(name)
                if isinstance(value, str):
                    registry.register(value)

    return ParseOutcome(config=config, fatal=None, rejections=rejections, level_resolution=level)


def _validate_devices(
    raw_devices: object,
    *,
    schemas: Mapping[str, PluginConfigSchema],
    secrets: SecretResolver,
    log: logging.Logger,
) -> tuple[list[DeviceEntry], tuple[EntryRejection, ...]]:
    """Validate the device list in order, keeping the accepted entries and reporting the rest."""
    if raw_devices is None or not isinstance(raw_devices, list | tuple):
        # No list means no entry to validate, which Requirement 2.4 already covers: the
        # caller reports zero accepted entries and exits non-zero.
        if raw_devices is not None:
            log.error(
                "%s",
                f"the device list holds a {type(raw_devices).__name__}, not a list of device entries",
                extra={"field": "devices"},
            )
        return [], ()

    accepted: list[DeviceEntry] = []
    rejections: list[EntryRejection] = []
    claimed: dict[str, str] = {}

    for index, raw in enumerate(raw_devices):
        if len(accepted) >= MAX_DEVICES:
            surplus = _surplus_rejection(_identifier_of(raw), index)
            _log_rejection(log, surplus)
            rejections.append(surplus.rejection)
            continue

        outcome = _validate_entry(index, raw, schemas=schemas, secrets=secrets, claimed=claimed)
        if isinstance(outcome, _Rejected):
            _log_rejection(log, outcome)
            rejections.append(outcome.rejection)
        else:
            accepted.append(outcome)

    return accepted, tuple(rejections)


def _identifier_of(raw: object) -> str | None:
    """The declared identifier of a raw entry, when it has a usable one."""
    if isinstance(raw, Mapping):
        value = raw.get("device_id")
        if isinstance(value, str):
            return value
    return None


def load_options(
    path: Path | None = None,
    *,
    schemas: Mapping[str, PluginConfigSchema],
    secrets: SecretResolver | None = None,
    logger: logging.Logger | None = None,
    registry: CredentialRegistry | None = CREDENTIAL_REGISTRY,
) -> ParseOutcome:
    """Read and parse the Addon_Configuration from ``path``.

    Splits the read from the parse so the error record can say which one failed
    (Requirements 1.7, 2.10). ``path`` defaults to :data:`DEFAULT_OPTIONS_PATH`, where
    Supervisor writes the options.
    """
    log = logger if logger is not None else get_logger(__name__)

    raw_text = read_options_text(path)
    if isinstance(raw_text, FatalConfigError):
        _log_fatal(log, raw_text)
        return ParseOutcome(config=None, fatal=raw_text, level_resolution=resolve_log_level(None))

    return parse_options(raw_text, schemas=schemas, secrets=secrets, logger=log, registry=registry)
