"""Rust-to-Python interoperability for the protocol-v1 reporter request signature.

``fixtures/protocol_v1_rust_signed_report.json`` is the exact, unmodified stdout of the reporter repository's
``cargo run --locked --example protocol_v1_sign_demo`` at its merged commit ``244cd1d``. The Rust example signs one
``reporter.report.create`` request with an ephemeral in-memory P-256 key that it never serializes, so the fixture holds
only public verification material and the exact request bytes. The authority is the RFC 2606 name ``ha.example.com``.

The opposite direction, Python-produced vectors verified by Rust, is covered by the reporter's own conformance tests
against the server-owned corpus in ``docs/protocol/v1/vectors/``.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final, cast

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from remote_energy_meter.protocol_v1 import (
    ProtocolViolation,
    SignedRequest,
    VerificationKey,
    parse_canonical_json,
    parse_p256_public_jwk,
    route_profile,
    verify_signed_request,
)
from remote_energy_meter.protocol_v1.crypto import P256PublicJwk

FIXTURE: Final = Path(__file__).parent / "fixtures" / "protocol_v1_rust_signed_report.json"
P256_ORDER: Final = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
FIXTURE_KEYS: Final = frozenset(
    {"public_jwk", "request", "route", "signature", "signature_base_hex", "signature_input", "stable_identity"}
)
REQUEST_KEYS: Final = frozenset({"authority_hex", "body_hex", "headers", "method_hex", "target_hex"})
PRIVATE_MEMBER_NAMES: Final = frozenset({"d", "private_jwk", "private_key", "signing_key", "private_key_hex"})
JsonObject = dict[str, Any]


def _raw() -> bytes:
    return FIXTURE.read_bytes()


def _fixture() -> JsonObject:
    value = parse_canonical_json(_raw(), max_depth=4)
    assert isinstance(value, dict)
    return cast(JsonObject, value)


def _public_jwk(fixture: JsonObject) -> P256PublicJwk:
    # The fixture is canonical JSON, so the nested object's canonical bytes are its sorted compact form.
    member = json.dumps(fixture["public_jwk"], separators=(",", ":"), sort_keys=True).encode("ascii")
    return parse_p256_public_jwk(member)


def _request(fixture: JsonObject) -> SignedRequest:
    request = fixture["request"]
    return SignedRequest(
        method=bytes.fromhex(request["method_hex"]),
        authority=bytes.fromhex(request["authority_hex"]),
        target=bytes.fromhex(request["target_hex"]),
        headers=[(bytes.fromhex(item["name_hex"]), bytes.fromhex(item["value_hex"])) for item in request["headers"]],
        body=bytes.fromhex(request["body_hex"]),
    )


def _verify(fixture: JsonObject, request: SignedRequest, jwk: P256PublicJwk | None = None) -> Any:
    key = _public_jwk(fixture) if jwk is None else jwk
    return verify_signed_request(
        request,
        route=route_profile(fixture["route"]),
        # The trusted identity comes from server state, never from the fixture's claim.
        keys={"rem": VerificationKey(identity=_public_jwk(fixture).thumbprint, public_jwk=key)},
        expected_authority=b"ha.example.com:58443",
        received_at=0,
        active_control_generation=None,
    )


def _signature_bytes(fixture: JsonObject) -> bytes:
    label, separator, value = fixture["signature"].partition("=")
    assert (label, separator) == ("rem", "=")
    assert value.startswith(":") and value.endswith(":")
    return base64.b64decode(value[1:-1], validate=True)


def _walk_keys(value: object) -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _walk_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_keys(item)


def _flip_at(raw: bytes, index: int) -> bytes:
    # XOR 0x01 never toggles ASCII letter case (0x20), so a mutated field name is a different name, not an alias.
    return raw[:index] + bytes([raw[index] ^ 0x01]) + raw[index + 1 :]


def test_fixture_is_canonical_closed_and_public_only() -> None:
    fixture = _fixture()

    assert set(fixture) == FIXTURE_KEYS
    assert set(fixture["request"]) == REQUEST_KEYS
    assert fixture["route"] == "reporter.report.create"
    assert not PRIVATE_MEMBER_NAMES & set(_walk_keys(fixture))
    assert set(fixture["public_jwk"]) == {"crv", "kty", "x", "y"}


def test_rust_signed_request_verifies_in_python_with_identical_bound_bytes() -> None:
    fixture = _fixture()
    jwk = _public_jwk(fixture)
    request = _request(fixture)

    verified = _verify(fixture, request)

    assert verified.route_id == "reporter.report.create"
    assert verified.verified_identity == jwk.thumbprint == fixture["stable_identity"]
    assert dict(verified.signature_bases)["rem"] == bytes.fromhex(fixture["signature_base_hex"])
    assert dict(verified.signature_inputs)["rem"] == fixture["signature_input"].removeprefix("rem=").encode("ascii")
    assert dict(verified.signatures)["rem"] == _signature_bytes(fixture)
    assert dict(verified.keyids)["rem"] == fixture["stable_identity"]
    # The reporter profile transmits no client clock.
    assert b";created=" not in dict(verified.signature_inputs)["rem"]


def test_rust_signature_is_low_s_and_verifies_independently_of_the_package() -> None:
    fixture = _fixture()
    jwk = fixture["public_jwk"]
    signature = _signature_bytes(fixture)
    x = int.from_bytes(base64.urlsafe_b64decode(jwk["x"] + "="), "big")
    y = int.from_bytes(base64.urlsafe_b64decode(jwk["y"] + "="), "big")
    public_key = ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")

    assert len(signature) == 64
    assert 0 < r < P256_ORDER
    assert 0 < s <= P256_ORDER // 2
    public_key.verify(
        encode_dss_signature(r, s), bytes.fromhex(fixture["signature_base_hex"]), ec.ECDSA(hashes.SHA256())
    )


def _field_mutations() -> Iterator[tuple[str, int]]:
    fixture = _fixture()
    request = fixture["request"]
    for field in ("method_hex", "authority_hex", "target_hex", "body_hex"):
        yield from ((field, index) for index in range(len(bytes.fromhex(request[field]))))
    for position, header in enumerate(request["headers"]):
        for part in ("name_hex", "value_hex"):
            yield from ((f"headers[{position}].{part}", index) for index in range(len(bytes.fromhex(header[part]))))


def _mutated_request(fixture: JsonObject, field: str, index: int) -> SignedRequest:
    request = _request(fixture)
    if field.startswith("headers["):
        position = int(field[len("headers[") : field.index("]")])
        part = 0 if field.endswith(".name_hex") else 1
        headers = list(request.headers)
        pair = list(headers[position])
        pair[part] = _flip_at(pair[part], index)
        headers[position] = (pair[0], pair[1])
        return SignedRequest(request.method, request.authority, request.target, headers, request.body)
    attribute = field.removesuffix("_hex")
    values = {
        "method": request.method,
        "authority": request.authority,
        "target": request.target,
        "body": request.body,
    }
    values[attribute] = _flip_at(values[attribute], index)
    return SignedRequest(values["method"], values["authority"], values["target"], request.headers, values["body"])


FIELD_MUTATIONS: Final = tuple(_field_mutations())


def test_every_request_byte_is_covered_by_a_mutation() -> None:
    fixture = _fixture()
    request = _request(fixture)
    expected = (
        len(request.method)
        + len(request.authority)
        + len(request.target)
        + len(request.body)
        + sum(len(name) + len(value) for name, value in request.headers)
    )

    assert len(FIELD_MUTATIONS) == expected
    assert len(set(FIELD_MUTATIONS)) == expected


@pytest.mark.parametrize("field", sorted({field for field, _ in FIELD_MUTATIONS}))
def test_every_one_byte_request_mutation_of_the_rust_fixture_is_rejected(field: str) -> None:
    fixture = _fixture()
    accepted = []
    for mutated_field, index in FIELD_MUTATIONS:
        if mutated_field != field:
            continue
        try:
            _verify(fixture, _mutated_request(fixture, field, index))
        except ProtocolViolation:
            continue
        accepted.append(index)

    assert accepted == [], f"{field}: one-byte mutations at {accepted} were accepted"


def test_every_one_byte_raw_signature_mutation_is_rejected() -> None:
    fixture = _fixture()
    request = _request(fixture)
    signature = _signature_bytes(fixture)
    for index in range(len(signature)):
        encoded = base64.b64encode(_flip_at(signature, index)).decode("ascii")
        headers = [
            (name, f"rem=:{encoded}:".encode("ascii") if name == b"signature" else value)
            for name, value in request.headers
        ]
        mutated = SignedRequest(request.method, request.authority, request.target, headers, request.body)
        with pytest.raises(ProtocolViolation) as caught:
            _verify(fixture, mutated)
        assert caught.value.code == "authentication_required", index


@pytest.mark.parametrize("coordinate", ["x", "y"])
def test_every_one_byte_public_key_coordinate_mutation_is_rejected_at_parse(coordinate: str) -> None:
    fixture = _fixture()
    raw = base64.urlsafe_b64decode(fixture["public_jwk"][coordinate] + "=")
    for index in range(len(raw)):
        member = dict(fixture["public_jwk"])
        member[coordinate] = base64.urlsafe_b64encode(_flip_at(raw, index)).rstrip(b"=").decode("ascii")
        encoded = json.dumps(member, separators=(",", ":"), sort_keys=True).encode("ascii")
        # A one-byte coordinate change leaves the point off P-256 for this fixture's key.
        with pytest.raises(ProtocolViolation) as caught:
            parse_p256_public_jwk(encoded)
        assert caught.value.code == "invalid_request", index


def test_a_different_valid_key_bound_to_the_same_identity_is_rejected() -> None:
    fixture = _fixture()
    numbers = ec.generate_private_key(ec.SECP256R1()).public_key().public_numbers()
    member = {
        "crv": "P-256",
        "kty": "EC",
        "x": base64.urlsafe_b64encode(numbers.x.to_bytes(32, "big")).rstrip(b"=").decode("ascii"),
        "y": base64.urlsafe_b64encode(numbers.y.to_bytes(32, "big")).rstrip(b"=").decode("ascii"),
    }
    other = parse_p256_public_jwk(json.dumps(member, separators=(",", ":"), sort_keys=True).encode("ascii"))

    with pytest.raises(ProtocolViolation) as caught:
        _verify(fixture, _request(fixture), other)
    assert caught.value.code == "authentication_required"
