"""Unit tests for the Diagnostics_Logger (task 3.2).

Covers the record shape, level resolution and fallback, the seven-to-four failure
classification mapping, and the redaction filter reaching records that propagate up from a
child logger. Exhaustive coverage of the record shape across arbitrary events is Property
33 (task 3.4), level filtering is Property 34 (task 3.5), and the classification mapping
across the whole enum is Property 35 (task 3.6).
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from remote_energy_meter.config.models import LogLevel
from remote_energy_meter.diagnostics.logger import (
    DEVICE_ID_FIELD,
    EMPTY_MESSAGE,
    LOG_LEVEL_RANK,
    LOGGER_NAME,
    MAX_MESSAGE_LENGTH,
    TRUNCATION_SUFFIX,
    LoggedFailureClassification,
    StructuredFormatter,
    configure_logging,
    get_logger,
    log_read_failure,
    logged_classification,
    resolve_log_level,
    severity_of,
    stdlib_level,
    truncate_message,
)
from remote_energy_meter.diagnostics.redaction import REDACTION_MARKER, CredentialRegistry
from remote_energy_meter.plugins.interface import FailureClassification

TEST_LOGGER_NAME = "remote_energy_meter_test"


@pytest.fixture
def sink() -> io.StringIO:
    return io.StringIO()


@pytest.fixture
def target() -> Iterator[logging.Logger]:
    """An isolated logger, so configuring it cannot disturb the root logger or pytest."""
    logger = logging.getLogger(TEST_LOGGER_NAME)
    saved_level, saved_propagate = logger.level, logger.propagate
    logger.propagate = False
    try:
        yield logger
    finally:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
        logger.filters.clear()
        logger.setLevel(saved_level)
        logger.propagate = saved_propagate


def records(sink: io.StringIO) -> list[dict[str, Any]]:
    """Every emitted record, parsed. Asserts the one-record-per-line guarantee."""
    lines = sink.getvalue().splitlines()
    parsed: list[dict[str, Any]] = [json.loads(line) for line in lines if line]
    return parsed


# --------------------------------------------------------------------------------------------------
# the seven-to-four failure classification mapping (Requirement 9.3)
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("internal", "expected"),
    [
        (FailureClassification.AUTHENTICATION, LoggedFailureClassification.AUTHENTICATION),
        (FailureClassification.TIMEOUT, LoggedFailureClassification.TIMEOUT),
        (FailureClassification.TLS_VALIDATION, LoggedFailureClassification.TLS_VALIDATION),
        (FailureClassification.COMMUNICATION, LoggedFailureClassification.READ),
        (FailureClassification.RESPONSE_FORMAT, LoggedFailureClassification.READ),
        (FailureClassification.INVALID_MEASUREMENT, LoggedFailureClassification.READ),
        (FailureClassification.PLUGIN, LoggedFailureClassification.READ),
    ],
)
def test_classification_mapping_follows_the_design_table(
    internal: FailureClassification, expected: LoggedFailureClassification
) -> None:
    """The design's mapping table, value for value."""
    assert logged_classification(internal) == expected


def test_classification_mapping_is_total_over_the_internal_enum() -> None:
    """A new internal classification must not fall through to a default."""
    for internal in FailureClassification:
        assert logged_classification(internal) in set(LoggedFailureClassification)


def test_logged_classifications_are_exactly_the_four_of_requirement_9_3() -> None:
    assert {logged.value for logged in LoggedFailureClassification} == {
        "authentication",
        "timeout",
        "tls_validation",
        "read",
    }


def test_log_read_failure_emits_one_warning_naming_the_device_and_one_classification(
    target: logging.Logger, sink: io.StringIO
) -> None:
    """Requirement 9.3: one warning record, the entry identifier, exactly one classification."""
    configure_logging(LogLevel.DEBUG, registry=CredentialRegistry(), stream=sink, logger=target)

    log_read_failure(target, "poweredge_a", FailureClassification.RESPONSE_FORMAT, detail="/redfish/v1/x")

    emitted = records(sink)
    assert len(emitted) == 1
    assert emitted[0]["level"] == "warning"
    assert emitted[0][DEVICE_ID_FIELD] == "poweredge_a"
    assert emitted[0]["classification"] == "read"
    assert emitted[0]["detail"] == "/redfish/v1/x"


# --------------------------------------------------------------------------------------------------
# level resolution (Requirements 9.2, 9.6, 9.7)
# --------------------------------------------------------------------------------------------------


