"""Properties 33, 34, and 35: the log record shape, level filtering, and failure classification (tasks 3.4-3.6).

Three statements about ``diagnostics/logger.py``, each over an input space too large to
enumerate:

* **Property 33** — every emitted record carries a UTC timestamp with at least whole-second
  resolution, exactly one severity from the four accepted values, the Device_Entry
  identifier whenever the event concerns one device, and a message of 1 to 512 characters
  (Requirement 9.1);
* **Property 34** — the emitted records are exactly those whose severity ranks at or above
  the effective level, where the effective level is the configured value when it is an
  exact member of the set and ``info`` otherwise, and a non-member configured value
  additionally produces one warning record naming what was rejected (Requirements 9.2, 9.7);
* **Property 35** — a failed power reading operation produces exactly one warning record
  naming the Device_Entry identifier and exactly one classification drawn from
  ``authentication``, ``timeout``, ``tls_validation``, ``read``, under a mapping that is
  total over the seven internal classifications (Requirement 9.3).

## Property 33: the generated event space

Requirement 9.1 fixes four things about every record the Diagnostics_Logger writes, and
this module establishes all four across arbitrary events rather than the handful of
examples in ``tests/unit/test_logger.py``: a UTC timestamp with at least whole-second
resolution, exactly one severity from the four accepted values, the Device_Entry
identifier whenever the event concerns one device, and a message of 1 to 512 characters.

The generated event space is deliberately hostile in the places where the shape is easiest
to break:

* **message text** — empty (the one-character minimum needs a stand-in), longer than 512
  characters (truncation), multi-line, and non-ASCII;
* **severity** — arbitrary stdlib numeric levels, including ``critical`` and custom values
  above ``error`` and below ``debug``, because Requirement 9.1 closes the severity domain
  at four values while ``logging`` has no such bound;
* **structured fields** — records with and without a device identifier, and with arbitrary
  caller-supplied ``extra=`` fields, so promoting the device identifier to a top-level
  field cannot quietly drop or rename anything else;
* **exception info** — a real traceback, whose rendering is inherently multi-line.

"One record, one line" is asserted by splitting the stream on ``"\\n"`` rather than with
``str.splitlines()``. ``"\\n"`` is what ``StreamHandler`` writes as the record terminator
and what a log consumer reads, whereas ``splitlines()`` also breaks on U+0085, U+2028, and
U+2029 — characters a message may legitimately contain and that no line-oriented log
reader treats as a record boundary.

## Property 34: why the ranking, the level bands, and the resolution rule are restated here

The severity ranking, the accepted set, and the fallback rule are all written out in this
module from Requirements 9.2, 9.6, and 9.7 rather than imported from the module under test.
Importing ``LOG_LEVEL_RANK`` and calling ``resolve_log_level`` to compute the expectation
would assert only that the implementation equals itself; a generated configured value is
worth generating only when something independent says what it should resolve to.

Each event is generated as a *severity* plus a stdlib numeric level drawn from that
severity's band, which is what lets the expectation be independent: the event knows the
severity it must report as by construction, so the test never has to re-derive it with a
copy of ``severity_of``. The bands cover the custom numeric levels ``logging`` permits and
Requirement 9.1 collapses onto four values, ``critical`` among them.

Bands start at ``logging.DEBUG``, and a custom level *below* ``debug`` is deliberately out
of scope. Requirement 9.2 constrains which records may be emitted, not which must be, so
suppressing a level-5 record when the effective level is ``debug`` satisfies the
requirement while reading as a violation of the "exactly" in Property 34. Nothing in the
addon logs below ``debug``, so the band floor is where the two readings agree.

## Property 35: where the expectation comes from, and how a new classification fails

The expectation is computed from Requirement 9.3's *vocabulary*, not from the
implementation's mapping table. Requirement 9.3 fixes the logged set at ``authentication``,
``timeout``, ``tls_validation``, ``read``. Three of those four name a specific remedy an
administrator can act on, and each is also the name of an internal classification; ``read``
names no internal classification and is therefore the catch-all for "the read did not
produce a usable value". So the rule is: an internal classification whose own name is one of
the four logged values maps to that value, and every other internal classification maps to
``read``. Importing ``LoggedFailureClassification`` and ``logged_classification`` to build
the expectation would assert only that the implementation equals itself, and restating the
seven pairs by hand would only repeat ``tests/unit/test_logger.py``.

Totality is established by construction rather than asserted afterwards: every example
covers a permutation of the whole ``FailureClassification`` enum, so an unmapped member
cannot be missed by the draw. The ``match`` in ``logged_classification`` has no catch-all,
so an unmapped member returns ``None`` and this property fails on the very first record for
it. Extra draws are appended after the permutation so classifications repeat, which is what
makes *functionality* — one logged value per internal value, every time, not merely once —
observable at all.

:data:`EXPECTED_INTERNAL_CLASSIFICATIONS` pins the seven internal names as a taxonomy
guard. Adding an eighth classification is meant to fail here even when the implementation
mapping was extended correctly, because the name-correspondence rule would otherwise
silently expect the new value to collapse onto ``read``. Failing forces the choice of
logged value to be made deliberately, in the requirement and in this test, rather than
inherited from a default.

Generators are local to this module rather than shared, so this file stands alone.
"""

