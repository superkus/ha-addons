"""The iDRAC device plugin: one HTTPS Redfish request per reading.

This module is the I/O shell around :mod:`remote_energy_meter.plugins.idrac.redfish`. It owns the transport — URL,
credentials, TLS mode, timeout, session lifetime — and delegates every judgement about the *body* to
:func:`~remote_energy_meter.plugins.idrac.redfish.extract_watts`, which is pure. So the response-shape rules of
Requirements 4.4, 4.5, and 4.11 are stated once and cannot drift between the read operation and the connection
validation operation (Requirement 4.13): both call the same private read.

Presence in ``remote_energy_meter.plugins`` is registration, so nothing here registers anything. The registry finds
:class:`IdracPlugin` by walking the package (Requirements 3.2, 10.3).

## Decisions worth knowing about

**Exactly one HTTP request per read (Requirement 4.3).** ``allow_redirects=False`` is what makes that literally true
rather than nearly true: a Redfish resource has no business redirecting, and following one would put a second request
on the wire under a single ``read_power`` call. A 3xx is therefore reported as a communication failure like any other
unsuccessful status.

**One session per instance (Requirement 3.6).** The registry creates one plugin instance per ``Device_Entry``, and
each instance lazily creates its own :class:`aiohttp.ClientSession`, hence its own connection pool and its own TLS
context. That is what keeps a hung socket on one iDRAC from occupying the other's connection slot. The session is
created inside a coroutine, never at construction time, because aiohttp binds a session to the running loop.

**The caller owns the timeout.** ``read_power`` and ``validate_connection`` bound one request by the value they are
handed; they do not consult ``request_timeout_s`` unless that value is unusable. Retry counts, retry delays, and poll
intervals stay with the Poll_Scheduler (Requirement 5.5), which is also the component that reads
``request_timeout_s`` in order to decide what to hand down (Requirement 5.8). The bound is applied twice on purpose —
aiohttp's own total timeout plus an outer :func:`asyncio.timeout` — because the inner one is only as trustworthy as
the transport, and Requirement 4.7 has to hold regardless.

**An SSL failure is classified by what verification was asked to do.** With ``verify_tls`` true, a failed handshake
means the certificate did not validate, which is Requirement 4.9's TLS validation failure. With ``verify_tls`` false,
no chain was being validated, so the same exception can only mean the connection failed, which is Requirement 4.12's
communication failure. Collapsing both onto ``TLS_VALIDATION`` would tell an administrator who explicitly disabled
verification to go and fix a certificate.

**Nothing here raises, and nothing here logs.** Every outcome is a returned :class:`ReadFailure`, because an exception
escaping to the scheduler is classified generically as a plugin failure (Requirement 3.7) and loses the distinction
Requirements 4.5, 4.6, 4.11, and 4.12 draw. Failure details name the host or the requested resource path and never a
credential: plaintext leaves ``SecretStr`` at exactly one point in this module, where the HTTP Basic header is built.
"""

from __future__ import annotations

import asyncio
import json
import math
import ssl
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import ClassVar, Final, Protocol

import aiohttp
from pydantic import BaseModel

from remote_energy_meter.plugins.idrac.redfish import extract_watts
from remote_energy_meter.plugins.idrac.schema import IdracParams
from remote_energy_meter.plugins.interface import (
    FailureClassification,
    PowerReading,
    ReadFailure,
    ReadResult,
    ValidationResult,
)

__all__ = [
    "AUTHENTICATION_STATUSES",
    "MAX_RESPONSE_BYTES",
    "PLUGIN_ID",
    "REQUEST_HEADERS",
    "IdracPlugin",
    "RedfishResponse",
    "RedfishSession",
    "power_url",
]

# --------------------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------------------

#: The identifier a ``Device_Entry`` names to be metered by this plugin, matched byte-for-byte and case-sensitively
#: (Requirement 3.3).
PLUGIN_ID: Final = "idrac"

#: Requirement 4.6: the two statuses that mean the credentials were refused. Never retried by the scheduler, because
#: repeating a wrong credential walks toward an iDRAC account lockout.
AUTHENTICATION_STATUSES: Final = frozenset({401, 403})