def test_omitted_level_resolves_to_info_without_a_rejection() -> None:
    """Requirement 9.6."""
    resolution = resolve_log_level(None)
    assert resolution.level is LogLevel.INFO
    assert resolution.rejected_value is None
    assert not resolution.fell_back


@pytest.mark.parametrize("level", list(LogLevel))
def test_each_accepted_level_resolves_to_itself(level: LogLevel) -> None:
    """Requirement 9.2: an exact member of the set is used as configured."""
    assert resolve_log_level(level.value) == resolve_log_level(level)
    assert resolve_log_level(level.value).level is level
    assert resolve_log_level(level.value).rejected_value is None


@pytest.mark.parametrize("configured", ["INFO", "Debug", "verbose", "", " info", "trace"])
def test_out_of_set_string_falls_back_to_info_and_reports_the_rejected_value(configured: str) -> None:
    """Requirement 9.7. Matching is exact and not case-folded, so ``INFO`` is a rejection."""
    resolution = resolve_log_level(configured)
    assert resolution.level is LogLevel.INFO
    assert resolution.rejected_value == configured
    assert resolution.fell_back


@pytest.mark.parametrize("configured", [5, True, 1.5, ["info"], {"level": "info"}])
def test_non_string_level_falls_back_to_info(configured: object) -> None:
    """A value arriving from JSON as a number or a container takes the same fallback path."""
    resolution = resolve_log_level(configured)
    assert resolution.level is LogLevel.INFO
    assert resolution.fell_back


def test_level_ranking_is_the_requirement_9_2_order() -> None:
    ranks = [LOG_LEVEL_RANK[level] for level in (LogLevel.DEBUG, LogLevel.INFO, LogLevel.WARNING, LogLevel.ERROR)]
    assert ranks == sorted(ranks)
    assert len(set(ranks)) == len(ranks)


@pytest.mark.parametrize(
    ("levelno", "expected"),
    [
        (logging.NOTSET, LogLevel.DEBUG),
        (5, LogLevel.DEBUG),
        (logging.DEBUG, LogLevel.DEBUG),
        (logging.INFO, LogLevel.INFO),
        (logging.WARNING, LogLevel.WARNING),
        (logging.ERROR, LogLevel.ERROR),
        (logging.CRITICAL, LogLevel.ERROR),
        (99, LogLevel.ERROR),
    ],
)
def test_every_stdlib_level_reports_as_exactly_one_accepted_severity(levelno: int, expected: LogLevel) -> None:
    """Requirement 9.1 closes the severity domain, including for ``critical``."""
    assert severity_of(levelno) is expected


def test_out_of_set_level_emits_one_warning_naming_the_rejected_value(
    target: logging.Logger, sink: io.StringIO
) -> None:
    """Requirement 9.7: the fallback is announced, once, on the configured stream."""
    setup = configure_logging("verbose", registry=CredentialRegistry(), stream=sink, logger=target)

    assert setup.level is LogLevel.INFO
    emitted = records(sink)
    assert len(emitted) == 1
    assert emitted[0]["level"] == "warning"
    assert "verbose" in emitted[0]["message"]


def test_accepted_level_emits_no_fallback_warning(target: logging.Logger, sink: io.StringIO) -> None:
    setup = configure_logging("warning", registry=CredentialRegistry(), stream=sink, logger=target)

    assert setup.level is LogLevel.WARNING
    assert records(sink) == []


@pytest.mark.parametrize("configured", list(LogLevel))
def test_only_records_at_or_above_the_effective_level_are_emitted(
    configured: LogLevel, target: logging.Logger, sink: io.StringIO
) -> None:
    """Requirement 9.2, one example per configured level."""
    configure_logging(configured, registry=CredentialRegistry(), stream=sink, logger=target)

    target.debug("d")
    target.info("i")
    target.warning("w")
    target.error("e")

    expected = [level.value for level in LogLevel if LOG_LEVEL_RANK[level] >= LOG_LEVEL_RANK[configured]]
    assert [record["level"] for record in records(sink)] == expected


# --------------------------------------------------------------------------------------------------
# record shape (Requirement 9.1)
# --------------------------------------------------------------------------------------------------


def test_record_carries_timestamp_severity_and_message(target: logging.Logger, sink: io.StringIO) -> None:
    configure_logging(LogLevel.DEBUG, registry=CredentialRegistry(), stream=sink, logger=target)

    target.info("startup complete", extra={"accepted_devices": 2})

    (record,) = records(sink)
    assert record["level"] == "info"
    assert record["message"] == "startup complete"
    assert record["accepted_devices"] == 2
    moment = datetime.strptime(record["timestamp"], "%Y-%m-%dT%H:%M:%S.%f%z")
    assert moment.tzinfo is not None
    assert moment.utcoffset() == UTC.utcoffset(None)


