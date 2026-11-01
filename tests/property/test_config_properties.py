"""Property 1: the Configuration_Printer and Configuration_Parser round trip (task 4.4).

*For any* valid Configuration_Object, printing it and parsing the result yields a Configuration_Object
that is field-by-field equal to the original -- the global poll interval, the log level, every MQTT
connection field *including secret values*, and the ordered list of Device_Entry values -- and produces
zero validation failures (Requirements 2.1, 2.8, 2.9).

## Why this module, and what else belongs in it

Properties 2, 3, and 4 (tasks 4.5, 4.6, 4.7) are statements about the same subject as this one:
``config/parser.py`` applied to a generated options document. They belong in this module rather than in
three further ones, because they reuse the machinery below -- the plugin schemas in ``SCHEMAS``, the
parameter and identifier generators, and :func:`captured_records` -- and differ only in which document
they build and what they assert about the outcome. The layout is therefore by concern rather than by
property:

1. **constants** -- the bounds and notable values every generator draws from;
2. **plugin schemas** -- the two schemas a generated ``Device_Entry`` may name;
3. **generators** -- valid whole configurations here; Properties 2 and 3 add device lists that
   interleave *invalid* entries, and Property 4 adds out-of-range global settings;
4. **the comparison** -- the field-by-field equality this property needs;
5. **the properties** -- one section per property, each carrying its own tag comment.

Only Property 1 needs a *valid* configuration in the strict sense. That is why the generators here are
built to stay inside every accepted bound, and why the assertions treat any rejection as a failure: for
Properties 2 through 4 the rejections are the subject, and those generators will deliberately step
outside the bounds this one respects.

## What "valid" is generated, and what is deliberately left out

A generated configuration is a whole document, not a single field: a global poll interval, a log level, a
complete MQTT section, and 1 to 6 Device_Entry values whose ``params`` satisfy the configuration schema
published by the plugin the entry names.

* **Identifiers are unique when compared without regard to letter case.** Requirement 2.5 excludes a
  later entry that repeats an earlier identifier, so a device list carrying a case-insensitive duplicate
  *cannot* round trip -- by design, not by defect. Generating one here would assert the opposite of what
  the parser is required to do; that case is Property 3's subject (task 4.6).
* **The device list is capped at 6 entries rather than the 50 Requirement 2.1 permits.** List length is
  not what this property is about, and the boundary that matters -- the fiftieth entry accepted, the
  fifty-first excluded one at a time -- is pinned by
  ``tests/unit/test_config_parser.py::test_surplus_devices_are_excluded_one_by_one``. Six keeps the
  generated document wide enough to catch an ordering defect and cheap enough to run at 150 examples.
* **A credential whose own text reads as a ``!secret`` reference is excluded.** The reference syntax has
  no escape, so the parser reads a printed literal of ``!secret hunter2`` as a reference again and
  resolves it against the store rather than taking it verbatim. That is a documented limitation of the
  syntax, pinned by
  ``tests/unit/test_config_printer.py::test_a_reference_shaped_credential_is_printed_verbatim``, and such
  a value is therefore not a *valid* input to this property. The credential alphabet excludes ``!``
  outright, and :func:`test_no_generated_credential_is_read_as_a_secret_reference` machine-checks that
  every notable credential is a literal -- including ``!secretive``, which begins with the tag and is
  still a literal because the tag is not followed by whitespace.
* **Non-finite floats are excluded from plugin parameters.** ``print_config`` deliberately keeps them
  printable, and a ``NaN`` does survive the text round trip -- but it is unequal to itself, so no
  equality could be asserted about it. The printer's own docstring is where that behaviour is stated.
* **An entry may name a plugin no schema was published for.** That is not an oversight: Requirement 3.4
  puts the rejection of an unknown plugin identifier in the Plugin_Registry, so the parser accepts such
  an entry with its ``params`` carried through unvalidated and unsubstituted. It is a valid parser
  outcome, and it exercises the branch where no credential field is known at all.

## Defaults are the subtle half of Requirement 2.9

A field holding its default value must be printed *explicitly* and come back identical. Nothing about
that is observable from a configuration in which every optional field happens to be set, so each example
draws a **regime** (:class:`Regime`) that fixes how optional fields are populated:

* ``defaults`` -- every optional field holds its declared default, and every optional plugin parameter is
  omitted from ``params`` altogether, so the plugin schema's own default would apply;
* ``explicit`` -- every optional field holds a value *different* from its default, and every optional
  plugin parameter is written out;
* ``mixed`` -- each field independently either holds its default or does not.

The regimes are what make the failure mode reachable. A printer that omitted default-valued fields would
still round trip today, because the parser applies the same defaults -- and would stop the moment a
default changed. So the printed text is inspected directly: every field the models declare must appear as
an explicit key in the document, which is asserted against ``model_fields`` rather than against a list
written here, so a field added to a model cannot quietly escape the check.

For ``params`` the distinction is sharper and does not depend on a default ever changing. The parser
carries ``params`` as the mapping it read, so an omitted optional parameter must come back *omitted*: the
Poll_Scheduler reads ``model_fields_set`` to tell an unset per-device request timeout from one explicitly
configured to the same value as the default (Requirement 5.8). Mapping equality covers both directions --
a key invented by a normalizing printer, and a key dropped by a lossy one.

## Comparing credentials means comparing plaintext, and that is legitimate here

``SecretStr`` hides its value from ``str`` and ``repr`` precisely so a credential cannot leak into a
record (Requirement 8.2), so a comparison that went through either would compare two identical masks and
prove nothing. Property 1 names "every MQTT connection field including secret values", so the values are
compared **revealed**, through :func:`revealed` and the ``SecretStr`` branch of :func:`comparable`.

This test is the one place where reading credential plaintext is the right thing to do, because emitting
resolved literal credentials is the Configuration_Printer's stated purpose (Requirement 2.8): a printer
that emitted the mask instead would produce a configuration that cannot authenticate, and would
round-trip the mask itself into a password. Every revealed value here is generated, belongs to no real
system, and is registered only with a :class:`CredentialRegistry` private to the example.

Revealed values are also **tagged** rather than compared bare. ``comparable`` renders a credential as
``("SecretStr", plaintext)`` and every leaf as ``(type name, value)``, so a credential that came back as
the plain string it was printed from is not equal to the ``SecretStr`` it started as, and a ``True`` that
came back as ``1`` is not equal to the boolean it started as. Untagged, Python's own coercions would let
both pass.

## Where the "never routed to a sink" half of Requirement 2.8 is checked

The printed text carries plaintext, so it must reach no log record (Requirement 8.2) and no MQTT payload
(Requirement 8.3). The logging half is asserted here, over every generated configuration: the whole round
trip runs with a handler attached to the root logger, and **no record at all** may be emitted -- which is
a stronger statement than "no record carries a credential", and needs no scan of record text to make.

The MQTT half is structural rather than observable: ``config/printer.py`` imports neither ``logging`` nor
an MQTT client, so neither sink is reachable from it and nothing is emitted as a side effect. That is a
property of the module's imports, checked by reading it, and restated in its docstring.

## Why the parse is asserted to have succeeded before anything is compared

Two empty configurations compare equal, and so do two configurations that lost the same entry. Every
example therefore asserts the parse succeeded *non-vacuously* before comparing anything: no fatal error,
no per-entry rejection, no rejected log level, no log record, and a restored device list of the same
non-zero length as the original. The secret store is empty on purpose, so a generated credential that the
parser tried to resolve as a reference fails loudly here instead of quietly resolving to something else.
"""

from __future__ import annotations

import json
import logging
import string
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from enum import Enum, StrEnum
from typing import Any, Final, NamedTuple

from hypothesis import event, given, settings
from hypothesis import strategies as st
from pydantic import BaseModel, Field, SecretStr

from remote_energy_meter.config.models import (
    CREDENTIAL_MARKER,
    DEFAULT_POLL_INTERVAL_S,
    MAX_DEVICE_ID_LENGTH,
    MAX_POLL_INTERVAL_S,
    MAX_PORT,
    MIN_POLL_INTERVAL_S,
    MIN_PORT,
    ConfigurationObject,
    DeviceEntry,
    LogLevel,
    MqttSettings,
)
from remote_energy_meter.config.parser import FatalReason, PluginConfigSchema, parse_options
from remote_energy_meter.config.printer import print_config
from remote_energy_meter.config.runtime import EntryRejection, RejectionReason
from remote_energy_meter.config.secrets import SECRET_TAG, SecretResolver, is_reference
from remote_energy_meter.diagnostics.logger import DEVICE_ID_FIELD
from remote_energy_meter.diagnostics.redaction import MAX_CREDENTIAL_LENGTH, CredentialRegistry
from remote_energy_meter.plugins.idrac.schema import (
    DEFAULT_POWER_PATH,
    DEFAULT_REQUEST_TIMEOUT_S,
    MAX_PASSWORD_LENGTH,
    MAX_POWER_PATH_LENGTH,
    MAX_REQUEST_TIMEOUT_S,
    MAX_USERNAME_LENGTH,
    MIN_REQUEST_TIMEOUT_S,
    IdracParams,
)

