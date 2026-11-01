"""Property tests for MQTT power-state publication (Requirements 6.4 and 6.14).

The publisher runs against the recording fake client from its unit tests. No socket or broker is
created: the real connection supervisor receives the fake factory, while state publication runs
through the public ``deliver_reading`` boundary.
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Final

from hypothesis import given, settings
from hypothesis import strategies as st

from remote_energy_meter.mqtt.publisher import MAX_PUBLISHABLE_WATTS, MIN_PUBLISHABLE_WATTS, MqttPublisher
from remote_energy_meter.mqtt.topics import device_topics_for
from tests.unit.test_mqtt_publisher import (
    DEVICE_ID,
    FakeClient,
    FakeFactory,
    FakeSleep,
    a_reading,
    as_device,
    make_devices,
    make_settings,
    settle,
)

SEED_WATTS: Final = 4321.0
SEED_PAYLOAD: Final = "4321.0"
JUST_BELOW_MIN_WATTS: Final = math.nextafter(MIN_PUBLISHABLE_WATTS, -math.inf)
JUST_ABOVE_MAX_WATTS: Final = math.nextafter(MAX_PUBLISHABLE_WATTS, math.inf)

#: Explicit boundaries, nearest outside values, representative negatives/non-finite values, and
#: decimal cases immediately below, on, and above rounding ties. Broad strategies below fill the
#: intervals between these points.
TARGETED_WATTS: Final[tuple[float, ...]] = (
    MIN_PUBLISHABLE_WATTS,
    math.nextafter(MIN_PUBLISHABLE_WATTS, math.inf),
    MAX_PUBLISHABLE_WATTS,
    math.nextafter(MAX_PUBLISHABLE_WATTS, -math.inf),
    JUST_BELOW_MIN_WATTS,
    JUST_ABOVE_MAX_WATTS,
    -0.1,
    -1.0,
    -100_000.0,
    float("nan"),
    float("inf"),
    float("-inf"),
    0.04,
    0.05,
    0.06,
    120.44,
    120.45,
    120.46,
    99_999.94,
    99_999.95,
    99_999.96,
)


class _RecordHandler(logging.Handler):
    """Capture one Hypothesis example's publisher records without shared pytest state."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@st.composite
def rounding_values(draw: st.DrawFn) -> float:
    """Finite in-range values surrounding one-decimal rounding boundaries."""
    whole = draw(st.integers(min_value=0, max_value=99_999))
    hundredths = draw(st.sampled_from((4, 5, 6, 14, 15, 16, 24, 25, 26, 84, 85, 86, 94, 95, 96)))
    return float(whole) + hundredths / 100.0


def power_values() -> st.SearchStrategy[float]:
    """All usability partitions, with dense coverage around the range and rounding boundaries."""
    finite_in_range = st.floats(
        min_value=MIN_PUBLISHABLE_WATTS,
        max_value=MAX_PUBLISHABLE_WATTS,
        allow_nan=False,
        allow_infinity=False,
        width=64,
    )
    finite_negative = st.floats(
        max_value=JUST_BELOW_MIN_WATTS,
        allow_nan=False,
        allow_infinity=False,
        width=64,
    )
    finite_above_range = st.floats(
        min_value=JUST_ABOVE_MAX_WATTS,
        allow_nan=False,
        allow_infinity=False,
        width=64,
    )
    return st.one_of(
        st.sampled_from(TARGETED_WATTS),
        finite_in_range,
        finite_negative,
        finite_above_range,
        rounding_values(),
    )


def _power_state_payloads(client: FakeClient, topic: str) -> tuple[str, ...]:
    return tuple(record.payload for record in client.publishes if record.topic == topic)


async def _observe_publication(
    watts: float,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[logging.LogRecord, ...]]:
    """Seed a valid state, deliver ``watts``, and return state-topic and log observations."""
    logger = logging.getLogger("remote_energy_meter_property_publisher")
    saved_level, saved_propagate = logger.level, logger.propagate
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    recorder = _RecordHandler()
    logger.addHandler(recorder)

    mqtt = make_settings()
    factory = FakeFactory()
    publisher = MqttPublisher(
        mqtt,
        make_devices(DEVICE_ID),
        client_factory=factory,
        sleep=FakeSleep(),
        logger=logger,
    )
    await publisher.start()
    try:
        assert await settle(lambda: publisher.connected), "the fake client did not connect"
        client = factory.clients[0]
        power_topic = device_topics_for(mqtt, DEVICE_ID).power_state

        await publisher.deliver_reading(as_device(), a_reading(SEED_WATTS), duration_ms=10)
        seeded_states = _power_state_payloads(client, power_topic)
        publish_count_before_value = len(client.publishes)

        await publisher.deliver_reading(as_device(), a_reading(watts), duration_ms=20)
        new_states = tuple(
            record.payload for record in client.publishes[publish_count_before_value:] if record.topic == power_topic
        )
        final_states = _power_state_payloads(client, power_topic)
        return seeded_states, new_states, final_states, tuple(recorder.records)
    finally:
        await publisher.stop()
        logger.removeHandler(recorder)
        logger.setLevel(saved_level)
        logger.propagate = saved_propagate


