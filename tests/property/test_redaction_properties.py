"""Property 32: no credential fragment escapes to any sink (task 3.3).

Property 32 is a containment statement over three sinks, one per requirement it validates:

* **log records** (Requirement 8.2) — the message, the ``%``-arguments folded into it, and
  the structured fields passed through ``extra=``;
* **exception text** (Requirement 8.7) — the rendered traceback of an exception whose own
  message embeds a credential, which is the shape a misbehaving plugin produces;
* **published MQTT payloads** (Requirement 8.3) — the retained integration inventory,
  empty legacy cleanup payloads, power and energy state, Availability_State,
  diagnostic attributes, and the registered last will.

For every one of them the emitted text must contain no substring of the credential of 4
or more characters, and every redaction must be the same fixed-length marker regardless
of the credential's length.

## Why checking 4-character windows is the whole check

A substring of 5 or more characters contains a substring of exactly 4, so "no substring of
4 or more characters survives" is equivalent to "no 4-character window survives". Every
assertion here therefore enumerates the credential's windows, which keeps the check exact
and linear rather than quadratic.

## Two generator alphabets, for two different reasons

:func:`credentials` draws from the full character space and is used against
``CredentialRegistry.redact`` directly, where the only text in play is text this test
supplied.

:func:`opaque_credentials` draws from :data:`_OPAQUE_ALPHABET`, which is chosen so that no
character of it occurs anywhere in the *structural* output of a sink — the JSON keys, the
severity and logger names, the UTC timestamp, the topic strings, the fixed Home Assistant
vocabulary (``Power``, ``Energy``, ``W``, ``kWh``, ``total_increasing``), the origin block,
or the device metadata this test generates from the disjoint
:data:`_METADATA_ALPHABET`. That disjointness is what lets the log, exception, and MQTT
properties assert over the **whole emitted text** rather than over hand-picked fields: a
window of the credential appearing anywhere in the output can only have come from the
credential, never from a coincidental collision with the sink's own vocabulary. Asserting
over a subset of fields would have been the weaker test, and a coincidence-tolerant
assertion would have been the flaky one.

## One deliberate exclusion

Both strategies exclude any credential sharing a 4-character window with
:data:`REDACTION_MARKER`. Redaction is idempotent because an existing marker is never
rescanned (documented in ``redaction.py``), so a credential that literally contains
``REDACTED`` would see that window "survive" inside the markers redaction itself inserted.
That is not a leak — the marker is a public constant, identical for every credential — but
it is not what Property 32 is about, so it is excluded rather than asserted around.
"""

from __future__ import annotations

import io
import json
import logging
import string
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any, Final

from hypothesis import given, settings
from hypothesis import strategies as st

from remote_energy_meter.config.models import MAX_DEVICE_ID_LENGTH, DeviceEntry, LogLevel
from remote_energy_meter.diagnostics.logger import configure_logging
from remote_energy_meter.diagnostics.redaction import (
    MAX_CREDENTIAL_LENGTH,
    REDACTION_MARKER,
    SHIELD_WINDOW,
    CredentialRegistry,
)
from remote_energy_meter.mqtt.inventory import PublishableDevice, inventory_payload
from remote_energy_meter.mqtt.topics import (
    BRIDGE_NODE,
    PAYLOAD_AVAILABLE,
    PAYLOAD_NOT_AVAILABLE,
    DeviceTopics,
    device_topics,
    integration_inventory_topic,
)

# --------------------------------------------------------------------------------------
# Fragment checking
# --------------------------------------------------------------------------------------


def windows(value: str) -> frozenset[str]:
    """Every substring of ``value`` of exactly :data:`SHIELD_WINDOW` characters."""
    return frozenset(value[i : i + SHIELD_WINDOW] for i in range(len(value) - SHIELD_WINDOW + 1))


def assert_no_fragment(text: str, credential: str, *, sink: str) -> None:
    """Assert no substring of ``credential`` of 4 or more characters occurs in ``text``."""
    assert credential not in text, f"{sink} leaked the whole credential"
    for window in windows(credential):
        assert window not in text, f"{sink} leaked the fragment {window!r} in {text!r}"


