"""Property 37: conforming plugins cross every generic core boundary unchanged.

Each Hypothesis example creates a real importable package containing one synthetic plugin. The
plugin identifier, credential field name, connection-field name, additional parameter shape,
reading value, and successful or classified-failure behavior all vary. Nothing is registered by
hand: :func:`remote_energy_meter.plugins.registry.discover` walks the package just as it walks the
shipped plugin namespace.

The shipped conformance suite establishes the premise that the generated class satisfies the
public extension contract. It is deliberately not the oracle for the property: expected parser,
registry, scheduler, accumulator, and publisher outcomes are derived independently from the
generated scenario and asserted at each real core boundary.

There is no network or elapsed-time dependency. Import files live under the test process's local
temporary directory, the MQTT transport is a recording client, plugin timestamps and publisher
``now`` are fixed UTC values, and scheduler clock/sleep/timeout behavior is virtual.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import string
import sys
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from itertools import count
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType, TracebackType
from typing import Final, Protocol, cast

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import BaseModel, SecretStr

from remote_energy_meter.app import AccumulateThenPublish
from remote_energy_meter.config.models import MqttSettings
from remote_energy_meter.config.parser import parse_options
from remote_energy_meter.diagnostics.redaction import (
    REDACTION_MARKER,
    CredentialRedactionFilter,
    CredentialRegistry,
)
from remote_energy_meter.energy import EnergyAccumulator, interval_kwh
from remote_energy_meter.mqtt.inventory import PublishableDevice
from remote_energy_meter.mqtt.publisher import BrokerClient, BrokerConnection, MqttPublisher
from remote_energy_meter.mqtt.topics import PAYLOAD_AVAILABLE, PAYLOAD_NOT_AVAILABLE, device_topics_for
from remote_energy_meter.plugins.conformance import assert_plugin_conforms_async
from remote_energy_meter.plugins.interface import (
    FailureClassification,
    PowerReading,
)
from remote_energy_meter.plugins.registry import DiscoveryResult, PluginRegistry, discover
from remote_energy_meter.scheduler import PollCycleExecutor, resolve_interval, resolve_timeout

PLUGIN_ID_ALPHABET: Final = string.ascii_lowercase + string.digits + "_.-"
DEVICE_ID_ALPHABET: Final = string.ascii_lowercase + string.digits + "_-"
FIELD_SUFFIX_ALPHABET: Final = string.ascii_lowercase
CREDENTIAL_ALPHABET: Final = "QXZJVK"
TEXT_VALUE_ALPHABET: Final = string.ascii_lowercase + string.digits + "-"
BASE_READING_AT: Final = datetime(2025, 1, 1, tzinfo=UTC)
PACKAGE_SEQUENCE: Final = count()
LOGGER_NAME: Final = "remote_energy_meter.property.plugin_core_compatibility"


class ParameterShape(StrEnum):
    """Additional schema shape carried beside the fields the scripted plugin consumes."""

    TEXT = "text"
    BOUNDED_INTEGER = "bounded_integer"
    OPTIONAL_FLAG = "optional_flag"
    INTEGER_LIST = "integer_list"
    NESTED_MODEL = "nested_model"


class ReadOutcome(StrEnum):
    """How the generated plugin's one scheduler cycle should finish."""

    SUCCESS = "success"
    RETRY_THEN_SUCCESS = "retry_then_success"
    CLASSIFIED_FAILURE = "classified_failure"


class _GeneratedPluginState(Protocol):
    """Test-only view of state exposed by every generated plugin instance."""

    params: BaseModel
    read_calls: int


