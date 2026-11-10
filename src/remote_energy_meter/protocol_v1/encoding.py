"""Canonical base64url and bounded REM-TRANSCRIPT encodings."""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Sequence
from typing import Final

from remote_energy_meter.protocol_v1.errors import invalid_request

type TranscriptField = tuple[str, bytes]
_TRANSCRIPT_PREFIX: Final = b"REM-TRANSCRIPT\x01"
_BASE64URL: Final = re.compile(r"[A-Za-z0-9_-]*\Z")


def _check_exact_length(value: bytes, exact_length: int) -> None:
    if exact_length < 0 or len(value) != exact_length:
        raise invalid_request("base64url value has the wrong decoded length")


def encode_base64url(value: bytes, *, exact_length: int) -> str:
    """Encode an exact-length byte value as canonical unpadded base64url.

    Args:
        value: Bytes to encode.
        exact_length: Required decoded byte length.

    Returns:
        The canonical unpadded base64url text.

    Raises:
        ProtocolViolation: ``exact_length`` is negative or ``value`` has another
            length. The failure has code ``invalid_request``.
    """
    _check_exact_length(value, exact_length)
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def decode_base64url(value: str, *, exact_length: int) -> bytes:
    """Decode canonical unpadded base64url with an exact typed length.

    Args:
        value: ASCII base64url text without padding.
        exact_length: Required decoded byte length.

    Returns:
        The decoded bytes.

    Raises:
        ProtocolViolation: The text is non-canonical, malformed, or has another
            decoded length. The failure has code ``invalid_request``.
    """
    encoded_length = (exact_length * 8 + 5) // 6 if exact_length >= 0 else -1
    if len(value) != encoded_length or not _BASE64URL.fullmatch(value) or "=" in value:
        raise invalid_request("base64url value is not canonical")
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError):
        raise invalid_request("base64url value is malformed") from None
    _check_exact_length(raw, exact_length)
    if base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != value:
        raise invalid_request("base64url value has non-zero unused bits")
    return raw


def decode_opaque_base64url(value: str, *, minimum: int, maximum: int) -> bytes:
    """Decode a canonical unpadded base64url value within closed byte bounds."""
    maximum_encoded_length = (maximum * 8 + 5) // 6 if maximum >= 0 else -1
    if (
        minimum < 0
        or maximum < minimum
        or len(value) > maximum_encoded_length
        or not _BASE64URL.fullmatch(value)
        or "=" in value
    ):
        raise invalid_request("opaque base64url value is not canonical")
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError):
        raise invalid_request("opaque base64url value is malformed") from None
    if not minimum <= len(raw) <= maximum:
        raise invalid_request("opaque base64url value has the wrong decoded length")
    if base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != value:
        raise invalid_request("opaque base64url value has non-zero unused bits")
    return raw


def _ascii(value: str, kind: str) -> bytes:
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError:
        raise invalid_request(f"transcript {kind} is not ASCII") from None
    if not encoded:
        raise invalid_request(f"transcript {kind} is empty")
    return encoded


def encode_transcript(
    purpose: str,
    fields: Sequence[TranscriptField],
    *,
    max_length: int,
) -> bytes:
    """Encode one bounded, ordered REM-TRANSCRIPT value.

    Args:
        purpose: Non-empty ASCII transcript purpose.
        fields: Ordered, uniquely named ASCII fields and their byte values.
        max_length: Maximum encoded transcript length in bytes.

    Returns:
        The exact ``REM-TRANSCRIPT`` bytes.

    Raises:
        ProtocolViolation: The purpose, fields, or bound violate the transcript
            profile. The failure has code ``invalid_request``.
    """
    if max_length <= 0:
        raise invalid_request("transcript bound is invalid")
    if len(purpose) > 0xFFFF or len(_TRANSCRIPT_PREFIX) + 2 + len(purpose) > max_length:
        raise invalid_request("transcript purpose exceeds its bound")
    purpose_bytes = _ascii(purpose, "purpose")

    chunks = [_TRANSCRIPT_PREFIX, len(purpose_bytes).to_bytes(2, "big"), purpose_bytes]
    encoded_length = len(_TRANSCRIPT_PREFIX) + 2 + len(purpose_bytes)
    names: set[str] = set()
    for name, value in fields:
        if name in names:
            raise invalid_request("transcript field is duplicated")
        names.add(name)
        if len(name) > 0xFFFF or len(value) > 0xFFFFFFFF:
            raise invalid_request("transcript field is too long")
        if encoded_length + 2 + len(name) + 4 + len(value) > max_length:
            raise invalid_request("transcript exceeds its byte bound")
        name_bytes = _ascii(name, "field name")
        encoded_length += 2 + len(name_bytes) + 4 + len(value)
        if encoded_length > max_length:
            raise invalid_request("transcript exceeds its byte bound")
        chunks.extend(
            (
                len(name_bytes).to_bytes(2, "big"),
                name_bytes,
                len(value).to_bytes(4, "big"),
                value,
            )
        )
    return b"".join(chunks)


def _take(raw: bytes, cursor: int, length: int) -> tuple[bytes, int]:
    end = cursor + length
    if end > len(raw):
        raise invalid_request("transcript is truncated")
    return raw[cursor:end], end


def decode_transcript(
    raw: bytes,
    *,
    purpose: str,
    fields: Sequence[str],
    max_length: int,
) -> tuple[TranscriptField, ...]:
    """Decode a transcript against its exact purpose and ordered field names.

    Args:
        raw: Exact transcript bytes.
        purpose: Required non-empty ASCII purpose.
        fields: Required unique field names in wire order.
        max_length: Maximum permitted transcript length in bytes.

    Returns:
        The decoded ordered field-name/value pairs.

    Raises:
        ProtocolViolation: The bound, header, purpose, field set/order, lengths, or
            trailing bytes violate the profile. The failure has code
            ``invalid_request``.
    """
    if max_length <= 0 or len(raw) > max_length:
        raise invalid_request("transcript exceeds its byte bound")
    if not raw.startswith(_TRANSCRIPT_PREFIX):
        raise invalid_request("transcript header is invalid")
    cursor = len(_TRANSCRIPT_PREFIX)
    length_bytes, cursor = _take(raw, cursor, 2)
    purpose_bytes, cursor = _take(raw, cursor, int.from_bytes(length_bytes, "big"))
    if purpose_bytes != _ascii(purpose, "purpose"):
        raise invalid_request("transcript purpose does not match")

    if len(fields) != len(set(fields)):
        raise invalid_request("expected transcript fields are duplicated")
    decoded: list[TranscriptField] = []
    for expected_name in fields:
        name_length, cursor = _take(raw, cursor, 2)
        name_bytes, cursor = _take(raw, cursor, int.from_bytes(name_length, "big"))
        if name_bytes != _ascii(expected_name, "field name"):
            raise invalid_request("transcript field order does not match")
        value_length, cursor = _take(raw, cursor, 4)
        value, cursor = _take(raw, cursor, int.from_bytes(value_length, "big"))
        decoded.append((expected_name, value))
    if cursor != len(raw):
        raise invalid_request("transcript contains unknown fields")
    return tuple(decoded)