from __future__ import annotations

import io
import json
import logging
import re
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any, Final

from hypothesis import given, settings
from hypothesis import strategies as st

from remote_energy_meter.config.models import LogLevel
from remote_energy_meter.diagnostics.logger import (
    DEVICE_ID_FIELD,
    EMPTY_MESSAGE,
    MAX_MESSAGE_LENGTH,
    TRUNCATION_SUFFIX,
    LoggingSetup,
    configure_logging,
    log_read_failure,
    severity_of,
)
from remote_energy_meter.diagnostics.redaction import CredentialRegistry
from remote_energy_meter.plugins.interface import FailureClassification

#: An isolated logger name, so configuring it cannot disturb the root logger or pytest.
TEST_LOGGER_NAME: Final = "remote_energy_meter_property_33"

#: The four values Requirement 9.1 allows in the severity field.
ACCEPTED_SEVERITIES: Final[frozenset[str]] = frozenset(level.value for level in LogLevel)

#: The record terminator ``StreamHandler`` writes, and the only record boundary a
#: line-oriented log consumer recognizes.
RECORD_SEPARATOR: Final = "\n"

#: A UTC timestamp with at least whole-second resolution. The seconds group is what
#: Requirement 9.1's resolution floor demands; the milliseconds are the surplus this
#: implementation provides.
TIMESTAMP_PATTERN: Final = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z\Z")

#: The fixed leading field order of :class:`StructuredFormatter`.
LEADING_FIELDS: Final = ("timestamp", "level", "logger", "message")


# --------------------------------------------------------------------------------------------------
# generators
# --------------------------------------------------------------------------------------------------

# Restricted to what UTF-8 can encode, which excludes lone surrogates: a lone surrogate is
# not text a logger can be handed through any real code path, and the addon log stream is
# UTF-8. Every other category stays in, including control characters and the line and
# paragraph separators.
_TEXT_CHARS = st.characters(codec="utf-8")


def message_texts() -> st.SearchStrategy[str]:
    """Message text spanning the boundaries Requirement 9.1 draws.

    Four shapes, each targeting a different part of the length rule: empty (below the
    one-character minimum), ordinary, exactly at and around the 512-character bound, and
    comfortably over it. Multi-line and non-ASCII text arrive through the general text
    strategy and through the explicit newline-joined shape.
    """
    return st.one_of(
        st.just(""),
        st.text(_TEXT_CHARS, max_size=40),
        st.text(_TEXT_CHARS, min_size=MAX_MESSAGE_LENGTH - 2, max_size=MAX_MESSAGE_LENGTH + 2),
        st.text(_TEXT_CHARS, min_size=MAX_MESSAGE_LENGTH + 1, max_size=MAX_MESSAGE_LENGTH * 2),
        st.lists(st.text(_TEXT_CHARS, max_size=30), min_size=2, max_size=5).map("\n".join),
        st.sampled_from(["", " ", "\n", "%s %d", "µΩ 温度 🔌", "x" * MAX_MESSAGE_LENGTH]),
    )


def stdlib_levels() -> st.SearchStrategy[int]:
    """Any stdlib numeric level, not only the four that have accepted names.

    Requirement 9.1 closes the severity domain at four values, so ``critical``, a level
    above it, and a custom level below ``debug`` all have to report as one of the four.
    """
    return st.one_of(
        st.sampled_from(
            [
                logging.DEBUG,
                logging.INFO,
                logging.WARNING,
                logging.ERROR,
                logging.CRITICAL,
            ]
        ),
        st.integers(min_value=1, max_value=60),
    )


