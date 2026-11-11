"""Generated properties for the exact protocol-v1 six-word SAS."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from remote_energy_meter.protocol_v1 import parse_sas, render_sas


@given(st.binary(min_size=6, max_size=6))
def test_every_six_byte_sas_round_trips_without_normalization(value: bytes) -> None:
    rendered = render_sas(value)

    assert rendered.encode("ascii").decode("ascii") == rendered
    assert len(rendered.split(" ")) == 6
    assert parse_sas(rendered) == value
