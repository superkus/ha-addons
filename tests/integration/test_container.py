"""Container-level integration coverage for the shipped Home Assistant addon image.

These tests build the real Dockerfile from the repository-root context with the base
image pinned in the Dockerfile's ``FROM`` line. They never replace
``/init`` or the s6 service for the behavior under test: startup failure and SIGTERM both
cross the production s6 boundary.

All runtime endpoints are local. The successful lifecycle case requires Docker host
networking so ``127.0.0.1`` means the host from inside the container; it skips clearly
when that capability is unavailable rather than falling back to an external broker or
Redfish endpoint.
"""

from __future__ import annotations

import asyncio
import json
import platform
import shlex
import shutil
import subprocess
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from uuid import uuid4

import aiomqtt
import pytest

from remote_energy_meter.app import EXIT_STARTUP_FAILED
from remote_energy_meter.mqtt.topics import PAYLOAD_AVAILABLE, PAYLOAD_NOT_AVAILABLE, device_topics
from tests.fakes.broker import LocalMosquittoBroker
from tests.fakes.redfish_server import FakeRedfishServer

pytestmark = pytest.mark.integration

_PACKAGE_ROOT: Final = Path(__file__).resolve().parents[2]
_DOCKERFILE: Final = _PACKAGE_ROOT / "remote_energy_meter" / "Dockerfile"
_LOOPBACK_HOST: Final = "127.0.0.1"

_DOCKER_DISCOVERY_TIMEOUT_S: Final = 10.0
_IMAGE_BUILD_TIMEOUT_S: Final = 420.0
_DOCKER_COMMAND_TIMEOUT_S: Final = 15.0
_CONTAINER_STARTUP_TIMEOUT_S: Final = 30.0
_SHUTDOWN_DEADLINE_S: Final = 10.0
_RETAINED_SNAPSHOT_TIMEOUT_S: Final = 5.0
_DIAGNOSTIC_TAIL_CHARACTERS: Final = 12_000

_IMAGE_PREFIX: Final = "remote-energy-meter-container-test"
_CONTAINER_PREFIX: Final = "rem-container-test"
_TEST_LABEL: Final = "remote-energy-meter.container-test"

_ARCHITECTURE_ALIASES: Final[dict[str, str]] = {
    "amd64": "amd64",
    "x86_64": "amd64",
    "aarch64": "aarch64",
    "arm64": "aarch64",
}

_MOUNT_UNAVAILABLE_MARKERS: Final = (
    "bind source path does not exist",
    "invalid mount config",
    "is not shared from the host",
    "mounts denied",
)


class _CommandDeadlineError(RuntimeError):
    """A bounded local command missed its explicit deadline."""


@dataclass(frozen=True, slots=True)
class _BuiltAddonImage:
    docker: str
    tag: str


@dataclass(frozen=True, slots=True)
class _ObservedMessage:
    topic: str
    payload: bytes
    retain: bool