#: The marker's own windows. A credential sharing one of them is excluded from generation
#: — see the module docstring.
_MARKER_WINDOWS: Final = windows(REDACTION_MARKER)


def _marker_disjoint(value: str) -> bool:
    return not (windows(value) & _MARKER_WINDOWS)


# --------------------------------------------------------------------------------------
# Generators
# --------------------------------------------------------------------------------------

#: Credentials for the whole-output assertions. Contains no character that occurs in any
#: sink's structural vocabulary, so a window found in the output can only have come from
#: the credential (see the module docstring):
#:
#: * no lowercase letter and no digit — those carry the JSON keys, the severity and logger
#:   names, the timestamp, the topic strings, the addon version, and the device metadata;
#: * none of ``P E W R M T Z`` — the only uppercase letters the sinks themselves emit,
#:   from ``Power``, ``Energy``, ``W``, ``kWh``, ``Remote Energy Meter``, and the ``T``/``Z``
#:   of the UTC timestamp;
#: * no whitespace, no ``"`` and no ``\\`` — JSON would escape them, which would weaken a
#:   raw-text assertion into a vacuous one;
#: * no ``* [ ] { } , : . / _ -`` — the punctuation JSON and the topic layout use, so the
#:   output holds no run of 4 consecutive characters drawn from this alphabet.
_OPAQUE_ALPHABET: Final = "ABCDFGHIJKLNOQSUVXY" + "!#$%&'()+;=?@^|~"

#: Device identifiers, display names, hosts, and plugin-supplied metadata. Disjoint from
#: :data:`_OPAQUE_ALPHABET`, and a subset of the identifier character class Requirement
#: 2.12 accepts.
_METADATA_ALPHABET: Final = string.ascii_lowercase + string.digits + "_-"


def credentials(max_size: int = MAX_CREDENTIAL_LENGTH) -> st.SearchStrategy[str]:
    """Credential values of 4 to ``max_size`` characters over the full character space.

    Whitespace-padded values are stripped rather than generated: ``register`` normalizes
    them and the parser rejects whitespace-only credentials (Requirement 8.5), so padding
    would test the registry's normalization rather than Property 32's containment.
    """
    return (
        st.text(min_size=SHIELD_WINDOW, max_size=max_size)
        .map(str.strip)
        .filter(lambda value: len(value) >= SHIELD_WINDOW and _marker_disjoint(value))
    )


def opaque_credentials(max_size: int = 128) -> st.SearchStrategy[str]:
    """Credential values drawn from the alphabet no sink's own vocabulary uses.

    Marker-disjoint by construction: every window of :data:`REDACTION_MARKER` contains at
    least one of ``*``, ``R``, ``E``, or ``T``, and the alphabet contains none of them.
    """
    return st.text(alphabet=_OPAQUE_ALPHABET, min_size=SHIELD_WINDOW, max_size=max_size)


def surrounding_text() -> st.SearchStrategy[str]:
    """Arbitrary text a credential is embedded in. Bounded so a message stays under 512."""
    return st.text(max_size=32)


def device_ids() -> st.SearchStrategy[str]:
    """Device identifiers Requirement 2.12 accepts and the topic layout can carry."""
    return st.text(alphabet=_METADATA_ALPHABET, min_size=1, max_size=MAX_DEVICE_ID_LENGTH).filter(
        lambda value: value.lower() != BRIDGE_NODE
    )


def metadata_names() -> st.SearchStrategy[str | None]:
    """Display names and plugin-supplied manufacturer/model values, or their absence."""
    return st.none() | st.text(alphabet=_METADATA_ALPHABET + " ", min_size=1, max_size=48)


# --------------------------------------------------------------------------------------
# Sinks
# --------------------------------------------------------------------------------------


