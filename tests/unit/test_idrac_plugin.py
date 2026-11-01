"""Unit tests for the iDRAC read and connection validation operations (task 10.3).

The HTTP layer is a test double satisfying the plugin's own
:class:`~remote_energy_meter.plugins.idrac.plugin.RedfishSession` protocol, so every branch of the classification
taxonomy is exercised — including the ones a cooperating server can never produce on demand, such as a certificate
that fails chain validation. The double raises *real* aiohttp and :mod:`ssl` exceptions rather than stand-ins, because
the thing under test is precisely which exception type maps onto which
:class:`~remote_energy_meter.plugins.interface.FailureClassification`. Task 15.1 stands up a real Redfish server over
HTTPS for the transport itself.

Covered here: Requirement 4.3 (exactly one HTTPS request per read, bearing that entry's own HTTP Basic credentials),
Requirement 4.2 (one implementation, two entries differing only in parameters), Requirement 4.6 (401 and 403),
Requirement 4.7 (the request is abandoned at the timeout), Requirements 4.8 and 4.9 (the TLS flag reaches the request
and a validation failure is classified apart from a communication failure), Requirement 4.10 (the default chassis
power path), Requirement 4.12 (any other unsuccessful status or connect failure names the host), and Requirement 4.13
(validation is one request that succeeds exactly when a read would).
"""

from __future__ import annotations

import asyncio
import json
import math
import ssl
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from types import TracebackType
from typing import Any, Final

import aiohttp
import pytest
from aiohttp.client_reqrep import ConnectionKey
from pydantic import BaseModel

from remote_energy_meter.plugins.idrac.plugin import (
    AUTHENTICATION_STATUSES,
    PLUGIN_ID,
    IdracPlugin,
    RedfishResponse,
    RedfishSession,
    power_url,
)
from remote_energy_meter.plugins.idrac.schema import DEFAULT_POWER_PATH, IdracParams
from remote_energy_meter.plugins.interface import (
    PLUGIN_ID_MAX_LENGTH,
    FailureClassification,
    PowerReading,
    ReadFailure,
    ValidationResult,
)
from remote_energy_meter.plugins.registry import discover

DEVICE_ID: Final = "poweredge_a"
HOST: Final = "192.0.2.9"
USERNAME: Final = "root"
PASSWORD: Final = "s3cr3t-idrac-pw"
EPOCH: Final = datetime(2025, 1, 1, 12, 30, 45, tzinfo=UTC)

VALID_PARAMS: Final[dict[str, Any]] = {"host": HOST, "username": USERNAME, "password": PASSWORD}


# --------------------------------------------------------------------------------------------------
# payload helpers
# --------------------------------------------------------------------------------------------------


def idrac_body(watts: object = 224) -> dict[str, Any]:
    """A realistic iDRAC chassis power body carrying ``watts``."""
    return {
        "@odata.id": DEFAULT_POWER_PATH,
        "Id": "Power",
        "PowerControl": [{"MemberId": "PowerControl", "PowerConsumedWatts": watts, "PowerCapacityWatts": 1600}],
    }


# --------------------------------------------------------------------------------------------------
# the HTTP double
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Stall:
    """An outcome that never answers: the request hangs for ``seconds`` unless it is abandoned first."""

    seconds: float


@dataclass(slots=True)
class FakeResponse:
    """A response carrying a status and a body, plus an optional side effect run when the body is read."""

    status: int = 200
    body: bytes = b"{}"
    on_read: Callable[[], None] | None = None

    async def read(self) -> bytes:
        if self.on_read is not None:
            self.on_read()
        return self.body


def json_response(payload: object, *, status: int = 200, on_read: Callable[[], None] | None = None) -> FakeResponse:
    """A JSON response body, encoded as a server would send it."""
    return FakeResponse(status=status, body=json.dumps(payload).encode(), on_read=on_read)


type Outcome = FakeResponse | BaseException | Stall


class FakeRequest:
    """The context manager one ``get`` call returns, replaying one configured outcome."""

    __slots__ = ("_outcome",)

    def __init__(self, outcome: Outcome) -> None:
        self._outcome = outcome

    async def __aenter__(self) -> RedfishResponse:
        outcome = self._outcome
        if isinstance(outcome, Stall):
            await asyncio.sleep(outcome.seconds)
            pytest.fail("a stalled request was expected to be abandoned")
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


