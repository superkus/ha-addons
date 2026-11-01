"""Broker lifecycle plus inventory/state transport for the companion integration.

The publisher registers the bridge last will, removes retained generic MQTT entity configs from
older releases, publishes one retained versioned inventory, and then publishes attributes,
availability, power, and cumulative energy. On every connection cleanup precedes inventory and
inventory precedes state. :meth:`MqttPublisher.start` remains non-blocking and reconnects forever.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import ssl
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Protocol

import aiomqtt

from remote_energy_meter.diagnostics.logger import get_logger, logged_classification
from remote_energy_meter.energy import KWH_DECIMAL_PLACES, exceeds_gap_threshold
from remote_energy_meter.mqtt.inventory import inventory_payload
from remote_energy_meter.mqtt.topics import (
    PAYLOAD_AVAILABLE,
    PAYLOAD_NOT_AVAILABLE,
    bridge_availability_topic,
    device_topics_for,
    integration_inventory_topic,
)
from remote_energy_meter.plugins.interface import FailureClassification

if TYPE_CHECKING:
    import logging
    from collections.abc import Awaitable, Callable, Sequence
    from contextlib import AbstractAsyncContextManager

    from remote_energy_meter.config.models import MqttSettings
    from remote_energy_meter.config.runtime import ResolvedDevice
    from remote_energy_meter.mqtt.inventory import PublishableDevice
    from remote_energy_meter.mqtt.topics import DeviceTopics
    from remote_energy_meter.plugins.interface import PowerReading, ReadFailure

# --------------------------------------------------------------------------------------------------
# reconnect schedule (Requirement 6.9)
# --------------------------------------------------------------------------------------------------

#: First delay after a connection is lost or refused.
INITIAL_RECONNECT_DELAY_S: Final = 1.0

#: Ceiling for the doubling schedule. Reached at the seventh attempt and held from then on.
MAX_RECONNECT_DELAY_S: Final = 60.0

#: ``1 * 2**6 == 64`` already exceeds the cap, so no larger exponent can change the result.
#: Capping the exponent rather than the product also keeps a long outage from evaluating
#: ``2.0 ** 1024``, which raises :exc:`OverflowError` instead of returning ``inf``.
_MAX_DOUBLINGS: Final = 6

#: QoS for configuration and availability publishes. At-least-once, because a dropped discovery
#: payload leaves an entity Home Assistant never learns about.
_CONTROL_QOS: Final = 1

#: CONNACK codes meaning "credentials refused": MQTT 3.1.1 codes 4 and 5, MQTT 5 reason codes 134
#: and 135. Read off the exception rather than matched in its message, because message text is not
#: part of any library's contract.
_AUTH_RETURN_CODES: Final = frozenset({4, 5, 134, 135})

# ------------------------------------------------------------------------------------------------
# published value guards (Requirement 6.14)
# ------------------------------------------------------------------------------------------------

#: Publishable range, deliberately wider than any one plugin's plausibility range. The iDRAC
#: plugin rejects a chassis reading outside 0-10000 W as an invalid measurement; this guard has to
#: hold for *every* plugin, including future families that draw more.
MIN_PUBLISHABLE_WATTS: Final = 0.0
MAX_PUBLISHABLE_WATTS: Final = 100_000.0

#: Requirement 6.4. One decimal place is what the power entity publishes.
WATTS_DECIMAL_PLACES: Final = 1

#: Key for the whole-millisecond read duration in the power entity's attribute payload
#: (Requirement 9.5). The design fixes the topic and the unit but not the key name, so this is a
#: local choice; Property 36 only requires it to carry the same number as the cycle debug record.
DURATION_ATTRIBUTE: Final = "duration_ms"


def usable_watts(watts: float) -> float | None:
    """Round ``watts`` for publication, or return ``None`` if it must not be published.

    Requirement 6.14 in one pure function, which is what makes Property 21 ("power state is
    published if and only if the value is usable") testable without a broker. A NaN is rejected
    rather than passed on because a non-finite value reaching a ``total_increasing`` sensor
    corrupts Home Assistant long-term statistics in a way that is tedious to repair.
    """
    if not math.isfinite(watts):
        return None
    if not MIN_PUBLISHABLE_WATTS <= watts <= MAX_PUBLISHABLE_WATTS:
        return None
    return round(watts, WATTS_DECIMAL_PLACES)


def reconnect_delay(attempt: int) -> float:
    """Seconds to wait before reconnect ``attempt`` (Requirement 6.9).

    Doubles from :data:`INITIAL_RECONNECT_DELAY_S` and holds at :data:`MAX_RECONNECT_DELAY_S`.
    There is deliberately no attempt limit: 6.12 requires the process to survive an arbitrarily
    long broker outage, so there is no attempt count at which giving up would be correct.

    :param attempt: 1 for the first delay after a connection is lost or refused.
    :raises ValueError: if ``attempt`` is below 1, which would ask for a delay before any attempt
        has failed.
    """
    if attempt < 1:
        raise ValueError(f"a reconnect delay needs at least one failed attempt: {attempt!r}")
    doublings = min(attempt - 1, _MAX_DOUBLINGS)
    return min(MAX_RECONNECT_DELAY_S, INITIAL_RECONNECT_DELAY_S * 2.0**doublings)


def classify_broker_failure(error: BaseException) -> FailureClassification:
    """Classify why a broker connection failed, for the record Requirement 6.12 asks for.

    Certificate and handshake failures are separated from ordinary unreachability because they
    need a different fix, and a refused credential is separated where the library surfaces a
    CONNACK code. Everything else is :attr:`FailureClassification.COMMUNICATION`: the
    administrator's next step for a refused TCP connection, a closed socket, and a protocol error
    is the same one.
    """
    if isinstance(error, ssl.SSLError):
        return FailureClassification.TLS_VALIDATION
    if isinstance(error, TimeoutError):
        return FailureClassification.TIMEOUT
    code = getattr(error, "rc", None)
    if isinstance(code, int) and code in _AUTH_RETURN_CODES:
        return FailureClassification.AUTHENTICATION
    return FailureClassification.COMMUNICATION


# --------------------------------------------------------------------------------------------------
# the client boundary
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Will:
    """The last will registered at connection time (Requirement 6.6).

    Declared here rather than imported from the client library, so the connection parameters a
    test asserts against do not depend on that library's constructor.
    """

    topic: str
    payload: str
    qos: int = _CONTROL_QOS
    retain: bool = True


@dataclass(frozen=True, slots=True)
class BrokerConnection:
    """Everything needed to open one broker connection.

    The credentials are ``repr=False``: this object is the natural thing to put in a debug record,
    and a dataclass repr is the easy way for a password to reach a log line. The redaction filter
    would scrub it, but not putting it there is one fewer thing depending on the filter.
    """

    hostname: str
    port: int
    client_id: str
    will: Will
    tls: bool = False
    username: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)

    @property
    def address(self) -> str:
        """``host:port``, which is what a log record may name (Requirement 6.12)."""
        return f"{self.hostname}:{self.port}"


@dataclass(frozen=True, slots=True)
class BufferedReading:
    """The one reading held per device while the broker is unreachable (Requirement 6.10).

    One reading rather than a queue: Home Assistant's recorder wants a current value, and
    replaying a backlog of stale wattages would draw a history that never happened.

    ``interval_s`` travels with the reading because the freshness rule is per-device, and the
    device it came from may not be reachable at flush time to ask again.
    """

    reading: PowerReading
    duration_ms: int
    interval_s: float


class BrokerClient(Protocol):
    """The one operation this module needs from a connected client.

    A protocol rather than the concrete client, so the lifecycle is testable without a broker.
    """

    async def publish(self, topic: str, payload: str, qos: int = 0, retain: bool = False) -> None:
        """Publish ``payload`` to ``topic``."""
        ...


if TYPE_CHECKING:
    ClientFactory = Callable[[BrokerConnection], AbstractAsyncContextManager[BrokerClient]]


def aiomqtt_client_factory(connection: BrokerConnection) -> AbstractAsyncContextManager[BrokerClient]:
    """Open a real ``aiomqtt`` connection, last will included.

    TLS uses a default verifying context, so a bad certificate surfaces as an :exc:`ssl.SSLError`
    and is classified as ``tls_validation`` rather than silently accepted.
    """
    will = aiomqtt.Will(
        topic=connection.will.topic,
        payload=connection.will.payload,
        qos=connection.will.qos,
        retain=connection.will.retain,
    )
    client: AbstractAsyncContextManager[BrokerClient] = aiomqtt.Client(
        hostname=connection.hostname,
        port=connection.port,
        username=connection.username,
        password=connection.password,
        identifier=connection.client_id,
        will=will,
        tls_context=ssl.create_default_context() if connection.tls else None,
    )
    return client


def _utc_now() -> datetime:
    """Default clock. Injected in tests so the freshness rule runs without waiting."""
    return datetime.now(UTC)


def _no_energy_total(device_id: str) -> float | None:
    """Default energy source: no total known, so the energy topic is left alone.

    Lets the publisher be constructed and exercised before the accumulator exists, the same way
    :class:`~remote_energy_meter.scheduler.NullCycleSink` lets the executor run before this class
    existed.
    """
    return None


# --------------------------------------------------------------------------------------------------
# the publisher
# --------------------------------------------------------------------------------------------------


class MqttPublisher:
    """Own the broker connection, migration cleanup, retained inventory, and state transport.

    Construct with the accepted devices, call :meth:`start` once, and :meth:`stop` at shutdown.
    Neither raises because the broker is unreachable: 6.12 makes that a logged condition rather
    than a startup failure.
    """

    def __init__(
        self,
        settings: MqttSettings,
        devices: Sequence[PublishableDevice] = (),
        *,
        client_factory: ClientFactory | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        energy_total_kwh: Callable[[str], float | None] | None = None,
        now: Callable[[], datetime] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._settings = settings
        self._devices = tuple(devices)
        self._topics: dict[str, DeviceTopics] = {
            device.device_id: device_topics_for(settings, device.device_id) for device in self._devices
        }
        self._client_factory: ClientFactory = client_factory if client_factory is not None else aiomqtt_client_factory
        self._sleep: Callable[[float], Awaitable[None]] = sleep if sleep is not None else asyncio.sleep
        # Injected rather than holding the accumulator, so the publisher depends on one number per
        # device instead of the whole accumulation surface. CycleSink.deliver_reading carries no
        # total, so the wiring in task 14.1 supplies EnergyAccumulator.total_kwh here.
        self._energy_total_kwh: Callable[[str], float | None] = (
            energy_total_kwh if energy_total_kwh is not None else _no_energy_total
        )
        self._log = logger if logger is not None else get_logger(__name__)
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._connected = asyncio.Event()
        self._client: BrokerClient | None = None
        self._failed_attempts = 0
        #: Devices that have produced at least one publishable reading since process start.
        #: Requirement 9.8 turns on this distinction, not on whether a duration is known.
        self._succeeded: set[str] = set()
        self._now: Callable[[], datetime] = now if now is not None else _utc_now
        #: At most one reading per device, held only while disconnected (Requirement 6.10).
        self._buffered: dict[str, BufferedReading] = {}

    @property
    def bridge_topic(self) -> str:
        """The bridge availability topic, which is also the last will target."""
        return bridge_availability_topic(self._settings.base_topic)

    @property
    def connected(self) -> bool:
        """Whether cleanup and inventory completed on a live connection."""
        return self._connected.is_set()

    async def start(self) -> None:
        """Schedule the connection supervisor and return.

        Deliberately does not wait for a connection. Requirement 6.12 requires startup to continue
        when the broker is down, so there is nothing here worth awaiting.
        """
        if self._task is not None:
            raise RuntimeError("publisher is already started")
        self._stopping.clear()
        self._failed_attempts = 0
        self._task = asyncio.create_task(self._supervise(), name="mqtt-publisher")

    async def publish_all_offline(self) -> None:
        """Mark every announced device unavailable, for the shutdown sequence (Requirement 1.5).

        Distinct from the last will, which marks the *bridge* offline and so covers every entity at
        once when the connection drops. An orderly shutdown says it per device as well, so a restart
        that comes back with a different device list does not leave a stale ``online`` retained on a
        device the new configuration no longer polls.

        Best effort: a broker that has already gone away must not stop the rest of shutdown.
        """
        client = self._client
        if client is None or not self._connected.is_set():
            return
        for device in self._devices:
            topics = self._topics[device.device_id]
            with contextlib.suppress(Exception):
                await client.publish(topics.availability, PAYLOAD_NOT_AVAILABLE, qos=_CONTROL_QOS, retain=True)

    async def stop(self) -> None:
        """Mark the bridge offline if possible, then stop the supervisor.

        The explicit ``offline`` publish is the graceful counterpart to the last will: the will
        covers a crash or a dropped socket, this covers an orderly shutdown.
        """
        self._stopping.set()
        client = self._client
        if client is not None and self._connected.is_set():
            with contextlib.suppress(Exception):
                await client.publish(self.bridge_topic, PAYLOAD_NOT_AVAILABLE, qos=_CONTROL_QOS, retain=True)
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _supervise(self) -> None:
        """Connect, hold the connection, and reconnect forever (Requirements 6.9, 6.12)."""
        while not self._stopping.is_set():
            try:
                async with self._client_factory(self._connection()) as client:
                    self._client = client
                    self._failed_attempts = 0
                    await self._on_connected(client)
                    await self._wait_for_connection_end(client)
            # Broad on purpose: a supervisor must outlive every failure mode the client can
            # raise. CancelledError derives from BaseException, so a cancelled stop still
            # propagates rather than being swallowed and retried.
            except Exception as error:
                self._log_connection_failure(error)
            finally:
                self._connected.clear()
                self._client = None
            if self._stopping.is_set():
                return
            self._failed_attempts += 1
            delay_s = reconnect_delay(self._failed_attempts)
            self._log.info(
                "reconnecting to MQTT broker in %.1f s",
                delay_s,
                extra={"broker": self._connection().address, "attempt": self._failed_attempts},
            )
            await self._sleep(delay_s)

    async def _wait_for_connection_end(self, client: BrokerClient) -> None:
        """Wait until shutdown or until the real aiomqtt transport reports a disconnect.

        An async context manager cannot raise into the body that owns it.  The aiomqtt message
        iterator is therefore the public connection-loss signal: it raises ``MqttError`` when
        paho reports an unexpected disconnect.  Recording clients injected by unit tests do not
        have a transport to lose, so they retain the original stop-event behavior.
        """
        if not isinstance(client, aiomqtt.Client):
            await self._stopping.wait()
            return

        async for _message in client.messages:
            # The publishing connection has no subscriptions.  Iterating is solely how aiomqtt
            # exposes disconnects without reaching into its private paho client.
            pass
        if not self._stopping.is_set():
            raise ConnectionError("MQTT broker connection ended unexpectedly")

    def _connection(self) -> BrokerConnection:
        """Build the connection parameters, last will included (Requirement 6.6)."""
        settings = self._settings
        return BrokerConnection(
            hostname=settings.host,
            port=settings.port,
            client_id=settings.client_id,
            will=Will(topic=self.bridge_topic, payload=PAYLOAD_NOT_AVAILABLE),
            tls=settings.tls,
            username=settings.username.get_secret_value() if settings.username is not None else None,
            password=settings.password.get_secret_value() if settings.password is not None else None,
        )

    async def _on_connected(self, client: BrokerClient) -> None:
        """Remove legacy MQTT entities, publish inventory, then publish any state.

        Every connection repeats the retained empty-payload cleanup so installations upgrading from
        generic MQTT discovery converge even when Home Assistant or the broker was unavailable during
        the first migration attempt.  The retained versioned inventory then precedes attributes,
        availability, and buffered values so the companion integration knows every exact topic first.
        """
        await self._cleanup_legacy_discovery(client)
        await self._publish_inventory(client)
        await self._publish_pending_attribute_placeholders(client)
        await client.publish(self.bridge_topic, PAYLOAD_AVAILABLE, qos=_CONTROL_QOS, retain=True)
        self._connected.set()
        await self._flush_buffered_readings(client)
        address = self._connection().address
        self._log.info(
            "connected to MQTT broker %s",
            address,
            extra={"broker": address, "devices": len(self._devices)},
        )

    async def _cleanup_legacy_discovery(self, client: BrokerClient) -> None:
        """Delete both retained generic MQTT discovery configs for every current device."""
        for device in self._devices:
            topics = self._topics[device.device_id]
            for topic in (topics.power_discovery, topics.energy_discovery):
                await client.publish(topic, "", qos=_CONTROL_QOS, retain=True)

    async def _publish_inventory(self, client: BrokerClient) -> None:
        """Publish one retained credential-free inventory after legacy cleanup."""
        await client.publish(
            integration_inventory_topic(self._settings.base_topic),
            inventory_payload(
                self._devices,
                self._topics,
                base_topic=self._settings.base_topic,
            ),
            qos=_CONTROL_QOS,
            retain=True,
        )

    def _log_connection_failure(self, error: BaseException) -> None:
        """One warning record naming the broker address and the classification (6.12)."""
        logged = logged_classification(classify_broker_failure(error))
        address = self._connection().address
        self._log.warning(
            "MQTT broker connection failed: %s",
            logged.value,
            extra={"broker": address, "classification": logged.value, "detail": str(error)},
        )

    # -- CycleSink: state, availability, and diagnostics (task 12.2) --------------------------

    async def deliver_reading(self, device: ResolvedDevice, reading: PowerReading, duration_ms: int) -> None:
        """Publish one successful cycle's state, availability, and diagnostics.

        Implements :class:`~remote_energy_meter.scheduler.CycleSink`, so the executor delivers here
        without importing this module.

        Requirement 6.14 gates the *power state* only. Availability is still ``online`` when a value
        is rejected, because the device did answer -- Property 22 ties availability to the most
        recent completed outcome, and a cycle that produced a reading is a successful outcome. The
        energy total is still published too, since the accumulator applies its own usability rule and
        a rejected instantaneous value does not invalidate the total already banked.
        """
        topics = self._topics.get(device.device_id)
        client = self._client
        if topics is None:
            # Anticipated: CycleSink promises never to end a poll loop over an unregistered device.
            self._log.warning(
                "no MQTT topics for device %s; nothing published",
                device.device_id,
                extra={"device_id": device.device_id},
            )
            return
        if client is None or not self._connected.is_set():
            # Requirement 6.10: hold the most recent reading, replacing any earlier one, and let
            # the next successful connection decide whether it is still worth publishing.
            self._buffered[device.device_id] = BufferedReading(reading, duration_ms, device.interval_s)
            return

        await self._publish_reading(client, topics, reading, duration_ms)

    async def report_unavailable(self, device: ResolvedDevice, failure: ReadFailure) -> None:
        """Publish ``offline`` for a cycle in which every attempt failed (Requirements 5.6, 6.8).

        The failure itself is already recorded by the poll cycle executor, so this publishes rather
        than logging again: two records for one failure would double every broker outage in the log.
        """
        topics = self._topics.get(device.device_id)
        client = self._client
        if topics is None or client is None or not self._connected.is_set():
            return
        await client.publish(topics.availability, PAYLOAD_NOT_AVAILABLE, qos=_CONTROL_QOS, retain=True)

    # -- publication helpers -----------------------------------------------------------------

    @staticmethod
    def _format_watts(watts: float) -> str:
        """Requirement 6.4: one decimal place, and always exactly one."""
        return f"{watts:.{WATTS_DECIMAL_PLACES}f}"

    @staticmethod
    def _attributes_payload(duration_ms: int | None) -> str:
        """The power entity's diagnostic attributes (Requirements 9.5, 9.8).

        ``None`` publishes the key with a JSON null rather than omitting it: Requirement 9.8 asks for
        the entity to carry *no duration value* before the first success, and an absent key would
        leave Home Assistant showing whatever the previous payload set.
        """
        return json.dumps({DURATION_ATTRIBUTE: duration_ms}, separators=(",", ":"))

    async def _publish_energy_total(self, client: BrokerClient, topics: DeviceTopics, device_id: str) -> None:
        """Publish the cumulative total on the retained energy topic, when one is available."""
        total_kwh = self._energy_total_kwh(device_id)
        if total_kwh is None:
            return
        await client.publish(
            topics.energy_state,
            f"{total_kwh:.{KWH_DECIMAL_PLACES}f}",
            qos=_CONTROL_QOS,
            retain=True,
        )

    async def _publish_pending_attribute_placeholders(self, client: BrokerClient) -> None:
        """Give every not-yet-successful device an attribute payload with no duration (9.8).

        Runs on each connection, after inventory and before availability, so a device announced but
        never yet read shows an empty duration rather than nothing at all.
        """
        for device in self._devices:
            if device.device_id in self._succeeded:
                continue
            topics = self._topics[device.device_id]
            await client.publish(topics.power_attributes, self._attributes_payload(None), retain=False)

    def _log_rejected_value(self, device_id: str, watts: float) -> None:
        """One warning naming the rejected value (Requirement 6.14)."""
        self._log.warning(
            "power value not publishable, last published state left unchanged: %r",
            watts,
            extra={"device_id": device_id, "rejected_watts": repr(watts)},
        )

    # -- the retained reading buffer (Requirement 6.10) ---------------------------------------

    async def _flush_buffered_readings(self, client: BrokerClient) -> None:
        """Publish each buffered reading that is still fresh, and discard the rest.

        Runs after inventory has been republished, so the ordering guarantee holds for a
        recovered connection as well as a first one.

        Freshness reuses :func:`~remote_energy_meter.energy.exceeds_gap_threshold`, the accumulator's
        4x-interval gap rule. Sharing it is the point: a value too old to integrate is also too old
        to display, and two separate thresholds would eventually disagree.
        """
        buffered, self._buffered = self._buffered, {}
        now = self._now()
        for device_id, held in buffered.items():
            topics = self._topics.get(device_id)
            if topics is None:
                continue
            if exceeds_gap_threshold(held.reading.measured_at, now, held.interval_s):
                self._log.info(
                    "discarded a buffered reading for %s: older than 4x its %.0f s poll interval",
                    device_id,
                    held.interval_s,
                    extra={"device_id": device_id, "interval_s": held.interval_s},
                )
                continue
            await self._publish_reading(client, topics, held.reading, held.duration_ms)

    async def _publish_reading(
        self,
        client: BrokerClient,
        topics: DeviceTopics,
        reading: PowerReading,
        duration_ms: int,
    ) -> None:
        """The publication sequence shared by a live reading and a flushed buffered one."""
        watts = usable_watts(reading.watts)
        if watts is None:
            self._log_rejected_value(reading.device_id, reading.watts)
        else:
            await client.publish(topics.power_state, self._format_watts(watts), retain=False)
            self._succeeded.add(reading.device_id)

        await client.publish(topics.power_attributes, self._attributes_payload(duration_ms), retain=False)
        await self._publish_energy_total(client, topics, reading.device_id)
        await client.publish(topics.availability, PAYLOAD_AVAILABLE, qos=_CONTROL_QOS, retain=True)
