"""P-256 public JWK, RFC 7638, RFC 9530, and low-S P1363 primitives."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from typing import Any, Final, cast

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from remote_energy_meter.protocol_v1.encoding import decode_base64url, encode_base64url
from remote_energy_meter.protocol_v1.errors import authentication_required, invalid_request

P256_ORDER: Final = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_CONTENT_DIGEST: Final = re.compile(r"sha-256=:([A-Za-z0-9+/]{43}=):\Z")


@dataclass(frozen=True, slots=True)
class P256PublicJwk:
    """An exact canonical public P-256 JWK and its derived identity.

    Callers obtain validated instances from ``parse_p256_public_jwk`` rather than
    constructing this representation directly.
    """

    canonical_bytes: bytes
    x: bytes
    y: bytes
    public_key: ec.EllipticCurvePublicKey
    thumbprint_bytes: bytes
    thumbprint: str


def _reject_duplicate_jwk_members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            raise invalid_request("JWK member is duplicated")
        result[name] = value
    return result


def parse_p256_public_jwk(raw: bytes) -> P256PublicJwk:
    """Parse the profile's byte-exact canonical public P-256 JWK.

    Args:
        raw: Exact UTF-8 JWK bytes in canonical member order.

    Returns:
        The validated coordinates, cryptography public key, and RFC 7638 identity.

    Raises:
        ProtocolViolation: The bytes exceed the bound or are not the profile's
            canonical, on-curve public JWK. The failure has code ``invalid_request``.
    """
    if len(raw) > 256:
        raise invalid_request("P-256 JWK exceeds the profile bound")
    try:
        text = raw.decode("utf-8")
        value = json.loads(text, object_pairs_hook=_reject_duplicate_jwk_members)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise invalid_request("P-256 JWK syntax is invalid") from None
    if not isinstance(value, dict) or list(value) != ["crv", "kty", "x", "y"]:
        raise invalid_request("P-256 JWK members or order are invalid")
    if value.get("crv") != "P-256" or value.get("kty") != "EC":
        raise invalid_request("P-256 JWK key type is invalid")
    if not isinstance(value.get("x"), str) or not isinstance(value.get("y"), str):
        raise invalid_request("P-256 JWK coordinates are invalid")
    x_text = cast(str, value["x"])
    y_text = cast(str, value["y"])
    x = decode_base64url(x_text, exact_length=32)
    y = decode_base64url(y_text, exact_length=32)
    canonical = (
        b'{"crv":"P-256","kty":"EC","x":"' + x_text.encode("ascii") + b'","y":"' + y_text.encode("ascii") + b'"}'
    )
    if raw != canonical:
        raise invalid_request("P-256 JWK is not canonical")
    try:
        public_key = ec.EllipticCurvePublicNumbers(
            int.from_bytes(x, "big"),
            int.from_bytes(y, "big"),
            ec.SECP256R1(),
        ).public_key()
    except ValueError:
        raise invalid_request("P-256 JWK point is not on the curve") from None
    thumbprint_bytes = hashlib.sha256(canonical).digest()
    return P256PublicJwk(
        canonical_bytes=canonical,
        x=x,
        y=y,
        public_key=public_key,
        thumbprint_bytes=thumbprint_bytes,
        thumbprint=encode_base64url(thumbprint_bytes, exact_length=32),
    )


def render_content_digest(body: bytes) -> str:
    """Render the RFC 9530 SHA-256 Content-Digest for exact body bytes.

    Args:
        body: Exact body bytes to hash.

    Returns:
        The sole permitted canonical RFC 9530 dictionary member.
    """
    digest = hashlib.sha256(body).digest()
    return f"sha-256=:{base64.b64encode(digest).decode('ascii')}:"


def parse_content_digest(header: str) -> bytes:
    """Parse the exact canonical RFC 9530 SHA-256 dictionary form.

    Args:
        header: The complete Content-Digest field value after HTTP OWS trimming.

    Returns:
        The 32-byte SHA-256 digest.

    Raises:
        ProtocolViolation: The field is oversized, malformed, non-canonical, or not
            the sole permitted SHA-256 member. The failure has code
            ``invalid_request``.
    """
    if len(header) > 256:
        raise invalid_request("Content-Digest header is too large")
    match = _CONTENT_DIGEST.fullmatch(header)
    if match is None:
        raise invalid_request("Content-Digest header is malformed")
    encoded = match.group(1)
    try:
        digest = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise invalid_request("Content-Digest value is malformed") from None
    if len(digest) != 32 or base64.b64encode(digest).decode("ascii") != encoded:
        raise invalid_request("Content-Digest value is not canonical SHA-256")
    return digest


def verify_content_digest(header: str, body: bytes) -> None:
    """Verify Content-Digest against the exact received body in constant time.

    Args:
        header: The complete Content-Digest field value after HTTP OWS trimming.
        body: Exact received body bytes.

    Returns:
        ``None`` after successful verification.

    Raises:
        ProtocolViolation: The field is invalid or its digest does not match
            ``body``. The failure has code ``invalid_request``.
    """
    supplied = parse_content_digest(header)
    calculated = hashlib.sha256(body).digest()
    if not hmac.compare_digest(supplied, calculated):
        raise invalid_request("Content-Digest does not match the request body")


def verify_p256_p1363(
    public_key: ec.EllipticCurvePublicKey,
    message: bytes,
    signature: bytes,
) -> None:
    """Verify a 64-byte, low-S IEEE P1363 ECDSA-P256-SHA256 signature.

    Args:
        public_key: P-256 verification key selected by trusted caller state.
        message: Exact signed bytes.
        signature: Exact 64-byte ``r || s`` P1363 signature.

    Returns:
        ``None`` after successful verification.

    Raises:
        ProtocolViolation: The key is not P-256, the signature encoding or scalars
            violate the low-S profile, or verification fails. The failure has code
            ``authentication_required``.
    """
    if not isinstance(public_key.curve, ec.SECP256R1):
        raise authentication_required("signature key is not P-256")
    if len(signature) != 64:
        raise authentication_required("signature is not a 64-byte P1363 value")
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")
    if not 0 < r < P256_ORDER or not 0 < s <= P256_ORDER // 2:
        raise authentication_required("signature scalar is outside the low-S P-256 profile")
    try:
        public_key.verify(
            encode_dss_signature(r, s),
            message,
            ec.ECDSA(hashes.SHA256()),
        )
    except InvalidSignature:
        raise authentication_required("P-256 signature verification failed") from None
