"""Pure parsing of ``ipmitool sdr get`` sensor output."""

from __future__ import annotations

import math
import re
from typing import Final

from remote_energy_meter.plugins.interface import FailureClassification, ReadFailure

__all__ = ["MAX_WATTS", "MIN_WATTS", "parse_sensor_output"]

MIN_WATTS: Final = 0.0
MAX_WATTS: Final = 100000.0

_NUMBER: Final = r"[+-]?(?:(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?|nan|inf(?:inity)?)"
_READING_LINE: Final = re.compile(
    r"^[ \t]*Sensor[ \t]+Reading[ \t]*:[ \t]*(?P<body>.*?)[ \t]*$", re.IGNORECASE | re.MULTILINE
)
_READING_BODY: Final = re.compile(
    rf"^(?P<value>{_NUMBER})(?:\s*\(\s*\+/-\s*{_NUMBER}\s*\))?\s*(?:watts?|w)\s*$",
    re.IGNORECASE,
)


def _failure(device_id: str, classification: FailureClassification, detail: str) -> ReadFailure:
    return ReadFailure(device_id=device_id, classification=classification, detail=detail)


def parse_sensor_output(output: str, *, device_id: str) -> float | ReadFailure:
    """Extract a watt value from one ``ipmitool sdr get`` response.

    Accepted examples include the hardware-observed
    ``Sensor Reading : 112 (+/- 0) Watts`` form, arbitrary horizontal whitespace,
    ordinary decimal/scientific notation, and output without the uncertainty suffix.
    The function performs no I/O and never includes device output in a failure detail.
    """
    line = _READING_LINE.search(output)
    if line is None:
        return _failure(
            device_id,
            FailureClassification.RESPONSE_FORMAT,
            "ipmitool output carried no Sensor Reading field",
        )

    reading = _READING_BODY.fullmatch(line.group("body").strip())
    if reading is None:
        return _failure(
            device_id,
            FailureClassification.RESPONSE_FORMAT,
            "the IPMI Sensor Reading was not a parseable watt value",
        )

    try:
        watts = float(reading.group("value"))
    except (OverflowError, ValueError):
        return _failure(
            device_id,
            FailureClassification.INVALID_MEASUREMENT,
            "the IPMI Sensor Reading was outside the supported watt range",
        )

    if not math.isfinite(watts) or not MIN_WATTS <= watts <= MAX_WATTS:
        return _failure(
            device_id,
            FailureClassification.INVALID_MEASUREMENT,
            f"the IPMI Sensor Reading was not finite or outside {MIN_WATTS:g}-{MAX_WATTS:g} W",
        )
    return watts
