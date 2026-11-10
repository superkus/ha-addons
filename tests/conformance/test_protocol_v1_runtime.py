"""Executable conformance contract for the Python protocol-v1 primitive layer."""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import hmac
import json
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from functools import partial
from pathlib import Path
from typing import Any, Final, NamedTuple, cast

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature

from remote_energy_meter.protocol_v1 import (
    ProtocolViolation,
    SignedRequest,
    VerificationKey,
    decode_transcript,
    encode_transcript,
    parse_canonical_json,
    parse_content_digest,
    parse_p256_public_jwk,
    render_content_digest,
    route_profile,
    verify_content_digest,
    verify_p256_p1363,
    verify_signed_request,
)
from remote_energy_meter.protocol_v1.http_signatures import (
    _ROUTES,
    RouteProfile,
    _canonical_authority,
    _components,
    _parse_signature_input,
)

ROOT: Final = Path(__file__).parents[2]
VECTORS: Final = ROOT / "docs" / "protocol" / "v1" / "vectors"
P256_ORDER: Final = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
JsonObject = dict[str, Any]


class VerificationCall(NamedTuple):
    """One immutable request and its environmental verification inputs."""

    request: SignedRequest
    route_id: str
    keys: Mapping[str, Any]
    expected_authority: bytes
    received_at: int
    active_control_generation: int | None


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> JsonObject:
    result: JsonObject = {}
    for key, value in pairs:
        if key in result:
            raise AssertionError(f"duplicate vector key {key!r}")
        result[key] = value
    return result


def _load(name: str) -> JsonObject:
    value = json.loads((VECTORS / name).read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)
    assert isinstance(value, dict)
    return cast(JsonObject, value)


def _cases(name: str) -> list[JsonObject]:
    values = _load(name)["cases"]
    assert isinstance(values, list)
    return cast(list[JsonObject], values)


def _case(name: str, case_id: str) -> JsonObject:
    return next(case for case in _cases(name) if case["id"] == case_id)


def _entries(case: JsonObject, group: str) -> dict[str, JsonObject]:
    values: Sequence[JsonObject]
    if group == "expected":
        values = cast(Sequence[JsonObject], cast(JsonObject, case[group])["outputs"])
    else:
        values = cast(Sequence[JsonObject], case[group])
    return {cast(str, value["name"]): value for value in values}


def _text(entries: Mapping[str, JsonObject], name: str) -> str:
    return cast(str, entries[name]["value"])


def _bytes(entry: JsonObject) -> bytes:
    encoding = entry["encoding"]
    value = cast(str, entry["value"])
    if encoding in {"ascii", "utf8"}:
        return value.encode("ascii" if encoding == "ascii" else "utf-8")
    if encoding == "hex":
        return bytes.fromhex(value)
    if encoding == "base64url":
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    raise AssertionError(f"entry is not byte-valued: {entry!r}")


def _flip(raw: bytes) -> bytes:
    assert raw
    return bytes([raw[0] ^ 1]) + raw[1:]


def _raise_code(code: str, operation: Callable[[], object]) -> None:
    with pytest.raises(ProtocolViolation) as caught:
        operation()
    assert caught.value.code == code


def _independent_request_hash(request: SignedRequest) -> bytes:
    digest = hashlib.sha256()
    for value in (request.method, request.authority, request.target, request.body):
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)
    digest.update(len(request.headers).to_bytes(4, "big"))
    for name, value in request.headers:
        digest.update(len(name).to_bytes(4, "big"))
        digest.update(name)
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)
    return digest.digest()