def device_identifiers() -> st.SearchStrategy[str]:
    """Device identifiers in the form the Configuration_Parser accepts (Requirement 2.12)."""
    return st.from_regex(r"[A-Za-z0-9_-]{1,64}", fullmatch=True)


def extra_fields() -> st.SearchStrategy[dict[str, Any]]:
    """Caller-supplied structured fields.

    Keys carry an ``x_`` prefix so they cannot collide with an attribute ``logging`` owns —
    a collision raises from ``Logger.log`` itself and would test the stdlib rather than the
    formatter. Values cover the JSON scalar space a call site realistically passes.
    """
    return st.dictionaries(
        keys=st.from_regex(r"x_[a-z0-9_]{1,8}", fullmatch=True),
        values=st.one_of(
            st.integers(min_value=-(10**9), max_value=10**9),
            st.floats(allow_nan=False, allow_infinity=False, width=32),
            st.booleans(),
            st.text(_TEXT_CHARS, max_size=20),
            st.none(),
        ),
        max_size=4,
    )


@dataclass(frozen=True, slots=True)
class LogEvent:
    """One event handed to the logger, and everything the record shape depends on."""

    message: str
    levelno: int
    device_id: str | None
    extra: dict[str, Any] = field(default_factory=dict)
    exception_message: str | None = None
    with_stack: bool = False


@st.composite
def log_events(draw: st.DrawFn) -> LogEvent:
    """One arbitrary log event."""
    return LogEvent(
        message=draw(message_texts()),
        levelno=draw(stdlib_levels()),
        device_id=draw(st.one_of(st.none(), device_identifiers())),
        extra=draw(extra_fields()),
        exception_message=draw(st.one_of(st.none(), st.text(_TEXT_CHARS, max_size=60))),
        with_stack=draw(st.booleans()),
    )


# --------------------------------------------------------------------------------------------------
# emission
# --------------------------------------------------------------------------------------------------


def _raise(message: str) -> None:
    """Stand in for a plugin raising during a read, so the record carries a real traceback."""
    raise RuntimeError(message)


@contextmanager
def configured_logger() -> Iterator[tuple[logging.Logger, io.StringIO]]:
    """An isolated logger writing structured records to an in-memory stream.

    Both the logger and the handler are dropped to level 1 after installation, so a
    generated custom level below ``debug`` still reaches the formatter; filtering by level
    is Property 34's subject, and suppressing records here would silently shrink the
    severity space this property is meant to cover.
    """
    sink = io.StringIO()
    logger = logging.getLogger(TEST_LOGGER_NAME)
    saved_level, saved_propagate = logger.level, logger.propagate
    logger.propagate = False
    try:
        setup = configure_logging(LogLevel.DEBUG, registry=CredentialRegistry(), stream=sink, logger=logger)
        logger.setLevel(1)
        setup.handler.setLevel(1)
        yield logger, sink
    finally:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
        logger.filters.clear()
        logger.setLevel(saved_level)
        logger.propagate = saved_propagate


def emit(logger: logging.Logger, event: LogEvent) -> None:
    """Log one event the way a component would."""
    extra = dict(event.extra)
    if event.device_id is not None:
        extra[DEVICE_ID_FIELD] = event.device_id

    if event.exception_message is None:
        logger.log(event.levelno, event.message, extra=extra, stack_info=event.with_stack)
        return

    try:
        _raise(event.exception_message)
    except RuntimeError:
        logger.log(event.levelno, event.message, extra=extra, exc_info=True, stack_info=event.with_stack)


def expected_fields(event: LogEvent) -> set[str]:
    """Exactly the fields the record for ``event`` must carry."""
    names = set(LEADING_FIELDS) | set(event.extra)
    if event.device_id is not None:
        names.add(DEVICE_ID_FIELD)
    if event.exception_message is not None:
        names.add("exception")
    if event.with_stack:
        names.add("stack")
    return names


# --------------------------------------------------------------------------------------------------
# the property
# --------------------------------------------------------------------------------------------------


