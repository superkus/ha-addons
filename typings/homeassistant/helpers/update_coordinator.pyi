import logging
from collections.abc import Callable
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

class DataUpdateCoordinator[T]:
    hass: HomeAssistant
    config_entry: ConfigEntry[Any]
    data: T
    last_update_success: bool
    def __init__(
        self,
        hass: HomeAssistant,
        logger: logging.Logger,
        *,
        config_entry: ConfigEntry[Any] | None = ...,
        name: str | None = ...,
    ) -> None: ...
    async def _async_setup(self) -> None: ...
    async def _async_update_data(self) -> T: ...
    async def async_config_entry_first_refresh(self) -> None: ...
    def async_set_updated_data(self, data: T) -> None: ...
    def async_add_listener(self, update_callback: Callable[[], None]) -> Callable[[], None]: ...

class CoordinatorEntity[C]:
    coordinator: C
    def __init__(self, coordinator: C) -> None: ...
    @property
    def available(self) -> bool: ...
