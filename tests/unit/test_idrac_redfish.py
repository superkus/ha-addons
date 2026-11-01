"""Unit tests for pure Redfish payload extraction (task 10.2).

Covers Requirement 4.4 (a numeric in-range value yields the watt value), Requirement 4.5 (an absent power member is a
response-format failure naming the requested path), and Requirement 4.11 (a present but non-numeric or out-of-range
value is an invalid-measurement failure). The property tests of tasks 10.4 and 10.5 generalize these; the cases here
pin the realistic iDRAC payload shapes, the vendor-extension fallback, and each range boundary.
"""

from __future__ import annotations

from typing import Any

import pytest

from remote_energy_meter.plugins.idrac.redfish import (
    MAX_WATTS,
    MIN_WATTS,
    POWER_WATTS_MEMBER,
    extract_watts,
)
from remote_energy_meter.plugins.idrac.schema import DEFAULT_POWER_PATH
from remote_energy_meter.plugins.interface import FailureClassification, ReadFailure

DEVICE_ID = "poweredge_a"


def extract(payload: object, *, power_path: str = DEFAULT_POWER_PATH) -> float | ReadFailure:
    """Extract from ``payload`` for the fixture device, defaulting the path to the chassis power resource."""
    return extract_watts(payload, device_id=DEVICE_ID, power_path=power_path)


def failure(payload: object, *, power_path: str = DEFAULT_POWER_PATH) -> ReadFailure:
    """Extract, asserting the outcome is a failure, and return it for further assertions."""
    outcome = extract(payload, power_path=power_path)

    assert isinstance(outcome, ReadFailure)
    return outcome


def idrac_payload(*power_control: dict[str, Any], **top_level: Any) -> dict[str, Any]:
    """A realistic iDRAC chassis power body carrying the given ``PowerControl`` members."""
    return {
        "@odata.id": DEFAULT_POWER_PATH,
        "@odata.type": "#Power.v1_5_0.Power",
        "Id": "Power",
        "Name": "Power",
        "PowerControl": list(power_control),
        "Voltages": [],
        **top_level,
    }


def power_control_member(**overrides: Any) -> dict[str, Any]:
    """A realistic ``PowerControl`` member, as iDRAC 9 publishes it."""
    member: dict[str, Any] = {
        "@odata.id": f"{DEFAULT_POWER_PATH}#/PowerControl/0",
        "MemberId": "PowerControl",
        "Name": "System Power Control",
        "PowerConsumedWatts": 224,
        "PowerCapacityWatts": 1600,
        "PowerMetrics": {"AverageConsumedWatts": 218, "IntervalInMin": 1, "MaxConsumedWatts": 310},
    }
    member.update(overrides)
    return member


# --------------------------------------------------------------------------------------------------
# Requirement 4.4: a numeric in-range value yields the watt value
# --------------------------------------------------------------------------------------------------


def test_a_realistic_idrac_payload_yields_its_power_value() -> None:
    assert extract(idrac_payload(power_control_member())) == 224.0


def test_an_integer_value_is_returned_as_a_float() -> None:
    outcome = extract(idrac_payload(power_control_member(PowerConsumedWatts=224)))

    assert isinstance(outcome, float)
    assert outcome == 224.0


def test_a_fractional_value_is_preserved_exactly() -> None:
    assert extract(idrac_payload(power_control_member(PowerConsumedWatts=224.5))) == 224.5


@pytest.mark.parametrize("watts", [MIN_WATTS, 0.5, 1.0, 224.0, 9999.5, MAX_WATTS])
def test_every_value_within_the_plausible_range_is_accepted(watts: float) -> None:
    """Requirement 4.4: 0 to 10000 W inclusive at both ends."""
    assert extract(idrac_payload(power_control_member(PowerConsumedWatts=watts))) == watts


def test_the_first_member_carrying_the_value_is_read_rather_than_index_zero() -> None:
    """A placeholder member ahead of the real one must not hide the reading."""
    placeholder = power_control_member()
    del placeholder[POWER_WATTS_MEMBER]

    assert extract(idrac_payload(placeholder, power_control_member(PowerConsumedWatts=310))) == 310.0


def test_a_later_member_does_not_override_an_earlier_one() -> None:
    first = power_control_member(PowerConsumedWatts=100)
    second = power_control_member(PowerConsumedWatts=900)

    assert extract(idrac_payload(first, second)) == 100.0


