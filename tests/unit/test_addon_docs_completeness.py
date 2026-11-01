"""Add-on operator-documentation completeness, separate from the plugin-author guide test.

The expected field sets come from the real Pydantic models and every plugin schema found by
production discovery. DOCS.md is parsed into heading-bounded sections and Markdown tables, so an
incidental field name elsewhere in the document cannot satisfy the check for the wrong scope.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Final, cast

import pytest
import yaml
from pydantic import BaseModel
from yaml.nodes import Node, ScalarNode

from remote_energy_meter.config.models import (
    ConfigurationObject,
    DeviceEntry,
    MqttSettings,
    credential_field_names,
)
from remote_energy_meter.plugins.registry import discover

ADDON_DOCS: Final = Path(__file__).resolve().parents[2] / "remote_energy_meter" / "DOCS.md"

CORE_SECTIONS: Final[tuple[tuple[str, str, type[BaseModel]], ...]] = (
    ("Global options", "", ConfigurationObject),
    ("MQTT options (`mqtt`)", "mqtt.", MqttSettings),
    ("Device envelope (`devices[]`)", "devices[].", DeviceEntry),
)
VALIDATED_PLUGIN_IDS: Final = frozenset({"idrac", "ipmi"})
EXAMPLE_TITLES: Final[dict[str, str]] = {
    "idrac": "Full configuration: Redfish (`idrac`)",
    "ipmi": "Full configuration: IPMI (`ipmi`)",
}

_HEADING_RE: Final = re.compile(r"^(?P<marks>#{2,3})\s+(?P<title>.+?)\s*$", re.MULTILINE)
_PLUGIN_SECTION_RE: Final = re.compile(r"^Plugin `(?P<plugin_id>[A-Za-z0-9_-]+)` \(`devices\[\]\.params`\)$")
_FENCED_YAML_RE: Final = re.compile(r"^```yaml\s*\n(?P<yaml>.*?)^```\s*$", re.MULTILINE | re.DOTALL)
_PATH_CELL_RE: Final = re.compile(r"^`(?P<path>[^`]+)`$")


@dataclass(frozen=True, slots=True)
class MarkdownSection:
    """One heading and the body bounded by the next heading at its level or above."""

    level: int
    title: str
    body: str


@dataclass(frozen=True, slots=True)
class SecretReference:
    """A parsed ``!secret name`` YAML scalar; the referenced value is intentionally unavailable."""

    name: str


class DocsYamlLoader(yaml.SafeLoader):
    """Safe YAML loader that preserves Home Assistant secret references as typed placeholders."""


def _construct_secret(loader: DocsYamlLoader, node: Node) -> SecretReference:
    if not isinstance(node, ScalarNode):
        raise TypeError("a !secret reference must be a scalar")
    return SecretReference(loader.construct_scalar(node).strip())


DocsYamlLoader.add_constructor("!secret", _construct_secret)


def read_docs() -> str:
    assert ADDON_DOCS.is_file(), f"add-on documentation is missing: {ADDON_DOCS}"
    document = ADDON_DOCS.read_text(encoding="utf-8")
    assert document.strip(), f"add-on documentation is empty: {ADDON_DOCS}"
    return document


def markdown_sections(document: str) -> tuple[MarkdownSection, ...]:
    """Parse level-two and level-three sections without considering body prose."""
    matches = list(_HEADING_RE.finditer(document))
    sections: list[MarkdownSection] = []
    for index, match in enumerate(matches):
        level = len(match["marks"])
        end = len(document)
        for following in matches[index + 1 :]:
            if len(following["marks"]) <= level:
                end = following.start()
                break
        sections.append(MarkdownSection(level=level, title=match["title"], body=document[match.end() : end]))
    return tuple(sections)


def required_section(document: str, *, level: int, title: str) -> MarkdownSection:
    matches = [section for section in markdown_sections(document) if section.level == level and section.title == title]
    assert len(matches) == 1, (
        f"{ADDON_DOCS.name} must contain exactly one level-{level} section named {title!r}; found {len(matches)}"
    )
    return matches[0]


def documented_paths(section: MarkdownSection) -> frozenset[str]:
    """Read only the Path column of this section's configuration-reference table."""
    lines = section.body.splitlines()
    header_indexes = [index for index, line in enumerate(lines) if line.strip().startswith("| Path |")]
    assert len(header_indexes) == 1, (
        f"section {section.title!r} must contain exactly one configuration table with a Path column"
    )

    paths: set[str] = set()
    for line in lines[header_indexes[0] + 2 :]:
        stripped = line.strip()
        if not stripped.startswith("|"):
            break
        first_cell = stripped.strip("|").split("|", maxsplit=1)[0].strip()
        match = _PATH_CELL_RE.fullmatch(first_cell)
        if match is not None:
            paths.add(match["path"])
    return frozenset(paths)


def expected_paths(model: type[BaseModel], prefix: str) -> frozenset[str]:
    return frozenset(f"{prefix}{name}" for name in model.model_fields)


def assert_section_covers_model(section: MarkdownSection, model: type[BaseModel], prefix: str) -> None:
    expected = expected_paths(model, prefix)
    actual = documented_paths(section)
    missing = sorted(expected - actual)
    assert not missing, f"{ADDON_DOCS.name} section {section.title!r} is missing configuration field(s): {missing}"


@lru_cache(maxsize=1)
def discovered_plugin_models() -> dict[str, type[BaseModel]]:
    """Return production-discovered plugin schemas, failing clearly if discovery itself is broken."""
    result = discover()
    assert result.fatal is None, f"plugin discovery failed while deriving documentation fields: {result.fatal}"
    return {plugin_id: plugin_cls.config_model for plugin_id, plugin_cls in result.plugins.items()}


