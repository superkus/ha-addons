"""Schema agreement: the Supervisor options schema and the models the parser reads agree (task 4.9).

Requirement 1.1 has the addon manifest declare "an options schema that covers every Addon_Configuration
field the Configuration_Parser reads". That shape is declared twice -- once in
``remote_energy_meter/config.yaml`` for the Supervisor, once in ``config/models.py`` plus each plugin's
published schema for the process -- and nothing but this module keeps the two in step.

The comparison runs **both ways**, deliberately. A test that only looked for *missing* declarations would
let dead options accumulate: an option an administrator can set, that Supervisor renders in the add-on
configuration UI, and that no code ever reads. Both directions are derived from ``model_fields`` rather
than from a list restated here, so adding a model field without touching the manifest fails, and so does
removing one while leaving the manifest declaration behind.

## Where Supervisor's vocabulary runs out

Supervisor's schema language is a short list of type words, an optional range or choice set in
parentheses, and a trailing ``?`` meaning "may be omitted". It cannot mirror a pydantic constraint
exactly in four places. Each is asserted as far as it goes, and the residue is stated here rather than
quietly skipped:

* **Requiredness of a nested block.** The ``?`` marker attaches to a scalar declaration; ``mqtt``,
  ``devices``, and ``params`` are blocks and carry no marker at all.
  :func:`test_nested_blocks_are_required_model_fields` turns the gap into a checkable claim -- every
  block corresponds to a *required* model field, so the missing marker cannot be wrong. A model field
  that became optional while staying a block would fail there.

* **Requiredness and bounds inside ``params``.** Requirement 2.2 validates ``params`` against the schema
  published by the plugin the entry names, so its accepted shape is plugin-specific while the manifest
  gets one declaration for every plugin. The manifest therefore declares the *union* of the shipped
  plugin schemas with every field optional, and this module compares against that same union rather than
  against any single plugin. :func:`test_plugin_parameter_declarations_are_all_optional` pins the choice
  so a future plugin's required field is not quietly promoted into a Supervisor requirement that the
  other plugins would fail.

* **Bounds whose violation must exclude one entry, not the whole document.** Supervisor refuses to save
  the entire options blob when its schema rejects any part of it. For the per-device poll interval
  (Requirements 2.2, 2.3, 5.7) and for plugin parameter ranges (Requirements 2.2, 2.3) that would
  contradict the requirement, which excludes the offending Device_Entry and retains the rest. Those
  bounds are deliberately absent from the manifest, and
  :func:`test_the_per_device_poll_interval_is_declared_without_bounds` and
  :func:`test_plugin_parameter_declarations_carry_no_bounds` assert the absence, so a well-meant
  tightening is caught here instead of turning one typo into a configuration an administrator cannot
  save.

* **String form and length.** Supervisor's ``str`` carries no length bound, and ``match(<regex>)`` -- the
  one word that could carry a form -- is used nowhere in the manifest. Two unrelated reasons, neither an
  oversight. For the device identifier and the plugin credentials, Requirements 2.3, 2.12, 8.5, and 8.8
  exclude the one offending entry, which a Supervisor schema failure cannot do because it refuses the
  whole document. For ``mqtt.host``, whose emptiness *is* fatal (Requirement 2.11), a regex would only
  restate in a second dialect what the model already rejects. So every string field is declared bare,
  which leaves the type and requiredness checks above with nothing to say about its value domain.
  :func:`test_string_declarations_carry_no_form_or_length_argument` states the resulting invariant
  outright, and :func:`test_the_device_identifier_form_is_enforced_on_the_model` closes the loop for the
  one field whose form the parser reports per entry.

The bounds the manifest *does* declare are the ones whose violation is already fatal to the whole
configuration -- the global poll interval (Requirement 2.7) and the broker port (Requirement 2.11) --
plus the closed log level set (Requirement 9.2), which Supervisor renders as a dropdown and which has no
legitimate value outside the enum. Where a bound is declared, it is checked against the model's own
bound rather than against a literal.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import NoneType, UnionType
from typing import Any, Final, Union, get_args, get_origin

import annotated_types
import pytest
import yaml
from pydantic import BaseModel, SecretStr
from pydantic.fields import FieldInfo

from remote_energy_meter.config.models import (
    CREDENTIAL_MARKER,
    DEVICE_ID_PATTERN,
    MAX_DEVICE_ID_LENGTH,
    MAX_POLL_INTERVAL_S,
    MAX_PORT,
    MIN_POLL_INTERVAL_S,
    MIN_PORT,
    ConfigurationObject,
    DeviceEntry,
    LogLevel,
    MqttSettings,
    credential_field_names,
)
from remote_energy_meter.plugins.registry import discover

#: What Supervisor's ``port`` type word means. Named here because the manifest spends one word where the
#: model spends two bounds, and the test below has to compare the two.
SUPERVISOR_PORT_RANGE: Final = (1, 65535)

#: Stand-ins for the two shapes a declaration can take that are not a type word: a block of fields, and
#: a list of blocks. They live in the same namespace as the Supervisor type words so one comparison
#: covers scalars and blocks alike.
NESTED_BLOCK: Final = "<block>"
NESTED_BLOCK_LIST: Final = "<list of blocks>"

#: The Supervisor type words that carry a string value. ``match`` is listed although the manifest uses it
#: nowhere: it is the only one of the three that takes an argument, so listing it is what keeps the test
#: below from being vacuous the day someone reaches for it.
STRING_TYPE_WORDS: Final = frozenset({"str", "match", "password"})


# --------------------------------------------------------------------------------------------------
# reading the manifest
# --------------------------------------------------------------------------------------------------

#: The Supervisor-facing declaration of the Addon_Configuration shape (Requirement 1.1).
ADDON_MANIFEST: Final = Path(__file__).resolve().parents[2] / "remote_energy_meter" / "config.yaml"


def as_block(value: object, path: str) -> Mapping[str, object]:
    """``value`` as a block of declarations keyed by field name."""
    assert isinstance(value, Mapping), f"the addon manifest entry at {path} is not a block of fields, but {value!r}"
    return {str(key): item for key, item in value.items()}


def child_block(parent: Mapping[str, object], key: str, path: str) -> Mapping[str, object]:
    """The nested block at ``key``, or a failure naming the block the manifest is missing."""
    assert key in parent, f"the addon manifest declares no {path} block"
    return as_block(parent[key], path)


def load_manifest() -> Mapping[str, object]:
    """The whole manifest. Real YAML, because the manifest carries comments and a folded description."""
    return as_block(yaml.safe_load(ADDON_MANIFEST.read_text(encoding="utf-8")), str(ADDON_MANIFEST))


MANIFEST: Final = load_manifest()
OPTIONS_SCHEMA: Final = child_block(MANIFEST, "schema", "schema")
DEFAULT_OPTIONS: Final = child_block(MANIFEST, "options", "options")


def device_declarations() -> Mapping[str, object]:
    """The single element template Supervisor uses to declare a homogeneous list."""
    declared = OPTIONS_SCHEMA.get("devices")
    assert isinstance(declared, list), "the addon manifest declares `schema.devices` as a list of device entries"
    assert len(declared) == 1, (
        "Supervisor declares a homogeneous list by exactly one element template, "
        f"but `schema.devices` holds {len(declared)}"
    )
    return as_block(declared[0], "schema.devices[]")


# --------------------------------------------------------------------------------------------------
# the Supervisor declaration grammar
# --------------------------------------------------------------------------------------------------

#: A type word, an optional parenthesised argument (a numeric range, or ``|``-separated choices), and an
#: optional trailing ``?`` marking the field as omittable.
_DECLARATION_RE: Final = re.compile(r"(?P<type_name>[a-z_]+)(?:\((?P<argument>.*)\))?(?P<optional>\?)?")


@dataclass(frozen=True, slots=True)
class Declaration:
    """One parsed Supervisor scalar declaration, such as ``int(5,3600)?``."""

    raw: str
    type_name: str
    argument: str | None
    optional: bool

    @property
    def bounds(self) -> tuple[str, ...]:
        """The comma-separated range arguments, empty when the declaration carries no range."""
        if self.argument is None:
            return ()
        return tuple(part.strip() for part in self.argument.split(","))

    @property
    def choices(self) -> tuple[str, ...]:
        """The ``|``-separated members of a ``list(...)`` declaration, in declared order."""
        if self.argument is None:
            return ()
        return tuple(part.strip() for part in self.argument.split("|"))


def parse_declaration(raw: object, path: str) -> Declaration:
    """Parse one scalar declaration, failing on anything this module does not understand."""
    assert isinstance(raw, str), f"the declaration at {path} is not a Supervisor type declaration, but {raw!r}"
    match = _DECLARATION_RE.fullmatch(raw)
    assert match is not None, f"the declaration {raw!r} at {path} is not a Supervisor type declaration"
    return Declaration(
        raw=raw,
        type_name=match["type_name"],
        argument=match["argument"],
        optional=match["optional"] is not None,
    )


def declared_kind(declared: object, path: str) -> str:
    """The Supervisor type word of ``declared``, or the stand-in for a block or a list of blocks."""
    if isinstance(declared, Mapping):
        return NESTED_BLOCK
    if isinstance(declared, list):
        return NESTED_BLOCK_LIST
    return parse_declaration(declared, path).type_name


def is_block(declared: object) -> bool:
    """True when the declaration is a nested block or a list of blocks rather than a scalar."""
    return isinstance(declared, Mapping | list)


# --------------------------------------------------------------------------------------------------
# the model side
# --------------------------------------------------------------------------------------------------


def base_annotation(annotation: object) -> object:
    """The type a Supervisor declaration has to carry: ``| None`` stripped, generics reduced to origin."""
    origin = get_origin(annotation)
    if origin is UnionType or origin is Union:
        present = [argument for argument in get_args(annotation) if argument is not NoneType]
        assert len(present) == 1, f"{annotation!r} is a union of several types, which one declaration cannot carry"
        return base_annotation(present[0])
    return origin if origin is not None else annotation


def accepted_kinds(annotation: object) -> frozenset[str]:
    """Every Supervisor declaration that faithfully carries ``annotation``.

    More than one is accepted where Supervisor offers a *refinement* of a base type: ``port`` narrows
    ``int``, ``match`` narrows ``str``, and ``password`` is ``str`` with the value masked in the
    configuration UI. A refinement being permitted here is not a licence to use it loosely -- each one
    that is used is checked for faithfulness by its own test below.
    """
    base = base_annotation(annotation)
    assert isinstance(base, type), f"{annotation!r} does not reduce to a type this module can compare"

    # Ordered from narrowest to widest: ``StrEnum`` is a ``str``, ``bool`` is an ``int``.
    if issubclass(base, StrEnum):
        return frozenset({"list"})
    if issubclass(base, SecretStr):
        return frozenset({"str", "password"})
    if issubclass(base, BaseModel):
        return frozenset({NESTED_BLOCK})
    if issubclass(base, bool):
        return frozenset({"bool"})
    if issubclass(base, str):
        return frozenset({"str", "match"})
    if issubclass(base, int):
        return frozenset({"int", "port"})
    if issubclass(base, float):
        return frozenset({"float"})
    if issubclass(base, dict):
        return frozenset({NESTED_BLOCK})
    if issubclass(base, list):
        return frozenset({NESTED_BLOCK_LIST})
    pytest.fail(f"{annotation!r} has no Supervisor declaration this module knows how to compare against")


def numeric_bounds(field: FieldInfo) -> tuple[object, object]:
    """The ``ge``/``le`` bounds declared on ``field``, each ``None`` when unbounded."""
    lower: object = None
    upper: object = None
    for constraint in field.metadata:
        if isinstance(constraint, annotated_types.Ge):
            lower = constraint.ge
        elif isinstance(constraint, annotated_types.Le):
            upper = constraint.le
    return lower, upper


def string_constraints(field: FieldInfo) -> tuple[object, object, str | None]:
    """The ``min_length``, ``max_length``, and ``pattern`` declared on ``field``, each ``None`` when absent.

    Lengths arrive as ``annotated_types`` constraints and are matched by type. ``pattern`` is read by
    attribute instead, because pydantic keeps it in a private metadata object whose class is not part of
    the public API -- naming that class here would make this module fail on a pydantic upgrade that
    renames it, which is not the drift this module exists to catch.
    """
    minimum: object = None
    maximum: object = None
    pattern: str | None = None
    for constraint in field.metadata:
        if isinstance(constraint, annotated_types.MinLen):
            minimum = constraint.min_length
        elif isinstance(constraint, annotated_types.MaxLen):
            maximum = constraint.max_length
        else:
            declared = getattr(constraint, "pattern", None)
            if isinstance(declared, str):
                pattern = declared
    return minimum, maximum, pattern


# --------------------------------------------------------------------------------------------------
# pairing each options level with the model that reads it
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Section:
    """One level of the options document: what the manifest declares there, and what reads it.

    Attributes:
        path: The dotted options path of this level, empty for the document root.
        declared: The manifest declarations at this level, by field name.
        fields: The model fields the parser reads at this level, by field name.
        credentials: Names within ``fields`` marked as credential fields (Requirement 8.1).
        source: What to name in a failure message as the reader of these fields.
        mirrors_requiredness: Whether a ``?`` marker at this level can agree with model requiredness.
            False for ``params``, whose requiredness is plugin-specific -- see the module docstring.
    """

    path: str
    declared: Mapping[str, object]
    fields: Mapping[str, FieldInfo]
    credentials: frozenset[str]
    source: str
    mirrors_requiredness: bool

    @property
    def label(self) -> str:
        """A short identifier for parametrised test output."""
        return self.path or "<root>"

    def at(self, name: str) -> str:
        """The dotted options path of ``name`` within this level, for failure messages."""
        return f"{self.path}.{name}" if self.path else name

    def shared_names(self) -> tuple[str, ...]:
        """Field names present on both sides, in model declaration order.

        The two set differences are each a test of their own, so every other test works over the
        intersection and reports its own failure rather than a lookup error.
        """
        return tuple(name for name in self.fields if name in self.declared)


def model_section(
    path: str, declared: Mapping[str, object], model: type[BaseModel], *, mirrors: bool = True
) -> Section:
    """A section read by exactly one model."""
    return Section(
        path=path,
        declared=declared,
        fields=model.model_fields,
        credentials=frozenset(credential_field_names(model)),
        source=model.__name__,
        mirrors_requiredness=mirrors,
    )


def plugin_params_section(declared: Mapping[str, object]) -> Section:
    """The ``params`` section, read by whichever plugin schema the entry names (Requirement 2.2).

    Built from the union over the shipped plugin schemas, discovered the same way the process discovers
    them, so a new plugin's parameters have to reach the manifest without this module being edited. A
    parameter name shared by two plugins must reduce to the same base type, since the manifest gets one
    declaration for it.
    """
    result = discover()
    assert result.ok, f"plugin discovery must succeed before the manifest can be compared against it: {result.fatal}"

    merged: dict[str, FieldInfo] = {}
    credentials: set[str] = set()
    owners: dict[str, str] = {}
    for plugin_id, plugin_cls in result.plugins.items():
        schema = plugin_cls.config_model
        credentials.update(credential_field_names(schema))
        for name, field in schema.model_fields.items():
            first = merged.get(name)
            if first is None:
                merged[name] = field
                owners[name] = plugin_id
                continue
            assert base_annotation(first.annotation) is base_annotation(field.annotation), (
                f"plugins {owners[name]!r} and {plugin_id!r} both declare the parameter {name!r} but with "
                "different base types, so one Supervisor declaration cannot carry both"
            )

    return Section(
        path="devices[].params",
        declared=declared,
        fields=merged,
        credentials=frozenset(credentials),
        source=f"the union of the shipped plugin schemas ({', '.join(sorted(result.plugin_ids))})",
        mirrors_requiredness=False,
    )


DEVICE_SCHEMA: Final = device_declarations()

SECTIONS: Final = (
    model_section("", OPTIONS_SCHEMA, ConfigurationObject),
    model_section("mqtt", child_block(OPTIONS_SCHEMA, "mqtt", "schema.mqtt"), MqttSettings),
    model_section("devices[]", DEVICE_SCHEMA, DeviceEntry),
    plugin_params_section(child_block(DEVICE_SCHEMA, "params", "schema.devices[].params")),
)

SECTION_IDS: Final = [section.label for section in SECTIONS]

MIRRORING_SECTIONS: Final = tuple(section for section in SECTIONS if section.mirrors_requiredness)
MIRRORING_IDS: Final = [section.label for section in MIRRORING_SECTIONS]

PARAMS_SECTION: Final = SECTIONS[-1]


# --------------------------------------------------------------------------------------------------
# field coverage, both directions (Requirement 1.1)
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("section", SECTIONS, ids=SECTION_IDS)
def test_the_manifest_declares_every_field_the_parser_reads(section: Section) -> None:
    """Requirement 1.1, forwards: a field the parser reads that Supervisor does not declare is unsettable."""
    undeclared = sorted(set(section.fields) - set(section.declared))

    assert not undeclared, (
        f"{section.source} reads {undeclared}, which the addon manifest does not declare under "
        f"`schema.{section.path}`" + (" " if section.path else "") + "-- an administrator cannot set them"
    )


@pytest.mark.parametrize("section", SECTIONS, ids=SECTION_IDS)
def test_every_declared_option_is_a_field_something_reads(section: Section) -> None:
    """Requirement 1.1, backwards: an option nothing reads is a promise the addon does not keep."""
    unread = sorted(set(section.declared) - set(section.fields))

    assert not unread, (
        f"the addon manifest declares {unread} at `schema.{section.path}`, which {section.source} does not "
        "read -- either the field was removed from the model or the declaration was never wired up"
    )


@pytest.mark.parametrize("section", SECTIONS, ids=SECTION_IDS)
def test_declared_types_carry_the_model_types(section: Section) -> None:
    """A declaration whose type cannot hold the model's type rejects valid input, or admits invalid input."""
    for name in section.shared_names():
        path = section.at(name)
        annotation = section.fields[name].annotation
        accepted = accepted_kinds(annotation)
        kind = declared_kind(section.declared[name], path)

        assert kind in accepted, (
            f"the addon manifest declares {path} as {kind!r}, but {section.source} reads it as "
            f"{annotation!r}, which Supervisor expresses as {sorted(accepted)}"
        )


