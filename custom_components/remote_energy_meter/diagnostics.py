"""Diagnostics for Remote Energy Meter."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from .const import CONF_BASE_TOPIC, CONF_INSTANCE_ID, CONF_INVENTORY_TOPIC, CONF_SCHEMA_VERSION
from .coordinator import RemoteEnergyMeterConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: RemoteEnergyMeterConfigEntry,
) -> dict[str, Any]:
    """Return credential-free metadata and typed availability, with no raw MQTT payloads."""
    coordinator = entry.runtime_data
    data = coordinator.data or {}
    return {
        "config": {
            CONF_INSTANCE_ID: entry.data[CONF_INSTANCE_ID],
            CONF_BASE_TOPIC: entry.data[CONF_BASE_TOPIC],
            CONF_INVENTORY_TOPIC: entry.data[CONF_INVENTORY_TOPIC],
            CONF_SCHEMA_VERSION: entry.data[CONF_SCHEMA_VERSION],
        },
        "bridge_available": coordinator.bridge_online,
        "devices": {
            device_id: {
                "available": state.available,
                "device_available": state.device_online,
                "power_state_received": state.power_w is not None,
                "energy_state_received": state.energy_kwh is not None,
                "plugin_id": state.descriptor.plugin_id,
            }
            for device_id, state in sorted(data.items())
        },
    }
