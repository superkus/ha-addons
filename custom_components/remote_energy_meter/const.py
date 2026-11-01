"""Constants for the Remote Energy Meter companion integration."""

from typing import Final

DOMAIN: Final = "remote_energy_meter"
NAME: Final = "Remote Energy Meter"

CONF_INSTANCE_ID: Final = "instance_id"
CONF_BASE_TOPIC: Final = "base_topic"
CONF_INVENTORY_TOPIC: Final = "inventory_topic"
CONF_SCHEMA_VERSION: Final = "schema_version"

DEFAULT_INSTANCE_ID: Final = DOMAIN
DEFAULT_BASE_TOPIC: Final = DOMAIN
SUPPORTED_SCHEMA_VERSION: Final = 1

ROLE_POWER: Final = "power"
ROLE_ENERGY: Final = "energy"
