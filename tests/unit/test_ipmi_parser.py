"""Example and edge-case tests for pure IPMI sensor-output parsing."""

from __future__ import annotations

import pytest

from remote_energy_meter.plugins.interface import FailureClassification, ReadFailure
from remote_energy_meter.plugins.ipmi.parser import MAX_WATTS, parse_sensor_output

DEVICE_ID = "poweredge_b"


def parse(output: str) -> float | ReadFailure:
    return parse_sensor_output(output, device_id=DEVICE_ID)


def failure(output: str) -> ReadFailure:
    result = parse(output)
    assert isinstance(result, ReadFailure), result
    return result


def test_the_hardware_observed_112_watt_format_is_accepted() -> None:
    assert parse("Sensor Reading        : 112 (+/- 0) Watts\n") == 112.0


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("Sensor Reading : 112.5 (+/- 0) Watts", 112.5),
        ("\tSensor\tReading\t:\t.5\t(+/-\t0.1)\tW\t", 0.5),
        ("Sensor Reading: 112. Watts", 112.0),
        ("sensor reading : 1.12e2 watts", 112.0),
        ("Sensor Reading : 0 W", 0.0),
        (f"Sensor Reading : {MAX_WATTS:g} Watts", MAX_WATTS),
    ],
)
def test_reasonable_whitespace_decimal_and_unit_variants_are_accepted(output: str, expected: float) -> None:
    assert parse(output) == expected


@pytest.mark.parametrize(
    "output",
    [
        "",
        "Sensor ID : System Level",
        "Sensor Reading :",
        "Sensor Reading : n/a Watts",
        "Sensor Reading : 112 Volts",
        "Sensor Reading : 112 (+/- unknown) Watts",
    ],
)
def test_missing_or_unparseable_readings_are_response_format_failures(output: str) -> None:
    result = failure(output)

    assert result.classification is FailureClassification.RESPONSE_FORMAT
    assert result.device_id == DEVICE_ID
    if output:
        assert output not in result.detail


@pytest.mark.parametrize("value", ["nan", "+inf", "-infinity", "-0.1", "100000.1", "1e999"])
def test_nonfinite_or_out_of_range_values_are_invalid_measurements(value: str) -> None:
    result = failure(f"Sensor Reading : {value} Watts")

    assert result.classification is FailureClassification.INVALID_MEASUREMENT
    assert result.device_id == DEVICE_ID
