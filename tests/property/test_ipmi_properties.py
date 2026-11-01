"""Property 38 for total and faithful generic IPMI sensor parsing."""

from __future__ import annotations

from typing import NamedTuple

from hypothesis import event, given, settings
from hypothesis import strategies as st

from remote_energy_meter.plugins.interface import FailureClassification, ReadFailure
from remote_energy_meter.plugins.ipmi.parser import parse_sensor_output

DEVICE_ID = "property_ipmi"


class ParseCase(NamedTuple):
    output: str
    expected: float | None
    label: str


@st.composite
def parseable_outputs(draw: st.DrawFn) -> ParseCase:
    value = draw(
        st.one_of(
            st.integers(min_value=0, max_value=100000).map(float),
            st.floats(min_value=0, max_value=100000, allow_nan=False, allow_infinity=False, width=64),
            st.sampled_from((0.0, 0.5, 112.0, 112.5, 99999.5, 100000.0)),
        )
    )
    left = draw(st.sampled_from(("", " ", "    ", "\t")))
    separator = draw(st.sampled_from((" : ", ":", "\t:\t", "  :\t")))
    unit = draw(st.sampled_from(("Watts", "watts", "W", "w")))
    uncertainty = draw(st.sampled_from(("", " (+/- 0)", "\t(+/-\t0.1)")))
    rendered = repr(value)
    return ParseCase(
        output=f"{left}Sensor Reading{separator}{rendered}{uncertainty} {unit}{left}\n",
        expected=float(value),
        label="parseable",
    )


@st.composite
def malformed_outputs(draw: st.DrawFn) -> ParseCase:
    decoy = draw(st.text(max_size=40))
    malformed = draw(
        st.sampled_from(
            (
                "",
                "Sensor ID : System Level",
                "Sensor Reading :",
                "Sensor Reading : not-a-number Watts",
                "Sensor Reading : 112 Volts",
                "Sensor Reading : 112 (+/- unknown) Watts",
            )
        )
    )
    return ParseCase(output=f"Other Field : {decoy}\n{malformed}\n", expected=None, label="malformed")


def ipmi_outputs() -> st.SearchStrategy[ParseCase]:
    return st.one_of(parseable_outputs(), malformed_outputs())


# Feature: remote-energy-meter, Property 38: IPMI sensor parsing is faithful and total
# **Validates: Requirements A.3**
@settings(max_examples=150, deadline=None)
@given(case=ipmi_outputs())
def test_ipmi_sensor_parsing_is_faithful_and_total(case: ParseCase) -> None:
    event(case.label)
    result = parse_sensor_output(case.output, device_id=DEVICE_ID)

    if case.expected is None:
        assert isinstance(result, ReadFailure)
        assert result.classification is FailureClassification.RESPONSE_FORMAT
        assert result.device_id == DEVICE_ID
    else:
        assert not isinstance(result, ReadFailure), result
        assert result == case.expected
