"""Unit contract for the Python REM protocol-v1 primitive layer."""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import subprocess
import sys
import textwrap
from collections.abc import Callable, Iterator
from functools import partial
from typing import Any, cast

import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from remote_energy_meter.protocol_v1 import (
    ProtocolViolation,
    SignedRequest,
    decode_base64url,
    decode_transcript,
    encode_base64url,
    encode_transcript,
    parse_canonical_json,
    parse_content_digest,
    parse_p256_public_jwk,
    render_content_digest,
    route_profile,
    verify_content_digest,
    verify_p256_p1363,
)
from remote_energy_meter.protocol_v1.canonical_json import MAX_DEPTH_CEILING
from remote_energy_meter.protocol_v1.crypto import P256_ORDER

VALID_JWK = (
    b'{"crv":"P-256","kty":"EC","x":"qXlTk75KQKYN-PBJPDM4qHw8tYKgDLaqixcHrthpc_U",'
    b'"y":"tdXohmn4d0O78ACF989uLrxgHWAryPJteiOQIvBjRDo"}'
)
THUMBPRINT = bytes.fromhex("5ef5d38f326668a5c96967e9b15f66507e0ad8fa4017a4a35a385b70237d7485")


def _assert_code(code: str, operation: Callable[[], object]) -> None:
    with pytest.raises(ProtocolViolation) as caught:
        operation()
    assert caught.value.code == code
    assert str(caught.value), "a classified failure should remain diagnosable without exposing input"


def test_protocol_violation_exposes_a_stable_machine_code() -> None:
    violation = ProtocolViolation("invalid_request", "malformed test input")

    assert violation.code == "invalid_request"
    assert violation.args == ("malformed test input",)


@pytest.mark.parametrize("length", [16, 32, 64, 128, 256])
def test_base64url_round_trip_requires_the_declared_exact_length(length: int) -> None:
    raw = bytes(index % 251 for index in range(length))

    encoded = encode_base64url(raw, exact_length=length)

    assert encoded == base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    assert decode_base64url(encoded, exact_length=length) == raw
    _assert_code("invalid_request", lambda: encode_base64url(raw, exact_length=length + 1))
    _assert_code("invalid_request", lambda: decode_base64url(encoded, exact_length=length + 1))


@pytest.mark.parametrize(
    "value",
    [
        "ERERERERERERERERERERERERERERERERERERERERERE=",
        " ERERERERERERERERERERERERERERERERERERERERERE",
        "ERERERERERERERERERERERERERERERERERERERERER+",
        "ERERERERERERERERERERERERERERERERERERERERERF",
        "é",
    ],
)
def test_base64url_rejects_noncanonical_spellings(value: str) -> None:
    _assert_code("invalid_request", lambda: decode_base64url(value, exact_length=32))


def test_transcript_round_trip_preserves_domain_order_names_and_raw_values() -> None:
    fields = (("role", b"reporter"), ("nonce", b"\x11" * 32))

    encoded = encode_transcript("rem-interactive-commit-v1", fields, max_length=4096)

    assert encoded.startswith(b"REM-TRANSCRIPT\x01\x00\x19rem-interactive-commit-v1")
    assert (
        decode_transcript(
            encoded,
            purpose="rem-interactive-commit-v1",
            fields=("role", "nonce"),
            max_length=4096,
        )
        == fields
    )


def test_transcript_decoder_rejects_wrong_domain_order_duplicates_omissions_and_bounds() -> None:
    fields = (("role", b"reporter"), ("nonce", b"\x11" * 32))
    encoded = encode_transcript("rem-interactive-commit-v1", fields, max_length=4096)

    bad_calls: list[Callable[[], object]] = [
        lambda: decode_transcript(
            encoded,
            purpose="rem-control-pairing-commit-v1",
            fields=("role", "nonce"),
            max_length=4096,
        ),
        lambda: decode_transcript(
            encoded,
            purpose="rem-interactive-commit-v1",
            fields=("nonce", "role"),
            max_length=4096,
        ),
        lambda: decode_transcript(
            encoded,
            purpose="rem-interactive-commit-v1",
            fields=("role",),
            max_length=4096,
        ),
        lambda: encode_transcript(
            "rem-interactive-commit-v1",
            (("role", b"reporter"), ("role", b"core")),
            max_length=4096,
        ),
        lambda: decode_transcript(
            encoded,
            purpose="rem-interactive-commit-v1",
            fields=("role", "nonce"),
            max_length=len(encoded) - 1,
        ),
        lambda: decode_transcript(
            encoded[:-1],
            purpose="rem-interactive-commit-v1",
            fields=("role", "nonce"),
            max_length=4096,
        ),
    ]
    for call in bad_calls:
        _assert_code("invalid_request", call)


