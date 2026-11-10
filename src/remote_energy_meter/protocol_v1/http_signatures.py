"""Narrow RFC 9421 request verification for REM protocol-v1 routes."""

from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import re
import socket
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

import idna

from remote_energy_meter.protocol_v1.canonical_json import JsonValue, parse_canonical_json
from remote_energy_meter.protocol_v1.crypto import P256PublicJwk, verify_content_digest, verify_p256_p1363
from remote_energy_meter.protocol_v1.encoding import decode_base64url, decode_opaque_base64url
from remote_energy_meter.protocol_v1.errors import ProtocolViolation, authentication_required, invalid_request


@dataclass(frozen=True, slots=True)
class VerificationKey:
    """Bind one trusted stable identity to a currently accepted signing key.

    Args:
        identity: Canonical unpadded base64url RFC 7638 thumbprint of the stable
            identity selected by trusted caller state.
        public_jwk: Parsed currently accepted signing key for that identity.

    Raises:
        TypeError: ``identity`` is not a string or ``public_jwk`` is not a parsed
            ``P256PublicJwk``.
        ValueError: ``identity`` is not a canonical 32-byte RFC 7638 thumbprint.
    """

    identity: str
    public_jwk: P256PublicJwk

    def __post_init__(self) -> None:
        if not isinstance(self.identity, str):
            raise TypeError("identity must be a string")
        try:
            decode_base64url(self.identity, exact_length=32)
        except ProtocolViolation:
            raise ValueError("verification identity must be a canonical RFC 7638 thumbprint") from None
        if not isinstance(self.public_jwk, P256PublicJwk):
            raise TypeError("public_jwk must be a parsed P-256 public JWK")


_UINT64_MAX: Final = 2**64 - 1
# Enrollment body ``mode`` -> the only REM-Purpose that may sign it. Every mode is
# listed: an unlisted or non-string mode is rejected, never left unbound.
_ENROLLMENT_MODE_PURPOSES: Final[Mapping[str, bytes]] = MappingProxyType(
    {"interactive": b"rem-enroll-v1", "managed": b"rem-managed-enroll-v1"}
)
_REPORTER_COMPONENTS: Final = ("@method", "@authority", "@path", "@query", "rem-purpose")
_CONTROL_COMPONENTS: Final = (
    "@method",
    "@authority",
    "@path",
    "@query",
    "rem-purpose",
    "rem-control-key-generation",
    "rem-request-nonce",
    "rem-request-expires",
)


@dataclass(frozen=True, slots=True)
class RouteProfile:
    """The signed subset of one immutable route-manifest entry.

    Callers obtain this opaque value from ``route_profile`` and pass that exact
    registered object to ``verify_signed_request``. Constructed copies are rejected,
    even when their fields compare equal.
    """

    id: str
    surface: str
    method: bytes
    path_template: bytes
    purpose: tuple[bytes, ...]
    signature_profile: str
    request_body_max_bytes: int
    query_kind: str | None = None
    reporter_extras: tuple[str, ...] = ()
    dual_signature: bool = False

    @property
    def has_body(self) -> bool:
        """Return whether this route requires a canonical JSON body."""
        return self.request_body_max_bytes > 0


@dataclass(frozen=True, slots=True)
class SignedRequest:
    """Snapshot exact request bytes supplied by a future bounded HTTP adapter.

    Args:
        method: Exact method bytes.
        authority: Exact authority bytes.
        target: Exact path and query bytes.
        headers: Ordered exact field-name/value byte pairs, including duplicates.
        body: Exact received body bytes.

    Bytes-like values are copied to immutable ``bytes`` during construction.

    Raises:
        TypeError: A request field or header pair is not bytes-like, or ``headers``
            does not contain name/value pairs.
    """

    method: bytes
    authority: bytes
    target: bytes
    headers: Sequence[tuple[bytes, bytes]]
    body: bytes

    def __post_init__(self) -> None:
        def snapshot(value: object, field: str) -> bytes:
            if not isinstance(value, (bytes, bytearray, memoryview)):
                raise TypeError(f"{field} must be bytes-like")
            return bytes(value)

        try:
            headers = tuple(
                (snapshot(name, "header name"), snapshot(value, "header value")) for name, value in self.headers
            )
        except (TypeError, ValueError):
            raise TypeError("headers must contain bytes-like name/value pairs") from None
        object.__setattr__(self, "method", snapshot(self.method, "method"))
        object.__setattr__(self, "authority", snapshot(self.authority, "authority"))
        object.__setattr__(self, "target", snapshot(self.target, "target"))
        object.__setattr__(self, "headers", headers)
        object.__setattr__(self, "body", snapshot(self.body, "body"))