def test_device_identifier_is_a_top_level_field_when_the_record_concerns_one_device(
    target: logging.Logger, sink: io.StringIO
) -> None:
    configure_logging(LogLevel.DEBUG, registry=CredentialRegistry(), stream=sink, logger=target)

    target.info("poll cycle skipped", extra={DEVICE_ID_FIELD: "poweredge_b"})
    target.info("plugin discovered")

    with_device, without_device = records(sink)
    assert with_device[DEVICE_ID_FIELD] == "poweredge_b"
    assert DEVICE_ID_FIELD not in without_device


def test_each_record_is_one_line_even_with_a_multi_line_message(target: logging.Logger, sink: io.StringIO) -> None:
    configure_logging(LogLevel.DEBUG, registry=CredentialRegistry(), stream=sink, logger=target)

    target.error("first line\nsecond line")

    assert len(sink.getvalue().splitlines()) == 1
    (record,) = records(sink)
    assert record["message"] == "first line\nsecond line"


def test_interpolated_arguments_reach_the_message(target: logging.Logger, sink: io.StringIO) -> None:
    configure_logging(LogLevel.DEBUG, registry=CredentialRegistry(), stream=sink, logger=target)

    target.info("device %s took %d ms", "poweredge_a", 42)

    (record,) = records(sink)
    assert record["message"] == "device poweredge_a took 42 ms"


def test_a_mismatched_format_string_still_produces_a_record(target: logging.Logger, sink: io.StringIO) -> None:
    """A logging mistake must not silently drop the diagnostic it was carrying."""
    configure_logging(LogLevel.DEBUG, registry=CredentialRegistry(), stream=sink, logger=target)

    target.info("expects two: %s %s", "only-one")

    (record,) = records(sink)
    assert "only-one" in record["message"]


def _explode(message: str) -> None:
    """Stand in for a plugin raising during a read, so the record carries a real traceback."""
    raise RuntimeError(message)


def test_exception_text_is_rendered_as_its_own_field(target: logging.Logger, sink: io.StringIO) -> None:
    configure_logging(LogLevel.DEBUG, registry=CredentialRegistry(), stream=sink, logger=target)

    try:
        _explode("plugin exploded")
    except RuntimeError:
        target.error("read failed", exc_info=True)

    (record,) = records(sink)
    assert "plugin exploded" in record["exception"]
    assert len(sink.getvalue().splitlines()) == 1


# --------------------------------------------------------------------------------------------------
# message bounds (Requirement 9.1)
# --------------------------------------------------------------------------------------------------


def test_a_message_at_the_boundary_is_left_alone() -> None:
    exact = "x" * MAX_MESSAGE_LENGTH
    assert truncate_message(exact) == exact


def test_a_message_one_character_over_the_boundary_is_truncated_to_the_boundary() -> None:
    truncated = truncate_message("x" * (MAX_MESSAGE_LENGTH + 1))
    assert len(truncated) == MAX_MESSAGE_LENGTH
    assert truncated.endswith(TRUNCATION_SUFFIX)
    assert truncated.startswith("x" * (MAX_MESSAGE_LENGTH - len(TRUNCATION_SUFFIX)))


def test_an_empty_message_gets_a_stand_in() -> None:
    """Requirement 9.1 sets a minimum of one character."""
    assert truncate_message("") == EMPTY_MESSAGE
    assert 1 <= len(EMPTY_MESSAGE) <= MAX_MESSAGE_LENGTH


def test_an_over_long_emitted_message_is_truncated_in_the_record(target: logging.Logger, sink: io.StringIO) -> None:
    configure_logging(LogLevel.DEBUG, registry=CredentialRegistry(), stream=sink, logger=target)

    target.info("%s", "y" * (MAX_MESSAGE_LENGTH * 2))

    (record,) = records(sink)
    assert len(record["message"]) == MAX_MESSAGE_LENGTH
    assert record["message"].endswith(TRUNCATION_SUFFIX)


# --------------------------------------------------------------------------------------------------
# installation: redaction coverage and idempotence
# --------------------------------------------------------------------------------------------------


