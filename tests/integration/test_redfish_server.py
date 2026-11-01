"""Focused transport tests for :mod:`tests.fakes.redfish_server`.

These tests exercise the fixture itself.  IdracPlugin TLS classification belongs to task
15.3 and is intentionally not asserted here.
"""

from __future__ import annotations

import asyncio
import ssl
from contextlib import suppress
from pathlib import Path

import aiohttp
import pytest

import tests.fakes.redfish_server as redfish_server_module
from tests.fakes.redfish_server import FakeRedfishServer, OpenSslUnavailableError, RecordedRequest

pytestmark = pytest.mark.integration


async def _get_json(server: FakeRedfishServer) -> tuple[int, object]:
    async with aiohttp.ClientSession() as session, session.get(server.url, ssl=False) as response:
        payload: object = await response.json()
        return response.status, payload


async def _assert_port_is_closed(url: str) -> None:
    timeout = aiohttp.ClientTimeout(total=0.5)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        with pytest.raises(aiohttp.ClientConnectorError):
            await session.get(url, ssl=False)


async def test_https_startup_exposes_endpoint_and_cleanup_releases_socket(tmp_path: Path) -> None:
    server = FakeRedfishServer(tmp_path, initial_watts=211.5)
    with pytest.raises(RuntimeError, match="has not been started"):
        _ = server.port

    async with server:
        assert server.is_running
        assert server.host == "127.0.0.1"
        assert 1 <= server.port <= 65535
        assert server.authority == f"{server.host}:{server.port}"
        assert server.base_url == f"https://{server.authority}"
        assert server.url == f"{server.base_url}{server.power_path}"
        assert server.certificate_path.is_file()
        assert server.key_path.is_file()
        assert server.certificate_path.parent == tmp_path
        assert server.key_path.parent == tmp_path

        status, payload = await _get_json(server)
        assert status == 200
        assert payload == {"PowerControl": [{"PowerConsumedWatts": 211.5}]}
        old_url = server.url

    await _assert_port_is_closed(old_url)


@pytest.mark.parametrize("server_hostname", ["localhost", "127.0.0.1"])
async def test_certificate_san_covers_localhost_and_loopback(tmp_path: Path, server_hostname: str) -> None:
    async with FakeRedfishServer(tmp_path) as server:
        trusted_context = ssl.create_default_context(cafile=str(server.certificate_path))
        _reader, writer = await asyncio.open_connection(
            server.host,
            server.port,
            ssl=trusted_context,
            server_hostname=server_hostname,
        )
        assert writer.get_extra_info("ssl_object") is not None
        writer.close()
        await writer.wait_closed()


async def test_valid_response_is_switchable_and_requests_are_recorded(tmp_path: Path) -> None:
    server = FakeRedfishServer(tmp_path)
    async with server:
        server.respond_with_power(487.25)
        authorization = "Basic Zml4dHVyZS11c2VyOmZpeHR1cmUtcGFzc3dvcmQ="
        headers = {"Authorization": authorization}
        async with aiohttp.ClientSession() as session, session.get(server.url, headers=headers, ssl=False) as response:
            payload: object = await response.json()

        assert response.status == 200
        assert payload == {"PowerControl": [{"PowerConsumedWatts": 487.25}]}
        assert server.requests == (RecordedRequest(method="GET", path=server.power_path, authorization=authorization),)


async def test_absent_power_behavior_returns_json_without_a_power_member(tmp_path: Path) -> None:
    server = FakeRedfishServer(tmp_path)
    server.respond_without_power()

    async with server:
        status, payload = await _get_json(server)

    assert status == 200
    assert payload == {"PowerControl": [{"Name": "System Chassis"}]}


@pytest.mark.parametrize("status", [401, 418, 503])
async def test_arbitrary_http_status_behavior_returns_the_selected_status(tmp_path: Path, status: int) -> None:
    server = FakeRedfishServer(tmp_path)
    async with server:
        server.respond_with_status(status)
        async with aiohttp.ClientSession() as session, session.get(server.url, ssl=False) as response:
            await response.read()
            assert response.status == status


async def test_switching_away_from_stall_releases_the_waiting_handler(tmp_path: Path) -> None:
    server = FakeRedfishServer(tmp_path)
    server.stall()

    async with server, aiohttp.ClientSession() as session:
        request = asyncio.create_task(session.get(server.url, ssl=False))
        await server.wait_until_stalled()
        assert server.active_stall_count == 1
        assert not request.done()

        server.respond_with_power(123.0)
        released_response = await asyncio.wait_for(request, timeout=1.0)
        try:
            assert released_response.status == 503
            assert await released_response.text() == "configured stall released"
        finally:
            released_response.release()

        assert server.active_stall_count == 0
        status, payload = await _get_json(server)
        assert status == 200
        assert payload == {"PowerControl": [{"PowerConsumedWatts": 123.0}]}


async def test_cleanup_cancels_stalled_handlers(tmp_path: Path) -> None:
    server = FakeRedfishServer(tmp_path)
    server.stall()
    session = aiohttp.ClientSession()
    request: asyncio.Task[aiohttp.ClientResponse] | None = None

    try:
        await server.start()
        old_url = server.url
        request = asyncio.create_task(session.get(server.url, ssl=False))
        await server.wait_until_stalled()
        assert server.active_stall_count == 1

        await server.close()
        assert not server.is_running
        assert server.active_stall_count == 0

        with suppress(aiohttp.ClientError, asyncio.CancelledError):
            response = await asyncio.wait_for(request, timeout=1.0)
            response.release()
        await _assert_port_is_closed(old_url)
    finally:
        if request is not None and not request.done():
            request.cancel()
            with suppress(asyncio.CancelledError):
                await request
        await session.close()
        await server.close()


async def test_missing_openssl_fails_with_an_actionable_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        redfish_server_module.shutil,  # type: ignore[attr-defined]
        "which",
        lambda _command: None,
    )
    server = FakeRedfishServer(tmp_path)

    with pytest.raises(OpenSslUnavailableError, match="install the OpenSSL CLI"):
        await server.start()

    assert not server.is_running
    assert not server.certificate_path.exists()
    assert not server.key_path.exists()