@dataclass(frozen=True, slots=True)
class Scenario:
    """One plugin family, one accepted entry, and one expected completed cycle."""

    plugin_id: str
    device_id: str
    endpoint_field: str
    credential_field: str
    extra_field: str
    shape: ParameterShape
    extra_params: Mapping[str, object]
    credential: str
    interval_s: int
    measured_after_s: int
    watts: float
    baseline_watts: float
    outcome: ReadOutcome
    failure_classification: FailureClassification
    failures_before_success: int

    def params(self, *, for_conformance: bool = False) -> dict[str, object]:
        """Valid parameters for this schema, with a guaranteed reading for conformance."""
        values: dict[str, object] = {
            self.endpoint_field: f"{self.device_id}.example.invalid",
            self.credential_field: self.credential,
            "watts": self.watts,
            "measured_after_s": self.measured_after_s,
            "classification": self.failure_classification.value,
            "failures_before_success": 0 if for_conformance else self.failures_before_success,
            "always_fail": False if for_conformance else self.outcome is ReadOutcome.CLASSIFIED_FAILURE,
        }
        values.update(self.extra_params)
        return values

    def incomplete_params(self) -> dict[str, object]:
        """The conformance suite's missing-required-field counterexample."""
        values = self.params(for_conformance=True)
        del values[self.endpoint_field]
        return values

    @property
    def succeeds(self) -> bool:
        return self.outcome is not ReadOutcome.CLASSIFIED_FAILURE

    @property
    def expected_attempts(self) -> int:
        if self.outcome is ReadOutcome.SUCCESS:
            return 1
        if self.outcome is ReadOutcome.RETRY_THEN_SUCCESS:
            return self.failures_before_success + 1
        if self.failure_classification is FailureClassification.AUTHENTICATION:
            return 1
        return 3

    @property
    def expected_failed_attempts(self) -> int:
        return self.expected_attempts - int(self.succeeds)

    @property
    def measured_at(self) -> datetime:
        return BASE_READING_AT + timedelta(seconds=self.measured_after_s)


def _bounded_watts() -> st.SearchStrategy[float]:
    """Finite values over the complete cross-plugin publication range, including both edges."""
    return st.one_of(
        st.sampled_from((0.0, 0.1, 99_999.9, 100_000.0)),
        st.floats(
            min_value=0.0,
            max_value=100_000.0,
            allow_nan=False,
            allow_infinity=False,
            width=64,
        ),
    )


@st.composite
def plugin_scenarios(draw: st.DrawFn) -> Scenario:
    """Generate conforming families with unrelated identifiers, schemas, and outcomes."""
    suffixes = draw(
        st.lists(
            st.text(alphabet=FIELD_SUFFIX_ALPHABET, min_size=1, max_size=8),
            min_size=3,
            max_size=3,
            unique=True,
        )
    )
    endpoint_field = f"endpoint_{suffixes[0]}"
    credential_field = f"credential_{suffixes[1]}"
    extra_field = f"parameter_{suffixes[2]}"
    shape = draw(st.sampled_from(tuple(ParameterShape)))

    extra_params: dict[str, object]
    if shape is ParameterShape.TEXT:
        extra_params = {
            extra_field: draw(st.text(alphabet=TEXT_VALUE_ALPHABET, min_size=1, max_size=24)),
        }
    elif shape is ParameterShape.BOUNDED_INTEGER:
        extra_params = {extra_field: draw(st.integers(min_value=-100, max_value=100))}
    elif shape is ParameterShape.OPTIONAL_FLAG:
        extra_params = {} if draw(st.booleans()) else {extra_field: draw(st.booleans())}
    elif shape is ParameterShape.INTEGER_LIST:
        extra_params = {
            extra_field: draw(st.lists(st.integers(min_value=0, max_value=31), min_size=1, max_size=4, unique=True))
        }
    else:
        extra_params = {
            extra_field: {
                "gain": draw(
                    st.floats(
                        min_value=0.125,
                        max_value=8.0,
                        allow_nan=False,
                        allow_infinity=False,
                        width=32,
                    )
                ),
                "labels": draw(
                    st.lists(
                        st.text(alphabet=TEXT_VALUE_ALPHABET, min_size=1, max_size=8),
                        min_size=1,
                        max_size=3,
                    )
                ),
            }
        }

    outcome = draw(st.sampled_from(tuple(ReadOutcome)))
    classifications = tuple(FailureClassification)
    if outcome is ReadOutcome.RETRY_THEN_SUCCESS:
        classifications = tuple(
            classification
            for classification in classifications
            if classification is not FailureClassification.AUTHENTICATION
        )
    classification = draw(st.sampled_from(classifications))
    failures_before_success = (
        draw(st.integers(min_value=1, max_value=2)) if outcome is ReadOutcome.RETRY_THEN_SUCCESS else 0
    )
    interval_s = draw(st.integers(min_value=5, max_value=60))

    return Scenario(
        plugin_id=draw(st.text(alphabet=PLUGIN_ID_ALPHABET, min_size=1, max_size=64)),
        device_id=draw(st.text(alphabet=DEVICE_ID_ALPHABET, min_size=1, max_size=32)),
        endpoint_field=endpoint_field,
        credential_field=credential_field,
        extra_field=extra_field,
        shape=shape,
        extra_params=extra_params,
        credential=draw(st.text(alphabet=CREDENTIAL_ALPHABET, min_size=12, max_size=32)),
        interval_s=interval_s,
        measured_after_s=draw(st.integers(min_value=1, max_value=4 * interval_s)),
        watts=draw(_bounded_watts()),
        baseline_watts=draw(_bounded_watts()),
        outcome=outcome,
        failure_classification=classification,
        failures_before_success=failures_before_success,
    )