# --------------------------------------------------------------------------------------------------
# requiredness, and the one place Supervisor cannot express it
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("section", MIRRORING_SECTIONS, ids=MIRRORING_IDS)
def test_optional_markers_mirror_model_requiredness(section: Section) -> None:
    """A missing ``?`` makes Supervisor demand a value the model defaults; a spurious one hides an omission."""
    for name in section.shared_names():
        declared = section.declared[name]
        if is_block(declared):
            continue  # no marker to compare -- see test_nested_blocks_are_required_model_fields

        path = section.at(name)
        field = section.fields[name]
        expected_optional = not field.is_required()

        assert parse_declaration(declared, path).optional is expected_optional, (
            f"the addon manifest declares {path} as {declared!r}, but {section.source} treats it as "
            f"{'optional' if expected_optional else 'required'}"
        )


@pytest.mark.parametrize("section", SECTIONS, ids=SECTION_IDS)
def test_nested_blocks_are_required_model_fields(section: Section) -> None:
    """The ``?`` marker attaches to a scalar only, so a block is required whether or not the model agrees.

    Asserting that every block *is* a required model field is what makes the missing marker harmless.
    """
    for name in section.shared_names():
        if not is_block(section.declared[name]):
            continue

        assert section.fields[name].is_required(), (
            f"the addon manifest declares {section.at(name)} as a block, which Supervisor cannot mark "
            f"omittable, but {section.source} treats it as optional"
        )


