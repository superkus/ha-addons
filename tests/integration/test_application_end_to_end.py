"""End-to-end Application lifecycle coverage over real loopback transports.

The parent test owns two HTTPS :class:`FakeRedfishServer` instances, one real
:class:`LocalMosquittoBroker`, and a concrete aiomqtt subscriber acting as Home
Assistant. A bounded child process constructs the production :class:`Application`
with temporary options and state paths; no publisher, scheduler, plugin, transport,
or persistence double participates in the exercised path.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from collections import deque
from collections.abc import AsyncIterator, Sequence
from contextlib import suppress
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Final, cast
from urllib.parse import urlsplit

import aiomqtt
import pytest
from custom_components.remote_energy_meter.protocol import entity_unique_id, parse_inventory_payload

from remote_energy_meter.app import Application
from remote_energy_meter.config.models import MqttSettings
from remote_energy_meter.diagnostics.logger import configure_logging, get_logger
from remote_energy_meter.mqtt.topics import DeviceTopics, device_topics_for, integration_inventory_topic
from remote_energy_meter.state_store import EnergyStateStore
from tests.fakes.broker import LocalMosquittoBroker
from tests.fakes.redfish_server import FakeRedfishServer

pytestmark = pytest.mark.integration

_LOOPBACK_HOST: Final = "127.0.0.1"
_CHILD_MODE: Final = "--application-child"
_CHILD_MODULE: Final = "tests.integration.test_application_end_to_end"
_CHILD_READY: Final = "APPLICATION_READY"
_CHILD_READY_TIMEOUT_S: Final = 15.0
_CHILD_EXIT_TIMEOUT_S: Final = 15.0
_CHILD_KILL_TIMEOUT_S: Final = 5.0
_CHILD_OUTPUT_TIMEOUT_S: Final = 2.0
_MESSAGE_TIMEOUT_S: Final = 25.0
_SHUTDOWN_MESSAGE_TIMEOUT_S: Final = 5.0
_E2E_TIMEOUT_S: Final = 60.0
_POLL_INTERVAL_S: Final = 5
_FIRST_RUN_ENERGY_SAMPLES: Final = 3
_MQTT_CLIENT_ID: Final = "remote-energy-meter-e2e"
_HOME_ASSISTANT_CLIENT_ID: Final = "home-assistant-e2e"
_PACKAGE_ROOT: Final = Path(__file__).resolve().parents[2]
_DEVICE_WATTS: Final[dict[str, float]] = {
    "e2e_idrac_a": 123.4,
    "e2e_idrac_b": 456.7,
}
_DEVICE_IDS: Final[tuple[str, ...]] = tuple(_DEVICE_WATTS)


@dataclass(frozen=True, slots=True)
class _ObservedMessage:
    topic: str
    payload: bytes


@dataclass(slots=True)
class _PhaseObservations:
    inventory: dict[str, object] | None
    cleanup_topics: set[str]
    power: dict[str, list[float]]
    energy: dict[str, list[float]]


@dataclass(slots=True)
class _ApplicationChild:
    process: asyncio.subprocess.Process
    output: deque[str]
    output_task: asyncio.Task[None]


def _string_keyed_mapping(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise AssertionError(f"{label} must be an object with string keys")
    return cast(dict[str, object], value)


def _json_object(payload: bytes, *, label: str) -> dict[str, object]:
    try:
        decoded: object = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AssertionError(f"{label} was not a JSON object: {error}") from error
    return _string_keyed_mapping(decoded, label=label)


def _numeric_payload(payload: bytes, *, topic: str) -> float:
    try:
        return float(payload.decode("ascii"))
    except (UnicodeDecodeError, ValueError) as error:
        raise AssertionError(f"{topic} did not carry an ASCII number: {payload!r}") from error


def _require_local_runtime_path(path: Path, *, label: str) -> Path:
    return path.expanduser().resolve(strict=False)


def _require_loopback_authority(authority: str, *, label: str) -> None:
    parsed = urlsplit(f"//{authority}")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"{label} must be a valid loopback host:port authority") from error
    if (
        parsed.hostname != _LOOPBACK_HOST
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{label} must target {_LOOPBACK_HOST}:PORT, not {authority!r}")


def _validate_child_options_are_loopback(options_path: Path, state_path: Path) -> None:
    """Refuse child startup unless every writable path is local and every endpoint is loopback."""
    local_options_path = _require_local_runtime_path(options_path, label="options path")
    _require_local_runtime_path(state_path, label="state path")

    try:
        decoded: object = json.loads(local_options_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"child could not inspect its options file: {error}") from error
    document = _string_keyed_mapping(decoded, label="child options")

    mqtt = _string_keyed_mapping(document.get("mqtt"), label="child MQTT settings")
    mqtt_host = mqtt.get("host")
    mqtt_port = mqtt.get("port")
    if mqtt_host != _LOOPBACK_HOST or not isinstance(mqtt_port, int) or isinstance(mqtt_port, bool):
        raise ValueError("child MQTT settings must target 127.0.0.1 with an integer port")
    if not 1 <= mqtt_port <= 65535:
        raise ValueError("child MQTT port must be in 1..65535")

    devices = document.get("devices")
    if not isinstance(devices, list) or not devices:
        raise ValueError("child options must contain at least one device")
    for index, raw_entry in enumerate(devices):
        entry = _string_keyed_mapping(raw_entry, label=f"child device {index}")
        if entry.get("plugin") != "idrac":
            raise ValueError("the E2E child permits only the production idrac plugin")
        params = _string_keyed_mapping(entry.get("params"), label=f"child device {index} params")
        authority = params.get("host")
        if not isinstance(authority, str):
            raise TypeError(f"child device {index} must provide a string host")
        _require_loopback_authority(authority, label=f"child device {index} host")


async def _run_application_child(options_path: Path, state_path: Path) -> int:
    """Run the real Application until SIGTERM/SIGINT, using only injected filesystem paths."""
    configure_logging()
    logger = get_logger(f"{__name__}.child")
    application = Application(
        options_path=options_path,
        state_store=EnergyStateStore(path=state_path, logger=logger),
        logger=logger,
    )
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed_handlers: list[signal.Signals] = []

    try:
        for stop_signal in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(stop_signal, stopping.set)
            except NotImplementedError as error:
                raise RuntimeError("the E2E child requires asyncio signal handlers") from error
            installed_handlers.append(stop_signal)

        failure = await application.start()
        if failure is not None:
            await application.shutdown()
            return 1

        print(_CHILD_READY, flush=True)
        try:
            await stopping.wait()
        finally:
            await application.shutdown()
        return 0
    finally:
        for stop_signal in installed_handlers:
            loop.remove_signal_handler(stop_signal)


def _child_main(arguments: Sequence[str]) -> int:
    if len(arguments) != 3 or arguments[0] != _CHILD_MODE:
        print(
            f"usage: python -m {_CHILD_MODULE} {_CHILD_MODE} OPTIONS_PATH STATE_PATH",
            file=sys.stderr,
        )
        return 2

    options_path = Path(arguments[1])
    state_path = Path(arguments[2])
    try:
        _validate_child_options_are_loopback(options_path, state_path)
    except (AssertionError, TypeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return asyncio.run(_run_application_child(options_path, state_path))


def _child_environment() -> dict[str, str]:
    environment = dict(os.environ)
    package_paths = os.pathsep.join((str(_PACKAGE_ROOT / "src"), str(_PACKAGE_ROOT)))
    inherited = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join((package_paths, inherited)) if inherited else package_paths
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONUNBUFFERED"] = "1"
    for variable in (
        "ALL_PROXY",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "all_proxy",
        "https_proxy",
        "http_proxy",
    ):
        environment.pop(variable, None)
    environment["NO_PROXY"] = "127.0.0.1,localhost"
    environment["no_proxy"] = "127.0.0.1,localhost"
    return environment


async def _capture_child_output(
    reader: asyncio.StreamReader,
    ready: asyncio.Event,
    output: deque[str],
) -> None:
    while True:
        line = await reader.readline()
        if not line:
            return
        text = line.decode("utf-8", errors="replace").rstrip()
        if text == _CHILD_READY:
            ready.set()
        else:
            output.append(text)


def _child_diagnostics(child: _ApplicationChild) -> str:
    status = child.process.returncode
    rendered = "\n".join(child.output)
    return f"status={status}; output:\n{rendered or 'no child output'}"


async def _finish_output_capture(child: _ApplicationChild) -> None:
    try:
        async with asyncio.timeout(_CHILD_OUTPUT_TIMEOUT_S):
            await child.output_task
    except TimeoutError:
        child.output_task.cancel()
        await asyncio.gather(child.output_task, return_exceptions=True)
        child.output.append("child output pipe did not close before its deadline")


async def _terminate_and_reap(child: _ApplicationChild) -> int:
    """Send SIGTERM, escalate only after a deadline, and always wait for the child."""
    process = child.process
    escalated = False
    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.send_signal(signal.SIGTERM)
        try:
            async with asyncio.timeout(_CHILD_EXIT_TIMEOUT_S):
                await process.wait()
        except TimeoutError:
            escalated = True
            with suppress(ProcessLookupError):
                process.kill()
            try:
                async with asyncio.timeout(_CHILD_KILL_TIMEOUT_S):
                    await process.wait()
            except TimeoutError as error:
                raise RuntimeError(f"Application child {process.pid} could not be reaped after SIGKILL") from error

    await _finish_output_capture(child)
    if process.returncode is None:
        raise RuntimeError(f"Application child {process.pid} has no return status after wait")
    if escalated:
        raise AssertionError(
            f"Application child {process.pid} missed its SIGTERM deadline and required SIGKILL; "
            f"{_child_diagnostics(child)}"
        )
    return process.returncode


async def _reap_despite_cancellation(child: _ApplicationChild) -> int:
    cleanup = asyncio.create_task(_terminate_and_reap(child), name=f"reap-application-{child.process.pid}")
    try:
        return await asyncio.shield(cleanup)
    except asyncio.CancelledError:
        await cleanup
        raise


async def _start_application_child(options_path: Path, state_path: Path) -> _ApplicationChild:
    _validate_child_options_are_loopback(options_path, state_path)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        _CHILD_MODULE,
        _CHILD_MODE,
        str(options_path),
        str(state_path),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(_PACKAGE_ROOT),
        env=_child_environment(),
        start_new_session=True,
    )
    assert process.stdout is not None

    ready = asyncio.Event()
    output: deque[str] = deque(maxlen=200)
    output_task = asyncio.create_task(
        _capture_child_output(process.stdout, ready, output),
        name=f"capture-application-{process.pid}",
    )
    child = _ApplicationChild(process=process, output=output, output_task=output_task)
    ready_waiter = asyncio.create_task(ready.wait(), name=f"wait-application-ready-{process.pid}")
    exit_waiter = asyncio.create_task(process.wait(), name=f"wait-application-exit-{process.pid}")

    try:
        _done, _pending = await asyncio.wait(
            {ready_waiter, exit_waiter},
            timeout=_CHILD_READY_TIMEOUT_S,
            return_when=asyncio.FIRST_COMPLETED,
        )
    except BaseException:
        for waiter in (ready_waiter, exit_waiter):
            waiter.cancel()
        await asyncio.gather(ready_waiter, exit_waiter, return_exceptions=True)
        await _reap_despite_cancellation(child)
        raise

    for waiter in (ready_waiter, exit_waiter):
        if not waiter.done():
            waiter.cancel()
    await asyncio.gather(ready_waiter, exit_waiter, return_exceptions=True)

    if ready.is_set() and process.returncode is None:
        return child

    if process.returncode is None:
        reason = f"did not signal readiness within {_CHILD_READY_TIMEOUT_S:g} seconds"
    else:
        reason = "exited before signaling readiness"
    await _reap_despite_cancellation(child)
    raise AssertionError(f"Application child {process.pid} {reason}; {_child_diagnostics(child)}")


def _observe(message: aiomqtt.Message) -> _ObservedMessage:
    payload = message.payload
    assert isinstance(payload, bytes), f"expected bytes from MQTT, got {type(payload).__name__}"
    return _ObservedMessage(topic=str(message.topic), payload=payload)


async def _next_observed(messages: AsyncIterator[aiomqtt.Message]) -> _ObservedMessage:
    try:
        return _observe(await anext(messages))
    except StopAsyncIteration as error:
        raise AssertionError("MQTT message stream ended before the E2E condition was met") from error


async def _collect_application_phase(
    messages: AsyncIterator[aiomqtt.Message],
    topics_by_device: dict[str, DeviceTopics],
    *,
    minimum_energy_samples: int,
) -> _PhaseObservations:
    cleanup_topics = {
        topic for topics in topics_by_device.values() for topic in (topics.power_discovery, topics.energy_discovery)
    }
    inventory_topic = integration_inventory_topic(
        next(iter(topics_by_device.values())).bridge_availability.removesuffix("/bridge/availability")
    )
    power_lookup: dict[str, str] = {}
    energy_lookup: dict[str, str] = {}
    for device_id, topics in topics_by_device.items():
        power_lookup[topics.power_state] = device_id
        energy_lookup[topics.energy_state] = device_id

    observations = _PhaseObservations(
        inventory=None,
        cleanup_topics=set(),
        power={device_id: [] for device_id in topics_by_device},
        energy={device_id: [] for device_id in topics_by_device},
    )

    def complete() -> bool:
        return (
            observations.inventory is not None
            and observations.cleanup_topics == cleanup_topics
            and all(observations.power[device_id] for device_id in topics_by_device)
            and all(len(observations.energy[device_id]) >= minimum_energy_samples for device_id in topics_by_device)
        )

    try:
        async with asyncio.timeout(_MESSAGE_TIMEOUT_S):
            while not complete():
                message = await _next_observed(messages)
                if message.topic in cleanup_topics:
                    if message.payload != b"":
                        raise AssertionError(f"legacy discovery topic was not deleted: {message.topic}")
                    observations.cleanup_topics.add(message.topic)
                elif message.topic == inventory_topic:
                    observations.inventory = _json_object(message.payload, label="integration inventory")
                elif message.topic in power_lookup:
                    observations.power[power_lookup[message.topic]].append(
                        _numeric_payload(message.payload, topic=message.topic)
                    )
                elif message.topic in energy_lookup:
                    observations.energy[energy_lookup[message.topic]].append(
                        _numeric_payload(message.payload, topic=message.topic)
                    )
                elif message.topic.startswith("homeassistant/"):
                    raise AssertionError(f"unexpected Home Assistant discovery entity on {message.topic}")
    except TimeoutError as error:
        sample_counts = {device_id: len(values) for device_id, values in observations.energy.items()}
        raise AssertionError(
            "timed out waiting for Application MQTT output: "
            f"inventory={observations.inventory is not None}, "
            f"cleanup={len(observations.cleanup_topics)}/{len(cleanup_topics)}, "
            f"energy samples {sample_counts}"
        ) from error
    return observations


async def _collect_shutdown_boundary(
    messages: AsyncIterator[aiomqtt.Message],
    topics_by_device: dict[str, DeviceTopics],
) -> None:
    expected = {topics.availability for topics in topics_by_device.values()}
    expected.add(next(iter(topics_by_device.values())).bridge_availability)
    observed: set[str] = set()

    try:
        async with asyncio.timeout(_SHUTDOWN_MESSAGE_TIMEOUT_S):
            while observed != expected:
                message = await _next_observed(messages)
                if message.topic in expected and message.payload == b"offline":
                    observed.add(message.topic)
    except TimeoutError as error:
        raise AssertionError(f"missing graceful offline publications on {sorted(expected - observed)}") from error


def _assert_inventory_topology(
    observations: _PhaseObservations,
    topics_by_device: dict[str, DeviceTopics],
) -> dict[tuple[str, str], str]:
    assert observations.inventory is not None
    parsed = parse_inventory_payload(
        json.dumps(observations.inventory),
        expected_instance_id="remote_energy_meter",
        expected_base_topic="remote_energy_meter",
    )
    assert {device.device_id for device in parsed.devices} == set(topics_by_device)
    expected_cleanup = {
        topic for topics in topics_by_device.values() for topic in (topics.power_discovery, topics.energy_discovery)
    }
    assert observations.cleanup_topics == expected_cleanup

    unique_ids: dict[tuple[str, str], str] = {}
    for device in parsed.devices:
        topics = topics_by_device[device.device_id]
        assert device.power_state_topic == topics.power_state
        assert device.energy_state_topic == topics.energy_state
        assert device.availability_topic == topics.availability
        assert device.power_attributes_topic == topics.power_attributes
        unique_ids[(device.device_id, "power")] = entity_unique_id(device.device_id, "power")
        unique_ids[(device.device_id, "energy")] = entity_unique_id(device.device_id, "energy")

    assert len(set(unique_ids.values())) == 4
    return unique_ids


def _assert_endpoint_power_and_monotonic_energy(observations: _PhaseObservations) -> None:
    for device_id, expected_watts in _DEVICE_WATTS.items():
        power_samples = observations.power[device_id]
        assert power_samples
        for sample in power_samples:
            assert sample == pytest.approx(expected_watts, abs=0.05)

        energy_samples = observations.energy[device_id]
        assert len(energy_samples) >= _FIRST_RUN_ENERGY_SAMPLES
        assert all(later >= earlier for earlier, later in pairwise(energy_samples))
        assert energy_samples[-1] > energy_samples[0]


def _read_persisted_totals(state_path: Path) -> dict[str, float]:
    assert state_path.is_file(), f"graceful shutdown did not create {state_path}"
    decoded: object = json.loads(state_path.read_text(encoding="utf-8"))
    document = _string_keyed_mapping(decoded, label="persisted energy state")
    assert document.get("schema_version") == 1
    devices = _string_keyed_mapping(document.get("devices"), label="persisted devices")
    assert set(devices) == set(_DEVICE_IDS)

    totals: dict[str, float] = {}
    for device_id in _DEVICE_IDS:
        state = _string_keyed_mapping(devices[device_id], label=f"persisted state for {device_id}")
        total = state.get("total_kwh")
        assert isinstance(total, (int, float)) and not isinstance(total, bool)
        totals[device_id] = float(total)
        assert totals[device_id] > 0.0
    return totals


def _assert_restart_resumes_totals(
    observations: _PhaseObservations,
    persisted_totals: dict[str, float],
) -> None:
    for device_id, persisted_total in persisted_totals.items():
        assert observations.power[device_id][0] == pytest.approx(_DEVICE_WATTS[device_id], abs=0.05)
        assert observations.energy[device_id][0] == pytest.approx(persisted_total, abs=1e-6)


def _write_options(
    options_path: Path,
    broker: LocalMosquittoBroker,
    first_server: FakeRedfishServer,
    second_server: FakeRedfishServer,
) -> None:
    assert broker.host == _LOOPBACK_HOST
    assert first_server.host == _LOOPBACK_HOST
    assert second_server.host == _LOOPBACK_HOST
    payload = {
        "poll_interval_s": _POLL_INTERVAL_S,
        "log_level": "info",
        "mqtt": {
            "host": broker.host,
            "port": broker.port,
            "client_id": _MQTT_CLIENT_ID,
            "base_topic": "remote_energy_meter",
            "discovery_prefix": "homeassistant",
            "tls": False,
        },
        "devices": [
            {
                "device_id": _DEVICE_IDS[0],
                "plugin": "idrac",
                "display_name": "E2E iDRAC A",
                "poll_interval_s": _POLL_INTERVAL_S,
                "params": {
                    "host": first_server.authority,
                    "username": "e2e-user-a",
                    "password": "e2e-password-a",
                    "verify_tls": False,
                    "request_timeout_s": 2.0,
                },
            },
            {
                "device_id": _DEVICE_IDS[1],
                "plugin": "idrac",
                "display_name": "E2E iDRAC B",
                "poll_interval_s": _POLL_INTERVAL_S,
                "params": {
                    "host": second_server.authority,
                    "username": "e2e-user-b",
                    "password": "e2e-password-b",
                    "verify_tls": False,
                    "request_timeout_s": 2.0,
                },
            },
        ],
    }
    options_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


@pytest.fixture
async def local_mosquitto_broker(tmp_path: Path) -> AsyncIterator[LocalMosquittoBroker]:
    """Start the local-only broker before any child; its helper skips clearly when absent."""
    async with LocalMosquittoBroker(tmp_path) as broker:
        yield broker


async def test_real_application_restores_energy_and_reuses_home_assistant_entities(
    tmp_path: Path,
    local_mosquitto_broker: LocalMosquittoBroker,
) -> None:
    """Requirements 6.11 and 7.4 through two generations of the real Application."""
    broker = local_mosquitto_broker
    first_server = FakeRedfishServer(tmp_path / "redfish-a", initial_watts=_DEVICE_WATTS[_DEVICE_IDS[0]])
    second_server = FakeRedfishServer(tmp_path / "redfish-b", initial_watts=_DEVICE_WATTS[_DEVICE_IDS[1]])

    async with first_server, second_server:
        options_path = tmp_path / "application-options.json"
        state_path = tmp_path / "application-state" / "energy_state.json"
        _write_options(options_path, broker, first_server, second_server)
        _validate_child_options_are_loopback(options_path, state_path)

        settings = MqttSettings(host=broker.host, port=broker.port, client_id=_MQTT_CLIENT_ID)
        topics_by_device = {device_id: device_topics_for(settings, device_id) for device_id in _DEVICE_IDS}
        first_child: _ApplicationChild | None = None
        second_child: _ApplicationChild | None = None

        try:
            async with asyncio.timeout(_E2E_TIMEOUT_S):
                async with aiomqtt.Client(
                    hostname=broker.host,
                    port=broker.port,
                    identifier=_HOME_ASSISTANT_CLIENT_ID,
                ) as home_assistant:
                    await home_assistant.subscribe(f"{settings.discovery_prefix}/#", qos=1)
                    await home_assistant.subscribe(f"{settings.base_topic}/#", qos=1)
                    messages = home_assistant.messages

                    first_child = await _start_application_child(options_path, state_path)
                    first_pid = first_child.process.pid
                    first_run = await _collect_application_phase(
                        messages,
                        topics_by_device,
                        minimum_energy_samples=_FIRST_RUN_ENERGY_SAMPLES,
                    )
                    first_unique_ids = _assert_inventory_topology(first_run, topics_by_device)
                    _assert_endpoint_power_and_monotonic_energy(first_run)
                    assert len(first_server.requests) >= _FIRST_RUN_ENERGY_SAMPLES + 1
                    assert len(second_server.requests) >= _FIRST_RUN_ENERGY_SAMPLES + 1

                    finished_first = first_child
                    first_status = await _terminate_and_reap(finished_first)
                    first_child = None
                    assert first_status == 0, _child_diagnostics(finished_first)
                    await _collect_shutdown_boundary(messages, topics_by_device)

                    persisted_totals = _read_persisted_totals(state_path)
                    for device_id, total in persisted_totals.items():
                        assert total >= first_run.energy[device_id][-1]

                    second_child = await _start_application_child(options_path, state_path)
                    assert second_child.process.pid != first_pid
                    restarted = await _collect_application_phase(
                        messages,
                        topics_by_device,
                        minimum_energy_samples=1,
                    )
                    restarted_unique_ids = _assert_inventory_topology(restarted, topics_by_device)
                    assert restarted_unique_ids == first_unique_ids
                    _assert_restart_resumes_totals(restarted, persisted_totals)

                    finished_second = second_child
                    second_status = await _terminate_and_reap(finished_second)
                    second_child = None
                    assert second_status == 0, _child_diagnostics(finished_second)
                    await _collect_shutdown_boundary(messages, topics_by_device)

                    final_totals = _read_persisted_totals(state_path)
                    for device_id, total in final_totals.items():
                        assert total >= persisted_totals[device_id]
        finally:
            for child in (second_child, first_child):
                if child is not None:
                    await _reap_despite_cancellation(child)


if __name__ == "__main__":
    sys.exit(_child_main(sys.argv[1:]))