@dataclass(slots=True)
class RecordedRequest:
    """Everything one observed ``get`` call carried."""

    url: str
    auth: aiohttp.BasicAuth
    ssl: bool
    allow_redirects: bool
    timeout: aiohttp.ClientTimeout
    headers: Mapping[str, str]


@dataclass(slots=True)
class FakeSession:
    """A session that records every request and replays configured outcomes in order.

    Satisfies :class:`RedfishSession` structurally, which is checked by ``mypy --strict`` at every construction site:
    the double is held to the exact call the plugin makes, not to an arbitrary one.
    """

    outcomes: deque[Outcome] = field(default_factory=deque)
    requests: list[RecordedRequest] = field(default_factory=list)
    close_calls: int = 0
    _closed: bool = False

    @property
    def closed(self) -> bool:
        return self._closed

    async def close(self) -> None:
        self.close_calls += 1
        self._closed = True

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
    ) -> FakeRequest:
        self.requests.append(
            RecordedRequest(
                url=url,
                auth=auth,
                ssl=ssl,
                allow_redirects=allow_redirects,
                timeout=timeout,
                headers=headers,
            )
        )
        if not self.outcomes:
            return FakeRequest(AssertionError(f"no outcome configured for request {len(self.requests)}"))
        return FakeRequest(self.outcomes.popleft())


def session_with(*outcomes: Outcome) -> FakeSession:
    """A session that will answer with ``outcomes``, in order."""
    return FakeSession(outcomes=deque(outcomes))


def connection_key(*, verify_tls: bool = True) -> ConnectionKey:
    """The connector key aiohttp attaches to a connection error, needed to build a realistic instance."""
    return ConnectionKey(
        host=HOST,
        port=443,
        is_ssl=True,
        ssl=verify_tls,
        proxy=None,
        proxy_auth=None,
        proxy_headers_hash=None,
    )


def certificate_error(*, verify_tls: bool = True) -> aiohttp.ClientConnectorCertificateError:
    """What aiohttp raises when the presented chain does not validate."""
    return aiohttp.ClientConnectorCertificateError(
        connection_key(verify_tls=verify_tls),
        ssl.SSLCertVerificationError("self-signed certificate"),
    )


def connect_error() -> aiohttp.ClientConnectorError:
    """What aiohttp raises when the TCP connection cannot be established."""
    return aiohttp.ClientConnectorError(connection_key(), OSError(111, "Connection refused"))


# --------------------------------------------------------------------------------------------------
# plugin helpers
# --------------------------------------------------------------------------------------------------


def make_plugin(
    session: FakeSession,
    *,
    device_id: str = DEVICE_ID,
    clock: Callable[[], datetime] | None = None,
    **overrides: Any,
) -> IdracPlugin:
    """An iDRAC plugin bound to ``session``, with the given parameter overrides applied."""
    params = IdracParams(**{**VALID_PARAMS, **overrides})
    return IdracPlugin(
        device_id, params, clock=clock if clock is not None else lambda: EPOCH, session_factory=lambda: session
    )


async def read(plugin: IdracPlugin, timeout_s: float = 5.0) -> PowerReading | ReadFailure:
    """Take one reading."""
    return await plugin.read_power(timeout_s)


async def read_failure(plugin: IdracPlugin, timeout_s: float = 5.0) -> ReadFailure:
    """Take one reading, asserting it failed, and return the failure for further assertions."""
    outcome = await plugin.read_power(timeout_s)

    assert isinstance(outcome, ReadFailure), f"expected a failure, got {outcome!r}"
    return outcome


async def read_reading(plugin: IdracPlugin, timeout_s: float = 5.0) -> PowerReading:
    """Take one reading, asserting it succeeded, and return it for further assertions."""
    outcome = await plugin.read_power(timeout_s)

    assert isinstance(outcome, PowerReading), f"expected a reading, got {outcome!r}"
    return outcome


# --------------------------------------------------------------------------------------------------
# the interface elements (Requirement 3.1)
# --------------------------------------------------------------------------------------------------