def test_plugin_parameter_declarations_are_all_optional() -> None:
    """Requirement 2.2: ``params`` is plugin-specific, so the manifest cannot demand any one plugin's field.

    Every shipped plugin's parameters share one declaration block, so a field required by one plugin would
    become a field Supervisor demands of every plugin. Requiredness stays with the plugin schema, which is
    what the Configuration_Parser validates the block against.
    """
    required = sorted(
        name
        for name, declared in PARAMS_SECTION.declared.items()
        if not parse_declaration(declared, PARAMS_SECTION.at(name)).optional
    )

    assert not required, (
        f"the addon manifest declares the plugin parameters {required} as required; that demands them of "
        "every plugin, including ones that do not have them"
    )


# --------------------------------------------------------------------------------------------------
# bounds the manifest declares, checked against the model's own bounds
# --------------------------------------------------------------------------------------------------


def test_the_global_poll_interval_range_is_the_model_range() -> None:
    """Requirement 2.7 makes an out-of-range global interval fatal, so declaring the range costs nothing."""
    declaration = parse_declaration(OPTIONS_SCHEMA["poll_interval_s"], "poll_interval_s")

    assert declaration.type_name == "int"
    assert declaration.bounds == (str(MIN_POLL_INTERVAL_S), str(MAX_POLL_INTERVAL_S)), (
        f"the addon manifest declares the global poll interval as {declaration.raw!r}, which is not the "
        f"{MIN_POLL_INTERVAL_S}-{MAX_POLL_INTERVAL_S} second range the Configuration_Object accepts"
    )
    assert numeric_bounds(ConfigurationObject.model_fields["poll_interval_s"]) == (
        MIN_POLL_INTERVAL_S,
        MAX_POLL_INTERVAL_S,
    )