# Feature: remote-energy-meter, Property 33: For any sequence of emitted log events, including events
# whose formatted message exceeds 512 characters, each emitted record carries a UTC timestamp with at
# least whole-second resolution, exactly one severity level from `debug`, `info`, `warning`, `error`,
# the Device_Entry identifier whenever the event concerns a single Device_Entry, and a message of 1 to
# 512 characters.
# Validates: Requirements 9.1
@settings(max_examples=200, deadline=None)
@given(events=st.lists(log_events(), min_size=1, max_size=8))
def test_every_log_record_has_the_required_shape(events: list[LogEvent]) -> None:
    """Requirement 9.1, over arbitrary events."""
    with configured_logger() as (logger, sink):
        before = datetime.now(UTC)
        for event in events:
            emit(logger, event)
        after = datetime.now(UTC)
        stream = sink.getvalue()

    # One record, one line: the stream is a sequence of complete lines, one per event.
    assert stream.endswith(RECORD_SEPARATOR)
    lines = stream.split(RECORD_SEPARATOR)[:-1]
    assert len(lines) == len(events)

    for event, line in zip(events, lines, strict=True):
        record: dict[str, Any] = json.loads(line)

        # --- the record carries exactly the expected fields, in the documented order ---
        assert set(record) == expected_fields(event)
        assert tuple(record)[: len(LEADING_FIELDS)] == LEADING_FIELDS

        # --- a UTC timestamp with at least whole-second resolution ---
        timestamp = record["timestamp"]
        assert isinstance(timestamp, str)
        assert TIMESTAMP_PATTERN.match(timestamp) is not None
        moment = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S.%f%z")
        assert moment.utcoffset() == timedelta(0)
        assert before - timedelta(seconds=1) <= moment <= after + timedelta(seconds=1)

        # --- exactly one severity, drawn from the four accepted values ---
        severity = record["level"]
        assert severity in ACCEPTED_SEVERITIES
        assert severity == severity_of(event.levelno).value

        # --- the Device_Entry identifier when, and only when, the event concerns one device ---
        if event.device_id is None:
            assert DEVICE_ID_FIELD not in record
        else:
            assert record[DEVICE_ID_FIELD] == event.device_id
            assert tuple(record)[len(LEADING_FIELDS)] == DEVICE_ID_FIELD

        # --- a message of 1 to 512 characters, and no record boundary inside it ---
        message = record["message"]
        assert isinstance(message, str)
        assert 1 <= len(message) <= MAX_MESSAGE_LENGTH
        assert RECORD_SEPARATOR not in line
        if not event.message:
            assert message == EMPTY_MESSAGE
        elif len(event.message) <= MAX_MESSAGE_LENGTH:
            assert message == event.message
        else:
            assert len(message) == MAX_MESSAGE_LENGTH
            assert message.endswith(TRUNCATION_SUFFIX)
            assert message == event.message[: MAX_MESSAGE_LENGTH - len(TRUNCATION_SUFFIX)] + TRUNCATION_SUFFIX

        # --- caller-supplied structure survives unchanged ---
        for name, value in event.extra.items():
            assert record[name] == value

        # --- a traceback is escaped into the record, not spread across lines ---
        if event.exception_message is not None:
            assert "RuntimeError" in record["exception"]
        if event.with_stack:
            assert isinstance(record["stack"], str)


# --------------------------------------------------------------------------------------------------
# Property 34: level filtering and level fallback (Requirements 9.2, 9.7)
# --------------------------------------------------------------------------------------------------

#: A second isolated logger name. Property 33's logger is forced down to level 1 after
#: installation; this one keeps whatever level was resolved, which is the whole subject
#: here, so the two must not share a logger.
LEVEL_TEST_LOGGER_NAME: Final = "remote_energy_meter_property_34"

#: The severity ranking Requirement 9.2 fixes, written out rather than imported from the
#: module under test.
EXPECTED_RANK: Final = MappingProxyType(
    {
        LogLevel.DEBUG: 0,
        LogLevel.INFO: 1,
        LogLevel.WARNING: 2,
        LogLevel.ERROR: 3,
    }
)