# --------------------------------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------------------------------

#: Where the parser writes the records of Requirements 2.3, 2.5, 2.7, and 2.10-2.12. Outside the
#: ``remote_energy_meter`` tree, so the package logger's own handlers are never involved.
LOGGER_NAME: Final = "tests.config_properties"

#: The bounds above are *imported* rather than written out, unlike the expectations in the other
#: property modules, and the distinction is deliberate. A generator has to produce values the published
#: schema accepts, and the schema is the contract it is generating against; a bound copied here would
#: drift from that contract and start producing entries the parser rightly rejects. The *expectation*
#: this property checks is equality, and equality is computed independently of both modules.

#: Requirement 2.12's accepted identifier character set, spelled as an alphabet rather than as the
#: pattern the model declares, because a generator needs characters to draw from.
DEVICE_ID_ALPHABET: Final = string.ascii_letters + string.digits + "-_"

#: Identifiers worth drawing on purpose: the one-character case, each permitted non-alphanumeric on its
#: own, a mixed-case identifier (the case Requirement 2.5 folds), and the 64-character boundary.
NOTABLE_DEVICE_IDS: Final[tuple[str, ...]] = (
    "a",
    "0",
    "_",
    "-",
    "poweredge_a",
    "PowerEdge-A",
    "x" * MAX_DEVICE_ID_LENGTH,
)

#: Longest device list a generated configuration carries. Well inside the 50 Requirement 2.1 permits --
#: see the module docstring on why the cap boundary is a unit test's job rather than this property's.
MAX_GENERATED_DEVICES: Final = 6

#: Characters a generated credential is built from. Three properties are load-bearing: no whitespace, so
#: a value is never blank once stripped and can never trip Requirement 8.5; no ``!``, so no value can be
#: read as a ``!secret`` reference (Requirement 8.4); and a handful of characters JSON must escape --
#: quote, backslash -- plus non-ASCII, so the printed text exercises escaping and ``ensure_ascii=False``
#: rather than only the easy subset.
CREDENTIAL_ALPHABET: Final = string.ascii_letters + string.digits + "-_.:@#$%^&*()+=[]{}|;<>,?/~" + '"' + "\\" + "éü密"

#: Credentials worth drawing on purpose. ``!secretive`` begins with the reference tag and is still a
#: literal, because the tag is not followed by whitespace -- the one case where "looks like a reference"
#: and "is a reference" part company. The padded value proves the parser does not trim what it stores.
NOTABLE_CREDENTIALS: Final[tuple[str, ...]] = (
    "hunter2",
    " padded credential ",
    f"{SECRET_TAG}ive",
    'quote"backslash\\',
    "ünïcodé-密码",
)

#: Longest MQTT broker credential generated. ``MqttSettings`` bounds neither field, and Requirement 8.8's
#: 256-character limit applies to plugin parameters, so this is a generation budget rather than a bound.
MAX_MQTT_CREDENTIAL_LENGTH: Final = 64

#: Broker ports worth drawing on purpose: both ends of Requirement 2.11's range and the two conventional
#: MQTT ports.
NOTABLE_PORTS: Final[tuple[int, ...]] = (MIN_PORT, 1883, 8883, MAX_PORT)

#: Poll intervals worth drawing on purpose: both ends of the accepted range and Requirement 2.6's default.
NOTABLE_INTERVALS: Final[tuple[int, ...]] = (MIN_POLL_INTERVAL_S, DEFAULT_POLL_INTERVAL_S, MAX_POLL_INTERVAL_S)

#: Hosts worth drawing on purpose: the iDRAC management addresses of the target environment, a hostname,
#: and an IPv6 literal, all of which ``IdracParams.host`` accepts as plain text.
NOTABLE_HOSTS: Final[tuple[str, ...]] = ("192.0.2.9", "192.0.2.20", "idrac-a.example", "::1")

#: Prefix every generated non-schema parameter key carries, so an extra key cannot collide with a field
#: of either published schema and turn into a validation failure.
EXTRA_KEY_PREFIX: Final = "x_"

#: The plugin identifiers a generated ``Device_Entry`` may name. ``UNLISTED_PLUGIN_ID`` is absent from
#: :data:`SCHEMAS` on purpose -- see the module docstring.
IDRAC_PLUGIN_ID: Final = "idrac"
PROBE_PLUGIN_ID: Final = "probe"
UNLISTED_PLUGIN_ID: Final = "not_discovered"

#: The fields the explicit comparison names, guarded against the models by
#: :func:`test_the_explicit_comparison_covers_every_declared_field` so a field added to a model cannot
#: slip past it while :func:`comparable` silently keeps passing.
COMPARED_CONFIG_FIELDS: Final[tuple[str, ...]] = ("poll_interval_s", "log_level", "mqtt", "devices")
COMPARED_MQTT_FIELDS: Final[tuple[str, ...]] = (
    "host",
    "port",
    "username",
    "password",
    "client_id",
    "base_topic",
    "discovery_prefix",
    "tls",
)
COMPARED_ENTRY_FIELDS: Final[tuple[str, ...]] = ("device_id", "plugin", "display_name", "poll_interval_s", "params")

#: Typed absences, so ``draw_optional`` solves its type variable to the optional field's own type.
NO_DISPLAY_NAME: Final[str | None] = None
NO_ENTRY_INTERVAL: Final[int | None] = None
NO_MQTT_CREDENTIAL: Final[SecretStr | None] = None


# --------------------------------------------------------------------------------------------------
# the plugin schemas a generated entry may name
# --------------------------------------------------------------------------------------------------


class ProbeParams(BaseModel):
    """A second published schema, so this property is about *any* plugin schema rather than iDRAC's.

    Shaped to cover what ``IdracParams`` cannot: an **optional** credential field, which the parser's
    empty-credential check (Requirement 8.5) must leave alone when it is absent or null, and which is
    the only way to generate a credential-marked field holding ``None``. ``retries`` is a non-credential
    optional field whose default is a number rather than a string or a boolean.

    The ``credential`` marker is imported rather than restated because it is the contract a plugin author
    signs (Requirement 8.1), not an expectation about behaviour: a schema declaring credentials under any
    other key would be invisible to the redaction registry, which is the point of having one marker.
    """

    endpoint: str = Field(min_length=1)
    token: SecretStr | None = Field(default=None, json_schema_extra={CREDENTIAL_MARKER: True})
    retries: int = Field(default=2, ge=0, le=9)


#: The schemas the parse runs against, keyed by plugin identifier exactly as the Plugin_Registry would
#: publish them.
SCHEMAS: Final[Mapping[str, PluginConfigSchema]] = {
    IDRAC_PLUGIN_ID: IdracParams,
    PROBE_PLUGIN_ID: ProbeParams,
}


class Regime(StrEnum):
    """How an example populates the fields that have a default (see the module docstring)."""

    DEFAULTS = "defaults"
    EXPLICIT = "explicit"
    MIXED = "mixed"


class Case(NamedTuple):
    """One generated configuration, with the regime it was drawn under for reporting."""

    config: ConfigurationObject
    regime: Regime


# --------------------------------------------------------------------------------------------------
# generators
# --------------------------------------------------------------------------------------------------


def draw_optional[T](draw: st.DrawFn, regime: Regime, default: T, other: st.SearchStrategy[T]) -> T:
    """Draw one optional model field according to ``regime``.

    ``other`` must not be able to produce ``default``, so that the ``explicit`` regime genuinely holds a
    non-default value and the two regimes stay distinguishable.
    """
    if regime is Regime.DEFAULTS:
        return default
    if regime is Regime.EXPLICIT:
        return draw(other)
    return draw(st.one_of(st.just(default), other))


def draw_presence(draw: st.DrawFn, regime: Regime) -> bool:
    """Whether one optional *plugin parameter* is written into ``params`` at all.

    Presence rather than value is what matters for a parameter: the parser carries ``params`` as read, so
    an omitted key must come back omitted for Requirement 5.8's ``model_fields_set`` distinction to
    survive.
    """
    if regime is Regime.DEFAULTS:
        return False
    if regime is Regime.EXPLICIT:
        return True
    return draw(st.booleans())


def credentials(max_size: int) -> st.SearchStrategy[str]:
    """A credential value the parser accepts: non-blank, at most ``max_size`` characters, never a reference.

    The length boundary is drawn explicitly, because a schema bound is where an off-by-one would show and
    uniform draws reach the maximum far too rarely.
    """
    return st.one_of(
        st.text(alphabet=CREDENTIAL_ALPHABET, min_size=1, max_size=max_size),
        st.sampled_from([value for value in NOTABLE_CREDENTIALS if len(value) <= max_size]),
        st.just("p" * max_size),
    )


def mqtt_credentials() -> st.SearchStrategy[SecretStr]:
    """A broker credential, including the empty string.

    ``MqttSettings`` bounds neither credential field, so an empty value is accepted -- and it is worth
    drawing, because empty text and an absent field are different configurations that a printer
    collapsing one onto the other would make indistinguishable.
    """
    return st.one_of(credentials(MAX_MQTT_CREDENTIAL_LENGTH), st.just("")).map(SecretStr)