def test_the_broker_port_is_declared_as_a_port_and_the_model_agrees() -> None:
    """Requirement 2.11 makes an out-of-range port fatal. Supervisor spends one word where the model spends two."""
    declaration = parse_declaration(SECTIONS[1].declared["port"], "mqtt.port")

    assert declaration.type_name == "port"
    assert numeric_bounds(MqttSettings.model_fields["port"]) == (MIN_PORT, MAX_PORT)
    assert (MIN_PORT, MAX_PORT) == SUPERVISOR_PORT_RANGE, (
        f"the addon manifest declares the broker port as `port`, which Supervisor validates as "
        f"{SUPERVISOR_PORT_RANGE[0]}-{SUPERVISOR_PORT_RANGE[1]}, but the model accepts {MIN_PORT}-{MAX_PORT}"
    )


def test_the_log_level_choices_are_the_declared_log_levels() -> None:
    """Requirement 9.2 fixes a closed set of four severities, and Supervisor renders it as a dropdown."""
    declaration = parse_declaration(OPTIONS_SCHEMA["log_level"], "log_level")

    assert declaration.type_name == "list"
    assert declaration.choices == tuple(level.value for level in LogLevel), (
        f"the addon manifest offers the log levels {list(declaration.choices)}, but the Diagnostics_Logger "
        f"accepts {[level.value for level in LogLevel]}"
    )