def test_p256_jwk_parser_returns_the_exact_canonical_identity() -> None:
    parsed = parse_p256_public_jwk(VALID_JWK)

    assert parsed.canonical_bytes == VALID_JWK
    assert parsed.x == decode_base64url("qXlTk75KQKYN-PBJPDM4qHw8tYKgDLaqixcHrthpc_U", exact_length=32)
    assert parsed.y == decode_base64url("tdXohmn4d0O78ACF989uLrxgHWAryPJteiOQIvBjRDo", exact_length=32)
    assert parsed.public_key.public_numbers() == ec.EllipticCurvePublicNumbers(
        int.from_bytes(parsed.x, "big"), int.from_bytes(parsed.y, "big"), ec.SECP256R1()
    )
    assert parsed.thumbprint_bytes == THUMBPRINT
    assert parsed.thumbprint == "XvXTjzJmaKXJaWfpsV9mUH4K2PpAF6SjWjhbcCN9dIU"


@pytest.mark.parametrize(
    "jwk",
    [
        (
            b'{"kty":"EC","crv":"P-256",'
            b'"x":"qXlTk75KQKYN-PBJPDM4qHw8tYKgDLaqixcHrthpc_U",'
            b'"y":"tdXohmn4d0O78ACF989uLrxgHWAryPJteiOQIvBjRDo"}'
        ),
        (
            b'{"crv": "P-256", "kty":"EC",'
            b'"x":"qXlTk75KQKYN-PBJPDM4qHw8tYKgDLaqixcHrthpc_U",'
            b'"y":"tdXohmn4d0O78ACF989uLrxgHWAryPJteiOQIvBjRDo"}'
        ),
        VALID_JWK[:-1] + b',"d":"ERERERERERERERERERERERERERERERERERERERERERE"}',
        VALID_JWK.replace(b'"P-256"', b'"P-384"'),
        VALID_JWK.replace(b"qXlTk75KQKYN-PBJPDM4qHw8tYKgDLaqixcHrthpc_U", b"A" * 43),
        VALID_JWK.replace(
            b"qXlTk75KQKYN-PBJPDM4qHw8tYKgDLaqixcHrthpc_U", b"qXlTk75KQKYN-PBJPDM4qHw8tYKgDLaqixcHrthpc_U="
        ),
    ],
)
def test_p256_jwk_parser_rejects_noncanonical_private_or_invalid_points(jwk: bytes) -> None:
    _assert_code("invalid_request", lambda: parse_p256_public_jwk(jwk))


def test_content_digest_is_exact_rfc9530_sha256() -> None:
    body = b'{"state":"available","watts":12.5}'
    expected_digest = hashlib.sha256(body).digest()
    header = "sha-256=:0kxpPrj5n3JB2UP9HDOUSY3rZ0PaDU+raWZlU4EZ730=:"

    assert render_content_digest(body) == header
    assert parse_content_digest(header) == expected_digest
    verify_content_digest(header, body)


@pytest.mark.parametrize(
    "header",
    [
        "sha-512=:" + "A" * 86 + "==:",
        "sha-256=:AAAA:",
        "sha-256=:0kxpPrj5n3JB2UP9HDOUSY3rZ0PaDU+raWZlU4EZ730=:, sha-256=:AAAA:",
        " SHA-256=:0kxpPrj5n3JB2UP9HDOUSY3rZ0PaDU+raWZlU4EZ730=: ",
        "x" * 257,
    ],
)
def test_content_digest_parser_rejects_every_nonprofile_form(header: str) -> None:
    _assert_code("invalid_request", lambda: parse_content_digest(header))


def test_content_digest_verification_binds_the_exact_body_bytes() -> None:
    header = render_content_digest(b'{"state":"available","watts":12.5}')
    _assert_code(
        "invalid_request",
        lambda: verify_content_digest(header, b'{"watts":12.5,"state":"available"}'),
    )


