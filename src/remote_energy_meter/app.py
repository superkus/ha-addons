"""Startup orchestration and shutdown sequence (Requirements 1.3-1.7, 3.5, 3.8, 6.12, 7.3).

Startup runs named stages in order. Naming them is the point: Requirement 1.4 wants a failed startup
to say *which* step failed, so the stage is a value rather than a message someone keeps in sync.

**One deliberate reordering.** The task's stage list begins with loading the configuration, but the
parser needs each plugin's ``config_model`` to validate that plugin's ``params`` block, and those
models are known only once discovery has run. Discovery therefore comes first. It reads no
configuration and cannot depend on one, so the dependency runs one way only.

**Credential registration is not a stage.** The configuration parser registers every
credential-marked field as it validates, against the process-wide registry, so the redaction filter
already knows every secret by the time parsing returns. A second pass would be a no-op implying the
parser could not be trusted.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Protocol, cast

from remote_energy_meter.config.parser import load_options
from remote_energy_meter.diagnostics.logger import DEVICE_ID_FIELD, configure_logging, get_logger
from remote_energy_meter.diagnostics.redaction import CREDENTIAL_REGISTRY
from remote_energy_meter.energy import EnergyAccumulator
from remote_energy_meter.mqtt.inventory import PublishableDevice
from remote_energy_meter.mqtt.publisher import MqttPublisher
from remote_energy_meter.plugins.registry import (
    PluginRegistry,
    discover,
    retained_devices,
    validate_connections,
)
from remote_energy_meter.scheduler import (
    PollCycleExecutor,
    PollScheduler,
    resolve_interval,
    resolve_timeout,
)
from remote_energy_meter.state_store import EnergyStateStore
from remote_energy_meter.supervisor_discovery import announce_supervisor_discovery

if TYPE_CHECKING:
    import logging
    from collections.abc import Awaitable, Callable, Mapping, Sequence
    from pathlib import Path

    from pydantic import BaseModel

    from remote_energy_meter.config.models import ConfigurationObject, MqttSettings
    from remote_energy_meter.config.runtime import ResolvedDevice
    from remote_energy_meter.diagnostics.redaction import CredentialRegistry
    from remote_energy_meter.plugins.interface import PowerReading, ReadFailure
    from remote_energy_meter.plugins.registry import InstantiatedDevice
    from remote_energy_meter.scheduler import CycleRunner


#: Requirement 1.5. The whole shutdown sequence, not any single step within it.
SHUTDOWN_BUDGET_S: Final = 10.0

#: Requirement 7.3. The persistence step's share of the shutdown budget.
SHUTDOWN_FLUSH_BUDGET_S: Final = 5.0

#: Exit status for a failed startup stage (Requirement 1.4).
EXIT_STARTUP_FAILED: Final = 1


class PublisherPort(Protocol):
    """What startup and shutdown need from a publisher.

    A protocol rather than :class:`MqttPublisher` itself, for the same reason ``CycleSink`` and
    ``BrokerClient`` are protocols: the wiring is worth testing without a broker, and a concrete type
    here would make the substitute a lie that only a cast could hide.
    """

    async def start(self) -> None:
        """Begin connecting. Must not block on the connection succeeding (Requirement 6.12)."""
        ...

    async def stop(self) -> None:
        """Disconnect cleanly."""
        ...

    async def publish_all_offline(self) -> None:
        """Mark every announced device unavailable (Requirement 1.5)."""
        ...

    async def deliver_reading(self, device: ResolvedDevice, reading: PowerReading, duration_ms: int) -> None:
        """Publish one successful cycle."""
        ...

    async def report_unavailable(self, device: ResolvedDevice, failure: ReadFailure) -> None:
        """Publish a fully failed cycle."""
        ...


if TYPE_CHECKING:
    # Declared after PublisherPort, which it names.
    PublisherFactory = Callable[[MqttSettings, Sequence[PublishableDevice]], PublisherPort]
    DiscoveryAnnouncer = Callable[[MqttSettings], Awaitable[bool]]


class StartupStage(StrEnum):
    """The named startup steps, in the order they run (Requirement 1.4)."""

    DISCOVER_PLUGINS = "discover_plugins"
    LOAD_CONFIGURATION = "load_configuration"
    INSTANTIATE_PLUGINS = "instantiate_plugins"
    VALIDATE_CONNECTIONS = "validate_connections"
    RESTORE_ENERGY_STATE = "restore_energy_state"
    START_PUBLISHER = "start_publisher"
    START_SCHEDULER = "start_scheduler"


@dataclass(frozen=True, slots=True)
class StartupFailure:
    """Why startup stopped, in the shape Requirement 1.4 asks to be logged."""

    stage: StartupStage
    classification: str
    detail: str


class AccumulateThenPublish:
    """The :class:`~remote_energy_meter.scheduler.CycleSink` the executor reports into.

    Accumulation happens *before* publication, and the order is load-bearing: the publisher reads the
    cumulative total back out of the accumulator, so publishing first would leave a reading's own
    contribution one cycle behind on the energy topic.

    The accumulator is synchronous and the publisher is not, which is precisely the wrapping the
    ``CycleSink`` docstring anticipates.
    """

    __slots__ = ("_accumulator", "_publisher")

    def __init__(self, accumulator: EnergyAccumulator, publisher: PublisherPort) -> None:
        self._accumulator = accumulator
        self._publisher = publisher

    async def deliver_reading(self, device: ResolvedDevice, reading: PowerReading, duration_ms: int) -> None:
        self._accumulator.record(reading)
        await self._publisher.deliver_reading(device, reading, duration_ms)

    async def report_unavailable(self, device: ResolvedDevice, failure: ReadFailure) -> None:
        await self._publisher.report_unavailable(device, failure)


class Application:
    """Owns every long-lived collaborator and the order they are brought up and torn down."""

    def __init__(
        self,
        *,
        options_path: Path | None = None,
        state_store: EnergyStateStore | None = None,
        publisher_factory: PublisherFactory | None = None,
        cycle_runner: CycleRunner | None = None,
        discovery_announcer: DiscoveryAnnouncer | None = None,
        credential_registry: CredentialRegistry | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        """Wire the application.

        :param options_path: where Supervisor wrote the options; the parser's default when omitted.
        :param state_store: injected so a test can point persistence at a temporary directory.
        :param publisher_factory: builds the publisher from settings and devices. Injected so startup
            can be exercised without a broker; the default builds a real :class:`MqttPublisher`.
        :param logger: where records go; this module's logger by default.
        """
        self._options_path = options_path
        self._store = state_store if state_store is not None else EnergyStateStore()
        self._publisher_factory: PublisherFactory | None = publisher_factory
        # Injected so a test can verify the wiring without starting real polling. When omitted the
        # real executor is built here, over the accumulate-then-publish sink.
        self._cycle_runner = cycle_runner
        # Supervisor discovery is best effort and deliberately outside the fatal startup stages.
        self._discovery_announcer: DiscoveryAnnouncer = (
            discovery_announcer if discovery_announcer is not None else announce_supervisor_discovery
        )
        # The process-wide registry by default, because the redaction filter installed on the root
        # logger consults that one. Injectable so a test does not have to register a real credential
        # into global state that every later test then inherits.
        self._credentials = credential_registry if credential_registry is not None else CREDENTIAL_REGISTRY
        self._log = logger if logger is not None else get_logger(__name__)
        self._accumulator = EnergyAccumulator()
        # Retain ownership as soon as construction succeeds. Validation may open transport resources
        # and a later startup stage may fail before ``_devices`` is populated; shutdown still has to
        # release those partially-started plugin instances.
        self._plugin_devices: tuple[InstantiatedDevice, ...] = ()
        self._devices: tuple[ResolvedDevice, ...] = ()
        self._publisher: PublisherPort | None = None
        self._scheduler_task: asyncio.Task[None] | None = None

    @property
    def accepted_device_count(self) -> int:
        """How many Device_Entry values survived to polling."""
        return len(self._devices)

    async def start(self) -> StartupFailure | None:
        """Run the startup stages in order. Returns the failure, or ``None`` on success.

        Requirement 1.4: a stage failure is logged once naming the stage and classification, the
        scheduler is not started, and the caller exits non-zero. Returning the failure rather than
        raising keeps the exit-status decision with the entry point.
        """
        stage = StartupStage.DISCOVER_PLUGINS
        try:
            discovered = discover(logger=self._log)
            if discovered.fatal is not None:
                fatal = discovered.fatal
                owners = ", ".join(fatal.owners) if fatal.owners else "unknown"
                return self._failed(
                    stage,
                    fatal.reason.value,
                    f"plugin identifier {fatal.plugin_id!r} declared by {owners}",
                )
            registry = PluginRegistry.from_discovery(discovered, logger=self._log)
            schemas: Mapping[str, type[BaseModel]] = {
                plugin_id: plugin_cls.config_model for plugin_id, plugin_cls in registry.plugins.items()
            }

            stage = StartupStage.LOAD_CONFIGURATION
            parsed = load_options(self._options_path, schemas=schemas, logger=self._log, registry=self._credentials)
            # Requirement 9.2: the configured level applies to every record after this point. The parser
            # has already written its own records at the default level, which is why it resolves the level
            # and hands it over rather than configuring logging itself.
            configure_logging(parsed.level_resolution.level.value, registry=self._credentials, logger=None)
            if parsed.fatal is not None or parsed.config is None:
                reason = parsed.fatal.reason.value if parsed.fatal is not None else "no_configuration"
                detail = parsed.fatal.detail if parsed.fatal is not None else "the parser produced no configuration"
                return self._failed(stage, reason, detail)
            config = parsed.config

            stage = StartupStage.INSTANTIATE_PLUGINS
            instantiated = registry.instantiate_all(config.devices)
            self._plugin_devices = instantiated.devices
            if not instantiated.devices:
                # Requirement 2.4 and 3.8: every entry excluded is fatal, and the parser and registry have
                # already explained each one, so this record says only that nothing is left to poll.
                return self._failed(
                    stage,
                    "no_accepted_devices",
                    f"all {len(config.devices)} configured device entries were excluded",
                )

            # Requirement 3.10: validation is reported, never a verdict. Every device is retained
            # whichever way it went, which is what retained_devices makes legible.
            # Resolved once per device, before validation needs the timeout. Resolving inside both the
            # validation call and the with_schedule call would emit Requirement 5.7's rejection twice.
            schedules = {id(device): self._schedule_for(device, config) for device in instantiated.devices}

            stage = StartupStage.VALIDATE_CONNECTIONS
            validations = await validate_connections(
                instantiated.devices,
                lambda device: schedules[id(device)][1],
                logger=self._log,
            )
            self._devices = tuple(
                device.with_schedule(*schedules[id(device)]) for device in retained_devices(validations)
            )

            stage = StartupStage.RESTORE_ENERGY_STATE
            restored = self._store.restore()
            self._accumulator.register_all(
                {device.device_id: float(device.interval_s) for device in self._devices},
                restored.devices,
            )

            stage = StartupStage.START_PUBLISHER
            publisher = self._build_publisher(config)
            self._publisher = publisher
            # Requirement 6.12: start returns as soon as the supervisor is scheduled, so a broker that is
            # down at boot delays nothing and fails nothing.
            await publisher.start()

            # Companion discovery is an onboarding convenience.  A missing token, unavailable
            # Supervisor, or even an unexpected announcer failure must not stop polling.
            try:
                await self._discovery_announcer(config.mqtt)
            except Exception as error:
                self._log.warning(
                    "Supervisor discovery failed non-fatally (%s)",
                    type(error).__name__,
                    extra={"error_type": type(error).__name__},
                )

            stage = StartupStage.START_SCHEDULER
            runner = self._cycle_runner
            if runner is None:
                runner = PollCycleExecutor(sink=AccumulateThenPublish(self._accumulator, publisher), logger=self._log)
            scheduler = PollScheduler(self._devices, executor=runner, logger=self._log)
            self._scheduler_task = asyncio.create_task(scheduler.run(), name="poll-scheduler")
        except Exception as error:
            return self._failed(
                stage,
                "unexpected_error",
                f"{type(error).__name__}: {error}",
            )

        self._log.info(
            "startup complete: polling %d device(s)",
            len(self._devices),
            extra={"accepted_devices": len(self._devices), "stage": StartupStage.START_SCHEDULER.value},
        )
        return None

    async def shutdown(self) -> None:
        """Tear down inside the Requirement 1.5 budget.

        Order: stop polling, close plugin transports, persist what was accumulated, then say goodbye on
        the broker. Plugin transports close only after in-flight reads have stopped. Persistence comes
        before publication because a lost kWh total cannot be recovered and a missed ``offline`` can --
        the last will covers it the moment the connection drops.

        The whole sequence runs under one outer budget. On expiry the remaining steps are abandoned
        rather than retried, because Requirement 1.6 wants the process gone, not thorough.
        """
        try:
            async with asyncio.timeout(SHUTDOWN_BUDGET_S):
                await self._stop_polling()
                await self._close_plugins()
                await self._persist_energy_state()
                await self._stop_publisher()
        except TimeoutError:
            self._log.warning(
                "shutdown exceeded its %.0f s budget; exiting without completing the remaining steps",
                SHUTDOWN_BUDGET_S,
                extra={"budget_s": SHUTDOWN_BUDGET_S},
            )

    async def _stop_polling(self) -> None:
        """Cancel every device loop without swallowing cancellation of the shutdown task itself."""
        task = self._scheduler_task
        self._scheduler_task = None
        if task is None:
            return

        caller = asyncio.current_task()
        caller_cancellations = caller.cancelling() if caller is not None else 0
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        # ``await task`` raises CancelledError for the child cancellation we requested. The outer
        # shutdown timeout also cancels *this* task, though, and suppressing that cancellation would
        # neutralize the 10-second deadline and allow offline publication to continue after expiry.
        if caller is not None and caller.cancelling() > caller_cancellations:
            raise asyncio.CancelledError

    async def _close_plugins(self) -> None:
        """Close each unique plugin resource once, while isolating sibling failures.

        Cleanup is an optional extension to the plugin protocol: transport-owning plugins may expose
        async ``aclose()`` or ``close()``, while plugins such as IPMI need neither. Ownership is consumed
        before the first await so repeated shutdown calls never invoke a plugin cleanup method twice.
        ``CancelledError`` is intentionally not caught; the outer shutdown deadline and caller
        cancellation must still abort the remaining sequence.
        """
        devices = self._plugin_devices
        self._plugin_devices = ()
        seen: set[int] = set()

        for device in devices:
            plugin = device.plugin
            identity = id(plugin)
            if identity in seen:
                continue
            seen.add(identity)

            try:
                cleanup = getattr(plugin, "aclose", None)
                if not callable(cleanup):
                    cleanup = getattr(plugin, "close", None)
                if not callable(cleanup):
                    continue
                await cast("Callable[[], Awaitable[object]]", cleanup)()
            except Exception as error:
                # Do not interpolate the exception or plugin representation: a third-party plugin may
                # include credentials in either. The type still makes the failure diagnosable.
                self._log.warning(
                    "plugin cleanup failed for device %s using plugin %s (%s)",
                    device.device_id,
                    device.plugin_id,
                    type(error).__name__,
                    extra={
                        DEVICE_ID_FIELD: device.device_id,
                        "plugin_id": device.plugin_id,
                        "error_type": type(error).__name__,
                    },
                )

    async def _persist_energy_state(self) -> None:
        """Flush the cumulative totals under their own bound (Requirement 7.3)."""
        if not self._devices:
            return
        result = await self._store.flush_within(self._accumulator.snapshot(), SHUTDOWN_FLUSH_BUDGET_S)
        if not result.written:
            self._log.warning(
                "energy state was not persisted on shutdown: %s",
                result.outcome.value,
                extra={"outcome": result.outcome.value},
            )

    async def _stop_publisher(self) -> None:
        """Mark every device offline, then disconnect cleanly (Requirements 1.5, 6.8)."""
        publisher = self._publisher
        self._publisher = None
        if publisher is None:
            return
        await publisher.publish_all_offline()
        await publisher.stop()

    # -- helpers -----------------------------------------------------------------------------

    def _schedule_for(self, device: InstantiatedDevice, config: ConfigurationObject) -> tuple[int, float]:
        """Resolve one device's poll interval and request timeout (Requirements 5.2, 5.7, 5.8).

        Requirement 5.7's rejection record is written here, because :func:`resolve_interval` is pure and
        reports the rejection in its return value rather than logging it.

        The request timeout comes from the plugin's own ``params``, not from the entry envelope: how
        long one request may take is a plugin concern, while how often to ask is not.
        """
        entry = device.entry
        resolution = resolve_interval(entry.poll_interval_s, config.poll_interval_s)
        if resolution.rejected:
            # Requirement 5.7 wants the *refused* value named, not just the one that replaced it: an
            # administrator reading this record needs to see what they wrote to know what to correct.
            self._log.warning(
                "device poll interval %s rejected, using the global interval %s: %s",
                resolution.rejected_value,
                resolution.interval_s,
                entry.device_id,
                extra={
                    DEVICE_ID_FIELD: entry.device_id,
                    "rejected_interval_s": resolution.rejected_value,
                    "interval_s": resolution.interval_s,
                },
            )
        declared = entry.params.get("request_timeout_s")
        timeout_s = resolve_timeout(declared if isinstance(declared, (int, float)) else None, resolution.interval_s)
        return resolution.interval_s, timeout_s

    def _build_publisher(self, config: ConfigurationObject) -> PublisherPort:
        """Build the publisher over the publishable view of every accepted device."""
        devices = [
            PublishableDevice(
                device_id=device.device_id,
                display_name=device.entry.display_name,
                plugin_id=device.plugin_id,
            )
            for device in self._devices
        ]
        if self._publisher_factory is not None:
            return self._publisher_factory(config.mqtt, devices)
        return MqttPublisher(
            config.mqtt,
            devices,
            energy_total_kwh=self._energy_total,
            logger=self._log,
        )

    def _energy_total(self, device_id: str) -> float | None:
        """The publisher's view of the cumulative total: a number, or nothing yet."""
        if device_id not in self._accumulator:
            return None
        return self._accumulator.total_kwh(device_id)

    def _failed(self, stage: StartupStage, classification: str, detail: str) -> StartupFailure:
        """Record one error naming the failed stage and classification (Requirement 1.4)."""
        self._log.error(
            "startup failed at stage %s: %s",
            stage.value,
            classification,
            extra={"stage": stage.value, "classification": classification, "detail": detail},
        )
        return StartupFailure(stage=stage, classification=classification, detail=detail)
