"""Unit tests for the broker connection lifecycle (task 12.1).

Covers the fixed behaviours: the retain flag on discovery publishes, the last will registered on
the bridge availability topic, a non-fatal first-connection failure, and the reconnect delay
schedule. The schedule runs against an injected sleep rather than real time, so the sequence is
asserted without spending 63 seconds observing it.

State, availability, and diagnostic publication (task 12.2) and the retained reading buffer with
rediscovery ordering (task 12.3) are not implemented yet and are not covered here.
"""

from __future__ import annotations

import asyncio
import json
import ssl
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

import pytest

from remote_energy_meter.config.models import MqttSettings
from remote_energy_meter.mqtt.inventory import PublishableDevice
from remote_energy_meter.mqtt.publisher import (
    MAX_PUBLISHABLE_WATTS,
    MAX_RECONNECT_DELAY_S,
    MIN_PUBLISHABLE_WATTS,
    BrokerConnection,
    MqttPublisher,
    classify_broker_failure,
    reconnect_delay,
    usable_watts,
)
from remote_energy_meter.mqtt.topics import device_topics_for, integration_inventory_topic
from remote_energy_meter.plugins.interface import FailureClassification, PowerReading, ReadFailure

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from types import TracebackType

    from remote_energy_meter.config.runtime import ResolvedDevice

DEVICE_ID = "poweredge_a"
OTHER_DEVICE_ID = "poweredge_b"


def make_settings(**overrides: Any) -> MqttSettings:
    """MqttSettings with the fields these tests care about, overridable."""
    values: dict[str, Any] = {"host": "core-mosquitto", "port": 1883}
    values.update(overrides)
    return MqttSettings(**values)


def make_devices(*device_ids: str) -> Sequence[PublishableDevice]:
    """Publishable views for the given identifiers."""
    return [PublishableDevice(device_id=device_id) for device_id in device_ids]


class Recorded:
    """One recorded publish call."""

    def __init__(self, topic: str, payload: str, qos: int, retain: bool) -> None:
        self.topic = topic
        self.payload = payload
        self.qos = qos
        self.retain = retain

    def __repr__(self) -> str:
        return f"Recorded(topic={self.topic!r}, qos={self.qos}, retain={self.retain})"


class FakeClient:
    """Stands in for a connected broker client, recording every publish."""

    def __init__(self) -> None:
        self.publishes: list[Recorded] = []

    async def publish(self, topic: str, payload: str, qos: int = 0, retain: bool = False) -> None:
        self.publishes.append(Recorded(topic, payload, qos, retain))

    @property
    def topics(self) -> list[str]:
        return [record.topic for record in self.publishes]


class FakeFactory:
    """A client factory that records connection parameters and can be scripted to fail.

    ``failures`` is how many opening attempts raise before one succeeds; ``error`` is what they
    raise. Every successful attempt yields a fresh :class:`FakeClient`, so a test can tell a
    reconnection's publishes from the original connection's.
    """

    def __init__(self, failures: int = 0, error: BaseException | None = None) -> None:
        self._remaining_failures = failures
        self._error = error if error is not None else OSError("connection refused")
        self.connections: list[BrokerConnection] = []
        self.clients: list[FakeClient] = []

    def __call__(self, connection: BrokerConnection) -> FakeFactory:
        self.connections.append(connection)
        return self

    async def __aenter__(self) -> FakeClient:
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            raise self._error
        client = FakeClient()
        self.clients.append(client)
        return client

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        return False

    @property
    def attempts(self) -> int:
        return len(self.connections)


class FakeSleep:
    """Records reconnect delays instead of waiting, and bounds the supervisor loop.

    After ``limit`` recorded delays it raises :exc:`asyncio.CancelledError`. That ends the
    supervisor without the loop's ``except Exception`` swallowing it, which is how a test of an
    endless reconnect schedule terminates.
    """

    def __init__(self, limit: int = 8) -> None:
        self.delays: list[float] = []
        self._limit = limit

    async def __call__(self, delay_s: float) -> None:
        self.delays.append(delay_s)
        if len(self.delays) >= self._limit:
            raise asyncio.CancelledError
        await asyncio.sleep(0)