def _shape_source(scenario: Scenario) -> tuple[str, str]:
    """Return any supporting model and the generated Params field declaration."""
    name = scenario.extra_field
    if scenario.shape is ParameterShape.TEXT:
        return "", f"    {name}: str = Field(min_length=1, max_length=24)\n"
    if scenario.shape is ParameterShape.BOUNDED_INTEGER:
        return "", f"    {name}: int = Field(ge=-100, le=100)\n"
    if scenario.shape is ParameterShape.OPTIONAL_FLAG:
        return "", f"    {name}: bool = False\n"
    if scenario.shape is ParameterShape.INTEGER_LIST:
        return "", f"    {name}: list[int] = Field(min_length=1, max_length=4)\n"
    nested = (
        "class NestedParams(BaseModel):\n"
        "    gain: float = Field(gt=0, le=8)\n"
        "    labels: list[str] = Field(min_length=1, max_length=3)\n\n\n"
    )
    return nested, f"    {name}: NestedParams\n"


def _plugin_module_source(scenario: Scenario) -> str:
    """Build a self-contained module whose class knows only the public plugin contract."""
    nested_source, extra_field_source = _shape_source(scenario)
    return f'''"""Generated by Property 37; deliberately unrelated to any shipped family."""

from datetime import datetime, timedelta
from typing import ClassVar

from pydantic import BaseModel, Field, SecretStr

from remote_energy_meter.config.models import CREDENTIAL_MARKER
from remote_energy_meter.plugins.interface import (
    FailureClassification,
    PowerReading,
    ReadFailure,
    ReadResult,
    ValidationResult,
)

BASE_READING_AT = datetime.fromisoformat({BASE_READING_AT.isoformat()!r})
CREDENTIAL_FIELD = {scenario.credential_field!r}


{nested_source}class Params(BaseModel):
    {scenario.endpoint_field}: str = Field(min_length=1, max_length=80)
    {scenario.credential_field}: SecretStr = Field(json_schema_extra={{CREDENTIAL_MARKER: True}})
    watts: float = Field(ge=0, le=100_000)
    measured_after_s: int = Field(ge=1, le=240)
    classification: FailureClassification
    failures_before_success: int = Field(ge=0, le=2)
    always_fail: bool
{extra_field_source}

class GeneratedPlugin:
    plugin_id: ClassVar[str] = {scenario.plugin_id!r}
    config_model: ClassVar[type[BaseModel]] = Params

    def __init__(self, device_id: str, params: BaseModel) -> None:
        if not isinstance(params, Params):
            raise TypeError("params were not validated against this plugin's schema")
        self.device_id = device_id
        self.params = params
        self.read_calls = 0

    async def validate_connection(self, timeout_s: float) -> ValidationResult:
        return ValidationResult.success()

    async def read_power(self, timeout_s: float) -> ReadResult:
        self.read_calls += 1
        if self.params.always_fail or self.read_calls <= self.params.failures_before_success:
            credential = getattr(self.params, CREDENTIAL_FIELD).get_secret_value()
            return ReadFailure(
                device_id=self.device_id,
                classification=self.params.classification,
                detail=f"synthetic failure carried credential {{credential}}",
            )
        return PowerReading(
            device_id=self.device_id,
            watts=self.params.watts,
            measured_at=BASE_READING_AT + timedelta(seconds=self.params.measured_after_s),
        )
'''


@contextmanager
def _importable_plugin_package(source: str) -> Iterator[ModuleType]:
    """Import one real package from local temporary storage and remove it afterwards."""
    with TemporaryDirectory() as location:
        root = Path(location)
        package_name = f"property37_plugins_{next(PACKAGE_SEQUENCE)}"
        package_dir = root / package_name
        package_dir.mkdir()
        (package_dir / "__init__.py").write_text("", encoding="utf-8")
        (package_dir / "generated.py").write_text(source, encoding="utf-8")

        sys.path.insert(0, str(root))
        importlib.invalidate_caches()
        try:
            yield importlib.import_module(package_name)
        finally:
            loaded_names = [name for name in sys.modules if name == package_name or name.startswith(f"{package_name}.")]
            for name in loaded_names:
                del sys.modules[name]
            if str(root) in sys.path:
                sys.path.remove(str(root))
            importlib.invalidate_caches()