# Feature: remote-energy-meter, Property 21: Power state is published if and only if the value is usable
# Validates: Requirements 6.4, 6.14
@settings(max_examples=150, deadline=None)
@given(watts=power_values())
def test_power_state_is_published_if_and_only_if_the_value_is_usable(watts: float) -> None:
    """Publish exactly one rounded state for usable watts; preserve the prior state otherwise.

    **Validates: Requirements 6.4, 6.14**
    """
    seeded_states, new_states, final_states, records = asyncio.run(_observe_publication(watts))
    rejection_records = [record for record in records if "power value not publishable" in record.getMessage()]

    assert seeded_states == (SEED_PAYLOAD,), "the control state was not seeded exactly once"
    usable = math.isfinite(watts) and MIN_PUBLISHABLE_WATTS <= watts <= MAX_PUBLISHABLE_WATTS
    if usable:
        expected_payload = f"{watts:.1f}"
        assert new_states == (expected_payload,), "a usable value must add exactly one rounded power state"
        assert final_states == (SEED_PAYLOAD, expected_payload)
        assert rejection_records == [], "a usable value must not be logged as rejected"
        return

    assert new_states == (), "a rejected value must not add a power state"
    assert final_states == seeded_states, "a rejected value changed the seeded prior power state"
    assert len(rejection_records) == 1, "a rejected value must produce exactly one rejection record"
    rejection = rejection_records[0]
    assert rejection.__dict__.get("device_id") == DEVICE_ID
    assert rejection.__dict__.get("rejected_watts") == repr(watts)
    assert repr(watts) in rejection.getMessage()


# --------------------------------------------------------------------------------------------------
# the reconnect supervisor (Requirement 6.9)
# --------------------------------------------------------------------------------------------------


class _NeverConnectingFactory(FakeFactory):
    """A :class:`FakeFactory` variant whose connection attempts always fail."""

    async def __aenter__(self) -> FakeClient:
        raise OSError("connection refused")


@st.composite
def reconnect_failure_scenarios(draw: st.DrawFn) -> tuple[int, bool]:
    """Finite failures before recovery, or a generated prefix of an endless outage."""
    eventually_succeeds = draw(st.booleans())
    if eventually_succeeds:
        failure_count = draw(st.integers(min_value=0, max_value=96))
    else:
        # Every endless-outage example reaches beyond the first capped delay. Larger generated
        # prefixes would expose any hidden attempt limit in the real supervisor loop.
        failure_count = draw(st.integers(min_value=8, max_value=128))
    return failure_count, eventually_succeeds


def _expected_reconnect_delays(failure_count: int) -> tuple[float, ...]:
    """Requirement-derived oracle, independent of the production backoff helper."""
    delays: list[float] = []
    next_delay = 1.0
    for _ in range(failure_count):
        delays.append(next_delay)
        next_delay = min(next_delay * 2.0, 60.0)
    return tuple(delays)


async def _observe_reconnect_schedule(
    failure_count: int,
    eventually_succeeds: bool,
) -> tuple[tuple[float, ...], int, bool, int]:
    """Run the real supervisor until recovery or the requested endless-outage prefix."""
    factory = FakeFactory(failures=failure_count) if eventually_succeeds else _NeverConnectingFactory()
    # A recovering run never reaches this limit because it stops sleeping after ``failure_count``
    # failures. An endless run is cancelled by FakeSleep exactly at the generated prefix length.
    sleep = FakeSleep(limit=failure_count + 1 if eventually_succeeds else failure_count)
    logger = logging.getLogger("remote_energy_meter_property_reconnect")
    saved_disabled = logger.disabled
    logger.disabled = True
    publisher = MqttPublisher(
        make_settings(),
        make_devices(DEVICE_ID),
        client_factory=factory,
        sleep=sleep,
        logger=logger,
    )

    try:
        await publisher.start()
        if eventually_succeeds:
            progressed = await settle(
                lambda: publisher.connected,
                turns=max(200, failure_count * 4 + 20),
            )
        else:
            progressed = await settle(
                lambda: len(sleep.delays) >= failure_count,
                turns=max(200, failure_count * 4 + 20),
            )
        assert progressed, "the supervisor stopped before reaching the generated connection outcome"
        return tuple(sleep.delays), factory.attempts, publisher.connected, len(factory.clients)
    finally:
        await publisher.stop()
        logger.disabled = saved_disabled


# Feature: remote-energy-meter, Property 23: Reconnect delays double from 1 second and cap at 60 seconds
# Validates: Requirements 6.9
@settings(max_examples=150, deadline=None)
@given(scenario=reconnect_failure_scenarios())
def test_reconnect_delays_double_from_one_second_and_cap_at_sixty(
    scenario: tuple[int, bool],
) -> None:
    """The real supervisor follows the exact unbounded schedule without wall-clock sleeps.

    **Validates: Requirements 6.9**
    """
    failure_count, eventually_succeeds = scenario
    delays, attempts, connected, successful_connections = asyncio.run(
        _observe_reconnect_schedule(failure_count, eventually_succeeds)
    )

    assert delays == _expected_reconnect_delays(failure_count)
    if eventually_succeeds:
        assert attempts == failure_count + 1, "the publisher gave up before the generated successful attempt"
        assert connected is True
        assert successful_connections == 1
        return

    assert attempts == failure_count, "the publisher stopped before the generated endless-outage prefix ended"
    assert connected is False
    assert successful_connections == 0


# --------------------------------------------------------------------------------------------------
# the retained reading buffer (Requirement 6.10)
# --------------------------------------------------------------------------------------------------

_MICROSECONDS_PER_SECOND: Final = 1_000_000
_MAX_EARLIER_READING_OFFSET_US: Final = 24 * 60 * 60 * _MICROSECONDS_PER_SECOND
_MAX_EXTRA_RECONNECT_AGE_US: Final = 7 * 24 * 60 * 60 * _MICROSECONDS_PER_SECOND

type _DisconnectedReadingCase = tuple[float, int, int]
type _BufferedDeviceCase = tuple[str, int, tuple[_DisconnectedReadingCase, ...]]
type _RetainedSnapshot = tuple[float, int, int, float]
type _RetainedFlushObservation = tuple[
    dict[str, _RetainedSnapshot],
    dict[str, tuple[str, ...]],
    dict[str, tuple[str, ...]],
    int,
    bool,
]


