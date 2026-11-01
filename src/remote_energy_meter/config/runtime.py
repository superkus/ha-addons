"""Runtime and rejection models.

These sit between the validated configuration (``config.models``) and the components that act on it:

* :class:`ResolvedDevice` is what the ``Plugin_Registry`` hands to the ``Poll_Scheduler`` — one accepted
  ``Device_Entry``, its own plugin instance (Requirement 3.6), its resolved poll interval (Requirement 5.2, 5.7), its
  resolved request timeout (Requirement 5.8), and the lock that keeps at most one read in flight per device
  (Requirement 5.3, 5.9).
* :class:`EntryRejection` is the report of a per-entry rejection: the entry is excluded, one error record names it, and
  parsing or resolution continues with the remaining entries.

They live beside the configuration models rather than inside them because they are *derived* from a
``Configuration_Object`` and carry live objects (a plugin instance, an ``asyncio.Lock``) that configuration never does.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from remote_energy_meter.config.models import DeviceEntry
    from remote_energy_meter.plugins.interface import DevicePlugin

__all__ = [
    "EntryRejection",
    "RejectionReason",
    "ResolvedDevice",
]


@dataclass(frozen=True, slots=True)
class ResolvedDevice:
    """One accepted ``Device_Entry`` paired with everything needed to poll it.

    Exactly one instance exists per accepted entry, and exactly one plugin instance is held by it, so a connection,
    authentication, or timeout failure on one device leaves every other device's connection state untouched
    (Requirement 3.6).

    Attributes:
        entry: The accepted, validated ``Device_Entry``.
        plugin: The plugin instance created for this entry, and for no other entry.
        interval_s: The resolved poll interval in whole seconds — the device-specific interval when it is present and
            within 5-3600 s, otherwise the global interval (Requirements 5.2, 5.7).
        timeout_s: The resolved per-attempt request timeout in seconds — the entry's timeout when present, otherwise
            ``min(interval_s / 3, 10.0)`` (Requirement 5.8).
        lock: Held for the duration of a poll cycle, so a cycle that is still running when the next scheduled time
            arrives causes that scheduled cycle to be skipped rather than overlapped (Requirements 5.3, 5.9).
    """

    entry: DeviceEntry
    plugin: DevicePlugin
    interval_s: int
    timeout_s: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def device_id(self) -> str:
        """The device identifier, which every log record and MQTT topic for this device is derived from."""
        return self.entry.device_id

    @property
    def plugin_id(self) -> str:
        """The plugin identifier named by the entry and resolved to :attr:`plugin`."""
        return self.entry.plugin


class RejectionReason(StrEnum):
    """Why a single ``Device_Entry`` was excluded while the rest of the configuration was kept.

    Every member is non-fatal by construction: the entry is dropped, one error record is written, and the remaining
    entries continue through validation and resolution.
    """

    SCHEMA_VIOLATION = "schema_violation"
    """A field violated the plugin-published configuration schema: missing, empty, wrongly typed, or out of range
    (Requirements 2.2, 2.3)."""

    INVALID_IDENTIFIER = "invalid_identifier"
    """The device identifier was empty, longer than 64 characters, or contained a character outside
    ``[A-Za-z0-9_-]`` (Requirement 2.12)."""

    DUPLICATE_IDENTIFIER = "duplicate_identifier"
    """A later entry declared an identifier already claimed, compared without regard to letter case, by an earlier
    entry; the first occurrence wins (Requirement 2.5)."""

    UNKNOWN_PLUGIN = "unknown_plugin"
    """The named plugin identifier was not among those discovered (Requirement 3.4)."""

    EMPTY_CREDENTIAL = "empty_credential"
    """A credential field was absent, or held zero characters after leading and trailing whitespace was removed
    (Requirement 8.5)."""

    OVERSIZED_CREDENTIAL = "oversized_credential"
    """A credential field held more than 256 characters (Requirement 8.8)."""

    UNRESOLVED_SECRET = "unresolved_secret"
    """A Home Assistant secret reference did not exist, or did not resolve within 5 seconds (Requirement 8.6)."""


@dataclass(frozen=True, slots=True)
class EntryRejection:
    """One excluded ``Device_Entry``, reported so the caller can write exactly one error record for it.

    Attributes:
        device_id: The rejected entry's identifier, or ``None`` when the identifier itself was unusable and so cannot
            be named (Requirement 2.12).
        reason: Which rejection path produced this exclusion.
        field_name: The first failing field, ordered by its declaration index in the plugin schema so the reported
            field is deterministic rather than dependent on validator internals (Requirement 2.3). ``None`` when the
            reason does not concern a single field, as for a duplicate identifier or an unknown plugin.
    """

    device_id: str | None
    reason: RejectionReason
    field_name: str | None = None