async def settle(predicate: Callable[[], bool], turns: int = 200) -> bool:
    """Yield to the event loop until ``predicate()`` holds. Everything injected is instant."""
    for _ in range(turns):
        if predicate():
            return True
        await asyncio.sleep(0)
    return predicate()


# --------------------------------------------------------------------------------------------------
# the reconnect schedule (Requirement 6.9)
# --------------------------------------------------------------------------------------------------


def test_reconnect_delay_doubles_from_one_second() -> None:
    """Requirement 6.9: 1 s, then doubling."""
    assert [reconnect_delay(n) for n in range(1, 7)] == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0]


def test_reconnect_delay_caps_and_holds() -> None:
    """The cap is a ceiling, not a final attempt: it holds for every later attempt."""
    assert reconnect_delay(7) == MAX_RECONNECT_DELAY_S
    assert reconnect_delay(50) == MAX_RECONNECT_DELAY_S
    # Far enough out that computing 2**(attempt-1) directly would raise OverflowError.
    assert reconnect_delay(5000) == MAX_RECONNECT_DELAY_S


def test_reconnect_delay_never_exceeds_the_cap() -> None:
    assert all(reconnect_delay(n) <= MAX_RECONNECT_DELAY_S for n in range(1, 200))


def test_reconnect_delay_rejects_an_attempt_before_any_failure() -> None:
    """Asking for a delay at attempt 0 means no attempt has failed yet, which is a caller bug."""
    with pytest.raises(ValueError, match="at least one failed attempt"):
        reconnect_delay(0)


# --------------------------------------------------------------------------------------------------
# failure classification (Requirement 6.12)
# --------------------------------------------------------------------------------------------------


def test_certificate_failure_is_classified_as_tls_validation() -> None:
    """A bad certificate needs a different fix from an unreachable broker."""
    error = ssl.SSLCertVerificationError("self signed certificate")
    assert classify_broker_failure(error) is FailureClassification.TLS_VALIDATION


def test_timeout_is_classified_as_timeout() -> None:
    assert classify_broker_failure(TimeoutError()) is FailureClassification.TIMEOUT


def test_refused_credentials_are_classified_from_the_connack_code() -> None:
    """MQTT 3.1.1 codes 4 and 5, and MQTT 5 reason codes 134 and 135, mean "not authorized"."""
    for code in (4, 5, 134, 135):
        error = ConnectionError("refused")
        error.rc = code  # type: ignore[attr-defined]
        assert classify_broker_failure(error) is FailureClassification.AUTHENTICATION


def test_other_broker_failures_are_classified_as_communication() -> None:
    """A refused socket and a protocol error lead to the same next step."""
    assert classify_broker_failure(OSError("refused")) is FailureClassification.COMMUNICATION
    error = ConnectionError("refused")
    error.rc = 1  # type: ignore[attr-defined]
    assert classify_broker_failure(error) is FailureClassification.COMMUNICATION


# --------------------------------------------------------------------------------------------------
# the last will (Requirement 6.6)
# --------------------------------------------------------------------------------------------------


async def test_last_will_targets_the_bridge_availability_topic() -> None:
    """One will on the bridge topic is how a stopped addon marks every entity unavailable."""
    factory = FakeFactory()
    publisher = MqttPublisher(make_settings(), make_devices(DEVICE_ID), client_factory=factory, sleep=FakeSleep())
    await publisher.start()
    assert await settle(lambda: publisher.connected)

    will = factory.connections[0].will
    assert will.topic == "remote_energy_meter/bridge/availability"
    assert will.payload == "offline"
    assert will.retain is True
    await publisher.stop()


async def test_connection_parameters_keep_credentials_out_of_the_repr() -> None:
    """The connection object is the natural thing to put in a debug record."""
    factory = FakeFactory()
    settings = make_settings(username="broker-user", password="s3cret-broker-password")
    publisher = MqttPublisher(settings, client_factory=factory, sleep=FakeSleep())
    await publisher.start()
    assert await settle(lambda: publisher.connected)

    connection = factory.connections[0]
    assert connection.password == "s3cret-broker-password"
    assert "s3cret-broker-password" not in repr(connection)
    assert "broker-user" not in repr(connection)
    await publisher.stop()


