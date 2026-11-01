from collections.abc import Callable

from .config_entries import ConfigEntries

class HomeAssistant:
    config_entries: ConfigEntries
    data: dict[str, object]

def callback[T: Callable[..., object]](func: T) -> T: ...