@pytest.mark.parametrize("section", SECTIONS, ids=SECTION_IDS)
def test_password_declarations_name_credential_fields(section: Section) -> None:
    """Supervisor's ``password`` masks the value in the configuration UI, so it must name a real credential.

    Only this direction is checkable. The reverse -- every credential field declared ``password`` -- is not
    a rule the manifest keeps: Requirement 8.1 derives the *redaction* set from the model marker, and a
    field such as a broker or iDRAC username is a credential to the redaction registry while still being a
    value an administrator needs to read back while typing it.
    """
    for name in section.shared_names():
        declared = section.declared[name]
        if is_block(declared):
            continue
        if parse_declaration(declared, section.at(name)).type_name != "password":
            continue

        assert name in section.credentials, (
            f"the addon manifest declares {section.at(name)} as `password`, which masks it in the "
            f"configuration UI, but {section.source} does not mark it with the {CREDENTIAL_MARKER!r} marker"
        )


# --------------------------------------------------------------------------------------------------
# constraints the manifest deliberately omits (Requirements 2.2, 2.3, 2.12, 5.7, 8.5, 8.8)
# --------------------------------------------------------------------------------------------------


def test_the_per_device_poll_interval_is_declared_without_bounds() -> None:
    """Requirements 2.3 and 5.7: an out-of-range device interval excludes one entry, or falls back.

    Supervisor refuses to save the whole options blob when its schema rejects any part of it, so declaring
    the 5-3600 second bound here would stop an administrator saving sixteen good entries because the
    seventeenth has a typo. The bound lives on ``DeviceEntry`` alone, where the parser can act per entry.
    """
    declaration = parse_declaration(DEVICE_SCHEMA["poll_interval_s"], "devices[].poll_interval_s")

    assert declaration.bounds == (), (
        f"the addon manifest declares the per-device poll interval as {declaration.raw!r}; a bound there "
        "makes Supervisor refuse the whole options blob, which Requirement 2.3 excludes one entry for"
    )
    assert numeric_bounds(DeviceEntry.model_fields["poll_interval_s"]) == (
        MIN_POLL_INTERVAL_S,
        MAX_POLL_INTERVAL_S,
    ), "the bound the manifest leaves out has to be enforced on the model, or it is enforced nowhere"