# --------------------------------------------------------------------------------------------------
# legacy cleanup and companion inventory publication
# --------------------------------------------------------------------------------------------------


async def test_legacy_discovery_configs_are_deleted_retained_in_exact_order_before_inventory() -> None:
    """Each connection migrates both old generic MQTT entities for every current device."""
    factory = FakeFactory()
    settings = make_settings()
    publisher = MqttPublisher(
        settings, make_devices(DEVICE_ID, OTHER_DEVICE_ID), client_factory=factory, sleep=FakeSleep()
    )
    await publisher.start()
    assert await settle(lambda: publisher.connected)

    client = factory.clients[0]
    first_topics = device_topics_for(settings, DEVICE_ID)
    second_topics = device_topics_for(settings, OTHER_DEVICE_ID)
    inventory_topic = integration_inventory_topic(settings.base_topic)
    expected_prefix = [
        first_topics.power_discovery,
        first_topics.energy_discovery,
        second_topics.power_discovery,
        second_topics.energy_discovery,
        inventory_topic,
    ]
    assert client.topics[: len(expected_prefix)] == expected_prefix

    cleanup = client.publishes[:4]
    assert all(record.payload == "" for record in cleanup)
    assert all(record.retain is True and record.qos == 1 for record in cleanup)
    assert all(record.topic.endswith("/config") for record in cleanup)
    assert not any(record.topic.endswith("/config") and record.payload for record in client.publishes)

    inventory = client.publishes[4]
    assert inventory.topic == inventory_topic
    assert inventory.retain is True and inventory.qos == 1
    announced = json.loads(inventory.payload)
    assert [device["device_id"] for device in announced["devices"]] == [DEVICE_ID, OTHER_DEVICE_ID]
    await publisher.stop()


async def test_inventory_precedes_attributes_and_bridge_availability() -> None:
    """The integration learns exact topics before any state-like publication."""
    factory = FakeFactory()
    settings = make_settings()
    publisher = MqttPublisher(settings, make_devices(DEVICE_ID), client_factory=factory, sleep=FakeSleep())
    await publisher.start()
    assert await settle(lambda: publisher.connected)

    topics = device_topics_for(settings, DEVICE_ID)
    order = factory.clients[0].topics
    inventory_index = order.index(integration_inventory_topic(settings.base_topic))
    assert inventory_index < order.index(topics.power_attributes)
    assert inventory_index < order.index(publisher.bridge_topic)
    await publisher.stop()


async def test_bridge_is_marked_online_retained_after_inventory() -> None:
    """The retained `online` marker is the counterpart to the retained will."""
    factory = FakeFactory()
    publisher = MqttPublisher(make_settings(), make_devices(DEVICE_ID), client_factory=factory, sleep=FakeSleep())
    await publisher.start()
    assert await settle(lambda: publisher.connected)

    marker = [r for r in factory.clients[0].publishes if r.topic == publisher.bridge_topic]
    assert [r.payload for r in marker] == ["online"]
    assert marker[0].retain is True
    await publisher.stop()


# --------------------------------------------------------------------------------------------------
# a failed first connection is not fatal (Requirements 6.9, 6.12)
# --------------------------------------------------------------------------------------------------


async def test_first_connection_failure_does_not_raise_out_of_start() -> None:
    """Requirement 6.12: the process keeps running when the broker is down at startup."""
    factory = FakeFactory(failures=3)
    publisher = MqttPublisher(make_settings(), make_devices(DEVICE_ID), client_factory=factory, sleep=FakeSleep())

    await publisher.start()  # must not raise
    assert await settle(lambda: publisher.connected)
    assert factory.attempts == 4, "three failures then one success"
    await publisher.stop()