@contextmanager
def log_sink(credential_values: Sequence[str]) -> Iterator[tuple[logging.Logger, io.StringIO]]:
    """A logger wired the way the addon wires it, writing to a buffer instead of stdout.

    ``configure_logging`` is used rather than a hand-built handler because it is the
    installation path the process actually takes: it attaches the structured formatter and
    then installs the redaction filter on both the logger and the handler. Testing a
    hand-wired approximation would leave the real wiring unverified.
    """
    registry = CredentialRegistry()
    registry.register_all(credential_values)

    logger = logging.getLogger("test_redaction_properties.sink")
    logger.handlers.clear()
    logger.filters.clear()
    logger.propagate = False
    stream = io.StringIO()
    configure_logging(LogLevel.DEBUG, registry=registry, stream=stream, logger=logger)
    try:
        yield logger, stream
    finally:
        logger.handlers.clear()
        logger.filters.clear()


def _raise_with(message: str) -> None:
    """Stand in for a plugin whose exception message embeds a credential."""
    raise RuntimeError(message)


def published_texts(
    device: PublishableDevice,
    topics: DeviceTopics,
    *,
    watts: float,
    total_kwh: float,
    duration_ms: int,
) -> list[str]:
    """Every text the MQTT_Publisher puts on the wire for one device.

    Enumerated from the pure construction layer and the fixed payload vocabulary rather
    than from a publisher instance, which keeps this property independent of the transport
    while still covering each sink Property 32 names: the credential-free retained inventory,
    empty legacy cleanup payloads, power and energy state payloads, both Availability_State
    payloads (the ``offline`` value is also the registered last will payload), the diagnostic
    attribute payload, and every topic those are published to.
    """
    texts = [
        inventory_payload(
            [device],
            {device.device_id: topics},
            base_topic=topics.bridge_availability.removesuffix("/bridge/availability"),
        ),
        "",
    ]
    texts.append(f"{round(watts, 1):.1f}")
    texts.append(f"{total_kwh:.6f}")
    texts.append(PAYLOAD_AVAILABLE)
    texts.append(PAYLOAD_NOT_AVAILABLE)
    texts.append(json.dumps({"duration_ms": duration_ms}, ensure_ascii=False))
    texts.extend(
        [
            topics.bridge_availability,
            topics.availability,
            topics.power_state,
            topics.energy_state,
            topics.power_attributes,
            topics.power_discovery,
            topics.energy_discovery,
            integration_inventory_topic(topics.bridge_availability.removesuffix("/bridge/availability")),
        ]
    )
    return texts


def _entry_with(device_id: str, display_name: str | None, host: str, username: str, password: str) -> DeviceEntry:
    """A Device_Entry whose plugin parameters carry credentials, as iDRAC's do."""
    return DeviceEntry(
        device_id=device_id,
        plugin="idrac",
        display_name=display_name,
        params={"host": host, "username": username, "password": password, "verify_tls": False},
    )


def _publishable_view(entry: DeviceEntry, manufacturer: str | None, model: str | None) -> PublishableDevice:
    """Derive the publishable view of an entry, as the publisher does.

    ``params`` is not read, and cannot be: :class:`PublishableDevice` has no field able to
    hold a mapping. That is the structural enforcement of Requirement 8.3 this property
    exercises.
    """
    return PublishableDevice(
        device_id=entry.device_id,
        display_name=entry.display_name,
        manufacturer=manufacturer,
        model=model,
        plugin_id=entry.plugin,
    )


# --------------------------------------------------------------------------------------
# The property
# --------------------------------------------------------------------------------------


# Feature: remote-energy-meter, Property 32: For any credential value of 4 or more
# characters and for any log record, exception text, or published MQTT payload that
# references it, the emitted text contains no substring of that credential of 4 or more
# characters, and every redaction is the same fixed-length marker regardless of the
# credential's length.
# Validates: Requirements 8.2, 8.3, 8.7
@settings(max_examples=300, deadline=None)
@given(credential=credentials(), prefix=surrounding_text(), suffix=surrounding_text())
def test_no_credential_fragment_survives_redaction(credential: str, prefix: str, suffix: str) -> None:
    """The redactor itself: any text embedding a credential comes back fragment-free.

    Every sink routes its text through ``CredentialRegistry.redact``, so this is the
    statement the other four rest on. It runs over the full character space, where the
    per-sink whole-output assertions cannot (see the module docstring).
    """
    registry = CredentialRegistry()
    registry.register(credential)

    redacted = registry.redact(f"{prefix}{credential}{suffix}")

    assert_no_fragment(redacted, credential, sink="redact")
    assert REDACTION_MARKER in redacted
    assert registry.redact(redacted) == redacted  # idempotent, so a second sink is safe