def test_plugin_parameter_declarations_carry_no_bounds() -> None:
    """Requirements 2.2 and 2.3: a plugin parameter out of range excludes one entry, not the document.

    The same reasoning as the per-device interval, plus one of its own: the block is the union over the
    shipped plugin schemas, so a bound taken from one plugin would be imposed on every other.
    """
    bounded = sorted(
        name
        for name, declared in PARAMS_SECTION.declared.items()
        if parse_declaration(declared, PARAMS_SECTION.at(name)).bounds
    )

    assert not bounded, (
        f"the addon manifest bounds the plugin parameters {bounded}; a violation there makes Supervisor "
        "refuse the whole options blob, which Requirement 2.3 excludes one entry for"
    )


@pytest.mark.parametrize("section", SECTIONS, ids=SECTION_IDS)
def test_string_declarations_carry_no_form_or_length_argument(section: Section) -> None:
    """Every string field is declared bare, so its value domain is the models' to police -- see the module docstring.

    Without this, a string field passes the type and requiredness checks above while nothing at all is said
    about the constraints its model carries: the length bound on ``mqtt.host``, the character set on
    ``device_id``, the credential lengths inside ``params``. Stating the invariant is what turns that
    silence into a claim, and what makes a later ``match(...)`` an explicit decision rather than a quiet
    change to which component reports a malformed value.
    """
    argued: list[str] = []
    for name in section.shared_names():
        declared = section.declared[name]
        if is_block(declared):
            continue
        declaration = parse_declaration(declared, section.at(name))
        if declaration.type_name in STRING_TYPE_WORDS and declaration.argument is not None:
            argued.append(f"{section.at(name)} as {declaration.raw!r}")

    assert not argued, (
        f"the addon manifest constrains the string value(s) {argued}; a violation there makes Supervisor "
        "refuse the whole options blob, and the models already reject the same values where the "
        "Configuration_Parser can report them per field"
    )