@dataclass(frozen=True, slots=True)
class VerifiedRequestIdentity:
    """All request and signature bytes bound by successful verification.

    Stateful replay and exact-result stores can hash or retain this value without
    reconstructing anything from normalized HTTP objects. Reporter identities contain
    no client wall-clock value because the reporter profile transmits none.
    """

    request: SignedRequest
    route_id: str
    method: bytes
    authority: bytes
    target: bytes
    path: bytes
    query: bytes
    headers: tuple[tuple[bytes, bytes], ...]
    body: bytes
    bound_components: tuple[tuple[str, tuple[tuple[bytes, bytes], ...]], ...]
    signature_inputs: tuple[tuple[str, bytes], ...]
    signatures: tuple[tuple[str, bytes], ...]
    signature_bases: tuple[tuple[str, bytes], ...]
    keyids: tuple[tuple[str, str], ...]
    request_hash: bytes
    verified_identity: str


# Static runtime copy of the signed routing profile. Route handlers and business
# behavior intentionally do not live in this package.
def _route(
    route_id: str,
    surface: str,
    method: str,
    path: str,
    purpose: str,
    body_max: int,
    *,
    query_kind: str | None = None,
    reporter_extras: tuple[str, ...] = (),
    dual_signature: bool = False,
) -> RouteProfile:
    return RouteProfile(
        id=route_id,
        surface=surface,
        method=method.encode("ascii"),
        path_template=path.encode("ascii"),
        purpose=tuple(item.encode("ascii") for item in purpose.split()),
        signature_profile=f"{surface}-rfc9421-v1",
        request_body_max_bytes=body_max,
        query_kind=query_kind,
        reporter_extras=reporter_extras,
        dual_signature=dual_signature,
    )


_ROUTES: Final = {
    profile.id: profile
    for profile in (
        _route(
            "reporter.enrollment.create",
            "reporter",
            "POST",
            "/api/v1/enrollments",
            "rem-enroll-v1 rem-managed-enroll-v1",
            4096,
        ),
        _route(
            "reporter.enrollment.reveal",
            "reporter",
            "POST",
            "/api/v1/enrollments/{enrollment_id}/reveal",
            "rem-enroll-reveal-v1",
            256,
        ),
        _route(
            "reporter.enrollment.confirm",
            "reporter",
            "POST",
            "/api/v1/enrollments/{enrollment_id}/confirm",
            "rem-enroll-confirm-v1",
            512,
        ),
        _route(
            "reporter.enrollment.status",
            "reporter",
            "GET",
            "/api/v1/enrollments/{enrollment_id}",
            "rem-enroll-status-v1",
            0,
            query_kind="enrollment-status",
        ),
        _route("reporter.challenge.create", "reporter", "POST", "/api/v1/challenges", "rem-challenge-v1", 512),
        _route("reporter.session.create", "reporter", "POST", "/api/v1/sessions", "rem-session-v1", 1024),
        _route(
            "reporter.report.create",
            "reporter",
            "POST",
            "/api/v1/reports",
            "rem-report-v1",
            1024,
            reporter_extras=("rem-session-id", "rem-sequence"),
        ),
        _route(
            "reporter.key-rotation-challenge.create",
            "reporter",
            "POST",
            "/api/v1/reporters/{reporter_id}/key-rotations/challenges",
            "rem-rotate-challenge-v1",
            2048,
        ),
        _route(
            "reporter.key-rotation.complete",
            "reporter",
            "POST",
            "/api/v1/reporters/{reporter_id}/key-rotations/{rotation_id}",
            "rem-rotate-v1",
            1024,
        ),
        _route(
            "control.managed-token.issue",
            "control",
            "POST",
            "/control/v1/managed-enrollment-tokens",
            "rem-control-managed-token-v1",
            1024,
        ),
        _route(
            "control.managed-token.status",
            "control",
            "POST",
            "/control/v1/managed-enrollment-token-operations/status",
            "rem-control-managed-token-status-v1",
            512,
        ),
        _route(
            "control.managed-token.revoke",
            "control",
            "POST",
            "/control/v1/managed-enrollment-tokens/revoke",
            "rem-control-managed-token-revoke-v1",
            512,
        ),
        _route(
            "control.roster.events",
            "control",
            "GET",
            "/control/v1/roster/events",
            "rem-control-roster-events-v1",
            0,
            query_kind="roster-events",
        ),
        _route(
            "control.roster.snapshot",
            "control",
            "GET",
            "/control/v1/roster/snapshot",
            "rem-control-roster-snapshot-v1",
            0,
            query_kind="roster-snapshot",
        ),
        _route(
            "control.reporter.action",
            "control",
            "POST",
            "/control/v1/reporters/actions",
            "rem-control-reporter-action-v1",
            2048,
        ),
        _route(
            "control.reporter-key-rotation.confirm",
            "control",
            "POST",
            "/control/v1/reporters/key-rotations/confirm",
            "rem-control-reporter-key-rotation-v1",
            1024,
        ),
        _route(
            "control.control-key-rotation-challenge.create",
            "control",
            "POST",
            "/control/v1/control-keys/rotation-challenges",
            "rem-control-key-rotation-challenge-v1",
            2048,
        ),
        _route(
            "control.control-key-rotation.complete",
            "control",
            "POST",
            "/control/v1/control-keys/rotations",
            "rem-control-key-rotation-v1",
            4096,
            dual_signature=True,
        ),
        _route(
            "control.control-spki-rotation",
            "control",
            "POST",
            "/control/v1/control-spki/rotations",
            "rem-control-spki-rotation-v1",
            4096,
        ),
        _route(
            "control.mqtt-key.provision",
            "control",
            "POST",
            "/control/v1/mqtt-keys/provision",
            "rem-control-mqtt-key-provision-v1",
            4096,
        ),
        _route(
            "control.mqtt-key.acknowledge",
            "control",
            "POST",
            "/control/v1/mqtt-keys/acknowledge",
            "rem-control-mqtt-key-ack-v1",
            1024,
        ),
        _route(
            "control.lease.refresh",
            "control",
            "POST",
            "/control/v1/leases/refresh",
            "rem-control-lease-refresh-v1",
            512,
        ),
    )
}


