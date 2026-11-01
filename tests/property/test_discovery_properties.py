"""Property 39: retained integration inventory is exact, stable, and credential-free."""

from __future__ import annotations

import json
import re
import string

from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import SecretStr

from remote_energy_meter.config.models import DEVICE_ID_PATTERN, MAX_DEVICE_ID_LENGTH, MqttSettings
from remote_energy_meter.mqtt.inventory import PublishableDevice, inventory_document, inventory_payload
from remote_energy_meter.mqtt.topics import BRIDGE_NODE, device_topics_for

_ID_RE = re.compile(DEVICE_ID_PATTERN)
_ID_ALPHABET = string.ascii_letters + string.digits + "_-"
_TOPIC_ALPHABET = string.ascii_lowercase + string.digits + "_-"
_CREDENTIAL_ALPHABET = string.ascii_letters + string.digits + "!@$%^&*"


def device_ids() -> st.SearchStrategy[str]:
    return st.text(_ID_ALPHABET, min_size=1, max_size=MAX_DEVICE_ID_LENGTH).filter(
        lambda value: _ID_RE.fullmatch(value) is not None and value.casefold() != BRIDGE_NODE
    )


@st.composite
def publishable_devices(draw: st.DrawFn) -> list[PublishableDevice]:
    """Generate the complete Property 39 domain: zero to fifty accepted devices."""
    ids = draw(st.lists(device_ids(), min_size=0, max_size=50, unique_by=str.casefold))
    public_text = st.text(string.ascii_letters + string.digits + " _-", min_size=1, max_size=24)
    optional_public_text = st.one_of(st.none(), public_text)
    display_names = draw(st.lists(st.one_of(st.none(), st.just(""), public_text), min_size=len(ids), max_size=len(ids)))
    manufacturers = draw(st.lists(optional_public_text, min_size=len(ids), max_size=len(ids)))
    models = draw(st.lists(optional_public_text, min_size=len(ids), max_size=len(ids)))
    plugin_ids = draw(
        st.lists(
            st.text(_TOPIC_ALPHABET, min_size=1, max_size=12),
            min_size=len(ids),
            max_size=len(ids),
        )
    )
    return [
        PublishableDevice(
            device_id=device_id,
            display_name=display_name,
            manufacturer=manufacturer,
            model=model,
            plugin_id=plugin_id,
        )
        for device_id, display_name, manufacturer, model, plugin_id in zip(
            ids,
            display_names,
            manufacturers,
            models,
            plugin_ids,
            strict=True,
        )
    ]


def topic_prefixes() -> st.SearchStrategy[str]:
    level = st.text(_TOPIC_ALPHABET, min_size=1, max_size=12)
    return st.lists(level, min_size=1, max_size=3).map("/".join)


def credential_values() -> st.SearchStrategy[str]:
    # Section signs cannot occur in generated public metadata, IDs, or topics, so an exact
    # match in the payload unambiguously identifies a credential leak.
    return st.text(_CREDENTIAL_ALPHABET, min_size=4, max_size=32).map(lambda value: f"§credential:{value}§")


def _all_mapping_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        keys = {str(key) for key in value}
        for child in value.values():
            keys.update(_all_mapping_keys(child))
        return keys
    if isinstance(value, list):
        nested_keys: set[str] = set()
        for child in value:
            nested_keys.update(_all_mapping_keys(child))
        return nested_keys
    return set()


# Feature: remote-energy-meter, Property 39: For any accepted device set and valid base topic,
# inventory serialization round-trips exactly with derived topics and no credential-bearing field or value.
# **Validates: Requirements B.2, B.3**
@settings(max_examples=200, deadline=None)
@given(
    devices=publishable_devices(),
    base_topic=topic_prefixes(),
    broker_username=credential_values(),
    broker_password=credential_values(),
)
def test_inventory_round_trips_with_exact_topics_and_no_credentials(
    devices: list[PublishableDevice],
    base_topic: str,
    broker_username: str,
    broker_password: str,
) -> None:
    mqtt = MqttSettings(
        host="broker.internal.example",
        port=1883,
        username=SecretStr(broker_username),
        password=SecretStr(broker_password),
        base_topic=base_topic,
    )
    topics = {device.device_id: device_topics_for(mqtt, device.device_id) for device in devices}
    expected_devices: list[dict[str, object]] = []
    for device in devices:
        derived = topics[device.device_id]
        expected: dict[str, object] = {
            "device_id": device.device_id,
            "name": device.display_name or device.device_id,
            "plugin_id": device.plugin_id or "unknown",
            "availability_topic": derived.availability,
            "power_state_topic": derived.power_state,
            "energy_state_topic": derived.energy_state,
            "power_attributes_topic": derived.power_attributes,
        }
        if device.manufacturer:
            expected["manufacturer"] = device.manufacturer
        if device.model:
            expected["model"] = device.model
        expected_devices.append(expected)
    expected_document: dict[str, object] = {
        "schema_version": 1,
        "instance_id": "remote_energy_meter",
        "base_topic": base_topic,
        "bridge_availability_topic": f"{base_topic}/bridge/availability",
        "devices": expected_devices,
    }

    payload = inventory_payload(devices, topics, base_topic=mqtt.base_topic)
    decoded = json.loads(payload)

    assert decoded == expected_document
    assert inventory_document(devices, topics, base_topic=mqtt.base_topic) == expected_document
    assert json.loads(json.dumps(decoded, ensure_ascii=False)) == decoded
    assert set(decoded) == {
        "schema_version",
        "instance_id",
        "base_topic",
        "bridge_availability_topic",
        "devices",
    }
    assert [device["device_id"] for device in decoded["devices"]] == [device.device_id for device in devices]
    for announced, source in zip(decoded["devices"], devices, strict=True):
        expected_fields = {
            "device_id",
            "name",
            "plugin_id",
            "availability_topic",
            "power_state_topic",
            "energy_state_topic",
            "power_attributes_topic",
        }
        if source.manufacturer:
            expected_fields.add("manufacturer")
        if source.model:
            expected_fields.add("model")
        assert set(announced) == expected_fields

    assert broker_username not in payload
    assert broker_password not in payload
    assert mqtt.host not in payload
    assert _all_mapping_keys(decoded).isdisjoint({"host", "username", "password", "secret", "token", "params"})