def device_identifiers() -> st.SearchStrategy[str]:
    """One accepted device identifier (Requirement 2.12)."""
    return st.one_of(
        st.text(alphabet=DEVICE_ID_ALPHABET, min_size=1, max_size=12),
        st.sampled_from(NOTABLE_DEVICE_IDS),
    )


def entry_intervals() -> st.SearchStrategy[int]:
    """A per-device poll interval inside the 5-3600 second range Requirement 2.2 accepts."""
    return st.one_of(
        st.integers(min_value=MIN_POLL_INTERVAL_S, max_value=MAX_POLL_INTERVAL_S),
        st.sampled_from(NOTABLE_INTERVALS),
    )


def global_intervals() -> st.SearchStrategy[int]:
    """A global poll interval other than Requirement 2.6's default, for the ``explicit`` regime."""
    return st.one_of(
        st.integers(min_value=MIN_POLL_INTERVAL_S, max_value=MAX_POLL_INTERVAL_S),
        st.sampled_from(NOTABLE_INTERVALS),
    ).filter(lambda value: value != DEFAULT_POLL_INTERVAL_S)


def display_names() -> st.SearchStrategy[str]:
    """Any display name, including the empty string and non-ASCII text.

    Unconstrained by the models, and the only entry field free to carry text a device identifier cannot
    (Requirement 2.12 restricts identifiers to ``[A-Za-z0-9_-]``), so this is where the printed document
    gets its awkward characters.
    """
    return st.text(max_size=24)


def non_default_text(default: str) -> st.SearchStrategy[str]:
    """Any text other than ``default``, for an optional string field in the ``explicit`` regime."""
    return st.text(max_size=24).filter(lambda value: value != default)


def json_scalars() -> st.SearchStrategy[Any]:
    """A value the options format can express, for a non-schema plugin parameter.

    Non-finite floats are excluded: ``print_config`` keeps them printable and they do survive the text
    round trip, but a ``NaN`` is unequal to itself so no equality could be asserted about it.
    """
    return st.one_of(
        st.none(),
        st.booleans(),
        st.integers(min_value=-(10**6), max_value=10**6),
        st.floats(allow_nan=False, allow_infinity=False),
        st.text(max_size=12),
    )


def extra_params() -> st.SearchStrategy[dict[str, Any]]:
    """Parameters no published schema declares, which the parser must carry through untouched.

    Nested containers are included because the printer copies a mapping key for key rather than dumping a
    validated model, and the copy is recursive.
    """
    return st.dictionaries(
        keys=st.text(alphabet=string.ascii_lowercase + string.digits, min_size=1, max_size=6).map(
            lambda suffix: f"{EXTRA_KEY_PREFIX}{suffix}"
        ),
        values=st.one_of(
            json_scalars(),
            st.lists(json_scalars(), max_size=3),
            st.dictionaries(keys=st.text(max_size=4), values=json_scalars(), max_size=2),
        ),
        max_size=2,
    )


@st.composite
def idrac_params(draw: st.DrawFn, regime: Regime) -> dict[str, Any]:
    """``params`` satisfying the iDRAC schema, with its optional fields governed by ``regime``.

    ``request_timeout_s`` may be drawn as the schema default itself, which is the case where a normalizing
    printer would be undetectable by value alone: an explicitly configured 10 seconds and an omitted one
    resolve to the same number, and only the presence of the key tells them apart (Requirement 5.8).
    """
    params: dict[str, Any] = {
        "host": draw(st.one_of(st.text(min_size=1, max_size=24), st.sampled_from(NOTABLE_HOSTS))),
        "username": draw(credentials(MAX_USERNAME_LENGTH)),
        "password": draw(credentials(MAX_PASSWORD_LENGTH)),
    }
    if draw_presence(draw, regime):
        params["verify_tls"] = draw(st.booleans())
    if draw_presence(draw, regime):
        params["power_path"] = draw(
            st.one_of(
                st.none(),
                st.text(max_size=32),
                st.sampled_from([DEFAULT_POWER_PATH, "p" * MAX_POWER_PATH_LENGTH]),
            )
        )
    if draw_presence(draw, regime):
        params["request_timeout_s"] = draw(
            st.one_of(
                st.floats(
                    min_value=MIN_REQUEST_TIMEOUT_S,
                    max_value=MAX_REQUEST_TIMEOUT_S,
                    allow_nan=False,
                    allow_infinity=False,
                ),
                st.sampled_from([MIN_REQUEST_TIMEOUT_S, DEFAULT_REQUEST_TIMEOUT_S, MAX_REQUEST_TIMEOUT_S]),
            )
        )
    params.update(draw(extra_params()))
    return params


@st.composite
def probe_params(draw: st.DrawFn, regime: Regime) -> dict[str, Any]:
    """``params`` satisfying :class:`ProbeParams`, whose credential field is optional and may be null."""
    params: dict[str, Any] = {"endpoint": draw(st.text(min_size=1, max_size=24))}
    if draw_presence(draw, regime):
        params["token"] = draw(st.one_of(st.none(), credentials(MAX_MQTT_CREDENTIAL_LENGTH)))
    if draw_presence(draw, regime):
        params["retries"] = draw(st.integers(min_value=0, max_value=9))
    params.update(draw(extra_params()))
    return params


def unlisted_params() -> st.SearchStrategy[dict[str, Any]]:
    """``params`` for an entry naming a plugin no schema was published for.

    Nothing validates them and nothing substitutes into them (Requirement 3.4 defers the rejection to the
    Plugin_Registry), so they may be anything the options format can express, including empty.
    """
    return extra_params()


@st.composite
def device_entries(draw: st.DrawFn, regime: Regime, device_id: str) -> DeviceEntry:
    """One accepted ``Device_Entry`` carrying ``device_id``."""
    plugin = draw(st.sampled_from([IDRAC_PLUGIN_ID, PROBE_PLUGIN_ID, UNLISTED_PLUGIN_ID]))
    if plugin == IDRAC_PLUGIN_ID:
        params = draw(idrac_params(regime))
    elif plugin == PROBE_PLUGIN_ID:
        params = draw(probe_params(regime))
    else:
        params = draw(unlisted_params())

    return DeviceEntry(
        device_id=device_id,
        plugin=plugin,
        display_name=draw_optional(draw, regime, NO_DISPLAY_NAME, display_names()),
        poll_interval_s=draw_optional(draw, regime, NO_ENTRY_INTERVAL, entry_intervals()),
        params=params,
    )


@st.composite
def mqtt_sections(draw: st.DrawFn, regime: Regime) -> MqttSettings:
    """A complete MQTT connection section (Requirement 2.11), optional fields governed by ``regime``."""
    return MqttSettings(
        host=draw(st.one_of(st.text(min_size=1, max_size=24), st.just("core-mosquitto"))),
        port=draw(st.one_of(st.integers(min_value=MIN_PORT, max_value=MAX_PORT), st.sampled_from(NOTABLE_PORTS))),
        username=draw_optional(draw, regime, NO_MQTT_CREDENTIAL, mqtt_credentials()),
        password=draw_optional(draw, regime, NO_MQTT_CREDENTIAL, mqtt_credentials()),
        client_id=draw_optional(draw, regime, "remote_energy_meter", non_default_text("remote_energy_meter")),
        base_topic=draw_optional(draw, regime, "remote_energy_meter", non_default_text("remote_energy_meter")),
        discovery_prefix=draw_optional(draw, regime, "homeassistant", non_default_text("homeassistant")),
        tls=draw_optional(draw, regime, False, st.just(True)),
    )


@st.composite
def configurations(draw: st.DrawFn) -> Case:
    """A whole valid Configuration_Object, drawn under one regime.

    Identifiers are drawn case-insensitively unique because Requirement 2.5 is *required* to exclude a
    later duplicate, so a device list carrying one cannot round trip by design.
    """
    regime = draw(st.sampled_from(Regime))
    size = draw(st.integers(min_value=1, max_value=MAX_GENERATED_DEVICES))
    identifiers = draw(
        st.lists(device_identifiers(), min_size=size, max_size=size, unique_by=lambda value: value.lower())
    )

    return Case(
        config=ConfigurationObject(
            poll_interval_s=draw_optional(draw, regime, DEFAULT_POLL_INTERVAL_S, global_intervals()),
            log_level=draw_optional(
                draw,
                regime,
                LogLevel.INFO,
                st.sampled_from([LogLevel.DEBUG, LogLevel.WARNING, LogLevel.ERROR]),
            ),
            mqtt=draw(mqtt_sections(regime)),
            devices=[draw(device_entries(regime, device_id)) for device_id in identifiers],
        ),
        regime=regime,
    )


# --------------------------------------------------------------------------------------------------
# the comparison, and the log capture
# --------------------------------------------------------------------------------------------------


def revealed(value: SecretStr | None) -> str | None:
    """The plaintext behind a credential field, or ``None`` when the field is unset.

    Comparing masks would prove nothing -- every ``SecretStr`` renders the same one -- and Property 1 names
    secret values explicitly. This test is the one place where reading plaintext is right: emitting
    resolved literal credentials is the printer's stated purpose (Requirement 2.8). An unset field and an
    empty one stay distinguishable, ``None`` never being equal to ``""``.
    """
    return None if value is None else value.get_secret_value()