@st.composite
def disconnected_reading_scenarios(draw: st.DrawFn) -> tuple[_BufferedDeviceCase, ...]:
    """Multiple devices with chronological reading sequences and adversarial reconnect ages.

    The first device is exactly at its own 4x-interval boundary and the second is one microsecond
    beyond its independently generated boundary. Distinct generated intervals make those targeted
    ages exercise per-device freshness rather than one global threshold.
    """
    device_count = draw(st.integers(min_value=2, max_value=5))
    intervals = draw(
        st.lists(
            st.integers(min_value=5, max_value=3600),
            min_size=device_count,
            max_size=device_count,
            unique=True,
        )
    )
    scenarios: list[_BufferedDeviceCase] = []

    for index, interval_s in enumerate(intervals):
        threshold_us = 4 * interval_s * _MICROSECONDS_PER_SECOND
        if index == 0:
            latest_age_us = threshold_us
        elif index == 1:
            latest_age_us = threshold_us + 1
        else:
            latest_age_us = draw(
                st.integers(
                    min_value=0,
                    max_value=threshold_us + _MAX_EXTRA_RECONNECT_AGE_US,
                )
            )

        reading_count = draw(st.integers(min_value=2, max_value=7))
        watts_values = draw(
            st.lists(
                st.integers(min_value=0, max_value=100_000),
                min_size=reading_count,
                max_size=reading_count,
                unique=True,
            )
        )
        durations_ms = draw(
            st.lists(
                st.integers(min_value=0, max_value=60_000),
                min_size=reading_count,
                max_size=reading_count,
            )
        )
        earlier_offsets_us = sorted(
            draw(
                st.lists(
                    st.integers(min_value=1, max_value=_MAX_EARLIER_READING_OFFSET_US),
                    min_size=reading_count - 1,
                    max_size=reading_count - 1,
                    unique=True,
                )
            ),
            reverse=True,
        )
        ages_us = [latest_age_us + offset_us for offset_us in earlier_offsets_us]
        ages_us.append(latest_age_us)
        readings = tuple(
            (float(watts), duration_ms, age_us)
            for watts, duration_ms, age_us in zip(watts_values, durations_ms, ages_us, strict=True)
        )
        scenarios.append((f"buffered_device_{index}", interval_s, readings))

    return tuple(scenarios)


async def _observe_retained_reading_flush(
    scenarios: tuple[_BufferedDeviceCase, ...],
) -> _RetainedFlushObservation:
    """Buffer every generated reading, reconnect once, then attempt a second drain."""
    from datetime import timedelta

    from remote_energy_meter.plugins.interface import PowerReading

    mqtt = make_settings()
    factory = FakeFactory()
    reconnect_at = a_reading(0.0).measured_at
    logger = logging.getLogger("remote_energy_meter_property_retained_buffer")
    saved_disabled = logger.disabled
    logger.disabled = True
    publisher = MqttPublisher(
        mqtt,
        make_devices(*(device_id for device_id, _, _ in scenarios)),
        client_factory=factory,
        sleep=FakeSleep(),
        now=lambda: reconnect_at,
        logger=logger,
    )

    try:
        for device_id, interval_s, readings in scenarios:
            device = as_device(device_id, interval_s)
            for watts, duration_ms, age_us in readings:
                reading = PowerReading(
                    device_id=device_id,
                    watts=watts,
                    measured_at=reconnect_at - timedelta(microseconds=age_us),
                )
                await publisher.deliver_reading(device, reading, duration_ms)

        assert factory.clients == [], "disconnected delivery must not create or use a broker client"
        microsecond = timedelta(microseconds=1)
        retained_before = {
            device_id: (
                held.reading.watts,
                (reconnect_at - held.reading.measured_at) // microsecond,
                held.duration_ms,
                held.interval_s,
            )
            for device_id, held in publisher._buffered.items()
        }

        await publisher.start()
        assert await settle(lambda: publisher.connected and not publisher._buffered), (
            "the fake reconnect did not drain the retained-reading buffer"
        )
        client = factory.clients[0]
        state_payloads: dict[str, tuple[str, ...]] = {}
        availability_payloads: dict[str, tuple[str, ...]] = {}
        for device_id, _, _ in scenarios:
            topics = device_topics_for(mqtt, device_id)
            state_payloads[device_id] = tuple(
                record.payload for record in client.publishes if record.topic == topics.power_state
            )
            availability_payloads[device_id] = tuple(
                record.payload for record in client.publishes if record.topic == topics.availability
            )

        publishes_before_second_flush = len(client.publishes)
        await publisher._flush_buffered_readings(client)
        second_flush_publish_count = len(client.publishes) - publishes_before_second_flush
        return (
            retained_before,
            state_payloads,
            availability_payloads,
            second_flush_publish_count,
            not publisher._buffered,
        )
    finally:
        await publisher.stop()
        logger.disabled = saved_disabled


# Feature: remote-energy-meter, Property 24: At most one reading is retained per device and published only while fresh
# Validates: Requirements 6.10
@settings(max_examples=150, deadline=None)
@given(scenarios=disconnected_reading_scenarios())
def test_at_most_one_reading_is_retained_per_device_and_published_only_while_fresh(
    scenarios: tuple[_BufferedDeviceCase, ...],
) -> None:
    """Retain only each device's latest reading and drain it once iff its exact age is fresh.

    **Validates: Requirements 6.10**
    """
    from datetime import timedelta

    retained, states, availabilities, second_flush_publish_count, buffer_empty = asyncio.run(
        _observe_retained_reading_flush(scenarios)
    )

    assert set(retained) == {device_id for device_id, _, _ in scenarios}
    for device_id, interval_s, readings in scenarios:
        latest_watts, latest_duration_ms, latest_age_us = readings[-1]
        assert retained[device_id] == (
            latest_watts,
            latest_age_us,
            latest_duration_ms,
            float(interval_s),
        ), "the retained slot must contain only the last reading delivered for that device"

        # This oracle is intentionally derived from Requirement 6.10 in exact timedelta arithmetic.
        # It does not call the production gap-threshold helper used by the publisher.
        age = timedelta(microseconds=latest_age_us)
        freshness_limit = timedelta(seconds=4 * interval_s)
        expected_fresh = age <= freshness_limit
        expected_states = (f"{latest_watts:.1f}",) if expected_fresh else ()
        expected_availability = ("online",) if expected_fresh else ()

        assert len(states[device_id]) <= 1, "one retained slot must be flushed at most once"
        assert len(availabilities[device_id]) <= 1, "one retained slot must report availability at most once"
        assert states[device_id] == expected_states
        assert availabilities[device_id] == expected_availability

    boundary_device = scenarios[0]
    boundary_age_us = boundary_device[2][-1][2]
    assert timedelta(microseconds=boundary_age_us) == timedelta(seconds=4 * boundary_device[1])
    beyond_device = scenarios[1]
    beyond_age_us = beyond_device[2][-1][2]
    assert timedelta(microseconds=beyond_age_us) == (
        timedelta(seconds=4 * beyond_device[1]) + timedelta(microseconds=1)
    )
    assert second_flush_publish_count == 0, "a drained reading must not be published by a later flush"
    assert buffer_empty, "the retained-reading buffer must be empty after reconnect"