def route_profile(route_id: str) -> RouteProfile:
    """Return the registered signed-route profile for an exact route ID.

    Args:
        route_id: Exact ID from the signed subset of ``routes.json``.

    Returns:
        The registered singleton that callers must pass unchanged to
        ``verify_signed_request``.

    Raises:
        ProtocolViolation: ``route_id`` is unknown. The failure has code ``not_found``.
    """
    try:
        return _ROUTES[route_id]
    except KeyError:
        raise ProtocolViolation("not_found", "signed route profile was not found") from None


def _canonical_uint64(value: bytes) -> int:
    if len(value) > 20 or not re.fullmatch(rb"0|[1-9][0-9]*", value):
        raise invalid_request("unsigned integer header or query value is not canonical")
    result = int(value)
    if result > _UINT64_MAX:
        raise invalid_request("unsigned integer header or query value is out of range")
    return result


def _canonical_authority(authority: bytes) -> None:
    if len(authority) > 259:
        raise authentication_required("authority exceeds the profile bound")
    try:
        text = authority.decode("ascii")
    except UnicodeDecodeError:
        raise authentication_required("authority is not canonical ASCII") from None
    if text != text.lower() or "%" in text or "@" in text:
        raise authentication_required("authority is not canonical")
    host, separator, port_text = text.rpartition(":")
    if not separator or not host or not re.fullmatch(r"[1-9][0-9]*", port_text):
        raise authentication_required("authority does not include a canonical explicit port")
    if int(port_text) > 65535:
        raise authentication_required("authority port is out of range")
    if host.endswith(".") or len(host) > 253:
        raise authentication_required("authority host is not canonical")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise authentication_required("authority host must not be an IP literal")
    try:
        socket.inet_aton(host)
    except OSError:
        pass
    else:
        raise authentication_required("authority host must not be a numeric IP form")
    labels = host.split(".")
    if any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels):
        raise authentication_required("authority host syntax is invalid")
    try:
        for label in labels:
            decoded = idna.decode(label, strict=True, uts46=True, std3_rules=True)
            encoded = idna.encode(decoded, strict=True, uts46=True, std3_rules=True, transitional=False).decode("ascii")
            if encoded != label:
                raise authentication_required("authority host is not canonical IDNA2008")
    except idna.IDNAError:
        raise authentication_required("authority host is not valid IDNA2008") from None


def _template_matches(template: bytes, path: bytes) -> bool:
    template_parts = template.split(b"/")
    path_parts = path.split(b"/")
    if len(template_parts) != len(path_parts):
        return False
    lengths = {b"enrollment_id": 16, b"rotation_id": 16, b"reporter_id": 32}
    for expected, actual in zip(template_parts, path_parts, strict=True):
        if expected.startswith(b"{") and expected.endswith(b"}"):
            name = expected[1:-1]
            try:
                decode_base64url(actual.decode("ascii"), exact_length=lengths[name])
            except (UnicodeDecodeError, KeyError, ProtocolViolation):
                return False
        elif expected != actual:
            return False
    return True