class _RecordHandler(logging.Handler):
    """Capture records after the production credential filter has scrubbed them."""

    def __init__(self, credential_registry: CredentialRegistry) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []
        self.addFilter(CredentialRedactionFilter(credential_registry))

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextmanager
def _recording_logger(
    credential_registry: CredentialRegistry,
) -> Iterator[tuple[logging.Logger, list[logging.LogRecord]]]:
    """Isolate one example's discovery, parser, scheduler, and redaction records."""
    logger = logging.getLogger(LOGGER_NAME)
    saved_handlers = list(logger.handlers)
    saved_filters = list(logger.filters)
    saved_level = logger.level
    saved_propagate = logger.propagate
    logger.handlers.clear()
    logger.filters.clear()
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    handler = _RecordHandler(credential_registry)
    logger.addHandler(handler)
    try:
        yield logger, handler.records
    finally:
        logger.handlers.clear()
        logger.handlers.extend(saved_handlers)
        logger.filters.clear()
        logger.filters.extend(saved_filters)
        logger.setLevel(saved_level)
        logger.propagate = saved_propagate


@dataclass(frozen=True, slots=True)
class Published:
    """One MQTT publication captured at the broker-client boundary."""

    topic: str
    payload: str
    qos: int
    retain: bool


class RecordingClient:
    """A broker-free client that records every publication in order."""

    def __init__(self) -> None:
        self.publishes: list[Published] = []

    async def publish(self, topic: str, payload: str, qos: int = 0, retain: bool = False) -> None:
        self.publishes.append(Published(topic, payload, qos, retain))


class _RecordingConnection(AbstractAsyncContextManager[BrokerClient]):
    """One successful recording connection produced by the factory."""

    def __init__(self, owner: RecordingFactory) -> None:
        self._owner = owner

    async def __aenter__(self) -> BrokerClient:
        client = RecordingClient()
        self._owner.clients.append(client)
        return client

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        return False


class RecordingFactory:
    """Record connection parameters while guaranteeing that no socket is opened."""

    def __init__(self) -> None:
        self.connections: list[BrokerConnection] = []
        self.clients: list[RecordingClient] = []

    def __call__(self, connection: BrokerConnection) -> AbstractAsyncContextManager[BrokerClient]:
        self.connections.append(connection)
        return _RecordingConnection(self)


class VirtualClock:
    """A monotonic scheduler clock advanced only by injected retry sleeps."""

    def __init__(self) -> None:
        self.now_s = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now_s

    async def sleep(self, delay_s: float) -> None:
        self.sleeps.append(delay_s)
        self.now_s += delay_s


@asynccontextmanager
async def _virtual_timeout(timeout_s: float) -> AsyncIterator[None]:
    """An immediate-operation timeout boundary that never consults a real clock."""
    assert timeout_s > 0
    yield


async def _unexpected_reconnect_sleep(delay_s: float) -> None:
    """A successful recording connection must never enter reconnect backoff."""
    raise AssertionError(f"recording publisher unexpectedly requested a {delay_s:g}s reconnect sleep")


async def _settle_connected(publisher: MqttPublisher) -> None:
    """Yield turns, not time, until the recording connection has published discovery."""
    for _ in range(50):
        if publisher.connected:
            return
        await asyncio.sleep(0)
    raise AssertionError("recording publisher did not connect within 50 event-loop turns")


def _credential_windows(value: str) -> frozenset[str]:
    return frozenset(value[index : index + 4] for index in range(len(value) - 3))


def _string_leaves(value: object) -> Iterator[str]:
    """Yield every string reachable in a record's structured fields."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield from _string_leaves(key)
            yield from _string_leaves(item)
    elif isinstance(value, list | tuple | set | frozenset):
        for item in value:
            yield from _string_leaves(item)


def _assert_credential_absent(values: Iterator[str], credential: str, *, sink: str) -> None:
    """Requirement 8's four-character rule, over one observed sink."""
    windows = _credential_windows(credential)
    for value in values:
        surviving = windows.intersection(_credential_windows(value))
        assert not surviving, f"credential fragment {min(surviving)!r} reached {sink}: {value!r}"