def test_the_plugin_declares_the_interface_elements() -> None:
    """The registry keys on these four; an identifier or schema of the wrong kind would exclude the module."""
    assert IdracPlugin.plugin_id == PLUGIN_ID == "idrac"
    assert 0 < len(IdracPlugin.plugin_id) <= PLUGIN_ID_MAX_LENGTH
    assert IdracPlugin.config_model is IdracParams
    assert issubclass(IdracPlugin.config_model, BaseModel)
    assert callable(IdracPlugin.validate_connection)
    assert callable(IdracPlugin.read_power)


def test_the_plugin_is_discovered_by_walking_the_shipped_package() -> None:
    """Requirements 3.2, 10.3: presence in the package is registration, with no core edit and no register call."""
    result = discover()

    assert result.plugins.get(PLUGIN_ID) is IdracPlugin


def test_parameters_of_another_schema_are_refused_at_construction() -> None:
    """The registry turns a raising constructor into a rejection of one entry, so refusing loudly is safe."""

    class OtherParams(BaseModel):
        host: str

    with pytest.raises(TypeError, match="IdracParams"):
        IdracPlugin(DEVICE_ID, OtherParams(host=HOST))


# --------------------------------------------------------------------------------------------------
# the requested URL (Requirements 4.3, 4.10)
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("host", "path", "expected"),
    [
        ("192.0.2.9", "/redfish/v1/Chassis/1/Power", "https://192.0.2.9/redfish/v1/Chassis/1/Power"),
        ("idrac.example", "/a", "https://idrac.example/a"),
        ("192.0.2.9:8443", "/a", "https://192.0.2.9:8443/a"),
        ("  192.0.2.9  ", "/a", "https://192.0.2.9/a"),
        ("192.0.2.9/", "/a", "https://192.0.2.9/a"),
        ("https://192.0.2.9", "/a", "https://192.0.2.9/a"),
        ("HTTP://192.0.2.9", "/a", "https://192.0.2.9/a"),
        ("fd00::9", "/a", "https://[fd00::9]/a"),
        ("[fd00::9]:8443", "/a", "https://[fd00::9]:8443/a"),
        ("192.0.2.9", "redfish/v1", "https://192.0.2.9/redfish/v1"),
    ],
    ids=[
        "ipv4",
        "hostname",
        "explicit port",
        "surrounding whitespace",
        "trailing slash",
        "https prefix",
        "http prefix",
        "bare ipv6",
        "bracketed ipv6 with port",
        "relative path",
    ],
)
def test_the_power_url_is_always_one_https_url(host: str, path: str, expected: str) -> None:
    """Requirement 4.3 fixes the scheme, so a configured ``http://`` is stripped rather than obeyed."""
    assert power_url(host, path) == expected


def test_an_omitted_power_path_requests_the_default_chassis_resource() -> None:
    """Requirement 4.10, resolved through the schema so the read cannot re-derive it differently."""
    plugin = make_plugin(session_with())

    assert plugin.url == f"https://{HOST}{DEFAULT_POWER_PATH}"
    assert DEFAULT_POWER_PATH == "/redfish/v1/Chassis/System.Embedded.1/Power"


def test_a_configured_power_path_replaces_the_default() -> None:
    plugin = make_plugin(session_with(), power_path="/redfish/v1/Chassis/Custom.1/Power")

    assert plugin.url == f"https://{HOST}/redfish/v1/Chassis/Custom.1/Power"


# --------------------------------------------------------------------------------------------------
# a successful read (Requirements 4.3, 4.4)
# --------------------------------------------------------------------------------------------------


async def test_a_valid_response_yields_a_reading_for_this_device() -> None:
    plugin = make_plugin(session_with(json_response(idrac_body(224))))

    reading = await read_reading(plugin)

    assert reading.device_id == DEVICE_ID
    assert reading.watts == 224.0
    assert reading.measured_at == EPOCH


async def test_a_read_issues_exactly_one_request_to_the_configured_resource() -> None:
    """Requirement 4.3: one request per read, not a probe plus a read and not a redirect chain."""
    session = session_with(json_response(idrac_body()))
    plugin = make_plugin(session)

    await read_reading(plugin)

    assert len(session.requests) == 1
    assert session.requests[0].url == f"https://{HOST}{DEFAULT_POWER_PATH}"
    assert session.requests[0].allow_redirects is False