#: The stdlib numeric level range that reports as each severity. Each band's floor is the
#: canonical stdlib level for that severity, and the rest of the band is the custom levels
#: ``logging`` permits — ``critical`` (50) lives inside the ``error`` band.
SEVERITY_BANDS: Final = MappingProxyType(
    {
        LogLevel.DEBUG: (logging.DEBUG, logging.INFO - 1),
        LogLevel.INFO: (logging.INFO, logging.WARNING - 1),
        LogLevel.WARNING: (logging.WARNING, logging.ERROR - 1),
        LogLevel.ERROR: (logging.ERROR, logging.CRITICAL + 10),
    }
)

#: Carried in ``extra=`` on every generated event, so an emitted record identifies its
#: position in the generated sequence and is distinguishable from the Requirement 9.7
#: rejection notice, which carries no such field.
SEQUENCE_FIELD: Final = "x_seq"


# --------------------------------------------------------------------------------------------------
# generators
# --------------------------------------------------------------------------------------------------


def configured_level_values() -> st.SearchStrategy[object]:
    """Configured log level values: exact members, and everything Requirement 9.7 catches.

    ``None`` is the omitted level of Requirement 9.6. The near-miss family is written out
    rather than left to free text because those are the values a hand-edited addon
    configuration actually contains — case variants, stray whitespace, truncations, and the
    neighbouring ``logging`` level names — and they are exactly what a case-folded or
    prefix match would wrongly accept. Non-string values arrive from parsed JSON, where a
    bare ``10`` or ``true`` under ``log_level`` is a plausible mistake.

    Values stay short so the rejection notice fits inside the 512-character message bound;
    truncation is Property 33's subject, not this one's.
    """
    accepted = sorted(ACCEPTED_SEVERITIES)
    return st.one_of(
        st.none(),
        st.sampled_from(list(LogLevel)),
        st.sampled_from(accepted),
        st.sampled_from(
            [
                "DEBUG",
                "Info",
                "WARNING",
                "Error",
                " info",
                "info ",
                "info\n",
                "\tdebug",
                "inf",
                "warn",
                "err",
                "critical",
                "fatal",
                "trace",
                "verbose",
                "notset",
                "none",
                "null",
                "",
                " ",
                "10",
                "info,debug",
                "level=info",
            ]
        ),
        st.text(_TEXT_CHARS, max_size=24).filter(lambda value: value not in ACCEPTED_SEVERITIES),
        st.integers(min_value=-10, max_value=60),
        st.booleans(),
        st.floats(allow_nan=False, allow_infinity=False, width=32),
        st.lists(st.sampled_from(accepted), max_size=2),
        st.dictionaries(st.just("level"), st.sampled_from(accepted), max_size=1),
    )


@dataclass(frozen=True, slots=True)
class LevelEvent:
    """One event handed to the logger, carrying the severity it must report as."""

    severity: LogLevel
    levelno: int
    message: str
    device_id: str | None


@st.composite
def level_events(draw: st.DrawFn) -> LevelEvent:
    """One event at a known severity, logged at a stdlib level inside that severity's band.

    Half the draws use the canonical stdlib level for the severity, the rest any level in
    the band, so the sequences cover both what a component logs through
    ``logger.warning`` and the custom numeric levels Requirement 9.1 has to collapse.
    """
    severity = draw(st.sampled_from(list(LogLevel)))
    floor, ceiling = SEVERITY_BANDS[severity]
    return LevelEvent(
        severity=severity,
        levelno=draw(st.one_of(st.just(floor), st.integers(min_value=floor, max_value=ceiling))),
        message=draw(st.text(_TEXT_CHARS, max_size=30)),
        device_id=draw(st.one_of(st.none(), device_identifiers())),
    )


# --------------------------------------------------------------------------------------------------
# the expectation, and emission
# --------------------------------------------------------------------------------------------------


def expected_resolution(configured: object) -> tuple[LogLevel, tuple[str, ...] | None]:
    """The effective level for ``configured``, and how a notice may name a rejected value.

    Read off Requirements 9.2, 9.6, and 9.7: ``None`` is the omitted level and resolves to
    ``info`` with nothing rejected, an *exact* member of the set is used as configured, and
    every other value resolves to ``info`` and is reported. A :class:`LogLevel` member is a
    ``str`` equal to its own value, so it takes the exact-member path.

    The second element is ``None`` when no notice is due, and otherwise the renderings that
    count as naming the value — a non-string may reasonably be named by either its text or
    its ``repr``.
    """
    if configured is None:
        return LogLevel.INFO, None
    if isinstance(configured, str):
        if configured in ACCEPTED_SEVERITIES:
            return LogLevel(configured), None
        return LogLevel.INFO, (configured,)
    return LogLevel.INFO, (str(configured), repr(configured))


