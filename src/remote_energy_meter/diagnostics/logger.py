"""The Diagnostics_Logger: one structured record per event, on the addon log stream.

Supervisor presents whatever the addon writes to stdout as the addon log, so this module
configures the stdlib ``logging`` module to emit exactly one line per record there, and
nothing else in the process ever writes to stdout.

Four rules from Requirement 9.1 shape every record, and each is enforced here rather
than at the call sites:

* a UTC timestamp with at least whole-second resolution — taken from ``record.created``
  and rendered to millisecond precision, always UTC, never local time;
* **exactly one** severity from ``debug``, ``info``, ``warning``, ``error`` — a record
  logged at any other stdlib level (``critical``, or a custom numeric level) is still
  reported as one of the four, so the field's domain is closed;
* the Device_Entry identifier whenever the record concerns one device — passed as
  ``extra={DEVICE_ID_FIELD: ...}`` and surfaced as a top-level field;
* a message of 1 to 512 characters — an over-long message is truncated with an ellipsis
  to exactly 512, an empty one becomes :data:`EMPTY_MESSAGE`.

Level resolution follows Requirements 9.2, 9.6, and 9.7: the configured value when it is
an *exact* member of the set, otherwise ``info`` plus one warning record naming what was
rejected. Matching is exact and not case-folded, because Requirement 9.2 fixes the set in
lower case and Requirement 9.7 sends everything else down the fallback path — accepting
``INFO`` silently would make the fallback untestable and the accepted set undefined.

Requirement 9.3 constrains the *logged* failure taxonomy to four values while
:class:`FailureClassification` carries seven, because the internal classifications drive
different retry and accumulator behavior. :func:`logged_classification` is the total
mapping between them, written as a ``match`` over the enum with no catch-all so that
``mypy --strict`` reports a missing return — at build time — the day an eighth
classification is added.

Redaction is installed here as well, because a log stream with no redaction filter is a
credential leak waiting for its first exception. :func:`install_redaction_filter` is
called *after* the handler is attached, so the filter lands on both the logger and the
handler; that is what covers records propagated up from child loggers.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import IO, Any, Final

from remote_energy_meter.config.models import LogLevel
from remote_energy_meter.diagnostics.redaction import (
    CredentialRedactionFilter,
    CredentialRegistry,
    install_redaction_filter,
    is_valid_exception_info,
)
from remote_energy_meter.plugins.interface import FailureClassification

__all__ = [
    "DEVICE_ID_FIELD",
    "EMPTY_MESSAGE",
    "LOGGER_NAME",
    "LOG_LEVEL_RANK",
    "MAX_MESSAGE_LENGTH",
    "TRUNCATION_SUFFIX",
    "LevelResolution",
    "LoggedFailureClassification",
    "LoggingSetup",
    "StructuredFormatter",
    "configure_logging",
    "get_logger",
    "log_read_failure",
    "logged_classification",
    "resolve_log_level",
    "severity_of",
    "stdlib_level",
    "truncate_message",
]

#: Requirement 9.1: the accepted message length bound.
MAX_MESSAGE_LENGTH: Final = 512

#: Appended when a formatted message would exceed :data:`MAX_MESSAGE_LENGTH`. The
#: truncated result is exactly ``MAX_MESSAGE_LENGTH`` characters, suffix included.
TRUNCATION_SUFFIX: Final = "..."

#: Requirement 9.1 sets a *minimum* of one character, so an empty message needs a stand-in
#: rather than an empty field.
EMPTY_MESSAGE: Final = "(no message)"

#: The record field carrying the Device_Entry identifier. Passed through ``extra=`` and
#: promoted to a top-level field by :class:`StructuredFormatter`.
DEVICE_ID_FIELD: Final = "device_id"

#: The package logger. Components call ``get_logger(__name__)``, whose records propagate
#: to the configured root handler.
LOGGER_NAME: Final = "remote_energy_meter"

#: The severity ranking of Requirement 9.2, ``debug < info < warning < error``, derived
#: from :class:`LogLevel`'s declaration order rather than restated.
LOG_LEVEL_RANK: Final = MappingProxyType({level: rank for rank, level in enumerate(LogLevel)})

_STDLIB_LEVELS: Final = MappingProxyType(
    {
        LogLevel.DEBUG: logging.DEBUG,
        LogLevel.INFO: logging.INFO,
        LogLevel.WARNING: logging.WARNING,
        LogLevel.ERROR: logging.ERROR,
    }
)

#: Attributes ``logging`` itself owns. They are structural — level, source location,
#: timings — and are rendered explicitly or not at all, so they must not be re-emitted as
#: caller-supplied structured fields.
_RESERVED_RECORD_ATTRS: Final[frozenset[str]] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


# --------------------------------------------------------------------------------------------------
# the four logged failure classifications (Requirement 9.3)
# --------------------------------------------------------------------------------------------------


class LoggedFailureClassification(StrEnum):
    """The only failure classifications a log record may carry (Requirement 9.3).

    Four values against the seven of :class:`FailureClassification`: an administrator
    reading the addon log needs to know whether to fix a credential, chase a network, or
    fix a certificate. Everything else is "the read did not produce a usable value", and
    splitting that four ways in the log would not change what the administrator does.
    """

    AUTHENTICATION = "authentication"
    TIMEOUT = "timeout"
    TLS_VALIDATION = "tls_validation"
    READ = "read"


def logged_classification(classification: FailureClassification) -> LoggedFailureClassification:
    """Map an internal failure classification onto the one value a log record may carry.

    Total by construction: a ``match`` over every enum member with **no catch-all**, so
    adding a member to :class:`FailureClassification` without extending this mapping is a
    ``mypy --strict`` error ("missing return statement") rather than a surprise in a log
    record.
    """
    match classification:
        case FailureClassification.AUTHENTICATION:
            return LoggedFailureClassification.AUTHENTICATION
        case FailureClassification.TIMEOUT:
            return LoggedFailureClassification.TIMEOUT
        case FailureClassification.TLS_VALIDATION:
            return LoggedFailureClassification.TLS_VALIDATION
        case FailureClassification.COMMUNICATION:
            return LoggedFailureClassification.READ
        case FailureClassification.RESPONSE_FORMAT:
            return LoggedFailureClassification.READ
        case FailureClassification.INVALID_MEASUREMENT:
            return LoggedFailureClassification.READ
        case FailureClassification.PLUGIN:
            return LoggedFailureClassification.READ


# --------------------------------------------------------------------------------------------------
# level resolution (Requirements 9.2, 9.6, 9.7)
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LevelResolution:
    """The effective log level, and what was rejected to arrive at it.

    ``rejected_value`` is set if and only if a value was supplied that is not an exact
    member of the accepted set, which is exactly when Requirement 9.7 requires one
    warning record naming it. An omitted level is not a rejection (Requirement 9.6).
    """

    level: LogLevel
    rejected_value: str | None = None

    @property
    def fell_back(self) -> bool:
        """True when the configured value was rejected and ``info`` was substituted."""
        return self.rejected_value is not None


def resolve_log_level(configured: object) -> LevelResolution:
    """Resolve the configured log level value to an effective :class:`LogLevel`.

    ``None`` means the Addon_Configuration omitted the level, which resolves to ``info``
    with no rejection (Requirement 9.6). Any value that is not an exact member of
    ``debug``, ``info``, ``warning``, ``error`` also resolves to ``info``, but is reported
    as a rejection so the caller emits the warning of Requirement 9.7.

    ``configured`` is typed as ``object`` deliberately: it arrives from parsed JSON and
    may be a number or a boolean, and those must take the documented fallback path rather
    than raising somewhere further in.
    """
    if configured is None:
        return LevelResolution(LogLevel.INFO)
    if isinstance(configured, LogLevel):
        return LevelResolution(configured)
    if isinstance(configured, str):
        try:
            return LevelResolution(LogLevel(configured))
        except ValueError:
            return LevelResolution(LogLevel.INFO, rejected_value=configured)
    return LevelResolution(LogLevel.INFO, rejected_value=repr(configured))


def stdlib_level(level: LogLevel) -> int:
    """The stdlib ``logging`` numeric level for one accepted log level."""
    return _STDLIB_LEVELS[level]


def severity_of(levelno: int) -> LogLevel:
    """Collapse any stdlib numeric level onto exactly one of the four severities.

    Requirement 9.1 closes the severity domain, so ``critical`` and any custom numeric
    level have to land somewhere. They land on the nearest accepted level at or below
    them, and anything under ``info`` reports as ``debug``.
    """
    if levelno >= logging.ERROR:
        return LogLevel.ERROR
    if levelno >= logging.WARNING:
        return LogLevel.WARNING
    if levelno >= logging.INFO:
        return LogLevel.INFO
    return LogLevel.DEBUG


# --------------------------------------------------------------------------------------------------
# record rendering
# --------------------------------------------------------------------------------------------------


def truncate_message(text: str) -> str:
    """Bound ``text`` to 1-512 characters (Requirement 9.1).

    An over-long message keeps its first characters and ends with an ellipsis, because
    the leading text is where the event name and the identifiers are. An empty message
    becomes :data:`EMPTY_MESSAGE`.

    Called from the formatter, which runs *after* the redaction filter, so truncation can
    never expose a credential fragment that redaction would have removed.
    """
    if not text:
        return EMPTY_MESSAGE
    if len(text) <= MAX_MESSAGE_LENGTH:
        return text
    return text[: MAX_MESSAGE_LENGTH - len(TRUNCATION_SUFFIX)] + TRUNCATION_SUFFIX


def _utc_timestamp(created: float) -> str:
    """Render a record's creation time as a UTC timestamp with millisecond resolution."""
    moment = datetime.fromtimestamp(created, tz=UTC)
    return f"{moment:%Y-%m-%dT%H:%M:%S}.{moment.microsecond // 1000:03d}Z"


