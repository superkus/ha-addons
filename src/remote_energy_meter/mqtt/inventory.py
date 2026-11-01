"""Credential-free inventory protocol for the Home Assistant companion integration.

The add-on owns polling and MQTT transport.  Home Assistant owns entities.  On each broker
connection the add-on publishes one retained inventory document describing the accepted devices
and the exact topics the companion integration must subscribe to.  No plugin parameter mapping is
accepted by this module, so credentials cannot enter the protocol by construction.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from remote_energy_meter.config.models import DEVICE_ID_PATTERN, MAX_DEVICE_ID_LENGTH
from remote_energy_meter.mqtt.topics import bridge_availability_topic, integration_inventory_topic

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from remote_energy_meter.mqtt.topics import DeviceTopics

__all__ = [
    "DEFAULT_INSTANCE_ID",
    "INVENTORY_SCHEMA_VERSION",
    "PublishableDevice",
    "inventory_document",
    "inventory_payload",
]

INVENTORY_SCHEMA_VERSION: Final = 1
DEFAULT_INSTANCE_ID: Final = "remote_energy_meter"

_INSTANCE_ID_RE: Final = re.compile(DEVICE_ID_PATTERN)


@dataclass(frozen=True, slots=True)
class PublishableDevice:
    """Credential-free metadata for one accepted device."""

    device_id: str
    display_name: str | None = None
    manufacturer: str | None = None
    model: str | None = None
    plugin_id: str | None = None

    @property
    def name(self) -> str:
        """Return the Home Assistant display name, falling back to the stable ID."""
        return self.display_name if self.display_name else self.device_id


def _validate_instance_id(instance_id: str) -> str:
    if not _INSTANCE_ID_RE.fullmatch(instance_id) or len(instance_id) > MAX_DEVICE_ID_LENGTH:
        raise ValueError("instance_id must use 1-64 letters, digits, hyphens, or underscores")
    return instance_id


def _normalized_base_topic(base_topic: str) -> str:
    suffix = "/bridge/availability"
    bridge_topic = bridge_availability_topic(base_topic)
    if not bridge_topic.endswith(suffix):  # pragma: no cover - protected by the topic helper
        raise ValueError("bridge availability topic does not use the expected protocol suffix")
    return bridge_topic[: -len(suffix)]


def inventory_document(
    devices: Sequence[PublishableDevice],
    topics_by_device: Mapping[str, DeviceTopics],
    *,
    base_topic: str,
    instance_id: str = DEFAULT_INSTANCE_ID,
) -> dict[str, object]:
    """Build the versioned inventory object published to the integration topic."""
    normalized_base = _normalized_base_topic(base_topic)
    checked_instance = _validate_instance_id(instance_id)
    seen: set[str] = set()
    announced: list[dict[str, object]] = []

    for device in devices:
        folded = device.device_id.casefold()
        if folded in seen:
            raise ValueError("inventory contains duplicate device identifiers")
        seen.add(folded)
        try:
            topics = topics_by_device[device.device_id]
        except KeyError as error:
            raise ValueError("inventory device has no derived topic set") from error
        if topics.device_id != device.device_id:
            raise ValueError("inventory metadata and topic set identify different devices")

        item: dict[str, object] = {
            "device_id": device.device_id,
            "name": device.name,
            "plugin_id": device.plugin_id or "unknown",
            "availability_topic": topics.availability,
            "power_state_topic": topics.power_state,
            "energy_state_topic": topics.energy_state,
            "power_attributes_topic": topics.power_attributes,
        }
        if device.manufacturer:
            item["manufacturer"] = device.manufacturer
        if device.model:
            item["model"] = device.model
        announced.append(item)

    return {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "instance_id": checked_instance,
        "base_topic": normalized_base,
        "bridge_availability_topic": bridge_availability_topic(normalized_base),
        "devices": announced,
    }


def inventory_payload(
    devices: Sequence[PublishableDevice],
    topics_by_device: Mapping[str, DeviceTopics],
    *,
    base_topic: str,
    instance_id: str = DEFAULT_INSTANCE_ID,
) -> str:
    """Serialize a deterministic retained inventory payload."""
    document = inventory_document(
        devices,
        topics_by_device,
        base_topic=base_topic,
        instance_id=instance_id,
    )
    # Assert the publication topic is derivable now, at construction time, rather than failing
    # after legacy cleanup has already run on a live connection.
    integration_inventory_topic(str(document["base_topic"]))
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
