from dataclasses import dataclass
from typing import Any

@dataclass(slots=True)
class HassioServiceInfo:
    config: dict[str, Any]
    name: str
    slug: str
    uuid: str