def plugin_sections(document: str) -> dict[str, MarkdownSection]:
    sections: dict[str, MarkdownSection] = {}
    duplicates: list[str] = []
    for section in markdown_sections(document):
        if section.level != 3:
            continue
        match = _PLUGIN_SECTION_RE.fullmatch(section.title)
        if match is None:
            continue
        plugin_id = match["plugin_id"]
        if plugin_id in sections:
            duplicates.append(plugin_id)
        sections[plugin_id] = section
    assert not duplicates, f"{ADDON_DOCS.name} has duplicate plugin configuration sections: {sorted(duplicates)}"
    return sections


def fenced_yaml(section: MarkdownSection) -> str:
    matches = list(_FENCED_YAML_RE.finditer(section.body))
    assert len(matches) == 1, f"section {section.title!r} must contain exactly one fenced YAML configuration"
    return matches[0]["yaml"]


def load_example(section: MarkdownSection) -> Mapping[str, object]:
    loaded: object = yaml.load(fenced_yaml(section), Loader=DocsYamlLoader)
    assert isinstance(loaded, Mapping), f"section {section.title!r} must contain a YAML mapping"
    return cast("Mapping[str, object]", loaded)


def mapping_value(parent: Mapping[str, object], key: str, context: str) -> Mapping[str, object]:
    value = parent.get(key)
    assert isinstance(value, Mapping), f"{context}.{key} must be a mapping in the full example"
    return cast("Mapping[str, object]", value)


def only_device(configuration: Mapping[str, object], context: str) -> Mapping[str, object]:
    value = configuration.get("devices")
    assert isinstance(value, list), f"{context}.devices must be a list in the full example"
    assert len(value) == 1, f"{context} must contain exactly one validated-system device entry"
    device: object = value[0]
    assert isinstance(device, Mapping), f"{context}.devices[0] must be a mapping"
    return cast("Mapping[str, object]", device)


def assert_exact_model_fields(mapping: Mapping[str, object], model: type[BaseModel], context: str) -> None:
    actual = {str(key) for key in mapping}
    expected = set(model.model_fields)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    assert not missing, f"{context} is not a full configuration; missing field(s): {missing}"
    assert not extra, f"{context} contains field(s) outside {model.__name__}: {extra}"


def assert_credentials_are_secret_references(
    mapping: Mapping[str, object], model: type[BaseModel], context: str
) -> None:
    invalid = [
        name
        for name in credential_field_names(model)
        if not isinstance(mapping.get(name), SecretReference) or not cast("SecretReference", mapping[name]).name
    ]
    assert not invalid, f"{context} must use non-empty !secret references for credential field(s): {invalid}"


@pytest.mark.parametrize(("title", "prefix", "model"), CORE_SECTIONS)
def test_every_core_field_is_documented_in_its_configuration_section(
    title: str,
    prefix: str,
    model: type[BaseModel],
) -> None:
    """A mention elsewhere cannot hide a missing global, MQTT, or device-envelope table row."""
    section = required_section(read_docs(), level=3, title=title)
    assert_section_covers_model(section, model, prefix)


def test_every_discovered_plugin_has_a_dedicated_configuration_section() -> None:
    """Every production-discovered config_model gets its own params table in the add-on docs."""
    models = discovered_plugin_models()
    sections = plugin_sections(read_docs())
    missing_sections = sorted(set(models) - set(sections))
    assert not missing_sections, f"{ADDON_DOCS.name} is missing plugin section(s): {missing_sections}"

    missing_fields: dict[str, list[str]] = {}
    for plugin_id, model in models.items():
        expected = expected_paths(model, "devices[].params.")
        actual = documented_paths(sections[plugin_id])
        missing = sorted(expected - actual)
        if missing:
            missing_fields[plugin_id] = missing
    assert not missing_fields, f"plugin configuration sections are missing field(s): {missing_fields}"


def test_idrac_and_ipmi_have_dedicated_configuration_sections() -> None:
    """The two hardware-validated plugin IDs must stay discovered and independently documented."""
    models = discovered_plugin_models()
    sections = plugin_sections(read_docs())
    missing_from_discovery = sorted(VALIDATED_PLUGIN_IDS - set(models))
    missing_from_docs = sorted(VALIDATED_PLUGIN_IDS - set(sections))
    assert not missing_from_discovery, f"validated plugin ID(s) are no longer discovered: {missing_from_discovery}"
    assert not missing_from_docs, f"validated plugin ID(s) have no dedicated DOCS.md section: {missing_from_docs}"


@pytest.mark.parametrize(("plugin_id", "title"), EXAMPLE_TITLES.items())
def test_each_validated_system_has_a_full_secret_only_configuration(plugin_id: str, title: str) -> None:
    """Both copy/paste examples include every current field and expose no literal credential."""
    models = discovered_plugin_models()
    assert plugin_id in models, f"full example {title!r} names an undiscovered plugin {plugin_id!r}"

    configuration = load_example(required_section(read_docs(), level=2, title=title))
    mqtt = mapping_value(configuration, "mqtt", title)
    device = only_device(configuration, title)
    params = mapping_value(device, "params", f"{title}.devices[0]")

    assert device.get("plugin") == plugin_id, f"full example {title!r} must select plugin {plugin_id!r}"
    assert_exact_model_fields(configuration, ConfigurationObject, title)
    assert_exact_model_fields(mqtt, MqttSettings, f"{title}.mqtt")
    assert_exact_model_fields(device, DeviceEntry, f"{title}.devices[0]")
    assert_exact_model_fields(params, models[plugin_id], f"{title}.devices[0].params")
    assert_credentials_are_secret_references(mqtt, MqttSettings, f"{title}.mqtt")
    assert_credentials_are_secret_references(params, models[plugin_id], f"{title}.devices[0].params")