def _target(route: RouteProfile, target: bytes) -> tuple[bytes, bytes, tuple[tuple[str, bytes], ...]]:
    if len(target) > 256:
        raise ProtocolViolation("target_too_long", "request target exceeds the profile bound")
    try:
        target.decode("ascii")
    except UnicodeDecodeError:
        raise invalid_request("request target is not ASCII") from None
    if not target.startswith(b"/") or b"#" in target or b"%" in target or b"//" in target:
        raise invalid_request("request target grammar is invalid")
    path, separator, raw_query = target.partition(b"?")
    if any(part in {b".", b"..", b""} for part in path.split(b"/")[1:]):
        raise invalid_request("request path is not canonical")
    if not _template_matches(route.path_template, path):
        if any(
            candidate.id != route.id and _template_matches(candidate.path_template, path)
            for candidate in _ROUTES.values()
        ):
            raise invalid_request("request target belongs to another route profile")
        raise ProtocolViolation("not_found", "request path does not match the route")
    if route.query_kind is None:
        if separator:
            raise invalid_request("route forbids a query")
        return path, b"?", ()
    if not separator or not raw_query:
        raise invalid_request("route requires an exact query")
    pairs: list[tuple[str, bytes]] = []
    for pair in raw_query.split(b"&"):
        name, equals, value = pair.partition(b"=")
        if not equals or not name or not value:
            raise invalid_request("query member is malformed")
        try:
            pairs.append((name.decode("ascii"), value))
        except UnicodeDecodeError:
            raise invalid_request("query member is not ASCII") from None
    names = tuple(name for name, _ in pairs)
    values = dict(pairs)
    if len(values) != len(pairs):
        raise invalid_request("query member is duplicated")
    if route.query_kind == "enrollment-status":
        if names != ("after_status_generation", "wait_seconds"):
            raise invalid_request("enrollment status query form is invalid")
        _canonical_uint64(values["after_status_generation"])
        wait = _canonical_uint64(values["wait_seconds"])
        if wait > 25:
            raise invalid_request("long-poll wait is out of range")
    elif route.query_kind == "roster-events":
        if names != ("after_generation", "wait_seconds"):
            raise invalid_request("roster events query form is invalid")
        _canonical_uint64(values["after_generation"])
        wait = _canonical_uint64(values["wait_seconds"])
        if wait > 25:
            raise invalid_request("long-poll wait is out of range")
    elif names == ("after_generation",):
        _canonical_uint64(values["after_generation"])
    elif names == ("continuation_token",):
        try:
            token = values["continuation_token"].decode("ascii")
        except UnicodeDecodeError:
            raise invalid_request("snapshot token is not ASCII") from None
        decode_opaque_base64url(token, minimum=1, maximum=128)
    else:
        raise invalid_request("roster snapshot query form is invalid")
    return path, b"?" + raw_query, tuple(pairs)


_SIGNATURE_HEADERS: Final = frozenset({b"signature", b"signature-input"})
_HEADER_LIMITS: Final = {
    b"content-digest": 256,
    b"signature": 1024,
    b"signature-input": 1024,
}


def _header_name(name: bytes) -> bytes:
    """Return the case-insensitive HTTP field name used by the profile."""
    return name.lower()


def _header_value(value: bytes) -> bytes:
    """Return the RFC 9421 field value after removing surrounding OWS."""
    return value.strip(b" \t")


def _header_sizes(request: SignedRequest) -> None:
    """Section 9 step 4: header count, total size and per-header bounds only."""
    if len(request.headers) > 24 or sum(len(name) + len(value) for name, value in request.headers) > 8192:
        raise ProtocolViolation("headers_too_large", "request headers exceed the profile bound")
    for name, value in request.headers:
        maximum = _HEADER_LIMITS.get(_header_name(name))
        if maximum is not None and len(value) > maximum:
            raise ProtocolViolation("headers_too_large", "profile header exceeds its byte bound")


def _media(route: RouteProfile, request: SignedRequest) -> None:
    """Section 9 step 6: body cap, then media type and content coding."""
    if len(request.body) > route.request_body_max_bytes:
        raise ProtocolViolation("request_body_too_large", "request body exceeds the route bound")
    for raw_name, raw_value in request.headers:
        name = _header_name(raw_name)
        value = _header_value(raw_value)
        if name == b"content-encoding":
            raise ProtocolViolation("unsupported_media_type", "request content encoding is unsupported")
        if route.has_body and name == b"content-type" and value != b"application/json":
            raise ProtocolViolation("unsupported_media_type", "request content type is unsupported")
    if route.has_body and all(_header_name(name) != b"content-type" for name, _ in request.headers):
        raise ProtocolViolation("unsupported_media_type", "request content type is unsupported")


