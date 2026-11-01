"""Topic and entity unique identifier derivation.

Pure functions from data to data: no client, no connection, no I/O. Every topic string
and every ``unique_id`` the publisher and the discovery payload builder use is produced
here, so the topic layout of the design lives in exactly one place.

Two decisions in this module are load-bearing.

**The availability model has two levels.** Requirement 6.6 asks the last will to mark
*every* device offline, but MQTT permits exactly one will message. So every entity
declares two availability topics with ``availability_mode: all`` — the process-wide
bridge topic (:func:`bridge_availability_topic`, the will target) and its own device
topic (:func:`device_availability_topic`). One will published to the bridge topic makes
every entity unavailable at once, while per-device topics still tell an unreachable
server apart from a stopped addon.

**A unique identifier is derived from the device identifier and the entity role, and
from nothing else.** Requirement 6.11 exists so that a restart reuses the existing Home
Assistant entities rather than orphaning them and creating duplicates. That holds only
if the derivation excludes every value that can change between runs: the broker host and
port, credentials, the poll interval, the plugin identifier, the addon version, a run
counter, and the wall clock. :func:`unique_id` therefore takes exactly two arguments,
which is the cheapest way to make an accidental dependency impossible rather than merely
unlikely.

Lowercasing the identifier in the derivation is safe because the parser accepts only
``[A-Za-z0-9_-]`` identifiers (Requirement 2.12) and treats identifiers
case-insensitively when detecting duplicates (Requirement 2.5). Two distinct accepted
identifiers therefore cannot collide after lowercasing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from remote_energy_meter.config.models import DEVICE_ID_PATTERN, MAX_DEVICE_ID_LENGTH

if TYPE_CHECKING:  # pragma: no cover - typing only
    from remote_energy_meter.config.models import MqttSettings

__all__ = [
    "BRIDGE_NODE",
    "DEFAULT_BASE_TOPIC",
    "DEFAULT_DISCOVERY_PREFIX",
    "DISCOVERY_COMPONENT",
    "PAYLOAD_AVAILABLE",
    "PAYLOAD_NOT_AVAILABLE",
    "UNIQUE_ID_PREFIX",
    "DeviceTopics",
    "EntityRole",
    "attributes_topic",
    "bridge_availability_topic",
    "device_availability_topic",
    "device_node_id",
    "device_topics",
    "device_topics_for",
    "discovery_config_topic",
    "integration_inventory_topic",
    "state_topic",
    "unique_id",
]

# --------------------------------------------------------------------------------------
# Fixed vocabulary
# --------------------------------------------------------------------------------------

#: Default root of every topic this addon publishes to. Configurable through
#: ``MqttSettings.base_topic``.
DEFAULT_BASE_TOPIC: Final = "remote_energy_meter"

#: Default root Home Assistant watches for MQTT_Discovery configuration payloads.
#: Configurable through ``MqttSettings.discovery_prefix``.
DEFAULT_DISCOVERY_PREFIX: Final = "homeassistant"

#: Topic level standing for the metering process itself rather than one metered device.
#: Reserved: a device identifier equal to it would collide with the bridge topics, so
#: :func:`device_availability_topic` rejects it.
BRIDGE_NODE: Final = "bridge"

#: Home Assistant discovery component both entities belong to. Power and energy are both
#: sensors, which is why one component name suffices.
DISCOVERY_COMPONENT: Final = "sensor"

#: Prefix carried by every derived node and entity identifier, so this addon's entities
#: are recognizable in Home Assistant and cannot collide with another integration's.
UNIQUE_ID_PREFIX: Final = "rem"

#: The two Availability_State values (Requirements 6.7, 6.8).
PAYLOAD_AVAILABLE: Final = "online"
PAYLOAD_NOT_AVAILABLE: Final = "offline"

_AVAILABILITY_LEAF: Final = "availability"
_STATE_LEAF: Final = "state"
_ATTRIBUTES_LEAF: Final = "attributes"
_CONFIG_LEAF: Final = "config"

_DEVICE_ID_RE: Final = re.compile(DEVICE_ID_PATTERN)

#: MQTT single-level and multi-level wildcards. Legal in a subscription filter, never in
#: a topic a client publishes to, so a configured prefix carrying one is rejected here
#: rather than at the broker.
_WILDCARDS: Final = ("+", "#")


class EntityRole(StrEnum):
    """Which of a device's two entities a topic or identifier belongs to.

    Requirement 6.1 gives each Device_Entry exactly one power sensor and exactly one
    energy sensor, so the role is a closed set of two. It is an enum rather than a string
    so that ``mypy --strict`` rejects a typo'd role at build time instead of producing a
    topic no subscriber will ever match.
    """

    POWER = "power"
    ENERGY = "energy"


# --------------------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------------------


def _validated_device_id(device_id: str) -> str:
    """Return ``device_id`` unchanged, or raise if it cannot appear in a topic.

    The parser already rejects malformed identifiers (Requirement 2.12), so reaching here
    with one is a programming error rather than a configuration error — hence a raise
    rather than a classified rejection. The check stays because it is the boundary that
    keeps a topic separator or an MQTT wildcard out of a published topic.
    """
    if not _DEVICE_ID_RE.fullmatch(device_id) or len(device_id) > MAX_DEVICE_ID_LENGTH:
        raise ValueError(
            f"device identifier {device_id!r} is not usable in a topic: "
            f"expected 1 to {MAX_DEVICE_ID_LENGTH} characters from letters, digits, hyphen, underscore"
        )
    return device_id


def _validated_prefix(value: str, *, field_name: str) -> str:
    """Normalize a configured topic prefix: trim whitespace and boundary separators.

    ``MqttSettings`` does not constrain ``base_topic`` or ``discovery_prefix`` beyond
    being strings, so normalization happens here. A leading or trailing ``/`` would
    produce an empty topic level, and a wildcard would make the topic invalid to publish
    to at all.
    """
    prefix = value.strip().strip("/")
    if not prefix:
        raise ValueError(f"{field_name} must contain at least one topic level")
    if any(wildcard in prefix for wildcard in _WILDCARDS):
        raise ValueError(f"{field_name} {value!r} must not contain an MQTT wildcard ({' or '.join(_WILDCARDS)})")
    if "\x00" in prefix:
        raise ValueError(f"{field_name} must not contain a null character")
    return prefix


# --------------------------------------------------------------------------------------
# Identifier derivation (Requirement 6.11)
# --------------------------------------------------------------------------------------


def device_node_id(device_id: str) -> str:
    """Return the discovery node identifier for a device: ``rem_<device_id lowercased>``.

    Shared by the discovery topic of both entities and by the ``device.identifiers``
    block that ties them to one Home Assistant device (Requirement 6.1).
    """
    return f"{UNIQUE_ID_PREFIX}_{_validated_device_id(device_id).lower()}"


def unique_id(device_id: str, role: EntityRole) -> str:
    """Return the entity unique identifier: ``rem_<device_id lowercased>_<role>``.

    Derived from the device identifier and the entity role only, so two runs over the
    same Configuration_Object produce identical identifiers and Home Assistant reuses the
    existing entities across a restart (Requirement 6.11). Nothing run-scoped — host,
    port, credentials, poll interval, plugin identifier, clock — participates, and the
    signature is what enforces that.
    """
    return f"{device_node_id(device_id)}_{role.value}"


# --------------------------------------------------------------------------------------
# Topic derivation
# --------------------------------------------------------------------------------------


def bridge_availability_topic(base_topic: str = DEFAULT_BASE_TOPIC) -> str:
    """Return the process-wide availability topic, which is also the last will target.

    One will message published here marks every entity unavailable at once, which is how
    Requirement 6.6 is satisfied under MQTT's one-will-per-connection limit.
    """
    return f"{_validated_prefix(base_topic, field_name='base_topic')}/{BRIDGE_NODE}/{_AVAILABILITY_LEAF}"


def integration_inventory_topic(base_topic: str = DEFAULT_BASE_TOPIC) -> str:
    """Return the retained device inventory topic consumed by the companion integration."""
    return f"{_validated_prefix(base_topic, field_name='base_topic')}/integration/devices"


def device_availability_topic(device_id: str, base_topic: str = DEFAULT_BASE_TOPIC) -> str:
    """Return the per-device Availability_State topic (Requirements 6.7, 6.8)."""
    checked = _validated_device_id(device_id)
    if checked.lower() == BRIDGE_NODE:
        raise ValueError(f"device identifier {device_id!r} is reserved: it collides with the bridge availability topic")
    return f"{_validated_prefix(base_topic, field_name='base_topic')}/{checked}/{_AVAILABILITY_LEAF}"


def state_topic(device_id: str, role: EntityRole, base_topic: str = DEFAULT_BASE_TOPIC) -> str:
    """Return the state topic of one entity of one device."""
    prefix = _validated_prefix(base_topic, field_name="base_topic")
    return f"{prefix}/{_validated_device_id(device_id)}/{role.value}/{_STATE_LEAF}"


def attributes_topic(device_id: str, role: EntityRole, base_topic: str = DEFAULT_BASE_TOPIC) -> str:
    """Return the JSON attributes topic of one entity, carrying diagnostics.

    The power entity's attributes topic carries the read duration in whole milliseconds
    (Requirement 9.5).
    """
    prefix = _validated_prefix(base_topic, field_name="base_topic")
    return f"{prefix}/{_validated_device_id(device_id)}/{role.value}/{_ATTRIBUTES_LEAF}"


def discovery_config_topic(
    device_id: str,
    role: EntityRole,
    discovery_prefix: str = DEFAULT_DISCOVERY_PREFIX,
) -> str:
    """Return the MQTT_Discovery configuration topic of one entity of one device.

    Both entities sit under one node identifier, which is what makes them one Home
    Assistant device (Requirement 6.1). Payloads published here are retained, so Home
    Assistant recreates the entities after its own restart (Requirement 6.5).
    """
    prefix = _validated_prefix(discovery_prefix, field_name="discovery_prefix")
    return f"{prefix}/{DISCOVERY_COMPONENT}/{device_node_id(device_id)}/{role.value}/{_CONFIG_LEAF}"


# --------------------------------------------------------------------------------------
# Bundled per-device view
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeviceTopics:
    """Every topic and identifier one metered device needs, derived once.

    The discovery payload builder and the publisher both need the same set, and deriving
    it once per device keeps the topic layout out of both. The bridge topic is carried
    here too, because every entity's availability list references it even though it is
    not per-device.
    """

    device_id: str
    node_id: str
    bridge_availability: str
    availability: str
    power_state: str
    energy_state: str
    power_attributes: str
    power_discovery: str
    energy_discovery: str
    power_unique_id: str
    energy_unique_id: str

    def state(self, role: EntityRole) -> str:
        """Return the state topic for ``role``."""
        match role:
            case EntityRole.POWER:
                return self.power_state
            case EntityRole.ENERGY:
                return self.energy_state

    def discovery(self, role: EntityRole) -> str:
        """Return the MQTT_Discovery configuration topic for ``role``."""
        match role:
            case EntityRole.POWER:
                return self.power_discovery
            case EntityRole.ENERGY:
                return self.energy_discovery

    def unique_id(self, role: EntityRole) -> str:
        """Return the entity unique identifier for ``role``."""
        match role:
            case EntityRole.POWER:
                return self.power_unique_id
            case EntityRole.ENERGY:
                return self.energy_unique_id


def device_topics(
    device_id: str,
    base_topic: str = DEFAULT_BASE_TOPIC,
    discovery_prefix: str = DEFAULT_DISCOVERY_PREFIX,
) -> DeviceTopics:
    """Derive the full topic and identifier set for one device."""
    return DeviceTopics(
        device_id=device_id,
        node_id=device_node_id(device_id),
        bridge_availability=bridge_availability_topic(base_topic),
        availability=device_availability_topic(device_id, base_topic),
        power_state=state_topic(device_id, EntityRole.POWER, base_topic),
        energy_state=state_topic(device_id, EntityRole.ENERGY, base_topic),
        power_attributes=attributes_topic(device_id, EntityRole.POWER, base_topic),
        power_discovery=discovery_config_topic(device_id, EntityRole.POWER, discovery_prefix),
        energy_discovery=discovery_config_topic(device_id, EntityRole.ENERGY, discovery_prefix),
        power_unique_id=unique_id(device_id, EntityRole.POWER),
        energy_unique_id=unique_id(device_id, EntityRole.ENERGY),
    )


def device_topics_for(settings: MqttSettings, device_id: str) -> DeviceTopics:
    """Derive the full topic and identifier set for one device from the MQTT settings.

    Only the two topic prefixes are read. Host, port, credentials, and TLS flag are not,
    which is what keeps the derived identifiers stable across a configuration change to
    any of them (Requirement 6.11).
    """
    return device_topics(device_id, settings.base_topic, settings.discovery_prefix)