class StructuredFormatter(logging.Formatter):
    """Renders one log record as one line of JSON.

    JSON rather than a human-shaped line for two reasons: structured fields passed through
    ``extra=`` survive as fields instead of being flattened into prose, and embedded
    newlines in a message or a traceback are escaped, so "one record, one line" holds even
    for a multi-line exception rendering.

    Field order is fixed — timestamp, level, logger, message, device identifier, then any
    caller-supplied fields — so records are readable in a terminal without a JSON tool.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Render ``record``. Never raises on a caller's bad format string or odd value."""
        payload: dict[str, Any] = {
            "timestamp": _utc_timestamp(record.created),
            "level": severity_of(record.levelno).value,
            "logger": record.name,
            "message": truncate_message(self._render(record)),
        }

        device_id = record.__dict__.get(DEVICE_ID_FIELD)
        if device_id is not None:
            payload[DEVICE_ID_FIELD] = str(device_id)

        for name, value in record.__dict__.items():
            if name in _RESERVED_RECORD_ATTRS or name == DEVICE_ID_FIELD:
                continue
            payload[name] = value

        exception_text = self._exception_text(record)
        if exception_text:
            payload["exception"] = exception_text
        if record.stack_info:
            payload["stack"] = record.stack_info

        return json.dumps(payload, ensure_ascii=False, default=str)

    @staticmethod
    def _render(record: logging.LogRecord) -> str:
        """Interpolate the message, tolerating a mismatched format string.

        The redaction filter normally renders and rewrites ``msg`` before a handler sees
        the record, in which case this is a plain string return. When no credential is
        registered the filter short-circuits, so interpolation still happens here — and a
        ``%``-formatting mistake must produce a diagnosable record rather than a logging
        error swallowed by ``logging.raiseExceptions``.
        """
        try:
            return record.getMessage()
        except (TypeError, ValueError, KeyError, IndexError):
            return f"{record.msg!s} {record.args!r}"

    def _exception_text(self, record: logging.LogRecord) -> str:
        """Render only trusted exception metadata, preferring preformatted redacted text."""
        if isinstance(record.exc_text, str):
            return record.exc_text

        exc_info: object = record.exc_info
        if not is_valid_exception_info(exc_info):
            return ""
        try:
            return self.formatException(exc_info)
        except Exception:
            # Even a structurally valid tuple may hold an exception with a broken ``__str__``.
            # Logging must remain best-effort and emit the rest of the structured record.
            return ""


