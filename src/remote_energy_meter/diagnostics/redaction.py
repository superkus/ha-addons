"""Credential redaction: the last line of defence between a secret and a log record.

Requirement 8.2 is stronger than "do not print the password". It forbids a log record
from containing *any substring of a credential value of 4 or more characters*, and it
requires every redaction to be the same fixed-length marker so the marker cannot leak
the credential's length. Requirement 8.7 extends the same guarantee to exception text.

This module implements the third of the design's three layers:

1. **Typed values** — credential fields are :class:`pydantic.SecretStr`, so an
   accidental interpolation of a config object renders ``**********``. Plaintext is
   reachable only through an explicit ``get_secret_value()`` call.
2. **A registry plus a logging filter** — every credential value is registered at
   startup; :class:`CredentialRedactionFilter` rewrites each record before any handler
   formats it. That is this module.
3. **Exception scrubbing** — the same filter renders and rewrites ``exc_info``, so a
   plugin exception whose message embeds a password is scrubbed before it reaches the
   log stream.

## 4-gram shielding

For each registered credential the filter derives the set of its 4-character windows
(*4-grams*). Scanning a record's text left to right, a position whose 4-character window
is one of those grams is a *hit*. The hit is expanded to the **maximal span** that is
still a substring of the credential — growing right, then left, to a fixpoint — and the
whole span is replaced with :data:`REDACTION_MARKER`.

Cost is bounded: a credential is at most :data:`MAX_CREDENTIAL_LENGTH` characters, so it
contributes at most ``256 - 4 + 1 = 253`` distinct windows, gram membership is a set
lookup, and span growth is bounded by the credential's own length.

The failure direction is deliberately unsafe-for-readability and safe-for-secrecy: a
coincidental 4-character collision with unrelated text redacts a few harmless
characters. **Over-redacting is always preferred to under-redacting.** Two consequences
follow that callers should know about:

* A registered credential shorter than 4 characters has no 4-grams, so it is redacted by
  exact match only. A 1-character credential therefore replaces every occurrence of that
  character. The parser rejects empty credentials (Requirement 8.5); absurdly short ones
  are accepted and will garble the log. Secrecy still holds.
* Redaction is idempotent, because :data:`REDACTION_MARKER` occurrences are excised from
  the scan and re-joined afterwards, so a marker is never itself scanned.

## Where the filter has to be attached

A ``logging.Filter`` on a *logger* only sees records logged through that logger, not
records that propagate up to it from a child logger — ``Logger.handle`` applies the
logger's own filters, and ``callHandlers`` then applies each *handler's* filters. So
attaching this filter to the root logger alone would leave records from
``logging.getLogger("remote_energy_meter")`` unscrubbed. :func:`install_redaction_filter`
attaches it to the logger *and* to every one of its handlers, which is what actually
covers every record. Handlers added later must be given the filter too; the installer
returns it for exactly that reason.
"""

from __future__ import annotations

import logging
import threading
import traceback
from collections.abc import Iterable
from dataclasses import dataclass
from types import TracebackType
from typing import Final, TypeGuard

from pydantic import BaseModel, SecretStr

from remote_energy_meter.config.models import credential_field_names

__all__ = [
    "MAX_CREDENTIAL_LENGTH",
    "REDACTION_MARKER",
    "SHIELD_WINDOW",
    "CredentialRedactionFilter",
    "CredentialRegistry",
    "install_redaction_filter",
    "is_valid_exception_info",
]

#: The fixed replacement for every redacted span. Constant, so its length reveals
#: nothing about the length of the credential it replaced (Requirement 8.2).
REDACTION_MARKER: Final = "***REDACTED***"

#: The shielding window width. A substring of a credential of at least this many
#: characters must never appear in a log record (Requirement 8.2).
SHIELD_WINDOW: Final = 4

