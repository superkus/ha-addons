"""Pure extraction of a power value from an iDRAC Redfish power payload.

This module is deliberately free of HTTP, TLS, and session mechanics: it takes a *decoded* JSON payload plus the
resource path that was requested, and returns either a watt value or a classified :class:`ReadFailure`. That split is
what lets Requirements 4.4, 4.5, and 4.11 be exercised against generated payloads with no server involved
(Properties 9 and 10), and it keeps the response-shape rules in one place that both the read operation and the
connection validation operation read (Requirement 4.13).

Shape of the resource. The Redfish chassis ``Power`` resource carries a ``PowerControl`` array whose members hold
``PowerConsumedWatts``, the power actually drawn by the chassis. A realistic iDRAC body looks like::

    {"@odata.id": "/redfish/v1/Chassis/System.Embedded.1/Power",
     "PowerControl": [{"MemberId": "PowerControl", "PowerConsumedWatts": 224, "PowerCapacityWatts": 1600}]}

Three rules govern the search, in order:

* **First member that carries the value wins**, rather than index 0. Firmware versions differ in how many
  ``PowerControl`` members they publish and which of them are placeholders, so indexing blindly reads a member that
  may have no power value at all.
* **"Carries it" means the key is present, whatever its value.** A member holding ``PowerConsumedWatts: "n/a"`` has a
  power member; it is an *invalid measurement* (Requirement 4.11), not an absent one. Treating it as absent and
  searching on would report a value from a different member than the device published.
* **The Dell ``Oem`` variant is a fallback used only when the standard member is absent anywhere**, per the task
  contract. It is consulted inside each ``PowerControl`` member first, then at the top level, which is where firmware
  that publishes no ``PowerControl`` array at all puts it.

A JSON string is not a JSON number, so ``"224"`` is rejected as non-numeric (Requirement 4.11) rather than coerced.
Coercing would make the plugin's accepted input space wider than the device's published contract, and a value the
firmware quoted is a value whose meaning is not established. ``bool`` is likewise rejected even though it is an
``int`` subclass in Python: ``PowerConsumedWatts: true`` is a malformed payload, not 1 watt.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from remote_energy_meter.plugins.interface import FailureClassification, ReadFailure

__all__ = [
    "MAX_WATTS",
    "MIN_WATTS",
    "OEM_MEMBER",
    "OEM_VENDOR_MEMBER",
    "POWER_CONTROL_MEMBER",
    "POWER_WATTS_MEMBER",
    "extract_watts",
]

# --------------------------------------------------------------------------------------------------
# Member names and bounds
# --------------------------------------------------------------------------------------------------

#: The array of power control members inside the chassis ``Power`` resource.
POWER_CONTROL_MEMBER: Final = "PowerControl"

#: The member holding the power actually drawn, in watts.
POWER_WATTS_MEMBER: Final = "PowerConsumedWatts"

#: The vendor extension container, and the Dell key inside it. Consulted only as a fallback.
OEM_MEMBER: Final = "Oem"
OEM_VENDOR_MEMBER: Final = "Dell"

#: Requirements 4.4 and 4.11: the plausible instantaneous draw of one chassis, in watts, inclusive at both ends. The
#: publisher applies its own wider guard that must hold for any plugin, including future families with larger draws
#: (Requirement 6.14); this bound is about a PowerEdge.
MIN_WATTS: Final = 0.0
MAX_WATTS: Final = 10000.0

#: How much of a rejected value is rendered into a failure detail. A payload member can hold an arbitrarily long
#: string, and the detail travels into a log record whose message is capped at 512 characters (Requirement 9.1);
#: truncating here keeps the useful part of the detail from being the part that gets cut.
_MAX_RENDERED_VALUE_CHARS: Final = 64


# --------------------------------------------------------------------------------------------------
# Internal search outcome
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Located:
    """A power member was found. ``value`` is unvalidated; ``source`` names where it came from."""

    value: object
    source: str


@dataclass(frozen=True, slots=True)
class _Absent:
    """No power member was found anywhere. ``reason`` names the structural cause."""

    reason: str


_MISSING: Final = object()
"""Sentinel distinguishing an absent key from a key present with value ``None``."""


def _member(container: object, key: str) -> object:
    """Return ``container[key]``, or :data:`_MISSING` when ``container`` is not a mapping or lacks the key.

    Accepting a non-mapping container is what lets the ``Oem.Dell`` lookups be written as a chain without a type
    check at every step: a missing intermediate propagates as a missing leaf.
    """
    if isinstance(container, Mapping):
        items: Mapping[object, object] = container
        if key in items:
            return items[key]
    return _MISSING


def _power_control_elements(payload: Mapping[object, object]) -> tuple[list[object], str | None]:
    """Return the ``PowerControl`` members, plus the reason they are unusable when there are none.

    An absent array, a non-array value, and an empty array are all reported distinctly, because they point at
    different problems: wrong resource, unexpected firmware shape, and a chassis publishing no control member.
    """
    raw = _member(payload, POWER_CONTROL_MEMBER)
    if raw is _MISSING:
        return [], f"the response carries no {POWER_CONTROL_MEMBER} array"
    if not isinstance(raw, list):
        return [], f"{POWER_CONTROL_MEMBER} is not an array"
    elements: list[object] = raw
    if not elements:
        return [], f"{POWER_CONTROL_MEMBER} is empty"
    return elements, None


def _locate_watts(payload: object) -> _Located | _Absent:
    """Find the power member in ``payload``, applying the three search rules in the module docstring."""
    if not isinstance(payload, Mapping):
        return _Absent("the response body is not a JSON object")
    body: Mapping[object, object] = payload

    elements, empty_reason = _power_control_elements(body)

    # Rule 1 and 2: the first member carrying the standard key, whatever its value.
    for index, element in enumerate(elements):
        value = _member(element, POWER_WATTS_MEMBER)
        if value is not _MISSING:
            return _Located(value=value, source=f"{POWER_CONTROL_MEMBER}[{index}].{POWER_WATTS_MEMBER}")

    # Rule 3: the Dell vendor extension, inside a member first, then at the top level.
    for index, element in enumerate(elements):
        oem = _member(_member(element, OEM_MEMBER), OEM_VENDOR_MEMBER)
        value = _member(oem, POWER_WATTS_MEMBER)
        if value is not _MISSING:
            source = f"{POWER_CONTROL_MEMBER}[{index}].{OEM_MEMBER}.{OEM_VENDOR_MEMBER}.{POWER_WATTS_MEMBER}"
            return _Located(value=value, source=source)

    top_level_oem = _member(_member(body, OEM_MEMBER), OEM_VENDOR_MEMBER)
    value = _member(top_level_oem, POWER_WATTS_MEMBER)
    if value is not _MISSING:
        return _Located(value=value, source=f"{OEM_MEMBER}.{OEM_VENDOR_MEMBER}.{POWER_WATTS_MEMBER}")

    if empty_reason is not None:
        return _Absent(empty_reason)
    return _Absent(f"no {POWER_CONTROL_MEMBER} member carries {POWER_WATTS_MEMBER}")


def _render(value: object) -> str:
    """Render a rejected value for a failure detail, bounded in length."""
    text = repr(value)
    if len(text) > _MAX_RENDERED_VALUE_CHARS:
        return text[:_MAX_RENDERED_VALUE_CHARS] + "..."
    return text


# --------------------------------------------------------------------------------------------------
# The extraction
# --------------------------------------------------------------------------------------------------


def extract_watts(payload: object, *, device_id: str, power_path: str) -> float | ReadFailure:
    """Return the power value in watts carried by ``payload``, or a classified failure.

    Pure: no I/O, no clock, no session. Never raises — an unusable payload is *returned* as a
    :class:`ReadFailure`, because a raised exception would be classified generically as a plugin failure by the
    scheduler and lose the distinction Requirements 4.5 and 4.11 draw.

    :param payload: the decoded JSON body of the Redfish power resource. Any object is accepted; a body that is not a
        JSON object is a response-format failure, not a crash.
    :param device_id: the ``Device_Entry`` identifier, carried on the failure so the scheduler and the diagnostics
        layer can attribute it.
    :param power_path: the Redfish resource path that was requested. Named in the response-format detail, as
        Requirement 4.5 requires, and in the invalid-measurement detail because a value from an unexpected resource is
        the usual cause.
    :returns: the value as a float when it is numeric and within :data:`MIN_WATTS` to :data:`MAX_WATTS` inclusive
        (Requirement 4.4); a :data:`FailureClassification.RESPONSE_FORMAT` failure when no power member is present
        (Requirement 4.5); a :data:`FailureClassification.INVALID_MEASUREMENT` failure when a member is present but
        non-numeric or out of range (Requirement 4.11).
    """
    located = _locate_watts(payload)
    if isinstance(located, _Absent):
        return ReadFailure(
            device_id=device_id,
            classification=FailureClassification.RESPONSE_FORMAT,
            detail=f"{power_path} carries no {POWER_WATTS_MEMBER} value: {located.reason}",
        )

    value = located.value

    # bool first: it is an int subclass, so `PowerConsumedWatts: true` would otherwise read as 1 watt.
    if isinstance(value, bool) or not isinstance(value, int | float):
        return _invalid(
            device_id=device_id,
            power_path=power_path,
            source=located.source,
            problem="is not a number",
            value=value,
        )

    # A JSON integer has no width limit, so ``json.loads`` yields a Python ``int`` of any magnitude, and one past the
    # IEEE-754 double range makes ``float()`` raise OverflowError. Such a value is out of range in exactly the sense
    # Requirement 4.11 means, so it is classified here rather than escaping to be reclassified as a plugin failure
    # (Requirement 3.7), which would misattribute a device data problem to a bug in this module.
    try:
        watts = float(value)
    except (OverflowError, ValueError):
        return _invalid(
            device_id=device_id,
            power_path=power_path,
            source=located.source,
            problem=f"is outside {MIN_WATTS:g}-{MAX_WATTS:g} W",
            value=value,
        )

    if not math.isfinite(watts):
        return _invalid(
            device_id=device_id,
            power_path=power_path,
            source=located.source,
            problem="is not a finite number",
            value=value,
        )
    if not MIN_WATTS <= watts <= MAX_WATTS:
        return _invalid(
            device_id=device_id,
            power_path=power_path,
            source=located.source,
            problem=f"is outside {MIN_WATTS:g}-{MAX_WATTS:g} W",
            value=value,
        )

    return watts


def _invalid(*, device_id: str, power_path: str, source: str, problem: str, value: object) -> ReadFailure:
    """Build the invalid-measurement failure for a present but unusable value (Requirement 4.11)."""
    return ReadFailure(
        device_id=device_id,
        classification=FailureClassification.INVALID_MEASUREMENT,
        detail=f"{source} at {power_path} {problem}: {_render(value)}",
    )