def test_unrelated_payload_members_are_ignored() -> None:
    payload = idrac_payload(power_control_member(), PowerSupplies=[{"PowerOutputWatts": 400}], Redundancy=[])

    assert extract(payload) == 224.0


# --------------------------------------------------------------------------------------------------
# The Dell Oem fallback, used only when the standard member is absent
# --------------------------------------------------------------------------------------------------


def test_the_oem_variant_inside_a_member_is_used_when_the_standard_member_is_absent() -> None:
    oem = {"Dell": {"@odata.type": "#DellPower.v1_0_0.DellPower", "PowerConsumedWatts": 275}}
    member = power_control_member(Oem=oem)
    del member[POWER_WATTS_MEMBER]

    assert extract(idrac_payload(member)) == 275.0


def test_the_top_level_oem_variant_is_used_when_no_power_control_array_exists() -> None:
    payload = {"@odata.id": DEFAULT_POWER_PATH, "Oem": {"Dell": {"PowerConsumedWatts": 190}}}

    assert extract(payload) == 190.0


def test_the_oem_variant_is_ignored_when_the_standard_member_is_present() -> None:
    """The fallback is a fallback: a present standard member wins even when it disagrees."""
    member = power_control_member(
        PowerConsumedWatts=224,
        Oem={"Dell": {"PowerConsumedWatts": 999}},
    )

    assert extract(idrac_payload(member)) == 224.0


def test_a_present_but_unusable_standard_member_is_not_replaced_by_the_oem_variant() -> None:
    """A present key is an invalid measurement, not an absent one -- searching on reports another member's value."""
    member = power_control_member(PowerConsumedWatts="n/a", Oem={"Dell": {"PowerConsumedWatts": 275}})

    assert failure(idrac_payload(member)).classification is FailureClassification.INVALID_MEASUREMENT


def test_an_oem_container_without_the_dell_key_is_absent() -> None:
    member = power_control_member(Oem={"Lenovo": {"PowerConsumedWatts": 275}})
    del member[POWER_WATTS_MEMBER]

    assert failure(idrac_payload(member)).classification is FailureClassification.RESPONSE_FORMAT


# --------------------------------------------------------------------------------------------------
# Requirement 4.5: an absent power member is a response-format failure naming the path
# --------------------------------------------------------------------------------------------------


def test_a_member_without_the_power_key_is_a_response_format_failure() -> None:
    member = power_control_member()
    del member[POWER_WATTS_MEMBER]

    outcome = failure(idrac_payload(member))

    assert outcome.classification is FailureClassification.RESPONSE_FORMAT
    assert outcome.device_id == DEVICE_ID


@pytest.mark.parametrize(
    ("payload", "note"),
    [
        ({}, "an empty object"),
        ({"PowerControl": []}, "an empty PowerControl array"),
        ({"PowerControl": {}}, "PowerControl as an object"),
        ({"PowerControl": "unavailable"}, "PowerControl as a string"),
        ({"PowerControl": [None]}, "a null member"),
        ({"PowerControl": ["PowerConsumedWatts"]}, "a string member"),
        ({"PowerControl": [{}]}, "an empty member"),
        ({"PowerControl": [{"MemberId": "PowerControl"}]}, "a member with no power key"),
        ({"Thermal": {"Fans": []}}, "the wrong resource entirely"),
        (None, "a null body"),
        ([], "an array body"),
        ("not json", "a string body"),
        (224.0, "a bare number body"),
    ],
)
def test_payloads_carrying_no_power_member_are_response_format_failures(payload: object, note: str) -> None:
    """Requirement 4.5: absent, whatever the reason, and never raised."""
    assert failure(payload).classification is FailureClassification.RESPONSE_FORMAT, note


def test_the_response_format_detail_names_the_requested_path() -> None:
    """Requirement 4.5: the failure names the requested Redfish power resource path."""
    path = "/redfish/v1/Chassis/Custom.1/Power"

    detail = failure({}, power_path=path).detail

    assert path in detail
    assert POWER_WATTS_MEMBER in detail