async def test_a_read_carries_the_http_basic_credentials_of_its_own_entry() -> None:
    """Requirement 4.3: the entry's own username and password, in a Basic authentication header."""
    session = session_with(json_response(idrac_body()))

    await read_reading(make_plugin(session))

    auth = session.requests[0].auth
    assert (auth.login, auth.password) == (USERNAME, PASSWORD)
    assert auth.encode().startswith("Basic ")


async def test_two_entries_of_the_same_family_use_their_own_credentials_and_hosts() -> None:
    """Requirement 4.2: both target servers are metered by this one implementation, sharing no state."""
    session_a = session_with(json_response(idrac_body(100)))
    session_b = session_with(json_response(idrac_body(200)))
    plugin_a = make_plugin(session_a, device_id="poweredge_a", host="192.0.2.9", password="pw-a")
    plugin_b = make_plugin(session_b, device_id="poweredge_b", host="192.0.2.20", password="pw-b")

    reading_a, reading_b = await asyncio.gather(read_reading(plugin_a), read_reading(plugin_b))

    assert (reading_a.device_id, reading_a.watts) == ("poweredge_a", 100.0)
    assert (reading_b.device_id, reading_b.watts) == ("poweredge_b", 200.0)
    assert session_a.requests[0].url.startswith("https://192.0.2.9/")
    assert session_b.requests[0].url.startswith("https://192.0.2.20/")
    assert session_a.requests[0].auth.password == "pw-a"
    assert session_b.requests[0].auth.password == "pw-b"


async def test_the_request_announces_that_it_wants_json() -> None:
    """Firmware that content-negotiates otherwise answers an HTML error page a parser cannot use."""
    session = session_with(json_response(idrac_body()))

    await read_reading(make_plugin(session))

    assert session.requests[0].headers["Accept"] == "application/json"


async def test_a_body_sent_without_a_json_content_type_is_still_read() -> None:
    """iDRAC firmware labels this resource inconsistently, so the body is decoded rather than content-negotiated."""
    session = session_with(FakeResponse(status=200, body=json.dumps(idrac_body(310)).encode()))

    assert (await read_reading(make_plugin(session))).watts == 310.0


# --------------------------------------------------------------------------------------------------
# the measurement timestamp (Requirement 4.4)
# --------------------------------------------------------------------------------------------------


async def test_the_timestamp_is_taken_when_the_response_arrives_not_when_it_was_requested() -> None:
    """Requirement 4.4: energy integration differences these timestamps, so the moment they mark matters."""
    elapsed = [0.0]

    def clock() -> datetime:
        return EPOCH + timedelta(seconds=elapsed[0])

    def on_read() -> None:
        elapsed[0] += 5.0

    session = session_with(json_response(idrac_body(), on_read=on_read))

    reading = await read_reading(make_plugin(session, clock=clock))

    assert reading.measured_at == EPOCH + timedelta(seconds=5)


async def test_the_clock_is_read_once_per_reading() -> None:
    calls = [0]

    def clock() -> datetime:
        calls[0] += 1
        return EPOCH

    plugin = make_plugin(session_with(json_response(idrac_body())), clock=clock)

    await read_reading(plugin)

    assert calls[0] == 1


async def test_a_naive_clock_reading_is_normalized_to_utc() -> None:
    """A naive timestamp would silently distort every kWh total downstream, so the contract is enforced here."""
    naive = EPOCH.replace(tzinfo=None)
    plugin = make_plugin(session_with(json_response(idrac_body())), clock=lambda: naive)

    reading = await read_reading(plugin)

    assert reading.measured_at.tzinfo is not None
    assert reading.measured_at.utcoffset() == timedelta(0)
    assert reading.measured_at == EPOCH


async def test_a_non_utc_clock_reading_is_converted_to_utc() -> None:
    elsewhere = EPOCH.astimezone(timezone(timedelta(hours=5, minutes=30)))
    plugin = make_plugin(session_with(json_response(idrac_body())), clock=lambda: elsewhere)

    reading = await read_reading(plugin)

    assert reading.measured_at.utcoffset() == timedelta(0)
    assert reading.measured_at == EPOCH


