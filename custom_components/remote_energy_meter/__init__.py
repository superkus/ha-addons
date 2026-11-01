"""Remote Energy Meter Home Assistant companion integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .coordinator import RemoteEnergyMeterCoordinator

    type RemoteEnergyMeterConfigEntry = ConfigEntry[RemoteEnergyMeterCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: RemoteEnergyMeterConfigEntry) -> bool:
    """Set up one push-only Remote Energy Meter config entry."""
    from homeassistant.const import Platform

    from .coordinator import RemoteEnergyMeterCoordinator

    platforms = (Platform.SENSOR,)
    coordinator = RemoteEnergyMeterCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, platforms)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: RemoteEnergyMeterConfigEntry) -> bool:
    """Unload platforms and all MQTT subscriptions."""
    from homeassistant.const import Platform

    if not await hass.config_entries.async_unload_platforms(entry, (Platform.SENSOR,)):
        return False
    entry.runtime_data.shutdown()
    return True
