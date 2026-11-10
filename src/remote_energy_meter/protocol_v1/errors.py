"""Stable, input-independent failures for REM protocol-v1 primitives."""

from __future__ import annotations

from typing import Final, Literal

type ErrorCode = Literal[
    "invalid_request",
    "authentication_required",
    "authorization_denied",
    "not_found",
    "method_not_allowed",
    "request_timeout",
    "conflict",
    "snapshot_required",
    "stale_snapshot",
    "operation_result_expired",
    "request_body_too_large",
    "target_too_long",
    "unsupported_media_type",
    "rate_limited",
    "headers_too_large",
    "internal_error",
    "overloaded",
]

ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "invalid_request",
        "authentication_required",
        "authorization_denied",
        "not_found",
        "method_not_allowed",
        "request_timeout",
        "conflict",
        "snapshot_required",
        "stale_snapshot",
        "operation_result_expired",
        "request_body_too_large",
        "target_too_long",
        "unsupported_media_type",
        "rate_limited",
        "headers_too_large",
        "internal_error",
        "overloaded",
    }
)


class ProtocolViolation(ValueError):  # noqa: N818
    """A protocol rejection with a stable RFC 9457 machine code.

    Args:
        code: One of the closed protocol error codes.
        message: A fixed developer diagnostic that contains no untrusted input.

    Raises:
        ValueError: ``code`` is outside the closed protocol error set.

    Messages are fixed diagnostics for developers. They never include request input,
    identities, key material, signatures, or body bytes.
    """

    code: ErrorCode

    def __init__(self, code: ErrorCode, message: str) -> None:
        if code not in ERROR_CODES:
            raise ValueError("unknown protocol error code")
        self.code = code
        super().__init__(message)


def invalid_request(message: str) -> ProtocolViolation:
    """Build a classified malformed-input failure."""
    return ProtocolViolation("invalid_request", message)


def authentication_required(message: str) -> ProtocolViolation:
    """Build a classified authentication failure."""
    return ProtocolViolation("authentication_required", message)