# --------------------------------------------------------------------------------------------------
# migration cleanup and inventory ordering (Requirement B.4)
# --------------------------------------------------------------------------------------------------

type _RediscoveryDeviceCase = tuple[str, int]
type _BufferedReconnectReading = tuple[int, float, int]
type _RediscoveryScenario = tuple[
    tuple[_RediscoveryDeviceCase, ...],
    tuple[tuple[_BufferedReconnectReading, ...], ...],
]


@st.composite
def rediscovery_order_scenarios(draw: st.DrawFn) -> _RediscoveryScenario:
    """Generate the complete Property 40 domain: zero to fifty devices and one or more connections."""
    device_count = draw(st.integers(min_value=0, max_value=50))
    reconnect_count = draw(st.integers(min_value=1, max_value=6))
    intervals = draw(
        st.lists(
            st.integers(min_value=5, max_value=3600),
            min_size=device_count,
            max_size=device_count,
        )
    )
    devices = tuple((f"rediscovery_device_{index}", interval_s) for index, interval_s in enumerate(intervals))

    reconnects: list[tuple[_BufferedReconnectReading, ...]] = []
    for _reconnect_index in range(reconnect_count):
        selected_indices = (
            draw(
                st.sets(
                    st.integers(min_value=0, max_value=device_count - 1),
                    min_size=0,
                    max_size=device_count,
                )
            )
            if device_count
            else set()
        )
        buffered: list[_BufferedReconnectReading] = []
        for device_index in sorted(selected_indices):
            watts = float(draw(st.integers(min_value=0, max_value=100_000)))
            duration_ms = draw(st.integers(min_value=0, max_value=60_000))
            buffered.append((device_index, watts, duration_ms))
        reconnects.append(tuple(buffered))

    return devices, tuple(reconnects)


async def _observe_rediscovery_order(scenario: _RediscoveryScenario) -> tuple[FakeClient, ...]:
    """Run every generated reconnect through the publisher's real on-connect sequence."""
    devices, reconnects = scenario
    reconnect_at = a_reading(0.0).measured_at
    publisher = MqttPublisher(
        make_settings(),
        make_devices(*(device_id for device_id, _ in devices)),
        client_factory=FakeFactory(),
        sleep=FakeSleep(),
        energy_total_kwh=lambda _device_id: 1.0,
        now=lambda: reconnect_at,
    )
    clients: list[FakeClient] = []

    try:
        for reconnect_index, buffered in enumerate(reconnects):
            publisher._connected.clear()
            for device_index, watts, duration_ms in buffered:
                device_id, interval_s = devices[device_index]
                await publisher.deliver_reading(
                    as_device(device_id, interval_s),
                    a_reading(watts, device_id),
                    duration_ms,
                )

            expected_buffered_ids = {devices[device_index][0] for device_index, _, _ in buffered}
            assert set(publisher._buffered) == expected_buffered_ids, (
                f"reconnect {reconnect_index} did not retain exactly its generated device subset"
            )

            client = FakeClient()
            clients.append(client)
            await publisher._on_connected(client)
            assert not publisher._buffered, f"reconnect {reconnect_index} did not drain its fresh readings"
    finally:
        await publisher.stop()

    return tuple(clients)


# Feature: remote-energy-meter, Property 40: On every connection, all legacy retained discovery
# deletions precede the one retained inventory, which precedes every state or availability publish.
# **Validates: Requirements B.4**
@settings(max_examples=150, deadline=None)
@given(scenario=rediscovery_order_scenarios())
def test_cleanup_then_inventory_precedes_state_on_every_connection(
    scenario: _RediscoveryScenario,
) -> None:
    """Migration and companion inventory ordering hold independently on every connection."""
    import json

    from remote_energy_meter.mqtt.topics import bridge_availability_topic, integration_inventory_topic

    devices, reconnects = scenario
    clients = asyncio.run(_observe_rediscovery_order(scenario))
    mqtt = make_settings()
    bridge_topic = bridge_availability_topic(mqtt.base_topic)
    inventory_topic = integration_inventory_topic(mqtt.base_topic)
    topics_by_device = {device_id: device_topics_for(mqtt, device_id) for device_id, _ in devices}
    cleanup_topics = {
        topic for topics in topics_by_device.values() for topic in (topics.power_discovery, topics.energy_discovery)
    }
    state_or_availability_topics = {bridge_topic}
    for topics in topics_by_device.values():
        state_or_availability_topics.update(
            (topics.power_state, topics.energy_state, topics.power_attributes, topics.availability)
        )

    assert len(clients) == len(reconnects) >= 1
    assert len({id(client) for client in clients}) == len(clients)

    for reconnect_index, client in enumerate(clients):
        connection_topics = tuple(record.topic for record in client.publishes)
        cleanup_positions = [position for position, topic in enumerate(connection_topics) if topic in cleanup_topics]
        inventory_positions = [position for position, topic in enumerate(connection_topics) if topic == inventory_topic]
        state_positions = [
            position for position, topic in enumerate(connection_topics) if topic in state_or_availability_topics
        ]

        assert len(cleanup_positions) == 2 * len(devices)
        assert len(inventory_positions) == 1
        assert state_positions
        assert max(cleanup_positions, default=-1) < inventory_positions[0] < min(state_positions)
        for cleanup_topic in cleanup_topics:
            matching = [record for record in client.publishes if record.topic == cleanup_topic]
            assert len(matching) == 1
            assert matching[0].payload == ""
            assert matching[0].retain is True
            assert matching[0].qos == 1
        inventory = client.publishes[inventory_positions[0]]
        assert inventory.retain is True
        assert inventory.qos == 1
        assert [item["device_id"] for item in json.loads(inventory.payload)["devices"]] == [
            device_id for device_id, _ in devices
        ], f"connection {reconnect_index} inventory did not announce every device"


