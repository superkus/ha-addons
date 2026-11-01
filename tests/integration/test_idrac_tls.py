"""Real HTTPS integration coverage for the iDRAC TLS verification setting.

The request probe uses :class:`aiohttp.ClientSession` with ``TraceConfig`` rather
than a session double. Both branches therefore traverse aiohttp's real connector and
Python's SSL stack against :class:`tests.fakes.redfish_server.FakeRedfishServer` on
loopback.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest
from pydantic import SecretStr

from remote_energy_meter.plugins.idrac.plugin import IdracPlugin, RedfishSession
from remote_energy_meter.plugins.idrac.schema import IdracParams
from remote_energy_meter.plugins.interface import FailureClassification, PowerReading, ReadFailure
from tests.fakes.redfish_server import FakeRedfishServer, RecordedRequest

pytestmark = pytest.mark.integration

_DEVICE_USERNAME = "integration-user"
_DEVICE_PASSWORD = "integration-password"
_REQUEST_TIMEOUT_S = 2.0


def _real_session_factory(
    attempted_urls: list[str],
    created_sessions: list[aiohttp.ClientSession],
) -> Callable[[], RedfishSession]:
    """Create one concrete aiohttp session and observe request starts without replacing transport."""
    trace_config = aiohttp.TraceConfig()

    async def record_request_start(
        _session: aiohttp.ClientSession,
        _trace_context: SimpleNamespace,
        params: aiohttp.TraceRequestStartParams,
    ) -> None:
        attempted_urls.append(str(params.url))

    trace_config.on_request_start.append(record_request_start)

    def create_session() -> RedfishSession:
        session = aiohttp.ClientSession(trace_configs=[trace_config])
        created_sessions.append(session)
        return session

    return create_session


async def test_self_signed_certificate_obeys_tls_verification_and_closes_sessions(tmp_path: Path) -> None:
    """Requirements 4.8 and 4.9 use real loopback HTTPS in both verification modes."""
    configured_watts = 347.25
    server = FakeRedfishServer(tmp_path, initial_watts=configured_watts)

    disabled_attempts: list[str] = []
    enabled_attempts: list[str] = []
    disabled_sessions: list[aiohttp.ClientSession] = []
    enabled_sessions: list[aiohttp.ClientSession] = []

    async with server:
        assert server.host == "127.0.0.1"

        disabled_plugin = IdracPlugin(
            "tls-disabled",
            IdracParams(
                host=server.authority,
                username=SecretStr(_DEVICE_USERNAME),
                password=SecretStr(_DEVICE_PASSWORD),
                verify_tls=False,
            ),
            session_factory=_real_session_factory(disabled_attempts, disabled_sessions),
        )
        enabled_plugin = IdracPlugin(
            "tls-enabled",
            IdracParams(
                host=server.authority,
                username=SecretStr(_DEVICE_USERNAME),
                password=SecretStr(_DEVICE_PASSWORD),
                verify_tls=True,
            ),
            session_factory=_real_session_factory(enabled_attempts, enabled_sessions),
        )

        try:
            disabled_result = await disabled_plugin.read_power(_REQUEST_TIMEOUT_S)
            enabled_result = await enabled_plugin.read_power(_REQUEST_TIMEOUT_S)
        finally:
            await disabled_plugin.aclose()
            await enabled_plugin.aclose()

        assert isinstance(disabled_result, PowerReading)
        assert disabled_result.device_id == "tls-disabled"
        assert disabled_result.watts == configured_watts

        assert isinstance(enabled_result, ReadFailure)
        assert enabled_result.device_id == "tls-enabled"
        assert enabled_result.classification is FailureClassification.TLS_VALIDATION

        # TraceConfig observes one real aiohttp request start in each branch. The fake's
        # HTTP handler sees only the disabled-verification branch because certificate
        # validation rejects the other connection during the TLS handshake.
        assert disabled_attempts == [server.url]
        assert enabled_attempts == [server.url]
        assert server.requests == (
            RecordedRequest(
                method="GET",
                path=server.power_path,
                authorization=aiohttp.BasicAuth(_DEVICE_USERNAME, _DEVICE_PASSWORD).encode(),
            ),
        )

        assert disabled_plugin.closed
        assert enabled_plugin.closed
        assert len(disabled_sessions) == 1
        assert len(enabled_sessions) == 1
        assert disabled_sessions[0].closed
        assert enabled_sessions[0].closed

    assert not server.is_running