# --------------------------------------------------------------------------------------------------
# authentication (Requirement 4.6)
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", sorted(AUTHENTICATION_STATUSES))
async def test_an_unauthorized_or_forbidden_status_is_an_authentication_failure(status: int) -> None:
    """Requirement 4.6. The scheduler never retries this class, which is why it must not read as a communication one."""
    session = session_with(FakeResponse(status=status))

    failure = await read_failure(make_plugin(session))

    assert failure.classification is FailureClassification.AUTHENTICATION
    assert failure.device_id == DEVICE_ID
    assert HOST in failure.detail
    assert len(session.requests) == 1


async def test_an_authentication_failure_detail_carries_no_credential() -> None:
    """Requirement 8.2's structural half: the detail is built from the host and the status, never from the secret."""
    failure = await read_failure(make_plugin(session_with(FakeResponse(status=401))))

    assert PASSWORD not in failure.detail
    assert USERNAME not in failure.detail


async def test_a_username_http_basic_cannot_encode_is_an_authentication_failure() -> None:
    """A colon in a username makes Basic credentials unrepresentable; a raised error would read as a plugin defect."""
    session = session_with(json_response(idrac_body()))

    failure = await read_failure(make_plugin(session, username="root:admin"))

    assert failure.classification is FailureClassification.AUTHENTICATION
    assert not session.requests, "no request may go out without usable credentials"


# --------------------------------------------------------------------------------------------------
# other unsuccessful statuses and connect failures (Requirement 4.12)
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [301, 302, 400, 404, 405, 429, 500, 501, 503])
async def test_any_other_unsuccessful_status_is_a_communication_failure_naming_the_host(status: int) -> None:
    """Requirement 4.12. A 3xx counts: redirects are not followed, so it is one request that did not answer."""
    failure = await read_failure(make_plugin(session_with(FakeResponse(status=status))))

    assert failure.classification is FailureClassification.COMMUNICATION
    assert HOST in failure.detail
    assert str(status) in failure.detail


@pytest.mark.parametrize(
    "error",
    [
        connect_error(),
        aiohttp.ClientConnectionError("connection reset"),
        aiohttp.ClientPayloadError("truncated body"),
        aiohttp.ClientError("something aiohttp did not like"),
        OSError(113, "No route to host"),
        ConnectionResetError("peer went away"),
    ],
    ids=["connector", "connection", "payload", "client", "os", "reset"],
)
async def test_a_transport_failure_is_a_communication_failure_naming_the_host(error: BaseException) -> None:
    """Requirement 4.12: the connection could not be established or could not be read to the end."""
    failure = await read_failure(make_plugin(session_with(error)))

    assert failure.classification is FailureClassification.COMMUNICATION
    assert HOST in failure.detail


async def test_a_transport_failure_names_the_exception_type_in_its_detail() -> None:
    """Naming the type is what makes a refused connection distinguishable from a reset one in the log."""
    failure = await read_failure(make_plugin(session_with(connect_error())))

    assert "ClientConnectorError" in failure.detail


async def test_an_oversized_exception_text_is_bounded_in_the_detail() -> None:
    """The detail travels into a log message capped at 512 characters (Requirement 9.1)."""
    failure = await read_failure(make_plugin(session_with(aiohttp.ClientError("x" * 4096))))

    assert len(failure.detail) < 512


# --------------------------------------------------------------------------------------------------
# timeouts (Requirement 4.7)
# --------------------------------------------------------------------------------------------------


async def test_a_stalled_request_is_abandoned_at_the_timeout() -> None:
    """Requirement 4.7: the attempt is abandoned rather than left to hang until the transport gives up."""
    session = session_with(Stall(seconds=30.0))
    plugin = make_plugin(session)

    started = time.monotonic()
    failure = await read_failure(plugin, timeout_s=0.05)
    elapsed = time.monotonic() - started

    assert failure.classification is FailureClassification.TIMEOUT
    assert elapsed < 1.0, "the bound must be enforced by the plugin, not by the transport"
    assert "0.05" in failure.detail