# --------------------------------------------------------------------------------------------------
# installation
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LoggingSetup:
    """What :func:`configure_logging` installed.

    ``redaction_filter`` is returned so a caller that adds a handler later can attach the
    same filter instance to it — a handler without the filter is an uncovered path to the
    log stream.
    """

    level: LogLevel
    rejected_value: str | None
    handler: logging.Handler
    redaction_filter: CredentialRedactionFilter


def configure_logging(
    configured_level: object = None,
    *,
    registry: CredentialRegistry | None = None,
    stream: IO[str] | None = None,
    logger: logging.Logger | None = None,
) -> LoggingSetup:
    """Install the structured handler, the effective level, and the redaction filter.

    Order matters in two places. The handler is attached *before*
    :func:`install_redaction_filter`, so the filter lands on the logger and on the
    handler; a filter on the logger alone would miss every record propagated up from a
    child logger. And the Requirement 9.7 warning is emitted *after* installation, so it
    travels the same redacted, structured path as every other record.

    Idempotent: a previously installed structured handler is removed first, so calling
    this twice — startup, then again once the configuration is known — leaves exactly one
    handler rather than duplicating every record.

    :param configured_level: the raw configured log level, or ``None`` when omitted.
    :param registry: the credential registry the redaction filter consults; the
        process-wide registry by default.
    :param stream: where records are written; ``sys.stdout`` by default, which is what
        Supervisor presents as the addon log.
    :param logger: the logger to configure; the root logger by default.
    """
    target = logger if logger is not None else logging.getLogger()
    resolution = resolve_log_level(configured_level)
    level_no = stdlib_level(resolution.level)

    for existing in list(target.handlers):
        if isinstance(existing.formatter, StructuredFormatter):
            target.removeHandler(existing)

    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(StructuredFormatter())
    handler.setLevel(level_no)
    target.addHandler(handler)
    target.setLevel(level_no)

    redaction_filter = install_redaction_filter(registry, target)
    if not any(isinstance(existing, CredentialRedactionFilter) for existing in handler.filters):
        handler.addFilter(redaction_filter)

    if resolution.rejected_value is not None:
        target.warning(
            "configured log level %s is not one of %s; using %s",
            resolution.rejected_value,
            ", ".join(level.value for level in LogLevel),
            resolution.level.value,
        )

    return LoggingSetup(
        level=resolution.level,
        rejected_value=resolution.rejected_value,
        handler=handler,
        redaction_filter=redaction_filter,
    )


