"""Integration-owned power and energy sensor entities."""

from __future__ import annotations

from typing import Literal

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, ROLE_ENERGY, ROLE_POWER
from .coordinator import DeviceState, RemoteEnergyMeterConfigEntry, RemoteEnergyMeterCoordinator
from .protocol import InventoryDevice, entity_unique_id

Role = Literal["power", "energy"]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RemoteEnergyMeterConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add current devices and any devices announced later without reloading the entry."""
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def add_new_devices() -> None:
        data = coordinator.data or {}
        new_ids = sorted(set(data) - known)
        if not new_ids:
            return
        entities: list[RemoteEnergyMeterSensor] = []
        for device_id in new_ids:
            descriptor = data[device_id].descriptor
            entities.extend(
                (
                    RemoteEnergyMeterSensor(coordinator, descriptor, ROLE_POWER),
                    RemoteEnergyMeterSensor(coordinator, descriptor, ROLE_ENERGY),
                )
            )
        known.update(new_ids)
        async_add_entities(entities)

    entry.async_on_unload(coordinator.async_add_listener(add_new_devices))
    add_new_devices()


class RemoteEnergyMeterSensor(CoordinatorEntity[RemoteEnergyMeterCoordinator], SensorEntity):
    """One power or cumulative-energy sensor owned by this integration."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: RemoteEnergyMeterCoordinator,
        descriptor: InventoryDevice,
        role: Role,
    ) -> None:
        super().__init__(coordinator)
        self._device_id = descriptor.device_id
        self._descriptor = descriptor
        self._role = role
        self._attr_unique_id = entity_unique_id(descriptor.device_id, role)
        self._attr_translation_key = role
        self._attr_native_unit_of_measurement: UnitOfPower | UnitOfEnergy
        if role == ROLE_POWER:
            self._attr_device_class = SensorDeviceClass.POWER
            self._attr_native_unit_of_measurement = UnitOfPower.WATT
            self._attr_state_class = SensorStateClass.MEASUREMENT
            self._attr_suggested_display_precision = 1
        else:
            self._attr_device_class = SensorDeviceClass.ENERGY
            self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
            self._attr_state_class = SensorStateClass.TOTAL_INCREASING
            self._attr_suggested_display_precision = 6

    @property
    def _state(self) -> DeviceState | None:
        data = self.coordinator.data or {}
        return data.get(self._device_id)

    @property
    def available(self) -> bool:
        state = self._state
        return super().available and state is not None and state.available

    @property
    def native_value(self) -> float | None:
        state = self._state
        if state is None:
            return None
        return state.power_w if self._role == ROLE_POWER else state.energy_kwh

    @property
    def extra_state_attributes(self) -> dict[str, int] | None:
        if self._role != ROLE_POWER:
            return None
        state = self._state
        if state is None or state.duration_ms is None:
            return None
        return {"duration_ms": state.duration_ms}

    @property
    def device_info(self) -> DeviceInfo:
        state = self._state
        descriptor = state.descriptor if state is not None else self._descriptor
        values: dict[str, object] = {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": descriptor.name,
        }
        if descriptor.manufacturer is not None:
            values["manufacturer"] = descriptor.manufacturer
        if descriptor.model is not None:
            values["model"] = descriptor.model
        return DeviceInfo(**values)