@pytest.mark.parametrize(
    "error",
    [TimeoutError("total timeout"), aiohttp.ServerTimeoutError("no response")],
    ids=["asyncio", "aiohttp server timeout"],
)
async def test_a_transport_reported_timeout_is_a_timeout_failure(error: BaseException) -> None:
    """``ServerTimeoutError`` is an ``OSError`` too, so a communication clause ordered first would swallow it."""
    failure = await read_failure(make_plugin(session_with(error)))

    assert failure.classification is FailureClassification.TIMEOUT
    assert HOST in failure.detail


async def test_the_caller_supplied_timeout_reaches_the_request() -> None:
    """The scheduler owns the bound (Requirement 5.5); the plugin applies the value it is handed."""
    session = session_with(json_response(idrac_body()))

    await read_reading(make_plugin(session), timeout_s=7.5)

    assert session.requests[0].timeout.total == 7.5


@pytest.mark.parametrize("unusable", [0.0, -1.0, math.nan, math.inf])
async def test_an_unusable_timeout_falls_back_to_the_configured_request_timeout(unusable: float) -> None:
    """A bound of zero or a non-number cannot be applied; the entry's own configured value is the only fallback."""
    session = session_with(json_response(idrac_body()))

    await read_reading(make_plugin(session, request_timeout_s=12.0), timeout_s=unusable)

    assert session.requests[0].timeout.total == 12.0


async def test_cancellation_from_the_scheduler_is_not_swallowed() -> None:
    """Requirement 5.4 has the scheduler cancel an attempt; converting that into a failure would hide a shutdown."""
    plugin = make_plugin(session_with(Stall(seconds=30.0)))
    task = asyncio.ensure_future(plugin.read_power(30.0))
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


# --------------------------------------------------------------------------------------------------
# TLS (Requirements 4.8, 4.9)
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("verify_tls", [True, False])
async def test_the_tls_verification_flag_reaches_the_request(verify_tls: bool) -> None:
    """Requirement 4.8: ``False`` is what makes a factory iDRAC's self-signed certificate acceptable."""
    session = session_with(json_response(idrac_body()))

    await read_reading(make_plugin(session, verify_tls=verify_tls))

    assert session.requests[0].ssl is verify_tls


async def test_a_self_signed_certificate_is_accepted_when_verification_is_disabled() -> None:
    """Requirement 4.8: with the flag off, the response is read and a reading is returned."""
    session = session_with(json_response(idrac_body(275)))

    reading = await read_reading(make_plugin(session, verify_tls=False))

    assert reading.watts == 275.0
    assert session.requests[0].ssl is False


@pytest.mark.parametrize(
    "error",
    [
        certificate_error(),
        aiohttp.ClientConnectorSSLError(connection_key(), OSError("handshake failed")),
        ssl.SSLCertVerificationError("unable to get local issuer certificate"),
        ssl.SSLError("wrong version number"),
    ],
    ids=["certificate", "ssl connector", "verification", "ssl"],
)
async def test_a_tls_failure_is_a_tls_validation_failure_while_verification_is_on(error: BaseException) -> None:
    """Requirement 4.9: distinct from a communication failure, because the fix is a certificate, not a network."""
    failure = await read_failure(make_plugin(session_with(error), verify_tls=True))

    assert failure.classification is FailureClassification.TLS_VALIDATION
    assert HOST in failure.detail


async def test_a_tls_failure_with_verification_disabled_is_a_communication_failure() -> None:
    """No chain was being validated, so the same exception can only mean the connection failed (Requirement 4.12)."""
    failure = await read_failure(make_plugin(session_with(certificate_error(verify_tls=False)), verify_tls=False))

    assert failure.classification is FailureClassification.COMMUNICATION
    assert HOST in failure.detail


