from collections.abc import Iterable
from typing import Protocol

class AddEntitiesCallback(Protocol):
    def __call__(self, new_entities: Iterable[object], update_before_add: bool = ...) -> None: ...
