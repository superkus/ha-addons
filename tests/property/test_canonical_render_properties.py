"""Property: the canonical renderer and the strict parser are exact inverses over the whole profile."""

from __future__ import annotations

from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from remote_energy_meter.protocol_v1 import JsonValue, parse_canonical_json, render_canonical_json

_MAX_DEPTH = 6


def _decimals() -> st.SearchStrategy[Decimal]:
    # Profile decimals: non-negative, one to six fractional digits, no trailing zero. A fraction of at least 1 padded
    # to six digits keeps a non-zero digit after stripping trailing zeroes.
    return st.builds(
        lambda whole, fraction: Decimal(f"{whole}.{str(fraction).zfill(6).rstrip('0')}"),
        st.integers(min_value=0, max_value=10**12),
        st.integers(min_value=1, max_value=999999),
    )


_SCALARS: st.SearchStrategy[JsonValue] = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=0, max_value=2**64 - 1),
    _decimals(),
    st.text(max_size=16),
)

_KEYS = st.text(alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x7E), max_size=8)

_VALUES: st.SearchStrategy[JsonValue] = st.recursive(
    _SCALARS,
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(_KEYS, children, max_size=4),
    ),
    max_leaves=12,
)


def _depth(value: JsonValue) -> int:
    if isinstance(value, list):
        return 1 + max((_depth(child) for child in value), default=0)
    if isinstance(value, dict):
        return 1 + max((_depth(child) for child in value.values()), default=0)
    return 0


@given(_VALUES)
@settings(max_examples=300)
def test_parse_inverts_render_and_render_is_a_fixed_point(value: JsonValue) -> None:
    if _depth(value) > _MAX_DEPTH:
        return
    rendered = render_canonical_json(value, max_depth=_MAX_DEPTH)

    parsed = parse_canonical_json(rendered, max_depth=_MAX_DEPTH)

    assert parsed == value
    assert render_canonical_json(parsed, max_depth=_MAX_DEPTH) == rendered
