"""Unit tests for startup orchestration and the shutdown sequence (task 14.3).

These tests prove the wiring holds together: named stages run in exact order, every stage failure is
classified before polling starts, startup completion carries the accepted count, and shutdown either
persists then disconnects or stops publication at the outer deadline.

Configuration parsing, plugin discovery and instantiation, interval resolution, the accumulator, and
the state store are real. The plugin validation call and broker are replaced with in-process fakes so
these lifecycle tests never depend on network access or wall-clock delays.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any, Never, cast

import pytest

import remote_energy_meter.app as app_module
from remote_energy_meter.app import Application, StartupStage
from remote_energy_meter.config.parser import load_options
from remote_energy_meter.diagnostics.redaction import CredentialRegistry
from remote_energy_meter.plugins.idrac.plugin import IdracPlugin
from remote_energy_meter.plugins.interface import ValidationResult
from remote_energy_meter.plugins.registry import PluginRegistry, discover, validate_connections
from remote_energy_meter.scheduler import PollScheduler
from remote_energy_meter.state_store import EnergyStateStore

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Sequence
    from pathlib import Path

    from remote_energy_meter.config.models import DeviceEntry, MqttSettings
    from remote_energy_meter.config.parser import ParseOutcome
    from remote_energy_meter.mqtt.inventory import PublishableDevice
    from remote_energy_meter.plugins.registry import (
        ConnectionValidation,
        DiscoveryResult,
        InstantiatedDevice,
        InstantiationOutcome,
    )
    from remote_energy_meter.state_store import RestoreResult

DEVICE_ID = "poweredge_a"

#: A syntactically valid host; plugin validation is replaced below, so no socket is opened.
HOST = "127.0.0.1"


@pytest.fixture(autouse=True)
def avoid_network_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep application lifecycle tests independent of iDRAC network reachability."""

    async def validation_succeeds(_plugin: IdracPlugin, _timeout_s: float) -> ValidationResult:
        return ValidationResult.success()

    monkeypatch.setattr(IdracPlugin, "validate_connection", validation_succeeds)


@pytest.fixture(autouse=True)
def restore_root_logging() -> Iterator[None]:
    """Undo the global logging changes startup makes.

    ``Application.start`` calls ``configure_logging``, which by design installs a handler and the
    credential redaction filter on the *root* logger -- that is the whole point in production. In a test
    session it outlives the test, and a redaction filter with a non-empty registry rewrites every later
    record: it moves tracebacks from ``exc_info`` into a redacted ``exc_text``. That silently broke an
    unrelated scheduler test that asserts on ``exc_info``, which is a nastier failure than it sounds,
    because the culprit is whichever test ran first.
    """
    root = logging.getLogger()
    handlers = list(root.handlers)
    filters = list(root.filters)
    level = root.level
    # Each handler's own filter list has to be saved too. install_redaction_filter attaches the filter
    # to the logger *and* to every handler already on it, which in a test session means pytest's own
    # capture handler. Restoring only root.handlers puts the same handler objects back, still carrying
    # the filter.
    handler_filters = {id(handler): list(handler.filters) for handler in handlers}
    try:
        yield
    finally:
        for handler in handlers:
            handler.filters[:] = handler_filters[id(handler)]
        root.handlers[:] = handlers
        root.filters[:] = filters
        root.setLevel(level)


class RecordingRunner:
    """Stands in for the poll cycle executor, so no test starts real polling.

    Without this the real executor runs the real iDRAC plugin against loopback the moment startup
    finishes, and the asyncio noise from those aborted reads contaminates caplog in unrelated tests.
    The accumulate-then-publish composition it displaces is covered directly below.
    """

    def __init__(self) -> None:
        self.cycles: list[str] = []

    async def run_cycle(self, device: Any) -> Any:
        self.cycles.append(device.device_id)
        return None


