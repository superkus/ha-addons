"""Strict parsing and rendering for the REM canonical JSON subset used by signed bodies and stored documents."""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Final, cast

from remote_energy_meter.protocol_v1.errors import invalid_request

type JsonScalar = bool | int | Decimal | str | None
type JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
_DECIMAL: Final = re.compile(r"(?:0|[1-9][0-9]*)\.[0-9]{1,6}\Z")
_UINT64_MAX: Final = 2**64 - 1
_DEFAULT_MAX_LENGTH: Final = 65536
MAX_DEPTH_CEILING: Final = 64
_ESCAPE: Final = {
    0x08: b"\\b",
    0x09: b"\\t",
    0x0A: b"\\n",
    0x0C: b"\\f",
    0x0D: b"\\r",
}


def _reject_constant(_value: str) -> None:
    raise invalid_request("JSON non-finite number is forbidden")


def _parse_uint64(value: str) -> int:
    if len(value) > 20 or not re.fullmatch(r"0|[1-9][0-9]*", value):
        raise invalid_request("JSON integer is outside the uint64 profile")
    parsed = int(value)
    if parsed > _UINT64_MAX:
        raise invalid_request("JSON integer is outside the uint64 profile")
    return parsed


def _object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise invalid_request("JSON object member is duplicated")
        result[key] = value
    return result


def _exceeds_depth(value: JsonValue, maximum: int) -> bool:
    pending: list[tuple[JsonValue, int]] = [(value, 0)]
    while pending:
        current, depth = pending.pop()
        if isinstance(current, dict):
            child_depth = depth + 1
            if child_depth > maximum:
                return True
            pending.extend((child, child_depth) for child in current.values())
        elif isinstance(current, list):
            child_depth = depth + 1
            if child_depth > maximum:
                return True
            pending.extend((child, child_depth) for child in current)
    return False


def _structural_depth_exceeds(raw: bytes, maximum: int) -> bool:
    """Linear pre-scan of container nesting, so json.loads never recurses past the bound.

    Brackets inside strings do not count. Scanning bytes is safe for UTF-8: every
    byte of a multi-byte sequence is >= 0x80 and never equals ``"``, ``\\`` or a bracket.
    """
    depth = 0
    in_string = False
    escaped = False
    for byte in raw:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:  # backslash
                escaped = True
            elif byte == 0x22:  # quote
                in_string = False
        elif byte == 0x22:
            in_string = True
        elif byte in (0x5B, 0x7B):  # [ {
            depth += 1
            if depth > maximum:
                return True
        elif byte in (0x5D, 0x7D):  # ] }
            depth -= 1
    return False


def _render_string(value: str) -> bytes:
    chunks = [b'"']
    for character in value:
        codepoint = ord(character)
        if character == '"':
            chunks.append(b'\\"')
        elif character == "\\":
            chunks.append(b"\\\\")
        elif codepoint in _ESCAPE:
            chunks.append(_ESCAPE[codepoint])
        elif codepoint < 0x20:
            chunks.append(f"\\u{codepoint:04x}".encode("ascii"))
        elif 0xD800 <= codepoint <= 0xDFFF:
            raise invalid_request("JSON string contains a surrogate code point")
        else:
            chunks.append(character.encode("utf-8"))
    chunks.append(b'"')
    return b"".join(chunks)


def _render_decimal(value: Decimal) -> bytes:
    if not value.is_finite() or value.is_signed():
        raise invalid_request("JSON decimal is outside the canonical profile")
    rendered = format(value, "f")
    if not _DECIMAL.fullmatch(rendered) or rendered.endswith("0"):
        raise invalid_request("JSON decimal is outside the canonical profile")
    return rendered.encode("ascii")


def _render(value: JsonValue) -> bytes:
    if value is None:
        return b"null"
    if value is True:
        return b"true"
    if value is False:
        return b"false"
    if isinstance(value, int):
        return str(value).encode("ascii")
    if isinstance(value, Decimal):
        return _render_decimal(value)
    if isinstance(value, str):
        return _render_string(value)
    if isinstance(value, list):
        return b"[" + b",".join(_render(child) for child in value) + b"]"
    members: list[tuple[bytes, JsonValue]] = []
    for key, child in value.items():
        try:
            key_bytes = key.encode("ascii")
        except UnicodeEncodeError:
            raise invalid_request("JSON object member name is not ASCII") from None
        members.append((key_bytes, child))
    members.sort(key=lambda item: item[0])
    return (
        b"{" + b",".join(_render_string(key.decode("ascii")) + b":" + _render(child) for key, child in members) + b"}"
    )


