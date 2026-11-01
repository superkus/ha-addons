"""Pure protocol tests for companion-integration inventory generation and validation."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

import pytest
from custom_components.remote_energy_meter.protocol import (
    InventoryValidationError,
    entity_unique_id,
    parse_inventory_payload,
)

from remote_energy_meter.mqtt.inventory import (
    DEFAULT_INSTANCE_ID,
    INVENTORY_SCHEMA_VERSION,
    PublishableDevice,
    inventory_document,
    inventory_payload,
)
from remote_energy_meter.mqtt.topics import DeviceTopics, device_topics, integration_inventory_topic

#: Any dotted-quad: the inventory carries device identities and topics, never a network address.
_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")


def _inventory(*device_ids: str) -> tuple[str, dict[str, DeviceTopics]]:
    devices = [
        PublishableDevice(
            device_id=device_id,
            display_name=f"Meter {device_id}",
            manufacturer="Example",
            model="Power source",
            plugin_id="test",
        )
        for device_id in device_ids
    ]
    topics = {device_id: device_topics(device_id) for device_id in device_ids}
    return inventory_payload(devices, topics, base_topic="remote_energy_meter"), topics


def test_generated_inventory_round_trips_through_the_companion_validator() -> None:
    payload, topics = _inventory("server_a", "server_b")

    parsed = parse_inventory_payload(
        payload,
        expected_instance_id=DEFAULT_INSTANCE_ID,
        expected_base_topic="remote_energy_meter",
        expected_schema_version=INVENTORY_SCHEMA_VERSION,
    )

    assert parsed.schema_version == 1
    assert parsed.instance_id == "remote_energy_meter"
    assert parsed.bridge_availability_topic == "remote_energy_meter/bridge/availability"
    assert [device.device_id for device in parsed.devices] == ["server_a", "server_b"]
    for device in parsed.devices:
        derived = topics[device.device_id]
        assert device.availability_topic == derived.availability
        assert device.power_state_topic == derived.power_state
        assert device.energy_state_topic == derived.energy_state
        assert device.power_attributes_topic == derived.power_attributes


def test_inventory_document_contains_only_credential_free_metadata_and_topics() -> None:
    device = PublishableDevice(device_id="server_a", display_name="Server A", plugin_id="idrac")
    topics = device_topics("server_a")
    document = inventory_document(
        [device],
        {device.device_id: topics},
        base_topic="remote_energy_meter",
    )
    text = json.dumps(document).casefold()

    assert set(document) == {
        "schema_version",
        "instance_id",
        "base_topic",
        "bridge_availability_topic",
        "devices",
    }
    assert all(term not in text for term in ("password", "username", "secret", "token"))
    assert _IPV4.search(text) is None, "the inventory must not expose any device network address"
    announced = document["devices"]
    assert isinstance(announced, list)
    assert set(announced[0]) == {
        "device_id",
        "name",
        "plugin_id",
        "availability_topic",
        "power_state_topic",
        "energy_state_topic",
        "power_attributes_topic",
    }


@pytest.mark.parametrize(
    "mutation",
    [
        lambda data: data.update(schema_version=2),
        lambda data: data.update(instance_id="other"),
        lambda data: data.update(extra="not allowed"),
        lambda data: data["devices"][0].update(password="must never be accepted"),
        lambda data: data["devices"][0].update(power_state_topic="remote_energy_meter/other/power/state"),
        lambda data: data["devices"].append(dict(data["devices"][0])),
    ],
)
def test_malformed_or_cross_wired_inventory_is_rejected(
    mutation: Callable[[dict[str, Any]], None],
) -> None:
    payload, _ = _inventory("server_a")
    data: dict[str, Any] = json.loads(payload)
    mutation(data)

    with pytest.raises(InventoryValidationError):
        parse_inventory_payload(
            json.dumps(data),
            expected_instance_id=DEFAULT_INSTANCE_ID,
            expected_base_topic="remote_energy_meter",
        )


@pytest.mark.parametrize("payload", ["", "not-json", "[]", b"\xff", '{"schema_version":true}'])
def test_arbitrary_malformed_payloads_are_rejected_without_other_exceptions(payload: str | bytes) -> None:
    with pytest.raises(InventoryValidationError):
        parse_inventory_payload(payload)


def test_inventory_and_entity_identifiers_are_deterministic() -> None:
    assert integration_inventory_topic(" /meters/ ") == "meters/integration/devices"
    assert entity_unique_id("Server_A", "power") == "rem_server_a_power"
    assert entity_unique_id("Server_A", "energy") == "rem_server_a_energy"