# --------------------------------------------------------------------------------------------------
# per-cycle scheduler-to-publisher accounting (Requirements 9.4, 9.5)
# --------------------------------------------------------------------------------------------------

_ACCOUNTING_PLUGIN_ID: Final = "accounting"
_ACCOUNTING_INTERVAL_S: Final = 30
_ACCOUNTING_TIMEOUT_S: Final = 6.0
_ACCOUNTING_ATTEMPTS: Final = 3
_ACCOUNTING_LOGGER_NAME: Final = "remote_energy_meter_property_cycle_accounting"
_ACCOUNTING_FAILURES: Final[tuple[str, ...]] = (
    "authentication",
    "timeout",
    "tls_validation",
    "communication",
    "response_format",
    "invalid_measurement",
    "plugin",
)
_ACCOUNTING_RETRYABLE_FAILURES: Final[tuple[str, ...]] = tuple(
    classification for classification in _ACCOUNTING_FAILURES if classification != "authentication"
)
_ACCOUNTING_DURATION_BOUNDARIES_MS: Final[tuple[int, ...]] = (0, 1, 2, 125, 999, 1000, 1001, 2500, 5000)


class _AccountingDeviceCase:
    """Generated outcome and per-attempt durations for one accepted device's cycle."""

    __slots__ = ("attempt_durations_ms", "failure_classification", "succeeds_at", "watts")

    def __init__(
        self,
        *,
        succeeds_at: int | None,
        attempt_durations_ms: tuple[int, ...],
        failure_classification: str,
        watts: float,
    ) -> None:
        self.succeeds_at = succeeds_at
        self.attempt_durations_ms = attempt_durations_ms
        self.failure_classification = failure_classification
        self.watts = watts

    @property
    def succeeded(self) -> bool:
        """Whether this script eventually returns a reading."""
        return self.succeeds_at is not None

    @property
    def expected_final_attempt(self) -> int:
        """The requirement-derived final attempt, without consulting ``CycleResult``."""
        if self.succeeds_at is not None:
            return self.succeeds_at
        if self.failure_classification == "authentication":
            return 1
        # Every generated duration plus both retry delays fits inside the 30 second cycle budget.
        return _ACCOUNTING_ATTEMPTS

    @property
    def expected_duration_ms(self) -> int:
        """The final operation's generated integral-millisecond duration."""
        return self.attempt_durations_ms[self.expected_final_attempt - 1]


@st.composite
def per_cycle_accounting_scenarios(draw: st.DrawFn) -> tuple[_AccountingDeviceCase, ...]:
    """Two to six devices, always including successful and fully failed completed cycles."""
    device_count = draw(st.integers(min_value=2, max_value=6))
    successful_indices = draw(
        st.sets(
            st.integers(min_value=0, max_value=device_count - 1),
            min_size=1,
            max_size=device_count - 1,
        )
    )
    duration_ms = st.one_of(
        st.sampled_from(_ACCOUNTING_DURATION_BOUNDARIES_MS),
        st.integers(min_value=0, max_value=5000),
    )
    cases: list[_AccountingDeviceCase] = []
    for index in range(device_count):
        succeeds = index in successful_indices
        succeeds_at = draw(st.integers(min_value=1, max_value=_ACCOUNTING_ATTEMPTS)) if succeeds else None
        failure_classification = draw(
            st.sampled_from(_ACCOUNTING_RETRYABLE_FAILURES if succeeds else _ACCOUNTING_FAILURES)
        )
        attempt_durations_ms = tuple(
            draw(
                st.lists(
                    duration_ms,
                    min_size=_ACCOUNTING_ATTEMPTS,
                    max_size=_ACCOUNTING_ATTEMPTS,
                )
            )
        )
        watts = draw(st.integers(min_value=0, max_value=100_000)) / 10.0
        cases.append(
            _AccountingDeviceCase(
                succeeds_at=succeeds_at,
                attempt_durations_ms=attempt_durations_ms,
                failure_classification=failure_classification,
                watts=watts,
            )
        )
    return tuple(cases)


class _AccountingClock:
    """A monotonic virtual clock; sleeping advances generated time and never waits on wall time."""

    __slots__ = ("now",)

    def __init__(self) -> None:
        self.now = 1234.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay_s: float) -> None:
        if delay_s < 0:
            raise AssertionError(f"a clock cannot sleep for a negative duration: {delay_s}")
        self.now += delay_s
        await asyncio.sleep(0)


class _NoAccountingDeadline:
    """An injected timeout context for generated operations known to finish before their timeout."""

    async def __aenter__(self) -> None:
        await asyncio.sleep(0)

    async def __aexit__(
        self,
        exc_type: object | None,
        exc_value: object | None,
        traceback: object | None,
    ) -> bool:
        del exc_type, exc_value, traceback
        await asyncio.sleep(0)
        return False