async def test_first_connection_failure_logs_the_broker_address_and_classification(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Requirement 6.12: one warning naming where it tried to connect and why it failed."""
    factory = FakeFactory(failures=1)
    publisher = MqttPublisher(make_settings(), make_devices(DEVICE_ID), client_factory=factory, sleep=FakeSleep())
    with caplog.at_level("WARNING"):
        await publisher.start()
        assert await settle(lambda: publisher.connected)

    warnings = [record for record in caplog.records if record.levelname == "WARNING"]
    assert len(warnings) == 1
    assert getattr(warnings[0], "broker", None) == "core-mosquitto:1883"
    assert getattr(warnings[0], "classification", None) == "read"
    await publisher.stop()


async def test_a_broker_that_never_accepts_keeps_retrying_on_the_doubling_schedule() -> None:
    """Requirement 6.9: delays double from 1 s and hold at 60 s, with no attempt limit."""
    sleep = FakeSleep(limit=9)
    factory = FakeFactory(failures=1000)
    publisher = MqttPublisher(make_settings(), make_devices(DEVICE_ID), client_factory=factory, sleep=sleep)

    await publisher.start()
    assert await settle(lambda: len(sleep.delays) >= 9)

    assert sleep.delays[:8] == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]
    assert publisher.connected is False
    await publisher.stop()


async def test_a_recovered_connection_restarts_the_delay_schedule() -> None:
    """Each outage gets its own schedule; a recovered connection does not inherit a 60 s delay."""
    sleep = FakeSleep()
    factory = FakeFactory(failures=2)
    publisher = MqttPublisher(make_settings(), make_devices(DEVICE_ID), client_factory=factory, sleep=sleep)
    await publisher.start()
    assert await settle(lambda: publisher.connected)

    assert sleep.delays == [1.0, 2.0]
    await publisher.stop()


# --------------------------------------------------------------------------------------------------
# shutdown
# --------------------------------------------------------------------------------------------------


async def test_stop_marks_the_bridge_offline_and_ends_the_supervisor() -> None:
    """The graceful counterpart to the will: an orderly shutdown says offline itself."""
    factory = FakeFactory()
    publisher = MqttPublisher(make_settings(), make_devices(DEVICE_ID), client_factory=factory, sleep=FakeSleep())
    await publisher.start()
    assert await settle(lambda: publisher.connected)

    await publisher.stop()

    bridge = [r for r in factory.clients[0].publishes if r.topic == publisher.bridge_topic]
    assert [r.payload for r in bridge] == ["online", "offline"]
    assert bridge[-1].retain is True
    assert publisher.connected is False


async def test_starting_twice_is_a_caller_error() -> None:
    """Two supervisors would mean two connections and two wills."""
    publisher = MqttPublisher(make_settings(), client_factory=FakeFactory(), sleep=FakeSleep())
    await publisher.start()
    with pytest.raises(RuntimeError, match="already started"):
        await publisher.start()
    await publisher.stop()


# --------------------------------------------------------------------------------------------------
# the publishable value guard (Requirement 6.14)
# --------------------------------------------------------------------------------------------------


def test_usable_watts_rounds_to_one_decimal_place() -> None:
    """Requirement 6.4."""
    assert usable_watts(120.44) == 120.4
    assert usable_watts(120.46) == 120.5


def test_usable_watts_rejects_non_finite_values() -> None:
    """A NaN reaching a total_increasing sensor corrupts long-term statistics."""
    assert usable_watts(float("nan")) is None
    assert usable_watts(float("inf")) is None
    assert usable_watts(float("-inf")) is None


def test_usable_watts_accepts_the_inclusive_bounds() -> None:
    assert usable_watts(MIN_PUBLISHABLE_WATTS) == 0.0
    assert usable_watts(MAX_PUBLISHABLE_WATTS) == MAX_PUBLISHABLE_WATTS


def test_usable_watts_rejects_values_outside_the_bounds() -> None:
    assert usable_watts(-0.1) is None
    assert usable_watts(MAX_PUBLISHABLE_WATTS + 1) is None


# --------------------------------------------------------------------------------------------------
# state, availability, and diagnostics (Requirements 6.4, 6.7, 6.8, 9.5, 9.8)
# --------------------------------------------------------------------------------------------------


class FakeResolvedDevice:
    """Stands in for ResolvedDevice. The publisher reads only ``device_id`` and ``interval_s``."""

    def __init__(self, device_id: str = DEVICE_ID, interval_s: float = 30.0) -> None:
        self.device_id = device_id
        self.interval_s = interval_s


def as_device(device_id: str = DEVICE_ID, interval_s: float = 30.0) -> ResolvedDevice:
    return cast("ResolvedDevice", FakeResolvedDevice(device_id, interval_s))


def a_reading(watts: float, device_id: str = DEVICE_ID) -> PowerReading:
    return PowerReading(device_id=device_id, watts=watts, measured_at=datetime(2025, 1, 1, tzinfo=UTC))


async def connected(factory: FakeFactory, **kwargs: Any) -> MqttPublisher:
    """A started publisher for one device, already connected."""
    publisher = MqttPublisher(
        make_settings(), make_devices(DEVICE_ID), client_factory=factory, sleep=FakeSleep(), **kwargs
    )
    await publisher.start()
    assert await settle(lambda: publisher.connected)
    return publisher


async def test_a_reading_publishes_state_availability_and_the_duration_attribute() -> None:
    """Requirements 6.4, 6.7, 9.5 in one cycle."""
    factory = FakeFactory()
    publisher = await connected(factory, energy_total_kwh=lambda _: 1.5)
    client = factory.clients[0]
    seen = len(client.publishes)

    await publisher.deliver_reading(as_device(), a_reading(120.46), 250)

    topics = device_topics_for(make_settings(), DEVICE_ID)
    published = {record.topic: record for record in client.publishes[seen:]}
    assert published[topics.power_state].payload == "120.5"
    assert published[topics.power_state].retain is False, "a stale wattage shown as current is worse than none"
    assert published[topics.availability].payload == "online"
    assert published[topics.availability].retain is True
    assert json.loads(published[topics.power_attributes].payload) == {"duration_ms": 250}
    await publisher.stop()


async def test_the_energy_total_is_published_retained_to_six_decimal_places() -> None:
    """The cumulative total must survive a Home Assistant restart, so its topic is retained."""
    factory = FakeFactory()
    publisher = await connected(factory, energy_total_kwh=lambda _: 2.25)
    client = factory.clients[0]
    seen = len(client.publishes)

    await publisher.deliver_reading(as_device(), a_reading(100.0), 10)

    topics = device_topics_for(make_settings(), DEVICE_ID)
    energy = next(r for r in client.publishes[seen:] if r.topic == topics.energy_state)
    assert energy.payload == "2.250000"
    assert energy.retain is True
    await publisher.stop()


async def test_an_unusable_value_leaves_the_last_published_state_unchanged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Requirement 6.14: discard, publish nothing to the state topic, and log the rejected value."""
    factory = FakeFactory()
    publisher = await connected(factory)
    client = factory.clients[0]
    topics = device_topics_for(make_settings(), DEVICE_ID)

    await publisher.deliver_reading(as_device(), a_reading(150.0), 10)
    seen = len(client.publishes)

    with caplog.at_level("WARNING"):
        await publisher.deliver_reading(as_device(), a_reading(float("nan")), 20)

    states = [r for r in client.publishes[seen:] if r.topic == topics.power_state]
    assert states == [], "the rejected value must not reach the state topic"
    earlier = [r for r in client.publishes[:seen] if r.topic == topics.power_state]
    assert earlier[-1].payload == "150.0", "the previously published state stands"
    assert any("not publishable" in record.getMessage() for record in caplog.records)
    await publisher.stop()


async def test_an_unusable_value_still_reports_the_device_online() -> None:
    """The device answered, so Property 22's "most recent completed outcome" is a success."""
    factory = FakeFactory()
    publisher = await connected(factory)
    client = factory.clients[0]
    seen = len(client.publishes)

    await publisher.deliver_reading(as_device(), a_reading(float("inf")), 10)

    topics = device_topics_for(make_settings(), DEVICE_ID)
    availability = [r for r in client.publishes[seen:] if r.topic == topics.availability]
    assert [r.payload for r in availability] == ["online"]
    await publisher.stop()


async def test_a_fully_failed_cycle_publishes_offline_retained() -> None:
    """Requirements 5.6, 6.8."""
    factory = FakeFactory()
    publisher = await connected(factory)
    client = factory.clients[0]
    seen = len(client.publishes)

    failure = ReadFailure(device_id=DEVICE_ID, classification=FailureClassification.TIMEOUT, detail="timed out")
    await publisher.report_unavailable(as_device(), failure)

    topics = device_topics_for(make_settings(), DEVICE_ID)
    availability = [r for r in client.publishes[seen:] if r.topic == topics.availability]
    assert [r.payload for r in availability] == ["offline"]
    assert availability[0].retain is True
    await publisher.stop()


async def test_before_the_first_success_the_duration_attribute_has_no_value() -> None:
    """Requirement 9.8: the entity exists with an explicit null duration, not a stale one."""
    factory = FakeFactory()
    publisher = await connected(factory)
    topics = device_topics_for(make_settings(), DEVICE_ID)

    placeholder = [r for r in factory.clients[0].publishes if r.topic == topics.power_attributes]
    assert len(placeholder) == 1
    assert json.loads(placeholder[0].payload) == {"duration_ms": None}
    await publisher.stop()


async def test_a_reconnection_after_a_success_does_not_republish_the_placeholder() -> None:
    """Once a device has reported, a null duration would be a regression, not a placeholder."""
    factory = FakeFactory()
    publisher = await connected(factory)
    await publisher.deliver_reading(as_device(), a_reading(120.0), 30)

    # Reaches past the public surface on purpose: it replays exactly the on-connect sequence a
    # reconnection runs, without needing the fake client to model a dropped socket.
    await publisher._on_connected(factory.clients[0])

    topics = device_topics_for(make_settings(), DEVICE_ID)
    payloads = [json.loads(r.payload) for r in factory.clients[0].publishes if r.topic == topics.power_attributes]
    assert payloads.count({"duration_ms": None}) == 1, "only the pre-success connection publishes a null"
    await publisher.stop()


async def test_nothing_is_published_for_an_unregistered_device(caplog: pytest.LogCaptureFixture) -> None:
    """CycleSink promises an unregistered device does not end a poll loop."""
    factory = FakeFactory()
    publisher = await connected(factory)
    seen = len(factory.clients[0].publishes)

    with caplog.at_level("WARNING"):
        await publisher.deliver_reading(as_device("never_announced"), a_reading(120.0, "never_announced"), 10)

    assert factory.clients[0].publishes[seen:] == []
    assert any("no MQTT topics" in record.getMessage() for record in caplog.records)
    await publisher.stop()


# --------------------------------------------------------------------------------------------------
# the retained reading buffer and rediscovery ordering (Requirements 6.10, 6.13)
# --------------------------------------------------------------------------------------------------

MEASURED_AT = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
INTERVAL_S = 30.0


def at(offset_s: float) -> datetime:
    """A clock reading ``offset_s`` after MEASURED_AT."""
    return MEASURED_AT + timedelta(seconds=offset_s)


def buffered_reading(watts: float, device_id: str = DEVICE_ID) -> PowerReading:
    return PowerReading(device_id=device_id, watts=watts, measured_at=MEASURED_AT)


async def test_a_reading_while_disconnected_is_buffered_not_published() -> None:
    """Requirement 6.10: the reading is held rather than dropped on the floor."""
    factory = FakeFactory()
    publisher = MqttPublisher(
        make_settings(), make_devices(DEVICE_ID), client_factory=factory, sleep=FakeSleep(), now=lambda: at(0)
    )

    # Not started, so there is no connection yet.
    await publisher.deliver_reading(as_device(interval_s=INTERVAL_S), buffered_reading(120.0), 40)

    assert factory.clients == [], "nothing may be published while disconnected"


async def test_only_the_most_recent_reading_per_device_is_retained() -> None:
    """One reading, not a queue: replaying stale wattages would draw a false history."""
    factory = FakeFactory()
    publisher = MqttPublisher(
        make_settings(), make_devices(DEVICE_ID), client_factory=factory, sleep=FakeSleep(), now=lambda: at(10)
    )
    device = as_device(interval_s=INTERVAL_S)
    await publisher.deliver_reading(device, buffered_reading(100.0), 10)
    await publisher.deliver_reading(device, buffered_reading(200.0), 20)
    await publisher.deliver_reading(device, buffered_reading(300.0), 30)

    await publisher.start()
    assert await settle(lambda: publisher.connected)

    topics = device_topics_for(make_settings(), DEVICE_ID)
    states = [r.payload for r in factory.clients[0].publishes if r.topic == topics.power_state]
    assert states == ["300.0"], "only the last reading survives the outage"
    await publisher.stop()


async def test_a_fresh_buffered_reading_is_published_on_reconnection() -> None:
    """Within 4x the poll interval, so it still describes the present."""
    factory = FakeFactory()
    publisher = MqttPublisher(
        make_settings(),
        make_devices(DEVICE_ID),
        client_factory=factory,
        sleep=FakeSleep(),
        now=lambda: at(4 * INTERVAL_S - 1),
        energy_total_kwh=lambda _: 3.0,
    )
    await publisher.deliver_reading(as_device(interval_s=INTERVAL_S), buffered_reading(120.5), 40)
    await publisher.start()
    assert await settle(lambda: publisher.connected)

    topics = device_topics_for(make_settings(), DEVICE_ID)
    published = {r.topic: r for r in factory.clients[0].publishes}
    assert published[topics.power_state].payload == "120.5"
    assert published[topics.availability].payload == "online"
    assert json.loads(published[topics.power_attributes].payload) == {"duration_ms": 40}
    await publisher.stop()


async def test_a_stale_buffered_reading_is_discarded(caplog: pytest.LogCaptureFixture) -> None:
    """Beyond 4x the interval it is too old to display, the same rule that stops integration."""
    factory = FakeFactory()
    publisher = MqttPublisher(
        make_settings(),
        make_devices(DEVICE_ID),
        client_factory=factory,
        sleep=FakeSleep(),
        now=lambda: at(4 * INTERVAL_S + 1),
    )
    await publisher.deliver_reading(as_device(interval_s=INTERVAL_S), buffered_reading(120.5), 40)

    with caplog.at_level("INFO"):
        await publisher.start()
        assert await settle(lambda: publisher.connected)

    topics = device_topics_for(make_settings(), DEVICE_ID)
    states = [r for r in factory.clients[0].publishes if r.topic == topics.power_state]
    assert states == [], "a stale value must not be presented as current"
    assert any("discarded a buffered reading" in record.getMessage() for record in caplog.records)
    await publisher.stop()


async def test_inventory_is_republished_before_any_buffered_state() -> None:
    """Every reconnection republishes inventory before a buffered state."""
    factory = FakeFactory()
    settings = make_settings()
    publisher = MqttPublisher(
        settings,
        make_devices(DEVICE_ID),
        client_factory=factory,
        sleep=FakeSleep(),
        now=lambda: at(0),
    )
    await publisher.deliver_reading(as_device(interval_s=INTERVAL_S), buffered_reading(120.0), 40)
    await publisher.start()
    assert await settle(lambda: publisher.connected)

    topics = device_topics_for(settings, DEVICE_ID)
    order = factory.clients[0].topics
    state_index = order.index(topics.power_state)
    assert order.index(integration_inventory_topic(settings.base_topic)) < state_index
    await publisher.stop()


async def test_the_buffer_is_emptied_by_a_flush() -> None:
    """A flushed reading must not be republished by the next connection."""
    factory = FakeFactory()
    publisher = MqttPublisher(
        make_settings(), make_devices(DEVICE_ID), client_factory=factory, sleep=FakeSleep(), now=lambda: at(0)
    )
    await publisher.deliver_reading(as_device(interval_s=INTERVAL_S), buffered_reading(120.0), 40)
    await publisher.start()
    assert await settle(lambda: publisher.connected)

    client = factory.clients[0]
    seen = len(client.publishes)
    await publisher._flush_buffered_readings(client)

    assert client.publishes[seen:] == [], "the buffer was already drained"
    await publisher.stop()