class FakePublisher:
    """Records the lifecycle calls the application makes, without a broker."""

    def __init__(self, settings: MqttSettings, devices: Sequence[PublishableDevice]) -> None:
        self.settings = settings
        self.devices = list(devices)
        self.started = False
        self.stopped = False
        self.offline_published = False
        self.calls: list[str] = []

    async def start(self) -> None:
        self.started = True
        self.calls.append("start")

    async def stop(self) -> None:
        self.stopped = True
        self.calls.append("stop")

    async def publish_all_offline(self) -> None:
        self.offline_published = True
        self.calls.append("offline")

    async def deliver_reading(self, device: Any, reading: Any, duration_ms: int) -> None:
        self.calls.append("reading")

    async def report_unavailable(self, device: Any, failure: Any) -> None:
        self.calls.append("unavailable")


def options_document(**overrides: Any) -> dict[str, Any]:
    """A minimal accepted configuration for one iDRAC device."""
    document: dict[str, Any] = {
        "mqtt": {"host": "core-mosquitto", "port": 1883},
        "devices": [
            {
                "device_id": DEVICE_ID,
                "plugin": "idrac",
                "params": {"host": HOST, "username": "svc", "password": "hunter2hunter2"},
            }
        ],
    }
    document.update(overrides)
    return document


def write_options(tmp_path: Path, document: dict[str, Any]) -> Path:
    path = tmp_path / "options.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def build(tmp_path: Path, document: dict[str, Any] | None = None) -> tuple[Application, list[FakePublisher]]:
    """An application over a real store in ``tmp_path`` and a recording publisher."""
    built: list[FakePublisher] = []

    def factory(settings: MqttSettings, devices: Sequence[PublishableDevice]) -> Any:
        publisher = FakePublisher(settings, devices)
        built.append(publisher)
        return publisher

    async def discovery_succeeds(_settings: MqttSettings) -> bool:
        return True

    application = Application(
        options_path=write_options(tmp_path, document if document is not None else options_document()),
        state_store=EnergyStateStore(data_dir=tmp_path),
        publisher_factory=factory,
        cycle_runner=RecordingRunner(),
        discovery_announcer=discovery_succeeds,
        # Isolated: registering this configuration's password into the process-wide registry would
        # switch the redaction filter on for every later test in the session.
        credential_registry=CredentialRegistry(),
    )
    return application, built


async def test_startup_runs_every_named_stage_in_exact_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Requirement 1.3: all named startup stages occur once and in declaration order."""
    stages: list[StartupStage] = []

    original_discover = discover
    original_load_options = load_options
    original_instantiate_all = PluginRegistry.instantiate_all
    original_validate_connections = validate_connections
    original_restore = EnergyStateStore.restore
    original_publisher_start = FakePublisher.start
    original_scheduler = PollScheduler

    def recording_discover(*args: Any, **kwargs: Any) -> DiscoveryResult:
        stages.append(StartupStage.DISCOVER_PLUGINS)
        return original_discover(*args, **kwargs)

    def recording_load_options(*args: Any, **kwargs: Any) -> ParseOutcome:
        stages.append(StartupStage.LOAD_CONFIGURATION)
        return original_load_options(*args, **kwargs)

    def recording_instantiate_all(registry: PluginRegistry, entries: Iterable[DeviceEntry]) -> InstantiationOutcome:
        stages.append(StartupStage.INSTANTIATE_PLUGINS)
        return original_instantiate_all(registry, entries)

    async def recording_validate_connections(
        devices: Sequence[InstantiatedDevice],
        timeout_for: Callable[[InstantiatedDevice], float] | float,
        *,
        logger: logging.Logger | None = None,
    ) -> tuple[ConnectionValidation, ...]:
        stages.append(StartupStage.VALIDATE_CONNECTIONS)
        return await original_validate_connections(devices, timeout_for, logger=logger)

    def recording_restore(store: EnergyStateStore) -> RestoreResult:
        stages.append(StartupStage.RESTORE_ENERGY_STATE)
        return original_restore(store)

    async def recording_publisher_start(publisher: FakePublisher) -> None:
        stages.append(StartupStage.START_PUBLISHER)
        await original_publisher_start(publisher)

    def recording_scheduler(*args: Any, **kwargs: Any) -> PollScheduler:
        stages.append(StartupStage.START_SCHEDULER)
        return original_scheduler(*args, **kwargs)

    monkeypatch.setattr(app_module, "discover", recording_discover)
    monkeypatch.setattr(app_module, "load_options", recording_load_options)
    monkeypatch.setattr(PluginRegistry, "instantiate_all", recording_instantiate_all)
    monkeypatch.setattr(app_module, "validate_connections", recording_validate_connections)
    monkeypatch.setattr(EnergyStateStore, "restore", recording_restore)
    monkeypatch.setattr(FakePublisher, "start", recording_publisher_start)
    monkeypatch.setattr(app_module, "PollScheduler", recording_scheduler)
    application, _ = build(tmp_path)

    assert await application.start() is None
    assert stages == list(StartupStage)
    await application.shutdown()


async def test_publisher_starts_before_one_shot_discovery_and_scheduler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The best-effort announcement runs exactly once in the required startup window."""
    events: list[str] = []
    original_publisher_start = FakePublisher.start
    original_scheduler = PollScheduler

    async def recording_publisher_start(publisher: FakePublisher) -> None:
        events.append("publisher")
        await original_publisher_start(publisher)

    async def recording_announcer(_settings: MqttSettings) -> bool:
        events.append("supervisor_discovery")
        return False

    def recording_scheduler(*args: Any, **kwargs: Any) -> PollScheduler:
        events.append("scheduler")
        return original_scheduler(*args, **kwargs)

    monkeypatch.setattr(FakePublisher, "start", recording_publisher_start)
    monkeypatch.setattr(app_module, "PollScheduler", recording_scheduler)
    application, _ = build(tmp_path)
    application._discovery_announcer = recording_announcer

    assert await application.start() is None
    assert events == ["publisher", "supervisor_discovery", "scheduler"]
    assert application._scheduler_task is not None
    await application.shutdown()


