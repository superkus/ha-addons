"""The Configuration_Printer: a Configuration_Object back out as Addon_Configuration text.

Requirement 2.8: the text this module produces is text the Configuration_Parser accepts
without recording a validation failure. Requirement 2.9 goes further and makes the pair a
round trip -- parsing the printed text must reproduce a Configuration_Object equal to the
original field for field -- which is what makes the parser testable as a property rather
than only against hand-written examples.

.. danger::

   **The returned string carries credential values in plaintext.**

   Every credential is emitted as its resolved literal value: the MQTT broker username and
   password, and every plugin parameter the plugin's schema marks as a credential
   (Requirement 8.1). This module is the one component that legitimately holds plaintext,
   and it holds it only in its return value.

   The returned string MUST NOT be written to a log record (Requirement 8.2) and MUST NOT
   be placed in an MQTT payload of any kind, including a discovery configuration, a state
   value, an availability value, a diagnostic attribute, or the last will
   (Requirement 8.3). It is for a caller that already holds the credentials -- writing a
   normalized options file, or a test asserting the round trip.

   Two structural choices back that rule up: this module imports no logging and no MQTT
   client, so neither sink is reachable from here, and nothing is emitted as a side
   effect. The only way a credential leaves is through the value the caller asked for.

## Why literal values rather than ``!secret`` references

A credential field of the Addon_Configuration may be written as a literal or as a
``!secret <name>`` reference, and the parser substitutes the reference before validating
(Requirement 8.4). By the time a Configuration_Object exists, the reference is gone: the
object holds the value, not the name it was written under. Printing a reference would
therefore mean inventing one, and re-parsing it would resolve to whatever the secret store
says *now* rather than to the value in hand -- not a round trip. The reference is a
convenience of the source text; the value is the configuration.

The one case this leaves open is a *literal* credential whose own text happens to be
shaped like a reference, such as a password of ``!secret hunter2``. The parser reads the
printed value as a reference again, and there is no escape syntax to prevent it. That is a
property of the reference syntax rather than of this module, so nothing is quoted, escaped,
or rewritten here: the value is printed verbatim and the caveat is stated instead of
papered over.

## Why every field is emitted, including the ones holding defaults

A printer that omitted defaults would be lossy in a way that only shows up later. The
parser applies defaults itself -- 30 seconds for the global poll interval (Requirement
2.6), ``info`` for the log level (Requirement 9.6) -- so a Configuration_Object is fully
resolved, and re-parsing an abbreviated document would re-derive those values from
defaults rather than read them back. That happens to agree today and would stop agreeing
the moment a default changes, turning a version bump into a silent configuration change.
Emitting every declared field makes parse-print-parse a fixed point on the first pass.

Fields are discovered from the models rather than listed here, so a field added to
:class:`~remote_energy_meter.config.models.MqttSettings` or
:class:`~remote_energy_meter.config.models.DeviceEntry` is printed without this module
being touched, and Requirements 2.8 and 2.9 stay true as the schema grows.

## Why ``params`` is copied rather than normalized

``Device_Entry.params`` is emitted as the mapping the parser carried, key for key. It is
deliberately *not* round-tripped through the plugin's published schema, because dumping a
validated model would write every schema default in explicitly and destroy the distinction
the Poll_Scheduler depends on: ``model_fields_set`` is how an omitted per-device request
timeout is told from one explicitly configured to the same value as the default
(Requirement 5.8). Normalizing here would silently convert the former into the latter.

The consequence is that ``params`` values must be representable in the options format.
They are, whenever the Configuration_Object came from the parser, because they came out of
the options document in the first place; :func:`print_config` raises ``TypeError`` for a
programmatically constructed mapping holding a value JSON cannot express.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from enum import Enum
from typing import Any, Final

from pydantic import BaseModel, SecretStr

from remote_energy_meter.config.models import ConfigurationObject

__all__ = [
    "PRINT_INDENT",
    "as_options_document",
    "print_config",
]

#: Indent width of the printed document. The output is read by administrators as often as
#: by the parser, so it is indented rather than compact.
PRINT_INDENT: Final = 2


def print_config(config: ConfigurationObject) -> str:
    """Serialize ``config`` as Addon_Configuration text (Requirement 2.8).

    The output is JSON, because that is the format Supervisor writes to
    ``/data/options.json`` and the format the Configuration_Parser reads. Every declared
    field of every model is present, including fields holding their default value, and
    every credential is present as its resolved literal value.

    Args:
        config: A Configuration_Object, ordinarily one the Configuration_Parser produced.

    Returns:
        Text that :func:`~remote_energy_meter.config.parser.parse_options` accepts without
        recording a validation failure, and that parses back to a Configuration_Object
        equal to ``config`` field for field (Requirement 2.9).

        **The text carries credentials in plaintext.** See the module docstring: it must
        not reach a log record (Requirement 8.2) or an MQTT payload (Requirement 8.3).

    Raises:
        TypeError: If a ``Device_Entry.params`` mapping holds a value that has no options
            format representation. Cannot happen for a Configuration_Object the parser
            produced, whose parameters were read from the options document.
    """
    # `allow_nan` is left at its default: `json` renders a non-finite float as `NaN` or
    # `Infinity` and `json.loads` reads both back, so the round trip stays total for a
    # plugin parameter holding one. Rejecting it here would turn an odd but parseable
    # options file into a printer that cannot print its own input.
    return json.dumps(as_options_document(config), indent=PRINT_INDENT, ensure_ascii=False) + "\n"


def as_options_document(config: ConfigurationObject) -> dict[str, Any]:
    """Render ``config`` as the mapping :func:`print_config` serializes.

    Exposed separately so a caller can emit another options-shaped format, and so a test
    can assert the round trip is a fixed point without going through text. Carries
    credentials in plaintext, exactly as :func:`print_config` does.

    Field order follows model declaration order, which puts the global settings ahead of
    the MQTT section and the device list.
    """
    return _render_model(config)


def _render_model(model: BaseModel) -> dict[str, Any]:
    """Render every declared field of ``model``, in declaration order.

    Iterating the model's fields rather than a list written here is what makes the printer
    total over the schema: a field the models declare is a field the printer emits.
    ``model_dump`` is not used because its JSON mode renders a
    :class:`~pydantic.SecretStr` as a mask, which would print a configuration that cannot
    authenticate and would round-trip the mask itself into a credential.
    """
    return {name: _render(getattr(model, name)) for name in type(model).model_fields}


def _render(value: object) -> Any:
    """Render one field value into its options-format representation.

    Unwraps a credential to its plaintext (see the module docstring), renders an
    enumeration as its value, walks nested models and device lists, and copies a mapping
    -- ``Device_Entry.params`` -- key for key without consulting a plugin schema, so no
    schema default is written in where the administrator omitted one (Requirement 5.8).
    """
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    if isinstance(value, Enum):
        # A `StrEnum` member is also a `str` and would serialize correctly on its own; going
        # through `.value` keeps that true for a plain `Enum` field added later.
        return _render(value.value)
    if isinstance(value, BaseModel):
        return _render_model(value)
    if isinstance(value, list | tuple):
        return [_render(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _render(item) for key, item in value.items()}
    return value