@contextmanager
def level_configured_logger(configured: object) -> Iterator[tuple[logging.Logger, io.StringIO, LoggingSetup]]:
    """An isolated logger configured from ``configured``, left at whatever level that resolves to.

    The one difference from :func:`configured_logger` is that nothing is lowered afterwards:
    the installed level does the filtering this property is about.
    """
    sink = io.StringIO()
    logger = logging.getLogger(LEVEL_TEST_LOGGER_NAME)
    saved_level, saved_propagate = logger.level, logger.propagate
    logger.propagate = False
    try:
        setup = configure_logging(configured, registry=CredentialRegistry(), stream=sink, logger=logger)
        yield logger, sink, setup
    finally:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
        logger.filters.clear()
        logger.setLevel(saved_level)
        logger.propagate = saved_propagate


def emit_at_level(logger: logging.Logger, index: int, event: LevelEvent) -> None:
    """Log one event the way a component would, tagged with its position in the sequence."""
    extra: dict[str, Any] = {SEQUENCE_FIELD: index}
    if event.device_id is not None:
        extra[DEVICE_ID_FIELD] = event.device_id
    logger.log(event.levelno, event.message, extra=extra)


def parse_records(stream: str) -> list[dict[str, Any]]:
    """The records written to ``stream``, in emission order, one per line."""
    if not stream:
        return []
    assert stream.endswith(RECORD_SEPARATOR)
    return [json.loads(line) for line in stream.split(RECORD_SEPARATOR)[:-1]]


# --------------------------------------------------------------------------------------------------
# the property
# --------------------------------------------------------------------------------------------------


# Feature: remote-energy-meter, Property 34: For any configured log level value and any sequence of log
# events, the emitted records are exactly those whose severity ranks at or above the effective level under
# `debug` < `info` < `warning` < `error`, where the effective level is the configured value when it is an
# exact member of the set and `info` otherwise, and a non-member configured value additionally produces one
# warning record naming the rejected value.
# Validates: Requirements 9.2, 9.7
@settings(max_examples=200, deadline=None)
@given(configured=configured_level_values(), events=st.lists(level_events(), min_size=1, max_size=8))
def test_level_filtering_and_fallback_are_exact(configured: object, events: list[LevelEvent]) -> None:
    """Requirements 9.2 and 9.7, over arbitrary configured values and event sequences."""
    expected_level, rejected_renderings = expected_resolution(configured)

    with level_configured_logger(configured) as (logger, sink, setup):
        for index, event in enumerate(events):
            emit_at_level(logger, index, event)
        stream = sink.getvalue()

    records = parse_records(stream)

    # --- the effective level is the configured value when it is an exact member, info otherwise ---
    assert setup.level is expected_level
    assert (setup.rejected_value is None) == (rejected_renderings is None)
    assert setup.handler.level == SEVERITY_BANDS[expected_level][0]

    # --- a non-member value produces exactly one warning record naming it, ahead of every event ---
    notices = [record for record in records if SEQUENCE_FIELD not in record]
    if rejected_renderings is None:
        assert notices == []
    else:
        assert len(notices) == 1
        assert records[0] is notices[0]
        assert notices[0]["level"] == LogLevel.WARNING.value
        assert any(rendering in notices[0]["message"] for rendering in rejected_renderings)

    # --- the event records are exactly those whose severity ranks at or above the effective level ---
    emitted = [record for record in records if SEQUENCE_FIELD in record]
    expected_indexes = [
        index for index, event in enumerate(events) if EXPECTED_RANK[event.severity] >= EXPECTED_RANK[expected_level]
    ]
    assert [record[SEQUENCE_FIELD] for record in emitted] == expected_indexes

    for record in emitted:
        event = events[record[SEQUENCE_FIELD]]

        # --- each survivor reports its own severity, and that severity clears the level ---
        assert record["level"] in ACCEPTED_SEVERITIES
        assert record["level"] == event.severity.value
        assert EXPECTED_RANK[LogLevel(record["level"])] >= EXPECTED_RANK[expected_level]

        # --- filtering does not disturb what the record carries ---
        if event.device_id is None:
            assert DEVICE_ID_FIELD not in record
        else:
            assert record[DEVICE_ID_FIELD] == event.device_id