def _no_accounting_deadline(timeout_s: float) -> _NoAccountingDeadline:
    if timeout_s <= 0:
        raise AssertionError(f"a request timeout must be positive: {timeout_s}")
    return _NoAccountingDeadline()


class _AccountingPlugin:
    """A generated plugin whose real executor-visible attempts run entirely on the virtual clock."""

    __slots__ = ("_attempts", "_case", "_clock", "_device_id")

    def __init__(self, device_id: str, case: _AccountingDeviceCase, clock: _AccountingClock) -> None:
        self._device_id = device_id
        self._case = case
        self._clock = clock
        self._attempts = 0

    async def read_power(self, timeout_s: float) -> object:
        """Return the generated success or classified failure after its generated operation duration."""
        del timeout_s
        self._attempts += 1
        duration_index = min(self._attempts, _ACCOUNTING_ATTEMPTS) - 1
        await self._clock.sleep(self._case.attempt_durations_ms[duration_index] / 1000.0)
        if self._attempts == self._case.succeeds_at:
            return a_reading(self._case.watts, self._device_id)

        from remote_energy_meter.plugins.interface import FailureClassification, ReadFailure

        return ReadFailure(
            device_id=self._device_id,
            classification=FailureClassification(self._case.failure_classification),
            detail="generated failure",
        )


type _AccountingObservation = tuple[
    tuple[logging.LogRecord, ...],
    dict[str, tuple[str, ...]],
]


async def _observe_per_cycle_accounting(
    cases: tuple[_AccountingDeviceCase, ...],
) -> _AccountingObservation:
    """Run one real executor cycle per device into the real publisher and record both accounting sinks."""
    from typing import cast

    from remote_energy_meter.config.models import DeviceEntry
    from remote_energy_meter.config.runtime import ResolvedDevice
    from remote_energy_meter.plugins.interface import DevicePlugin
    from remote_energy_meter.scheduler import PollCycleExecutor

    device_ids = tuple(f"accounted_device_{index}" for index in range(len(cases)))
    mqtt = make_settings()
    factory = FakeFactory()
    logger = logging.getLogger(_ACCOUNTING_LOGGER_NAME)
    saved_level, saved_propagate, saved_disabled = logger.level, logger.propagate, logger.disabled
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.disabled = False
    recorder = _RecordHandler()
    logger.addHandler(recorder)
    publisher = MqttPublisher(
        mqtt,
        make_devices(*device_ids),
        client_factory=factory,
        sleep=FakeSleep(),
        logger=logger,
    )

    try:
        await publisher.start()
        assert await settle(lambda: publisher.connected), "the recording MQTT client did not connect"
        client = factory.clients[0]
        publish_start = len(client.publishes)
        recorder.records.clear()

        clock = _AccountingClock()
        executor = PollCycleExecutor(
            sink=publisher,
            clock=clock,
            sleep=clock.sleep,
            timeout=_no_accounting_deadline,
            logger=logger,
        )
        for device_id, case in zip(device_ids, cases, strict=True):
            plugin = _AccountingPlugin(device_id, case, clock)
            device = ResolvedDevice(
                entry=DeviceEntry(device_id=device_id, plugin=_ACCOUNTING_PLUGIN_ID, params={}),
                plugin=cast("DevicePlugin", plugin),
                interval_s=_ACCOUNTING_INTERVAL_S,
                timeout_s=_ACCOUNTING_TIMEOUT_S,
            )
            await executor.run_cycle(device)

        cycle_records = tuple(record for record in recorder.records if record.levelno == logging.DEBUG)
        cycle_publishes = client.publishes[publish_start:]
        attribute_payloads = {
            device_id: tuple(
                record.payload
                for record in cycle_publishes
                if record.topic == device_topics_for(mqtt, device_id).power_attributes
            )
            for device_id in device_ids
        }
        return cycle_records, attribute_payloads
    finally:
        await publisher.stop()
        logger.removeHandler(recorder)
        logger.setLevel(saved_level)
        logger.propagate = saved_propagate
        logger.disabled = saved_disabled


# Feature: remote-energy-meter, Property 36: Every polled device is accounted for once per cycle
# Validates: Requirements 9.4, 9.5
@settings(max_examples=150, deadline=None)
@given(cases=per_cycle_accounting_scenarios())
def test_every_polled_device_is_accounted_for_once_per_cycle(
    cases: tuple[_AccountingDeviceCase, ...],
) -> None:
    """Every completed device cycle has one debug record, and only successes have one matching MQTT duration.

    **Validates: Requirements 9.4, 9.5**
    """
    import json

    records, attribute_payloads = asyncio.run(_observe_per_cycle_accounting(cases))
    device_ids = tuple(f"accounted_device_{index}" for index in range(len(cases)))

    assert len(cases) >= 2
    assert any(case.succeeded for case in cases), "the scenario must contain a successful cycle"
    assert any(not case.succeeded for case in cases), "the scenario must contain a fully failed cycle"
    assert len(records) == len(cases), "a completed cycle must emit one debug record per polled device"

    for device_id, case in zip(device_ids, cases, strict=True):
        matching_records = [record for record in records if record.__dict__.get("device_id") == device_id]
        assert len(matching_records) == 1, f"{device_id} must be named by exactly one cycle debug record"
        record = matching_records[0]
        fields = record.__dict__
        expected_duration_ms = case.expected_duration_ms

        assert type(fields.get("duration_ms")) is int
        assert fields["duration_ms"] == expected_duration_ms
        assert f"{expected_duration_ms} ms" in record.getMessage()

        payloads = attribute_payloads[device_id]
        if case.succeeded:
            assert fields["watts"] == case.watts
            assert f"{case.watts:.1f} W" in record.getMessage()
            assert len(payloads) == 1, f"{device_id} must publish one diagnostic attribute for its successful cycle"
            assert json.loads(payloads[0]) == {"duration_ms": expected_duration_ms}
        else:
            assert fields["watts"] is None
            assert "no reading" in record.getMessage()
            assert payloads == (), f"{device_id} must not publish a success duration for a fully failed cycle"

    expected_diagnostic_count = sum(case.succeeded for case in cases)
    assert sum(len(payloads) for payloads in attribute_payloads.values()) == expected_diagnostic_count


