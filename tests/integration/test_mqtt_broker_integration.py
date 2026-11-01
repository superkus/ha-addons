"""Real-broker integration coverage for :class:`MqttPublisher`.

Every network endpoint comes from :class:`tests.fakes.broker.LocalMosquittoBroker`, whose
listener is bound to IPv4 loopback.  The tests use concrete aiomqtt clients as Home Assistant
subscribers and exercise only the publisher's public lifecycle and cycle-sink methods.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator, Callable, Collection, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

import aiomqtt
import pytest

from remote_energy_meter.config.models import DeviceEntry, MqttSettings
from remote_energy_meter.config.runtime import ResolvedDevice
from remote_energy_meter.mqtt.inventory import PublishableDevice
from remote_energy_meter.mqtt.publisher import MqttPublisher
from remote_energy_meter.mqtt.topics import (
    bridge_availability_topic,
    device_topics_for,
    integration_inventory_topic,
)
from remote_energy_meter.plugins.interface import PowerReading
from tests.fakes.broker import LocalMosquittoBroker

if TYPE_CHECKING:
    from remote_energy_meter.plugins.interface import DevicePlugin

pytestmark = pytest.mark.integration

_DEVICE_ID: Final = "broker_integration"
_CHILD_MODE: Final = "--last-will-child"
_CHILD_MODULE: Final = "tests.integration.test_mqtt_broker_integration"
_CHILD_READY: Final = b"MQTT_PUBLISHER_READY"
_MESSAGE_TIMEOUT_S: Final = 10.0
_CHILD_EXIT_TIMEOUT_S: Final = 5.0
_SNAPSHOT_SENTINEL_TOPIC: Final = "remote_energy_meter/test_control/retained_snapshot_complete"
_PACKAGE_ROOT: Final = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class _ObservedMessage:
    topic: str
    payload: bytes
    retain: bool


def _require_loopback_host(host: str) -> None:
    """Refuse to launch the child helper against anything except the fixture listener."""
    if host != "127.0.0.1":
        raise ValueError(f"last-will child requires the local broker at 127.0.0.1, not {host!r}")


def _settings(host: str, port: int, *, client_id: str) -> MqttSettings:
    _require_loopback_host(host)
    return MqttSettings(host=host, port=port, client_id=client_id)


def _publishable_device() -> PublishableDevice:
    return PublishableDevice(
        device_id=_DEVICE_ID,
        display_name="Broker Integration Meter",
        manufacturer="Remote Energy Meter tests",
        model="Loopback Mosquitto",
        plugin_id="integration",
    )


def _resolved_device() -> ResolvedDevice:
    """Build the real scheduler-to-publisher value; its unused plugin slot is opaque here."""
    entry = DeviceEntry(device_id=_DEVICE_ID, plugin="integration", params={})
    return ResolvedDevice(
        entry=entry,
        plugin=cast("DevicePlugin", object()),
        interval_s=30,
        timeout_s=10.0,
    )


def _reading(watts: float) -> PowerReading:
    return PowerReading(device_id=_DEVICE_ID, watts=watts, measured_at=datetime.now(UTC))


def _observe(message: aiomqtt.Message) -> _ObservedMessage:
    payload = message.payload
    assert isinstance(payload, bytes), f"expected a bytes MQTT payload, got {type(payload).__name__}"
    return _ObservedMessage(topic=str(message.topic), payload=payload, retain=bool(message.retain))


async def _next_observed(messages: AsyncIterator[aiomqtt.Message]) -> _ObservedMessage:
    try:
        return _observe(await anext(messages))
    except StopAsyncIteration as error:
        raise AssertionError("MQTT message stream ended before the expected publication") from error


async def _collect_until_expected(
    messages: AsyncIterator[aiomqtt.Message],
    expected: Collection[tuple[str, bytes]],
    *,
    timeout_s: float = _MESSAGE_TIMEOUT_S,
) -> list[_ObservedMessage]:
    remaining = set(expected)
    observed: list[_ObservedMessage] = []
    async with asyncio.timeout(timeout_s):
        while remaining:
            message = await _next_observed(messages)
            observed.append(message)
            # An empty expected payload is a topic-only match, used for discovery JSON whose
            # package-version field is intentionally not fixed by this transport test.
            matched = {
                item
                for item in remaining
                if item[0] == message.topic and (item[1] == b"" or item[1] == message.payload)
            }
            remaining.difference_update(matched)
    return observed


async def _collect_through_sentinel(
    messages: AsyncIterator[aiomqtt.Message],
    *,
    timeout_s: float = _MESSAGE_TIMEOUT_S,
) -> list[_ObservedMessage]:
    observed: list[_ObservedMessage] = []
    async with asyncio.timeout(timeout_s):
        while True:
            message = await _next_observed(messages)
            observed.append(message)
            if message.topic == _SNAPSHOT_SENTINEL_TOPIC:
                return observed


async def _wait_until(predicate: Callable[[], bool], *, timeout_s: float = _MESSAGE_TIMEOUT_S) -> None:
    loop = asyncio.get_running_loop()
    async with asyncio.timeout(timeout_s):
        while not predicate():
            wake = asyncio.Event()
            timer = loop.call_later(0.01, wake.set)
            try:
                await wake.wait()
            finally:
                timer.cancel()


def _messages_by_topic(messages: Sequence[_ObservedMessage]) -> dict[str, _ObservedMessage]:
    by_topic: dict[str, _ObservedMessage] = {}
    for message in messages:
        assert message.topic not in by_topic, f"unexpected duplicate publication on {message.topic}"
        by_topic[message.topic] = message
    return by_topic


def _json_object(payload: bytes) -> dict[str, object]:
    decoded: object = json.loads(payload)
    assert isinstance(decoded, dict)
    assert all(isinstance(key, str) for key in decoded)
    return cast(dict[str, object], decoded)


def _child_environment() -> dict[str, str]:
    environment = dict(os.environ)
    package_paths = os.pathsep.join((str(_PACKAGE_ROOT / "src"), str(_PACKAGE_ROOT)))
    inherited = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join((package_paths, inherited)) if inherited else package_paths
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


async def _hold_last_will_publisher(host: str, port: int) -> None:
    """Child-process body: connect a real publisher, signal readiness, then wait to be killed."""
    settings = _settings(host, port, client_id="rem-last-will-child")
    publisher = MqttPublisher(settings, [_publishable_device()])
    await publisher.start()
    try:
        await _wait_until(lambda: publisher.connected)
        sys.stdout.buffer.write(_CHILD_READY + b"\n")
        sys.stdout.buffer.flush()
        await asyncio.Event().wait()
    finally:
        # A normal helper failure disconnects cleanly.  The assertion path uses SIGKILL, so this
        # block cannot run and Mosquitto must publish the registered will.
        await publisher.stop()


def _child_main(arguments: Sequence[str]) -> int:
    if len(arguments) != 3 or arguments[0] != _CHILD_MODE:
        print(f"usage: python -m {_CHILD_MODULE} {_CHILD_MODE} 127.0.0.1 PORT", file=sys.stderr)
        return 2

    host = arguments[1]
    try:
        _require_loopback_host(host)
        port = int(arguments[2])
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    if not 1 <= port <= 65535:
        print(f"port outside 1..65535: {port}", file=sys.stderr)
        return 2

    asyncio.run(_hold_last_will_publisher(host, port))
    return 0


async def _kill_and_reap(process: asyncio.subprocess.Process) -> int:
    """Kill the helper without MQTT DISCONNECT and always reap it within a fixed deadline."""
    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.kill()
    try:
        return await asyncio.wait_for(process.wait(), timeout=_CHILD_EXIT_TIMEOUT_S)
    except TimeoutError as error:
        with suppress(ProcessLookupError):
            process.kill()
        raise RuntimeError(f"last-will helper process {process.pid} could not be reaped") from error


async def _start_last_will_child(host: str, port: int) -> asyncio.subprocess.Process:
    _require_loopback_host(host)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        _CHILD_MODULE,
        _CHILD_MODE,
        host,
        str(port),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(_PACKAGE_ROOT),
        env=_child_environment(),
        start_new_session=True,
    )
    assert process.stdout is not None
    assert process.stderr is not None

    try:
        ready_line = await asyncio.wait_for(process.stdout.readline(), timeout=_MESSAGE_TIMEOUT_S)
    except BaseException:
        await _kill_and_reap(process)
        raise

    if ready_line.rstrip() != _CHILD_READY:
        await _kill_and_reap(process)
        diagnostics = (await process.stderr.read()).decode(errors="replace").strip()
        raise AssertionError(
            f"last-will child exited before connecting (status {process.returncode}): {diagnostics or 'no diagnostics'}"
        )
    return process


def test_last_will_child_helper_rejects_every_non_loopback_host() -> None:
    """Pure safety behavior still runs when Mosquitto is unavailable and live cases skip."""
    _require_loopback_host("127.0.0.1")
    for host in ("localhost", "0.0.0.0", "192.0.2.1", "broker.example"):
        with pytest.raises(ValueError, match="requires the local broker"):
            _require_loopback_host(host)


async def test_home_assistant_receives_inventory_state_availability_and_retained_snapshot(tmp_path: Path) -> None:
    """Real broker coverage for migration cleanup, inventory, state, and retain behavior."""
    async with LocalMosquittoBroker(tmp_path) as broker:
        settings = _settings(broker.host, broker.port, client_id="rem-broker-publication")
        topics = device_topics_for(settings, _DEVICE_ID)
        inventory_topic = integration_inventory_topic(settings.base_topic)
        device = _publishable_device()
        resolved = _resolved_device()

        # Model an upgrade from a release that created generic MQTT entities. The new
        # publisher must delete both retained configs before announcing inventory.
        async with aiomqtt.Client(
            hostname=broker.host,
            port=broker.port,
            identifier="legacy-discovery-seed",
        ) as legacy:
            await legacy.publish(topics.power_discovery, b'{"legacy":true}', qos=1, retain=True)
            await legacy.publish(topics.energy_discovery, b'{"legacy":true}', qos=1, retain=True)

        async with aiomqtt.Client(
            hostname=broker.host,
            port=broker.port,
            identifier="home-assistant-live",
        ) as home_assistant:
            await home_assistant.subscribe(f"{settings.discovery_prefix}/#", qos=1)
            await home_assistant.subscribe(f"{settings.base_topic}/#", qos=1)
            messages = home_assistant.messages
            legacy_snapshot = await _collect_until_expected(
                messages,
                {
                    (topics.power_discovery, b'{"legacy":true}'),
                    (topics.energy_discovery, b'{"legacy":true}'),
                },
            )
            assert {message.topic for message in legacy_snapshot} == {
                topics.power_discovery,
                topics.energy_discovery,
            }

            publisher = MqttPublisher(settings, [device], energy_total_kwh=lambda _device_id: 4.25)
            await publisher.start()
            try:
                await _wait_until(lambda: publisher.connected)
                startup = await _collect_until_expected(
                    messages,
                    {
                        (topics.power_discovery, b""),
                        (topics.energy_discovery, b""),
                        (inventory_topic, b""),
                        (topics.power_attributes, b'{"duration_ms":null}'),
                        (topics.bridge_availability, b"online"),
                    },
                )
                startup_by_topic = _messages_by_topic(startup)
                assert startup_by_topic[topics.power_discovery].payload == b""
                assert startup_by_topic[topics.energy_discovery].payload == b""
                inventory = _json_object(startup_by_topic[inventory_topic].payload)
                assert inventory["schema_version"] == 1
                assert inventory["instance_id"] == "remote_energy_meter"
                assert inventory["bridge_availability_topic"] == topics.bridge_availability
                announced = inventory["devices"]
                assert isinstance(announced, list) and len(announced) == 1
                assert announced[0]["power_state_topic"] == topics.power_state
                assert announced[0]["energy_state_topic"] == topics.energy_state
                assert startup.index(startup_by_topic[topics.power_discovery]) < startup.index(
                    startup_by_topic[inventory_topic]
                )
                assert startup.index(startup_by_topic[topics.energy_discovery]) < startup.index(
                    startup_by_topic[inventory_topic]
                )

                await publisher.deliver_reading(resolved, _reading(321.26), duration_ms=87)
                state = await _collect_until_expected(
                    messages,
                    {
                        (topics.power_state, b"321.3"),
                        (topics.power_attributes, b'{"duration_ms":87}'),
                        (topics.energy_state, b"4.250000"),
                        (topics.availability, b"online"),
                    },
                )
                state_by_topic = _messages_by_topic(state)

                assert state_by_topic[topics.power_state].payload == b"321.3"
                assert state_by_topic[topics.energy_state].payload == b"4.250000"
                assert state_by_topic[topics.availability].payload == b"online"

                async with aiomqtt.Client(
                    hostname=broker.host,
                    port=broker.port,
                    identifier="home-assistant-fresh",
                ) as fresh_home_assistant:
                    await fresh_home_assistant.subscribe(f"{settings.discovery_prefix}/#", qos=1)
                    await fresh_home_assistant.subscribe(f"{settings.base_topic}/#", qos=1)
                    await fresh_home_assistant.publish(
                        _SNAPSHOT_SENTINEL_TOPIC,
                        b"snapshot-complete",
                        qos=1,
                        retain=False,
                    )
                    snapshot = await _collect_through_sentinel(fresh_home_assistant.messages)

                sentinel = snapshot[-1]
                assert sentinel.topic == _SNAPSHOT_SENTINEL_TOPIC
                assert sentinel.retain is False
                retained = _messages_by_topic(snapshot[:-1])
                assert set(retained) == {
                    inventory_topic,
                    topics.bridge_availability,
                    topics.energy_state,
                    topics.availability,
                }
                assert all(message.retain for message in retained.values())
                assert retained[inventory_topic].payload == startup_by_topic[inventory_topic].payload
                assert retained[topics.bridge_availability].payload == b"online"
                assert retained[topics.energy_state].payload == b"4.250000"
                assert retained[topics.availability].payload == b"online"
                assert topics.power_discovery not in retained
                assert topics.energy_discovery not in retained
                assert topics.power_state not in retained
                assert topics.power_attributes not in retained
            finally:
                await publisher.stop()


async def test_real_connection_loss_republishes_inventory_before_buffered_state(tmp_path: Path) -> None:
    """A broker-forced reconnect repeats cleanup and inventory before buffered state."""
    async with LocalMosquittoBroker(tmp_path) as broker:
        settings = _settings(broker.host, broker.port, client_id="rem-reconnect-target")
        topics = device_topics_for(settings, _DEVICE_ID)
        inventory_topic = integration_inventory_topic(settings.base_topic)
        resolved = _resolved_device()

        async with aiomqtt.Client(
            hostname=broker.host,
            port=broker.port,
            identifier="home-assistant-reconnect-observer",
        ) as home_assistant:
            await home_assistant.subscribe(f"{settings.discovery_prefix}/#", qos=1)
            await home_assistant.subscribe(f"{settings.base_topic}/#", qos=1)
            messages = home_assistant.messages

            publisher = MqttPublisher(settings, [_publishable_device()], energy_total_kwh=lambda _device_id: 9.5)
            await publisher.start()
            try:
                await _wait_until(lambda: publisher.connected)
                await _collect_until_expected(
                    messages,
                    {
                        (topics.power_discovery, b""),
                        (topics.energy_discovery, b""),
                        (inventory_topic, b""),
                        (topics.power_attributes, b'{"duration_ms":null}'),
                        (topics.bridge_availability, b"online"),
                    },
                )
                await publisher.deliver_reading(resolved, _reading(111.1), duration_ms=10)
                await _collect_until_expected(
                    messages,
                    {
                        (topics.power_state, b"111.1"),
                        (topics.power_attributes, b'{"duration_ms":10}'),
                        (topics.energy_state, b"9.500000"),
                        (topics.availability, b"online"),
                    },
                )

                # Mosquitto disconnects the old session when a second client claims its ID.
                # Exiting this context before the one-second retry leaves the identifier free for
                # the publisher's next connection.
                async with aiomqtt.Client(
                    hostname=broker.host,
                    port=broker.port,
                    identifier=settings.client_id,
                ):
                    await _wait_until(lambda: not publisher.connected)
                    await publisher.deliver_reading(resolved, _reading(222.26), duration_ms=20)

                recovery = await _collect_until_expected(
                    messages,
                    {
                        (topics.power_discovery, b""),
                        (topics.energy_discovery, b""),
                        (inventory_topic, b""),
                        (topics.bridge_availability, b"online"),
                        (topics.power_state, b"222.3"),
                        (topics.power_attributes, b'{"duration_ms":20}'),
                        (topics.energy_state, b"9.500000"),
                        (topics.availability, b"online"),
                    },
                )
                await _wait_until(lambda: publisher.connected)

                cleanup_indices = [
                    index
                    for index, message in enumerate(recovery)
                    if message.topic in {topics.power_discovery, topics.energy_discovery} and message.payload == b""
                ]
                inventory_index = next(
                    index for index, message in enumerate(recovery) if message.topic == inventory_topic
                )
                state_or_availability_indices = [
                    index
                    for index, message in enumerate(recovery)
                    if (message.topic, message.payload)
                    in {
                        (topics.bridge_availability, b"online"),
                        (topics.power_state, b"222.3"),
                        (topics.energy_state, b"9.500000"),
                        (topics.availability, b"online"),
                    }
                ]
                assert max(cleanup_indices) < inventory_index < min(state_or_availability_indices)
                assert (
                    sum(message.topic == topics.power_discovery and message.payload == b"" for message in recovery) == 1
                )
                assert (
                    sum(message.topic == topics.energy_discovery and message.payload == b"" for message in recovery)
                    == 1
                )
                assert sum(message.topic == inventory_topic for message in recovery) == 1
            finally:
                await publisher.stop()


async def test_ungraceful_publisher_process_death_emits_a_retained_bridge_last_will(tmp_path: Path) -> None:
    """Requirement 6.6: SIGKILL prevents DISCONNECT, so Mosquitto must retain ``offline``."""
    async with LocalMosquittoBroker(tmp_path) as broker:
        bridge_topic = bridge_availability_topic()
        child: asyncio.subprocess.Process | None = None

        async with aiomqtt.Client(
            hostname=broker.host,
            port=broker.port,
            identifier="home-assistant-last-will-observer",
        ) as home_assistant:
            await home_assistant.subscribe(bridge_topic, qos=1)
            messages = home_assistant.messages
            try:
                child = await _start_last_will_child(broker.host, broker.port)
                live_online = await _collect_until_expected(messages, {(bridge_topic, b"online")})
                assert live_online[-1].retain is False

                exit_code = await _kill_and_reap(child)
                assert exit_code != 0, "the helper must die ungracefully rather than send MQTT DISCONNECT"

                live_offline = await _collect_until_expected(messages, {(bridge_topic, b"offline")})
                assert live_offline[-1].retain is False

                async with aiomqtt.Client(
                    hostname=broker.host,
                    port=broker.port,
                    identifier="home-assistant-last-will-fresh",
                ) as fresh_home_assistant:
                    await fresh_home_assistant.subscribe(bridge_topic, qos=1)
                    retained_offline = await _collect_until_expected(
                        fresh_home_assistant.messages,
                        {(bridge_topic, b"offline")},
                    )

                assert retained_offline[-1].payload == b"offline"
                assert retained_offline[-1].retain is True
            finally:
                if child is not None:
                    await _kill_and_reap(child)


if __name__ == "__main__":
    raise SystemExit(_child_main(sys.argv[1:]))