def comparable(value: object) -> Any:
    """A test-local normal form for field-by-field comparison, independent of the printer.

    Written here rather than borrowed from ``as_options_document`` on purpose: routing both sides of the
    comparison through the implementation would only establish that the printer agrees with itself.

    Every leaf is tagged with its type name, which is what makes the comparison stricter than ``==`` where
    Python's coercions would hide a change: ``True`` printed as ``1`` and read back as an integer, a float
    ``10.0`` read back as an integer ``10``, or a credential read back as the plain string it was printed
    from, all compare equal untagged and unequal here. Models are walked through ``model_fields``, so a
    field added to a model is compared without this function being touched.
    """
    if isinstance(value, SecretStr):
        return ("SecretStr", value.get_secret_value())
    if isinstance(value, Enum):
        return (type(value).__name__, value.value)
    if isinstance(value, BaseModel):
        return {name: comparable(getattr(value, name)) for name in type(value).model_fields}
    if isinstance(value, list | tuple):
        return [comparable(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): comparable(item) for key, item in value.items()}
    return (type(value).__name__, value)


class _ListHandler(logging.Handler):
    """Collects records instead of writing them, so a sink can be observed rather than read."""

    def __init__(self, records: list[logging.LogRecord]) -> None:
        super().__init__(level=logging.NOTSET)
        self._records = records

    def emit(self, record: logging.LogRecord) -> None:
        self._records.append(record)


@contextmanager
def captured_records() -> Iterator[list[logging.LogRecord]]:
    """Capture every record reaching the root logger for the duration of the block.

    The root logger rather than one named logger, because Requirement 8.2 is a statement about *any* sink:
    a printer that logged through a logger of its own would be caught here and missed by a narrower
    capture. The previous level is restored on the way out, so nothing leaks into the next example.
    """
    records: list[logging.LogRecord] = []
    handler = _ListHandler(records)
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        root.setLevel(previous_level)
        root.removeHandler(handler)


def assert_mqtt_fields_equal(restored: MqttSettings, original: MqttSettings) -> None:
    """Every MQTT connection field, named one at a time, credentials compared revealed."""
    assert restored.host == original.host
    assert restored.port == original.port
    assert revealed(restored.username) == revealed(original.username)
    assert revealed(restored.password) == revealed(original.password)
    assert restored.client_id == original.client_id
    assert restored.base_topic == original.base_topic
    assert restored.discovery_prefix == original.discovery_prefix
    assert restored.tls == original.tls


# --------------------------------------------------------------------------------------------------
# generator and comparison guards
# --------------------------------------------------------------------------------------------------


def test_the_explicit_comparison_covers_every_declared_field() -> None:
    """A field added to a model must be added to the explicit comparison as well.

    :func:`comparable` would keep passing on its own -- it walks ``model_fields`` -- but it would do so
    over a field no generator populates, which holds its default on both sides and proves nothing. Failing
    here forces the new field into the generators and into the named assertions deliberately.
    """
    assert tuple(ConfigurationObject.model_fields) == COMPARED_CONFIG_FIELDS
    assert tuple(MqttSettings.model_fields) == COMPARED_MQTT_FIELDS
    assert tuple(DeviceEntry.model_fields) == COMPARED_ENTRY_FIELDS


def test_no_generated_credential_is_read_as_a_secret_reference() -> None:
    """The generator invariant the whole property rests on, machine-checked rather than asserted in prose.

    A generated credential must be a literal and must be non-blank: a reference would be resolved against
    the empty store and rejected, and a blank value would be rejected under Requirement 8.5. Both would
    fail the property for the generator's reasons rather than the implementation's.
    """
    for value in NOTABLE_CREDENTIALS:
        assert not is_reference(value)
        assert value.strip()

    assert SECRET_TAG[0] not in CREDENTIAL_ALPHABET
    assert not any(character.isspace() for character in CREDENTIAL_ALPHABET)


# --------------------------------------------------------------------------------------------------
# Property 1: the configuration round trip
# --------------------------------------------------------------------------------------------------


# Feature: remote-energy-meter, Property 1: For any valid Configuration_Object, printing it and parsing
# the result yields a Configuration_Object that is field-by-field equal to the original (global poll
# interval, log level, every MQTT connection field including secret values, and the ordered list of
# Device_Entry values) and produces zero validation failures.
# Validates: Requirements 2.1, 2.8, 2.9
@settings(max_examples=150, deadline=None)
@given(case=configurations())
def test_configuration_survives_a_print_parse_round_trip(case: Case) -> None:
    """Requirements 2.1, 2.8, and 2.9, over generated whole configurations."""
    config = case.config
    event(f"regime: {case.regime.value}")
    event(f"devices: {len(config.devices)}")

    # Private to this example: registering generated credentials with the process-wide registry would
    # leave arbitrary text redacted from every later record in the session.
    registry = CredentialRegistry()
    log = logging.getLogger(LOGGER_NAME)

    with captured_records() as records:
        text = print_config(config)

        # --- Requirement 2.8: the printed text carries plaintext, so no sink may see it ---
        # No record at all, which is stronger than "no record carries a credential" and needs no scan of
        # record text. The MQTT half is structural: the printer imports no client, so no payload is
        # reachable from it.
        assert records == []

        # The store is empty on purpose. A printed credential the parser tried to resolve as a reference
        # fails the assertions below rather than quietly resolving to something else.
        outcome = parse_options(
            text,
            schemas=SCHEMAS,
            secrets=SecretResolver.from_mapping({}, registry=registry),
            logger=log,
            registry=registry,
        )

    # --- the parse succeeded, and non-vacuously (Requirement 2.8) ---
    # Two empty configurations compare equal, and so do two that lost the same entry, so what was
    # produced is established before anything is compared.
    assert outcome.fatal is None
    assert outcome.rejections == ()
    assert outcome.level_resolution.rejected_value is None
    assert [record.getMessage() for record in records] == []
    restored = outcome.config
    assert restored is not None
    assert len(restored.devices) == len(config.devices)
    assert len(restored.devices) >= 1

    # --- Requirement 2.9: every declared field is printed explicitly, defaults included ---
    # Read off the text rather than off the printer's own rendering, and keyed to ``model_fields`` rather
    # than to a list written here. A printer that omitted default-valued fields would still round trip
    # today, because the parser applies the same defaults, and would stop the moment a default changed.
    document: dict[str, Any] = json.loads(text)
    assert set(document) == set(ConfigurationObject.model_fields)
    assert set(document["mqtt"]) == set(MqttSettings.model_fields)
    for printed_entry in document["devices"]:
        assert set(printed_entry) == set(DeviceEntry.model_fields)

    # --- field by field, the fields Property 1 names (Requirements 2.1, 2.9) ---
    assert restored.poll_interval_s == config.poll_interval_s
    assert restored.log_level is config.log_level
    assert_mqtt_fields_equal(restored.mqtt, config.mqtt)

    # Order first and on its own: a list compared pairwise would report a reordering as a mismatch of
    # every field of two unrelated entries.
    assert [entry.device_id for entry in restored.devices] == [entry.device_id for entry in config.devices]
    for original, seen in zip(config.devices, restored.devices, strict=True):
        assert seen.device_id == original.device_id
        assert seen.plugin == original.plugin
        assert seen.display_name == original.display_name
        assert seen.poll_interval_s == original.poll_interval_s
        # Mapping equality covers both failure directions: a key a normalizing printer invented from a
        # schema default, and a key a lossy one dropped (Requirement 5.8).
        assert seen.params == original.params

    # --- and totally, over whatever the models declare ---
    # The tagged normal form, so a value that came back as a different type is not equal to what it
    # started as. See ``comparable``.
    assert comparable(restored) == comparable(config)

    # --- corollary: print-parse-print is a fixed point, so the first print already normalized ---
    assert print_config(restored) == text


# ==================================================================================================
# Property 2: entry acceptance
# ==================================================================================================
#
# The subject is the same as Property 1 -- ``config/parser.py`` over a generated options document --
# so this section lives here rather than in a module of its own, reusing the plugin schemas in
# ``SCHEMAS``, the valid-entry generators above, :func:`comparable`, and :func:`captured_records`, and
# adding only what this property needs: a device list that *interleaves* invalid entries among the
# valid ones, and the rejection each invalid entry must produce.
#
# Where Property 1 asserts a valid configuration round trips, Property 2 asserts the split: the
# accepted entries are exactly the valid ones in their original order, and every rejected entry yields
# exactly one error record naming its identifier and first failing field. Duplicate identifiers
# (Requirement 2.5) and out-of-range global settings (Requirements 2.7, 2.11) are deliberately kept
# out -- they are the subjects of Properties 3 and 4 (tasks 4.6, 4.7) -- so that a rejection here is
# only ever for this property's own reasons.