#: Redfish speaks JSON; saying so keeps firmware that content-negotiates from answering with an HTML error page.
REQUEST_HEADERS: Final[dict[str, str]] = {"Accept": "application/json"}

#: The statuses from which a body is worth reading. Everything else is a communication failure (Requirement 4.12).
_SUCCESS_STATUSES: Final = range(200, 300)

#: How much of an exception's text reaches a failure detail. The detail travels into a log message capped at 512
#: characters (Requirement 9.1), and an aiohttp connection error can carry a long chain of causes.
_MAX_DETAIL_CHARS: Final = 160
#: The chassis Power resource is a few KiB; anything past this is not a Redfish answer, and an endpoint
#: (or a man-in-the-middle, when ``verify_tls`` is off) must not be able to fill memory for ``timeout_s``.
MAX_RESPONSE_BYTES: Final = 1_048_576

#: Stripped from a configured host so that a host written as ``https://192.0.2.9`` still yields one HTTPS URL rather
#: than a doubled scheme. Requirement 4.3 fixes the scheme, so ``http://`` is stripped too rather than honoured.
_SCHEME_PREFIXES: Final = ("https://", "http://")


# --------------------------------------------------------------------------------------------------
# The transport surface this plugin uses
# --------------------------------------------------------------------------------------------------


class RedfishResponse(Protocol):
    """The part of an HTTP response this plugin reads.

    Narrow on purpose: a status and a body. :class:`aiohttp.ClientResponse` satisfies it, and so does a test double,
    which is what lets every classification branch be exercised without a TLS stack.
    """

    @property
    def status(self) -> int:
        """The HTTP status of the response."""

    @property
    def content_length(self) -> int | None:
        """The declared ``Content-Length``, or ``None`` when the server sent none."""

    async def read(self) -> bytes:
        """The full response body."""
        ...


class RedfishSession(Protocol):
    """The part of an HTTP client session this plugin uses.

    ``get`` declares exactly the arguments this plugin passes, which is what makes :class:`aiohttp.ClientSession`
    satisfy the protocol structurally — aiohttp types its own keyword set as a ``TypedDict``, so a looser
    ``**kwargs: Any`` here would *not* match. The narrow signature also means a test double is checked against the
    call this plugin actually makes rather than against anything at all.
    """

    @property
    def closed(self) -> bool:
        """Whether the session has been closed."""

    async def close(self) -> None:
        """Release the session's connections."""
        ...

    def get(
        self,
        url: str,
        /,
        *,
        auth: aiohttp.BasicAuth,
        ssl: bool,
        allow_redirects: bool,
        timeout: aiohttp.ClientTimeout,
        headers: Mapping[str, str],
    ) -> AbstractAsyncContextManager[RedfishResponse]:
        """Issue one GET request, yielding the response for the duration of the context."""
        ...


def _default_session() -> RedfishSession:
    """A fresh session owned by exactly one plugin instance (Requirement 3.6).

    Called from inside a coroutine, so the session binds to the running loop.
    """
    return aiohttp.ClientSession()


def _utc_now() -> datetime:
    """The current instant as a timezone-aware UTC value."""
    return datetime.now(tz=UTC)


# --------------------------------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------------------------------


def _bracket_ipv6(authority: str) -> str:
    """Wrap a bare IPv6 literal in brackets so it can carry a port and terminate a URL authority.

    Two colons or more means a literal address, because a ``host:port`` authority has exactly one. An already
    bracketed value is left alone.
    """
    if authority.startswith("["):
        return authority
    if authority.count(":") >= 2:
        return f"[{authority}]"
    return authority


def power_url(host: str, power_path: str) -> str:
    """The absolute HTTPS URL of one iDRAC's Redfish power resource.

    Pure, so the URL a device is polled at can be asserted without issuing a request.

    :param host: the configured iDRAC management address. A hostname, an IPv4 literal, a bare IPv6 literal, any of
        those with a port, and any of those written with a scheme are all accepted.
    :param power_path: the resource path, already resolved through
        :attr:`~remote_energy_meter.plugins.idrac.schema.IdracParams.resolved_power_path` so Requirement 4.10's
        default is applied in one place.
    :returns: an ``https://`` URL. The scheme is fixed by Requirement 4.3 and is not configurable: a configured
        ``http://`` prefix is stripped rather than obeyed, because sending iDRAC credentials in the clear is not a
        choice this addon offers.
    """
    authority = host.strip()
    for prefix in _SCHEME_PREFIXES:
        if authority.lower().startswith(prefix):
            authority = authority[len(prefix) :]
            break
    authority = _bracket_ipv6(authority.rstrip("/"))
    path = power_path if power_path.startswith("/") else f"/{power_path}"
    return f"https://{authority}{path}"