def test_the_device_identifier_form_is_enforced_on_the_model() -> None:
    """Requirement 2.12: the form the manifest leaves out has to be enforced where a failure excludes one entry."""
    declaration = parse_declaration(DEVICE_SCHEMA["device_id"], "devices[].device_id")

    assert declaration.type_name == "str"
    assert string_constraints(DeviceEntry.model_fields["device_id"]) == (1, MAX_DEVICE_ID_LENGTH, DEVICE_ID_PATTERN), (
        "the addon manifest declares the device identifier as a bare `str`, so the 1-"
        f"{MAX_DEVICE_ID_LENGTH} character bound and the {DEVICE_ID_PATTERN!r} character set have to be "
        "enforced on DeviceEntry, or they are enforced nowhere"
    )


# --------------------------------------------------------------------------------------------------
# the shipped default options
# --------------------------------------------------------------------------------------------------


def test_every_default_option_is_a_declared_option() -> None:
    """Supervisor requires each key in ``options`` to appear in ``schema``, recursively."""

    def compare(defaults: Mapping[str, object], declared: Mapping[str, object], path: str) -> None:
        undeclared = sorted(set(defaults) - set(declared))
        assert not undeclared, f"the default options at `options.{path}` declare {undeclared}, absent from `schema`"

        for name, value in defaults.items():
            where = f"{path}.{name}" if path else name
            if isinstance(value, Mapping):
                compare(as_block(value, f"options.{where}"), as_block(declared[name], f"schema.{where}"), where)
            elif isinstance(value, list):
                template = declared[name]
                assert isinstance(template, list) and len(template) == 1
                for index, element in enumerate(value):
                    compare(
                        as_block(element, f"options.{where}[{index}]"),
                        as_block(template[0], f"schema.{where}[]"),
                        f"{where}[]",
                    )

    compare(DEFAULT_OPTIONS, OPTIONS_SCHEMA, "")


def test_the_default_options_are_a_configuration_the_models_accept() -> None:
    """The shipped defaults are what a fresh install runs, so the models have to accept them as written.

    Validated against the models rather than through ``parse_options``, because the example password is a
    ``!secret`` reference and resolving it needs a Home Assistant ``secrets.yaml`` that no test owns
    (Requirement 8.4). Reference substitution happens before validation, so an unresolved reference is a
    well-formed credential string at this level.
    """
    config = ConfigurationObject.model_validate(DEFAULT_OPTIONS)

    schemas: Mapping[str, type[BaseModel]] = {
        plugin_id: plugin_cls.config_model for plugin_id, plugin_cls in discover().plugins.items()
    }
    for entry in config.devices:
        schema = schemas.get(entry.plugin)
        assert schema is not None, (
            f"the default device entry {entry.device_id!r} names the plugin {entry.plugin!r}, which is not "
            f"shipped; discovery found {sorted(schemas)}"
        )
        parsed: Any = schema.model_validate(entry.params)
        assert parsed is not None