@pytest.mark.parametrize(
    "failed_stage",
    [
        StartupStage.DISCOVER_PLUGINS,
        StartupStage.VALIDATE_CONNECTIONS,
        StartupStage.RESTORE_ENERGY_STATE,
        StartupStage.START_PUBLISHER,
        StartupStage.START_SCHEDULER,
    ],
)
async def test_each_previously_uncovered_stage_failure_is_classified_and_skips_polling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failed_stage: StartupStage,
) -> None:
    """Requirement 1.4: unexpected failures are attributed to their exact stage."""

    def fail_sync(*_args: object, **_kwargs: object) -> Never:
        raise RuntimeError("injected startup failure")

    async def fail_async(*_args: object, **_kwargs: object) -> Never:
        raise RuntimeError("injected startup failure")

    if failed_stage is StartupStage.DISCOVER_PLUGINS:
        monkeypatch.setattr(app_module, "discover", fail_sync)
    elif failed_stage is StartupStage.VALIDATE_CONNECTIONS:
        monkeypatch.setattr(app_module, "validate_connections", fail_async)
    elif failed_stage is StartupStage.RESTORE_ENERGY_STATE:
        monkeypatch.setattr(EnergyStateStore, "restore", fail_sync)
    elif failed_stage is StartupStage.START_PUBLISHER:
        monkeypatch.setattr(FakePublisher, "start", fail_async)
    elif failed_stage is StartupStage.START_SCHEDULER:
        monkeypatch.setattr(app_module, "PollScheduler", fail_sync)
    else:  # pragma: no cover - the parameter list is intentionally exhaustive above
        raise AssertionError(f"unhandled stage {failed_stage}")

    application, _ = build(tmp_path)
    with caplog.at_level("ERROR"):
        failure = await application.start()

    assert failure is not None
    assert failure.stage is failed_stage
    assert failure.classification == "unexpected_error"
    assert application._scheduler_task is None
    stage_errors = [
        record
        for record in caplog.records
        if record.levelno == logging.ERROR and getattr(record, "stage", None) == failed_stage.value
    ]
    assert len(stage_errors) == 1
    assert getattr(stage_errors[0], "classification", None) == failure.classification


async def test_startup_accepts_a_valid_configuration_and_starts_polling(tmp_path: Path) -> None:
    """The whole stage sequence against real collaborators."""
    application, built = build(tmp_path)

    failure = await application.start()

    assert failure is None
    assert application.accepted_device_count == 1
    assert len(built) == 1
    assert built[0].started is True, "Requirement 6.12: the publisher starts without blocking"
    assert [device.device_id for device in built[0].devices] == [DEVICE_ID]
    await application.shutdown()


