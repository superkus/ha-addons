# mypy: ignore-errors
"""Small executable Home Assistant API surface for custom-component unit tests.

The production integration imports Home Assistant normally.  The project test environment does not
install Home Assistant, so these stubs model the current config-entry, coordinator, entity, and MQTT
contracts without adding or changing the prepared environment.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from enum import StrEnum
from types import ModuleType
from typing import Any, ClassVar


class _Package(ModuleType):
    __path__: ClassVar[list[str]] = []


class _Marker:
    def __init__(self, key: object, default: object = None) -> None:
        self.key = key
        self.default = default

    def __hash__(self) -> int:
        return hash((self.key, self.default))


class Schema:
    def __init__(self, schema: object) -> None:
        self.schema = schema


class Required(_Marker):
    pass


class Optional(_Marker):
    pass


class FakeConfigEntries:
    def __init__(self) -> None:
        self.entries: list[ConfigEntry] = []
        self.forwarded: list[tuple[str, tuple[object, ...]]] = []
        self.unloaded: list[tuple[str, tuple[object, ...]]] = []
        self.reloads: list[str] = []
        self.unload_result = True

    async def async_forward_entry_setups(self, entry: ConfigEntry, platforms: tuple[object, ...]) -> None:
        self.forwarded.append((entry.entry_id, tuple(platforms)))

    async def async_unload_platforms(self, entry: ConfigEntry, platforms: tuple[object, ...]) -> bool:
        self.unloaded.append((entry.entry_id, tuple(platforms)))
        return self.unload_result

    def async_schedule_reload(self, entry_id: str) -> None:
        self.reloads.append(entry_id)


class HomeAssistant:
    def __init__(self) -> None:
        self.config_entries = FakeConfigEntries()
        self.data: dict[str, object] = {}


def callback(function: Any) -> Any:
    return function


class ConfigEntry:
    def __class_getitem__(cls, _item: object) -> type[ConfigEntry]:
        return cls

    def __init__(
        self,
        data: dict[str, Any],
        *,
        unique_id: str | None = None,
        title: str = "Remote Energy Meter",
        entry_id: str = "entry-1",
    ) -> None:
        self.data = dict(data)
        self.unique_id = unique_id
        self.title = title
        self.entry_id = entry_id
        self.runtime_data: Any = None
        self._unload_callbacks: list[Any] = []

    def async_on_unload(self, callback_function: Any) -> Any:
        self._unload_callbacks.append(callback_function)
        return callback_function

    def run_unload_callbacks(self) -> None:
        callbacks, self._unload_callbacks = self._unload_callbacks, []
        for callback_function in callbacks:
            callback_function()


class ConfigFlow:
    VERSION = 1

    def __init_subclass__(cls, *, domain: str | None = None, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        cls.domain = domain

    def __init__(self) -> None:
        self.hass = HomeAssistant()
        self.context: dict[str, Any] = {}
        self._unique_id: str | None = None

    async def async_set_unique_id(self, unique_id: str) -> None:
        self._unique_id = unique_id

    def _async_current_entries(self, **_kwargs: object) -> list[ConfigEntry]:
        return list(self.hass.config_entries.entries)

    def async_show_form(self, *, step_id: str, **kwargs: object) -> dict[str, object]:
        return {"type": "form", "step_id": step_id, **kwargs}

    def async_abort(self, *, reason: str) -> dict[str, object]:
        return {"type": "abort", "reason": reason}

    def async_create_entry(self, *, title: str, data: dict[str, Any]) -> dict[str, object]:
        return {"type": "create_entry", "title": title, "data": data}

    def async_update_reload_and_abort(
        self,
        entry: ConfigEntry,
        *,
        unique_id: str | None = None,
        title: str | None = None,
        data: dict[str, Any] | None = None,
        data_updates: dict[str, Any] | None = None,
        reload_even_if_entry_is_unchanged: bool = True,
        reason: str = "already_configured",
    ) -> dict[str, object]:
        before = (entry.unique_id, entry.title, dict(entry.data))
        if unique_id is not None:
            entry.unique_id = unique_id
        if title is not None:
            entry.title = title
        if data is not None:
            entry.data = dict(data)
        if data_updates is not None:
            entry.data.update(data_updates)
        after = (entry.unique_id, entry.title, dict(entry.data))
        if reload_even_if_entry_is_unchanged or before != after:
            self.hass.config_entries.async_schedule_reload(entry.entry_id)
        return self.async_abort(reason=reason)


ConfigFlowResult = dict[str, object]


@dataclass(slots=True)
class HassioServiceInfo:
    config: dict[str, Any]
    name: str
    slug: str
    uuid: str


class ConfigEntryNotReady(RuntimeError):  # noqa: N818 - exact Home Assistant public name
    pass


class HomeAssistantError(RuntimeError):
    pass


@dataclass(slots=True)
class ReceiveMessage:
    topic: str
    payload: str | bytes
    qos: int = 0
    retain: bool = False
    subscribed_topic: str = ""
    timestamp: float = 0.0


async def _wait_for_mqtt(_hass: HomeAssistant) -> bool:
    return True


async def _unconfigured_subscribe(*_args: object, **_kwargs: object) -> Any:
    raise AssertionError("test must install an MQTT subscription recorder")


class DataUpdateCoordinator:
    def __class_getitem__(cls, _item: object) -> type[DataUpdateCoordinator]:
        return cls

    def __init__(
        self,
        hass: HomeAssistant,
        _logger: object,
        *,
        config_entry: ConfigEntry | None = None,
        name: str | None = None,
        **_kwargs: object,
    ) -> None:
        self.hass = hass
        self.config_entry = config_entry
        self.name = name
        self.data: Any = None
        self.last_update_success = True
        self._listeners: list[Any] = []

    async def async_config_entry_first_refresh(self) -> None:
        setup = getattr(self, "_async_setup", None)
        if setup is not None:
            await setup()
        self.data = await self._async_update_data()

    def async_set_updated_data(self, data: Any) -> None:
        self.data = data
        self.last_update_success = True
        for listener in tuple(self._listeners):
            listener()

    def async_add_listener(self, listener: Any) -> Any:
        self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe


class CoordinatorEntity:
    def __class_getitem__(cls, _item: object) -> type[CoordinatorEntity]:
        return cls

    def __init__(self, coordinator: DataUpdateCoordinator) -> None:
        self.coordinator = coordinator

    @property
    def available(self) -> bool:
        return bool(self.coordinator.last_update_success)


class SensorEntity:
    pass


class SensorDeviceClass(StrEnum):
    POWER = "power"
    ENERGY = "energy"


class SensorStateClass(StrEnum):
    MEASUREMENT = "measurement"
    TOTAL_INCREASING = "total_increasing"


class UnitOfPower(StrEnum):
    WATT = "W"


class UnitOfEnergy(StrEnum):
    KILO_WATT_HOUR = "kWh"


class Platform(StrEnum):
    SENSOR = "sensor"


class DeviceInfo(dict[str, object]):
    def __init__(self, **kwargs: object) -> None:
        super().__init__(kwargs)


AddEntitiesCallback = Any


def install_homeassistant_stubs() -> None:
    """Install the stubs once, before importing the custom component."""
    if "homeassistant" in sys.modules:
        return

    voluptuous = ModuleType("voluptuous")
    voluptuous.Schema = Schema
    voluptuous.Required = Required
    voluptuous.Optional = Optional

    homeassistant = _Package("homeassistant")
    components = _Package("homeassistant.components")
    helpers = _Package("homeassistant.helpers")
    service_info = _Package("homeassistant.helpers.service_info")

    config_entries = ModuleType("homeassistant.config_entries")
    config_entries.ConfigEntry = ConfigEntry
    config_entries.ConfigFlow = ConfigFlow
    config_entries.ConfigFlowResult = ConfigFlowResult

    core = ModuleType("homeassistant.core")
    core.HomeAssistant = HomeAssistant
    core.callback = callback

    const = ModuleType("homeassistant.const")
    const.Platform = Platform
    const.UnitOfEnergy = UnitOfEnergy
    const.UnitOfPower = UnitOfPower

    exceptions = ModuleType("homeassistant.exceptions")
    exceptions.ConfigEntryNotReady = ConfigEntryNotReady
    exceptions.HomeAssistantError = HomeAssistantError

    mqtt = ModuleType("homeassistant.components.mqtt")
    mqtt.ReceiveMessage = ReceiveMessage
    mqtt.async_wait_for_mqtt_client = _wait_for_mqtt
    mqtt.async_subscribe = _unconfigured_subscribe

    sensor = ModuleType("homeassistant.components.sensor")
    sensor.SensorDeviceClass = SensorDeviceClass
    sensor.SensorEntity = SensorEntity
    sensor.SensorStateClass = SensorStateClass

    hassio = ModuleType("homeassistant.helpers.service_info.hassio")
    hassio.HassioServiceInfo = HassioServiceInfo

    update_coordinator = ModuleType("homeassistant.helpers.update_coordinator")
    update_coordinator.DataUpdateCoordinator = DataUpdateCoordinator
    update_coordinator.CoordinatorEntity = CoordinatorEntity

    device_registry = ModuleType("homeassistant.helpers.device_registry")
    device_registry.DeviceInfo = DeviceInfo

    entity_platform = ModuleType("homeassistant.helpers.entity_platform")
    entity_platform.AddEntitiesCallback = AddEntitiesCallback

    components.mqtt = mqtt
    homeassistant.components = components
    homeassistant.helpers = helpers

    modules = {
        "voluptuous": voluptuous,
        "homeassistant": homeassistant,
        "homeassistant.components": components,
        "homeassistant.components.mqtt": mqtt,
        "homeassistant.components.sensor": sensor,
        "homeassistant.config_entries": config_entries,
        "homeassistant.const": const,
        "homeassistant.core": core,
        "homeassistant.exceptions": exceptions,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.device_registry": device_registry,
        "homeassistant.helpers.entity_platform": entity_platform,
        "homeassistant.helpers.service_info": service_info,
        "homeassistant.helpers.service_info.hassio": hassio,
        "homeassistant.helpers.update_coordinator": update_coordinator,
    }
    sys.modules.update(modules)
