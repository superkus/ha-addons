"""Strict, Home Assistant-independent parser for the MQTT inventory protocol."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Final, Literal

from .const import ROLE_ENERGY, ROLE_POWER, SUPPORTED_SCHEMA_VERSION

_MAX_PAYLOAD_BYTES: Final = 256 * 1024
_MAX_DEVICES: Final = 50
_ID_RE: Final = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_WILDCARDS: Final = ("+", "#")


class InventoryValidationError(ValueError):
    """The received inventory does not conform to the supported schema."""


@dataclass(frozen=True, slots=True)
class InventoryDevice:
    """Credential-free metadata and exact MQTT topics for one metered device."""

    device_id: str
    name: str
    plugin_id: str
    availability_topic: str
    power_state_topic: str
    energy_state_topic: str
    power_attributes_topic: str
    manufacturer: str | None = None
    model: str | None = None


@dataclass(frozen=True, slots=True)
class Inventory:
    """One validated retained inventory announcement."""

    schema_version: int
    instance_id: str
    base_topic: str
    bridge_availability_topic: str
    devices: tuple[InventoryDevice, ...]


def normalize_base_topic(value: object) -> str:
    """Normalize and validate a publish-topic prefix."""
    if not isinstance(value, str):
        raise InventoryValidationError("base_topic must be text")
    normalized = value.strip().strip("/")
    if not normalized or "\x00" in normalized or any(item in normalized for item in _WILDCARDS):
        raise InventoryValidationError("base_topic is not a valid MQTT publish prefix")
    return normalized


def inventory_topic_for_base(base_topic: str) -> str:
    """Return the retained inventory topic for a normalized base topic."""
    return f"{normalize_base_topic(base_topic)}/integration/devices"


def entity_unique_id(device_id: str, role: Literal["power", "energy"]) -> str:
    """Return a stable integration-owned entity ID derived only from device and role."""
    if not _ID_RE.fullmatch(device_id):
        raise InventoryValidationError("device_id is not valid")
    if role not in (ROLE_POWER, ROLE_ENERGY):
        raise InventoryValidationError("entity role is not supported")
    return f"rem_{device_id.lower()}_{role}"


def _mapping(value: object, *, path: str, required: set[str], optional: set[str] | None = None) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise InventoryValidationError(f"{path} must be an object")
    allowed = required | (optional or set())
    if set(value) != required | (set(value) & (optional or set())):
        missing = len(required - set(value))
        extra = len(set(value) - allowed)
        raise InventoryValidationError(f"{path} has an invalid field set ({missing} missing, {extra} extra)")
    return value


def _text(mapping: dict[str, object], key: str, *, path: str, allow_empty: bool = False) -> str:
    value = mapping[key]
    if not isinstance(value, str) or (not allow_empty and not value):
        raise InventoryValidationError(f"{path}.{key} must be non-empty text")
    return value


def _optional_text(mapping: dict[str, object], key: str, *, path: str) -> str | None:
    if key not in mapping:
        return None
    return _text(mapping, key, path=path)


def _exact_topic(value: str, expected: str, *, path: str) -> str:
    if value != expected or "\x00" in value or any(item in value for item in _WILDCARDS):
        raise InventoryValidationError(f"{path} does not match the schema topic layout")
    return value


def parse_inventory_payload(
    payload: str | bytes,
    *,
    expected_instance_id: str | None = None,
    expected_base_topic: str | None = None,
    expected_schema_version: int = SUPPORTED_SCHEMA_VERSION,
) -> Inventory:
    """Parse a retained inventory payload and reject every malformed or cross-wired shape."""
    if isinstance(payload, bytes):
        if len(payload) > _MAX_PAYLOAD_BYTES:
            raise InventoryValidationError("inventory payload is too large")
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise InventoryValidationError("inventory payload is not UTF-8") from error
    elif isinstance(payload, str):
        text = payload
        if len(text.encode("utf-8")) > _MAX_PAYLOAD_BYTES:
            raise InventoryValidationError("inventory payload is too large")
    else:
        raise InventoryValidationError("inventory payload must be text or bytes")

    try:
        decoded: object = json.loads(text)
    except (json.JSONDecodeError, TypeError) as error:
        raise InventoryValidationError("inventory payload is not valid JSON") from error

    root = _mapping(
        decoded,
        path="inventory",
        required={"schema_version", "instance_id", "base_topic", "bridge_availability_topic", "devices"},
    )
    schema_version = root["schema_version"]
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise InventoryValidationError("inventory.schema_version must be an integer")
    if schema_version != expected_schema_version:
        raise InventoryValidationError("inventory schema version is not supported")

    instance_id = _text(root, "instance_id", path="inventory")
    if not _ID_RE.fullmatch(instance_id):
        raise InventoryValidationError("inventory.instance_id is not valid")
    if expected_instance_id is not None and instance_id != expected_instance_id:
        raise InventoryValidationError("inventory.instance_id does not match the config entry")

    base_topic = normalize_base_topic(_text(root, "base_topic", path="inventory"))
    if expected_base_topic is not None and base_topic != normalize_base_topic(expected_base_topic):
        raise InventoryValidationError("inventory.base_topic does not match the config entry")
    bridge_topic = _exact_topic(
        _text(root, "bridge_availability_topic", path="inventory"),
        f"{base_topic}/bridge/availability",
        path="inventory.bridge_availability_topic",
    )

    raw_devices = root["devices"]
    if not isinstance(raw_devices, list) or len(raw_devices) > _MAX_DEVICES:
        raise InventoryValidationError("inventory.devices must be a list of at most 50 devices")

    devices: list[InventoryDevice] = []
    seen_ids: set[str] = set()
    seen_topics: set[str] = {bridge_topic}
    required = {
        "device_id",
        "name",
        "plugin_id",
        "availability_topic",
        "power_state_topic",
        "energy_state_topic",
        "power_attributes_topic",
    }
    optional = {"manufacturer", "model"}
    for index, raw_device in enumerate(raw_devices):
        path = f"inventory.devices[{index}]"
        item = _mapping(raw_device, path=path, required=required, optional=optional)
        device_id = _text(item, "device_id", path=path)
        folded = device_id.casefold()
        if not _ID_RE.fullmatch(device_id) or folded == "bridge":
            raise InventoryValidationError(f"{path}.device_id is not valid")
        if folded in seen_ids:
            raise InventoryValidationError("inventory contains duplicate device IDs")
        seen_ids.add(folded)

        expected_topics = {
            "availability_topic": f"{base_topic}/{device_id}/availability",
            "power_state_topic": f"{base_topic}/{device_id}/power/state",
            "energy_state_topic": f"{base_topic}/{device_id}/energy/state",
            "power_attributes_topic": f"{base_topic}/{device_id}/power/attributes",
        }
        topics: dict[str, str] = {}
        for key, expected in expected_topics.items():
            topic = _exact_topic(_text(item, key, path=path), expected, path=f"{path}.{key}")
            if topic in seen_topics:
                raise InventoryValidationError("inventory reuses a topic across protocol roles")
            seen_topics.add(topic)
            topics[key] = topic

        devices.append(
            InventoryDevice(
                device_id=device_id,
                name=_text(item, "name", path=path),
                plugin_id=_text(item, "plugin_id", path=path),
                availability_topic=topics["availability_topic"],
                power_state_topic=topics["power_state_topic"],
                energy_state_topic=topics["energy_state_topic"],
                power_attributes_topic=topics["power_attributes_topic"],
                manufacturer=_optional_text(item, "manufacturer", path=path),
                model=_optional_text(item, "model", path=path),
            )
        )

    return Inventory(
        schema_version=schema_version,
        instance_id=instance_id,
        base_topic=base_topic,
        bridge_availability_topic=bridge_topic,
        devices=tuple(devices),
    )


def parse_finite_number(payload: str | bytes, *, minimum: float = -math.inf, maximum: float = math.inf) -> float:
    """Parse one finite numeric state payload within inclusive bounds."""
    if isinstance(payload, bytes):
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise InventoryValidationError("state payload is not UTF-8") from error
    elif isinstance(payload, str):
        text = payload
    else:
        raise InventoryValidationError("state payload must be text or bytes")
    try:
        value = float(text)
    except (TypeError, ValueError) as error:
        raise InventoryValidationError("state payload is not numeric") from error
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise InventoryValidationError("state payload is outside the accepted finite range")
    return value