def _headers(request: SignedRequest) -> dict[bytes, bytes]:
    """Section 9 step 7: header syntax and duplicates outside the signature headers.

    Duplicate signature headers are an authentication failure (step 8) and are
    reported by ``_signature_headers`` after every ``invalid_request`` check.
    """
    grouped: dict[bytes, list[bytes]] = {}
    for raw_name, raw_value in request.headers:
        if not re.fullmatch(rb"[A-Za-z0-9-]+", raw_name):
            raise invalid_request("header name is malformed")
        if re.search(rb"[\x00-\x08\x0a-\x1f\x7f]", raw_value):
            raise invalid_request("header value contains a forbidden control byte")
        name = _header_name(raw_name)
        grouped.setdefault(name, []).append(_header_value(raw_value))
    if any(len(values) > 1 and name not in _SIGNATURE_HEADERS for name, values in grouped.items()):
        raise invalid_request("profile header is duplicated")
    return {name: values[0] for name, values in grouped.items()}


def _signature_headers(request: SignedRequest) -> tuple[bytes, bytes]:
    """Section 9 step 8: exactly one Signature-Input and one Signature header."""
    found: dict[bytes, list[bytes]] = {name: [] for name in _SIGNATURE_HEADERS}
    for raw_name, raw_value in request.headers:
        name = _header_name(raw_name)
        if name in found:
            found[name].append(_header_value(raw_value))
    for values in found.values():
        if not values:
            raise authentication_required("required profile header is missing")
        if len(values) > 1:
            raise authentication_required("profile signature header is duplicated")
    return found[b"signature-input"][0], found[b"signature"][0]


def _require_header(headers: Mapping[bytes, bytes], name: bytes) -> bytes:
    try:
        return headers[name]
    except KeyError:
        raise invalid_request("required profile header is missing") from None


def _body(route: RouteProfile, request: SignedRequest, headers: Mapping[bytes, bytes]) -> JsonValue | None:
    """Section 9 step 7: body framing, Content-Digest and canonical JSON."""
    if b"repr-digest" in headers or b"transfer-encoding" in headers:
        raise invalid_request("request carries forbidden body metadata")
    content_length = headers.get(b"content-length")
    if content_length is not None and _canonical_uint64(content_length) != len(request.body):
        raise invalid_request("Content-Length does not match the exact body")
    if route.has_body:
        digest = _require_header(headers, b"content-digest")
        try:
            digest_text = digest.decode("ascii")
        except UnicodeDecodeError:
            raise invalid_request("Content-Digest is not ASCII") from None
        verify_content_digest(digest_text, request.body)
        return parse_canonical_json(request.body, max_depth=8, max_length=route.request_body_max_bytes)
    if request.body or any(name in headers for name in (b"content-type", b"content-digest")):
        raise invalid_request("bodyless route carries body metadata")
    return None


def _components(route: RouteProfile, query_values: tuple[tuple[str, bytes], ...]) -> tuple[tuple[str, str | None], ...]:
    base = _REPORTER_COMPONENTS if route.surface == "reporter" else _CONTROL_COMPONENTS
    result: list[tuple[str, str | None]] = [(component, None) for component in base]
    result.extend((component, None) for component in route.reporter_extras)
    result.extend(("@query-param", name) for name, _ in query_values)
    if route.has_body:
        result.extend((("content-type", None), ("content-digest", None)))
    return tuple(result)


def _component_list(components: tuple[tuple[str, str | None], ...]) -> str:
    rendered = []
    for component, name in components:
        item = f'"{component}"'
        if name is not None:
            item += f';name="{name}"'
        rendered.append(item)
    return "(" + " ".join(rendered) + ")"


@dataclass(frozen=True, slots=True)
class _Parameters:
    raw: str
    keyid: str
    tag: str
    created: int | None = None
    expires: int | None = None
    nonce: str | None = None


def _parse_uint_parameter(value: str) -> int:
    if not re.fullmatch(r"0|[1-9][0-9]*", value):
        raise authentication_required("signature time parameter is not canonical")
    result = int(value)
    if result > _UINT64_MAX:
        raise authentication_required("signature time parameter is out of range")
    return result