# Feature: remote-energy-meter, Property 32: For any credential value of 4 or more
# characters and for any log record, exception text, or published MQTT payload that
# references it, the emitted text contains no substring of that credential of 4 or more
# characters, and every redaction is the same fixed-length marker regardless of the
# credential's length.
# Validates: Requirements 8.2, 8.3, 8.7
@settings(max_examples=200, deadline=None)
@given(
    credential=opaque_credentials(),
    other=opaque_credentials(max_size=32),
    prefix=st.text(alphabet=_METADATA_ALPHABET + " ", max_size=32),
    suffix=st.text(alphabet=_METADATA_ALPHABET + " ", max_size=32),
    device_id=device_ids(),
)
def test_no_credential_fragment_reaches_a_log_record(
    credential: str,
    other: str,
    prefix: str,
    suffix: str,
    device_id: str,
) -> None:
    """Requirement 8.2: no emitted record carries a fragment, by any route into a record.

    Three routes at once, because they fail independently: the ``%``-argument folded into
    the message (where a fragment can be formed only at the format boundary, so the
    message has to be rendered before it is redacted), the format string itself, and the
    structured fields passed through ``extra=`` including strings nested in containers.
    """
    with log_sink([credential, other]) as (logger, stream):
        logger.warning(
            "connect failed for %s%s",
            credential,
            suffix,
            extra={
                "device_id": device_id,
                "detail": f"{prefix}{credential}{suffix}",
                "payload": {"items": [credential, other], "pair": (other,)},
            },
        )
        logger.info("literal %s in the format string", other)

    emitted = stream.getvalue()

    assert_no_fragment(emitted, credential, sink="log record")
    assert_no_fragment(emitted, other, sink="log record")
    assert REDACTION_MARKER in emitted
    # The record is still a usable diagnostic: one JSON object per line, device id intact.
    for line in emitted.splitlines():
        assert isinstance(json.loads(line), dict)
    assert device_id in emitted


# Feature: remote-energy-meter, Property 32: For any credential value of 4 or more
# characters and for any log record, exception text, or published MQTT payload that
# references it, the emitted text contains no substring of that credential of 4 or more
# characters, and every redaction is the same fixed-length marker regardless of the
# credential's length.
# Validates: Requirements 8.2, 8.3, 8.7
@settings(max_examples=200, deadline=None)
@given(credential=opaque_credentials(max_size=64), device_id=device_ids())
def test_no_credential_fragment_reaches_exception_text(credential: str, device_id: str) -> None:
    """Requirement 8.7: a plugin exception embedding a credential is scrubbed first.

    The rendered traceback is the sink here, and the diagnostic value has to survive the
    scrub — an exception record that no longer names its exception type would satisfy
    containment and be useless.
    """
    with log_sink([credential]) as (logger, stream):
        try:
            _raise_with(f"401 for user root with password {credential}")
        except RuntimeError:
            logger.exception("read failed", extra={"device_id": device_id})

    emitted = stream.getvalue()

    assert_no_fragment(emitted, credential, sink="exception text")
    assert REDACTION_MARKER in emitted
    assert "RuntimeError" in emitted


