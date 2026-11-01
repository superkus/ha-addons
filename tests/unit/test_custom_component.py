# mypy: ignore-errors
"""Isolated Home Assistant-style tests for the first-class companion integration."""
# ruff: noqa: E402  # Home Assistant stubs must be installed before importing the integration.

from __future__ import annotations

import inspect
from collections.abc import Callable

import pytest

from tests.fakes.homeassistant_runtime import (
    ConfigEntry,
    HassioServiceInfo,
    HomeAssistant,
    ReceiveMessage,
    install_homeassistant_stubs,
)

install_homeassistant_stubs()

from custom_components.remote_energy_meter import async_setup_entry, async_unload_entry
from custom_components.remote_energy_meter.config_flow import RemoteEnergyMeterConfigFlow
from custom_components.remote_energy_meter.const import (
    CONF_BASE_TOPIC,
    CONF_INSTANCE_ID,
    CONF_INVENTORY_TOPIC,
    CONF_SCHEMA_VERSION,
)
from custom_components.remote_energy_meter.coordinator import RemoteEnergyMeterCoordinator
from custom_components.remote_energy_meter.diagnostics import async_get_config_entry_diagnostics
from custom_components.remote_energy_meter.sensor import RemoteEnergyMeterSensor
from custom_components.remote_energy_meter.sensor import async_setup_entry as async_setup_sensors

from remote_energy_meter.mqtt.inventory import PublishableDevice, inventory_payload
from remote_energy_meter.mqtt.topics import device_topics, integration_inventory_topic


class FakeMqtt:
    """Record exact subscriptions and dispatch messages like Home Assistant MQTT."""

    def __init__(self) -> None:
        self.subscriptions: dict[str, list[Callable[[ReceiveMessage], object]]] = {}
        self.unsubscribed: list[str] = []

    async def async_subscribe(
        self,
        _hass: HomeAssistant,
        topic: str,
        callback: Callable[[ReceiveMessage], object],
        qos: int = 0,
    ) -> Callable[[], None]:
        callbacks = self.subscriptions.setdefault(topic, [])
        callbacks.append(callback)

        def unsubscribe() -> None:
            if callback in callbacks:
                callbacks.remove(callback)
                self.unsubscribed.append(topic)

        return unsubscribe

    async def emit(self, topic: str, payload: str | bytes, *, retain: bool = False) -> None:
        message = ReceiveMessage(topic=topic, payload=payload, retain=retain, subscribed_topic=topic)
        for callback in tuple(self.subscriptions.get(topic, [])):
            result = callback(message)
            if inspect.isawaitable(result):
                await result


def _entry(base_topic: str = "remote_energy_meter") -> ConfigEntry:
    return ConfigEntry(
        {
            CONF_INSTANCE_ID: "remote_energy_meter",
            CONF_BASE_TOPIC: base_topic,
            CONF_INVENTORY_TOPIC: integration_inventory_topic(base_topic),
            CONF_SCHEMA_VERSION: 1,
        },
        unique_id="remote_energy_meter",
    )


def _payload(*device_ids: str, base_topic: str = "remote_energy_meter") -> str:
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
    topics = {device_id: device_topics(device_id, base_topic) for device_id in device_ids}
    return inventory_payload(devices, topics, base_topic=base_topic)


@pytest.fixture
def mqtt_runtime(monkeypatch: pytest.MonkeyPatch) -> FakeMqtt:
    from custom_components.remote_energy_meter import coordinator as coordinator_module

    runtime = FakeMqtt()
    monkeypatch.setattr(coordinator_module.mqtt, "async_wait_for_mqtt_client", lambda _hass: _true())
    monkeypatch.setattr(coordinator_module.mqtt, "async_subscribe", runtime.async_subscribe)
    return runtime


async def _true() -> bool:
    return True


async def test_hassio_flow_uses_instance_id_and_confirms_discovery() -> None:
    flow = RemoteEnergyMeterConfigFlow()
    info = HassioServiceInfo(
        name="Remote Energy Meter add-on",
        slug="local_remote_energy_meter",
        uuid="supervisor-message-uuid-must-not-be-used",
        config={
            "instance_id": "remote_energy_meter",
            "base_topic": "remote_energy_meter",
            "inventory_topic": "remote_energy_meter/integration/devices",
            "schema_version": 1,
        },
    )

    form = await flow.async_step_hassio(info)
    assert form["type"] == "form" and form["step_id"] == "hassio_confirm"
    assert flow._unique_id == "remote_energy_meter"
    assert flow._unique_id != info.uuid

    result = await flow.async_step_hassio_confirm({})
    assert result["type"] == "create_entry"
    assert result["data"] == info.config


async def test_hassio_rediscovery_updates_topics_and_reloads_the_one_existing_entry() -> None:
    flow = RemoteEnergyMeterConfigFlow()
    existing = _entry("old_base")
    existing.unique_id = "manual"
    flow.hass.config_entries.entries.append(existing)
    info = HassioServiceInfo(
        name="Remote Energy Meter",
        slug="local_remote_energy_meter",
        uuid="message-2",
        config={
            "instance_id": "stable-instance",
            "base_topic": "new_base",
            "inventory_topic": "new_base/integration/devices",
            "schema_version": 1,
        },
    )

    result = await flow.async_step_hassio(info)

    assert result == {"type": "abort", "reason": "already_configured"}
    assert existing.unique_id == "stable-instance"
    assert existing.data == info.config
    assert flow.hass.config_entries.reloads == [existing.entry_id]


