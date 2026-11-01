"""Focused transport tests for :mod:`tests.fakes.broker`.

Home Assistant discovery assertions belong to task 15.4 and are intentionally absent here.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import aiomqtt
import pytest

import tests.fakes.broker as broker_module
from tests.fakes.broker import LocalMosquittoBroker

pytestmark = pytest.mark.integration


async def _assert_local_port_is_closed(host: str, port: int) -> None:
    with pytest.raises(OSError):
        _reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=0.5)
        writer.close()
        await writer.wait_closed()


async def test_startup_waits_for_ready_loopback_listener_and_writes_local_artifacts(tmp_path: Path) -> None:
    broker = LocalMosquittoBroker(tmp_path)
    with pytest.raises(RuntimeError, match="has not been started"):
        _ = broker.port

    async with broker:
        assert broker.is_running
        assert broker.host == "127.0.0.1"
        assert 1 <= broker.port <= 65535
        assert broker.address == f"{broker.host}:{broker.port}"

        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(broker.host, broker.port),
            timeout=1.0,
        )
        writer.close()
        await writer.wait_closed()

        for path in (
            broker.root_directory,
            broker.config_path,
            broker.log_path,
            broker.process_output_path,
            broker.data_directory,
        ):
            assert path.is_relative_to(tmp_path)
        assert broker.config_path.is_file()
        assert broker.log_path.is_file()
        assert broker.process_output_path.is_file()
        assert broker.data_directory.is_dir()

        configuration = broker.config_path.read_text(encoding="utf-8")
        assert f"listener {broker.port} 127.0.0.1" in configuration
        assert "allow_anonymous true" in configuration
        assert "persistence false" in configuration
        assert f"persistence_location {broker.data_directory}/" in configuration

    assert not broker.is_running


async def test_aiomqtt_publish_subscribe_round_trip_stays_on_local_broker(tmp_path: Path) -> None:
    topic = "remote-energy-meter/tests/broker-round-trip"
    payload = b"local-only-payload"

    async with (
        LocalMosquittoBroker(tmp_path) as broker,
        aiomqtt.Client(
            hostname=broker.host,
            port=broker.port,
            identifier="rem-fixture-subscriber",
        ) as subscriber,
    ):
        await subscriber.subscribe(topic, qos=1)
        async with aiomqtt.Client(
            hostname=broker.host,
            port=broker.port,
            identifier="rem-fixture-publisher",
        ) as publisher:
            await publisher.publish(topic, payload, qos=1)

        message = await asyncio.wait_for(anext(subscriber.messages), timeout=2.0)

    assert str(message.topic) == topic
    assert message.payload == payload


async def test_cleanup_reaps_process_and_releases_ephemeral_port(tmp_path: Path) -> None:
    broker = LocalMosquittoBroker(tmp_path)
    async with broker:
        host = broker.host
        port = broker.port

    assert not broker.is_running
    await _assert_local_port_is_closed(host, port)


async def test_missing_binary_skips_with_actionable_local_only_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(broker_module, "resolve_mosquitto_binary", lambda: None)
    broker = LocalMosquittoBroker(tmp_path)

    with pytest.raises(BaseException) as skipped:
        await broker.start()

    assert isinstance(skipped.value, pytest.skip.Exception)
    message = str(skipped.value)
    assert "mosquitto" in message
    assert "not found on PATH" in message
    assert "Install Mosquitto" in message
    assert "never fall back to an external broker" in message
    assert not broker.is_running