def _brief(exc: BaseException) -> str:
    """Render an exception for a failure detail, bounded in length and named by type."""
    text = f"{type(exc).__name__}: {exc}".strip().rstrip(":")
    if len(text) > _MAX_DETAIL_CHARS:
        return f"{text[:_MAX_DETAIL_CHARS]}..."
    return text


def _as_utc(moment: datetime) -> datetime:
    """Normalize a clock reading to a timezone-aware UTC value.

    Requirement 4.4 makes the timestamp's zone load-bearing: the Energy_Accumulator differences these values, so a
    naive or non-UTC timestamp silently distorts every kWh total downstream. Normalizing here means an injected clock
    cannot break that contract, whatever it returns.
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


# --------------------------------------------------------------------------------------------------
# The plugin
# --------------------------------------------------------------------------------------------------


class IdracPlugin:
    """Reads instantaneous power from one Dell PowerEdge chassis through iDRAC Redfish.

    One instance serves one ``Device_Entry``. Requirement 4.2 has both target servers metered by this same class with
    entries that differ only in connection parameters, so nothing about a particular server is baked in here and no
    state is shared between instances.

    The clock and the session factory are injectable so that the timestamp contract of Requirement 4.4 and every
    failure classification of Requirements 4.6, 4.7, 4.9, and 4.12 can be exercised against a test double, leaving the
    real TLS stack to the integration layer.
    """

    #: Requirement 3.1: the identifier the registry keys this plugin by.
    plugin_id: ClassVar[str] = PLUGIN_ID

    #: Requirement 3.1: the published connection parameter schema (Requirement 4.1).
    config_model: ClassVar[type[BaseModel]] = IdracParams

    __slots__ = ("_clock", "_closed", "_device_id", "_params", "_session", "_session_factory", "_url")

    def __init__(
        self,
        device_id: str,
        params: BaseModel,
        *,
        clock: Callable[[], datetime] | None = None,
        session_factory: Callable[[], RedfishSession] | None = None,
    ) -> None:
        """Bind this instance to one ``Device_Entry``.

        :param device_id: the entry identifier, carried on every reading and every failure so the scheduler, the
            diagnostics layer, and the publisher can attribute them.
        :param params: an :class:`~remote_energy_meter.plugins.idrac.schema.IdracParams` instance. The signature is
            widened to ``BaseModel`` to match the ``Plugin_Interface`` constructor; anything else raises
            :class:`TypeError`, which the registry turns into a rejection of this one entry rather than a crash.
        :param clock: source of the measurement timestamp, read at response receipt. Defaults to the UTC wall clock.
        :param session_factory: source of the HTTP session, called at most once per instance while the instance is
            open. Defaults to a fresh :class:`aiohttp.ClientSession`.
        """
        if not isinstance(params, IdracParams):
            raise TypeError(
                f"{type(self).__name__} requires {IdracParams.__name__} parameters, got {type(params).__name__}"
            )
        self._device_id = device_id
        self._params = params
        self._url = power_url(params.host, params.resolved_power_path)
        self._clock: Callable[[], datetime] = clock if clock is not None else _utc_now
        self._session_factory: Callable[[], RedfishSession] = (
            session_factory if session_factory is not None else _default_session
        )
        self._session: RedfishSession | None = None
        self._closed = False

    # -- introspection -----------------------------------------------------------------------------

    @property
    def device_id(self) -> str:
        """The identifier of the ``Device_Entry`` this instance meters."""
        return self._device_id

    @property
    def url(self) -> str:
        """The absolute HTTPS URL every read of this device requests."""
        return self._url

    @property
    def closed(self) -> bool:
        """Whether :meth:`aclose` has been called."""
        return self._closed

    def __repr__(self) -> str:
        """A representation carrying no credential material."""
        return f"{type(self).__name__}(device_id={self._device_id!r}, url={self._url!r})"

    # -- the interface operations -------------------------------------------------------------------

    async def read_power(self, timeout_s: float) -> ReadResult:
        """Take one power reading, issuing exactly one HTTPS request (Requirement 4.3).

        :param timeout_s: the bound on that single request, in seconds (Requirement 4.7). A value that is not a
            positive finite number falls back to the entry's configured ``request_timeout_s``.
        :returns: a :class:`PowerReading` when the response carries a numeric value within 0 to 10000 W
            (Requirement 4.4), otherwise a classified :class:`ReadFailure`. Never raises, apart from
            :class:`asyncio.CancelledError` propagating to the scheduler that cancelled the attempt.
        """
        return await self._read_once(timeout_s)

    async def validate_connection(self, timeout_s: float) -> ValidationResult:
        """Check reachability once, succeeding only on a payload a read would accept (Requirement 4.13).

        Deliberately implemented as the same single request as :meth:`read_power`, with the reading discarded. Any
        other implementation could disagree with the read about what counts as usable, which is the one thing this
        operation exists to rule out. Its result is a diagnostic and never gates retention (Requirement 3.10).
        """
        outcome = await self._read_once(timeout_s)
        if isinstance(outcome, ReadFailure):
            return ValidationResult.failure(outcome.classification)
        return ValidationResult.success()

    # -- lifecycle ---------------------------------------------------------------------------------

    async def aclose(self) -> None:
        """Release this instance's session. Idempotent, and safe on an instance that never read anything.

        The instance stays closed afterwards: a read on a closed instance returns a communication failure rather than
        quietly opening a fresh socket that the shutdown sequence has already stopped waiting for.
        """
        self._closed = True
        session = self._session
        self._session = None
        if session is not None and not session.closed:
            await session.close()

    async def close(self) -> None:
        """Alias of :meth:`aclose`, for callers that expect aiohttp's name."""
        await self.aclose()

    # -- internals ---------------------------------------------------------------------------------

    def _failure(self, classification: FailureClassification, detail: str) -> ReadFailure:
        """Build a failure attributed to this device. ``detail`` never carries credential material."""
        return ReadFailure(device_id=self._device_id, classification=classification, detail=detail)

    def _effective_timeout(self, timeout_s: float) -> float:
        """The bound to apply to one request: the caller's value when usable, the configured one otherwise."""
        if math.isfinite(timeout_s) and timeout_s > 0:
            return float(timeout_s)
        return self._params.request_timeout_s

    def _auth(self) -> aiohttp.BasicAuth | ReadFailure:
        """The HTTP Basic credentials of *this* entry (Requirement 4.3).

        The only place in this module where plaintext leaves ``SecretStr``. A username aiohttp cannot encode is
        reported as an authentication failure rather than raised, and the detail names the host, never the value.
        """
        try:
            auth = aiohttp.BasicAuth(
                login=self._params.username.get_secret_value(),
                password=self._params.password.get_secret_value(),
            )
        except ValueError as exc:
            return self._failure(
                FailureClassification.AUTHENTICATION,
                f"the configured credentials for {self._params.host} cannot be sent as HTTP Basic ({_brief(exc)})",
            )
        else:
            return auth

    def _ensure_session(self) -> RedfishSession:
        """This instance's session, created on first use."""
        session = self._session
        if session is None or session.closed:
            session = self._session_factory()
            self._session = session
        return session

    async def _read_once(self, timeout_s: float) -> ReadResult:
        """One request, one outcome. Shared by the read and the validation operations (Requirement 4.13)."""
        if self._closed:
            return self._failure(
                FailureClassification.COMMUNICATION,
                f"{self._params.host} was not contacted: this plugin instance is closed",
            )
        auth = self._auth()
        if isinstance(auth, ReadFailure):
            return auth
        fetched = await self._fetch(auth, self._effective_timeout(timeout_s))
        if isinstance(fetched, ReadFailure):
            return fetched
        body, received_at = fetched
        return self._reading_from(body, received_at)

    async def _fetch(self, auth: aiohttp.BasicAuth, timeout_s: float) -> tuple[bytes, datetime] | ReadFailure:
        """Issue the one request and return its body with the receipt timestamp, or a classified failure.

        The clock is read once the body has arrived, so the timestamp is the moment the response was received rather
        than the moment the request was issued (Requirement 4.4) and never precedes the request.
        """
        host = self._params.host
        session = self._ensure_session()
        try:
            async with (
                asyncio.timeout(timeout_s),
                session.get(
                    self._url,
                    auth=auth,
                    # Requirements 4.8, 4.9: ``False`` accepts the self-signed certificate a factory iDRAC presents,
                    # ``True`` validates the chain. The flag maps onto aiohttp's argument directly.
                    ssl=self._params.verify_tls,
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=timeout_s),
                    headers=REQUEST_HEADERS,
                ) as response,
            ):
                status = response.status
                if status in AUTHENTICATION_STATUSES:
                    return self._failure(
                        FailureClassification.AUTHENTICATION,
                        f"{host} refused the configured credentials with HTTP {status}",
                    )
                if status not in _SUCCESS_STATUSES:
                    return self._failure(
                        FailureClassification.COMMUNICATION,
                        f"{host} answered HTTP {status} for {self._params.resolved_power_path}",
                    )
                # Refuse before reading when the server is honest about an oversized body, and after reading
                # when it is not: either way nothing larger than the cap reaches the parser or stays in memory.
                declared = response.content_length
                if declared is not None and declared > MAX_RESPONSE_BYTES:
                    return self._oversized(host, declared)
                body = await response.read()
                if len(body) > MAX_RESPONSE_BYTES:
                    return self._oversized(host, len(body))
                received_at = self._clock()
        except TimeoutError:
            # Both aiohttp's own total timeout and the outer bound land here; ``ServerTimeoutError`` is an
            # ``OSError`` as well, so this clause has to precede the communication ones.
            return self._failure(
                FailureClassification.TIMEOUT,
                f"{host} did not answer within {timeout_s:g}s",
            )
        except (aiohttp.ClientSSLError, ssl.SSLError) as exc:
            return self._tls_failure(exc)
        except (aiohttp.ClientError, OSError) as exc:
            return self._failure(
                FailureClassification.COMMUNICATION,
                f"{host} could not be read: {_brief(exc)}",
            )
        else:
            return body, received_at

    def _oversized(self, host: str, size: int) -> ReadFailure:
        return self._failure(
            FailureClassification.RESPONSE_FORMAT,
            f"{host} sent a {size}-byte response for {self._params.resolved_power_path}; "
            f"the limit is {MAX_RESPONSE_BYTES} bytes",
        )

    def _tls_failure(self, exc: BaseException) -> ReadFailure:
        """Classify a TLS-layer failure by what verification was asked to do.

        With verification on, the handshake failing means the certificate did not validate (Requirement 4.9). With it
        off, no chain was being checked, so the same exception can only mean the connection failed
        (Requirement 4.12) — and telling an administrator who disabled verification to fix a certificate would send
        them after the wrong thing.
        """
        host = self._params.host
        if self._params.verify_tls:
            return self._failure(
                FailureClassification.TLS_VALIDATION,
                f"{host} presented a TLS certificate that failed validation: {_brief(exc)}",
            )
        return self._failure(
            FailureClassification.COMMUNICATION,
            f"the TLS connection to {host} failed with certificate verification disabled: {_brief(exc)}",
        )

    def _reading_from(self, body: bytes, received_at: datetime) -> ReadResult:
        """Turn a received body into a reading, delegating every shape judgement to the pure extractor."""
        path = self._params.resolved_power_path
        try:
            payload: object = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            # A body that is not JSON carries no power value, which is the same absence Requirement 4.5 describes.
            return self._failure(
                FailureClassification.RESPONSE_FORMAT,
                f"{path} did not return a JSON body: {_brief(exc)}",
            )
        watts = extract_watts(payload, device_id=self._device_id, power_path=path)
        if isinstance(watts, ReadFailure):
            return watts
        return PowerReading(device_id=self._device_id, watts=watts, measured_at=_as_utc(received_at))
