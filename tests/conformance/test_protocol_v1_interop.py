"""Rust-to-Python interoperability for the protocol-v1 reporter request signature.

``fixtures/protocol_v1_rust_signed_report.json`` is the exact, unmodified stdout of the reporter repository's
``cargo run --locked --example protocol_v1_sign_demo`` at its merged commit ``244cd1d`` (``REPORTER_COMMIT``). The
Rust example signs one ``reporter.report.create`` request with an ephemeral in-memory P-256 key that it never
serializes, so the fixture holds only public verification material and the exact request bytes. The authority is the
RFC 2606 name ``ha.example.com``.

The fixture is pinned by ``REPORTER_COMMIT``, ``FIXTURE_SHA256``, and ``EXPECTED_IDENTITY``. Regenerating it from a
newer reporter commit means replacing the file with that commit's unmodified output and updating all three together.

The server-owned corpus in ``docs/protocol/v1/vectors/`` was produced by a maintainer tool, not by either
implementation; the Python runtime tests and the reporter's Rust conformance tests each verify it independently. This
fixture is the only artifact that one implementation produced and the other verified.
"""

from __future__ import annotations

import base64
import hashlib
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
from tests.conformance.test_protocol_v1_artifacts import PRIVATE_KEY_NAMES, PUBLIC_EC_JWK_MEMBERS

FIXTURE: Final = Path(__file__).parent / "fixtures" / "protocol_v1_rust_signed_report.json"
REPORTER_COMMIT: Final = "244cd1d"
FIXTURE_SHA256: Final = "ed50da0f758eb408f7978b34f3adbcb637c418f08d70e8f2ffe9aca6bda47c71"
EXPECTED_IDENTITY: Final = "eK92MFIAjsOk8dCHImhsEZ8vFwnfFsNB_8JOG9OXx2w"
P256_ORDER: Final = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
FIXTURE_KEYS: Final = frozenset(
    {"public_jwk", "request", "route", "signature", "signature_base_hex", "signature_input", "stable_identity"}
)
REQUEST_KEYS: Final = frozenset({"authority_hex", "body_hex", "headers", "method_hex", "target_hex"})
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