#: Requirement 8.8: the parser rejects a credential field longer than this, which is
#: what bounds the number of windows one credential contributes to at most
#: ``MAX_CREDENTIAL_LENGTH - SHIELD_WINDOW + 1`` (253).
MAX_CREDENTIAL_LENGTH: Final = 256

#: How deep :class:`CredentialRedactionFilter` walks containers found on record
#: attributes. Structured log records nest a level or two; a bound keeps a cyclic or
#: pathological value from turning a log call into a hang.
_MAX_SCRUB_DEPTH: Final = 4

#: How deep :meth:`CredentialRegistry.register_model` walks nested models.
_MAX_MODEL_DEPTH: Final = 8

type ExceptionInfo = tuple[type[BaseException], BaseException, TracebackType | None]


def is_valid_exception_info(value: object) -> TypeGuard[ExceptionInfo]:
    """Whether ``value`` is safe to pass to a stdlib exception formatter.

    Third-party handlers sometimes put booleans, lists, or tuple-shaped placeholders into
    ``LogRecord.exc_info``. Shape alone is insufficient: traceback formatting also expects an
    exception class, an exception instance, and either a real traceback or ``None``.
    """
    if not isinstance(value, tuple) or len(value) != 3:
        return False
    exc_type, exc_value, exc_traceback = value
    return (
        isinstance(exc_type, type)
        and issubclass(exc_type, BaseException)
        and isinstance(exc_value, BaseException)
        and (exc_traceback is None or isinstance(exc_traceback, TracebackType))
    )


#: Record attributes ``logging`` itself owns. They are structural — logger name,
#: level, source location, timings — and are never scrubbed, both because they cannot
#: carry a credential and because rewriting them would corrupt formatting. ``msg``,
#: ``args``, ``exc_info``, ``exc_text``, and ``stack_info`` are excluded here because the
#: filter handles them explicitly rather than as generic attributes.
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


@dataclass(frozen=True, slots=True)
class _Shield:
    """One registered credential with its 4-gram window set precomputed."""

    value: str
    grams: frozenset[str]

    @classmethod
    def of(cls, value: str) -> _Shield:
        windows = {value[i : i + SHIELD_WINDOW] for i in range(len(value) - SHIELD_WINDOW + 1)}
        return cls(value=value, grams=frozenset(windows))


# --------------------------------------------------------------------------------------------------
# the pure shielding algorithm
# --------------------------------------------------------------------------------------------------


def _shield_text(text: str, shield: _Shield) -> str:
    """Replace every maximal substring of ``shield.value`` of at least 4 characters in ``text``.

    Scans left to right. At a hit, the span grows right and left to a fixpoint while it
    remains a substring of the credential, then the whole span becomes one marker.

    Left growth is retained even though a left-to-right scan makes it provably
    unreachable: a span reaching left of the hit would imply an earlier position whose
    window is also a gram, and that position was already scanned. It is kept because it
    costs nothing and because it keeps the function correct under any future change to
    the scan order — an unsafe direction to leave to an invariant that lives in a
    comment.
    """
    text_length = len(text)
    if text_length < SHIELD_WINDOW:
        return text

    secret = shield.value
    grams = shield.grams
    pieces: list[str] = []
    emitted = 0  # everything before this index is already in ``pieces``
    index = 0
    last_start = text_length - SHIELD_WINDOW

    while index <= last_start:
        if text[index : index + SHIELD_WINDOW] not in grams:
            index += 1
            continue

        start, end = index, index + SHIELD_WINDOW
        growing = True
        while growing:
            growing = False
            if end < text_length and text[start : end + 1] in secret:
                end += 1
                growing = True
            if start > emitted and text[start - 1 : end] in secret:
                start -= 1
                growing = True

        pieces.append(text[emitted:start])
        pieces.append(REDACTION_MARKER)
        emitted = end
        index = end

    if not pieces:
        return text
    pieces.append(text[emitted:])
    return "".join(pieces)


