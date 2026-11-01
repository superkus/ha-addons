"""Unit tests for topic and unique identifier derivation (task 8.1).

Covers each topic shape from the design's MQTT topic layout, the two-level availability
model, and the ``rem_<device_id>_<role>`` unique identifier form. Determinism of the
identifiers across the full identifier space is Property 25 (task 8.4).
"""

from __future__ import annotations

import pytest

from remote_energy_meter.config.models import MqttSettings
from remote_energy_meter.mqtt.topics import (
    DeviceTopics,
    EntityRole,
    attributes_topic,
    bridge_availability_topic,
    device_availability_topic,
    device_node_id,
    device_topics,
    device_topics_for,
    discovery_config_topic,
    integration_inventory_topic,
    state_topic,
    unique_id,
)

DEVICE_ID = "poweredge_a"


# --------------------------------------------------------------------------------------------------
# availability topics: two levels, one will target
# --------------------------------------------------------------------------------------------------


def test_bridge_availability_topic_is_the_process_wide_will_target() -> None:
    """Requirement 6.6: one will message on this topic marks every entity unavailable."""
    assert bridge_availability_topic() == "remote_energy_meter/bridge/availability"


def test_device_availability_topic_is_per_device() -> None:
    """Requirements 6.7, 6.8: a per-device topic distinguishes one unreachable device."""
    assert device_availability_topic(DEVICE_ID) == "remote_energy_meter/poweredge_a/availability"


def test_device_availability_topic_never_collides_with_the_bridge_topic() -> None:
    with pytest.raises(ValueError, match="reserved"):
        device_availability_topic("Bridge")


# --------------------------------------------------------------------------------------------------
# state and attributes topics
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        (EntityRole.POWER, "remote_energy_meter/poweredge_a/power/state"),
        (EntityRole.ENERGY, "remote_energy_meter/poweredge_a/energy/state"),
    ],
)
def test_state_topic_shape(role: EntityRole, expected: str) -> None:
    assert state_topic(DEVICE_ID, role) == expected


def test_power_attributes_topic_shape() -> None:
    """Requirement 9.5: the read duration is published as a diagnostic attribute."""
    assert attributes_topic(DEVICE_ID, EntityRole.POWER) == "remote_energy_meter/poweredge_a/power/attributes"


# --------------------------------------------------------------------------------------------------
# discovery configuration topics
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        (EntityRole.POWER, "homeassistant/sensor/rem_poweredge_a/power/config"),
        (EntityRole.ENERGY, "homeassistant/sensor/rem_poweredge_a/energy/config"),
    ],
)
def test_discovery_config_topic_shape(role: EntityRole, expected: str) -> None:
    assert discovery_config_topic(DEVICE_ID, role) == expected


def test_both_entities_share_one_discovery_node() -> None:
    """Requirement 6.1: one Home Assistant device carrying both entities."""
    power = discovery_config_topic(DEVICE_ID, EntityRole.POWER)
    energy = discovery_config_topic(DEVICE_ID, EntityRole.ENERGY)

    assert power.rsplit("/", 2)[0] == energy.rsplit("/", 2)[0] == "homeassistant/sensor/rem_poweredge_a"


# --------------------------------------------------------------------------------------------------
# unique identifiers (Requirement 6.11)
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        (EntityRole.POWER, "rem_poweredge_a_power"),
        (EntityRole.ENERGY, "rem_poweredge_a_energy"),
    ],
)
def test_unique_id_shape(role: EntityRole, expected: str) -> None:
    assert unique_id(DEVICE_ID, role) == expected


def test_unique_id_lowercases_the_device_identifier() -> None:
    assert unique_id("PowerEdge-A", EntityRole.POWER) == "rem_poweredge-a_power"


def test_unique_id_distinguishes_the_two_roles_of_one_device() -> None:
    assert unique_id(DEVICE_ID, EntityRole.POWER) != unique_id(DEVICE_ID, EntityRole.ENERGY)