async def test_manual_flow_normalizes_base_topic_and_refuses_duplicates() -> None:
    flow = RemoteEnergyMeterConfigFlow()
    result = await flow.async_step_user({"base_topic": " /meters/ "})

    assert result["type"] == "create_entry"
    assert result["data"] == {
        "instance_id": "remote_energy_meter",
        "base_topic": "meters",
        "inventory_topic": "meters/integration/devices",
        "schema_version": 1,
    }

    duplicate = RemoteEnergyMeterConfigFlow()
    duplicate.hass.config_entries.entries.append(_entry())
    assert await duplicate.async_step_user() == {"type": "abort", "reason": "already_configured"}


async def test_setup_and_unload_forward_sensor_platform_and_release_subscriptions(
    mqtt_runtime: FakeMqtt,
) -> None:
    hass = HomeAssistant()
    entry = _entry()

    assert await async_setup_entry(hass, entry) is True
    assert isinstance(entry.runtime_data, RemoteEnergyMeterCoordinator)
    assert hass.config_entries.forwarded == [(entry.entry_id, ("sensor",))]
    assert entry.data[CONF_INVENTORY_TOPIC] in mqtt_runtime.subscriptions

    assert await async_unload_entry(hass, entry) is True
    assert hass.config_entries.unloaded == [(entry.entry_id, ("sensor",))]
    assert entry.data[CONF_INVENTORY_TOPIC] in mqtt_runtime.unsubscribed


async def test_coordinator_dynamic_devices_entities_values_availability_and_malformed_messages(
    mqtt_runtime: FakeMqtt,
) -> None:
    hass = HomeAssistant()
    entry = _entry()
    coordinator = RemoteEnergyMeterCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    entities: list[RemoteEnergyMeterSensor] = []
    await async_setup_sensors(hass, entry, entities.extend)

    await mqtt_runtime.emit(entry.data[CONF_INVENTORY_TOPIC], _payload("server_a"), retain=True)
    assert len(entities) == 2
    assert {entity._attr_unique_id for entity in entities} == {"rem_server_a_power", "rem_server_a_energy"}

    # A later retained inventory adds entities without forwarding/reloading the platform.
    await mqtt_runtime.emit(entry.data[CONF_INVENTORY_TOPIC], _payload("server_a", "server_b"), retain=True)
    assert len(entities) == 4
    assert {entity._attr_unique_id for entity in entities} == {
        "rem_server_a_power",
        "rem_server_a_energy",
        "rem_server_b_power",
        "rem_server_b_energy",
    }

    topics = device_topics("server_a")
    power = next(entity for entity in entities if entity._attr_unique_id == "rem_server_a_power")
    energy = next(entity for entity in entities if entity._attr_unique_id == "rem_server_a_energy")
    assert power.available is False

    await mqtt_runtime.emit("remote_energy_meter/bridge/availability", "online")
    await mqtt_runtime.emit(topics.availability, "online")
    await mqtt_runtime.emit(topics.power_state, "123.4")
    await mqtt_runtime.emit(topics.energy_state, "4.250000")
    await mqtt_runtime.emit(topics.power_attributes, '{"duration_ms":87}')

    assert power.available is True and energy.available is True
    assert power.native_value == 123.4
    assert energy.native_value == 4.25
    assert power.extra_state_attributes == {"duration_ms": 87}
    assert energy.extra_state_attributes is None
    assert power._attr_device_class == "power"
    assert power._attr_native_unit_of_measurement == "W"
    assert power._attr_state_class == "measurement"
    assert energy._attr_device_class == "energy"
    assert energy._attr_native_unit_of_measurement == "kWh"
    assert energy._attr_state_class == "total_increasing"
    assert power.device_info["identifiers"] == {("remote_energy_meter", "server_a")}

    # Malformed inventory and state payloads are ignored without replacing valid state.
    await mqtt_runtime.emit(entry.data[CONF_INVENTORY_TOPIC], "not-json")
    await mqtt_runtime.emit(topics.power_state, "nan")
    await mqtt_runtime.emit(topics.energy_state, "-1")
    await mqtt_runtime.emit(topics.power_attributes, '{"duration_ms":"slow"}')
    await mqtt_runtime.emit(topics.availability, "unknown")
    assert power.native_value == 123.4
    assert energy.native_value == 4.25
    assert power.extra_state_attributes == {"duration_ms": 87}
    assert power.available is True

    await mqtt_runtime.emit("remote_energy_meter/bridge/availability", "offline")
    assert power.available is False and energy.available is False


async def test_diagnostics_include_metadata_and_availability_but_no_raw_payloads(
    mqtt_runtime: FakeMqtt,
) -> None:
    hass = HomeAssistant()
    entry = _entry()
    coordinator = RemoteEnergyMeterCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await mqtt_runtime.emit(entry.data[CONF_INVENTORY_TOPIC], _payload("server_a"), retain=True)

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["config"] == entry.data
    assert set(diagnostics["devices"]) == {"server_a"}
    assert diagnostics["devices"]["server_a"]["available"] is False
    assert "payload" not in str(diagnostics).casefold()
    assert all(term not in str(diagnostics).casefold() for term in ("password", "username", "secret"))