def _verify(
    fixture: JsonObject, request: SignedRequest, jwk: P256PublicJwk | None = None, identity: str | None = None
) -> Any:
    # Model the server's key registry: by default the "rem" entry binds the fixture's public key to that key's own
    # thumbprint, as enrolment would store it. Neither the request's keyid nor the fixture's `stable_identity` claim
    # is trusted here. `jwk` and `identity` replace the bound key or identity for key-substitution tests.
    key = _public_jwk(fixture) if jwk is None else jwk
    trusted_identity = _public_jwk(fixture).thumbprint if identity is None else identity
    return verify_signed_request(
        request,
        route=route_profile(fixture["route"]),
        keys={"rem": VerificationKey(identity=trusted_identity, public_jwk=key)},
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


def test_fixture_is_the_pinned_reporter_output() -> None:
    assert hashlib.sha256(_raw()).hexdigest() == FIXTURE_SHA256
    fixture = _fixture()
    assert fixture["stable_identity"] == _public_jwk(fixture).thumbprint == EXPECTED_IDENTITY


def test_fixture_is_canonical_closed_and_public_only() -> None:
    fixture = _fixture()

    assert set(fixture) == FIXTURE_KEYS
    assert set(fixture["request"]) == REQUEST_KEYS
    assert fixture["route"] == "reporter.report.create"
    assert PRIVATE_KEY_NAMES.isdisjoint(_walk_keys(fixture))
    assert set(fixture["public_jwk"]) == PUBLIC_EC_JWK_MEMBERS


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


# The exact set of rejection codes each field's one-byte mutations produce, measured against the fixture. A field with
# more than one code has mutations that fail at different, deterministic stages (for example, a renamed header is
# missing from the covered components before its value would fail the signature).
EXPECTED_MUTATION_CODES: Final[dict[str, frozenset[str]]] = {
    "method_hex": frozenset({"method_not_allowed"}),
    "authority_hex": frozenset({"authentication_required"}),
    "target_hex": frozenset({"not_found", "invalid_request"}),
    "body_hex": frozenset({"invalid_request"}),
    # headers[0]: rem-purpose
    "headers[0].name_hex": frozenset({"invalid_request"}),
    "headers[0].value_hex": frozenset({"authentication_required", "invalid_request"}),
    # headers[1]: rem-session-id
    "headers[1].name_hex": frozenset({"invalid_request"}),
    "headers[1].value_hex": frozenset({"invalid_request"}),
    # headers[2]: rem-sequence
    "headers[2].name_hex": frozenset({"invalid_request"}),
    "headers[2].value_hex": frozenset({"authentication_required"}),
    # headers[3]: content-type
    "headers[3].name_hex": frozenset({"unsupported_media_type"}),
    "headers[3].value_hex": frozenset({"unsupported_media_type"}),
    # headers[4]: content-digest
    "headers[4].name_hex": frozenset({"invalid_request"}),
    "headers[4].value_hex": frozenset({"invalid_request"}),
    # headers[5]: signature-input
    "headers[5].name_hex": frozenset({"authentication_required", "invalid_request"}),
    "headers[5].value_hex": frozenset({"authentication_required"}),
    # headers[6]: signature
    "headers[6].name_hex": frozenset({"authentication_required", "invalid_request"}),
    "headers[6].value_hex": frozenset({"authentication_required"}),
}


def test_expected_mutation_codes_cover_exactly_the_mutated_fields() -> None:
    assert set(EXPECTED_MUTATION_CODES) == {field for field, _ in FIELD_MUTATIONS}


@pytest.mark.parametrize("field", sorted(EXPECTED_MUTATION_CODES))
def test_every_one_byte_request_mutation_of_the_rust_fixture_is_rejected(field: str) -> None:
    fixture = _fixture()
    accepted = []
    codes: dict[str, list[int]] = {}
    for mutated_field, index in FIELD_MUTATIONS:
        if mutated_field != field:
            continue
        try:
            _verify(fixture, _mutated_request(fixture, field, index))
        except ProtocolViolation as violation:
            codes.setdefault(violation.code, []).append(index)
            continue
        accepted.append(index)

    assert accepted == [], f"{field}: one-byte mutations at {accepted} were accepted"
    assert set(codes) == EXPECTED_MUTATION_CODES[field], f"{field}: rejection codes by mutated index {codes}"


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


# The P-256 base point G: a fixed, valid public key whose private scalar (1) is public, so it never signed the fixture.
P256_GENERATOR_X: Final = 0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296
P256_GENERATOR_Y: Final = 0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5


def _generator_jwk() -> P256PublicJwk:
    member = {
        "crv": "P-256",
        "kty": "EC",
        "x": base64.urlsafe_b64encode(P256_GENERATOR_X.to_bytes(32, "big")).rstrip(b"=").decode("ascii"),
        "y": base64.urlsafe_b64encode(P256_GENERATOR_Y.to_bytes(32, "big")).rstrip(b"=").decode("ascii"),
    }
    return parse_p256_public_jwk(json.dumps(member, separators=(",", ":"), sort_keys=True).encode("ascii"))


def _with_keyid(request: SignedRequest, old: str, new: str) -> SignedRequest:
    headers = [
        (name, value.replace(f'keyid="{old}"'.encode("ascii"), f'keyid="{new}"'.encode("ascii")))
        if name == b"signature-input"
        else (name, value)
        for name, value in request.headers
    ]
    assert headers != list(request.headers)
    return SignedRequest(request.method, request.authority, request.target, headers, request.body)


def test_a_substituted_key_whose_thumbprint_differs_from_the_signed_keyid_is_rejected() -> None:
    fixture = _fixture()
    other = _generator_jwk()
    assert other.thumbprint != fixture["stable_identity"]

    with pytest.raises(ProtocolViolation, match="key ID does not match") as caught:
        _verify(fixture, _request(fixture), other)
    assert caught.value.code == "authentication_required"


def test_a_substituted_key_matching_a_rewritten_keyid_fails_the_ecdsa_check() -> None:
    fixture = _fixture()
    other = _generator_jwk()
    # The keyid now names the substituted key and that key is the one trusted for its identity, so only the
    # signature itself (made by the fixture's key over the original base) can reject the request.
    request = _with_keyid(_request(fixture), fixture["stable_identity"], other.thumbprint)

    with pytest.raises(ProtocolViolation, match="P-256 signature verification failed") as caught:
        _verify(fixture, request, other, identity=other.thumbprint)
    assert caught.value.code == "authentication_required"