def test_device_node_id_is_shared_by_both_roles() -> None:
    node = device_node_id(DEVICE_ID)

    assert unique_id(DEVICE_ID, EntityRole.POWER).startswith(f"{node}_")
    assert unique_id(DEVICE_ID, EntityRole.ENERGY).startswith(f"{node}_")


# --------------------------------------------------------------------------------------------------
# configured prefixes
# --------------------------------------------------------------------------------------------------


def test_configured_prefixes_replace_the_defaults() -> None:
    assert state_topic(DEVICE_ID, EntityRole.POWER, "meters/rem") == "meters/rem/poweredge_a/power/state"
    assert (
        discovery_config_topic(DEVICE_ID, EntityRole.ENERGY, "ha-discovery")
        == "ha-discovery/sensor/rem_poweredge_a/energy/config"
    )


def test_prefix_boundary_separators_and_whitespace_are_trimmed() -> None:
    """A stray separator would otherwise produce an empty topic level."""
    assert bridge_availability_topic("  /meters/rem/  ") == "meters/rem/bridge/availability"


@pytest.mark.parametrize("prefix", ["", "   ", "/", "meters/+/rem", "meters/#"])
def test_unusable_prefixes_are_rejected(prefix: str) -> None:
    """A wildcard is legal in a subscription filter but never in a published topic."""
    with pytest.raises(ValueError, match="base_topic"):
        bridge_availability_topic(prefix)


@pytest.mark.parametrize("device_id", ["", "has space", "has/slash", "has+wildcard", "a" * 65])
def test_unusable_device_identifiers_are_rejected(device_id: str) -> None:
    with pytest.raises(ValueError, match="not usable in a topic"):
        state_topic(device_id, EntityRole.POWER)


# --------------------------------------------------------------------------------------------------
# bundled per-device view
# --------------------------------------------------------------------------------------------------


def test_device_topics_bundles_every_derived_value() -> None:
    topics = device_topics(DEVICE_ID)

    assert topics == DeviceTopics(
        device_id=DEVICE_ID,
        node_id="rem_poweredge_a",
        bridge_availability="remote_energy_meter/bridge/availability",
        availability="remote_energy_meter/poweredge_a/availability",
        power_state="remote_energy_meter/poweredge_a/power/state",
        energy_state="remote_energy_meter/poweredge_a/energy/state",
        power_attributes="remote_energy_meter/poweredge_a/power/attributes",
        power_discovery="homeassistant/sensor/rem_poweredge_a/power/config",
        energy_discovery="homeassistant/sensor/rem_poweredge_a/energy/config",
        power_unique_id="rem_poweredge_a_power",
        energy_unique_id="rem_poweredge_a_energy",
    )


@pytest.mark.parametrize("role", list(EntityRole))
def test_device_topics_accessors_agree_with_the_free_functions(role: EntityRole) -> None:
    topics = device_topics(DEVICE_ID)

    assert topics.state(role) == state_topic(DEVICE_ID, role)
    assert topics.discovery(role) == discovery_config_topic(DEVICE_ID, role)
    assert topics.unique_id(role) == unique_id(DEVICE_ID, role)


def test_device_topics_for_reads_only_the_two_prefixes() -> None:
    """Requirement 6.11: host, port, and credentials do not enter the derivation."""
    settings = MqttSettings(host="core-mosquitto", port=1883)
    moved = MqttSettings(host="198.51.100.5", port=8883, username="addons", password="s3cret", tls=True)  # type: ignore[arg-type]

    assert device_topics_for(settings, DEVICE_ID) == device_topics_for(moved, DEVICE_ID)


def test_device_topics_for_honours_custom_prefixes() -> None:
    settings = MqttSettings(host="core-mosquitto", port=1883, base_topic="meters", discovery_prefix="ha")
    topics = device_topics_for(settings, DEVICE_ID)

    assert topics.availability == "meters/poweredge_a/availability"
    assert topics.power_discovery == "ha/sensor/rem_poweredge_a/power/config"


def test_integration_inventory_topic_is_under_the_normalized_base() -> None:
    assert integration_inventory_topic(" /meters/ ") == "meters/integration/devices"