async def test_startup_logs_the_accepted_device_count(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Requirement 1.3: startup-complete names how many entries are being polled."""
    application, _ = build(tmp_path)

    with caplog.at_level("INFO"):
        assert await application.start() is None

    complete = [record for record in caplog.records if "startup complete" in record.getMessage()]
    assert len(complete) == 1, "exactly one startup-complete record is written"
    assert complete[0].levelno == logging.INFO
    assert getattr(complete[0], "accepted_devices", None) == 1
    assert getattr(complete[0], "stage", None) == StartupStage.START_SCHEDULER.value
    await application.shutdown()


async def test_a_configuration_with_no_accepted_devices_fails_before_the_scheduler(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Requirements 2.4, 3.8: nothing left to poll is fatal, and the stage is named."""
    document = options_document(devices=[{"device_id": DEVICE_ID, "plugin": "not_installed", "params": {}}])
    application, built = build(tmp_path, document)

    with caplog.at_level("ERROR"):
        failure = await application.start()

    assert failure is not None
    assert failure.stage is StartupStage.INSTANTIATE_PLUGINS
    assert failure.classification == "no_accepted_devices"
    assert application.accepted_device_count == 0
    assert application._scheduler_task is None
    assert built == [], "the publisher is never built when there is nothing to publish"
    named = [
        record
        for record in caplog.records
        if record.levelno == logging.ERROR and getattr(record, "stage", None) == StartupStage.INSTANTIATE_PLUGINS.value
    ]
    assert len(named) == 1, "Requirement 1.4: one error record names the failed stage"
    assert getattr(named[0], "classification", None) == failure.classification


async def test_an_unreadable_configuration_fails_at_the_load_stage(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Requirement 1.7: the read failure is distinguished from a parse failure."""
    missing = tmp_path / "absent.json"
    application = Application(
        options_path=missing,
        state_store=EnergyStateStore(data_dir=tmp_path),
        publisher_factory=lambda settings, devices: FakePublisher(settings, devices),
        cycle_runner=RecordingRunner(),
        credential_registry=CredentialRegistry(),
    )

    with caplog.at_level("ERROR"):
        failure = await application.start()

    assert failure is not None
    assert failure.stage is StartupStage.LOAD_CONFIGURATION
    assert failure.classification == "options_unreadable"
    assert application._scheduler_task is None
    stage_errors = [
        record
        for record in caplog.records
        if record.levelno == logging.ERROR and getattr(record, "stage", None) == StartupStage.LOAD_CONFIGURATION.value
    ]
    assert len(stage_errors) == 1
    assert getattr(stage_errors[0], "classification", None) == failure.classification


async def test_shutdown_persists_energy_state_before_disconnecting(tmp_path: Path) -> None:
    """Requirement 7.3 then 1.5: a lost total is unrecoverable, a missed offline is not."""
    application, built = build(tmp_path)
    assert await application.start() is None

    await application.shutdown()

    assert built[0].calls[-2:] == ["offline", "stop"], "offline is published, then the connection closes"
    assert (tmp_path / "energy_state.json").exists(), "the cumulative totals were written"


async def test_shutdown_deadline_warns_and_stops_before_any_further_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Requirement 1.6: outer-budget expiry abandons every remaining publication step."""
    application, built = build(tmp_path)
    assert await application.start() is None
    assert built[0].calls == ["start"]

    requested_deadlines: list[float | None] = []
    real_timeout = asyncio.timeout

    def expire_immediately(delay: float | None) -> asyncio.Timeout:
        requested_deadlines.append(delay)
        return real_timeout(0)

    monkeypatch.setattr(asyncio, "timeout", expire_immediately)
    with caplog.at_level("WARNING"):
        await application.shutdown()

    assert requested_deadlines == [app_module.SHUTDOWN_BUDGET_S]
    assert built[0].calls == ["start"], "deadline expiry permits no offline or disconnect publication"
    deadline_warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and getattr(record, "budget_s", None) == app_module.SHUTDOWN_BUDGET_S
    ]
    assert len(deadline_warnings) == 1
    assert "shutdown exceeded" in deadline_warnings[0].getMessage()


async def test_shutdown_is_safe_after_a_failed_startup(tmp_path: Path) -> None:
    """Nothing was started, so shutdown must not raise on the collaborators that never existed."""
    document = options_document(devices=[{"device_id": DEVICE_ID, "plugin": "not_installed", "params": {}}])
    application, _ = build(tmp_path, document)
    assert await application.start() is not None

    await application.shutdown()


async def test_a_rejected_device_interval_falls_back_to_the_global_one(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Requirement 5.7, and exactly one record for it."""
    document = options_document()
    document["poll_interval_s"] = 45
    document["devices"][0]["poll_interval_s"] = 3600
    document["devices"][0]["params"]["request_timeout_s"] = 4.0
    application, _ = build(tmp_path, document)

    with caplog.at_level("WARNING"):
        assert await application.start() is None

    rejections = [r for r in caplog.records if "poll interval" in r.getMessage() and "rejected" in r.getMessage()]
    assert len(rejections) <= 1, "the interval is resolved once, so it is reported at most once"
    await application.shutdown()


def test_the_interval_rejection_record_names_the_rejected_value(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Requirement 5.7: the record names the entry identifier *and the rejected poll interval value*.

    Naming only the resolved global interval leaves the administrator unable to tell what they wrote that was
    refused, so the refused value is asserted as its own field, distinguishable from the resolved one.

    ``_schedule_for`` is exercised directly with a ``DeviceEntry`` built past its own bounds, because the parser
    excludes an out-of-range ``poll_interval_s`` before startup ever reaches the resolution (Requirement 2.2) --
    which is exactly the second-line-of-defence position ``resolve_interval`` documents for this branch.
    """
    from remote_energy_meter.config.models import ConfigurationObject, DeviceEntry, MqttSettings
    from remote_energy_meter.plugins.registry import InstantiatedDevice

    entry = DeviceEntry.model_construct(
        device_id=DEVICE_ID,
        plugin="idrac",
        display_name=None,
        poll_interval_s=4,  # below the 5 s floor, so it is refused
        params={},
    )
    device = InstantiatedDevice(entry=entry, plugin=cast("Any", object()))
    config = ConfigurationObject(
        poll_interval_s=45,
        mqtt=MqttSettings(host="core-mosquitto", port=1883),
        devices=[DeviceEntry(device_id=DEVICE_ID, plugin="idrac", params={})],
    )
    application, _ = build(tmp_path)

    with caplog.at_level("WARNING"):
        interval_s, _timeout_s = application._schedule_for(device, config)

    assert interval_s == 45, "the global interval is used instead"
    rejections = [r for r in caplog.records if "poll interval" in r.getMessage() and "rejected" in r.getMessage()]
    assert len(rejections) == 1, "exactly one rejection record"
    record = rejections[0]
    assert getattr(record, "rejected_interval_s", None) == 4, "the refused value travels as its own field"
    assert getattr(record, "interval_s", None) == 45, "and stays distinguishable from the resolved interval"
    assert getattr(record, "device_id", None) == DEVICE_ID
    assert "4" in record.getMessage(), "the message names the refused value too"


# --------------------------------------------------------------------------------------------------
# the sink composition the recording runner displaces above
# --------------------------------------------------------------------------------------------------


async def test_a_reading_is_accumulated_before_it_is_published() -> None:
    """The order is load-bearing: the publisher reads the total back out of the accumulator.

    Publishing first would leave every reading's own contribution one cycle behind on the energy
    topic, which is the kind of off-by-one that looks like a rounding problem for months.
    """
    from datetime import UTC, datetime

    from remote_energy_meter.app import AccumulateThenPublish
    from remote_energy_meter.energy import EnergyAccumulator
    from remote_energy_meter.plugins.interface import PowerReading

    order: list[str] = []

    class WatchingAccumulator(EnergyAccumulator):
        def record(self, reading: PowerReading) -> Any:
            order.append("accumulate")
            return super().record(reading)

    class WatchingPublisher(FakePublisher):
        async def deliver_reading(self, device: Any, reading: Any, duration_ms: int) -> None:
            order.append("publish")

    accumulator = WatchingAccumulator()
    accumulator.register(DEVICE_ID, 30.0)
    publisher = WatchingPublisher.__new__(WatchingPublisher)
    publisher.calls = []
    sink = AccumulateThenPublish(accumulator, publisher)

    reading = PowerReading(device_id=DEVICE_ID, watts=120.0, measured_at=datetime(2025, 1, 1, tzinfo=UTC))
    await sink.deliver_reading(cast_device(), reading, 25)

    assert order == ["accumulate", "publish"]


async def test_a_failed_cycle_reaches_the_publisher_only() -> None:
    """There is no reading to integrate, so nothing should reach the accumulator."""
    from remote_energy_meter.app import AccumulateThenPublish
    from remote_energy_meter.energy import EnergyAccumulator
    from remote_energy_meter.plugins.interface import FailureClassification, ReadFailure

    accumulator = EnergyAccumulator()
    accumulator.register(DEVICE_ID, 30.0)
    publisher = FakePublisher.__new__(FakePublisher)
    publisher.calls = []
    sink = AccumulateThenPublish(accumulator, publisher)

    failure = ReadFailure(device_id=DEVICE_ID, classification=FailureClassification.TIMEOUT, detail="timed out")
    await sink.report_unavailable(cast_device(), failure)

    assert publisher.calls == ["unavailable"]
    assert accumulator.total_kwh(DEVICE_ID) == 0.0


def cast_device() -> Any:
    """A stand-in resolved device: the sink passes it straight through."""

    class _Device:
        device_id = DEVICE_ID
        interval_s = 30.0

    return _Device()


# --------------------------------------------------------------------------------------------------
# optional plugin resource cleanup
# --------------------------------------------------------------------------------------------------


class _TrackedCleanupPlugin:
    """Generic resource-owning plugin exposing both optional cleanup spellings."""

    def __init__(self, *, failure: str | None = None) -> None:
        self.failure = failure
        self.aclose_calls = 0
        self.close_calls = 0

    async def aclose(self) -> None:
        self.aclose_calls += 1
        if self.failure is not None:
            raise RuntimeError(self.failure)

    async def close(self) -> None:
        self.close_calls += 1


class _CloseOnlyPlugin:
    """Generic plugin using the aiohttp-style fallback cleanup name."""

    def __init__(self) -> None:
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


class _BlockingCleanupPlugin:
    """Plugin whose cleanup makes caller cancellation observable."""

    def __init__(self) -> None:
        self.calls = 0
        self.started = asyncio.Event()

    async def aclose(self) -> None:
        self.calls += 1
        self.started.set()
        await asyncio.Event().wait()


def _cleanup_device(device_id: str, plugin_id: str, plugin: object) -> InstantiatedDevice:
    """Wrap an arbitrary plugin object in the registry's startup-owned device metadata."""
    from remote_energy_meter.config.models import DeviceEntry
    from remote_energy_meter.plugins.registry import InstantiatedDevice

    entry = DeviceEntry(device_id=device_id, plugin=plugin_id, params={})
    return InstantiatedDevice(entry=entry, plugin=cast("Any", plugin))


async def test_normal_shutdown_closes_a_real_idrac_session_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The application, not each caller, owns the real iDRAC aiohttp session lifecycle."""
    sessions: list[Any] = []
    close_calls: list[str] = []
    original_aclose = IdracPlugin.aclose

    async def validation_opens_session(plugin: IdracPlugin, _timeout_s: float) -> ValidationResult:
        sessions.append(plugin._ensure_session())
        return ValidationResult.success()

    async def recording_aclose(plugin: IdracPlugin) -> None:
        close_calls.append(plugin.device_id)
        await original_aclose(plugin)

    monkeypatch.setattr(IdracPlugin, "validate_connection", validation_opens_session)
    monkeypatch.setattr(IdracPlugin, "aclose", recording_aclose)
    application, _ = build(tmp_path)

    try:
        assert await application.start() is None
        assert len(sessions) == 1
        assert not bool(sessions[0].closed)
    finally:
        await application.shutdown()

    await application.shutdown()
    assert bool(sessions[0].closed)
    assert close_calls == [DEVICE_ID]


async def test_partial_start_failure_still_closes_the_validated_idrac_session_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure after validation must not orphan the session validation opened."""
    sessions: list[Any] = []
    close_calls: list[str] = []
    original_aclose = IdracPlugin.aclose

    async def validation_opens_session(plugin: IdracPlugin, _timeout_s: float) -> ValidationResult:
        sessions.append(plugin._ensure_session())
        return ValidationResult.success()

    async def recording_aclose(plugin: IdracPlugin) -> None:
        close_calls.append(plugin.device_id)
        await original_aclose(plugin)

    def fail_restore(_store: EnergyStateStore) -> Never:
        raise RuntimeError("injected failure after validation")

    monkeypatch.setattr(IdracPlugin, "validate_connection", validation_opens_session)
    monkeypatch.setattr(IdracPlugin, "aclose", recording_aclose)
    monkeypatch.setattr(EnergyStateStore, "restore", fail_restore)
    application, _ = build(tmp_path)

    try:
        failure = await application.start()
        assert failure is not None
        assert failure.stage is StartupStage.RESTORE_ENERGY_STATE
        assert len(sessions) == 1
        assert not bool(sessions[0].closed)
    finally:
        await application.shutdown()

    await application.shutdown()
    assert bool(sessions[0].closed)
    assert close_calls == [DEVICE_ID]


async def test_shutdown_closes_each_unique_fake_plugin_once_and_prefers_aclose(tmp_path: Path) -> None:
    """Duplicate device wrappers cannot double-close one shared third-party resource."""
    plugin = _TrackedCleanupPlugin()
    application, _ = build(tmp_path)
    application._plugin_devices = (
        _cleanup_device("device_a", "fake", plugin),
        _cleanup_device("device_b", "fake", plugin),
    )

    await application.shutdown()
    await application.shutdown()

    assert plugin.aclose_calls == 1
    assert plugin.close_calls == 0, "aclose is preferred; aliases are not both invoked"


async def test_one_plugin_close_failure_does_not_block_siblings_or_log_credentials(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """One warning identifies the failed resource while later and stateless plugins continue."""
    credential = "not-for-the-log"
    failing = _TrackedCleanupPlugin(failure=f"close exposed {credential}")
    healthy = _CloseOnlyPlugin()
    application, _ = build(tmp_path)
    application._plugin_devices = (
        _cleanup_device("failed_device", "resourceful", failing),
        _cleanup_device("healthy_device", "close_only", healthy),
        _cleanup_device("stateless_device", "ipmi", object()),
    )

    with caplog.at_level(logging.WARNING):
        await application.shutdown()

    assert failing.aclose_calls == 1
    assert healthy.close_calls == 1
    warnings = [record for record in caplog.records if getattr(record, "error_type", None) == "RuntimeError"]
    assert len(warnings) == 1
    assert getattr(warnings[0], "device_id", None) == "failed_device"
    assert getattr(warnings[0], "plugin_id", None) == "resourceful"
    assert credential not in warnings[0].getMessage()
    assert credential not in caplog.text


async def test_cancelling_shutdown_while_a_plugin_closes_propagates_to_the_caller(tmp_path: Path) -> None:
    """Optional cleanup must not neutralize the outer deadline or explicit caller cancellation."""
    plugin = _BlockingCleanupPlugin()
    application, _ = build(tmp_path)
    application._plugin_devices = (_cleanup_device("slow_device", "slow_plugin", plugin),)

    shutdown = asyncio.create_task(application.shutdown())
    await asyncio.wait_for(plugin.started.wait(), timeout=1.0)
    shutdown.cancel()

    with pytest.raises(asyncio.CancelledError):
        await shutdown

    await application.shutdown()
    assert plugin.calls == 1


async def test_supervisor_discovery_failure_warns_but_does_not_fail_startup(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Companion discovery is nonfatal; polling still starts after an announcer exception."""
    application, built = build(tmp_path)

    async def discovery_fails(_settings: MqttSettings) -> bool:
        raise RuntimeError("Supervisor unavailable")

    application._discovery_announcer = discovery_fails
    with caplog.at_level(logging.WARNING):
        failure = await application.start()

    assert failure is None
    assert application._scheduler_task is not None
    assert built[0].started is True
    assert any("Supervisor discovery failed non-fatally" in record.getMessage() for record in caplog.records)
    await application.shutdown()