def _parse_signature_input(
    raw: bytes,
    route: RouteProfile,
    components: tuple[tuple[str, str | None], ...],
) -> tuple[tuple[str, _Parameters], ...]:
    if len(raw) > 1024:
        raise ProtocolViolation("headers_too_large", "Signature-Input exceeds the profile bound")
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        raise authentication_required("Signature-Input is not ASCII") from None
    labels = ("rem", "rem-proposed") if route.dual_signature else ("rem",)
    members = text.split(", ")
    if len(members) != len(labels):
        raise authentication_required("Signature-Input label set is invalid")
    component_text = re.escape(_component_list(components))
    parsed: list[tuple[str, _Parameters]] = []
    for expected_label, member in zip(labels, members, strict=True):
        if route.surface == "reporter":
            pattern = (
                rf"{expected_label}=({component_text};keyid=\"([A-Za-z0-9_-]{{43}})\";"
                rf"alg=\"ecdsa-p256-sha256\";tag=\"([A-Za-z0-9-]+)\")"
            )
            match = re.fullmatch(pattern, member)
            if match is None:
                raise authentication_required("reporter Signature-Input does not match the profile")
            raw_parameters, keyid, tag = match.groups()
            parsed.append((expected_label, _Parameters(raw_parameters, keyid, tag)))
        else:
            pattern = (
                rf"{expected_label}=({component_text};created=([0-9]+);expires=([0-9]+);"
                rf"nonce=\"([A-Za-z0-9_-]{{22}})\";keyid=\"([A-Za-z0-9_-]{{43}})\";"
                rf"alg=\"ecdsa-p256-sha256\";tag=\"([A-Za-z0-9-]+)\")"
            )
            match = re.fullmatch(pattern, member)
            if match is None:
                raise authentication_required("control Signature-Input does not match the profile")
            raw_parameters, created_text, expires_text, nonce, keyid, tag = match.groups()
            parsed.append(
                (
                    expected_label,
                    _Parameters(
                        raw=raw_parameters,
                        keyid=keyid,
                        tag=tag,
                        created=_parse_uint_parameter(created_text),
                        expires=_parse_uint_parameter(expires_text),
                        nonce=nonce,
                    ),
                )
            )
    if route.dual_signature:
        current = parsed[0][1]
        proposed = parsed[1][1]
        if (
            current.created,
            current.expires,
            current.nonce,
            current.tag,
        ) != (
            proposed.created,
            proposed.expires,
            proposed.nonce,
            proposed.tag,
        ):
            raise authentication_required("dual-signature parameters differ outside keyid")
    return tuple(parsed)


def _parse_signature(raw: bytes, labels: tuple[str, ...]) -> tuple[tuple[str, bytes], ...]:
    if len(raw) > 1024:
        raise ProtocolViolation("headers_too_large", "Signature exceeds the profile bound")
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        raise authentication_required("Signature is not ASCII") from None
    members = text.split(", ")
    if len(members) != len(labels):
        raise authentication_required("Signature label set is invalid")
    parsed: list[tuple[str, bytes]] = []
    for label, member in zip(labels, members, strict=True):
        match = re.fullmatch(rf"{label}=:([A-Za-z0-9+/]{{86}}==):", member)
        if match is None:
            raise authentication_required("Signature member does not match the profile")
        encoded = match.group(1)
        try:
            signature = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise authentication_required("Signature byte sequence is malformed") from None
        if len(signature) != 64 or base64.b64encode(signature).decode("ascii") != encoded:
            raise authentication_required("Signature byte sequence is not canonical P1363")
        parsed.append((label, signature))
    return tuple(parsed)


def _component_values(
    request: SignedRequest,
    path: bytes,
    query: bytes,
    headers: Mapping[bytes, bytes],
    query_values: tuple[tuple[str, bytes], ...],
) -> dict[tuple[str, str | None], bytes]:
    result: dict[tuple[str, str | None], bytes] = {
        ("@method", None): request.method,
        ("@authority", None): request.authority,
        ("@path", None): path,
        ("@query", None): query,
    }
    for name, value in headers.items():
        result[(name.decode("ascii"), None)] = value.strip(b" \t")
    result.update({("@query-param", name): value for name, value in query_values})
    return result


def _signature_base(
    components: tuple[tuple[str, str | None], ...],
    values: Mapping[tuple[str, str | None], bytes],
    parameters: _Parameters,
) -> tuple[bytes, tuple[tuple[bytes, bytes], ...]]:
    lines: list[bytes] = []
    bound: list[tuple[bytes, bytes]] = []
    for component, name in components:
        rendered_name = f'"{component}"'.encode("ascii")
        if name is not None:
            rendered_name += f';name="{name}"'.encode("ascii")
        try:
            value = values[(component, name)]
        except KeyError:
            raise authentication_required("signed component value is missing") from None
        lines.append(rendered_name + b": " + value)
        bound.append((rendered_name, value))
    lines.append(b'"@signature-params": ' + parameters.raw.encode("ascii"))
    return b"\n".join(lines), tuple(bound)