# Feature: remote-energy-meter, Property 32: For any credential value of 4 or more
# characters and for any log record, exception text, or published MQTT payload that
# references it, the emitted text contains no substring of that credential of 4 or more
# characters, and every redaction is the same fixed-length marker regardless of the
# credential's length.
# Validates: Requirements 8.2, 8.3, 8.7
@settings(max_examples=200, deadline=None)
@given(
    username=opaque_credentials(max_size=64),
    password=opaque_credentials(max_size=64),
    device_id=device_ids(),
    display_name=metadata_names(),
    manufacturer=metadata_names(),
    model=metadata_names(),
    host=st.text(alphabet=_METADATA_ALPHABET + ".", min_size=1, max_size=32),
    watts=st.floats(min_value=0.0, max_value=100_000.0),
    total_kwh=st.floats(min_value=0.0, max_value=1_000_000.0),
    duration_ms=st.integers(min_value=0, max_value=60_000),
)
def test_no_credential_fragment_reaches_an_mqtt_payload(
    username: str,
    password: str,
    device_id: str,
    display_name: str | None,
    manufacturer: str | None,
    model: str | None,
    host: str,
    watts: float,
    total_kwh: float,
    duration_ms: int,
) -> None:
    """Requirement 8.3: no published payload or topic carries a credential fragment.

    The credentials sit in ``DeviceEntry.params`` exactly as an iDRAC entry's do, and the
    payloads are built from the view derived from that entry. Redacting each published
    text is asserted to be a no-op, which says the same thing from the other side: there
    was nothing in the payload for the registry to remove.
    """
    entry = _entry_with(device_id, display_name, host, username, password)
    device = _publishable_view(entry, manufacturer, model)
    topics = device_topics(entry.device_id)
    registry = CredentialRegistry()
    registry.register_all([username, password])

    for text in published_texts(device, topics, watts=watts, total_kwh=total_kwh, duration_ms=duration_ms):
        assert_no_fragment(text, username, sink="mqtt payload")
        assert_no_fragment(text, password, sink="mqtt payload")
        assert registry.redact(text) == text


# Feature: remote-energy-meter, Property 32: For any credential value of 4 or more
# characters and for any log record, exception text, or published MQTT payload that
# references it, the emitted text contains no substring of that credential of 4 or more
# characters, and every redaction is the same fixed-length marker regardless of the
# credential's length.
# Validates: Requirements 8.2, 8.3, 8.7
@settings(max_examples=100, deadline=None)
@given(
    first=st.tuples(credentials(max_size=64), credentials(max_size=64)),
    second=st.tuples(credentials(max_size=64), credentials(max_size=64)),
    device_id=device_ids(),
    display_name=metadata_names(),
    manufacturer=metadata_names(),
    model=metadata_names(),
)
def test_mqtt_payloads_do_not_depend_on_credential_values(
    first: tuple[str, str],
    second: tuple[str, str],
    device_id: str,
    display_name: str | None,
    manufacturer: str | None,
    model: str | None,
) -> None:
    """Requirement 8.3 from the structural side: credentials cannot influence a payload.

    Two entries differing only in their credentials produce byte-identical payloads and
    topics, so no credential value — of any alphabet, of any length — can reach a
    published payload even coincidentally. This is the guarantee the ``PublishableDevice``
    view exists to provide, stated as a property rather than left to review.
    """
    left = _publishable_view(_entry_with(device_id, display_name, "idrac.example", *first), manufacturer, model)
    right = _publishable_view(_entry_with(device_id, display_name, "idrac.example", *second), manufacturer, model)
    topics = device_topics(device_id)
    fixed: dict[str, Any] = {"watts": 214.0, "total_kwh": 12.345678, "duration_ms": 42}

    assert published_texts(left, topics, **fixed) == published_texts(right, topics, **fixed)


# Feature: remote-energy-meter, Property 32: For any credential value of 4 or more
# characters and for any log record, exception text, or published MQTT payload that
# references it, the emitted text contains no substring of that credential of 4 or more
# characters, and every redaction is the same fixed-length marker regardless of the
# credential's length.
# Validates: Requirements 8.2, 8.3, 8.7
@settings(max_examples=200, deadline=None)
@given(shorter=credentials(max_size=16), longer=credentials())
def test_every_redaction_is_the_same_fixed_length_marker(shorter: str, longer: str) -> None:
    """A marker whose length varied with the credential would leak the credential's length.

    Redacting a text that is exactly one credential yields exactly the marker, for any
    credential of any length, so the emitted length carries no information about what was
    removed.
    """
    for value in (shorter, longer):
        registry = CredentialRegistry()
        registry.register(value)

        assert registry.redact(value) == REDACTION_MARKER

    short_registry, long_registry = CredentialRegistry(), CredentialRegistry()
    short_registry.register(shorter)
    long_registry.register(longer)

    assert len(short_registry.redact(shorter)) == len(long_registry.redact(longer))