def _bounded_command(
    command: Sequence[str],
    *,
    timeout_s: float,
    cwd: Path = _PACKAGE_ROOT,
) -> subprocess.CompletedProcess[str]:
    """Run one non-interactive command with captured output and a hard deadline."""
    try:
        return subprocess.run(
            list(command),
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as error:
        raise _CommandDeadlineError(f"command exceeded {timeout_s:g} seconds: {shlex.join(command)}") from error


async def _bounded_docker_command(
    image: _BuiltAddonImage,
    arguments: Sequence[str],
    *,
    timeout_s: float = _DOCKER_COMMAND_TIMEOUT_S,
) -> subprocess.CompletedProcess[str]:
    command = (image.docker, *arguments)
    return await asyncio.to_thread(_bounded_command, command, timeout_s=timeout_s)


def _command_diagnostics(result: subprocess.CompletedProcess[str]) -> str:
    sections = [part.strip() for part in (result.stdout, result.stderr) if part.strip()]
    rendered = "\n".join(sections) or "no command output"
    return rendered[-_DIAGNOSTIC_TAIL_CHARACTERS:]


def _require_success(result: subprocess.CompletedProcess[str], *, action: str) -> None:
    if result.returncode != 0:
        raise AssertionError(
            f"{action} failed with command status {result.returncode}:\n{_command_diagnostics(result)}"
        )


def _native_addon_architecture() -> str:
    machine = platform.machine().casefold()
    architecture = _ARCHITECTURE_ALIASES.get(machine)
    if architecture is None:
        declared = ", ".join(sorted(set(_ARCHITECTURE_ALIASES.values())))
        pytest.skip(
            "Container integration tests skipped: native machine architecture "
            f"{machine!r} is not one the add-on declares ({declared})."
        )
    return architecture


def _pinned_base_image() -> str:
    """The digest-pinned base image from the Dockerfile's single ``FROM`` line, for the failure message."""
    for line in _DOCKERFILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("FROM "):
            return line.removeprefix("FROM ").strip()
    raise AssertionError(f"{_DOCKERFILE} has no FROM line")


def _docker_executable() -> str:
    executable = shutil.which("docker")
    if executable is None:
        pytest.skip(
            "Container integration tests skipped: the Docker CLI was not found on PATH. "
            "Install Docker and ensure its CLI is available."
        )
    return executable


def _require_local_docker_daemon(docker: str) -> None:
    """Refuse a remote daemon before a build could transmit the repository context."""
    try:
        endpoint = _bounded_command(
            (docker, "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"),
            timeout_s=_DOCKER_DISCOVERY_TIMEOUT_S,
        )
    except _CommandDeadlineError as error:
        pytest.skip(f"Container integration tests skipped: Docker context inspection timed out: {error}")
    if endpoint.returncode != 0:
        pytest.skip(
            "Container integration tests skipped: the active Docker context could not be inspected: "
            f"{_command_diagnostics(endpoint)}"
        )
    daemon_endpoint = endpoint.stdout.strip()
    if not daemon_endpoint.startswith("unix://"):
        pytest.skip(
            "Container integration tests skipped: a local Unix-socket Docker daemon is required so the "
            f"repository context is never sent to a remote daemon; active endpoint is {daemon_endpoint!r}."
        )

    try:
        daemon = _bounded_command(
            (docker, "info", "--format", "{{.ServerVersion}}"),
            timeout_s=_DOCKER_DISCOVERY_TIMEOUT_S,
        )
    except _CommandDeadlineError as error:
        pytest.skip(f"Container integration tests skipped: the Docker daemon did not answer in time: {error}")
    if daemon.returncode != 0 or not daemon.stdout.strip():
        pytest.skip(
            "Container integration tests skipped: the Docker CLI is present but its daemon is unavailable: "
            f"{_command_diagnostics(daemon)}"
        )


def _remove_image(docker: str, tag: str) -> None:
    try:
        removed = _bounded_command(
            (docker, "image", "rm", "--force", tag),
            timeout_s=_DOCKER_COMMAND_TIMEOUT_S,
        )
    except _CommandDeadlineError as error:
        raise AssertionError(f"unique test image cleanup timed out for {tag}: {error}") from error
    if removed.returncode != 0 and "No such image" not in _command_diagnostics(removed):
        raise AssertionError(f"could not remove unique test image {tag!r}:\n{_command_diagnostics(removed)}")


@pytest.fixture(scope="module")
def built_addon_image() -> Iterator[_BuiltAddonImage]:
    """Build the unmodified addon image once and always remove its unique tag."""
    docker = _docker_executable()
    _require_local_docker_daemon(docker)
    architecture = _native_addon_architecture()
    base_image = _pinned_base_image()
    image_tag = f"{_IMAGE_PREFIX}:{uuid4().hex}"

    try:
        built = _bounded_command(
            (
                docker,
                "build",
                "--file",
                str(_DOCKERFILE.relative_to(_PACKAGE_ROOT)),
                "--build-arg",
                f"BUILD_ARCH={architecture}",
                "--build-arg",
                "BUILD_VERSION=integration-test",
                "--tag",
                image_tag,
                ".",
            ),
            timeout_s=_IMAGE_BUILD_TIMEOUT_S,
        )
    except _CommandDeadlineError as error:
        raise AssertionError(f"addon image build did not finish: {error}") from error
    _require_success(
        built,
        action=f"building the addon image from the repository root with base {base_image!r} for {architecture}",
    )

    image = _BuiltAddonImage(docker=docker, tag=image_tag)
    try:
        yield image
    finally:
        _remove_image(docker, image_tag)


def _local_data_directory(tmp_path: Path, name: str) -> Path:
    directory = (tmp_path / name).resolve(strict=False)
    directory.mkdir(parents=True)
    return directory


def _mount_is_unavailable(result: subprocess.CompletedProcess[str]) -> bool:
    diagnostics = _command_diagnostics(result).casefold()
    return any(marker in diagnostics for marker in _MOUNT_UNAVAILABLE_MARKERS)


async def _remove_container(image: _BuiltAddonImage, container_name: str) -> None:
    cleanup = asyncio.create_task(
        _bounded_docker_command(image, ("rm", "--force", container_name)),
        name=f"remove-{container_name}",
    )
    try:
        removed = await asyncio.shield(cleanup)
    except asyncio.CancelledError as error:
        removed = await cleanup
        if removed.returncode != 0 and "No such container" not in _command_diagnostics(removed):
            raise AssertionError(
                f"could not remove unique test container {container_name!r}:\n{_command_diagnostics(removed)}"
            ) from error
        raise
    if removed.returncode != 0 and "No such container" not in _command_diagnostics(removed):
        raise AssertionError(
            f"could not remove unique test container {container_name!r}:\n{_command_diagnostics(removed)}"
        )


@asynccontextmanager
async def _managed_addon_container(
    image: _BuiltAddonImage,
    *,
    container_name: str,
    data_directory: Path,
    network: str,
) -> AsyncIterator[str]:
    """Create one real addon container and force-remove it on every exit path."""
    created = await _bounded_docker_command(
        image,
        (
            "create",
            "--name",
            container_name,
            "--label",
            f"{_TEST_LABEL}=true",
            "--network",
            network,
            "--mount",
            f"type=bind,source={data_directory},target=/data",
            image.tag,
        ),
    )
    if created.returncode != 0:
        if _mount_is_unavailable(created):
            pytest.skip(
                "Container integration tests skipped: Docker cannot bind pytest's local temporary "
                f"directory to /data: {_command_diagnostics(created)}"
            )
        _require_success(created, action=f"creating addon container {container_name!r}")

    try:
        yield container_name
    finally:
        await _remove_container(image, container_name)


async def _start_container(image: _BuiltAddonImage, container_name: str) -> None:
    started = await _bounded_docker_command(image, ("start", container_name))
    if started.returncode != 0 and _mount_is_unavailable(started):
        pytest.skip(
            "Container integration tests skipped: Docker cannot start a container with pytest's "
            f"temporary /data bind mount: {_command_diagnostics(started)}"
        )
    _require_success(started, action=f"starting addon container {container_name!r}")


async def _wait_for_container(
    image: _BuiltAddonImage,
    container_name: str,
    *,
    timeout_s: float,
) -> int:
    waited = await _bounded_docker_command(
        image,
        ("wait", container_name),
        timeout_s=timeout_s,
    )
    _require_success(waited, action=f"waiting for addon container {container_name!r}")
    statuses = [line.strip() for line in waited.stdout.splitlines() if line.strip()]
    if len(statuses) != 1:
        raise AssertionError(f"docker wait returned an unexpected status payload: {waited.stdout!r}")
    try:
        return int(statuses[0])
    except ValueError as error:
        raise AssertionError(f"docker wait returned a non-integer status: {statuses[0]!r}") from error


async def _container_logs(image: _BuiltAddonImage, container_name: str) -> str:
    logged = await _bounded_docker_command(image, ("logs", container_name))
    if logged.returncode != 0:
        return f"docker logs failed:\n{_command_diagnostics(logged)}"
    return _command_diagnostics(logged)


async def _require_host_network_loopback(
    image: _BuiltAddonImage,
    *,
    host_port: int,
) -> None:
    """Prove that ``--network host`` reaches a host-loopback listener from this daemon."""
    container_name = f"{_CONTAINER_PREFIX}-network-{uuid4().hex}"
    probe = (
        "import socket, sys; "
        "connection = socket.create_connection(('127.0.0.1', int(sys.argv[1])), 2.0); "
        "connection.close()"
    )
    result: subprocess.CompletedProcess[str] | None = None
    timeout_error: _CommandDeadlineError | None = None
    try:
        try:
            result = await _bounded_docker_command(
                image,
                (
                    "run",
                    "--name",
                    container_name,
                    "--network",
                    "host",
                    "--entrypoint",
                    "python3",
                    image.tag,
                    "-c",
                    probe,
                    str(host_port),
                ),
                timeout_s=_DOCKER_COMMAND_TIMEOUT_S,
            )
        except _CommandDeadlineError as error:
            timeout_error = error
    finally:
        await _remove_container(image, container_name)

    if timeout_error is not None:
        pytest.skip(
            "Container SIGTERM integration test skipped: Docker host networking did not complete a "
            f"container-to-host loopback probe: {timeout_error}"
        )
    if result is None or result.returncode != 0:
        diagnostics = "no probe output" if result is None else _command_diagnostics(result)
        pytest.skip(
            "Container SIGTERM integration test skipped: --network host cannot reach the local "
            f"Mosquitto listener at 127.0.0.1:{host_port} from a container: {diagnostics}"
        )


def _observe(message: aiomqtt.Message) -> _ObservedMessage:
    payload = message.payload
    assert isinstance(payload, bytes), f"expected bytes from MQTT, got {type(payload).__name__}"
    return _ObservedMessage(topic=str(message.topic), payload=payload, retain=bool(message.retain))


async def _collect_expected_messages(
    messages: AsyncIterator[aiomqtt.Message],
    expected: Mapping[str, bytes],
    *,
    timeout_s: float,
) -> dict[str, _ObservedMessage]:
    remaining = dict(expected)
    observed: dict[str, _ObservedMessage] = {}
    try:
        async with asyncio.timeout(timeout_s):
            while remaining:
                try:
                    message = _observe(await anext(messages))
                except StopAsyncIteration as error:
                    raise AssertionError("MQTT stream ended before all expected messages arrived") from error
                expected_payload = remaining.get(message.topic)
                if expected_payload is not None and message.payload == expected_payload:
                    observed[message.topic] = message
                    del remaining[message.topic]
    except TimeoutError as error:
        raise AssertionError(f"timed out waiting for MQTT messages: {sorted(remaining.items())!r}") from error
    return observed


def _write_valid_options(
    data_directory: Path,
    *,
    broker: LocalMosquittoBroker,
    redfish: FakeRedfishServer,
    device_id: str,
    client_id: str,
    base_topic: str,
) -> None:
    document = {
        "poll_interval_s": 5,
        "log_level": "info",
        "mqtt": {
            "host": broker.host,
            "port": broker.port,
            "client_id": client_id,
            "base_topic": base_topic,
            "discovery_prefix": "homeassistant",
            "tls": False,
        },
        "devices": [
            {
                "device_id": device_id,
                "plugin": "idrac",
                "display_name": "Container Integration iDRAC",
                "poll_interval_s": 5,
                "params": {
                    "host": redfish.authority,
                    "username": "container-test-user",
                    "password": "container-test-password",
                    "verify_tls": False,
                    "request_timeout_s": 2.0,
                },
            }
        ],
    }
    (data_directory / "options.json").write_text(json.dumps(document, indent=2), encoding="utf-8")


async def test_s6_propagates_the_production_startup_failure_status(
    tmp_path: Path,
    built_addon_image: _BuiltAddonImage,
) -> None:
    """Requirement 1.2: s6 reports the metering process's exact fatal startup status."""
    data_directory = _local_data_directory(tmp_path, "fatal-options-data")
    (data_directory / "options.json").write_text('{"mqtt":', encoding="utf-8")
    container_name = f"{_CONTAINER_PREFIX}-fatal-{uuid4().hex}"

    async with _managed_addon_container(
        built_addon_image,
        container_name=container_name,
        data_directory=data_directory,
        network="none",
    ):
        await _start_container(built_addon_image, container_name)
        status = await _wait_for_container(
            built_addon_image,
            container_name,
            timeout_s=_DOCKER_COMMAND_TIMEOUT_S,
        )
        logs = await _container_logs(built_addon_image, container_name)

        assert "load_configuration" in logs, f"fatal options did not reach the production load stage:\n{logs}"
        assert "options_unparseable" in logs, f"fatal options were not the observed startup cause:\n{logs}"
        assert status == EXIT_STARTUP_FAILED, (
            "s6 did not propagate the metering process's production startup failure status: "
            f"expected {EXIT_STARTUP_FAILED}, got {status}\n{logs}"
        )


async def test_sigterm_publishes_retained_offline_states_and_exits_zero(
    tmp_path: Path,
    built_addon_image: _BuiltAddonImage,
) -> None:
    """Requirement 1.5 over the real image, s6, MQTT transport, and Redfish transport."""
    token = uuid4().hex
    device_id = f"container_{token[:12]}"
    client_id = f"rem-container-{token[:16]}"
    base_topic = f"remote_energy_meter/container_{token}"
    container_name = f"{_CONTAINER_PREFIX}-graceful-{token}"
    observer_id = f"ha-container-observer-{token[:16]}"
    fresh_observer_id = f"ha-container-retained-{token[:16]}"
    data_directory = _local_data_directory(tmp_path, "graceful-data")

    async with LocalMosquittoBroker(tmp_path / "broker") as broker:
        await _require_host_network_loopback(built_addon_image, host_port=broker.port)
        redfish = FakeRedfishServer(tmp_path / "redfish", initial_watts=275.5)
        async with redfish:
            _write_valid_options(
                data_directory,
                broker=broker,
                redfish=redfish,
                device_id=device_id,
                client_id=client_id,
                base_topic=base_topic,
            )
            topics = device_topics(device_id, base_topic=base_topic)
            expected_online = {
                topics.bridge_availability: PAYLOAD_AVAILABLE.encode(),
                topics.availability: PAYLOAD_AVAILABLE.encode(),
            }
            expected_offline = {
                topics.bridge_availability: PAYLOAD_NOT_AVAILABLE.encode(),
                topics.availability: PAYLOAD_NOT_AVAILABLE.encode(),
            }

            async with aiomqtt.Client(
                hostname=broker.host,
                port=broker.port,
                identifier=observer_id,
            ) as home_assistant:
                await home_assistant.subscribe(f"{base_topic}/#", qos=1)

                async with _managed_addon_container(
                    built_addon_image,
                    container_name=container_name,
                    data_directory=data_directory,
                    network="host",
                ):
                    await _start_container(built_addon_image, container_name)
                    try:
                        await _collect_expected_messages(
                            home_assistant.messages,
                            expected_online,
                            timeout_s=_CONTAINER_STARTUP_TIMEOUT_S,
                        )
                    except AssertionError as error:
                        logs = await _container_logs(built_addon_image, container_name)
                        raise AssertionError(f"addon container did not become ready: {error}\n{logs}") from error

                    assert redfish.requests, "device online was published without a request reaching fake Redfish"
                    shutdown_started = asyncio.get_running_loop().time()
                    signalled = await _bounded_docker_command(
                        built_addon_image,
                        ("kill", "--signal", "SIGTERM", container_name),
                    )
                    _require_success(signalled, action=f"sending SIGTERM to addon container {container_name!r}")

                    try:
                        status = await _wait_for_container(
                            built_addon_image,
                            container_name,
                            timeout_s=_SHUTDOWN_DEADLINE_S,
                        )
                    except (AssertionError, _CommandDeadlineError) as error:
                        logs = await _container_logs(built_addon_image, container_name)
                        raise AssertionError(
                            f"addon container did not exit inside the {_SHUTDOWN_DEADLINE_S:g} s shutdown bound: "
                            f"{error}\n{logs}"
                        ) from error
                    shutdown_elapsed = asyncio.get_running_loop().time() - shutdown_started
                    logs = await _container_logs(built_addon_image, container_name)
                    assert shutdown_elapsed <= _SHUTDOWN_DEADLINE_S, (
                        f"SIGTERM shutdown took {shutdown_elapsed:.3f} s, exceeding {_SHUTDOWN_DEADLINE_S:g} s\n{logs}"
                    )
                    assert status == 0, f"graceful container shutdown returned {status}, expected 0\n{logs}"

                    live_offline = await _collect_expected_messages(
                        home_assistant.messages,
                        expected_offline,
                        timeout_s=_RETAINED_SNAPSHOT_TIMEOUT_S,
                    )
                    assert set(live_offline) == set(expected_offline)

                    async with aiomqtt.Client(
                        hostname=broker.host,
                        port=broker.port,
                        identifier=fresh_observer_id,
                    ) as fresh_home_assistant:
                        await fresh_home_assistant.subscribe(f"{base_topic}/#", qos=1)
                        retained_offline = await _collect_expected_messages(
                            fresh_home_assistant.messages,
                            expected_offline,
                            timeout_s=_RETAINED_SNAPSHOT_TIMEOUT_S,
                        )

                    assert set(retained_offline) == set(expected_offline)
                    assert all(message.retain for message in retained_offline.values()), (
                        "a fresh MQTT subscriber did not receive both bridge and device offline states as retained"
                    )