def test_records_from_a_child_logger_are_redacted(target: logging.Logger, sink: io.StringIO) -> None:
    """Requirement 8.2 through the propagation path a filter on the logger alone would miss."""
    registry = CredentialRegistry()
    registry.register("Sup3rSecretPassw0rd")
    configure_logging(LogLevel.DEBUG, registry=registry, stream=sink, logger=target)

    logging.getLogger(f"{TEST_LOGGER_NAME}.plugins.idrac").error("auth failed for Sup3rSecretPassw0rd")

    (record,) = records(sink)
    assert "Sup3rSecretPassw0rd" not in sink.getvalue()
    assert REDACTION_MARKER in record["message"]


def test_credential_fragments_in_exception_text_are_redacted(target: logging.Logger, sink: io.StringIO) -> None:
    """Requirement 8.7."""
    registry = CredentialRegistry()
    registry.register("Sup3rSecretPassw0rd")
    configure_logging(LogLevel.DEBUG, registry=registry, stream=sink, logger=target)

    try:
        _explode("login rejected: Sup3rSecretPassw0rd")
    except RuntimeError:
        target.error("read failed", exc_info=True)

    assert "Sup3rSecretPassw0rd" not in sink.getvalue()
    assert REDACTION_MARKER in records(sink)[0]["exception"]


def test_reconfiguring_leaves_exactly_one_structured_handler(target: logging.Logger, sink: io.StringIO) -> None:
    """Startup configures the level twice; records must not double up."""
    configure_logging(LogLevel.INFO, registry=CredentialRegistry(), stream=sink, logger=target)
    configure_logging(LogLevel.DEBUG, registry=CredentialRegistry(), stream=sink, logger=target)

    target.info("once")

    structured = [h for h in target.handlers if isinstance(h.formatter, StructuredFormatter)]
    assert len(structured) == 1
    assert len(records(sink)) == 1


def test_setup_reports_what_it_installed(target: logging.Logger, sink: io.StringIO) -> None:
    registry = CredentialRegistry()
    setup = configure_logging("error", registry=registry, stream=sink, logger=target)

    assert setup.level is LogLevel.ERROR
    assert setup.rejected_value is None
    assert setup.handler.level == stdlib_level(LogLevel.ERROR)
    assert setup.redaction_filter.registry is registry
    assert setup.handler in target.handlers


# --------------------------------------------------------------------------------------------------
# logger naming
# --------------------------------------------------------------------------------------------------


def test_get_logger_namespaces_names_under_the_package_logger() -> None:
    assert get_logger().name == LOGGER_NAME
    assert get_logger(LOGGER_NAME).name == LOGGER_NAME
    assert get_logger(f"{LOGGER_NAME}.scheduler").name == f"{LOGGER_NAME}.scheduler"
    assert get_logger("scheduler").name == f"{LOGGER_NAME}.scheduler"


# --------------------------------------------------------------------------------------------------
# third-party exception metadata hardening
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "malformed",
    [
        pytest.param(True, id="true"),
        pytest.param(False, id="false"),
        pytest.param("not-an-exception-triple", id="string"),
        pytest.param(object(), id="object"),
        pytest.param((), id="empty-tuple"),
        pytest.param((RuntimeError,), id="short-tuple"),
        pytest.param((RuntimeError, RuntimeError("boom"), None, "extra"), id="long-tuple"),
        pytest.param((RuntimeError, "not-an-exception", None), id="invalid-value"),
        pytest.param((RuntimeError, RuntimeError("boom"), object()), id="invalid-traceback"),
    ],
)
def test_malformed_exc_info_never_breaks_structured_json(malformed: object) -> None:
    """Third-party ``LogRecord`` values are untrusted; the useful record must still be emitted."""
    record = logging.LogRecord(
        name="third_party",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="third-party failure",
        args=(),
        exc_info=None,
    )
    record.__dict__["exc_info"] = malformed

    rendered = StructuredFormatter().format(record)
    payload = json.loads(rendered)

    assert payload["message"] == "third-party failure"
    assert payload["level"] == "error"
    assert "exception" not in payload


def test_preformatted_exception_text_wins_over_malformed_exc_info() -> None:
    """A filter-provided redacted traceback is authoritative even if another handler damaged exc_info."""
    record = logging.LogRecord(
        name="third_party",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="third-party failure",
        args=(),
        exc_info=None,
    )
    record.exc_text = "already formatted and redacted"
    record.__dict__["exc_info"] = True

    payload = json.loads(StructuredFormatter().format(record))

    assert payload["exception"] == "already formatted and redacted"
