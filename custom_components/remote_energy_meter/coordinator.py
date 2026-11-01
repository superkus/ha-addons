"""Push-only MQTT coordinator for Remote Energy Meter."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, override

from homeassistant.components import mqtt
from homeassistant.components.mqtt import ReceiveMessage
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import CONF_BASE_TOPIC, CONF_INSTANCE_ID, CONF_INVENTORY_TOPIC, CONF_SCHEMA_VERSION, DOMAIN
from .protocol import (
    Inventory,
    InventoryDevice,
    InventoryValidationError,
    parse_finite_number,
    parse_inventory_payload,
)

_LOGGER = logging.getLogger(__name__)
_MAX_POWER_W = 100_000.0


@dataclass(frozen=True, slots=True)
class DeviceState:
    """Current typed state accumulated from one device's MQTT topics."""

    descriptor: InventoryDevice
    bridge_online: bool = False
    device_online: bool = False
    power_w: float | None = None
    energy_kwh: float | None = None
    duration_ms: int | None = None

    @property
    def available(self) -> bool:
        """Both process and per-device availability must be online."""
        return self.bridge_online and self.device_online


class RemoteEnergyMeterCoordinator(DataUpdateCoordinator[dict[str, DeviceState]]):
    """Own the inventory subscription and all dynamically announced topic subscriptions."""

    config_entry: ConfigEntry[Any]

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry[Any]) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN)
        self.config_entry = entry
        self.instance_id: str = entry.data[CONF_INSTANCE_ID]
        self.base_topic: str = entry.data[CONF_BASE_TOPIC]
        self.inventory_topic: str = entry.data[CONF_INVENTORY_TOPIC]
        self.schema_version: int = entry.data[CONF_SCHEMA_VERSION]
        self._inventory: Inventory | None = None
        self._states: dict[str, DeviceState] = {}
        self._bridge_online = False
        self._inventory_unsubscribe: Callable[[], None] | None = None
        self._device_unsubscribes: list[Callable[[], None]] = []
        self._subscription_signature: tuple[tuple[str, ...], ...] | None = None

    @property
    def inventory(self) -> Inventory | None:
        return self._inventory

    @property
    def bridge_online(self) -> bool:
        return self._bridge_online

    @override
    async def _async_setup(self) -> None:
        if not await mqtt.async_wait_for_mqtt_client(self.hass):
            raise ConfigEntryNotReady("MQTT integration is not available")
        self._inventory_unsubscribe = await mqtt.async_subscribe(
            self.hass,
            self.inventory_topic,
            self._inventory_received,
            qos=1,
        )

    @override
    async def _async_update_data(self) -> dict[str, DeviceState]:
        """Return accumulated push state; no polling interval is configured."""
        return dict(self._states)

    def shutdown(self) -> None:
        """Unsubscribe every MQTT callback owned by this config entry."""
        if self._inventory_unsubscribe is not None:
            self._inventory_unsubscribe()
            self._inventory_unsubscribe = None
        self._unsubscribe_devices()

    def _unsubscribe_devices(self) -> None:
        callbacks, self._device_unsubscribes = self._device_unsubscribes, []
        for unsubscribe in callbacks:
            unsubscribe()
        self._subscription_signature = None

    async def _inventory_received(self, message: ReceiveMessage) -> None:
        try:
            inventory = parse_inventory_payload(
                message.payload,
                expected_instance_id=self.instance_id,
                expected_base_topic=self.base_topic,
                expected_schema_version=self.schema_version,
            )
        except InventoryValidationError as error:
            _LOGGER.warning("Ignoring malformed Remote Energy Meter inventory: %s", error)
            return

        signature = self._signature(inventory)
        if signature != self._subscription_signature:
            # Install typed device slots before awaiting subscriptions. A broker may deliver a
            # retained availability/state message immediately after any individual subscribe; the
            # callback must have a state object to update rather than dropping that first value.
            self._unsubscribe_devices()
            self._apply_inventory(inventory)
            try:
                subscriptions = await self._subscribe_inventory(inventory)
            except Exception as error:
                _LOGGER.warning(
                    "Inventory accepted but MQTT topics could not be subscribed (%s)",
                    type(error).__name__,
                )
                return
            self._device_unsubscribes = subscriptions
            self._subscription_signature = signature
            return
        self._apply_inventory(inventory)

    @staticmethod
    def _signature(inventory: Inventory) -> tuple[tuple[str, ...], ...]:
        return (
            (inventory.bridge_availability_topic,),
            *(
                (
                    device.device_id,
                    device.availability_topic,
                    device.power_state_topic,
                    device.energy_state_topic,
                    device.power_attributes_topic,
                )
                for device in inventory.devices
            ),
        )

    async def _subscribe_inventory(self, inventory: Inventory) -> list[Callable[[], None]]:
        unsubscribes: list[Callable[[], None]] = []
        try:
            unsubscribes.append(
                await mqtt.async_subscribe(self.hass, inventory.bridge_availability_topic, self._bridge_received, qos=1)
            )
            for device in inventory.devices:
                unsubscribes.append(
                    await mqtt.async_subscribe(
                        self.hass,
                        device.availability_topic,
                        self._availability_handler(device.device_id),
                        qos=1,
                    )
                )
                unsubscribes.append(
                    await mqtt.async_subscribe(
                        self.hass,
                        device.power_state_topic,
                        self._power_handler(device.device_id),
                    )
                )
                unsubscribes.append(
                    await mqtt.async_subscribe(
                        self.hass,
                        device.energy_state_topic,
                        self._energy_handler(device.device_id),
                        qos=1,
                    )
                )
                unsubscribes.append(
                    await mqtt.async_subscribe(
                        self.hass,
                        device.power_attributes_topic,
                        self._attributes_handler(device.device_id),
                    )
                )
        except BaseException:
            for unsubscribe in unsubscribes:
                unsubscribe()
            raise
        return unsubscribes

    def _apply_inventory(self, inventory: Inventory) -> None:
        bridge_changed = (
            self._inventory is not None
            and self._inventory.bridge_availability_topic != inventory.bridge_availability_topic
        )
        if bridge_changed:
            self._bridge_online = False
        updated: dict[str, DeviceState] = {}
        for descriptor in inventory.devices:
            old = self._states.get(descriptor.device_id)
            if old is not None and self._same_state_topics(old.descriptor, descriptor):
                updated[descriptor.device_id] = replace(
                    old,
                    descriptor=descriptor,
                    bridge_online=self._bridge_online,
                )
            else:
                updated[descriptor.device_id] = DeviceState(
                    descriptor=descriptor,
                    bridge_online=self._bridge_online,
                )
        self._inventory = inventory
        self._states = updated
        self.async_set_updated_data(dict(self._states))

    @staticmethod
    def _same_state_topics(left: InventoryDevice, right: InventoryDevice) -> bool:
        return (
            left.availability_topic,
            left.power_state_topic,
            left.energy_state_topic,
            left.power_attributes_topic,
        ) == (
            right.availability_topic,
            right.power_state_topic,
            right.energy_state_topic,
            right.power_attributes_topic,
        )

    @staticmethod
    def _payload_text(payload: object) -> str | None:
        if isinstance(payload, str):
            return payload
        if isinstance(payload, bytes):
            try:
                return payload.decode("utf-8")
            except UnicodeDecodeError:
                return None
        return None

    @callback
    def _bridge_received(self, message: ReceiveMessage) -> None:
        value = self._payload_text(message.payload)
        if value not in ("online", "offline"):
            return
        self._bridge_online = value == "online"
        self._states = {
            device_id: replace(state, bridge_online=self._bridge_online) for device_id, state in self._states.items()
        }
        self.async_set_updated_data(dict(self._states))

    def _availability_handler(self, device_id: str) -> Callable[[ReceiveMessage], None]:
        @callback
        def receive(message: ReceiveMessage) -> None:
            value = self._payload_text(message.payload)
            if value not in ("online", "offline"):
                return
            self._update_device(device_id, lambda state: replace(state, device_online=value == "online"))

        return receive

    def _power_handler(self, device_id: str) -> Callable[[ReceiveMessage], None]:
        @callback
        def receive(message: ReceiveMessage) -> None:
            try:
                value = parse_finite_number(message.payload, minimum=0.0, maximum=_MAX_POWER_W)
            except InventoryValidationError:
                return
            self._update_device(device_id, lambda state: replace(state, power_w=value))

        return receive

    def _energy_handler(self, device_id: str) -> Callable[[ReceiveMessage], None]:
        @callback
        def receive(message: ReceiveMessage) -> None:
            try:
                value = parse_finite_number(message.payload, minimum=0.0)
            except InventoryValidationError:
                return
            self._update_device(device_id, lambda state: replace(state, energy_kwh=value))

        return receive

    def _attributes_handler(self, device_id: str) -> Callable[[ReceiveMessage], None]:
        @callback
        def receive(message: ReceiveMessage) -> None:
            text = self._payload_text(message.payload)
            if text is None:
                return
            try:
                value: object = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                return
            if not isinstance(value, dict) or set(value) != {"duration_ms"}:
                return
            duration = value["duration_ms"]
            if duration is not None and (isinstance(duration, bool) or not isinstance(duration, int) or duration < 0):
                return
            self._update_device(device_id, lambda state: replace(state, duration_ms=duration))

        return receive

    def _update_device(self, device_id: str, update: Callable[[DeviceState], DeviceState]) -> None:
        state = self._states.get(device_id)
        if state is None:
            return
        self._states[device_id] = update(state)
        self.async_set_updated_data(dict(self._states))


type RemoteEnergyMeterConfigEntry = ConfigEntry[RemoteEnergyMeterCoordinator]
