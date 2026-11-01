"""Persisted energy state models.

The ``Energy_Accumulator`` keeps a cumulative kilowatt-hour total per device in memory and the ``state_store`` writes it
to the addon data directory, so a restart resumes from the last flushed total instead of from zero (Requirements 7.3,
7.4, 7.9). These are the shapes that cross that boundary.

On-disk shape::

    {
      "schema_version": 1,
      "updated_at": "2025-01-01T00:00:00Z",
      "devices": {
        "poweredge_a": {"total_kwh": 12.345678, "baseline_watts": 214.0, "baseline_at": "2025-01-01T00:00:00Z"}
      }
    }

``schema_version`` is present from the first release so a later format change is detectable and migratable, rather than
being read as a corrupt entry and silently resetting every total to zero (Requirement 7.6).

Construction validates each field, raising :class:`TypeError` for a value of the wrong type and :class:`ValueError` for
a value out of range or a naive timestamp. That is deliberate: it gives the ``state_store`` a single place to catch a
bad *per-device* entry, start that device at 0 kWh with a stated reason, and restore every other device untouched
(Requirement 7.6).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "DeviceEnergyState",
    "EnergyStateFile",
]

CURRENT_SCHEMA_VERSION: Final[int] = 1
"""The ``schema_version`` this release writes, and the only version it reads without migration."""


def _check_non_negative_number(value: float, name: str) -> None:
    """Raise if ``value`` is not a finite, non-negative number.

    ``TypeError`` for a value that is not a number at all, ``ValueError`` for one that is a number but unusable, so a
    caller reading persisted state can distinguish a structural problem from an out-of-range total.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{name} is not a number: {value!r}")
    if not math.isfinite(value):
        raise ValueError(f"{name} is not finite: {value!r}")
    if value < 0:
        raise ValueError(f"{name} is negative: {value!r}")


@dataclass(frozen=True, slots=True)
class DeviceEnergyState:
    """The persisted energy state of one device.

    Attributes:
        total_kwh: The cumulative energy total in kilowatt-hours, held to 6 decimal places by the accumulator. Never
            negative, because the accumulator clamps each interval contribution at zero (Requirements 7.1, 7.2).
        baseline_watts: The power value of the most recent reading, or ``None`` when no reading has been taken.
        baseline_at: The measurement timestamp of the most recent reading as a timezone-aware UTC value, or ``None``
            when no reading has been taken (Requirement 7.3). Retained for diagnostics and **not** restored as an
            active accumulation baseline: after a restart the gap since that timestamp is unknown, so the first reading
            after start re-baselines (Requirement 7.7).
    """

    total_kwh: float
    baseline_watts: float | None = None
    baseline_at: datetime | None = None

    def __post_init__(self) -> None:
        _check_non_negative_number(self.total_kwh, "total_kwh")

        if self.baseline_watts is not None:
            _check_non_negative_number(self.baseline_watts, "baseline_watts")

        if self.baseline_at is not None and self.baseline_at.tzinfo is None:
            raise ValueError("baseline_at is not timezone-aware")

        if (self.baseline_watts is None) != (self.baseline_at is None):
            raise ValueError("baseline_watts and baseline_at must both be present or both be absent")

    @classmethod
    def zeroed(cls) -> DeviceEnergyState:
        """The starting state of a device with no usable persisted total (Requirements 7.6, 7.7)."""
        return cls(total_kwh=0.0)

    @property
    def has_baseline(self) -> bool:
        """Whether a reading was recorded before the state was persisted."""
        return self.baseline_at is not None


@dataclass(frozen=True, slots=True)
class EnergyStateFile:
    """The whole persisted state document.

    Attributes:
        schema_version: The on-disk format version, :data:`CURRENT_SCHEMA_VERSION` for state this release writes.
        updated_at: When the document was written, as a timezone-aware UTC value.
        devices: Per-device state keyed by device identifier. A device absent from the mapping starts at 0 kWh.
    """

    schema_version: int
    updated_at: datetime
    devices: dict[str, DeviceEnergyState] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.updated_at.tzinfo is None:
            raise ValueError("updated_at is not timezone-aware")

    @property
    def is_current_schema(self) -> bool:
        """Whether the document was written by a release using the current on-disk format."""
        return self.schema_version == CURRENT_SCHEMA_VERSION