class Mutation(StrEnum):
    """One way to make a single ``Device_Entry`` invalid, spanning the reasons Property 2 covers.

    Each member introduces exactly one defect into an otherwise valid entry, so the parser's "first
    failing field" (Requirement 2.3) is unambiguous and the reported rejection is deterministic.
    """

    BAD_IDENTIFIER = "bad_identifier"
    """A malformed device identifier (Requirement 2.12): empty, over 64 characters, a character
    outside ``[A-Za-z0-9_-]``, or not a string. Rejected before any other field is examined."""

    MISSING_REQUIRED = "missing_required_field"
    """A required non-credential schema field is absent (Requirements 2.2, 2.3): the iDRAC host."""

    OUT_OF_RANGE = "out_of_range_value"
    """A schema field holds a value outside its declared range (Requirements 2.2, 2.3): the request
    timeout outside 1-60 seconds."""

    BAD_DEVICE_INTERVAL = "bad_device_interval"
    """A device-specific poll interval outside 5-3600 seconds (Requirements 2.2, 2.3), which is an
    envelope field rather than a plugin one."""

    EMPTY_CREDENTIAL = "empty_credential"
    """A required credential field is absent or holds only whitespace (Requirement 8.5)."""

    OVERSIZED_CREDENTIAL = "oversized_credential"
    """A credential field holds more than 256 characters (Requirement 8.8)."""

    UNRESOLVED_SECRET = "unresolved_secret"
    """A credential field names a ``!secret`` reference the store cannot resolve (Requirement 8.6)."""


class Acceptance(NamedTuple):
    """One generated options document and the outcome the parser must produce for it."""

    text: str
    accepted: list[DeviceEntry]
    rejections: list[EntryRejection]
    regime: Regime
    mutations: tuple[Mutation, ...]


def malformed_identifiers() -> st.SearchStrategy[Any]:
    """A device identifier the parser must reject under Requirement 2.12.

    Every branch violates the accepted form -- empty, over the 64-character bound, a character outside
    ``[A-Za-z0-9_-]``, or not a string at all -- so none can be confused with a well-formed identifier,
    and a malformed entry therefore never claims an identifier another entry needs.
    """
    return st.one_of(
        st.just(""),
        st.text(alphabet=DEVICE_ID_ALPHABET, min_size=MAX_DEVICE_ID_LENGTH + 1, max_size=MAX_DEVICE_ID_LENGTH + 8),
        st.sampled_from(["has space", "bad!char", "slash/here", "dot.dot", "café", "emoji😀"]),
        st.integers(),
        st.booleans(),
        st.none(),
        st.lists(st.integers(), max_size=2),
    )


@st.composite
def valid_idrac_params(draw: st.DrawFn) -> dict[str, Any]:
    """A minimal iDRAC ``params`` mapping the schema accepts, to mutate into an invalid one.

    Only the required fields, each valid: a non-empty host and literal, non-blank credentials inside
    the schema's own length bounds and Requirement 8.8's 256-character limit. A single mutation applied
    to this base is then the entry's only defect, which is what makes the rejection deterministic.
    """
    return {
        "host": draw(st.one_of(st.text(min_size=1, max_size=24), st.sampled_from(NOTABLE_HOSTS))),
        "username": draw(credentials(MAX_USERNAME_LENGTH)),
        "password": draw(credentials(MAX_PASSWORD_LENGTH)),
    }


@st.composite
def invalid_entry(draw: st.DrawFn, mutation: Mutation, device_id: str | None) -> tuple[dict[str, Any], EntryRejection]:
    """One raw device-list element the parser must reject, with the rejection it must report.

    ``device_id`` is a well-formed, list-unique identifier for every mutation except
    :attr:`Mutation.BAD_IDENTIFIER`, which supplies its own malformed one and so needs none.
    """
    raw: dict[str, Any]

    if mutation is Mutation.BAD_IDENTIFIER:
        bad = draw(malformed_identifiers())
        raw = {"plugin": IDRAC_PLUGIN_ID, "params": draw(valid_idrac_params()), "device_id": bad}
        expected_id = bad if isinstance(bad, str) else None
        return raw, EntryRejection(
            device_id=expected_id, reason=RejectionReason.INVALID_IDENTIFIER, field_name="device_id"
        )

    assert device_id is not None  # supplied for every identifier-bearing mutation
    params = draw(valid_idrac_params())
    poll_interval_s: int | None = None
    reason: RejectionReason
    field_name: str

    if mutation is Mutation.MISSING_REQUIRED:
        del params["host"]
        reason, field_name = RejectionReason.SCHEMA_VIOLATION, "params.host"
    elif mutation is Mutation.OUT_OF_RANGE:
        params["request_timeout_s"] = draw(st.sampled_from([0, -5, 61, 999, 3601]))
        reason, field_name = RejectionReason.SCHEMA_VIOLATION, "params.request_timeout_s"
    elif mutation is Mutation.BAD_DEVICE_INTERVAL:
        poll_interval_s = draw(st.sampled_from([-1, 0, 1, 4, 3601, 10000]))
        reason, field_name = RejectionReason.SCHEMA_VIOLATION, "poll_interval_s"
    elif mutation is Mutation.EMPTY_CREDENTIAL:
        cred = draw(st.sampled_from(["username", "password"]))
        if draw(st.booleans()):
            params.pop(cred, None)  # absent
        else:
            params[cred] = draw(st.sampled_from(["", "   ", "\t", "\n ", "  \t  "]))  # whitespace only
        reason, field_name = RejectionReason.EMPTY_CREDENTIAL, f"params.{cred}"
    elif mutation is Mutation.OVERSIZED_CREDENTIAL:
        cred = draw(st.sampled_from(["username", "password"]))
        length = draw(st.integers(min_value=MAX_CREDENTIAL_LENGTH + 1, max_value=MAX_CREDENTIAL_LENGTH + 40))
        params[cred] = "p" * length
        reason, field_name = RejectionReason.OVERSIZED_CREDENTIAL, f"params.{cred}"
    else:  # Mutation.UNRESOLVED_SECRET
        cred = draw(st.sampled_from(["username", "password"]))
        name = draw(st.text(alphabet=string.ascii_letters + string.digits + "_", min_size=1, max_size=20))
        params[cred] = f"{SECRET_TAG} {name}"
        reason, field_name = RejectionReason.UNRESOLVED_SECRET, f"params.{cred}"

    raw = {"device_id": device_id, "plugin": IDRAC_PLUGIN_ID, "params": params}
    if poll_interval_s is not None:
        raw["poll_interval_s"] = poll_interval_s
    return raw, EntryRejection(device_id=device_id, reason=reason, field_name=field_name)


@st.composite
def acceptance_cases(draw: st.DrawFn) -> Acceptance:
    """A whole options document interleaving accepted entries with rejected ones.

    At least one valid and one invalid entry are always present, so the split this property is about is
    exercised on every example. Identifiers are drawn case-insensitively unique so Requirement 2.5's
    duplicate path -- Property 3's subject -- never fires here.
    """
    regime = draw(st.sampled_from(Regime))
    num_valid = draw(st.integers(min_value=1, max_value=3))
    num_invalid = draw(st.integers(min_value=1, max_value=3))
    slots = draw(st.permutations([False] * num_valid + [True] * num_invalid))

    # A unique identifier pool for every entry that reaches the identifier check well formed: the
    # accepted entries and every rejected entry other than a malformed-identifier one, all of which
    # *claim* their identifier even when later excluded (Requirement 2.5 compares declared identifiers).
    ids = draw(
        st.lists(
            device_identifiers(),
            min_size=num_valid + num_invalid,
            max_size=num_valid + num_invalid,
            unique_by=lambda value: value.lower(),
        )
    )
    id_iter = iter(ids)

    valid_entries: list[DeviceEntry] = []
    invalid_raws: list[dict[str, Any]] = []
    rejections: list[EntryRejection] = []
    mutations: list[Mutation] = []

    for is_invalid in slots:
        if not is_invalid:
            valid_entries.append(draw(device_entries(regime, next(id_iter))))
            continue
        mutation = draw(st.sampled_from(list(Mutation)))
        mutations.append(mutation)
        device_id = None if mutation is Mutation.BAD_IDENTIFIER else next(id_iter)
        raw, rejection = draw(invalid_entry(mutation, device_id))
        invalid_raws.append(raw)
        rejections.append(rejection)

    config_valid = ConfigurationObject(
        poll_interval_s=draw_optional(draw, regime, DEFAULT_POLL_INTERVAL_S, global_intervals()),
        log_level=draw_optional(
            draw, regime, LogLevel.INFO, st.sampled_from([LogLevel.DEBUG, LogLevel.WARNING, LogLevel.ERROR])
        ),
        mqtt=draw(mqtt_sections(regime)),
        devices=valid_entries,
    )

    # The accepted entries are rendered by the Configuration_Printer, whose output Property 1 has
    # already shown the parser reproduces field for field; only the rejected entries are hand built,
    # then spliced back into the printed device list at their original positions.
    document: dict[str, Any] = json.loads(print_config(config_valid))
    rendered_valid = iter(document["devices"])
    invalid = iter(invalid_raws)
    document["devices"] = [next(invalid) if is_invalid else next(rendered_valid) for is_invalid in slots]

    return Acceptance(
        text=json.dumps(document, ensure_ascii=False),
        accepted=valid_entries,
        rejections=rejections,
        regime=regime,
        mutations=tuple(mutations),
    )


