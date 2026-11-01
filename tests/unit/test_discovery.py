"""Unit coverage for the retained companion-integration inventory protocol.

Generic MQTT entity discovery is intentionally absent. Legacy discovery topics remain only so
empty retained payloads can remove entities created by older releases.
"""

from __future__ import annotations

import dataclasses
import json

import pytest
from pydantic import SecretStr

from remote_energy_meter.config.models import MqttSettings
from remote_energy_meter.mqtt.inventory import PublishableDevice, inventory_document, inventory_payload
from remote_energy_meter.mqtt.topics import device_topics, device_topics_for

DEVICE = PublishableDevice(
    device_id="poweredge_a",
    display_name="PowerEdge A",
    manufacturer="Dell Inc.",
    model="PowerEdge R720",
    plugin_id="idrac",
)
TOPICS = device_topics(DEVICE.device_id)


def test_publishable_view_is_credential_free_by_shape() -> None:
    assert {field.name for field in dataclasses.fields(PublishableDevice)} == {
        "device_id",
        "display_name",
        "manufacturer",
        "model",
        "plugin_id",
    }
    with pytest.raises(TypeError):
        PublishableDevice(device_id="server", password="not accepted")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        PublishableDevice(device_id="server", host="not accepted")  # type: ignore[call-arg]


def test_inventory_document_pins_exact_metadata_and_topic_shape() -> None:
    document = inventory_document(
        [DEVICE],
        {DEVICE.device_id: TOPICS},
        base_topic="remote_energy_meter",
    )

    assert document == {
        "schema_version": 1,
        "instance_id": "remote_energy_meter",
        "base_topic": "remote_energy_meter",
        "bridge_availability_topic": "remote_energy_meter/bridge/availability",
        "devices": [
            {
                "device_id": "poweredge_a",
                "name": "PowerEdge A",
                "plugin_id": "idrac",
                "availability_topic": "remote_energy_meter/poweredge_a/availability",
                "power_state_topic": "remote_energy_meter/poweredge_a/power/state",
                "energy_state_topic": "remote_energy_meter/poweredge_a/energy/state",
                "power_attributes_topic": "remote_energy_meter/poweredge_a/power/attributes",
                "manufacturer": "Dell Inc.",
                "model": "PowerEdge R720",
            }
        ],
    }


def test_inventory_serialization_is_deterministic_json_round_trip() -> None:
    first = inventory_payload([DEVICE], {DEVICE.device_id: TOPICS}, base_topic="remote_energy_meter")
    second = inventory_payload([DEVICE], {DEVICE.device_id: TOPICS}, base_topic="remote_energy_meter")

    assert first == second
    assert json.loads(first) == inventory_document(
        [DEVICE],
        {DEVICE.device_id: TOPICS},
        base_topic="remote_energy_meter",
    )


def test_inventory_excludes_broker_and_device_connection_material() -> None:
    broker_username = "broker-user-never-publish"
    broker_password = "broker-password-never-publish"
    settings = MqttSettings(
        host="broker.internal.example",
        port=1883,
        username=SecretStr(broker_username),
        password=SecretStr(broker_password),
    )
    topics = device_topics_for(settings, DEVICE.device_id)

    payload = inventory_payload([DEVICE], {DEVICE.device_id: topics}, base_topic=settings.base_topic)
    serialized = payload.casefold()

    for forbidden_value in (broker_username, broker_password, settings.host):
        assert forbidden_value.casefold() not in serialized
    for forbidden_key in ("host", "username", "password", "secret", "token", "params"):
        assert f'"{forbidden_key}"' not in serialized


def test_inventory_refuses_metadata_topic_cross_wiring() -> None:
    with pytest.raises(ValueError, match="different devices"):
        inventory_document([DEVICE], {DEVICE.device_id: device_topics("poweredge_b")}, base_topic="remote_energy_meter")


def test_inventory_refuses_case_insensitive_duplicate_device_ids() -> None:
    duplicate = PublishableDevice(device_id="POWEREDGE_A", plugin_id="idrac")
    with pytest.raises(ValueError, match="duplicate"):
        inventory_document(
            [DEVICE, duplicate],
            {
                DEVICE.device_id: TOPICS,
                duplicate.device_id: device_topics(duplicate.device_id),
            },
            base_topic="remote_energy_meter",
        )
