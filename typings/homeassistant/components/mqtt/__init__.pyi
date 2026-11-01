from collections.abc import Callable, Coroutine
from typing import Any

from homeassistant.core import HomeAssistant

class ReceiveMessage:
    topic: str
    payload: str | bytes
    qos: int
    retain: bool
    subscribed_topic: str
    timestamp: float

async def async_wait_for_mqtt_client(hass: HomeAssistant) -> bool: ...
async def async_subscribe(
    hass: HomeAssistant,
    topic: str,
    msg_callback: Callable[[ReceiveMessage], Coroutine[Any, Any, None] | None],
    qos: int = ...,
    encoding: str | None = ...,
) -> Callable[[], None]: ...