# Feature: remote-energy-meter, Property 2: Accepted entries are exactly the valid entries, in order
#
# For any device list interleaving valid Device_Entry values with entries carrying a schema violation,
# a malformed device identifier, an absent or whitespace-only credential, a credential longer than 256
# characters, or an unresolvable secret reference, the accepted entries are exactly the valid entries in
# their original order, and exactly one error record is emitted per rejected entry naming that entry's
# identifier and its first failing field.
# Validates: Requirements 2.2, 2.3, 2.12, 8.5, 8.6, 8.8
@settings(max_examples=150, deadline=None)
@given(case=acceptance_cases())
def test_accepted_entries_are_exactly_the_valid_entries_in_order(case: Acceptance) -> None:
    """Requirements 2.2, 2.3, 2.12, 8.5, 8.6, and 8.8, over device lists mixing valid and invalid entries."""
    event(f"regime: {case.regime.value}")
    event(f"accepted: {len(case.accepted)}")
    event(f"rejected: {len(case.rejections)}")
    for mutation in case.mutations:
        event(f"mutation: {mutation.value}")

    # Private to this example, so registering the generated credentials of accepted entries does not
    # leave arbitrary text redacted from every later record in the session.
    registry = CredentialRegistry()
    log = logging.getLogger(LOGGER_NAME)

    with captured_records() as records:
        outcome = parse_options(
            case.text,
            schemas=SCHEMAS,
            secrets=SecretResolver.from_mapping({}, registry=registry),
            logger=log,
            registry=registry,
        )

    # At least one entry is valid, so the configuration is not rejected outright and a
    # Configuration_Object exists to compare against.
    assert outcome.fatal is None
    restored = outcome.config
    assert restored is not None

    # --- the accepted entries are exactly the valid ones, in their original order ---
    # Order is asserted on its own first: a list compared pairwise would report a single reordering as a
    # mismatch of every field of two unrelated entries. A rejection in the middle must not shift the
    # survivors (Requirements 2.2, 2.3, 8.5, 8.6, 8.8).
    assert [entry.device_id for entry in restored.devices] == [entry.device_id for entry in case.accepted]
    # Then field by field, in the tagged normal form Property 1 uses, so an entry that came back altered
    # is caught rather than passing on a type coercion.
    assert [comparable(entry) for entry in restored.devices] == [comparable(entry) for entry in case.accepted]

    # --- one rejection per excluded entry, in device-list order, naming id and first failing field ---
    # The structured rejection carries the identifier and the field Requirements 2.3 and 2.12 require the
    # record to name; asserting it here is the "naming" half of the property.
    assert list(outcome.rejections) == case.rejections

    # --- exactly one error record per rejected entry, and no others ---
    assert len(records) == len(case.rejections)
    for record, rejection in zip(records, case.rejections, strict=True):
        assert record.levelno == logging.ERROR
        if rejection.reason is RejectionReason.INVALID_IDENTIFIER:
            # A malformed identifier cannot be named as a device_id, so the record names the rejected
            # value and the accepted form instead (Requirement 2.12).
            assert "rejected_identifier" in record.__dict__
        else:
            assert record.__dict__.get(DEVICE_ID_FIELD) == rejection.device_id
            assert record.__dict__.get("field") == rejection.field_name


# ==================================================================================================
# Property 3: duplicate identifier resolution
# ==================================================================================================
#
# Still the same subject -- ``config/parser.py`` over a printed options document -- so this section
# reuses the valid-entry generators, ``SCHEMAS``, :func:`comparable`, and :func:`captured_records`
# above, and adds only what a duplicate needs: a way to recase an identifier so a second entry
# repeats it without regard to letter case, and a device list built to carry such repeats.
#
# Where Property 2 keeps identifiers case-insensitively unique so a rejection there is only ever for
# a schema, credential, or identifier defect, this property makes the collision the whole point.
# Every entry generated here is otherwise valid, so the only reason any entry is excluded is that an
# earlier entry already claimed its folded identifier (Requirement 2.5). That isolation is
# deliberate: an accepted entry is the first occurrence of its fold and nothing else, and a rejected
# one is a later duplicate and nothing else.
#
# A whole configuration is built from valid ``Device_Entry`` values -- some sharing a case-folded
# identifier -- and printed, because ``ConfigurationObject`` imposes no uniqueness of its own and the
# printer renders the device list verbatim; the parser is what must resolve the duplicates. The
# expectation is derived by the same first-occurrence walk the parser performs, over the same order,
# so the two cannot drift: whichever spelling appears first in the printed list is the survivor,
# every later spelling that folds onto it is dropped, and each drop owes exactly one error record
# naming the dropped identifier and the earlier occurrence it resolved to.


class DuplicateCase(NamedTuple):
    """One printed options document whose device list repeats identifiers under case folding.

    Attributes:
        text: The printed Addon_Configuration, ready to parse.
        accepted: The entries that must survive -- the first occurrence of each case-folded
            identifier, in their original device-list order.
        rejections: One :class:`EntryRejection` per dropped duplicate, in device-list order, each
            naming the duplicate's own declared identifier.
        retained_ids: For each rejection, the identifier of the earlier entry it duplicates -- the
            survivor the parser reports as ``duplicate_of``, so "resolve to the first occurrence" is
            checked rather than only "a duplicate is dropped". Aligned element for element with
            :attr:`rejections`.
        regime: The regime every entry was drawn under, for reporting.
    """

    text: str
    accepted: list[DeviceEntry]
    rejections: list[EntryRejection]
    retained_ids: list[str]
    regime: Regime


@st.composite
def duplicate_identifier(draw: st.DrawFn, base: str) -> str:
    """An identifier equal to ``base`` under case folding, its letters recased so the repeat is genuine.

    Draws among ``base`` verbatim -- an identical repeat is a duplicate too -- its upper, lower, and
    swapped-case forms, and an independent per-letter recasing. Digits, hyphen, and underscore are
    unaffected by case, so every form folds back to ``base.lower()``; a ``base`` with no letters can
    only reproduce itself, which is still a valid case-insensitive duplicate.
    """
    per_letter = "".join(
        draw(st.sampled_from([character.lower(), character.upper()])) if character.isalpha() else character
        for character in base
    )
    return draw(st.sampled_from([base, base.upper(), base.lower(), base.swapcase(), per_letter]))


@st.composite
def duplicate_cases(draw: st.DrawFn) -> DuplicateCase:
    """A whole options document whose device list repeats identifiers under case folding.

    Distinct case-folded identifiers seed the groups; each group carries its seed entry plus zero or
    more duplicates that recase the seed. At least one duplicate is always present, so the property's
    subject is exercised on every example, and the whole list is shuffled so the surviving spelling is
    not always the seed -- first-occurrence-wins must hold whichever form lands first.
    """
    regime = draw(st.sampled_from(Regime))

    num_groups = draw(st.integers(min_value=1, max_value=4))
    seeds = draw(
        st.lists(
            device_identifiers(),
            min_size=num_groups,
            max_size=num_groups,
            unique_by=lambda value: value.lower(),
        )
    )

    dup_counts = draw(st.lists(st.integers(min_value=0, max_value=3), min_size=num_groups, max_size=num_groups))
    if sum(dup_counts) == 0:
        # Guarantee the subject: without a duplicate this would be an ordinary Property 1 configuration.
        dup_counts[draw(st.integers(min_value=0, max_value=num_groups - 1))] += 1

    entries: list[DeviceEntry] = []
    for seed, count in zip(seeds, dup_counts, strict=True):
        entries.append(draw(device_entries(regime, seed)))
        for _ in range(count):
            entries.append(draw(device_entries(regime, draw(duplicate_identifier(seed)))))

    ordered: list[DeviceEntry] = draw(st.permutations(entries))

    # The parser's own resolution, computed here over the same order so the two cannot drift: the
    # first spelling of each fold is kept, and every later one is dropped naming the survivor.
    accepted: list[DeviceEntry] = []
    rejections: list[EntryRejection] = []
    retained_ids: list[str] = []
    survivor: dict[str, str] = {}
    for entry in ordered:
        folded = entry.device_id.lower()
        if folded in survivor:
            rejections.append(
                EntryRejection(
                    device_id=entry.device_id,
                    reason=RejectionReason.DUPLICATE_IDENTIFIER,
                    field_name="device_id",
                )
            )
            retained_ids.append(survivor[folded])
        else:
            survivor[folded] = entry.device_id
            accepted.append(entry)

    config = ConfigurationObject(
        poll_interval_s=draw_optional(draw, regime, DEFAULT_POLL_INTERVAL_S, global_intervals()),
        log_level=draw_optional(
            draw, regime, LogLevel.INFO, st.sampled_from([LogLevel.DEBUG, LogLevel.WARNING, LogLevel.ERROR])
        ),
        mqtt=draw(mqtt_sections(regime)),
        devices=list(ordered),
    )

    return DuplicateCase(
        text=print_config(config),
        accepted=accepted,
        rejections=rejections,
        retained_ids=retained_ids,
        regime=regime,
    )