def _redact_segment(text: str, shields: tuple[_Shield, ...]) -> str:
    """Apply every shield to one marker-free segment of text."""
    for shield in shields:
        if len(shield.value) < SHIELD_WINDOW:
            # No windows exist, so exact match is all that can be asked for. Requirement
            # 8.2's first clause still forbids the value itself appearing verbatim.
            text = text.replace(shield.value, REDACTION_MARKER)
        else:
            text = _shield_text(text, shield)
    return text


def _redact(text: str, shields: tuple[_Shield, ...]) -> str:
    """Redact ``text``, leaving existing markers intact so the operation is idempotent."""
    if not shields or not text:
        return text
    if REDACTION_MARKER not in text:
        return _redact_segment(text, shields)
    return REDACTION_MARKER.join(_redact_segment(segment, shields) for segment in text.split(REDACTION_MARKER))


# --------------------------------------------------------------------------------------------------
# the registry
# --------------------------------------------------------------------------------------------------


class CredentialRegistry:
    """The set of values that must never reach a log record or an MQTT payload.

    Requirement 8.1 fixes what goes in here: every MQTT connection username and
    password, and every ``Device_Entry`` field that the named plugin's configuration
    schema marks as a credential field. :meth:`register_model` reads those markers
    through ``credential_field_names``, so a plugin the core has never seen gets the
    same protection as the iDRAC plugin without the core knowing its parameter names.

    Registration is guarded by a lock and publishes an immutable snapshot, because
    ``logging`` records can be emitted from any thread while startup is still
    registering values. Readers take the snapshot in one attribute load and never see a
    partially built set.
    """

    __slots__ = ("_lock", "_shields", "_values")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: set[str] = set()
        self._shields: tuple[_Shield, ...] = ()

    # -- registration ------------------------------------------------------------------

    def register(self, value: str | SecretStr | None) -> None:
        """Register one credential value.

        ``None``, and any value that is empty once leading and trailing whitespace is
        removed, is ignored: the parser rejects those entries outright (Requirement 8.5)
        and registering whitespace would redact every space in every record.

        Both the value as given and its stripped form are registered, so a credential
        configured with stray whitespace is caught whether it reaches the log stream
        trimmed or untrimmed.
        """
        if value is None:
            return
        plaintext = value.get_secret_value() if isinstance(value, SecretStr) else value
        if not plaintext.strip():
            return
        with self._lock:
            before = len(self._values)
            self._values.add(plaintext)
            self._values.add(plaintext.strip())
            if len(self._values) != before:
                self._rebuild_locked()

    def register_all(self, values: Iterable[str | SecretStr | None]) -> None:
        """Register every credential value in ``values``."""
        for value in values:
            self.register(value)

    def register_model(self, model: BaseModel) -> None:
        """Register every credential-marked field of ``model`` and of its nested models.

        Nested models *are* walked here, unlike ``credential_field_names``, which
        deliberately leaves traversal policy to its caller. This is that caller, and for
        a security backstop the safe policy is to walk: a ``ConfigurationObject`` carries
        its broker credentials one level down in ``mqtt``, and a plugin is free to nest
        its parameters. Over-registering costs a little log readability; under-
        registering costs a secret.
        """
        self._register_model(model, depth=0, seen=set())

    def _register_model(self, model: BaseModel, *, depth: int, seen: set[int]) -> None:
        if depth > _MAX_MODEL_DEPTH or id(model) in seen:
            return
        seen.add(id(model))

        marked = frozenset(credential_field_names(type(model)))
        for name in type(model).model_fields:
            value = getattr(model, name, None)
            if name in marked:
                self._register_marked(value, depth=depth, seen=seen)
            else:
                self._walk_for_models(value, depth=depth, seen=seen)

    def _register_marked(self, value: object, *, depth: int, seen: set[int]) -> None:
        """A credential-marked field: register scalar values, walk containers."""
        if isinstance(value, SecretStr | str):
            self.register(value)
        elif isinstance(value, BaseModel):
            self._register_model(value, depth=depth + 1, seen=seen)
        elif isinstance(value, dict):
            for item in value.values():
                self._register_marked(item, depth=depth + 1, seen=seen)
        elif isinstance(value, list | tuple | set | frozenset):
            for item in value:
                self._register_marked(item, depth=depth + 1, seen=seen)

    def _walk_for_models(self, value: object, *, depth: int, seen: set[int]) -> None:
        """An unmarked field: descend looking for nested models that mark their own fields."""
        if isinstance(value, BaseModel):
            self._register_model(value, depth=depth + 1, seen=seen)
        elif isinstance(value, dict):
            for item in value.values():
                self._walk_for_models(item, depth=depth + 1, seen=seen)
        elif isinstance(value, list | tuple | set | frozenset):
            for item in value:
                self._walk_for_models(item, depth=depth + 1, seen=seen)

    def clear(self) -> None:
        """Forget every registered value. Used by tests; the process never needs it."""
        with self._lock:
            self._values.clear()
            self._shields = ()

    def _rebuild_locked(self) -> None:
        # Longest first, so a credential that contains another is shielded before the
        # shorter one fragments it. Ties break on the value itself, which makes the
        # redaction of any given text deterministic and therefore testable.
        self._shields = tuple(_Shield.of(value) for value in sorted(self._values, key=lambda v: (-len(v), v)))

    # -- use ---------------------------------------------------------------------------

    @property
    def is_empty(self) -> bool:
        """True when nothing has been registered, so redaction is a no-op."""
        return not self._shields

    def redact(self, text: str) -> str:
        """Return ``text`` with every registered credential fragment replaced.

        Safe to call on any text bound for any sink — a log record, an exception
        rendering, or an MQTT payload (Requirements 8.2, 8.3, 8.7). Idempotent.
        """
        return _redact(text, self._shields)