def _check_renderable(value: object, depth: int, max_depth: int) -> None:
    """Check the shape of ``value`` before rendering any byte: exact types, uint64 range, string keys, and depth.

    It rejects a type other than exactly ``None``, ``bool``, ``int``, ``Decimal``, ``str``, ``list`` or ``dict``; an
    integer outside uint64; a non-``str`` object key; and nesting deeper than ``max_depth``. It does not check content:
    a non-canonical ``Decimal``, a surrogate code point, or a non-ASCII key is rejected while rendering, and an
    oversize output by the parser's self-check in :func:`render_canonical_json`.

    ``depth`` is the number of containers enclosing ``value``; a container itself counts one level, as in the parser.
    """
    # Types are checked exactly: a subclass can override how it renders, so only the parser's own types are accepted.
    kind = type(value)
    if value is None or kind is bool or kind is str or kind is Decimal:
        return
    if kind is int:
        if not 0 <= cast(int, value) <= _UINT64_MAX:
            raise invalid_request("JSON integer is outside the uint64 profile")
        return
    if kind is not list and kind is not dict:
        # Floats, tuples, sets, bytes, and subclasses of any JSON type are outside the profile.
        raise invalid_request(f"{kind.__name__} is not a canonical JSON value")
    level = depth + 1
    if level > max_depth:
        raise invalid_request("JSON nesting is too deep")
    if kind is list:
        for child in cast(list[object], value):
            _check_renderable(child, level, max_depth)
        return
    for key, child in cast(dict[object, object], value).items():
        if type(key) is not str:
            raise invalid_request("JSON object member name is not a string")
        _check_renderable(child, level, max_depth)


def render_canonical_json(value: JsonValue, *, max_depth: int, max_length: int = _DEFAULT_MAX_LENGTH) -> bytes:
    """Render a value as the exact canonical bytes :func:`parse_canonical_json` accepts.

    The output is self-checked by parsing it back under the same bounds, so a value this function returns is always
    accepted unchanged by the parser and ``render_canonical_json(parse_canonical_json(raw)) == raw``.

    Args:
        value: Value built from exactly (not subclasses of) ``None``, ``bool``, uint64 ``int``, finite non-negative
            ``Decimal`` with at most six fractional digits and no trailing zero, ``str``, ``list``, and ``dict`` with
            ASCII ``str`` keys.
        max_depth: Maximum permitted object/array nesting depth, as for :func:`parse_canonical_json`.
        max_length: Maximum permitted output length in bytes.

    Raises:
        ValueError: ``max_depth`` exceeds the implementation's caller-facing safety ceiling.
        ProtocolViolation: The value is outside the profile or the output exceeds the bounds. These failures have code
            ``invalid_request``.
    """
    if max_depth > MAX_DEPTH_CEILING:
        raise ValueError(f"max_depth must not exceed {MAX_DEPTH_CEILING}")
    _check_renderable(value, 0, max_depth)
    rendered = _render(value)
    parse_canonical_json(rendered, max_depth=max_depth, max_length=max_length)
    return rendered


def parse_canonical_json(
    raw: bytes,
    *,
    max_depth: int,
    max_length: int = _DEFAULT_MAX_LENGTH,
) -> JsonValue:
    """Parse bounded canonical JSON without normalization or binary-float conversion.

    Args:
        raw: Exact UTF-8 wire bytes to parse.
        max_depth: Maximum permitted object/array nesting depth. Zero permits only a
            scalar root; the caller must not exceed ``MAX_DEPTH_CEILING``.
        max_length: Maximum permitted length of ``raw`` in bytes.

    Returns:
        The parsed JSON value. Decimal values remain ``Decimal`` rather than being
        converted to binary floating point.

    Raises:
        ValueError: ``max_depth`` exceeds the implementation's caller-facing safety
            ceiling. This is a plain caller error, not a ``ProtocolViolation``.
        ProtocolViolation: The wire bytes, canonical form, depth, or length violate
            the profile. These failures have code ``invalid_request``.
    """
    if max_depth > MAX_DEPTH_CEILING:
        raise ValueError(f"max_depth must not exceed {MAX_DEPTH_CEILING}")
    if max_depth < 0 or max_length < 0 or len(raw) > max_length or raw.startswith(b"\xef\xbb\xbf"):
        raise invalid_request("JSON encoding or length is not canonical")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise invalid_request("JSON is not UTF-8") from None
    if _structural_depth_exceeds(raw, max_depth):
        raise invalid_request("JSON nesting is too deep")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_object,
            parse_float=Decimal,
            parse_int=_parse_uint64,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, InvalidOperation, RecursionError, ValueError):
        raise invalid_request("JSON syntax is invalid") from None
    typed = cast(JsonValue, value)
    if _exceeds_depth(typed, max_depth):
        raise invalid_request("JSON nesting is too deep")
    if _render(typed) != raw:
        raise invalid_request("JSON is not in canonical form")
    return typed
