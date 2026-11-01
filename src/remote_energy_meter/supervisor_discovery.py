"""Best-effort Home Assistant Supervisor discovery announcement."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Final

import aiohttp

from remote_energy_meter.diagnostics.logger import get_logger
from remote_energy_meter.mqtt.inventory import DEFAULT_INSTANCE_ID, INVENTORY_SCHEMA_VERSION
from remote_energy_meter.mqtt.topics import bridge_availability_topic, integration_inventory_topic

if TYPE_CHECKING:
    import logging

    from remote_energy_meter.config.models import MqttSettings

__all__ = [
    "DISCOVERY_SERVICE",
    "SUPERVISOR_DISCOVERY_URL",
    "announce_supervisor_discovery",
    "supervisor_discovery_payload",
]

DISCOVERY_SERVICE: Final = "remote_energy_meter"
SUPERVISOR_DISCOVERY_URL: Final = "http://supervisor/discovery"
SUPERVISOR_TOKEN_ENV: Final = "SUPERVISOR_TOKEN"
_DISCOVERY_TIMEOUT_S: Final = 5.0


def _normalized_base_topic(base_topic: str) -> str:
    suffix = "/bridge/availability"
    bridge = bridge_availability_topic(base_topic)
    return bridge[: -len(suffix)]


def supervisor_discovery_payload(
    settings: MqttSettings,
    *,
    instance_id: str = DEFAULT_INSTANCE_ID,
) -> dict[str, object]:
    """Return the exact credential-free body accepted by Supervisor ``POST /discovery``."""
    base_topic = _normalized_base_topic(settings.base_topic)
    return {
        "service": DISCOVERY_SERVICE,
        "config": {
            "instance_id": instance_id,
            "base_topic": base_topic,
            "inventory_topic": integration_inventory_topic(base_topic),
            "schema_version": INVENTORY_SCHEMA_VERSION,
        },
    }


async def announce_supervisor_discovery(
    settings: MqttSettings,
    *,
    instance_id: str = DEFAULT_INSTANCE_ID,
    token: str | None = None,
    url: str = SUPERVISOR_DISCOVERY_URL,
    logger: logging.Logger | None = None,
) -> bool:
    """Post Supervisor discovery, returning false after a warning on every failure.

    Discovery is an onboarding convenience, never a prerequisite for polling or MQTT publication.
    The bearer token is used only in the HTTP header and is never placed in the body or a log field.
    """
    log = logger if logger is not None else get_logger(__name__)
    supervisor_token = token if token is not None else os.environ.get(SUPERVISOR_TOKEN_ENV)
    if not supervisor_token:
        log.warning("Supervisor discovery skipped: SUPERVISOR_TOKEN is unavailable")
        return False

    headers = {
        "Authorization": f"Bearer {supervisor_token}",
        "Content-Type": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=_DISCOVERY_TIMEOUT_S)
    try:
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.post(
                url,
                json=supervisor_discovery_payload(settings, instance_id=instance_id),
                headers=headers,
            ) as response,
        ):
            response.raise_for_status()
    except Exception as error:
        # Do not interpolate the response body or token.  The exception type plus endpoint is enough
        # to distinguish DNS/transport/HTTP failures while keeping this path credential-free.
        log.warning(
            "Supervisor discovery failed non-fatally (%s)",
            type(error).__name__,
            extra={"endpoint": url, "error_type": type(error).__name__},
        )
        return False
    else:
        log.info("Supervisor discovery announced for instance %s", instance_id)
        return True