# --------------------------------------------------------------------------------------------------
# published availability histories and graceful shutdown (Requirements 1.5, 6.7, 6.8)
# --------------------------------------------------------------------------------------------------


class _CompletedAvailabilityOutcome:
    """One successful reading or one cycle whose retries all failed."""

    __slots__ = ("classification", "duration_ms", "watts")

    def __init__(
        self,
        *,
        duration_ms: int,
        watts: float | None = None,
        classification: str | None = None,
    ) -> None:
        if (watts is None) == (classification is None):
            raise ValueError("exactly one of watts or classification must describe a completed outcome")
        self.watts = watts
        self.classification = classification
        self.duration_ms = duration_ms

    @property
    def succeeded(self) -> bool:
        """Whether this completed cycle produced a reading, usable watts or otherwise."""
        return self.watts is not None

    @property
    def expected_payload(self) -> str:
        """Availability required for this outcome."""
        return "online" if self.succeeded else "offline"


type _AvailabilityEvent = tuple[str, _CompletedAvailabilityOutcome]
type _AvailabilityScenario = tuple[tuple[str, ...], tuple[_AvailabilityEvent, ...]]
type _ObservedAvailabilityPublish = tuple[str, int, bool]
type _AvailabilityEventObservation = tuple[str, tuple[_ObservedAvailabilityPublish, ...]]
type _AvailabilityObservation = tuple[
    tuple[_AvailabilityEventObservation, ...],
    dict[str, tuple[_ObservedAvailabilityPublish, ...]],
    dict[str, tuple[_ObservedAvailabilityPublish, ...]],
    dict[str, tuple[_ObservedAvailabilityPublish, ...]],
]


def _availability_failure_classifications() -> st.SearchStrategy[str]:
    """Every final-failure classification accepted by ``ReadFailure``."""
    from remote_energy_meter.plugins.interface import FailureClassification

    return st.sampled_from(tuple(classification.value for classification in FailureClassification))


def _unusable_availability_watts() -> st.SearchStrategy[float]:
    """Values rejected as power state but still representing an answered device."""
    finite_below_range = st.floats(
        max_value=JUST_BELOW_MIN_WATTS,
        allow_nan=False,
        allow_infinity=False,
        width=64,
    )
    finite_above_range = st.floats(
        min_value=JUST_ABOVE_MAX_WATTS,
        allow_nan=False,
        allow_infinity=False,
        width=64,
    )
    return st.one_of(
        st.sampled_from(
            (
                JUST_BELOW_MIN_WATTS,
                JUST_ABOVE_MAX_WATTS,
                float("nan"),
                float("inf"),
                float("-inf"),
            )
        ),
        finite_below_range,
        finite_above_range,
    )


def _completed_availability_outcomes() -> st.SearchStrategy[_CompletedAvailabilityOutcome]:
    """Arbitrary successes and fully failed cycles for generated per-device histories."""
    duration_ms = st.integers(min_value=0, max_value=60_000)
    return st.one_of(
        st.builds(
            _CompletedAvailabilityOutcome,
            duration_ms=duration_ms,
            watts=power_values(),
        ),
        st.builds(
            _CompletedAvailabilityOutcome,
            duration_ms=duration_ms,
            classification=_availability_failure_classifications(),
        ),
    )


@st.composite
def availability_history_scenarios(draw: st.DrawFn) -> _AvailabilityScenario:
    """Interleaved non-empty histories for one to five independently tracked devices.

    Each scenario includes a publishable success, an unusable-watts success, and a fully failed
    cycle. Arbitrary history entries add transitions in either direction and exercise every failure
    classification over the generated examples.
    """
    device_count = draw(st.integers(min_value=1, max_value=5))
    device_ids = tuple(f"availability_device_{index}" for index in range(device_count))
    events: list[_AvailabilityEvent] = []

    for device_id in device_ids:
        history = draw(
            st.lists(
                _completed_availability_outcomes(),
                min_size=1,
                max_size=4,
            )
        )
        events.extend((device_id, outcome) for outcome in history)

    duration_ms = st.integers(min_value=0, max_value=60_000)
    events.extend(
        (
            (
                draw(st.sampled_from(device_ids)),
                _CompletedAvailabilityOutcome(
                    watts=draw(
                        st.floats(
                            min_value=MIN_PUBLISHABLE_WATTS,
                            max_value=MAX_PUBLISHABLE_WATTS,
                            allow_nan=False,
                            allow_infinity=False,
                            width=64,
                        )
                    ),
                    duration_ms=draw(duration_ms),
                ),
            ),
            (
                draw(st.sampled_from(device_ids)),
                _CompletedAvailabilityOutcome(
                    watts=draw(_unusable_availability_watts()),
                    duration_ms=draw(duration_ms),
                ),
            ),
            (
                draw(st.sampled_from(device_ids)),
                _CompletedAvailabilityOutcome(
                    classification=draw(_availability_failure_classifications()),
                    duration_ms=draw(duration_ms),
                ),
            ),
        )
    )

    priorities = draw(
        st.lists(
            st.integers(min_value=-1_000_000, max_value=1_000_000),
            min_size=len(events),
            max_size=len(events),
            unique=True,
        )
    )
    interleaved = tuple(event for _, event in sorted(zip(priorities, events, strict=True), key=lambda item: item[0]))
    return device_ids, interleaved


