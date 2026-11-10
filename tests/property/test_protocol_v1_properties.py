"""Generated properties for strict REM protocol-v1 encodings and signatures."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable
from functools import partial
from typing import Final

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from hypothesis import given, settings
from hypothesis import strategies as st

from remote_energy_meter.protocol_v1 import (
    ProtocolViolation,
    decode_base64url,
    decode_transcript,
    encode_base64url,
    encode_transcript,
    parse_canonical_json,
    parse_p256_public_jwk,
    render_content_digest,
    verify_content_digest,
    verify_p256_p1363,
)

P256_ORDER: Final = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
EXACT_LENGTHS: Final = st.sampled_from([16, 32, 64, 128, 256])
ASCII_NAMES: Final = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz_",
    min_size=1,
    max_size=24,
)


def _assert_invalid(operation: Callable[[], object]) -> None:
    with pytest.raises(ProtocolViolation) as caught:
        operation()
    assert caught.value.code == "invalid_request"


@st.composite
def exact_byte_strings(draw: st.DrawFn) -> tuple[int, bytes]:
    """Bytes paired with one profile-relevant exact length."""
    length = draw(EXACT_LENGTHS)
    return length, draw(st.binary(min_size=length, max_size=length))


@given(exact_byte_strings())
def test_base64url_round_trips_all_exact_bytes(case: tuple[int, bytes]) -> None:
    length, raw = case

    encoded = encode_base64url(raw, exact_length=length)

    assert encoded == base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    assert decode_base64url(encoded, exact_length=length) == raw


@given(exact_byte_strings())
def test_base64url_rejects_padding_whitespace_and_wrong_exact_lengths(case: tuple[int, bytes]) -> None:
    length, raw = case
    encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    _assert_invalid(lambda: decode_base64url(encoded + "=", exact_length=length))
    _assert_invalid(lambda: decode_base64url(" " + encoded, exact_length=length))
    _assert_invalid(lambda: decode_base64url(encoded, exact_length=length + 1))


@given(st.binary(min_size=1, max_size=32))
def test_base64url_rejects_nonzero_unused_trailing_bits(raw: bytes) -> None:
    encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    final_index = alphabet.index(encoded[-1])
    noncanonical = encoded[:-1] + alphabet[final_index ^ 1]

    if base64.urlsafe_b64decode(noncanonical + "=" * (-len(noncanonical) % 4)) == raw:
        _assert_invalid(lambda: decode_base64url(noncanonical, exact_length=len(raw)))


@st.composite
def transcript_cases(draw: st.DrawFn) -> tuple[str, tuple[tuple[str, bytes], ...]]:
    purpose = draw(st.text(alphabet="abcdefghijklmnopqrstuvwxyz-", min_size=1, max_size=48))
    names = draw(st.lists(ASCII_NAMES, min_size=0, max_size=8, unique=True))
    values = draw(st.lists(st.binary(max_size=64), min_size=len(names), max_size=len(names)))
    return purpose, tuple(zip(names, values, strict=True))


@given(transcript_cases())
def test_transcripts_round_trip_domain_order_names_and_arbitrary_bytes(
    case: tuple[str, tuple[tuple[str, bytes], ...]],
) -> None:
    purpose, fields = case

    encoded = encode_transcript(purpose, fields, max_length=4096)

    assert len(encoded) <= 4096
    assert (
        decode_transcript(
            encoded,
            purpose=purpose,
            fields=tuple(name for name, _ in fields),
            max_length=4096,
        )
        == fields
    )


@given(transcript_cases())
def test_transcripts_reject_any_truncation(case: tuple[str, tuple[tuple[str, bytes], ...]]) -> None:
    purpose, fields = case
    encoded = encode_transcript(purpose, fields, max_length=4096)
    expected_names = tuple(name for name, _ in fields)

    for cut in {0, 1, len(encoded) // 2, len(encoded) - 1}:
        _assert_invalid(
            partial(
                decode_transcript,
                encoded[:cut],
                purpose=purpose,
                fields=expected_names,
                max_length=4096,
            )
        )


@given(st.integers(min_value=1, max_value=P256_ORDER - 1))
@settings(max_examples=25)
def test_exact_canonical_jwk_round_trips_every_generated_valid_p256_point(private_value: int) -> None:
    public_numbers = ec.derive_private_key(private_value, ec.SECP256R1()).public_key().public_numbers()
    x = public_numbers.x.to_bytes(32, "big")
    y = public_numbers.y.to_bytes(32, "big")
    canonical = (
        b'{"crv":"P-256","kty":"EC","x":"'
        + base64.urlsafe_b64encode(x).rstrip(b"=")
        + b'","y":"'
        + base64.urlsafe_b64encode(y).rstrip(b"=")
        + b'"}'
    )

    parsed = parse_p256_public_jwk(canonical)

    assert parsed.canonical_bytes == canonical
    assert parsed.x == x
    assert parsed.y == y
    assert parsed.thumbprint_bytes == hashlib.sha256(canonical).digest()
    assert parsed.thumbprint == base64.urlsafe_b64encode(parsed.thumbprint_bytes).rstrip(b"=").decode("ascii")


@given(st.binary(max_size=4096))
def test_content_digest_round_trips_and_binds_every_body_byte(body: bytes) -> None:
    header = render_content_digest(body)

    assert header == f"sha-256=:{base64.b64encode(hashlib.sha256(body).digest()).decode('ascii')}:"
    verify_content_digest(header, body)
    changed = body + b"\x00"
    _assert_invalid(lambda: verify_content_digest(header, changed))


JSON_SCALARS: Final = st.one_of(st.none(), st.booleans(), st.integers(min_value=0, max_value=2**64 - 1), st.text())
JSON_OBJECTS: Final = st.dictionaries(
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz_", min_size=1, max_size=12),
    JSON_SCALARS,
    max_size=8,
)


@given(JSON_OBJECTS)
def test_canonical_json_accepts_the_unique_sorted_minimal_encoding(value: dict[str, object]) -> None:
    canonical = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()

    assert parse_canonical_json(canonical, max_depth=8) == value


@given(st.binary(max_size=256), st.integers(min_value=1, max_value=P256_ORDER - 1))
@settings(max_examples=25)
def test_p1363_accepts_low_s_and_rejects_the_mathematically_equivalent_high_s(
    message: bytes,
    private_value: int,
) -> None:
    private_key = ec.derive_private_key(private_value, ec.SECP256R1())
    der = private_key.sign(message, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    low_s = min(s, P256_ORDER - s)
    low_signature = r.to_bytes(32, "big") + low_s.to_bytes(32, "big")
    high_signature = r.to_bytes(32, "big") + (P256_ORDER - low_s).to_bytes(32, "big")

    verify_p256_p1363(private_key.public_key(), message, low_signature)
    with pytest.raises(ProtocolViolation) as caught:
        verify_p256_p1363(private_key.public_key(), message, high_signature)
    assert caught.value.code == "authentication_required"