def _hkdf_sha256(ikm: bytes, salt: bytes, info: bytes, length: int) -> bytes:
    pseudo_random_key = hmac.new(salt, ikm, hashlib.sha256).digest()
    output = b""
    previous = b""
    for counter in range(1, (length + 31) // 32 + 1):
        previous = hmac.new(pseudo_random_key, previous + info + bytes([counter]), hashlib.sha256).digest()
        output += previous
    return output[:length]


def _parse_transcript_independently(raw: bytes) -> tuple[str, tuple[tuple[str, bytes], ...]]:
    assert raw.startswith(b"REM-TRANSCRIPT\x01")
    cursor = 15
    purpose_length = int.from_bytes(raw[cursor : cursor + 2], "big")
    cursor += 2
    purpose = raw[cursor : cursor + purpose_length].decode("ascii")
    cursor += purpose_length
    fields: list[tuple[str, bytes]] = []
    while cursor < len(raw):
        name_length = int.from_bytes(raw[cursor : cursor + 2], "big")
        cursor += 2
        name = raw[cursor : cursor + name_length].decode("ascii")
        cursor += name_length
        value_length = int.from_bytes(raw[cursor : cursor + 4], "big")
        cursor += 4
        value = raw[cursor : cursor + value_length]
        assert len(value) == value_length
        cursor += value_length
        fields.append((name, value))
    assert cursor == len(raw)
    return purpose, tuple(fields)


def _independent_public_key(jwk_bytes: bytes) -> ec.EllipticCurvePublicKey:
    value = json.loads(jwk_bytes)
    x = base64.urlsafe_b64decode(value["x"] + "=")
    y = base64.urlsafe_b64decode(value["y"] + "=")
    return ec.EllipticCurvePublicNumbers(
        int.from_bytes(x, "big"), int.from_bytes(y, "big"), ec.SECP256R1()
    ).public_key()


def _independent_verify(public_key: ec.EllipticCurvePublicKey, message: bytes, signature: bytes) -> None:
    assert len(signature) == 64
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")
    assert 0 < r < P256_ORDER
    assert 0 < s <= P256_ORDER // 2
    public_key.verify(encode_dss_signature(r, s), message, ec.ECDSA(hashes.SHA256()))


def _signature_members(header: str) -> dict[str, bytes]:
    members: dict[str, bytes] = {}
    for member in header.split(", "):
        label, encoded = member.split("=:", 1)
        assert encoded.endswith(":")
        members[label] = base64.b64decode(encoded[:-1], validate=True)
    return members


def _signature_parameter_members(header: str) -> dict[str, str]:
    members: dict[str, str] = {}
    for member in header.split(", "):
        label, value = member.split("=", 1)
        members[label] = value
    return members


def _covered_components(parameters: str) -> tuple[tuple[str, str | None], ...]:
    inner = parameters[: parameters.index(")") + 1]
    return tuple((name, query_name) for name, query_name in re.findall(r'"([@a-z-]+)"(?:;name="([a-z_]+)")?', inner))


def _input_header_values(inputs: Mapping[str, JsonObject]) -> dict[str, bytes]:
    return {
        name.removeprefix("headers."): _bytes(entry) for name, entry in inputs.items() if name.startswith("headers.")
    }


def _render_signature_base(case: JsonObject, label: str) -> bytes:
    inputs = _entries(case, "inputs")
    intermediates = _entries(case, "intermediates")
    parameters = _text(intermediates, f"signature-params.{label}")
    target = _bytes(inputs["request.target"])
    path, separator, raw_query = target.partition(b"?")
    query = b"?" + raw_query if separator else b"?"
    query_values = {
        name.decode("ascii"): value
        for pair in raw_query.split(b"&")
        if pair
        for name, equals, value in (pair.partition(b"="),)
        if equals
    }
    headers = _input_header_values(inputs)
    values: dict[str, bytes] = {
        "@method": _bytes(inputs["request.method"]),
        "@authority": _bytes(inputs["request.authority"]),
        "@path": path,
        "@query": query,
    }
    lines: list[bytes] = []
    for component, query_name in _covered_components(parameters):
        if component == "@query-param":
            assert query_name is not None
            value = query_values[query_name]
            rendered_name = f'"@query-param";name="{query_name}"'.encode()
        else:
            value = values[component] if component.startswith("@") else headers[component]
            rendered_name = f'"{component}"'.encode()
        lines.append(rendered_name + b": " + value)
    lines.append(b'"@signature-params": ' + parameters.encode("ascii"))
    return b"\n".join(lines)


def _render_request_signature_base(request: SignedRequest, signature_input: str, label: str = "rem") -> bytes:
    """Render a test-generated signature base independently from the verifier."""
    parameters = _signature_parameter_members(signature_input)[label]
    path, separator, raw_query = request.target.partition(b"?")
    query_values = {
        name.decode("ascii"): value
        for pair in raw_query.split(b"&")
        if pair
        for name, equals, value in (pair.partition(b"="),)
        if equals
    }
    headers = {name.decode("ascii"): value for name, value in request.headers}
    values = {
        "@method": request.method,
        "@authority": request.authority,
        "@path": path,
        "@query": b"?" + raw_query if separator else b"?",
    }
    lines: list[bytes] = []
    for component, query_name in _covered_components(parameters):
        if component == "@query-param":
            assert query_name is not None
            value = query_values[query_name]
            rendered_name = f'"@query-param";name="{query_name}"'.encode()
        else:
            value = values[component] if component.startswith("@") else headers[component]
            rendered_name = f'"{component}"'.encode()
        lines.append(rendered_name + b": " + value)
    lines.append(b'"@signature-params": ' + parameters.encode("ascii"))
    return b"\n".join(lines)


def _http_cases() -> Iterator[tuple[str, JsonObject]]:
    for file_name in ("http-signatures.json", "key-rotations.json"):
        for case in _cases(file_name):
            inputs = _entries(case, "inputs")
            outputs = _entries(case, "expected")
            if "request.method" in inputs and "headers.signature-input" in outputs:
                yield file_name, case


HTTP_CASES: Final = tuple(_http_cases())


def _verification_call(case: JsonObject) -> VerificationCall:
    inputs = _entries(case, "inputs")
    outputs = _entries(case, "expected")
    signature_input = _text(outputs, "headers.signature-input")
    signature = _text(outputs, "headers.signature")
    headers = [(name.encode(), value) for name, value in _input_header_values(inputs).items()]
    headers.extend(((b"signature-input", signature_input.encode()), (b"signature", signature.encode())))
    parsed_keys = {
        name.removeprefix("jwk."): parse_p256_public_jwk(_bytes(entry))
        for name, entry in inputs.items()
        if name.startswith("jwk.")
    }
    keys = {
        name: VerificationKey(identity=public_jwk.thumbprint, public_jwk=public_jwk)
        for name, public_jwk in parsed_keys.items()
    }
    route_ids = cast(list[str], case["routes"])
    route_id = route_ids[0]
    if len(route_ids) > 1:
        route_id = next(
            candidate
            for candidate in route_ids
            if b"{" not in route_profile(candidate).path_template
            and route_profile(candidate).path_template == _bytes(inputs["request.target"]).partition(b"?")[0]
        )
    route = route_profile(route_id)
    if route.surface == "control":
        created_match = re.search(r";created=(\d+)", signature_input)
        assert created_match is not None
        received_at = int(created_match.group(1))
    else:
        received_at = 0
    generation_entry = inputs.get("headers.rem-control-key-generation")
    generation = int(_text(inputs, "headers.rem-control-key-generation")) if generation_entry else None
    return VerificationCall(
        request=SignedRequest(
            method=_bytes(inputs["request.method"]),
            authority=_bytes(inputs["request.authority"]),
            target=_bytes(inputs["request.target"]),
            headers=tuple(headers),
            body=_bytes(inputs["body"]) if "body" in inputs else b"",
        ),
        route_id=route_id,
        keys=keys,
        expected_authority=_bytes(inputs["request.authority"]),
        received_at=received_at,
        active_control_generation=generation,
    )


def _verify(call: VerificationCall) -> Any:
    return verify_signed_request(
        call.request,
        route=route_profile(call.route_id),
        keys=call.keys,
        expected_authority=call.expected_authority,
        received_at=call.received_at,
        active_control_generation=call.active_control_generation,
    )


def _resigned_enrollment_call(case_id: str, purpose: bytes, *, body: bytes | None = None) -> VerificationCall:
    """Return a validly signed enrollment request carrying a substituted purpose.

    With ``body``, the vector body is replaced and Content-Digest recomputed before
    signing, so the request stays authentic and only the body content differs.
    """
    case = _case("http-signatures.json", case_id)
    call = _verification_call(case)
    if body is not None:
        assert all(name != b"content-length" for name, _ in call.request.headers)
        digest = render_content_digest(body).encode("ascii")
        call = call._replace(
            request=dataclasses.replace(
                call.request,
                body=body,
                headers=tuple(
                    (name, digest if name == b"content-digest" else value) for name, value in call.request.headers
                ),
            )
        )
    previous_binding = call.keys["rem"]
    previous_purpose = _header(call.request, b"rem-purpose")
    signing_key = ec.generate_private_key(ec.SECP256R1())
    numbers = signing_key.public_key().public_numbers()

    def encoded_coordinate(value: int) -> str:
        return base64.urlsafe_b64encode(value.to_bytes(32, "big")).rstrip(b"=").decode("ascii")

    public_jwk = parse_p256_public_jwk(
        json.dumps(
            {
                "crv": "P-256",
                "kty": "EC",
                "x": encoded_coordinate(numbers.x),
                "y": encoded_coordinate(numbers.y),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    )
    signature_input = _header(call.request, b"signature-input").decode("ascii")
    signature_input = signature_input.replace(previous_binding.public_jwk.thumbprint, public_jwk.thumbprint)
    signature_input = signature_input.replace(
        f'tag="{previous_purpose.decode("ascii")}"',
        f'tag="{purpose.decode("ascii")}"',
    )
    headers = tuple(
        (name, purpose if name == b"rem-purpose" else signature_input.encode() if name == b"signature-input" else value)
        for name, value in call.request.headers
        if name != b"signature"
    )
    unsigned_request = dataclasses.replace(call.request, headers=headers)
    signature_base = _render_request_signature_base(unsigned_request, signature_input)
    der_signature = signing_key.sign(signature_base, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der_signature)
    signature = r.to_bytes(32, "big") + min(s, P256_ORDER - s).to_bytes(32, "big")
    request = dataclasses.replace(
        unsigned_request,
        headers=(*unsigned_request.headers, (b"signature", b"rem=:" + base64.b64encode(signature) + b":")),
    )
    binding = VerificationKey(identity=public_jwk.thumbprint, public_jwk=public_jwk)
    return call._replace(request=request, keys={"rem": binding})


def _replace_header(request: SignedRequest, name: bytes, value: bytes) -> SignedRequest:
    headers = tuple((header_name, value if header_name == name else old) for header_name, old in request.headers)
    return dataclasses.replace(request, headers=headers)


def _alter_parameter_member(header: str, label: str, field: str, operation: str) -> str:
    members = _signature_parameter_members(header)
    member = members[label]
    if field == "components":
        member = member.replace('"@method" "@authority"', '"@authority" "@method"', 1)
    elif field == "component-list":
        close = member.index(")")
        inner = member[:close]
        member = inner[: inner.rfind(" ")] + member[close:]
    else:
        pattern = rf";{re.escape(field)}=(\d+|\"[^\"]*\")"
        match = re.search(pattern, member)
        assert match is not None
        old = match.group(1)
        replacement = f'"{old[1:-1]}x"' if old.startswith('"') else str(int(old) + 1)
        member = member[: match.start(1)] + replacement + member[match.end(1) :]
    members[label] = member
    if operation == "omit" and field == "label":
        del members[label]
    return ", ".join(f"{member_label}={value}" for member_label, value in members.items())


def _alter_signature_member(header: str, label: str, replacement: bytes | None = None) -> str:
    members = _signature_members(header)
    members[label] = replacement if replacement is not None else _flip(members[label])
    return ", ".join(f"{member_label}=:{base64.b64encode(value).decode()}:" for member_label, value in members.items())


def _substitute(case: JsonObject, field: str) -> bytes | None:
    for substitute in cast(list[JsonObject], case["substitutes"]):
        if substitute["field"] == field:
            return _bytes(cast(JsonObject, substitute["replacement"]))
    return None


def _mutate_target(
    call: VerificationCall,
    request: SignedRequest,
    field: str,
    operation: str,
    substitute: bytes | None,
) -> SignedRequest:
    if substitute is not None:
        target = substitute
    elif field.endswith("path-parameter"):
        path, separator, query = request.target.partition(b"?")
        template = route_profile(call.route_id).path_template
        actual_parts = path.split(b"/")
        template_parts = template.split(b"/")
        index = next(
            index for index, part in enumerate(template_parts) if part.startswith(b"{") and part.endswith(b"}")
        )
        replacement = b"B" if actual_parts[index][:1] != b"B" else b"A"
        actual_parts[index] = replacement + actual_parts[index][1:]
        target = b"/".join(actual_parts) + (separator + query if separator else b"")
    elif field.endswith("query-member"):
        path, query = request.target.split(b"?", 1)
        pairs = query.split(b"&")
        assert operation == "duplicate" and len(pairs) == 1
        name, equals, value = pairs[0].partition(b"=")
        duplicate_value = value if value.isdigit() else b"AA"
        target = path + b"?" + query + b"&" + name + equals + duplicate_value
    elif field.endswith("query-order"):
        path, query = request.target.split(b"?", 1)
        pairs = query.split(b"&")
        assert len(pairs) > 1
        target = path + b"?" + b"&".join(reversed(pairs))
    elif field.endswith("query"):
        path, query = request.target.split(b"?", 1)
        pairs = query.split(b"&")
        name, equals, value = pairs[0].partition(b"=")
        if value.isdigit():
            changed_value = str(int(value) + 1).encode()
        else:
            changed_value = (b"B" if value[:1] != b"B" else b"A") + value[1:]
        pairs[0] = name + equals + changed_value
        target = path + b"?" + b"&".join(pairs)
    else:
        path, separator, query = request.target.partition(b"?")
        target = path + b"x" + (separator + query if separator else b"")
    return dataclasses.replace(request, target=target)


def _mutate_signature(request: SignedRequest, field: str, substitute: bytes | None) -> SignedRequest:
    label = "rem-proposed" if "rem-proposed" in field else "rem"
    header = next(value.decode() for name, value in request.headers if name == b"signature")
    return _replace_header(
        request,
        b"signature",
        _alter_signature_member(header, label, substitute).encode(),
    )


def _mutate_signature_input(
    request: SignedRequest,
    field: str,
    operation: str,
    mutation: JsonObject,
) -> SignedRequest:
    header = next(value.decode() for name, value in request.headers if name == b"signature-input")
    members = _signature_parameter_members(header)
    if field == "signature-input.labels":
        header = ", ".join(f"{key}={value}" for key, value in reversed(tuple(members.items())))
    elif field.endswith(".label") and operation == "duplicate":
        header = header.replace("rem-proposed=", "rem=", 1)
    elif field.endswith("rem-proposed") and operation == "omit":
        header = ", ".join(part for part in header.split(", ") if not part.startswith("rem-proposed="))
    else:
        raise AssertionError(f"unhandled signature-input mutation {mutation!r}")
    return _replace_header(request, b"signature-input", header.encode())


def _mutate_signature_parameters(
    request: SignedRequest,
    field: str,
    operation: str,
    substitute: bytes | None,
) -> SignedRequest:
    _, _, remainder = field.partition("signature-params.")
    label, _, parameter = remainder.partition(".")
    header = next(value.decode() for name, value in request.headers if name == b"signature-input")
    if substitute is not None and parameter:
        members = _signature_parameter_members(header)
        pattern = rf";{re.escape(parameter)}=(\d+|\"[^\"]*\")"
        rendered = substitute.decode()
        rendered = rendered if rendered.isdecimal() else f'"{rendered}"'
        members[label] = re.sub(pattern, f";{parameter}={rendered}", members[label], count=1)
        header = ", ".join(f"{key}={value}" for key, value in members.items())
    else:
        header = _alter_parameter_member(header, label, parameter, operation)
    return _replace_header(request, b"signature-input", header.encode())


def _mutate_profile_header(request: SignedRequest, field: str, substitute: bytes | None) -> SignedRequest:
    logical_field = field.removeprefix("headers.").split(".", 1)[0]
    name = logical_field.encode()
    old = next((value for header_name, value in request.headers if header_name == name), b"")
    if name in {b"rem-control-key-generation", b"rem-request-expires"} and old.isdigit():
        value = str(int(old) + 1).encode()
    else:
        value = substitute if substitute is not None else (_flip(old) if old else b"unexpected")
    request = _replace_header(request, name, value)
    if not any(header_name == name for header_name, _ in request.headers):
        request = dataclasses.replace(request, headers=(*request.headers, (name, value)))
    return request


def _mutate_http(case: JsonObject, mutation: JsonObject) -> VerificationCall:
    call = _verification_call(case)
    request = call.request
    field = cast(str, mutation["field"])
    operation = cast(str, mutation["operation"])
    substitute = _substitute(case, field)
    received_at = call.received_at
    generation = call.active_control_generation

    if field == "request.method":
        request = dataclasses.replace(request, method=b"GET" if request.method == b"POST" else b"POST")
    elif field == "request.authority":
        request = dataclasses.replace(request, authority=b"other.example.com:8444")
    elif field.startswith("request.target"):
        request = _mutate_target(call, request, field, operation, substitute)
    elif field == "body" or field.startswith("body."):
        request = dataclasses.replace(request, body=_flip(request.body))
    elif field == "headers.signature":
        signature = next(value for name, value in request.headers if name == b"signature")
        request = dataclasses.replace(request, headers=(*request.headers, (b"signature", signature)))
    elif field.startswith("headers.signature.") or field.startswith("signature.rem"):
        request = _mutate_signature(request, field, substitute)
    elif field.startswith("signature-input."):
        request = _mutate_signature_input(request, field, operation, mutation)
    elif field.startswith("signature-params."):
        request = _mutate_signature_parameters(request, field, operation, substitute)
    elif field.startswith("headers."):
        request = _mutate_profile_header(request, field, substitute)
    elif field == "request.replay.after-expiry":
        signature_input = next(value.decode() for name, value in request.headers if name == b"signature-input")
        match = re.search(r";expires=(\d+)", signature_input)
        assert match is not None
        received_at = int(match.group(1)) + 1
    else:
        raise AssertionError(f"unhandled HTTP mutation {mutation!r}")

    return call._replace(request=request, received_at=received_at, active_control_generation=generation)


@pytest.mark.parametrize(
    ("file_name", "case"), HTTP_CASES, ids=lambda value: value if isinstance(value, str) else value["id"]
)
def test_every_fixed_http_signature_recomputes_and_verifies(file_name: str, case: JsonObject) -> None:
    del file_name
    inputs = _entries(case, "inputs")
    intermediates = _entries(case, "intermediates")
    outputs = _entries(case, "expected")
    signature_header = _text(outputs, "headers.signature")
    signatures = _signature_members(signature_header)
    parameter_members = _signature_parameter_members(_text(outputs, "headers.signature-input"))

    for label, signature in signatures.items():
        expected_base = _bytes(intermediates[f"signature-base.{label}"])
        independent_base = _render_signature_base(case, label)
        assert independent_base == expected_base
        digest_entry = intermediates.get(f"signature-base.{label}.sha256")
        if digest_entry is not None:
            assert hashlib.sha256(independent_base).digest() == _bytes(digest_entry)
        assert parameter_members[label] == _text(intermediates, f"signature-params.{label}")
        assert signature == _bytes(intermediates[f"signature.{label}.p1363"])
        key = _independent_public_key(_bytes(inputs[f"jwk.{label}"]))
        _independent_verify(key, independent_base, signature)
        verify_p256_p1363(parse_p256_public_jwk(_bytes(inputs[f"jwk.{label}"])).public_key, independent_base, signature)

    first = _verify(_verification_call(case))
    second = _verify(_verification_call(case))
    call = _verification_call(case)
    path, separator, query = call.request.target.partition(b"?")
    assert first == second, "verification is pure; replay policy belongs to the route state layer"
    assert hash(first) == hash(second)
    assert first.request == call.request
    assert first.route_id == call.route_id
    assert first.method == call.request.method
    assert first.authority == call.request.authority
    assert first.target == call.request.target
    assert first.path == path
    assert first.query == (b"?" + query if separator else b"?")
    assert first.headers == tuple(call.request.headers)
    assert first.body == call.request.body
    assert first.bound_components == tuple(
        (
            label,
            tuple(
                tuple(line.split(b": ", 1))
                for line in _bytes(intermediates[f"signature-base.{label}"]).rsplit(b"\n", 1)[0].split(b"\n")
            ),
        )
        for label in signatures
    )
    assert first.signature_inputs == tuple((label, parameter_members[label].encode()) for label in signatures)
    assert first.signatures == tuple(signatures.items())
    assert first.signature_bases == tuple(
        (label, _bytes(intermediates[f"signature-base.{label}"])) for label in signatures
    )
    assert first.keyids == tuple((label, _text(inputs, f"keyid.{label}")) for label in signatures)
    assert first.request_hash == _independent_request_hash(call.request)
    assert first.verified_identity == call.keys["rem"].identity
    with pytest.raises((dataclasses.FrozenInstanceError, AttributeError, TypeError)):
        first.path = b"/changed"


HTTP_MUTATIONS: Final = tuple(
    (case, mutation)
    for _, case in HTTP_CASES
    for mutation in cast(list[JsonObject], case["mutations"])
    if not cast(str, mutation["field"]).startswith("transcript.")
)


@pytest.mark.parametrize(
    ("case", "mutation"),
    HTTP_MUTATIONS,
    ids=[cast(str, mutation["id"]) for case, mutation in HTTP_MUTATIONS],
)
def test_every_http_one_field_mutation_is_rejected_with_its_profile_code(
    case: JsonObject,
    mutation: JsonObject,
) -> None:
    call = _mutate_http(case, mutation)
    _raise_code(cast(str, mutation["expected_code"]), lambda: _verify(call))


@pytest.mark.parametrize(
    "case_id",
    [
        "http.route.control.roster.snapshot",
        "http.route.control.roster.snapshot.continuation",
    ],
)
def test_single_member_snapshot_duplicate_query_mutation_executes(case_id: str) -> None:
    case = _case("http-signatures.json", case_id)
    mutation = next(
        item for item in cast(list[JsonObject], case["mutations"]) if item["field"] == "request.target.query-member"
    )

    call = _mutate_http(case, mutation)
    pairs = call.request.target.split(b"?", 1)[1].split(b"&")
    names = [pair.partition(b"=")[0] for pair in pairs]

    assert mutation["operation"] == "duplicate"
    assert mutation["expected_code"] == "invalid_request"
    assert len(call.request.target) <= 256
    assert len(pairs) == 2 and names[0] == names[1]
    _raise_code("invalid_request", lambda: _verify(call))


@pytest.mark.parametrize(
    ("file_name", "case"), HTTP_CASES, ids=lambda value: value if isinstance(value, str) else value["id"]
)
def test_every_http_high_s_twin_is_rejected(file_name: str, case: JsonObject) -> None:
    del file_name
    intermediates = _entries(case, "intermediates")
    for name, entry in intermediates.items():
        if not name.startswith("signature.") or not name.endswith(".high-s"):
            continue
        label = name.removeprefix("signature.").removesuffix(".high-s")
        call = _verification_call(case)
        signature_header = next(value.decode() for header, value in call.request.headers if header == b"signature")
        request = _replace_header(
            call.request,
            b"signature",
            _alter_signature_member(signature_header, label, _bytes(entry)).encode(),
        )
        _raise_code(
            "authentication_required",
            partial(_verify, call._replace(request=request)),
        )


def test_common_transcript_vector_and_every_declared_mutation() -> None:
    case = _case("common.json", "common.transcript-encoding")
    inputs = _entries(case, "inputs")
    intermediates = _entries(case, "intermediates")
    expected = _entries(case, "expected")
    fields = (("role", _bytes(inputs["field.role"])), ("nonce", _bytes(inputs["field.nonce"])))
    transcript = encode_transcript(_text(inputs, "purpose"), fields, max_length=4096)

    assert transcript == _bytes(expected["transcript"])
    assert (
        decode_transcript(
            transcript,
            purpose=_text(inputs, "purpose"),
            fields=("role", "nonce"),
            max_length=4096,
        )
        == fields
    )
    structurally_malformed = (
        _flip(transcript),
        transcript[:15] + _flip(transcript[15:]),
        transcript[: -len(_bytes(intermediates["transcript.encoded.nonce"]))]
        + _bytes(intermediates["transcript.encoded.role"])
        + _bytes(intermediates["transcript.encoded.nonce"]),
        encode_transcript(_text(inputs, "purpose"), fields[:1], max_length=4096),
        encode_transcript(_text(inputs, "purpose"), tuple(reversed(fields)), max_length=4096),
    )
    mutations = cast(list[JsonObject], case["mutations"])
    for mutation, raw in zip(mutations[:5], structurally_malformed, strict=True):
        _raise_code(
            cast(str, mutation["expected_code"]),
            partial(
                decode_transcript,
                raw,
                purpose=_text(inputs, "purpose"),
                fields=("role", "nonce"),
                max_length=4096,
            ),
        )

    altered_value = encode_transcript(
        _text(inputs, "purpose"),
        (fields[0], ("nonce", _flip(fields[1][1]))),
        max_length=4096,
    )
    assert (
        decode_transcript(
            altered_value,
            purpose=_text(inputs, "purpose"),
            fields=("role", "nonce"),
            max_length=4096,
        )
        != fields
    )
    assert hashlib.sha256(altered_value).digest() != hashlib.sha256(transcript).digest()


def test_common_jwk_vector_recomputes_thumbprint_and_rejects_all_parse_mutations() -> None:
    case = _case("common.json", "common.jwk-thumbprint")
    inputs = _entries(case, "inputs")
    intermediates = _entries(case, "intermediates")
    expected = _entries(case, "expected")
    canonical = _bytes(intermediates["jwk.canonical"])
    parsed = parse_p256_public_jwk(canonical)

    assert parsed.canonical_bytes == canonical
    assert parsed.x == _bytes(inputs["jwk.x"])
    assert parsed.y == _bytes(inputs["jwk.y"])
    assert parsed.thumbprint_bytes == hashlib.sha256(canonical).digest() == _bytes(intermediates["thumbprint.sha256"])
    assert parsed.thumbprint == _text(expected, "reporter_id")

    substitutes = {cast(str, value["field"]): cast(JsonObject, value["replacement"]) for value in case["substitutes"]}
    invalid_jwks = (
        canonical.replace(_text(inputs, "jwk.x").encode(), _text(substitutes, "jwk.x").encode()),
        canonical.replace(_text(inputs, "jwk.y").encode(), _flip(_text(inputs, "jwk.y").encode())),
        _bytes(substitutes["jwk.canonical"]),
        b'{"kty":"EC","crv":"P-256","x":"'
        + _text(inputs, "jwk.x").encode()
        + b'","y":"'
        + _text(inputs, "jwk.y").encode()
        + b'"}',
    )
    for jwk in invalid_jwks:
        _raise_code("invalid_request", partial(parse_p256_public_jwk, jwk))
    assert _flip(parsed.thumbprint_bytes) != parsed.thumbprint_bytes


def test_common_content_digest_vector_recomputes_and_rejects_every_mutation() -> None:
    case = _case("common.json", "common.content-digest")
    inputs = _entries(case, "inputs")
    intermediates = _entries(case, "intermediates")
    expected = _entries(case, "expected")
    body = _bytes(inputs["body"])
    header = _text(expected, "headers.content-digest")

    assert hashlib.sha256(body).digest() == _bytes(intermediates["body.sha256"])
    assert render_content_digest(body) == header
    assert parse_content_digest(header) == hashlib.sha256(body).digest()
    verify_content_digest(header, body)

    sha512 = _bytes(cast(JsonObject, cast(list[JsonObject], case["substitutes"])[0]["replacement"])).decode()
    reordered_body = _bytes(cast(JsonObject, cast(list[JsonObject], case["substitutes"])[1]["replacement"]))
    calls = (
        lambda: parse_content_digest(sha512),
        lambda: verify_content_digest(header, reordered_body),
        lambda: verify_content_digest(header, _flip(body)),
        lambda: parse_content_digest(header + ", " + header),
    )
    mutations = cast(list[JsonObject], case["mutations"])
    assert len(calls) == len(mutations)
    for mutation, call in zip(mutations, calls, strict=True):
        _raise_code(cast(str, mutation["expected_code"]), call)


def _signature_fixture_cases() -> Iterator[tuple[str, str, str, str, str]]:
    yield ("managed-enrollment.json", "managed.proof", "proposed_jwk", "transcript", "proposed_signature")
    yield ("key-rotations.json", "rotation.reporter", "current_jwk", "transcript", "current_signature")
    yield ("key-rotations.json", "rotation.reporter", "proposed_jwk", "transcript", "proposed_signature")


@pytest.mark.parametrize(
    ("file_name", "case_id", "key_name", "message_name", "signature_name"),
    tuple(_signature_fixture_cases()),
)
def test_body_signature_vectors_accept_low_s_and_reject_high_s(
    file_name: str,
    case_id: str,
    key_name: str,
    message_name: str,
    signature_name: str,
) -> None:
    case = _case(file_name, case_id)
    inputs = _entries(case, "inputs")
    intermediates = _entries(case, "intermediates")
    expected = _entries(case, "expected")
    substitutes = {cast(str, value["field"]): cast(JsonObject, value["replacement"]) for value in case["substitutes"]}
    public_key = parse_p256_public_jwk(_bytes(inputs[key_name])).public_key
    message = _bytes(intermediates[message_name])
    signature = _bytes(expected[signature_name])
    high_s = _bytes(substitutes[f"{signature_name}.high-s"])

    verify_p256_p1363(public_key, message, signature)
    _independent_verify(_independent_public_key(_bytes(inputs[key_name])), message, signature)
    _raise_code(
        "authentication_required",
        lambda: verify_p256_p1363(public_key, message, _flip(signature)),
    )
    _raise_code("authentication_required", lambda: verify_p256_p1363(public_key, message, high_s))


def _decode_unpadded_base64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def test_regenerated_interactive_enrollment_http_values_verify_from_public_values() -> None:
    create_case = _case("http-signatures.json", "http.route.reporter.enrollment.create")
    reveal_case = _case("http-signatures.json", "http.route.reporter.enrollment.reveal")
    confirm_case = _case("http-signatures.json", "http.route.reporter.enrollment.confirm")
    create_inputs = _entries(create_case, "inputs")
    create_outputs = _entries(create_case, "expected")
    create_body = cast(JsonObject, json.loads(_text(create_inputs, "body")))
    create_response = cast(JsonObject, json.loads(_text(create_outputs, "response.body")))
    reveal_inputs = _entries(reveal_case, "inputs")
    reveal_intermediates = _entries(reveal_case, "intermediates")
    reveal_body = cast(JsonObject, json.loads(_text(reveal_inputs, "body")))
    reveal_response = cast(JsonObject, json.loads(_text(_entries(reveal_case, "expected"), "response.body")))
    reporter_nonce = _decode_unpadded_base64url(cast(str, reveal_body["reporter_nonce"]))
    core_nonce = _decode_unpadded_base64url(cast(str, reveal_response["core_nonce"]))
    reporter_commitment = hashlib.sha256(
        encode_transcript(
            "rem-interactive-commit-v1",
            (("role", b"reporter"), ("nonce", reporter_nonce)),
            max_length=4096,
        )
    ).digest()
    core_commitment = hashlib.sha256(
        encode_transcript(
            "rem-interactive-commit-v1",
            (("role", b"core"), ("nonce", core_nonce)),
            max_length=4096,
        )
    ).digest()
    assert reporter_commitment == _decode_unpadded_base64url(cast(str, create_body["reporter_commitment"]))
    assert core_commitment == _decode_unpadded_base64url(cast(str, create_response["core_commitment"]))

    interactive_jwk_bytes = json.dumps(create_body["proposed_jwk"], sort_keys=True, separators=(",", ":")).encode()
    interactive_jwk = parse_p256_public_jwk(interactive_jwk_bytes)
    interactive_fields = (
        ("schema_version", cast(int, create_body["schema_version"]).to_bytes(1, "big")),
        ("purpose", b"rem-interactive-enroll-v1"),
        ("instance_id", cast(str, create_response["instance_id"]).encode()),
        ("server_identity", cast(str, create_body["server_identity"]).encode()),
        ("trust_mode", cast(str, create_response["trust_mode"]).encode()),
        (
            "trust_anchor_spki_sha256",
            _decode_unpadded_base64url(cast(str, create_response["trust_anchor_spki_sha256"])),
        ),
        ("proposed_reporter_jwk", interactive_jwk.canonical_bytes),
        ("proposed_reporter_thumbprint", interactive_jwk.thumbprint_bytes),
        ("reporter_commitment", reporter_commitment),
        ("core_commitment", core_commitment),
        ("reporter_nonce", reporter_nonce),
        ("core_nonce", core_nonce),
        ("expires_at", cast(int, create_response["expires_at"]).to_bytes(8, "big")),
    )
    interactive_transcript = encode_transcript("rem-interactive-enroll-v1", interactive_fields, max_length=4096)
    assert interactive_transcript == _bytes(reveal_intermediates["fixture.interactive.transcript"])
    assert len(interactive_transcript) == int(_text(reveal_intermediates, "fixture.interactive.transcript.length"))
    assert cast(str, create_response["enrollment_id"]).encode() in _bytes(reveal_inputs["request.target"])
    interactive_digest = hashlib.sha256(interactive_transcript).digest()
    assert interactive_digest == _bytes(reveal_intermediates["fixture.interactive.transcript_digest"])
    assert interactive_digest == _decode_unpadded_base64url(cast(str, reveal_response["transcript_digest"]))
    confirm_body = cast(JsonObject, json.loads(_text(_entries(confirm_case, "inputs"), "body")))
    assert interactive_digest == _decode_unpadded_base64url(cast(str, confirm_body["transcript_digest"]))


def test_regenerated_managed_enrollment_http_values_verify_from_public_values() -> None:
    managed_case = _case("http-signatures.json", "http.route.reporter.enrollment.create.managed")
    managed_inputs = _entries(managed_case, "inputs")
    managed_intermediates = _entries(managed_case, "intermediates")
    managed_body = cast(JsonObject, json.loads(_text(managed_inputs, "body")))
    managed_jwk_bytes = json.dumps(managed_body["proposed_jwk"], sort_keys=True, separators=(",", ":")).encode()
    assert managed_jwk_bytes == _bytes(managed_inputs["jwk.rem"])
    managed_jwk = parse_p256_public_jwk(managed_jwk_bytes)
    assert managed_body["proposed_thumbprint"] == managed_jwk.thumbprint
    opaque_challenge = _decode_unpadded_base64url(cast(str, managed_body["opaque_challenge"]))
    assert len(opaque_challenge) == 73 and opaque_challenge[0] == 1
    assert opaque_challenge == _bytes(managed_intermediates["fixture.managed.opaque_challenge"])
    core_nonce = opaque_challenge[1:33]
    expires_at = int.from_bytes(opaque_challenge[33:41], "big")
    authority = _bytes(managed_inputs["request.authority"]).decode("ascii")
    host, port_text = authority.rsplit(":", 1)
    managed_fields = (
        ("schema_version", cast(int, managed_body["schema_version"]).to_bytes(1, "big")),
        ("purpose", b"rem-managed-enrollment-v1"),
        ("token_id", _decode_unpadded_base64url(cast(str, managed_body["token_id"]))),
        ("opaque_challenge", opaque_challenge),
        ("core_nonce", core_nonce),
        ("reporter_nonce", _decode_unpadded_base64url(cast(str, managed_body["reporter_nonce"]))),
        ("scheme", b"https"),
        ("host", host.encode("ascii")),
        ("port", int(port_text).to_bytes(2, "big")),
        ("trust_mode", _bytes(managed_inputs["fixture.managed.trust_mode"])),
        ("ca_spki_sha256", _bytes(managed_inputs["fixture.managed.ca_spki_sha256"])),
        ("proposed_jwk", managed_jwk.canonical_bytes),
        ("proposed_thumbprint", managed_jwk.thumbprint_bytes),
        ("expires_at", expires_at.to_bytes(8, "big")),
    )
    managed_transcript = encode_transcript("rem-managed-enrollment-v1", managed_fields, max_length=4096)
    assert managed_transcript == _bytes(managed_intermediates["fixture.managed.transcript"])
    assert len(managed_transcript) == int(_text(managed_intermediates, "fixture.managed.transcript.length"))
    assert core_nonce == _bytes(managed_intermediates["fixture.managed.core_nonce"])
    assert expires_at == int(_text(managed_intermediates, "fixture.managed.expires_at"))
    token_verifier = hashlib.sha256(_bytes(managed_inputs["fixture.managed.token"])).digest()
    assert token_verifier == _bytes(managed_intermediates["fixture.managed.token_verifier"])
    proof_key = _hkdf_sha256(
        token_verifier,
        core_nonce,
        _bytes(managed_intermediates["fixture.managed.proof_key.info"]),
        32,
    )
    assert proof_key == _bytes(managed_intermediates["fixture.managed.proof_key"])
    managed_signature = _decode_unpadded_base64url(cast(str, managed_body["proposed_signature"]))
    managed_proof = _decode_unpadded_base64url(cast(str, managed_body["proof"]))
    assert hmac.new(proof_key, managed_transcript, hashlib.sha256).digest() == managed_proof
    assert managed_proof == _bytes(managed_intermediates["fixture.managed.proof"])
    assert managed_signature == _bytes(managed_intermediates["fixture.managed.proposed_signature"])
    verify_p256_p1363(managed_jwk.public_key, managed_transcript, managed_signature)
    _independent_verify(_independent_public_key(managed_jwk_bytes), managed_transcript, managed_signature)


def test_regenerated_reporter_key_rotation_http_values_verify_from_public_values() -> None:
    challenge_case = _case("http-signatures.json", "http.route.reporter.key-rotation-challenge.create")
    complete_case = _case("http-signatures.json", "http.route.reporter.key-rotation.complete")
    challenge_inputs = _entries(challenge_case, "inputs")
    challenge_intermediates = _entries(challenge_case, "intermediates")
    challenge_outputs = _entries(challenge_case, "expected")
    complete_inputs = _entries(complete_case, "inputs")
    complete_intermediates = _entries(complete_case, "intermediates")
    challenge_body = cast(JsonObject, json.loads(_text(challenge_inputs, "body")))
    challenge_response = cast(JsonObject, json.loads(_text(challenge_outputs, "response.body")))
    complete_body = cast(JsonObject, json.loads(_text(complete_inputs, "body")))
    current_jwk = parse_p256_public_jwk(_bytes(challenge_inputs["jwk.rem"]))
    proposed_jwk = parse_p256_public_jwk(_bytes(complete_inputs["fixture.rotation.proposed_jwk"]))
    proposed_jwk_from_request = json.dumps(
        challenge_body["proposed_jwk"], sort_keys=True, separators=(",", ":")
    ).encode()
    assert proposed_jwk.canonical_bytes == proposed_jwk_from_request
    assert challenge_body["proposed_thumbprint"] == proposed_jwk.thumbprint
    assert challenge_response["reporter_id"] == current_jwk.thumbprint
    assert cast(str, challenge_response["reporter_id"]).encode() in _bytes(challenge_inputs["request.target"])
    assert challenge_body["current_generation"] == challenge_response["current_generation"]
    assert challenge_body["proposed_thumbprint"] == challenge_response["proposed_thumbprint"]
    assert complete_body["rotation_id"] == challenge_response["rotation_id"]
    assert complete_body["rotation_digest"] == challenge_response["rotation_digest"]
    rotation_fields = (
        ("schema_version", cast(int, challenge_body["schema_version"]).to_bytes(1, "big")),
        ("purpose", cast(str, challenge_body["purpose"]).encode()),
        ("reporter_id", current_jwk.thumbprint_bytes),
        ("current_thumbprint", current_jwk.thumbprint_bytes),
        ("current_generation", cast(int, challenge_body["current_generation"]).to_bytes(8, "big")),
        ("proposed_jwk", proposed_jwk.canonical_bytes),
        ("proposed_thumbprint", proposed_jwk.thumbprint_bytes),
        ("next_generation", cast(int, challenge_response["next_generation"]).to_bytes(8, "big")),
        ("operation_id", _decode_unpadded_base64url(cast(str, challenge_body["operation_id"]))),
        ("rotation_id", _decode_unpadded_base64url(cast(str, challenge_response["rotation_id"]))),
        ("core_nonce", _decode_unpadded_base64url(cast(str, challenge_response["core_nonce"]))),
        ("expires_at", cast(int, challenge_response["expires_at"]).to_bytes(8, "big")),
    )
    rotation_transcript = encode_transcript("rem-reporter-key-rotation-v1", rotation_fields, max_length=4096)
    assert rotation_transcript == _bytes(challenge_intermediates["fixture.rotation.transcript"])
    assert rotation_transcript == _bytes(complete_intermediates["fixture.rotation.transcript"])
    assert len(rotation_transcript) == int(_text(challenge_intermediates, "fixture.rotation.transcript.length"))
    rotation_digest = hashlib.sha256(rotation_transcript).digest()
    assert rotation_digest == _bytes(challenge_intermediates["fixture.rotation.rotation_digest"])
    assert rotation_digest == _decode_unpadded_base64url(cast(str, challenge_response["rotation_digest"]))
    assert rotation_digest == _decode_unpadded_base64url(cast(str, complete_body["rotation_digest"]))

    current_signature = _decode_unpadded_base64url(cast(str, complete_body["current_signature"]))
    proposed_signature = _decode_unpadded_base64url(cast(str, complete_body["proposed_signature"]))
    verify_p256_p1363(current_jwk.public_key, rotation_transcript, current_signature)
    verify_p256_p1363(proposed_jwk.public_key, rotation_transcript, proposed_signature)
    _independent_verify(_independent_public_key(current_jwk.canonical_bytes), rotation_transcript, current_signature)
    _independent_verify(_independent_public_key(proposed_jwk.canonical_bytes), rotation_transcript, proposed_signature)


def test_managed_proof_recomputes_transcript_thumbprint_hkdf_hmac_and_signature() -> None:
    case = _case("managed-enrollment.json", "managed.proof")
    inputs = _entries(case, "inputs")
    intermediates = _entries(case, "intermediates")
    expected = _entries(case, "expected")
    transcript = _bytes(intermediates["transcript"])
    purpose, fields = _parse_transcript_independently(transcript)

    assert purpose == "rem-managed-enrollment-v1"
    assert encode_transcript(purpose, fields, max_length=4096) == transcript
    assert (
        decode_transcript(transcript, purpose=purpose, fields=tuple(name for name, _ in fields), max_length=4096)
        == fields
    )
    token_verifier = hashlib.sha256(_bytes(inputs["token"])).digest()
    assert token_verifier == _bytes(intermediates["token_verifier"])
    proof_key = _hkdf_sha256(
        token_verifier,
        dict(fields)["core_nonce"],
        _bytes(intermediates["proof_key.info"]),
        32,
    )
    assert proof_key == _bytes(intermediates["proof_key"])
    assert hmac.new(proof_key, transcript, hashlib.sha256).digest() == _bytes(expected["proof"])
    parsed_jwk = parse_p256_public_jwk(_bytes(inputs["proposed_jwk"]))
    assert parsed_jwk.thumbprint_bytes == dict(fields)["proposed_thumbprint"]
    verify_p256_p1363(parsed_jwk.public_key, transcript, _bytes(expected["proposed_signature"]))

    evaluated: set[str] = set()
    for mutation in cast(list[JsonObject], case["mutations"]):
        mutation_id = cast(str, mutation["id"])
        field = cast(str, mutation["field"])
        if field.startswith("transcript."):
            mutated_fields = list(fields)
            if field == "transcript.field-order":
                mutated_fields[0], mutated_fields[1] = mutated_fields[1], mutated_fields[0]
            else:
                name = field.removeprefix("transcript.")
                index = next(index for index, (candidate, _) in enumerate(mutated_fields) if candidate == name)
                mutated_fields[index] = (name, _flip(mutated_fields[index][1]))
            mutated = encode_transcript(purpose, tuple(mutated_fields), max_length=4096)
            _raise_code(
                "authentication_required",
                partial(
                    verify_p256_p1363,
                    parsed_jwk.public_key,
                    mutated,
                    _bytes(expected["proposed_signature"]),
                ),
            )
            assert not hmac.compare_digest(
                hmac.new(proof_key, mutated, hashlib.sha256).digest(), _bytes(expected["proof"])
            )
            evaluated.add(mutation_id)
        elif field in {"proof", "proposed_signature", "proposed_signature.high-s"}:
            if field == "proof":
                assert not hmac.compare_digest(_flip(_bytes(expected["proof"])), _bytes(expected["proof"]))
            else:
                substitute = next(
                    (
                        _bytes(cast(JsonObject, value["replacement"]))
                        for value in case["substitutes"]
                        if value["field"] == field
                    ),
                    _flip(_bytes(expected["proposed_signature"])),
                )
                _raise_code(
                    "authentication_required",
                    partial(verify_p256_p1363, parsed_jwk.public_key, transcript, substitute),
                )
            evaluated.add(mutation_id)
        elif field in {"token", "token_verifier", "proof_key.info"}:
            altered_token_verifier = (
                _flip(token_verifier) if field != "token" else hashlib.sha256(_flip(_bytes(inputs["token"]))).digest()
            )
            altered_info = (
                _flip(_bytes(intermediates["proof_key.info"]))
                if field == "proof_key.info"
                else _bytes(intermediates["proof_key.info"])
            )
            altered_key = _hkdf_sha256(altered_token_verifier, dict(fields)["core_nonce"], altered_info, 32)
            assert not hmac.compare_digest(
                hmac.new(altered_key, transcript, hashlib.sha256).digest(), _bytes(expected["proof"])
            )
            evaluated.add(mutation_id)
    expected_primitive = {cast(str, item["id"]) for item in case["mutations"] if item["field"] != "request"}
    assert evaluated == expected_primitive


def test_signature_bearing_rotation_transcript_mutations_invalidate_each_body_signature() -> None:
    case = _case("key-rotations.json", "rotation.reporter")
    inputs = _entries(case, "inputs")
    intermediates = _entries(case, "intermediates")
    expected = _entries(case, "expected")
    transcript = _bytes(intermediates["transcript"])
    purpose, fields = _parse_transcript_independently(transcript)
    signatures = (
        (parse_p256_public_jwk(_bytes(inputs["current_jwk"])).public_key, _bytes(expected["current_signature"])),
        (parse_p256_public_jwk(_bytes(inputs["proposed_jwk"])).public_key, _bytes(expected["proposed_signature"])),
    )

    assert encode_transcript(purpose, fields, max_length=4096) == transcript
    for mutation in cast(list[JsonObject], case["mutations"]):
        field = cast(str, mutation["field"])
        if not field.startswith("transcript."):
            continue
        changed = list(fields)
        if field == "transcript.field-order":
            changed[0], changed[1] = changed[1], changed[0]
        else:
            name = field.removeprefix("transcript.")
            index = next(index for index, (candidate, _) in enumerate(changed) if candidate == name)
            changed[index] = (name, _flip(changed[index][1]))
        mutated = encode_transcript(purpose, tuple(changed), max_length=4096)
        for public_key, signature in signatures:
            _raise_code(
                "authentication_required",
                partial(verify_p256_p1363, public_key, mutated, signature),
            )


def test_control_spki_rotation_accepts_both_low_s_signatures_and_rejects_both_twins() -> None:
    case = _case("key-rotations.json", "rotation.control-spki")
    inputs = _entries(case, "inputs")
    intermediates = _entries(case, "intermediates")
    expected = _entries(case, "expected")
    transcript = _bytes(intermediates["transcript"])
    substitutes = {cast(str, value["field"]): cast(JsonObject, value["replacement"]) for value in case["substitutes"]}
    public_keys: list[ec.EllipticCurvePublicKey] = []
    signatures: list[bytes] = []
    for prefix in ("old", "new"):
        public_key = serialization.load_der_public_key(_bytes(inputs[f"{prefix}_spki_der"]))
        assert isinstance(public_key, ec.EllipticCurvePublicKey)
        signature = _bytes(expected[f"{prefix}_spki_signature"])
        high_s = _bytes(substitutes[f"{prefix}_spki_signature.high-s"])
        verify_p256_p1363(public_key, transcript, signature)
        _raise_code(
            "authentication_required",
            partial(verify_p256_p1363, public_key, transcript, _flip(signature)),
        )
        _raise_code(
            "authentication_required",
            partial(verify_p256_p1363, public_key, transcript, high_s),
        )
        public_keys.append(public_key)
        signatures.append(signature)

    purpose, fields = _parse_transcript_independently(transcript)
    for mutation in cast(list[JsonObject], case["mutations"]):
        field = cast(str, mutation["field"])
        if not field.startswith("transcript."):
            continue
        changed = list(fields)
        if field == "transcript.field-order":
            changed[0], changed[1] = changed[1], changed[0]
        else:
            name = field.removeprefix("transcript.")
            index = next(index for index, (candidate, _) in enumerate(changed) if candidate == name)
            changed[index] = (name, _flip(changed[index][1]))
        mutated = encode_transcript(purpose, tuple(changed), max_length=4096)
        for public_key, signature in zip(public_keys, signatures, strict=True):
            _raise_code(
                "authentication_required",
                partial(verify_p256_p1363, public_key, mutated, signature),
            )


def test_fixed_canonical_bodies_are_accepted_and_reordering_is_not_an_alias() -> None:
    for _, case in HTTP_CASES:
        inputs = _entries(case, "inputs")
        if "body" not in inputs:
            continue
        body = _bytes(inputs["body"])
        parsed = parse_canonical_json(body, max_depth=8)
        assert parsed is not None
        if body.startswith(b"{") and b"," in body:
            first_comma = body.index(b",")
            noncanonical = body[:first_comma] + b", " + body[first_comma + 1 :]
            _raise_code(
                "invalid_request",
                partial(parse_canonical_json, noncanonical, max_depth=8),
            )


def test_control_receipt_time_accepts_exact_bounds_and_rejects_one_second_outside() -> None:
    case = next(
        case
        for _, case in HTTP_CASES
        if _verification_call(case).active_control_generation is not None
        and "rem-proposed" not in _text(_entries(case, "expected"), "headers.signature-input")
    )
    call = _verification_call(case)
    signature_input = next(value.decode() for name, value in call.request.headers if name == b"signature-input")
    created_match = re.search(r";created=(\d+)", signature_input)
    expires_match = re.search(r";expires=(\d+)", signature_input)
    assert created_match is not None and expires_match is not None
    created = int(created_match.group(1))
    expires = int(expires_match.group(1))
    assert created + 1 <= expires <= created + 60

    assert _verify(call._replace(received_at=created - 30))
    assert _verify(call._replace(received_at=expires))
    _raise_code("authentication_required", lambda: _verify(call._replace(received_at=created - 31)))
    _raise_code("authentication_required", lambda: _verify(call._replace(received_at=expires + 1)))


def test_reporter_signature_omits_client_clock_and_receipt_time_is_not_freshness() -> None:
    case = next(case for _, case in HTTP_CASES if case["id"] == "http.reporter-signature")
    call = _verification_call(case)
    signature_input = next(value for name, value in call.request.headers if name == b"signature-input")

    assert b";created=" not in signature_input
    first = _verify(call._replace(received_at=0))
    second = _verify(call._replace(received_at=2**64 - 1))
    assert first == second
    assert b";created=" not in dict(first.signature_inputs)["rem"]
    assert not hasattr(first, "created")

    injected = signature_input.replace(b";keyid=", b";created=0;keyid=", 1)
    request = _replace_header(call.request, b"signature-input", injected)
    _raise_code("authentication_required", lambda: _verify(call._replace(request=request)))


def test_dual_signature_parameters_must_be_identical_except_for_keyid() -> None:
    case = next(case for _, case in HTTP_CASES if case["id"] == "rotation.control-key")
    call = _verification_call(case)
    route = route_profile(call.route_id)
    raw = next(value for name, value in call.request.headers if name == b"signature-input")
    current, proposed = raw.split(b", ", 1)
    created = re.search(rb";created=([0-9]+)", proposed)
    assert created is not None
    replacement = str(int(created.group(1)) + 1).encode("ascii")
    proposed = proposed[: created.start(1)] + replacement + proposed[created.end(1) :]

    _raise_code(
        "authentication_required",
        lambda: _parse_signature_input(current + b", " + proposed, route, _components(route, ())),
    )


def test_oversize_authority_is_rejected_before_ascii_decoding() -> None:
    class DecodeGuard(bytes):
        def decode(self, *_args: object, **_kwargs: object) -> str:
            raise AssertionError("oversize authority reached ASCII decoding")

    _raise_code("authentication_required", lambda: _canonical_authority(DecodeGuard(b"x" * 260)))


def test_signature_key_lookup_is_bound_to_the_profile_label_not_an_attacker_keyid() -> None:
    case = next(case for _, case in HTTP_CASES if case["id"] == "http.reporter-signature")
    call = _verification_call(case)
    signature_input = next(value.decode() for name, value in call.request.headers if name == b"signature-input")
    keyid = re.search(r';keyid="([A-Za-z0-9_-]{43})"', signature_input)
    assert keyid is not None
    key = next(iter(call.keys.values()))

    _raise_code("authentication_required", lambda: _verify(call._replace(keys={keyid.group(1): key})))


def test_verifier_rejects_caller_forged_route_profiles() -> None:
    case = next(case for _, case in HTTP_CASES if case["id"] == "http.reporter-signature")
    call = _verification_call(case)
    forged = dataclasses.replace(route_profile(call.route_id), id="reporter.forged")

    with pytest.raises(ValueError, match="registered route profile"):
        verify_signed_request(
            call.request,
            route=forged,
            keys=call.keys,
            expected_authority=call.expected_authority,
            received_at=call.received_at,
            active_control_generation=call.active_control_generation,
        )


@pytest.mark.parametrize(
    ("case_id", "name", "value", "expected_code"),
    [
        ("http.reporter-signature", b"repr-digest", b"sha-256=:AAAA:", "invalid_request"),
        ("http.reporter-signature", b"transfer-encoding", b"chunked", "invalid_request"),
        ("http.reporter-signature", b"x-extra", b"ok\r\nsmuggled: value", "invalid_request"),
        ("http.route.reporter.enrollment.status", b"content-length", b"1", "invalid_request"),
    ],
)
def test_unsigned_ambiguous_or_forbidden_body_metadata_is_rejected(
    case_id: str,
    name: bytes,
    value: bytes,
    expected_code: str,
) -> None:
    case = next(case for _, case in HTTP_CASES if case["id"] == case_id)
    call = _verification_call(case)
    request = dataclasses.replace(call.request, headers=(*call.request.headers, (name, value)))

    _raise_code(expected_code, lambda: _verify(call._replace(request=request)))


def test_individual_header_limits_precede_body_processing() -> None:
    case = next(case for _, case in HTTP_CASES if case["id"] == "http.reporter-signature")
    call = _verification_call(case)

    oversized_digest = _replace_header(call.request, b"content-digest", b"x" * 257)
    _raise_code(
        "headers_too_large",
        lambda: _verify(call._replace(request=oversized_digest)),
    )

    oversized_signature_input = _replace_header(call.request, b"signature-input", b"x" * 1025)
    oversized_body = dataclasses.replace(oversized_signature_input, body=b"x" * 1025)
    _raise_code(
        "headers_too_large",
        lambda: _verify(call._replace(request=oversized_body)),
    )


def test_oversized_uint64_header_is_classified_without_integer_conversion_failure() -> None:
    call = _call_for("http.control-signature")
    request = _replace_header(call.request, b"rem-control-key-generation", b"1" * 5000)

    _raise_code("invalid_request", lambda: _verify(call._replace(request=request)))


def test_bodyless_route_allows_only_the_explicit_zero_content_length() -> None:
    case = next(case for _, case in HTTP_CASES if case["id"] == "http.route.reporter.enrollment.status")
    call = _verification_call(case)
    request = dataclasses.replace(call.request, headers=(*call.request.headers, (b"content-length", b"0")))

    assert _verify(call._replace(request=request)).request == request


def _call_for(case_id: str) -> VerificationCall:
    return _verification_call(next(case for _, case in HTTP_CASES if case["id"] == case_id))


def _header(request: SignedRequest, name: bytes) -> bytes:
    return next(value for header_name, value in request.headers if header_name == name)


def _with_header(request: SignedRequest, name: bytes, value: bytes) -> SignedRequest:
    return dataclasses.replace(request, headers=(*request.headers, (name, value)))


def _verify_with_route(call: VerificationCall, route: Any) -> Any:
    return verify_signed_request(
        call.request,
        route=route,
        keys=call.keys,
        expected_authority=call.expected_authority,
        received_at=call.received_at,
        active_control_generation=call.active_control_generation,
    )


class _AlwaysEqualPurpose(tuple[bytes, ...]):
    """A purpose set that claims equality with any registered purpose set."""

    __slots__ = ()

    def __eq__(self, other: object) -> bool:
        del other
        return True

    def __ne__(self, other: object) -> bool:
        del other
        return False

    def __hash__(self) -> int:
        return 0


_FORGED_FIELD_VALUES: Final[dict[str, Any]] = {
    "surface": "control",
    "method": b"GET",
    "path_template": b"/api/v1/forged",
    "purpose": (b"rem-report-v1", b"rem-forged-v1"),
    "signature_profile": "control-rfc9421-v1",
    "request_body_max_bytes": 65536,
    "query_kind": "roster-events",
    "reporter_extras": (),
    "dual_signature": True,
}


def test_forged_field_table_covers_every_route_profile_field_except_id() -> None:
    route = route_profile("reporter.report.create")
    names = {field.name for field in dataclasses.fields(route)}

    assert names - {"id"} == set(_FORGED_FIELD_VALUES)
    for name, value in _FORGED_FIELD_VALUES.items():
        assert getattr(route, name) != value, f"forged {name} must differ from the registered value"


@pytest.mark.parametrize("field_name", sorted(_FORGED_FIELD_VALUES))
def test_verifier_rejects_a_registered_id_with_any_forged_field(field_name: str) -> None:
    call = _call_for("http.reporter-signature")
    forged = dataclasses.replace(route_profile(call.route_id), **{field_name: _FORGED_FIELD_VALUES[field_name]})

    with pytest.raises(ValueError, match="registered route profile"):
        _verify_with_route(call, forged)


def test_verifier_rejects_a_route_profile_subclass_that_claims_equality() -> None:
    call = _call_for("http.reporter-signature")
    registered = route_profile(call.route_id)

    @dataclasses.dataclass(frozen=True, slots=True, eq=False)
    class LyingRouteProfile(RouteProfile):
        def __eq__(self, other: object) -> bool:
            del other
            return True

        def __ne__(self, other: object) -> bool:
            del other
            return False

        def __hash__(self) -> int:
            return 0

    values = {field.name: getattr(registered, field.name) for field in dataclasses.fields(registered)}
    values["request_body_max_bytes"] = 65536
    forged = LyingRouteProfile(**values)
    assert registered == forged, "the forgery must defeat an equality check"
    assert not registered != forged  # noqa: SIM202 - the inequality operator is the forged surface

    with pytest.raises(ValueError, match="registered route profile"):
        _verify_with_route(call, forged)


def test_verifier_rejects_a_purpose_tuple_subclass_that_claims_equality() -> None:
    call = _call_for("http.reporter-signature")
    registered = route_profile(call.route_id)
    forged = dataclasses.replace(registered, purpose=_AlwaysEqualPurpose((b"rem-report-v1", b"rem-forged-v1")))
    assert registered == forged, "the forgery must defeat a field-by-field equality check"

    with pytest.raises(ValueError, match="registered route profile"):
        _verify_with_route(call, forged)


def test_verifier_requires_the_registered_route_object_not_an_equal_copy() -> None:
    call = _call_for("http.reporter-signature")
    registered = route_profile(call.route_id)
    copy = dataclasses.replace(registered)
    assert copy == registered and copy is not registered

    with pytest.raises(ValueError, match="registered route profile"):
        _verify_with_route(call, copy)
    assert _verify_with_route(call, registered).route_id == registered.id


# Section 9 admission order: each request below carries two faults from different
# steps; the verifier must report the earlier step's code.


@pytest.mark.parametrize(
    "authority",
    [b"other.example.com:58443", b"Ha.Example.com:58443", b"192.0.2.1:58443", b"ha.example.com"],
)
def test_oversized_signature_header_precedes_authority_failures(authority: bytes) -> None:
    call = _call_for("http.reporter-signature")
    request = _replace_header(call.request, b"signature", b"x" * 1025)
    request = dataclasses.replace(request, authority=authority)

    _raise_code("headers_too_large", lambda: _verify(call._replace(request=request)))


@pytest.mark.parametrize(
    ("name", "size"),
    [(b"signature", 1025), (b"signature-input", 1025), (b"content-digest", 257)],
)
def test_oversized_profile_header_precedes_oversized_body(name: bytes, size: int) -> None:
    call = _call_for("http.reporter-signature")
    request = _replace_header(call.request, name, b"x" * size)
    request = dataclasses.replace(request, body=b"x" * (route_profile(call.route_id).request_body_max_bytes + 1))

    _raise_code("headers_too_large", lambda: _verify(call._replace(request=request)))


@pytest.mark.parametrize("name", [b"signature", b"signature-input", b"rem-purpose", b"rem-session-id"])
def test_body_over_route_cap_precedes_duplicate_headers(name: bytes) -> None:
    call = _call_for("http.reporter-signature")
    request = _with_header(call.request, name, _header(call.request, name))
    request = dataclasses.replace(request, body=b"x" * (route_profile(call.route_id).request_body_max_bytes + 1))

    _raise_code("request_body_too_large", lambda: _verify(call._replace(request=request)))


@pytest.mark.parametrize("name", [b"signature", b"rem-purpose", b"content-digest"])
def test_content_encoding_precedes_duplicate_headers(name: bytes) -> None:
    call = _call_for("http.reporter-signature")
    request = _with_header(call.request, name, _header(call.request, name))
    request = _with_header(request, b"content-encoding", b"gzip")

    _raise_code("unsupported_media_type", lambda: _verify(call._replace(request=request)))


def test_duplicate_rem_header_precedes_wrong_content_type() -> None:
    call = _call_for("http.reporter-signature")
    request = _with_header(call.request, b"rem-purpose", _header(call.request, b"rem-purpose"))
    request = _replace_header(request, b"content-type", b"text/plain")

    _raise_code("unsupported_media_type", lambda: _verify(call._replace(request=request)))


def test_malformed_report_session_header_precedes_wrong_purpose() -> None:
    call = _call_for("http.reporter-signature")
    request = _replace_header(call.request, b"rem-purpose", b"rem-session-v1")
    request = _replace_header(request, b"rem-session-id", b"not!base64url")

    _raise_code("invalid_request", lambda: _verify(call._replace(request=request)))


@pytest.mark.parametrize(
    ("name", "value"),
    [
        (b"rem-request-nonce", b"not!base64url"),
        (b"rem-request-expires", b"01"),
        (b"rem-control-key-generation", b"01"),
    ],
)
def test_malformed_control_header_precedes_stale_control_key_generation(name: bytes, value: bytes) -> None:
    call = _call_for("http.control-signature")
    assert call.active_control_generation is not None
    stale = call._replace(
        request=_replace_header(call.request, name, value),
        active_control_generation=call.active_control_generation + 1,
    )

    _raise_code("invalid_request", lambda: _verify(stale))


@pytest.mark.parametrize(
    ("name", "value"),
    [
        (b"transfer-encoding", b"chunked"),
        (b"repr-digest", b"sha-256=:AAAA:"),
        (b"content-length", b"1"),
    ],
)
def test_wrong_content_type_precedes_invalid_body_framing(name: bytes, value: bytes) -> None:
    call = _call_for("http.reporter-signature")
    request = _replace_header(call.request, b"content-type", b"text/plain")
    request = _with_header(request, name, value)

    _raise_code("unsupported_media_type", lambda: _verify(call._replace(request=request)))


def test_content_encoding_precedes_invalid_body_framing() -> None:
    call = _call_for("http.reporter-signature")
    request = _with_header(call.request, b"transfer-encoding", b"chunked")
    request = _with_header(request, b"content-encoding", b"gzip")

    _raise_code("unsupported_media_type", lambda: _verify(call._replace(request=request)))


def test_body_route_without_content_type_is_unsupported_media_type() -> None:
    call = _call_for("http.reporter-signature")
    assert route_profile(call.route_id).has_body
    assert _header(call.request, b"content-type") == b"application/json"
    headers = tuple((name, value) for name, value in call.request.headers if name != b"content-type")
    request = dataclasses.replace(call.request, headers=headers)

    _raise_code("unsupported_media_type", lambda: _verify(call._replace(request=request)))


def test_http_field_names_are_case_insensitive_for_profile_headers() -> None:
    call = _call_for("http.reporter-signature")
    request = dataclasses.replace(
        call.request,
        headers=tuple((name.title(), value) for name, value in call.request.headers),
    )

    verified = _verify(call._replace(request=request))

    assert verified.request == request
    assert verified.signature_bases == _verify(call).signature_bases


def test_profile_header_values_use_the_trimmed_http_field_value() -> None:
    call = _call_for("http.reporter-signature")
    covered_headers = {
        b"rem-purpose",
        b"rem-session-id",
        b"rem-sequence",
        b"content-type",
        b"content-digest",
    }
    request = dataclasses.replace(
        call.request,
        headers=tuple(
            (name, b" \t" + value + b"\t " if name in covered_headers else value)
            for name, value in call.request.headers
        ),
    )

    verified = _verify(call._replace(request=request))

    assert verified.request == request
    assert verified.signature_bases == _verify(call).signature_bases


def test_mixed_case_duplicate_profile_header_is_still_a_duplicate() -> None:
    call = _call_for("http.reporter-signature")
    request = _with_header(call.request, b"Rem-Purpose", _header(call.request, b"rem-purpose"))

    _raise_code("invalid_request", lambda: _verify(call._replace(request=request)))


def test_mixed_case_oversized_signature_header_keeps_the_header_limit_classification() -> None:
    call = _call_for("http.reporter-signature")
    headers = tuple(
        (b"Signature" if name == b"signature" else name, b"x" * 1025 if name == b"signature" else value)
        for name, value in call.request.headers
    )
    request = dataclasses.replace(call.request, headers=headers)

    _raise_code("headers_too_large", lambda: _verify(call._replace(request=request)))


def test_non_ascii_rem_purpose_is_a_malformed_rem_header() -> None:
    call = _call_for("http.reporter-signature")
    request = _replace_header(call.request, b"rem-purpose", b"\xff")

    _raise_code("invalid_request", lambda: _verify(call._replace(request=request)))


def test_verified_reporter_identity_is_not_rederived_from_the_active_keyid() -> None:
    call = _call_for("http.reporter-signature")
    active_binding = call.keys["rem"]
    stable_reporter_id = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    binding = VerificationKey(identity=stable_reporter_id, public_jwk=active_binding.public_jwk)

    verified = _verify(call._replace(keys={"rem": binding}))

    assert verified.verified_identity == stable_reporter_id
    assert dict(verified.keyids)["rem"] == active_binding.public_jwk.thumbprint
    assert verified.verified_identity != dict(verified.keyids)["rem"]


@pytest.mark.parametrize(
    ("case_id", "mismatched_purpose"),
    [
        pytest.param(
            "http.route.reporter.enrollment.create",
            b"rem-managed-enroll-v1",
            id="interactive-body-with-managed-purpose",
        ),
        pytest.param(
            "http.route.reporter.enrollment.create.managed",
            b"rem-enroll-v1",
            id="managed-body-with-interactive-purpose",
        ),
    ],
)
def test_enrollment_create_rejects_a_valid_signature_for_the_other_body_mode(
    case_id: str,
    mismatched_purpose: bytes,
) -> None:
    call = _resigned_enrollment_call(case_id, mismatched_purpose)

    _raise_code("authentication_required", lambda: _verify(call))


@pytest.mark.parametrize(
    ("case_id", "matching_purpose", "mode"),
    [
        pytest.param("http.route.reporter.enrollment.create", b"rem-enroll-v1", "interactive", id="interactive"),
        pytest.param(
            "http.route.reporter.enrollment.create.managed", b"rem-managed-enroll-v1", "managed", id="managed"
        ),
    ],
)
def test_enrollment_create_accepts_a_resigned_request_whose_purpose_matches_the_body_mode(
    case_id: str,
    matching_purpose: bytes,
    mode: str,
) -> None:
    # Positive control for the mismatch test: the re-signing helper (fresh key, re-signed
    # base, and a recomputed Content-Digest) yields requests the verifier accepts, so the
    # mismatch rejection above comes from the purpose/mode binding alone.
    vector_body = _verification_call(_case("http-signatures.json", case_id)).request.body
    for call in (
        _resigned_enrollment_call(case_id, matching_purpose),
        _resigned_enrollment_call(case_id, matching_purpose, body=vector_body),
    ):
        verified = _verify(call)

        assert verified.verified_identity == call.keys["rem"].identity
        assert cast(JsonObject, parse_canonical_json(call.request.body, max_depth=8))["mode"] == mode


def _canonical_json_bytes(value: object) -> bytes:
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    # Guard the test's premise: the body passes the step-7 canonical JSON check, so only
    # the enrollment-mode check can reject it.
    parse_canonical_json(
        body, max_depth=8, max_length=route_profile("reporter.enrollment.create").request_body_max_bytes
    )
    return body


def _vector_body_object(case_id: str) -> JsonObject:
    body = parse_canonical_json(_verification_call(_case("http-signatures.json", case_id)).request.body, max_depth=8)
    assert isinstance(body, dict)
    return cast(JsonObject, body)


_MISSING: Final = object()


@pytest.mark.parametrize(
    "case_id",
    ["http.route.reporter.enrollment.create", "http.route.reporter.enrollment.create.managed"],
    ids=["interactive-vector", "managed-vector"],
)
@pytest.mark.parametrize("purpose", [b"rem-enroll-v1", b"rem-managed-enroll-v1"], ids=["enroll", "managed-enroll"])
@pytest.mark.parametrize(
    "mode",
    [
        pytest.param(_MISSING, id="missing"),
        pytest.param("bogus", id="unknown-string"),
        pytest.param("Interactive", id="wrong-case"),
        pytest.param(1, id="integer"),
        pytest.param(None, id="null"),
        pytest.param(["interactive"], id="array"),
    ],
)
def test_enrollment_create_rejects_an_authentic_request_whose_body_mode_is_invalid(
    case_id: str,
    purpose: bytes,
    mode: object,
) -> None:
    body = _vector_body_object(case_id)
    if mode is _MISSING:
        del body["mode"]
    else:
        body["mode"] = mode
    call = _resigned_enrollment_call(case_id, purpose, body=_canonical_json_bytes(body))

    _raise_code("invalid_request", lambda: _verify(call))


@pytest.mark.parametrize(
    "case_id",
    ["http.route.reporter.enrollment.create", "http.route.reporter.enrollment.create.managed"],
    ids=["interactive-vector", "managed-vector"],
)
@pytest.mark.parametrize("purpose", [b"rem-enroll-v1", b"rem-managed-enroll-v1"], ids=["enroll", "managed-enroll"])
def test_enrollment_create_rejects_an_authentic_request_whose_body_is_not_an_object(
    case_id: str,
    purpose: bytes,
) -> None:
    body = _canonical_json_bytes([_vector_body_object(case_id)])
    call = _resigned_enrollment_call(case_id, purpose, body=body)

    _raise_code("invalid_request", lambda: _verify(call))


_QUERY_KIND_BY_FORMS: Final = {
    (): None,
    ("after_status_generation={uint64}&wait_seconds={0..25}",): "enrollment-status",
    ("after_generation={uint64}&wait_seconds={0..25}",): "roster-events",
    ("after_generation={uint64}", "continuation_token={opaque128}"): "roster-snapshot",
}
_REPORTER_BASE_COMPONENTS: Final = ("@method", "@authority", "@path", "@query", "rem-purpose")
_CONTROL_BASE_COMPONENTS: Final = (
    "@method",
    "@authority",
    "@path",
    "@query",
    "rem-purpose",
    "rem-control-key-generation",
    "rem-request-nonce",
    "rem-request-expires",
)


def _query_names(query_form: str) -> tuple[str, ...]:
    return tuple(member.partition("=")[0] for member in query_form.split("&"))


def test_runtime_route_ids_exactly_match_signed_manifest_routes() -> None:
    manifest_route_ids = {
        cast(str, route["id"])
        for route in cast(list[JsonObject], _load("../routes.json")["routes"])
        if route["signature_profile"] is not None
    }

    assert set(_ROUTES) == manifest_route_ids


@pytest.mark.parametrize(
    "manifest_route",
    [
        route
        for route in cast(list[JsonObject], _load("../routes.json")["routes"])
        if route["signature_profile"] is not None
    ],
    ids=lambda route: cast(str, route["id"]),
)
def test_runtime_profile_matches_every_signed_route_manifest_entry(manifest_route: JsonObject) -> None:
    route_id = cast(str, manifest_route["id"])
    profile = route_profile(route_id)
    query_forms = tuple(cast(list[str], manifest_route["query_forms"]))

    assert profile.surface == manifest_route["surface"]
    assert profile.method == cast(str, manifest_route["method"]).encode("ascii")
    assert profile.path_template == cast(str, manifest_route["path_template"]).encode("ascii")
    assert profile.purpose == tuple(cast(str, manifest_route["purpose"]).encode("ascii").split())
    assert profile.signature_profile == manifest_route["signature_profile"]
    assert profile.request_body_max_bytes == manifest_route["request_body_max_bytes"]
    assert query_forms in _QUERY_KIND_BY_FORMS
    assert profile.query_kind == _QUERY_KIND_BY_FORMS[query_forms]

    vector_cases = [
        case
        for _, case in HTTP_CASES
        if route_id in cast(list[str], case["routes"]) and _verification_call(case).route_id == route_id
    ]
    assert vector_cases, f"no signed request vector represents {route_id}"

    represented_query_names: set[tuple[str, ...]] = set()
    for case in vector_cases:
        inputs = _entries(case, "inputs")
        outputs = _entries(case, "expected")
        signature_input = _text(outputs, "headers.signature-input")
        signature_members = _signature_parameter_members(signature_input)
        expected_labels = ("rem", "rem-proposed") if profile.dual_signature else ("rem",)
        assert tuple(signature_members) == expected_labels

        _, separator, raw_query = _bytes(inputs["request.target"]).partition(b"?")
        query_values = tuple(
            (name.decode("ascii"), value)
            for pair in raw_query.split(b"&")
            if pair
            for name, equals, value in (pair.partition(b"="),)
            if equals
        )
        assert bool(separator) == bool(query_forms)
        represented_query_names.add(tuple(name for name, _ in query_values))

        expected_components = tuple(
            (component, query_name or None) for component, query_name in _covered_components(signature_members["rem"])
        )
        assert _components(profile, query_values) == expected_components
        base_components = _REPORTER_BASE_COMPONENTS if profile.surface == "reporter" else _CONTROL_BASE_COMPONENTS
        expected_extras = tuple(
            component
            for component, query_name in expected_components
            if query_name is None
            and component not in base_components
            and component not in {"content-type", "content-digest"}
        )
        assert profile.reporter_extras == expected_extras

    expected_query_names = {_query_names(form) for form in query_forms} if query_forms else {()}
    assert represented_query_names == expected_query_names


def test_runtime_vector_matrix_executes_every_declared_in_scope_http_mutation() -> None:
    declared_mutations = {
        cast(str, mutation["id"])
        for _, case in HTTP_CASES
        for mutation in cast(list[JsonObject], case["mutations"])
        if not cast(str, mutation["field"]).startswith("transcript.")
    }
    executed_mutations = {cast(str, mutation["id"]) for _, mutation in HTTP_MUTATIONS}

    assert executed_mutations == declared_mutations