# --------------------------------------------------------------------------------------------------
# response bodies: delegated to the pure extractor (Requirements 4.5, 4.11)
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b"", FailureClassification.RESPONSE_FORMAT),
        (b"<html>500</html>", FailureClassification.RESPONSE_FORMAT),
        (b'{"PowerControl": [', FailureClassification.RESPONSE_FORMAT),
        (b"\xff\xfe\x00garbage", FailureClassification.RESPONSE_FORMAT),
    ],
    ids=["empty", "html", "truncated json", "undecodable bytes"],
)
async def test_a_body_that_is_not_json_is_a_response_format_failure(
    body: bytes, expected: FailureClassification
) -> None:
    """A body carrying no power value is the absence Requirement 4.5 describes, whatever made it unreadable."""
    failure = await read_failure(make_plugin(session_with(FakeResponse(status=200, body=body))))

    assert failure.classification is expected
    assert DEFAULT_POWER_PATH in failure.detail


async def test_a_response_without_a_power_member_is_a_response_format_failure() -> None:
    """Requirement 4.5, decided by the pure extractor and passed through unchanged."""
    failure = await read_failure(make_plugin(session_with(json_response({"PowerControl": [{"MemberId": "x"}]}))))

    assert failure.classification is FailureClassification.RESPONSE_FORMAT
    assert DEFAULT_POWER_PATH in failure.detail


@pytest.mark.parametrize("watts", ["n/a", None, 25000, -1], ids=["text", "null", "too large", "negative"])
async def test_an_unusable_power_value_is_an_invalid_measurement(watts: object) -> None:
    """Requirement 4.11, likewise decided once by the extractor rather than restated here."""
    failure = await read_failure(make_plugin(session_with(json_response(idrac_body(watts)))))

    assert failure.classification is FailureClassification.INVALID_MEASUREMENT


async def test_the_failure_of_an_unusable_body_is_attributed_to_this_device() -> None:
    failure = await read_failure(make_plugin(session_with(json_response({})), device_id="poweredge_b"))

    assert failure.device_id == "poweredge_b"


async def test_no_unusable_response_raises() -> None:
    """A raised exception would be classified generically as a plugin failure and lose the distinction."""
    for outcome in (
        FakeResponse(status=500),
        FakeResponse(status=200, body=b"nope"),
        json_response(idrac_body("n/a")),
        connect_error(),
        certificate_error(),
        TimeoutError(),
    ):
        assert isinstance(await read(make_plugin(session_with(outcome))), ReadFailure)


# --------------------------------------------------------------------------------------------------
# connection validation (Requirement 4.13)
# --------------------------------------------------------------------------------------------------


async def test_validation_succeeds_on_a_payload_a_read_would_accept() -> None:
    session = session_with(json_response(idrac_body(224)))

    result = await make_plugin(session).validate_connection(5.0)

    assert result == ValidationResult.success()
    assert len(session.requests) == 1


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (json_response(idrac_body(224)), None),
        (json_response(idrac_body(0)), None),
        (FakeResponse(status=401), FailureClassification.AUTHENTICATION),
        (FakeResponse(status=500), FailureClassification.COMMUNICATION),
        (json_response({}), FailureClassification.RESPONSE_FORMAT),
        (json_response(idrac_body(25000)), FailureClassification.INVALID_MEASUREMENT),
        (TimeoutError(), FailureClassification.TIMEOUT),
        (certificate_error(), FailureClassification.TLS_VALIDATION),
    ],
    ids=["valid", "zero watts", "unauthorized", "server error", "no member", "out of range", "timeout", "certificate"],
)
async def test_validation_agrees_with_reading_and_issues_one_request(
    outcome: Outcome, expected: FailureClassification | None
) -> None:
    """Requirement 4.13: one request, and success exactly when the same payload would yield a reading."""
    validation_session = session_with(outcome)
    read_session = session_with(outcome)

    validation = await make_plugin(validation_session).validate_connection(5.0)
    read_outcome = await read(make_plugin(read_session))

    assert validation.ok is isinstance(read_outcome, PowerReading)
    assert validation.classification == expected
    assert len(validation_session.requests) == 1


async def test_validation_uses_the_timeout_it_is_given() -> None:
    session = session_with(json_response(idrac_body()))

    await make_plugin(session).validate_connection(3.0)

    assert session.requests[0].timeout.total == 3.0


async def test_a_stalled_validation_is_abandoned_at_the_timeout() -> None:
    plugin = make_plugin(session_with(Stall(seconds=30.0)))

    result = await plugin.validate_connection(0.05)

    assert result == ValidationResult.failure(FailureClassification.TIMEOUT)