# --------------------------------------------------------------------------------------------------
# Property 35: the failure classification mapping (Requirement 9.3)
# --------------------------------------------------------------------------------------------------

#: A third isolated logger name. Property 33's logger is forced down to level 1 and
#: Property 34's carries whatever the generated configuration resolved to; this one is fixed
#: at ``debug`` so a warning record is never filtered away, so none of the three may share.
FAILURE_TEST_LOGGER_NAME: Final = "remote_energy_meter_property_35"

#: The four values Requirement 9.3 permits in a failure record, written out rather than
#: imported from :class:`LoggedFailureClassification`.
LOGGED_CLASSIFICATIONS: Final[frozenset[str]] = frozenset({"authentication", "timeout", "tls_validation", "read"})

#: The one logged value of Requirement 9.3 that names no internal classification, and is
#: therefore the catch-all every unnamed internal classification collapses onto.
CATCH_ALL_CLASSIFICATION: Final = "read"

#: The internal classification set this property was written against. Pinned as a taxonomy
#: guard: an added classification must be assigned a logged value deliberately rather than
#: inheriting ``read`` from the name-correspondence rule below.
EXPECTED_INTERNAL_CLASSIFICATIONS: Final[frozenset[str]] = frozenset(
    {
        "authentication",
        "timeout",
        "tls_validation",
        "communication",
        "response_format",
        "invalid_measurement",
        "plugin",
    }
)

#: The record field carrying the logged classification.
CLASSIFICATION_FIELD: Final = "classification"


# --------------------------------------------------------------------------------------------------
# generators
# --------------------------------------------------------------------------------------------------


def _names_no_classification(text: str) -> bool:
    """True when ``text`` spells none of the four logged classifications.

    Used to keep generated identifiers and details out of the counting below: a device
    identifier spelled ``timeout`` would make "the record names exactly one classification"
    ambiguous, and that ambiguity would be an artifact of how the test counts rather than
    anything the implementation does.
    """
    return text.lower() not in LOGGED_CLASSIFICATIONS


def failure_device_identifiers() -> st.SearchStrategy[str]:
    """Device identifiers (Requirement 2.12), excluding the four logged classification names."""
    return device_identifiers().filter(_names_no_classification)


def failure_details() -> st.SearchStrategy[str | None]:
    """The plugin's failure context: a resource path, a host address, or nothing.

    ``None`` is the omitted detail, and the empty string is the degenerate one a plugin can
    still hand over. The sampled shapes are the text Requirements 4.5 and 4.12 actually put
    here. Free text is filtered so no detail *contains* a logged classification name, for the
    same counting reason as :func:`failure_device_identifiers`.
    """
    return st.one_of(
        st.none(),
        st.sampled_from(
            [
                "",
                "/redfish/v1/Chassis/System.Embedded.1/Power",
                "/redfish/v1/Chassis/System.Embedded.1/Power?$select=PowerControl",
                "198.51.100.7:443",
                "idrac-a1.example",
                "https://idrac-b2.example.net/redfish/v1",
                "PowerControl[0] carries no consumed watts member",
                "HTTP 503",
                "certificate verify failed: self-signed certificate",
                "-12.5 W outside 0-10000 W",
            ]
        ),
        st.from_regex(r"[A-Za-z0-9 ./:_-]{1,40}", fullmatch=True).filter(
            lambda text: not any(token in text.lower() for token in LOGGED_CLASSIFICATIONS)
        ),
    )


@dataclass(frozen=True, slots=True)
class ReadFailureEvent:
    """One failed power reading operation, as the scheduler would report it."""

    device_id: str
    classification: FailureClassification
    detail: str | None


@st.composite
def failure_sequences(draw: st.DrawFn) -> list[ReadFailureEvent]:
    """Failed reads covering every internal classification at least once, in arbitrary order.

    A permutation of the whole enum, so the mapping is exercised exhaustively in every
    example and a member added to :class:`FailureClassification` is carried into this
    property automatically. Extra draws are appended so a classification can repeat, which is
    what makes the mapping's functionality observable rather than merely its totality.
    """
    members = list(FailureClassification)
    order = draw(st.permutations(members))
    extra = draw(st.lists(st.sampled_from(members), max_size=4))
    return [
        ReadFailureEvent(
            device_id=draw(failure_device_identifiers()),
            classification=classification,
            detail=draw(failure_details()),
        )
        for classification in [*order, *extra]
    ]


