"""Config flow for the Remote Energy Meter companion integration."""

from __future__ import annotations

from typing import Any, override

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

from .const import (
    CONF_BASE_TOPIC,
    CONF_INSTANCE_ID,
    CONF_INVENTORY_TOPIC,
    CONF_SCHEMA_VERSION,
    DEFAULT_BASE_TOPIC,
    DEFAULT_INSTANCE_ID,
    DOMAIN,
    SUPPORTED_SCHEMA_VERSION,
)
from .protocol import (
    InventoryValidationError,
    entity_unique_id,
    inventory_topic_for_base,
    normalize_base_topic,
)

_USER_SCHEMA = vol.Schema({vol.Required(CONF_BASE_TOPIC, default=DEFAULT_BASE_TOPIC): str})
_DISCOVERY_FIELDS = {CONF_INSTANCE_ID, CONF_BASE_TOPIC, CONF_INVENTORY_TOPIC, CONF_SCHEMA_VERSION}


def _discovery_entry_data(config: dict[str, Any]) -> dict[str, Any]:
    """Validate the exact credential-free Supervisor discovery config."""
    if set(config) != _DISCOVERY_FIELDS:
        raise InventoryValidationError("Supervisor discovery has an invalid field set")
    instance_id = config.get(CONF_INSTANCE_ID)
    base_value = config.get(CONF_BASE_TOPIC)
    inventory_value = config.get(CONF_INVENTORY_TOPIC)
    schema_version = config.get(CONF_SCHEMA_VERSION)
    if not isinstance(instance_id, str) or not instance_id:
        raise InventoryValidationError("Supervisor discovery instance_id is invalid")
    entity_unique_id(instance_id, "power")
    if not isinstance(base_value, str):
        raise InventoryValidationError("Supervisor discovery base_topic is invalid")
    base_topic = normalize_base_topic(base_value)
    if not isinstance(inventory_value, str) or inventory_value != inventory_topic_for_base(base_topic):
        raise InventoryValidationError("Supervisor discovery inventory_topic is invalid")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != SUPPORTED_SCHEMA_VERSION
    ):
        raise InventoryValidationError("Supervisor discovery schema_version is unsupported")
    return {
        CONF_INSTANCE_ID: instance_id,
        CONF_BASE_TOPIC: base_topic,
        CONF_INVENTORY_TOPIC: inventory_value,
        CONF_SCHEMA_VERSION: schema_version,
    }


class RemoteEnergyMeterConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle Supervisor discovery and manual base-topic fallback."""

    VERSION = 1
    _discovery_info: HassioServiceInfo | None = None
    _discovery_data: dict[str, Any] | None = None

    @override
    async def async_step_hassio(self, discovery_info: HassioServiceInfo) -> ConfigFlowResult:
        """Handle discovery initiated by Supervisor."""
        try:
            data = _discovery_entry_data(discovery_info.config)
        except InventoryValidationError:
            return self.async_abort(reason="invalid_discovery_info")

        instance_id = data[CONF_INSTANCE_ID]
        await self.async_set_unique_id(instance_id)
        entries = list(self._async_current_entries())
        matching = next((entry for entry in entries if entry.unique_id == instance_id), None)
        existing = matching if matching is not None else (entries[0] if len(entries) == 1 else None)
        if existing is not None:
            return self.async_update_reload_and_abort(
                existing,
                unique_id=instance_id,
                title=discovery_info.name,
                data=data,
                reload_even_if_entry_is_unchanged=False,
                reason="already_configured",
            )
        if entries:
            return self.async_abort(reason="already_configured")

        self._discovery_info = discovery_info
        self._discovery_data = data
        self.context.update(
            {
                "title_placeholders": {"name": discovery_info.name},
                "configuration_url": f"homeassistant://hassio/addon/{discovery_info.slug}/info",
            }
        )
        return await self.async_step_hassio_confirm()

    async def async_step_hassio_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask the user to confirm the discovered add-on instance."""
        if user_input is None:
            addon = self._discovery_info.name if self._discovery_info is not None else "Remote Energy Meter"
            return self.async_show_form(
                step_id="hassio_confirm",
                description_placeholders={"addon": addon},
            )
        if self._discovery_data is None or self._discovery_info is None:
            return self.async_abort(reason="invalid_discovery_info")
        return self.async_create_entry(title=self._discovery_info.name, data=self._discovery_data)

    @override
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Configure the one supported instance from its MQTT base topic."""
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                base_topic = normalize_base_topic(user_input[CONF_BASE_TOPIC])
            except (InventoryValidationError, KeyError, TypeError):
                errors["base"] = "invalid_base_topic"
            else:
                await self.async_set_unique_id(DEFAULT_INSTANCE_ID)
                return self.async_create_entry(
                    title="Remote Energy Meter",
                    data={
                        CONF_INSTANCE_ID: DEFAULT_INSTANCE_ID,
                        CONF_BASE_TOPIC: base_topic,
                        CONF_INVENTORY_TOPIC: inventory_topic_for_base(base_topic),
                        CONF_SCHEMA_VERSION: SUPPORTED_SCHEMA_VERSION,
                    },
                )
        return self.async_show_form(step_id="user", data_schema=_USER_SCHEMA, errors=errors)
