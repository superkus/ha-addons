"""The canonical-JSON renderer: exact bytes the strict parser accepts, and nothing it would reject."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from remote_energy_meter.protocol_v1 import ProtocolViolation, parse_canonical_json, render_canonical_json
from remote_energy_meter.protocol_v1.canonical_json import MAX_DEPTH_CEILING


def _assert_invalid(value: Any, *, max_depth: int = 8, max_length: int = 65536) -> None:
    with pytest.raises(ProtocolViolation) as caught:
        render_canonical_json(value, max_depth=max_depth, max_length=max_length)
    assert caught.value.code == "invalid_request"


def test_renders_sorted_compact_members_and_exact_scalars() -> None:
    value: dict[str, Any] = {
        "watts": Decimal("12.5"),
        "count": 18446744073709551615,
        "name": 'quote " backslash \\ tab \t nul \x00 é',
        "flags": [True, False, None],
        "empty": {},
    }

    rendered = render_canonical_json(value, max_depth=8)

    assert rendered == (
        b'{"count":18446744073709551615,"empty":{},"flags":[true,false,null],'
        b'"name":"quote \\" backslash \\\\ tab \\t nul \\u0000 \xc3\xa9","watts":12.5}'
    )
    assert parse_canonical_json(rendered, max_depth=8) == value


@pytest.mark.parametrize(
    "raw",
    [
        b"0",
        b'""',
        b"null",
        b"[]",
        b'{"a":[1,{"b":0.000001}],"z":"x"}',
        b'{"":true}',
    ],
)
def test_rendering_a_parsed_value_reproduces_its_canonical_bytes(raw: bytes) -> None:
    assert render_canonical_json(parse_canonical_json(raw, max_depth=8), max_depth=8) == raw


@pytest.mark.parametrize(
    "value",
    [
        1.5,
        -1,
        2**64,
        (1, 2),
        {1, 2},
        b"bytes",
        {"key": object()},
        {1: "non-string key"},
        {"é": "non-ASCII key"},
        "\ud800",
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-1.5"),
        Decimal("1.50"),
        Decimal("1.0000001"),
        Decimal("1"),
    ],
)
def test_values_outside_the_profile_are_rejected(value: Any) -> None:
    _assert_invalid(value)


def test_list_and_dict_subclasses_are_rejected() -> None:
    class Tracked(dict[str, int]):
        pass

    class Items(list[int]):
        pass

    _assert_invalid(Tracked(a=1))
    _assert_invalid(Items([1]))


def test_int_and_str_subclasses_are_rejected() -> None:
    """A subclass can render bytes that differ from its value, so scalars follow the same exact-type rule."""

    class Disguised(int):
        def __str__(self) -> str:
            return "7"

    class Text(str):
        pass

    _assert_invalid(Disguised(5))
    _assert_invalid(Text("x"))
    _assert_invalid({"nested": Disguised(5)})
    _assert_invalid([Text("x")])


@pytest.mark.parametrize(
    ("value", "max_depth"),
    [
        ({}, 0),
        ([], 0),
        ({"a": {"b": []}}, 2),
        ([[[[0]]]], 3),
    ],
)
def test_nesting_beyond_max_depth_is_rejected(value: Any, max_depth: int) -> None:
    _assert_invalid(value, max_depth=max_depth)


@pytest.mark.parametrize(("value", "max_depth"), [(0, 0), ({}, 1), ({"a": {"b": []}}, 3)])
def test_nesting_at_max_depth_is_accepted(value: Any, max_depth: int) -> None:
    rendered = render_canonical_json(value, max_depth=max_depth)
    assert parse_canonical_json(rendered, max_depth=max_depth) == value


def test_output_longer_than_max_length_is_rejected() -> None:
    assert render_canonical_json("abc", max_depth=0, max_length=5) == b'"abc"'
    _assert_invalid("abcd", max_depth=0, max_length=5)


def test_a_max_depth_above_the_ceiling_is_a_caller_error() -> None:
    with pytest.raises(ValueError, match="max_depth"):
        render_canonical_json(0, max_depth=MAX_DEPTH_CEILING + 1)


def test_booleans_are_not_treated_as_integers() -> None:
    assert render_canonical_json([True, 1], max_depth=1) == b"[true,1]"