def _request_hash(request: SignedRequest) -> bytes:
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


@dataclass(frozen=True, slots=True)
class _ProfileHeaders:
    generation: int | None
    request_nonce: str | None
    request_expires: int | None


def _parse_profile_headers(route: RouteProfile, headers: Mapping[bytes, bytes]) -> _ProfileHeaders:
    if route.surface == "reporter":
        if route.reporter_extras == ("rem-session-id", "rem-sequence"):
            try:
                session = _require_header(headers, b"rem-session-id").decode("ascii")
            except UnicodeDecodeError:
                raise invalid_request("report session ID is not ASCII") from None
            decode_base64url(session, exact_length=16)
            _canonical_uint64(_require_header(headers, b"rem-sequence"))
        return _ProfileHeaders(generation=None, request_nonce=None, request_expires=None)

    generation = _canonical_uint64(_require_header(headers, b"rem-control-key-generation"))
    try:
        request_nonce = _require_header(headers, b"rem-request-nonce").decode("ascii")
    except UnicodeDecodeError:
        raise invalid_request("control nonce is not ASCII") from None
    decode_base64url(request_nonce, exact_length=16)
    request_expires = _canonical_uint64(_require_header(headers, b"rem-request-expires"))
    return _ProfileHeaders(
        generation=generation,
        request_nonce=request_nonce,
        request_expires=request_expires,
    )


@dataclass(frozen=True, slots=True)
class _SignatureVerificationContext:
    route: RouteProfile
    purpose: bytes
    profile_headers: _ProfileHeaders
    received_at: int
    keys: Mapping[str, VerificationKey]
    components: tuple[tuple[str, str | None], ...]
    values: Mapping[tuple[str, str | None], bytes]
    signatures: Mapping[str, bytes]


@dataclass(frozen=True, slots=True)
class _VerifiedSignature:
    label: str
    base: bytes
    bound_components: tuple[tuple[bytes, bytes], ...]
    keyid: str
    identity: str | None


def _verify_signature(
    label: str,
    parameters: _Parameters,
    context: _SignatureVerificationContext,
) -> _VerifiedSignature:
    if parameters.tag.encode("ascii") != context.purpose:
        raise authentication_required("signature tag does not match REM-Purpose")
    if context.route.surface == "control":
        if parameters.created is None or parameters.expires is None or parameters.nonce is None:
            raise authentication_required("control signature parameters are incomplete")
        if context.profile_headers.request_nonce is None or context.profile_headers.request_expires is None:
            raise authentication_required("control request headers are incomplete")
        if (
            parameters.nonce != context.profile_headers.request_nonce
            or parameters.expires != context.profile_headers.request_expires
        ):
            raise authentication_required("control signature parameters do not match headers")
        if not parameters.created <= context.received_at + 30:
            raise authentication_required("control signature was created too far in the future")
        if (
            not parameters.created + 1 <= parameters.expires <= parameters.created + 60
            or context.received_at > parameters.expires
        ):
            raise authentication_required("control signature expiry is outside the profile bounds")
    try:
        key_binding = context.keys[label]
    except KeyError:
        raise authentication_required("signature role has no verification key") from None
    key_value = key_binding.public_jwk
    if key_value.thumbprint != parameters.keyid:
        raise authentication_required("signature key ID does not match the role-bound public key")
    identity = key_binding.identity if label == "rem" else None
    base, bound = _signature_base(context.components, context.values, parameters)
    verify_p256_p1363(key_value.public_key, base, context.signatures[label])
    return _VerifiedSignature(
        label=label,
        base=base,
        bound_components=bound,
        keyid=parameters.keyid,
        identity=identity,
    )