def _schema_map(discovered: DiscoveryResult) -> dict[str, type[BaseModel]]:
    return {plugin_id: plugin_cls.config_model for plugin_id, plugin_cls in discovered.plugins.items()}


def _raw_options(scenario: Scenario) -> str:
    return json.dumps(
        {
            "poll_interval_s": scenario.interval_s,
            "mqtt": {"host": "recording-broker", "port": 1883},
            "devices": [
                {
                    "device_id": scenario.device_id,
                    "plugin": scenario.plugin_id,
                    "display_name": f"Synthetic {scenario.device_id}",
                    "poll_interval_s": scenario.interval_s,
                    "params": scenario.params(),
                }
            ],
        },
        allow_nan=False,
    )


def _publishes_for(records: list[Published], topic: str) -> tuple[Published, ...]:
    return tuple(record for record in records if record.topic == topic)


async def _exercise_core(
    scenario: Scenario,
    discovered: DiscoveryResult,
    logger: logging.Logger,
    records: list[logging.LogRecord],
    credential_registry: CredentialRegistry,
) -> None:
    """Cross every core boundary and compare it with the independently generated scenario."""
    plugin_cls = discovered.plugins[scenario.plugin_id]

    # Establish the premise with the suite plugin authors actually ship and run. The rest of this
    # function does not use its findings as an oracle; every expected effect comes from Scenario.
    report = await assert_plugin_conforms_async(
        plugin_cls,
        valid_params=scenario.params(for_conformance=True),
        invalid_params=scenario.incomplete_params(),
        device_id=scenario.device_id,
        timeout_s=0.1,
        require_reading=True,
    )
    assert report.ok

    parsed = parse_options(
        _raw_options(scenario),
        schemas=_schema_map(discovered),
        logger=logger,
        registry=credential_registry,
    )
    assert parsed.fatal is None
    assert parsed.rejections == ()
    assert parsed.config is not None
    assert len(parsed.config.devices) == 1
    entry = parsed.config.devices[0]
    assert entry.plugin == scenario.plugin_id
    assert entry.params == scenario.params()

    # The parser must derive credential handling from the generated schema marker. This probe is
    # intentionally logged only after parsing registered the value, and uses the real filter.
    protected_record_start = len(records)
    logger.warning(
        "generic credential probe: %s",
        scenario.credential,
        extra={"credential_probe": scenario.credential},
    )
    probe = records[-1]
    assert probe.getMessage() == f"generic credential probe: {REDACTION_MARKER}"
    assert probe.__dict__["credential_probe"] == REDACTION_MARKER
    assert credential_registry.redact(scenario.credential) == REDACTION_MARKER

    registry = PluginRegistry.from_discovery(discovered, logger=logger)
    assert registry.resolve(scenario.plugin_id) is plugin_cls
    instantiated = registry.instantiate_all(parsed.config.devices)
    assert instantiated.rejections == ()
    assert len(instantiated.devices) == 1
    bound = instantiated.devices[0]
    assert bound.device_id == scenario.device_id
    assert bound.plugin_id == scenario.plugin_id

    generated_plugin = cast(_GeneratedPluginState, bound.plugin)
    params = generated_plugin.params
    assert isinstance(params, plugin_cls.config_model)
    credential_value = getattr(params, scenario.credential_field)
    assert isinstance(credential_value, SecretStr)
    assert credential_value.get_secret_value() == scenario.credential

    interval = resolve_interval(entry.poll_interval_s, parsed.config.poll_interval_s)
    assert not interval.rejected
    timeout_s = resolve_timeout(None, interval.interval_s)
    device = bound.with_schedule(interval.interval_s, timeout_s)

    accumulator = EnergyAccumulator()
    accumulator.register(device.device_id, device.interval_s)
    baseline = PowerReading(
        device_id=device.device_id,
        watts=scenario.baseline_watts,
        measured_at=BASE_READING_AT,
    )
    accumulator.record(baseline)
    state_before_cycle = accumulator.state_of(device.device_id)

    mqtt_settings: MqttSettings = parsed.config.mqtt
    publishable = PublishableDevice(
        device_id=device.device_id,
        display_name=entry.display_name,
        plugin_id=device.plugin_id,
    )
    factory = RecordingFactory()
    publisher = MqttPublisher(
        mqtt_settings,
        (publishable,),
        client_factory=factory,
        sleep=_unexpected_reconnect_sleep,
        energy_total_kwh=accumulator.total_kwh,
        now=lambda: scenario.measured_at,
        logger=logger,
    )
    await publisher.start()
    try:
        await _settle_connected(publisher)
        assert len(factory.connections) == 1
        assert len(factory.clients) == 1
        client = factory.clients[0]
        published_before_cycle = len(client.publishes)

        clock = VirtualClock()
        executor = PollCycleExecutor(
            sink=AccumulateThenPublish(accumulator, publisher),
            clock=clock,
            sleep=clock.sleep,
            timeout=_virtual_timeout,
            logger=logger,
        )
        cycle = await executor.run_cycle(device)
        cycle_publishes = client.publishes[published_before_cycle:]
    finally:
        await publisher.stop()

    assert cycle.attempt_count == scenario.expected_attempts
    assert generated_plugin.read_calls == scenario.expected_attempts
    assert len(clock.sleeps) == scenario.expected_attempts - 1

    failure_records = [
        record
        for record in records[protected_record_start:]
        if record.getMessage().startswith("power reading failed:")
        and record.__dict__.get("device_id") == scenario.device_id
    ]
    assert len(failure_records) == scenario.expected_failed_attempts
    for failure_record in failure_records:
        assert failure_record.__dict__["detail"] == f"synthetic failure carried credential {REDACTION_MARKER}"

    topics = device_topics_for(mqtt_settings, scenario.device_id)
    power_publishes = _publishes_for(cycle_publishes, topics.power_state)
    energy_publishes = _publishes_for(cycle_publishes, topics.energy_state)
    attribute_publishes = _publishes_for(cycle_publishes, topics.power_attributes)
    availability_publishes = _publishes_for(cycle_publishes, topics.availability)

    if scenario.succeeds:
        assert cycle.succeeded
        assert cycle.reading == PowerReading(
            device_id=scenario.device_id,
            watts=scenario.watts,
            measured_at=scenario.measured_at,
        )
        expected_total = interval_kwh(baseline, cycle.reading)
        state_after_cycle = accumulator.state_of(device.device_id)
        assert state_after_cycle.total_kwh == expected_total
        assert state_after_cycle.baseline_watts == scenario.watts
        assert state_after_cycle.baseline_at == scenario.measured_at
        assert tuple(record.payload for record in power_publishes) == (f"{scenario.watts:.1f}",)
        assert tuple(record.payload for record in energy_publishes) == (f"{expected_total:.6f}",)
        assert len(attribute_publishes) == 1
        assert json.loads(attribute_publishes[0].payload) == {"duration_ms": 0}
        assert tuple(record.payload for record in availability_publishes) == (PAYLOAD_AVAILABLE,)
    else:
        assert not cycle.succeeded
        assert cycle.failure is not None
        assert cycle.failure.classification is scenario.failure_classification
        assert accumulator.state_of(device.device_id) == state_before_cycle
        assert power_publishes == ()
        assert energy_publishes == ()
        assert attribute_publishes == ()
        assert tuple(record.payload for record in availability_publishes) == (PAYLOAD_NOT_AVAILABLE,)

    protected_records = records[protected_record_start:]
    _assert_credential_absent(
        (leaf for record in protected_records for leaf in _string_leaves(record.__dict__)),
        scenario.credential,
        sink="the core log stream",
    )
    _assert_credential_absent(
        (leaf for publication in factory.clients[0].publishes for leaf in (publication.topic, publication.payload)),
        scenario.credential,
        sink="the recording MQTT publisher",
    )


# Feature: remote-energy-meter, Property 37: Any conforming plugin works against the unmodified core
# Validates: Requirements 10.3
@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(scenario=plugin_scenarios())
def test_any_conforming_plugin_works_against_the_unmodified_core(scenario: Scenario) -> None:
    """Discover and run an unknown family without a parser/scheduler/energy/publisher adapter.

    **Validates: Requirements 10.3**
    """
    credential_registry = CredentialRegistry()
    with (
        _importable_plugin_package(_plugin_module_source(scenario)) as package,
        _recording_logger(credential_registry) as (logger, records),
    ):
        discovered = discover(package, logger=logger)
        assert discovered.fatal is None
        assert discovered.excluded == ()
        assert discovered.plugin_ids == (scenario.plugin_id,)

        discovery_records = [
            record
            for record in records
            if record.getMessage().startswith("device plugin discovered:")
            and record.__dict__.get("plugin_id") == scenario.plugin_id
        ]
        assert len(discovery_records) == 1
        asyncio.run(_exercise_core(scenario, discovered, logger, records, credential_registry))