def _availability_publishes_between(
    client: FakeClient,
    topic: str,
    start: int,
    end: int | None = None,
) -> tuple[_ObservedAvailabilityPublish, ...]:
    """Return payload, QoS, and retain flag for one availability topic in a publish slice."""
    return tuple(
        (record.payload, record.qos, record.retain) for record in client.publishes[start:end] if record.topic == topic
    )


async def _observe_availability_history(scenario: _AvailabilityScenario) -> _AvailabilityObservation:
    """Feed every outcome through the public publisher API, then perform graceful shutdown."""
    from remote_energy_meter.plugins.interface import FailureClassification, ReadFailure

    device_ids, events = scenario
    mqtt = make_settings()
    factory = FakeFactory()
    logger = logging.getLogger("remote_energy_meter_property_availability")
    saved_disabled = logger.disabled
    logger.disabled = True
    publisher = MqttPublisher(
        mqtt,
        make_devices(*device_ids),
        client_factory=factory,
        sleep=FakeSleep(),
        logger=logger,
    )
    stopped = False

    try:
        await publisher.start()
        assert await settle(lambda: publisher.connected), "the recording MQTT client did not connect"
        client = factory.clients[0]
        outcomes_start = len(client.publishes)
        event_observations: list[_AvailabilityEventObservation] = []

        for device_id, outcome in events:
            event_start = len(client.publishes)
            device = as_device(device_id)
            if outcome.succeeded:
                assert outcome.watts is not None
                await publisher.deliver_reading(
                    device,
                    a_reading(outcome.watts, device_id),
                    outcome.duration_ms,
                )
            else:
                assert outcome.classification is not None
                await publisher.report_unavailable(
                    device,
                    ReadFailure(
                        device_id=device_id,
                        classification=FailureClassification(outcome.classification),
                        detail="generated final failure",
                    ),
                )

            topics = device_topics_for(mqtt, device_id)
            event_observations.append(
                (
                    device_id,
                    _availability_publishes_between(
                        client,
                        topics.availability,
                        event_start,
                        len(client.publishes),
                    ),
                )
            )

        outcomes_end = len(client.publishes)
        outcome_histories = {
            device_id: _availability_publishes_between(
                client,
                device_topics_for(mqtt, device_id).availability,
                outcomes_start,
                outcomes_end,
            )
            for device_id in device_ids
        }

        shutdown_start = len(client.publishes)
        await publisher.publish_all_offline()
        shutdown_end = len(client.publishes)
        shutdown_histories = {
            device_id: _availability_publishes_between(
                client,
                device_topics_for(mqtt, device_id).availability,
                shutdown_start,
                shutdown_end,
            )
            for device_id in device_ids
        }

        await publisher.stop()
        stopped = True
        final_histories = {
            device_id: _availability_publishes_between(
                client,
                device_topics_for(mqtt, device_id).availability,
                outcomes_start,
            )
            for device_id in device_ids
        }
        return tuple(event_observations), outcome_histories, shutdown_histories, final_histories
    finally:
        if not stopped:
            await publisher.stop()
        logger.disabled = saved_disabled


# Feature: remote-energy-meter, Property 22: Published availability reflects the most recent completed outcome
# Validates: Requirements 1.5, 6.7, 6.8
@settings(max_examples=150, deadline=None)
@given(scenario=availability_history_scenarios())
def test_published_availability_reflects_the_most_recent_completed_outcome(
    scenario: _AvailabilityScenario,
) -> None:
    """Track every completed outcome independently, then leave every accepted device offline.

    **Validates: Requirements 1.5, 6.7, 6.8**
    """
    device_ids, events = scenario
    event_observations, outcome_histories, shutdown_histories, final_histories = asyncio.run(
        _observe_availability_history(scenario)
    )
    expected_histories: dict[str, list[str]] = {device_id: [] for device_id in device_ids}

    successful_watts = [outcome.watts for _, outcome in events if outcome.succeeded and outcome.watts is not None]
    assert successful_watts, "the scenario must contain successful completed outcomes"
    assert any(
        math.isfinite(watts) and MIN_PUBLISHABLE_WATTS <= watts <= MAX_PUBLISHABLE_WATTS for watts in successful_watts
    ), "the scenario must contain a publishable successful reading"
    assert any(
        not (math.isfinite(watts) and MIN_PUBLISHABLE_WATTS <= watts <= MAX_PUBLISHABLE_WATTS)
        for watts in successful_watts
    ), "the scenario must contain an unusable-watts success"
    assert any(not outcome.succeeded for _, outcome in events), "the scenario must contain a fully failed cycle"

    assert len(event_observations) == len(events)
    for (device_id, outcome), (observed_device_id, publications) in zip(
        events,
        event_observations,
        strict=True,
    ):
        expected_payload = outcome.expected_payload
        expected_histories[device_id].append(expected_payload)
        assert observed_device_id == device_id
        assert publications == ((expected_payload, 1, True),), (
            f"{device_id} must publish exactly one retained availability value for each completed outcome"
        )

    assert set(outcome_histories) == set(device_ids)
    assert sum(len(history) for history in outcome_histories.values()) == len(events)
    for device_id in device_ids:
        expected_payloads = tuple(expected_histories[device_id])
        observed_history = outcome_histories[device_id]
        assert expected_payloads, f"{device_id} must have a non-empty completed-outcome history"
        assert observed_history == tuple((payload, 1, True) for payload in expected_payloads), (
            f"{device_id} availability was changed by another device's history"
        )
        assert observed_history[-1][0] == expected_payloads[-1], (
            f"{device_id} retained availability must equal its own latest completed outcome"
        )

        shutdown_publications = shutdown_histories[device_id]
        assert shutdown_publications == (("offline", 1, True),), (
            f"graceful shutdown must publish exactly one retained offline value for {device_id}"
        )
        assert final_histories[device_id] == (*observed_history, *shutdown_publications)
        assert final_histories[device_id][-1] == ("offline", 1, True), (
            f"accepted device {device_id} did not end graceful shutdown offline"
        )