def verify_signed_request(
    request: SignedRequest,
    *,
    route: RouteProfile,
    keys: Mapping[str, VerificationKey],
    expected_authority: bytes,
    received_at: int,
    active_control_generation: int | None,
) -> VerifiedRequestIdentity:
    """Verify one request and return its complete immutable bound identity.

    Args:
        request: Immutable snapshot of the exact request bytes from a bounded adapter.
        route: Exact registered object returned by ``route_profile``. An equal copy or
            subclass is not accepted.
        keys: Trusted bindings keyed by required signature role, ``rem`` and, for the
            control-key rotation route, ``rem-proposed``. The map is never keyed by a
            request-selected key ID.
        expected_authority: Exact configured authority bytes to authenticate.
        received_at: Server receipt time as Unix seconds. Only the control profile
            uses this value; reporter signatures carry no client clock.
        active_control_generation: Currently active control-key generation for a
            control route. Pass ``None`` for reporter routes.

    Returns:
        The trusted stable identity and an immutable snapshot of all exact request,
        component, signature-input, signature, signature-base, and key-ID bytes bound
        by successful verification.

    Raises:
        ValueError: ``route`` is not the exact registered ``RouteProfile`` object.
            This is a plain caller error, not a ``ProtocolViolation``.
        ProtocolViolation: The request fails its first admission or authentication
            check. ``code`` contains the stable RFC 9457 classification.

    Each key binding carries the trusted stable identity selected with that role's
    public key. The result never derives identity from a rotatable request ``keyid``.
    """
    # Identity, not equality: a subclass or tuple-subclass field can forge ``==``.
    registered = _ROUTES.get(route.id)
    if registered is None or route is not registered:
        raise ValueError("route must be a registered route profile")
    route = registered
    # Section 9 steps 2-3: target, route and method.
    path, query, query_values = _target(route, request.target)
    if request.method != route.method:
        raise ProtocolViolation("method_not_allowed", "request method does not match the route")
    # Step 4: header bounds. Step 6: body cap and media type.
    _header_sizes(request)
    _media(route, request)

    # Step 7: every invalid_request check (REM headers, Content-Digest, canonical JSON).
    headers = _headers(request)
    body = _body(route, request, headers)
    enrollment_purpose: bytes | None = None
    if route.id == "reporter.enrollment.create":
        # Exact ``str`` type: a subclass could forge equality or hashing.
        mode = body.get("mode") if isinstance(body, dict) else None
        if type(mode) is not str or mode not in _ENROLLMENT_MODE_PURPOSES:
            raise invalid_request("enrollment mode is invalid")
        enrollment_purpose = _ENROLLMENT_MODE_PURPOSES[mode]
    purpose = _require_header(headers, b"rem-purpose")
    if not re.fullmatch(rb"[a-z0-9]+(?:-[a-z0-9]+)*", purpose):
        raise invalid_request("REM-Purpose header is malformed")
    profile_headers = _parse_profile_headers(route, headers)

    # Step 8: authentication.
    _canonical_authority(request.authority)
    if request.authority != expected_authority:
        raise authentication_required("request authority does not match the configured authority")
    if purpose not in route.purpose:
        raise authentication_required("request purpose does not match the route")
    # Fails closed: on this route ``enrollment_purpose`` is always set by step 7.
    if route.id == "reporter.enrollment.create" and purpose != enrollment_purpose:
        raise authentication_required("request purpose does not match the enrollment mode")
    if route.surface == "control" and (
        active_control_generation is None or profile_headers.generation != active_control_generation
    ):
        raise authentication_required("control key generation is not active")
    signature_input_raw, signature_raw = _signature_headers(request)
    components = _components(route, query_values)
    parameters = _parse_signature_input(signature_input_raw, route, components)
    labels = tuple(label for label, _ in parameters)
    signatures = _parse_signature(signature_raw, labels)
    signature_map = dict(signatures)
    values = _component_values(request, path, query, headers, query_values)

    verification_context = _SignatureVerificationContext(
        route=route,
        purpose=purpose,
        profile_headers=profile_headers,
        received_at=received_at,
        keys=keys,
        components=components,
        values=values,
        signatures=signature_map,
    )
    bases: list[tuple[str, bytes]] = []
    bound_components: list[tuple[str, tuple[tuple[bytes, bytes], ...]]] = []
    keyids: list[tuple[str, str]] = []
    verified_identity: str | None = None
    for label, item in parameters:
        verified_signature = _verify_signature(label, item, verification_context)
        bases.append((verified_signature.label, verified_signature.base))
        bound_components.append((verified_signature.label, verified_signature.bound_components))
        keyids.append((verified_signature.label, verified_signature.keyid))
        if verified_signature.identity is not None:
            verified_identity = verified_signature.identity

    if verified_identity is None:
        raise authentication_required("primary signature identity is missing")
    return VerifiedRequestIdentity(
        request=request,
        route_id=route.id,
        method=request.method,
        authority=request.authority,
        target=request.target,
        path=path,
        query=query,
        headers=tuple(request.headers),
        body=request.body,
        bound_components=tuple(bound_components),
        signature_inputs=tuple((label, item.raw.encode("ascii")) for label, item in parameters),
        signatures=signatures,
        signature_bases=tuple(bases),
        keyids=tuple(keyids),
        request_hash=_request_hash(request),
        verified_identity=verified_identity,
    )