# --------------------------------------------------------------------------------------------------
# the expectation, and emission
# --------------------------------------------------------------------------------------------------


def expected_logged_classification(classification: FailureClassification) -> str:
    """The logged value Requirement 9.3 allows for ``classification``.

    Read off Requirement 9.3's vocabulary alone: three of the four logged values name a
    specific remedy and are each also the name of an internal classification, so an internal
    classification that shares a name with a logged value maps to it. ``read`` names no
    internal classification and is the catch-all for everything that merely failed to produce
    a usable value.
    """
    if classification.value in LOGGED_CLASSIFICATIONS:
        return classification.value
    return CATCH_ALL_CLASSIFICATION


@contextmanager
def failure_logger() -> Iterator[tuple[logging.Logger, io.StringIO]]:
    """An isolated logger fixed at ``debug``, so a Requirement 9.3 warning is never filtered.

    Left at the level ``configure_logging`` resolved rather than lowered afterwards: ``debug``
    already admits every warning, and which records survive a level is Property 34's subject,
    not this one's.
    """
    sink = io.StringIO()
    logger = logging.getLogger(FAILURE_TEST_LOGGER_NAME)
    saved_level, saved_propagate = logger.level, logger.propagate
    logger.propagate = False
    try:
        configure_logging(LogLevel.DEBUG, registry=CredentialRegistry(), stream=sink, logger=logger)
        yield logger, sink
    finally:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
        logger.filters.clear()
        logger.setLevel(saved_level)
        logger.propagate = saved_propagate


# --------------------------------------------------------------------------------------------------
# the property
# --------------------------------------------------------------------------------------------------


# Feature: remote-energy-meter, Property 35: For any internal failure classification, a failed power
# reading operation produces exactly one warning record naming the Device_Entry identifier and exactly
# one classification drawn from `authentication`, `timeout`, `tls_validation`, `read`, according to a
# total mapping over the internal classification set.
# Validates: Requirements 9.3
@settings(max_examples=200, deadline=None)
@given(failures=failure_sequences())
def test_failure_classification_maps_onto_exactly_one_logged_value(failures: list[ReadFailureEvent]) -> None:
    """Requirement 9.3, over every internal classification and arbitrary identifiers and details."""
    # --- the internal set is the one the expectation below was derived for ---
    assert {member.value for member in FailureClassification} == EXPECTED_INTERNAL_CLASSIFICATIONS

    with failure_logger() as (logger, sink):
        for failure in failures:
            log_read_failure(logger, failure.device_id, failure.classification, failure.detail)
        stream = sink.getvalue()

    emitted = parse_records(stream)

    # --- exactly one record per failed reading operation ---
    assert len(emitted) == len(failures)

    observed: dict[FailureClassification, set[str]] = defaultdict(set)
    for failure, record in zip(failures, emitted, strict=True):
        # --- at warning severity, naming the Device_Entry identifier ---
        assert record["level"] == LogLevel.WARNING.value
        assert record[DEVICE_ID_FIELD] == failure.device_id

        # --- exactly one classification, drawn from the four of Requirement 9.3 ---
        logged = record[CLASSIFICATION_FIELD]
        assert isinstance(logged, str)
        assert logged in LOGGED_CLASSIFICATIONS
        # The prose message names the same classification by design, and "reading" contains
        # "read", so counting classification names across the message would be meaningless.
        # Every other field is generated so it can only hold a classification by carrying one.
        named = {
            name: value
            for name, value in record.items()
            if name != "message" and isinstance(value, str) and value in LOGGED_CLASSIFICATIONS
        }
        assert named == {CLASSIFICATION_FIELD: logged}

        # --- and it is the value Requirement 9.3's vocabulary implies for this classification ---
        assert logged == expected_logged_classification(failure.classification)
        observed[failure.classification].add(logged)

    # --- total over the internal set: every classification produced a logged value ---
    assert set(observed) == set(FailureClassification)

    # --- and functional: one logged value per internal classification, on every occurrence ---
    assert {classification: len(values) for classification, values in observed.items()} == dict.fromkeys(
        FailureClassification, 1
    )
