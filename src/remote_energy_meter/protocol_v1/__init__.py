"""Pure Python REM protocol-v1 parsing and verification primitives.

This package deliberately contains no listener, route handler, persistence, replay
state, authorization policy, or business operation.
"""

from remote_energy_meter.protocol_v1.canonical_json import JsonValue, parse_canonical_json
from remote_energy_meter.protocol_v1.crypto import (
    P256PublicJwk,
    parse_content_digest,
    parse_p256_public_jwk,
    render_content_digest,
    verify_content_digest,
    verify_p256_p1363,
)
from remote_energy_meter.protocol_v1.encoding import (
    TranscriptField,
    decode_base64url,
    decode_transcript,
    encode_base64url,
    encode_transcript,
)
from remote_energy_meter.protocol_v1.errors import ErrorCode, ProtocolViolation
from remote_energy_meter.protocol_v1.http_signatures import (
    RouteProfile,
    SignedRequest,
    VerificationKey,
    VerifiedRequestIdentity,
    route_profile,
    verify_signed_request,
)

__all__ = [
    "ErrorCode",
    "JsonValue",
    "P256PublicJwk",
    "ProtocolViolation",
    "RouteProfile",
    "SignedRequest",
    "TranscriptField",
    "VerificationKey",
    "VerifiedRequestIdentity",
    "decode_base64url",
    "decode_transcript",
    "encode_base64url",
    "encode_transcript",
    "parse_canonical_json",
    "parse_content_digest",
    "parse_p256_public_jwk",
    "render_content_digest",
    "route_profile",
    "verify_content_digest",
    "verify_p256_p1363",
    "verify_signed_request",
]