@pytest.mark.parametrize(
    "body",
    [
        b'{"b":2,"a":1}',
        b'{"a":1, "b":2}',
        b'{"a":1,"a":1}',
        b'\xef\xbb\xbf{"a":1}',
        b'{"value":1.0}',
        b'{"value":1e0}',
        b'{"value":-0}',
        b'{"text":"\\u0041"}',
        b"[[[[[[[[[0]]]]]]]]]",
        b"\xff",
    ],
)
def test_canonical_json_rejects_noncanonical_or_overdeep_input(body: bytes) -> None:
    _assert_code("invalid_request", lambda: parse_canonical_json(body, max_depth=8))


def test_canonical_json_returns_exact_integer_and_profile_decimal_values() -> None:
    parsed = parse_canonical_json(b'{"count":18446744073709551615,"watts":12.5}', max_depth=8)

    assert parsed == {"count": 18446744073709551615, "watts": 12.5}
    assert isinstance(parsed, dict)
    assert isinstance(parsed["count"], int)


def test_route_profile_and_request_values_are_immutable() -> None:
    profile = route_profile("control.roster.events")
    request = SignedRequest(
        method=b"GET",
        authority=b"control.example.com:8444",
        target=b"/control/v1/roster/events?after_generation=7&wait_seconds=25",
        headers=((b"rem-purpose", b"rem-control-roster-events-v1"),),
        body=b"",
    )

    assert profile.id == "control.roster.events"
    assert profile.method == b"GET"
    assert profile.path_template == b"/control/v1/roster/events"
    assert profile.purpose == (b"rem-control-roster-events-v1",)
    with pytest.raises((dataclasses.FrozenInstanceError, AttributeError, TypeError)):
        profile.method = b"POST"  # type: ignore[misc]
    with pytest.raises((dataclasses.FrozenInstanceError, AttributeError, TypeError)):
        request.target = b"/control/v1/roster/snapshot"  # type: ignore[misc]


def test_route_lookup_rejects_unknown_ids_instead_of_guessing_a_profile() -> None:
    _assert_code("not_found", lambda: route_profile("control.unknown"))


def test_signed_request_copies_mutable_constructor_inputs() -> None:
    headers: list[tuple[bytes, bytes]] = [(b"rem-purpose", b"rem-report-v1")]
    request = SignedRequest(
        method=b"POST",
        authority=b"ha.example.com:58443",
        target=b"/api/v1/reports",
        headers=headers,
        body=b"{}",
    )
    headers.append((b"content-type", b"application/json"))

    assert request.headers == ((b"rem-purpose", b"rem-report-v1"),)
    assert json.dumps(request.headers[0][0].decode()) == '"rem-purpose"'


def test_signed_request_snapshots_every_mutable_bytes_like_input() -> None:
    method = bytearray(b"POST")
    authority = bytearray(b"ha.example.com:58443")
    target = bytearray(b"/api/v1/reports")
    header_name = bytearray(b"rem-purpose")
    header_value = bytearray(b"rem-report-v1")
    body = bytearray(b"{}")

    request = SignedRequest(
        method=cast(Any, method),
        authority=cast(Any, authority),
        target=cast(Any, target),
        headers=cast(Any, [(header_name, header_value)]),
        body=cast(Any, body),
    )
    for value in (method, authority, target, header_name, header_value, body):
        value[:] = b"x" * len(value)

    assert request.method == b"POST"
    assert request.authority == b"ha.example.com:58443"
    assert request.target == b"/api/v1/reports"
    assert request.headers == ((b"rem-purpose", b"rem-report-v1"),)
    assert request.body == b"{}"
    assert hash(request)


def test_canonical_json_rejects_integers_outside_uint64() -> None:
    for body in (
        b"-1",
        b"18446744073709551616",
        b'{"value":-1}',
        b'{"value":18446744073709551616}',
    ):
        _assert_code("invalid_request", partial(parse_canonical_json, body, max_depth=8))