# Feature: remote-energy-meter, Property 3: Duplicate identifiers resolve to the first occurrence
#
# For any device list containing identifiers that are equal when compared without regard to letter
# case, exactly the first occurrence of each case-folded identifier is accepted, every later
# occurrence is excluded in place, and exactly one error record naming that identifier is emitted.
# Validates: Requirements 2.5
@settings(max_examples=150, deadline=None)
@given(case=duplicate_cases())
def test_duplicate_identifiers_resolve_to_the_first_occurrence(case: DuplicateCase) -> None:
    """Requirement 2.5, over device lists that repeat identifiers under case folding."""
    event(f"regime: {case.regime.value}")
    event(f"accepted: {len(case.accepted)}")
    event(f"duplicates: {len(case.rejections)}")

    # Private to this example, so registering the accepted entries' credentials does not leave
    # arbitrary text redacted from every later record in the session.
    registry = CredentialRegistry()
    log = logging.getLogger(LOGGER_NAME)

    with captured_records() as records:
        outcome = parse_options(
            case.text,
            schemas=SCHEMAS,
            secrets=SecretResolver.from_mapping({}, registry=registry),
            logger=log,
            registry=registry,
        )

    # At least one entry survives -- the first occurrence of every fold is valid -- so a
    # Configuration_Object exists and the list was not rejected outright.
    assert outcome.fatal is None
    restored = outcome.config
    assert restored is not None

    # --- exactly the first occurrence of each fold survives, in the original order ---
    # Order on its own first: a duplicate excluded from the middle must not shift the survivors.
    assert [entry.device_id for entry in restored.devices] == [entry.device_id for entry in case.accepted]
    # Then field by field, in the tagged normal form Property 1 uses, so a survivor that came back
    # altered is caught rather than passing on a coercion.
    assert [comparable(entry) for entry in restored.devices] == [comparable(entry) for entry in case.accepted]

    # --- one rejection per later duplicate, in device-list order, each naming its own identifier ---
    assert list(outcome.rejections) == case.rejections

    # --- exactly one error record per dropped duplicate, and no others ---
    # The count alone rules out a second record for a duplicate and any stray record for a survivor;
    # each record then names the dropped identifier and the earlier occurrence it resolved to, which
    # is the "first occurrence is kept" half of Requirement 2.5.
    assert len(records) == len(case.rejections)
    for record, rejection, retained in zip(records, case.rejections, case.retained_ids, strict=True):
        assert record.levelno == logging.ERROR
        assert record.__dict__.get(DEVICE_ID_FIELD) == rejection.device_id
        assert record.__dict__.get("duplicate_of") == retained


# ==================================================================================================
# Property 4: global setting rejection
# ==================================================================================================
#
# The same subject once more -- ``config/parser.py`` over a printed options document -- so this
# section reuses ``SCHEMAS``, the valid-entry and MQTT-section generators, :func:`captured_records`,
# and the per-example ``CredentialRegistry`` above, and adds only what this property needs: global
# settings drawn from just inside and just outside their accepted ranges, and the single defect an
# example introduces into an otherwise wholly valid document.
#
# Properties 2 and 3 are about the *per-entry* severity: one bad entry is excluded, every other entry
# is still metered. This property is about the other one. A global setting belongs to no single entry,
# so Requirements 2.7 and 2.11 reject the whole Addon_Configuration for it: no Configuration_Object at
# all, and a fatal error naming the offending global field for the caller to report before it exits
# non-zero. Confusing the two severities is the failure mode this property exists to catch -- a parser
# that dropped a Device_Entry, or quietly substituted a default, for a global setting it could not
# accept would satisfy Properties 1 through 3 and leave the addon metering the wrong thing or nothing.
#
# Every example therefore starts from a document the Configuration_Printer produced from generated
# valid entries -- output Property 1 has already shown the parser reproduces field for field -- with
# the global interval and the broker port overwritten by values drawn from *inside* their accepted
# ranges, both ends included, because a bound is where an off-by-one shows. At most one global is then
# put out of range, so the fatal error the parse must report is unambiguous, and the device list stays
# valid from end to end so a per-entry rejection can never be mistaken for the global one.
#
# The negative half is that same document with no defect at all, drawn as often as any defect. Without
# it the property would be satisfied by a parser that rejects every configuration, and "out of range"
# would be carrying no weight.

#: Global poll intervals drawn on purpose from just inside Requirement 2.7's range: one step in from
#: each end, and both ends written as whole-numbered floats -- ``5.0`` is a whole number of seconds
#: however it was written. Both ends themselves are already in :data:`NOTABLE_INTERVALS`, which this
#: property draws from as well.
INSIDE_EDGE_INTERVALS: Final[tuple[int | float, ...]] = (
    MIN_POLL_INTERVAL_S + 1,
    MAX_POLL_INTERVAL_S - 1,
    float(MIN_POLL_INTERVAL_S),
    float(MAX_POLL_INTERVAL_S),
)

#: Global poll intervals Requirement 2.7 rejects: one step outside each end, a fractional value just
#: below the low end, just above it, and just above the high end, and two values that are no number of
#: seconds at all -- a numeric string and a boolean, either of which would have to be guessed at to be
#: read as an interval.
REJECTED_INTERVALS: Final[tuple[Any, ...]] = (
    MIN_POLL_INTERVAL_S - 1,
    0,
    -1,
    MAX_POLL_INTERVAL_S + 1,
    10 * MAX_POLL_INTERVAL_S,
    MIN_POLL_INTERVAL_S - 0.5,
    MIN_POLL_INTERVAL_S + 0.5,
    MAX_POLL_INTERVAL_S + 0.5,
    "30",
    True,
)

#: Broker ports drawn on purpose from just inside Requirement 2.11's range: one step in from each end.
#: Both ends themselves are already in :data:`NOTABLE_PORTS`.
INSIDE_EDGE_PORTS: Final[tuple[int, ...]] = (MIN_PORT + 1, MAX_PORT - 1)

#: Broker ports Requirement 2.11 rejects: one step outside each end, a value far outside, and a
#: fractional one. Text is deliberately absent: ``"1883"`` is read as the number it spells and is
#: therefore an *accepted* port rather than an offending one, which is a statement about the options
#: format rather than about this range.
REJECTED_PORTS: Final[tuple[Any, ...]] = (MIN_PORT - 1, -1, MAX_PORT + 1, 10 * MAX_PORT, 1883.5)


class GlobalDefect(StrEnum):
    """Which global setting an example puts out of range, if any."""

    NONE = "none"
    """No defect: every global sits inside its accepted range and the configuration must be accepted."""

    INTERVAL = "global_poll_interval"
    """The global poll interval is not a whole number of seconds, or is outside 5-3600 (Req. 2.7)."""

    MQTT_HOST = "mqtt_host"
    """The broker host address is not supplied (Requirement 2.11)."""

    MQTT_PORT = "mqtt_port"
    """The broker port is not supplied, or is outside 1-65535 (Requirement 2.11)."""


class Omission(StrEnum):
    """How a required global value fails to be supplied."""

    ABSENT = "absent"
    """The key is not written in the document at all."""

    NULL = "null"
    """The key is written as ``null``, which is how Supervisor writes an option left unset."""

    EMPTY = "empty"
    """The key holds text of zero length, which supplies no host address."""


class GlobalCase(NamedTuple):
    """One printed options document carrying at most one out-of-range global setting.

    Attributes:
        text: The document to parse, printed from a wholly valid configuration and then mutated in at
            most one global field.
        defect: Which global was put out of range, :attr:`GlobalDefect.NONE` for the negative half.
        fatal: The classification and the offending field name the parse must report, or ``None`` when
            the configuration must be accepted. Read off the defect the generator introduced, so the
            expectation is never the parser's own opinion of what it did.
        accepted_globals: The global poll interval and broker port the Configuration_Object must carry
            when the document is accepted; ``None`` when it must be rejected.
        devices: How many (valid) entries the device list carries, all of which must survive.
        regime: The regime the configuration was drawn under, for reporting.
    """

    text: str
    defect: GlobalDefect
    fatal: tuple[FatalReason, str] | None
    accepted_globals: tuple[int, int] | None
    devices: int
    regime: Regime


def accepted_intervals() -> st.SearchStrategy[int | float]:
    """A global poll interval Requirement 2.7 accepts, with the bounds drawn on purpose."""
    return st.one_of(
        st.integers(min_value=MIN_POLL_INTERVAL_S, max_value=MAX_POLL_INTERVAL_S),
        st.sampled_from(NOTABLE_INTERVALS),
        st.sampled_from(INSIDE_EDGE_INTERVALS),
    )


def rejected_intervals() -> st.SearchStrategy[Any]:
    """A global poll interval Requirement 2.7 rejects, unbounded on both sides of the range."""
    return st.one_of(
        st.sampled_from(REJECTED_INTERVALS),
        st.integers(max_value=MIN_POLL_INTERVAL_S - 1),
        st.integers(min_value=MAX_POLL_INTERVAL_S + 1),
    )