def test_the_response_format_detail_distinguishes_the_structural_cause() -> None:
    """Different absences point at different problems, so they read differently in the log."""
    no_array = failure({"Id": "Power"}).detail
    empty_array = failure({"PowerControl": []}).detail
    no_member = failure({"PowerControl": [{"MemberId": "PowerControl"}]}).detail
    not_an_object = failure("not json").detail

    assert len({no_array, empty_array, no_member, not_an_object}) == 4


# --------------------------------------------------------------------------------------------------
# Requirement 4.11: a present but non-numeric or out-of-range value is an invalid measurement
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "224",
        "n/a",
        "",
        None,
        True,
        False,
        [224],
        {"value": 224},
        float("nan"),
        float("inf"),
        float("-inf"),
    ],
    ids=["numeric string", "text", "empty string", "null", "true", "false", "array", "object", "nan", "inf", "-inf"],
)
def test_a_non_numeric_value_is_an_invalid_measurement(value: object) -> None:
    """Requirement 4.11: present but not a usable number. Note that ``bool`` must not pass as 1 or 0 watts."""
    outcome = failure(idrac_payload(power_control_member(PowerConsumedWatts=value)))

    assert outcome.classification is FailureClassification.INVALID_MEASUREMENT
    assert outcome.device_id == DEVICE_ID


@pytest.mark.parametrize("watts", [-0.001, -1, -224.0, 10000.001, 10001, 25000.0, 1e9])
def test_a_value_outside_the_plausible_range_is_an_invalid_measurement(watts: float) -> None:
    """Requirement 4.11: outside 0 to 10000 W, on either side."""
    assert failure(idrac_payload(power_control_member(PowerConsumedWatts=watts))).classification is (
        FailureClassification.INVALID_MEASUREMENT
    )


@pytest.mark.parametrize("watts", [10**400, -(10**400)], ids=["unrepresentably large", "unrepresentably small"])
def test_a_value_too_large_for_a_float_is_an_invalid_measurement(watts: int) -> None:
    """Requirement 4.11: a JSON integer past the double range is out of range, not a crash.

    ``json.loads`` yields an arbitrary-precision ``int``, so a 400-digit ``PowerConsumedWatts`` reaches the extractor
    intact and ``float()`` on it raises OverflowError. Escaping would have the scheduler reclassify a device data
    problem as a plugin failure (Requirement 3.7), losing the classification this requirement specifies.
    """
    outcome = failure(idrac_payload(power_control_member(PowerConsumedWatts=watts)))

    assert outcome.classification is FailureClassification.INVALID_MEASUREMENT
    assert f"outside {MIN_WATTS:g}-{MAX_WATTS:g} W" in outcome.detail
    assert DEFAULT_POWER_PATH in outcome.detail


@pytest.mark.parametrize(
    ("inside", "outside"),
    [(MIN_WATTS, -0.000001), (MAX_WATTS, 10000.000001)],
    ids=["lower bound", "upper bound"],
)
def test_the_range_boundaries_are_inclusive(inside: float, outside: float) -> None:
    assert extract(idrac_payload(power_control_member(PowerConsumedWatts=inside))) == inside
    assert isinstance(extract(idrac_payload(power_control_member(PowerConsumedWatts=outside))), ReadFailure)


def test_the_invalid_measurement_detail_names_the_value_and_the_path() -> None:
    path = "/redfish/v1/Chassis/Custom.1/Power"

    detail = failure(idrac_payload(power_control_member(PowerConsumedWatts=25000)), power_path=path).detail

    assert "25000" in detail
    assert path in detail


def test_an_oversized_rejected_value_is_truncated_in_the_detail() -> None:
    """The detail travels into a 512-character log message, so a pathological value cannot crowd it out."""
    detail = failure(idrac_payload(power_control_member(PowerConsumedWatts="x" * 4096))).detail

    assert len(detail) < 512
    assert detail.endswith("...")


# --------------------------------------------------------------------------------------------------
# Purity
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"PowerControl": [{"PowerConsumedWatts": 224}]},
        {"PowerControl": [{"PowerConsumedWatts": "n/a"}]},
        {"PowerControl": [{"PowerConsumedWatts": float("nan")}]},
    ],
)
def test_extraction_never_mutates_its_input(payload: object) -> None:
    before = repr(payload)

    extract(payload)

    assert repr(payload) == before


def test_extraction_is_deterministic() -> None:
    payload = idrac_payload(power_control_member())

    assert extract(payload) == extract(payload)