# --------------------------------------------------------------------------------------------------
# session lifecycle (Requirement 3.6)
# --------------------------------------------------------------------------------------------------


def test_no_session_is_created_at_construction() -> None:
    """aiohttp binds a session to the running loop, and 50 instances are built before any of them reads."""
    created = [0]

    def factory() -> RedfishSession:
        created[0] += 1
        return FakeSession()

    IdracPlugin(DEVICE_ID, IdracParams(**VALID_PARAMS), session_factory=factory)

    assert created[0] == 0


async def test_one_session_serves_every_read_of_one_device() -> None:
    """A fresh session per read would discard the connection pool it exists to provide."""
    created: list[FakeSession] = []

    def factory() -> RedfishSession:
        session = session_with(json_response(idrac_body()), json_response(idrac_body(310)))
        created.append(session)
        return session

    plugin = IdracPlugin(DEVICE_ID, IdracParams(**VALID_PARAMS), clock=lambda: EPOCH, session_factory=factory)

    first = await read_reading(plugin)
    second = await read_reading(plugin)

    assert (first.watts, second.watts) == (224.0, 310.0)
    assert len(created) == 1
    assert len(created[0].requests) == 2


async def test_closing_releases_the_session() -> None:
    session = session_with(json_response(idrac_body()))
    plugin = make_plugin(session)
    await read_reading(plugin)

    await plugin.aclose()

    assert session.close_calls == 1
    assert plugin.closed


async def test_closing_is_idempotent_and_safe_before_any_read() -> None:
    session = session_with()
    plugin = make_plugin(session)

    await plugin.aclose()
    await plugin.close()

    assert session.close_calls == 0, "a session that was never created cannot be closed"
    assert plugin.closed


async def test_closing_twice_closes_the_session_once() -> None:
    session = session_with(json_response(idrac_body()))
    plugin = make_plugin(session)
    await read_reading(plugin)

    await plugin.aclose()
    await plugin.aclose()

    assert session.close_calls == 1


async def test_a_read_after_closing_fails_without_issuing_a_request() -> None:
    """Shutdown cancels the scheduler before closing plugins, so a read here means a bug — reported, not raised."""
    session = session_with(json_response(idrac_body()))
    plugin = make_plugin(session)
    await plugin.aclose()

    failure = await read_failure(plugin)

    assert failure.classification is FailureClassification.COMMUNICATION
    assert not session.requests


async def test_a_failing_device_leaves_another_devices_session_untouched() -> None:
    """Requirement 3.6: one instance per entry, so one hung or refused socket is one device's problem."""
    failing = session_with(connect_error())
    healthy = session_with(json_response(idrac_body(180)))
    plugin_a = make_plugin(failing, device_id="poweredge_a")
    plugin_b = make_plugin(healthy, device_id="poweredge_b")

    outcome_a, outcome_b = await asyncio.gather(read(plugin_a), read(plugin_b))

    assert isinstance(outcome_a, ReadFailure)
    assert isinstance(outcome_b, PowerReading)
    assert not healthy.closed
    assert len(healthy.requests) == 1


# --------------------------------------------------------------------------------------------------
# credentials never leak (Requirements 8.2, 8.3)
# --------------------------------------------------------------------------------------------------


def test_the_representation_carries_no_credential() -> None:
    plugin = make_plugin(session_with())

    rendered = repr(plugin)

    assert PASSWORD not in rendered
    assert DEVICE_ID in rendered


@pytest.mark.parametrize(
    "outcome",
    [
        FakeResponse(status=401),
        FakeResponse(status=500),
        FakeResponse(status=200, body=b"nope"),
        json_response(idrac_body("n/a")),
        connect_error(),
        certificate_error(),
        TimeoutError(),
    ],
    ids=["unauthorized", "server error", "not json", "invalid value", "connector", "certificate", "timeout"],
)
async def test_no_failure_detail_carries_a_credential(outcome: Outcome) -> None:
    """The detail reaches the log stream, so it is built from the host and the path and never from the secret."""
    failure = await read_failure(make_plugin(session_with(outcome), password=PASSWORD))

    assert PASSWORD not in failure.detail