def accepted_ports() -> st.SearchStrategy[int]:
    """A broker port Requirement 2.11 accepts, with the bounds drawn on purpose."""
    return st.one_of(
        st.integers(min_value=MIN_PORT, max_value=MAX_PORT),
        st.sampled_from(NOTABLE_PORTS),
        st.sampled_from(INSIDE_EDGE_PORTS),
    )


def rejected_ports() -> st.SearchStrategy[Any]:
    """A broker port Requirement 2.11 rejects, unbounded on both sides of the range."""
    return st.one_of(
        st.sampled_from(REJECTED_PORTS),
        st.integers(max_value=MIN_PORT - 1),
        st.integers(min_value=MAX_PORT + 1),
    )


def apply_omission(section: dict[str, Any], name: str, omission: Omission) -> None:
    """Stop ``section`` from supplying a value under ``name``, in the way ``omission`` names."""
    if omission is Omission.ABSENT:
        del section[name]
    elif omission is Omission.NULL:
        section[name] = None
    else:
        section[name] = ""


@st.composite
def global_setting_cases(draw: st.DrawFn) -> GlobalCase:
    """A printed options document with at most one global setting the parser must not accept.

    The device list is valid throughout and the globals that are not the subject are drawn from inside
    their accepted ranges, so whatever the parse reports is attributable to the one defect the example
    introduced -- or, for :attr:`GlobalDefect.NONE`, to nothing at all.
    """
    regime = draw(st.sampled_from(Regime))
    size = draw(st.integers(min_value=1, max_value=3))
    identifiers = draw(
        st.lists(device_identifiers(), min_size=size, max_size=size, unique_by=lambda value: value.lower())
    )
    valid = ConfigurationObject(
        poll_interval_s=draw_optional(draw, regime, DEFAULT_POLL_INTERVAL_S, global_intervals()),
        log_level=draw_optional(
            draw, regime, LogLevel.INFO, st.sampled_from([LogLevel.DEBUG, LogLevel.WARNING, LogLevel.ERROR])
        ),
        mqtt=draw(mqtt_sections(regime)),
        devices=[draw(device_entries(regime, device_id)) for device_id in identifiers],
    )

    # Printed rather than hand built, so nothing but the globals overwritten below can be at fault.
    document: dict[str, Any] = json.loads(print_config(valid))

    # Both globals start inside their accepted ranges, the bounds among the values drawn.
    interval = draw(accepted_intervals())
    port = draw(accepted_ports())
    document["poll_interval_s"] = interval
    document["mqtt"]["port"] = port

    defect = draw(st.sampled_from(GlobalDefect))
    fatal: tuple[FatalReason, str] | None = None

    if defect is GlobalDefect.INTERVAL:
        document["poll_interval_s"] = draw(rejected_intervals())
        fatal = (FatalReason.INVALID_GLOBAL_INTERVAL, "poll_interval_s")
    elif defect is GlobalDefect.MQTT_HOST:
        # All three omissions are "omits the broker host address" in Requirement 2.11's terms: no key,
        # an explicit null, and text of zero length all supply no address to connect to.
        apply_omission(document["mqtt"], "host", draw(st.sampled_from(Omission)))
        fatal = (FatalReason.INVALID_MQTT_SETTINGS, "mqtt.host")
    elif defect is GlobalDefect.MQTT_PORT:
        # Requirement 2.11 names two port defects -- omitted, and outside 1-65535 -- and both take this
        # path. Zero-length text is not drawn here: it is a port that cannot be read rather than one
        # that was left unset, and the requirement's own two conditions are what this property is about.
        if draw(st.booleans()):
            apply_omission(document["mqtt"], "port", draw(st.sampled_from([Omission.ABSENT, Omission.NULL])))
        else:
            document["mqtt"]["port"] = draw(rejected_ports())
        fatal = (FatalReason.INVALID_MQTT_SETTINGS, "mqtt.port")

    return GlobalCase(
        text=json.dumps(document, ensure_ascii=False),
        defect=defect,
        fatal=fatal,
        accepted_globals=None if fatal is not None else (int(interval), port),
        devices=len(identifiers),
        regime=regime,
    )


def test_the_generated_global_settings_sit_where_this_property_says_they_do() -> None:
    """The inside-versus-outside labelling the fatal expectation is read off, machine-checked.

    The expectation each example carries comes from *which* global the generator put out of range, so a
    sample that is not where its name claims would assert the opposite of Requirements 2.7 and 2.11 and
    still pass. The bounds themselves are pinned to the numbers those requirements name, so a drifted
    constant is caught here rather than quietly redefining the property.
    """
    assert (MIN_POLL_INTERVAL_S, MAX_POLL_INTERVAL_S) == (5, 3600)
    assert (MIN_PORT, MAX_PORT) == (1, 65535)

    for interval in NOTABLE_INTERVALS + INSIDE_EDGE_INTERVALS:
        assert float(interval).is_integer()
        assert MIN_POLL_INTERVAL_S <= interval <= MAX_POLL_INTERVAL_S

    for candidate in REJECTED_INTERVALS:
        # A boolean is excluded from "numeric" deliberately: ``True`` is an ``int`` in Python and is no
        # number of seconds in Requirement 2.7's terms.
        numeric = isinstance(candidate, int | float) and not isinstance(candidate, bool)
        inside = numeric and float(candidate).is_integer() and MIN_POLL_INTERVAL_S <= candidate <= MAX_POLL_INTERVAL_S
        assert not inside

    for port in NOTABLE_PORTS + INSIDE_EDGE_PORTS:
        assert MIN_PORT <= port <= MAX_PORT

    for candidate in REJECTED_PORTS:
        whole = isinstance(candidate, int) and not isinstance(candidate, bool)
        assert not (whole and MIN_PORT <= candidate <= MAX_PORT)


# Feature: remote-energy-meter, Property 4: Out-of-range global settings reject the whole configuration
#
# For any global poll interval that is not a whole number of seconds or falls outside 5-3600 seconds
# inclusive, and for any MQTT broker port outside 1-65535 inclusive or omitted broker host, the
# configuration is rejected with a non-zero exit and an error record naming the offending field; and for
# any interval within 5-3600 and port within 1-65535, the configuration is accepted.
# Validates: Requirements 2.7, 2.11
@settings(max_examples=150, deadline=None)
@given(case=global_setting_cases())
def test_out_of_range_global_settings_reject_the_whole_configuration(case: GlobalCase) -> None:
    """Requirements 2.7 and 2.11, over documents whose globals sit inside and outside their ranges."""
    event(f"regime: {case.regime.value}")
    event(f"defect: {case.defect.value}")
    event(f"devices: {case.devices}")

    # Private to this example, so the credentials the printed document carries do not leave arbitrary
    # text redacted from every later record in the session.
    registry = CredentialRegistry()
    log = logging.getLogger(LOGGER_NAME)

    with captured_records() as records:
        outcome = parse_options(
            case.text,
            schemas=SCHEMAS,
            secrets=SecretResolver.from_mapping({}, registry=registry),
            logger=log,
            registry=registry,
        )

    if case.fatal is None:
        # --- the negative half: every global in range, so nothing is rejected at all ---
        # Without this the property would be satisfied by a parser that rejects every configuration.
        assert outcome.fatal is None
        assert outcome.ok
        restored = outcome.config
        assert restored is not None
        assert outcome.rejections == ()
        assert records == []

        # The globals came through as the values drawn, so "accepted" means the configuration was used
        # rather than merely not rejected -- a parser that fell back to a default would fail here.
        assert case.accepted_globals is not None
        expected_interval, expected_port = case.accepted_globals
        assert restored.poll_interval_s == expected_interval
        assert restored.mqtt.port == expected_port
        assert len(restored.devices) == case.devices
        return

    reason, field_name = case.fatal

    # --- the whole configuration is rejected: no Configuration_Object at all ---
    # This is the distinction from Properties 2 and 3, where a defect costs one Device_Entry and the
    # rest are still metered. A global setting belongs to no entry, so there is nothing to exclude and
    # nothing left to run on.
    assert outcome.config is None
    assert not outcome.ok

    fatal = outcome.fatal
    assert fatal is not None

    # --- and the fatal error names the offending global field (Requirements 2.7, 2.11) ---
    assert fatal.reason is reason
    assert fatal.field_name == field_name

    # The device list is valid from end to end, so no entry could be excluded for reasons of its own: a
    # parser that reported a global defect as a per-entry rejection fails here.
    assert outcome.rejections == ()

    # --- exactly one error record, and it is the fatal one ---
    # The count rules out a stray record per device entry, which is what a parser that mistook the
    # severity would emit; the message and the structured fields tie the record to the fatal error.
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.ERROR
    assert record.getMessage() == fatal.detail
    assert record.__dict__.get("reason") == reason.value
    assert record.__dict__.get("field") == field_name

    if reason is FatalReason.INVALID_GLOBAL_INTERVAL:
        # Requirement 2.7 has the record name the accepted range as well as the offending field.
        assert str(MIN_POLL_INTERVAL_S) in fatal.detail
        assert str(MAX_POLL_INTERVAL_S) in fatal.detail