#: The process-wide registry. Credentials are registered once during startup and every
#: sink reads the same set, so there is exactly one place a credential could be missed
#: and exactly one place to fix it.
CREDENTIAL_REGISTRY: Final = CredentialRegistry()


# --------------------------------------------------------------------------------------------------
# the logging filter
# --------------------------------------------------------------------------------------------------


class CredentialRedactionFilter(logging.Filter):
    """Rewrites every log record so no credential fragment survives to a handler.

    Three channels are covered, and each is closed rather than merely filtered:

    ``msg`` and ``args``
        The record's message is **rendered first**, then redacted, then stored back into
        ``msg`` with ``args`` cleared. Redacting ``msg`` and each argument separately
        would not be enough: ``log("user=%sxyz", "abc")`` renders ``user=abcxyz``, whose
        window ``bcxy`` exists in neither the format string nor the argument. Rendering
        first is the only way to see what a handler would actually emit. Clearing
        ``args`` also means a second render cannot reintroduce anything.

    ``exc_info`` and ``stack_info``
        The traceback is formatted here, redacted, and written to ``exc_text``, which
        ``logging.Formatter.format`` emits verbatim. ``exc_info`` is then dropped, so no
        formatter can re-render the live exception objects and reach around the
        redaction (Requirement 8.7).

    everything else on the record
        Structured fields passed through ``extra=`` are scrubbed as a backstop, walking
        strings inside lists, tuples, sets, and dicts up to a bounded depth. Containers
        are rebuilt rather than mutated, so the caller's own objects are left alone.
    """

    def __init__(self, registry: CredentialRegistry | None = None) -> None:
        super().__init__()
        self._registry = registry if registry is not None else CREDENTIAL_REGISTRY

    @property
    def registry(self) -> CredentialRegistry:
        """The registry this filter consults."""
        return self._registry

    def filter(self, record: logging.LogRecord) -> bool:
        """Rewrite ``record`` in place. Always keeps the record; this filter never drops one."""
        registry = self._registry

        # Exception metadata is structural, not credential-dependent. Normalize it before the
        # empty-registry fast path so malformed third-party values can never reach a formatter that
        # expects the stdlib 3-tuple. A valid preformatted string remains authoritative.
        exc_text: object = record.__dict__.get("exc_text")
        if exc_text is not None and not isinstance(exc_text, str):
            record.exc_text = None
        exc_info: object = record.__dict__.get("exc_info")
        if exc_info is not None and not is_valid_exception_info(exc_info):
            record.exc_info = None

        if registry.is_empty:
            return True

        record.msg = registry.redact(self._render(record))
        record.args = None

        if record.exc_text is not None or is_valid_exception_info(record.exc_info):
            rendered = record.exc_text if record.exc_text is not None else self._render_exception(record)
            record.exc_text = registry.redact(rendered)
            record.exc_info = None

        if record.stack_info is not None:
            record.stack_info = registry.redact(record.stack_info)

        for name, value in list(record.__dict__.items()):
            if name in _RESERVED_RECORD_ATTRS:
                continue
            scrubbed = self._scrub(value, depth=0)
            if scrubbed is not value:
                record.__dict__[name] = scrubbed

        return True

    @staticmethod
    def _render(record: logging.LogRecord) -> str:
        """Render the message the way a handler would, tolerating a bad format string.

        A ``%``-formatting mismatch must not be able to suppress redaction, so a failed
        render falls back to a rendering that still contains both the format string and
        the arguments — everything that could leak — for the redactor to work on.
        """
        try:
            return record.getMessage()
        except (TypeError, ValueError, KeyError, IndexError):
            return f"{record.msg!s} {record.args!r}"

    @staticmethod
    def _render_exception(record: logging.LogRecord) -> str:
        """Render a validated exception tuple without allowing logging metadata to raise."""
        exc_info: object = record.exc_info
        if not is_valid_exception_info(exc_info):
            return ""
        exc_type, exc_value, exc_traceback = exc_info
        try:
            return "".join(traceback.format_exception(exc_type, exc_value, exc_traceback)).rstrip("\n")
        except Exception:
            # A hostile exception can itself have a broken ``__str__``. Its class still supplies a
            # useful, credential-redacted diagnostic without risking another logging failure.
            return exc_type.__name__

    def _scrub(self, value: object, *, depth: int) -> object:
        """Redact strings reachable from ``value``, rebuilding containers as needed."""
        if isinstance(value, str):
            return self._registry.redact(value)
        if isinstance(value, SecretStr):
            return value  # renders as a mask already; unwrapping it here would be the leak
        if depth >= _MAX_SCRUB_DEPTH:
            return value
        if isinstance(value, dict):
            return {key: self._scrub(item, depth=depth + 1) for key, item in value.items()}
        if isinstance(value, tuple):
            return tuple(self._scrub(item, depth=depth + 1) for item in value)
        if isinstance(value, list):
            return [self._scrub(item, depth=depth + 1) for item in value]
        if isinstance(value, set | frozenset):
            return {self._scrub(item, depth=depth + 1) for item in value}
        return value


def install_redaction_filter(
    registry: CredentialRegistry | None = None,
    logger: logging.Logger | None = None,
) -> CredentialRedactionFilter:
    """Attach a redaction filter to ``logger`` and to each handler already on it.

    Both are required for full coverage: the logger's own filters see only records
    logged directly through it, while a handler's filters see every record that reaches
    that handler, including records propagated from child loggers.

    Returns the filter so a caller that adds handlers afterwards can attach the same
    instance to them. Calling this twice with the same registry does not double-redact —
    redaction is idempotent — but the filter is not added twice to the same target.
    """
    target = logger if logger is not None else logging.getLogger()
    redaction_filter = CredentialRedactionFilter(registry)

    if not any(isinstance(existing, CredentialRedactionFilter) for existing in target.filters):
        target.addFilter(redaction_filter)
    for handler in target.handlers:
        if not any(isinstance(existing, CredentialRedactionFilter) for existing in handler.filters):
            handler.addFilter(redaction_filter)

    return redaction_filter