def test_canonical_json_rejects_oversize_before_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_parse(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("oversize JSON reached the parser")

    monkeypatch.setattr("remote_energy_meter.protocol_v1.canonical_json.json.loads", unexpected_parse)
    _assert_code(
        "invalid_request",
        lambda: parse_canonical_json(b"0" * 4097, max_depth=8, max_length=4096),
    )


def test_canonical_json_classifies_deep_input_without_leaking_recursion_error() -> None:
    body = b"[" * 500 + b"0" + b"]" * 500

    _assert_code("invalid_request", lambda: parse_canonical_json(body, max_depth=8))


def test_p256_jwk_rejects_oversize_before_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_parse(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("oversize JWK reached the parser")

    monkeypatch.setattr("remote_energy_meter.protocol_v1.crypto.json.loads", unexpected_parse)
    _assert_code("invalid_request", lambda: parse_p256_public_jwk(b"x" * 257))


def test_base64url_rejects_impossible_size_before_decoding(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_decode(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("impossible base64url reached the decoder")

    monkeypatch.setattr("remote_energy_meter.protocol_v1.encoding.base64.b64decode", unexpected_decode)
    _assert_code("invalid_request", lambda: decode_base64url("A" * 4096, exact_length=32))


def test_transcript_encoder_stops_at_the_bound_before_consuming_more_fields() -> None:
    class OversizeFirstField:
        def __iter__(self) -> Iterator[tuple[str, bytes]]:
            yield ("first", b"x" * 64)
            raise AssertionError("encoder consumed fields after exceeding its bound")

    _assert_code(
        "invalid_request",
        lambda: encode_transcript("p", cast(Any, OversizeFirstField()), max_length=32),
    )


def test_transcript_encoder_rejects_overbound_names_before_encoding_them() -> None:
    class EncodeGuard(str):
        def encode(self, *_args: object, **_kwargs: object) -> bytes:
            raise AssertionError("overbound transcript text was encoded before rejection")

    _assert_code(
        "invalid_request",
        lambda: encode_transcript(EncodeGuard("p" * 64), (), max_length=32),
    )
    _assert_code(
        "invalid_request",
        lambda: encode_transcript(
            "p",
            ((EncodeGuard("field" * 16), b""),),
            max_length=32,
        ),
    )


def test_content_digest_size_check_does_not_encode_oversize_text() -> None:
    class EncodeGuard(str):
        def encode(self, *_args: object, **_kwargs: object) -> bytes:
            raise AssertionError("oversize Content-Digest was encoded before rejection")

    _assert_code("invalid_request", lambda: parse_content_digest(EncodeGuard("x" * 257)))


def test_protocol_errors_do_not_retain_parser_exceptions_with_rejected_input() -> None:
    marker = b"private-input-marker"
    with pytest.raises(ProtocolViolation) as caught:
        parse_canonical_json(b'"' + marker + b"\xff" + b'"', max_depth=8)

    assert caught.value.__cause__ is None


# The probe runs in a fresh interpreter: if the parser recurses on attacker-controlled
# nesting, a small worker-thread stack overflows and kills the whole process, which
# would otherwise take the test session down with it.
_SMALL_STACK_DEEP_NESTING_PROBE = textwrap.dedent(
    """
    import threading

    from remote_energy_meter.protocol_v1 import ProtocolViolation, parse_canonical_json

    outcome = []


    def probe():
        try:
            parse_canonical_json(b"[" * 65536, max_depth=8, max_length=65536)
        except ProtocolViolation as error:
            outcome.append("violation:" + error.code)
        except BaseException as error:
            outcome.append("unexpected:" + type(error).__name__)
        else:
            outcome.append("accepted")


    previous = threading.stack_size(128 * 1024)
    try:
        worker = threading.Thread(target=probe)
        worker.start()
        worker.join()
    finally:
        threading.stack_size(previous)
    print(outcome[0] if outcome else "no-outcome")
    """
)


def test_canonical_json_rejects_deep_nesting_without_recursing_on_a_small_thread_stack() -> None:
    completed = subprocess.run(
        [sys.executable, "-c", _SMALL_STACK_DEEP_NESTING_PROBE],
        capture_output=True,
        check=False,
        env={"PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": ":".join(sys.path)},
        text=True,
        timeout=60,
    )

    assert completed.returncode == 0, f"deep-nesting probe crashed the interpreter (rc={completed.returncode})"
    assert completed.stdout.strip() == "violation:invalid_request"


def test_canonical_json_rejects_overdeep_input_before_invoking_the_recursive_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_parse(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("over-deep JSON reached the recursive parser")

    monkeypatch.setattr("remote_energy_meter.protocol_v1.canonical_json.json.loads", unexpected_parse)
    for body in (b"[" * 9 + b"]" * 9, b'{"a":' * 8 + b"{}" + b"}" * 8, b"[" * 65536):
        _assert_code("invalid_request", partial(parse_canonical_json, body, max_depth=8))


@pytest.mark.parametrize(
    "body",
    [
        b'{"a":"[[[[[[[[[[[["}',
        b'{"a":"{{{{{{{{{{{{"}',
        b'{"a":"\\"[[[[[[[[[[[["}',
        b'{"a":"\\\\","b":"[[[[[[[[[[[["}',
        b'["' + b"[" * 4096 + b'"]',
    ],
)
def test_canonical_json_brackets_inside_strings_do_not_count_toward_depth(body: bytes) -> None:
    parsed = parse_canonical_json(body, max_depth=1)

    assert isinstance(parsed, (dict, list))


@pytest.mark.parametrize("depth", [600, 3000])
def test_canonical_json_excessive_max_depth_never_leaks_recursion_error(depth: int) -> None:
    body = b"[" * depth + b"]" * depth

    with pytest.raises((ProtocolViolation, ValueError)) as caught:
        parse_canonical_json(body, max_depth=10**6, max_length=10**6)

    assert not isinstance(caught.value, RecursionError)
    if isinstance(caught.value, ProtocolViolation):
        assert caught.value.code == "invalid_request"


@pytest.mark.parametrize(
    ("body", "accepted"),
    [
        (b"[" * 8 + b"]" * 8, True),
        (b"[" * 9 + b"]" * 9, False),
        (b'{"a":' * 7 + b"{}" + b"}" * 7, True),
        (b'{"a":' * 8 + b"{}" + b"}" * 8, False),
        (b'{"a":' * 4 + b"[" * 4 + b"]" * 4 + b"}" * 4, True),
        (b'{"a":' * 4 + b"[" * 5 + b"]" * 5 + b"}" * 4, False),
    ],
)
def test_canonical_json_depth_bound_is_inclusive(body: bytes, accepted: bool) -> None:
    if accepted:
        assert parse_canonical_json(body, max_depth=8) is not None
    else:
        _assert_code("invalid_request", partial(parse_canonical_json, body, max_depth=8))


def test_canonical_json_max_depth_ceiling_is_inclusive_and_exceeding_it_is_a_caller_error() -> None:
    assert parse_canonical_json(b"[]", max_depth=MAX_DEPTH_CEILING) == []

    with pytest.raises(ValueError, match="max_depth must not exceed") as caught:
        parse_canonical_json(b"[]", max_depth=MAX_DEPTH_CEILING + 1)

    assert not isinstance(caught.value, ProtocolViolation)


@pytest.mark.parametrize(
    "fields",
    [
        (),
        (("role", b"reporter"),),
        (("role", b"reporter"), ("nonce", b"\x11" * 32)),
        (("empty", b""),),
    ],
)
def test_transcript_encoder_accepts_exactly_max_length_and_rejects_one_byte_less(
    fields: tuple[tuple[str, bytes], ...],
) -> None:
    purpose = "rem-interactive-commit-v1"
    header_length = len(b"REM-TRANSCRIPT\x01") + 2 + len(purpose)
    exact = header_length + sum(2 + len(name) + 4 + len(value) for name, value in fields)

    encoded = encode_transcript(purpose, fields, max_length=exact)

    assert len(encoded) == exact
    assert decode_transcript(encoded, purpose=purpose, fields=tuple(name for name, _ in fields), max_length=exact) == (
        fields
    )
    _assert_code("invalid_request", lambda: encode_transcript(purpose, fields, max_length=exact - 1))


@pytest.mark.parametrize(
    "signature",
    [
        b"\x00" * 63,
        b"\x00" * 65,
        b"\x00" * 32 + (1).to_bytes(32, "big"),
        P256_ORDER.to_bytes(32, "big") + (1).to_bytes(32, "big"),
        (1).to_bytes(32, "big") + b"\x00" * 32,
        (1).to_bytes(32, "big") + (P256_ORDER // 2 + 1).to_bytes(32, "big"),
        (1).to_bytes(32, "big") + P256_ORDER.to_bytes(32, "big"),
    ],
)
def test_p1363_rejects_every_length_and_scalar_boundary_outside_low_s_profile(signature: bytes) -> None:
    public_key = parse_p256_public_jwk(VALID_JWK).public_key

    with pytest.raises(ProtocolViolation) as caught:
        verify_p256_p1363(public_key, b"message", signature)

    assert caught.value.code == "authentication_required"