def get_logger(name: str | None = None) -> logging.Logger:
    """The logger a component should use.

    ``get_logger(__name__)`` from inside the package returns that module's logger, whose
    records propagate to the handler :func:`configure_logging` installed. A name from
    outside the package is namespaced under :data:`LOGGER_NAME` so it cannot escape the
    configured level or the redaction filter.
    """
    if name is None:
        return logging.getLogger(LOGGER_NAME)
    if name == LOGGER_NAME or name.startswith(f"{LOGGER_NAME}."):
        return logging.getLogger(name)
    return logging.getLogger(f"{LOGGER_NAME}.{name}")


def log_read_failure(
    logger: logging.Logger,
    device_id: str,
    classification: FailureClassification,
    detail: str | None = None,
) -> None:
    """Record one failed power reading operation (Requirement 9.3).

    Exactly one warning record, naming the Device_Entry identifier and exactly one of the
    four logged classifications. ``detail`` is the plugin's context — a resource path or a
    host address — and is untrusted text: the redaction filter scrubs credential fragments
    out of it before any handler formats the record.
    """
    logged = logged_classification(classification)
    extra: dict[str, Any] = {DEVICE_ID_FIELD: device_id, "classification": logged.value}
    if detail is not None:
        extra["detail"] = detail
    logger.warning("power reading failed: %s", logged.value, extra=extra)
