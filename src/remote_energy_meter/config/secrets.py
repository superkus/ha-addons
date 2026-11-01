"""Home Assistant secret reference resolution.

A credential field of the Addon_Configuration may hold a literal value or a Home Assistant
secret reference of the form ``!secret <name>``. Requirement 8.4 says such a reference is
resolved to its value *before* the ``Device_Entry`` is validated, and that each resolution
completes within 5 seconds. Requirement 8.6 says a reference that does not exist, or does not
resolve within that bound, rejects only that ``Device_Entry``, and that the report names the
**reference** while never carrying the **referenced value**.

## Where the secrets live

The addon manifest declares ``map: [homeassistant_config:ro]``, so Supervisor mounts the Home
Assistant configuration directory read only and ``secrets.yaml`` is found inside it. Current
Supervisor releases mount it at ``/homeassistant``; older ones used ``/config``. Both are
probed in that order (:data:`DEFAULT_SECRETS_PATHS`) and the path is injectable, which is how
the tests read a temporary file instead.

## Why the 5 second bound needs a thread

The bound is on wall-clock time, and the work being bounded is a blocking read of a file on a
mount the addon does not control. A synchronous ``read_text`` cannot be interrupted, so the
read runs on a daemon thread and each resolution waits on an :class:`threading.Event` with the
budget as its timeout. A stalled mount therefore costs one bounded wait rather than hanging
startup, and because the thread is a daemon it cannot hold up interpreter exit either
(Requirement 1.5 gives shutdown 10 seconds in total).

The store is loaded **once** per resolver and the outcome is cached, so a configuration with 50
devices does not re-read the file 50 times. The give-up is cached too: once one resolution has
waited out the budget, later resolutions fail immediately with
:attr:`SecretFailure.TIMEOUT` instead of each spending another 5 seconds, which keeps startup
inside the 30 second budget of Requirement 1.3. If the read completes late, the next resolution
sees the loaded store and succeeds — the give-up suppresses further *waiting*, not the result.

Resolution is synchronous because the Configuration_Parser is (see ``config/parser.py``). It
runs during startup, before the Poll_Scheduler exists, so the bounded wait has no concurrent
work to block.

## Keeping the value out of the failure report

Every failure carries a :class:`SecretFailure` classification and a ``detail`` string built
from the reference name, the store path, and a type or error class name — never from the
store's contents. The YAML path deserves specific mention: a ``yaml.MarkedYAMLError`` renders
the offending source line as part of ``str(error)``, and for a malformed ``secrets.yaml`` that
line is very likely a secret. This module therefore reports the error class and its line and
column and never the error's own text (:func:`_describe_yaml_error`).

Resolved values are registered with the credential registry the moment they are read, so a
value that reaches a log record through any later path is redacted (Requirements 8.2, 8.7).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

import yaml
from pydantic import SecretStr

from remote_energy_meter.diagnostics.redaction import CREDENTIAL_REGISTRY, CredentialRegistry

__all__ = [
    "DEFAULT_SECRETS_PATHS",
    "MAX_REFERENCE_LENGTH",
    "MAX_STORE_BYTES",
    "SECRET_RESOLUTION_TIMEOUT_S",
    "SECRET_TAG",
    "ResolvedSecret",
    "SecretFailure",
    "SecretResolution",
    "SecretResolver",
    "SecretStoreError",
    "SecretStoreLoader",
    "UnresolvedSecret",
    "is_reference",
    "load_secret_store",
    "parse_reference",
]

#: The tag that introduces a Home Assistant secret reference: ``!secret <name>``.
SECRET_TAG: Final = "!secret"

#: Requirements 8.4, 8.6: the per-resolution wall-clock bound, in seconds.
SECRET_RESOLUTION_TIMEOUT_S: Final = 5.0

#: Where ``secrets.yaml`` is looked for when no explicit path is given, in probe order.
#: ``homeassistant_config:ro`` mounts the Home Assistant configuration directory at
#: ``/homeassistant`` on current Supervisor releases and at ``/config`` on older ones.
DEFAULT_SECRETS_PATHS: Final[tuple[Path, ...]] = (
    Path("/homeassistant/secrets.yaml"),
    Path("/config/secrets.yaml"),
)

#: Largest ``secrets.yaml`` this module will parse. A secrets file is a short flat mapping; a
#: file orders of magnitude larger is a mistake or a hostile mount, and parsing it would cost
#: memory and time inside the startup budget.
MAX_STORE_BYTES: Final = 1 << 20

#: Longest reference name carried into a failure report. The name is not secret material, but
#: Requirement 9.1 caps a log message at 512 characters, so an absurd name is truncated rather
#: than allowed to crowd out the rest of the record.
MAX_REFERENCE_LENGTH: Final = 255


class SecretFailure(StrEnum):
    """Why a secret reference did not resolve.

    Every member is reported by the Configuration_Parser as
    :attr:`~remote_energy_meter.config.runtime.RejectionReason.UNRESOLVED_SECRET`; the
    distinction exists so the log record can say *why* without the parser re-inspecting the
    cause. Each value describes the reference or the store, never the referenced value.
    """

    NOT_FOUND = "not_found"
    """The store loaded, and holds no entry with that name (Requirement 8.6)."""

    TIMEOUT = "timeout"
    """The store did not load within the 5 second bound (Requirements 8.4, 8.6)."""

    MALFORMED_REFERENCE = "malformed_reference"
    """The value was written as a reference but names no secret, as in a bare ``!secret``."""

    STORE_MISSING = "store_missing"
    """No ``secrets.yaml`` exists at the configured or probed location."""

    STORE_UNREADABLE = "store_unreadable"
    """``secrets.yaml`` exists but could not be read: permissions, I/O error, or oversize."""

    STORE_MALFORMED = "store_malformed"
    """``secrets.yaml`` is not UTF-8, is not valid YAML, or is not a mapping of names to values."""

    UNSUPPORTED_VALUE = "unsupported_value"
    """The entry exists but is not usable as a credential, being a list, a mapping, a boolean,
    or null rather than text. A boolean is rejected rather than coerced because YAML reads an
    unquoted ``yes`` as ``True``, and silently authenticating with ``"True"`` would be worse
    than reporting the entry as unusable."""


@dataclass(frozen=True, slots=True)
class ResolvedSecret:
    """A reference that resolved.

    Attributes:
        name: The reference name, as written after the ``!secret`` tag.
        value: The referenced value, wrapped so that ``str`` and ``repr`` render a mask
            (Requirement 8.2). Already registered with the credential registry by the resolver
            that produced it, unless that resolver was constructed with ``registry=None``.
    """

    name: str
    value: SecretStr

    @property
    def plaintext(self) -> str:
        """The referenced value as text, for the one caller that needs it.

        The Configuration_Parser substitutes plaintext into the options mapping before handing
        it to the plugin-published schema, because a plugin is free to declare a credential
        field as ``str`` rather than ``SecretStr`` and validation of a wrapped value would then
        fail. Every other caller should prefer :attr:`value`.
        """
        return self.value.get_secret_value()


@dataclass(frozen=True, slots=True)
class UnresolvedSecret:
    """A reference that did not resolve, described without its value (Requirement 8.6).

    Attributes:
        name: The reference name, truncated to :data:`MAX_REFERENCE_LENGTH` characters.
        failure: Which resolution path failed.
        detail: A sentence about the reference or the store. Built only from the reference
            name, the store path, and type or error class names, so it cannot carry the
            referenced value or any other store content.
    """

    name: str
    failure: SecretFailure
    detail: str

    def describe(self) -> str:
        """One line naming the reference and the failure, safe for any sink."""
        return f"secret reference {self.name!r} unresolved ({self.failure.value}): {self.detail}"


#: What :meth:`SecretResolver.resolve` returns: the value, or a described failure.
SecretResolution = ResolvedSecret | UnresolvedSecret

#: A source of secrets. Returns the whole store as a mapping of name to raw value, or raises
#: :class:`SecretStoreError` with a classification. Called at most once per resolver, on a
#: worker thread, so it may block.
SecretStoreLoader = Callable[[], Mapping[str, object]]


class SecretStoreError(Exception):
    """The secret store could not be produced. Carries the classification for the report."""

    def __init__(self, failure: SecretFailure, detail: str) -> None:
        super().__init__(f"{failure.value}: {detail}")
        self.failure = failure
        self.detail = detail


@dataclass(frozen=True, slots=True)
class _StoreFailure:
    """A load outcome that failed, cached so every reference reports the same classification."""

    failure: SecretFailure
    detail: str


_LoadOutcome = Mapping[str, object] | _StoreFailure


# --------------------------------------------------------------------------------------------------
# reference syntax
# --------------------------------------------------------------------------------------------------


def parse_reference(value: object) -> str | None:
    """Return the secret name when ``value`` is a ``!secret`` reference.

    Returns ``None`` when ``value`` is not a reference at all and should be treated as a
    literal — anything that is not a string, and any string that does not begin with the tag
    followed by whitespace. ``"!secretpassword"`` is therefore a literal, not a reference to
    ``password``.

    Returns the empty string when the value *is* written as a reference but names nothing, as
    in a bare ``"!secret"``. That case is deliberately not folded into ``None``: reading
    ``"!secret"`` as a literal password would silently authenticate with the tag itself,
    whereas reporting it lets Requirement 8.6 reject the entry with a stated reason.
    """
    if not isinstance(value, str):
        return None

    stripped = value.strip()
    if not stripped.startswith(SECRET_TAG):
        return None

    remainder = stripped[len(SECRET_TAG) :]
    if not remainder:
        return ""
    if not remainder[0].isspace():
        return None
    return remainder.strip()


def is_reference(value: object) -> bool:
    """Whether ``value`` is written as a ``!secret`` reference, well formed or not."""
    return parse_reference(value) is not None


# --------------------------------------------------------------------------------------------------
# the default, file-backed loader
# --------------------------------------------------------------------------------------------------


def _first_existing(candidates: Sequence[Path]) -> Path | None:
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            # An unreadable parent directory is indistinguishable from an absent file here, and
            # either way this candidate cannot be used. Probing continues so a working
            # fallback still wins.
            continue
    return None


def _describe_yaml_error(error: yaml.YAMLError) -> str:
    """Describe a YAML failure by class and position, never by its own text.

    ``str()`` of a :class:`yaml.MarkedYAMLError` embeds a snippet of the offending source line.
    In ``secrets.yaml`` that line is very likely a secret, so the error's text is never used
    (Requirements 8.2, 8.6).
    """
    if isinstance(error, yaml.MarkedYAMLError):
        mark = error.problem_mark
        if mark is not None:
            return f"{type(error).__name__} at line {mark.line + 1}, column {mark.column + 1}"
    return type(error).__name__


def load_secret_store(
    path: Path | None = None,
    *,
    candidates: Sequence[Path] = DEFAULT_SECRETS_PATHS,
) -> dict[str, object]:
    """Read and parse ``secrets.yaml`` into a mapping of name to raw value.

    Args:
        path: The file to read. When ``None``, the first existing entry of ``candidates`` is
            used.
        candidates: Probed in order when ``path`` is ``None``.

    Returns:
        The parsed mapping. An empty file yields an empty mapping, so every reference against
        it reports :attr:`SecretFailure.NOT_FOUND` rather than a malformed store.

    Raises:
        SecretStoreError: With the classification the report should carry. The message names
            the path and the failure kind only.
    """
    chosen = path if path is not None else _first_existing(candidates)
    if chosen is None:
        probed = ", ".join(str(candidate) for candidate in candidates)
        raise SecretStoreError(SecretFailure.STORE_MISSING, f"no secrets file exists at any of {probed}")

    try:
        size = chosen.stat().st_size
    except FileNotFoundError as error:
        raise SecretStoreError(SecretFailure.STORE_MISSING, f"no secrets file exists at {chosen}") from error
    except OSError as error:
        raise SecretStoreError(
            SecretFailure.STORE_UNREADABLE,
            f"the secrets file at {chosen} could not be inspected: {type(error).__name__}",
        ) from error

    if size > MAX_STORE_BYTES:
        raise SecretStoreError(
            SecretFailure.STORE_UNREADABLE,
            f"the secrets file at {chosen} is larger than {MAX_STORE_BYTES} bytes",
        )

    try:
        text = chosen.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise SecretStoreError(SecretFailure.STORE_MISSING, f"no secrets file exists at {chosen}") from error
    except UnicodeDecodeError as error:
        raise SecretStoreError(
            SecretFailure.STORE_MALFORMED,
            f"the secrets file at {chosen} is not valid UTF-8",
        ) from error
    except OSError as error:
        raise SecretStoreError(
            SecretFailure.STORE_UNREADABLE,
            f"the secrets file at {chosen} could not be read: {type(error).__name__}",
        ) from error

    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise SecretStoreError(
            SecretFailure.STORE_MALFORMED,
            f"the secrets file at {chosen} is not valid YAML ({_describe_yaml_error(error)})",
        ) from error

    if document is None:
        return {}
    if not isinstance(document, Mapping):
        raise SecretStoreError(
            SecretFailure.STORE_MALFORMED,
            f"the secrets file at {chosen} holds a {type(document).__name__}, not a mapping of names to values",
        )

    # A YAML key may parse as a non-string scalar (``1234: value``). Such keys are kept under
    # their text form so a reference can name them; structured keys cannot be referenced by
    # name at all and are dropped.
    return {str(key): value for key, value in document.items() if isinstance(key, str | int)}


def _coerce_secret_value(raw: object) -> str | None:
    """Return ``raw`` as credential text, or ``None`` when it cannot be one.

    ``str`` passes through. An integer is accepted and rendered, because a numeric password
    written unquoted is a plausible mistake with an unambiguous text form. A boolean is
    rejected even though it is an ``int`` subclass: YAML reads unquoted ``yes``, ``no``,
    ``on``, and ``off`` as booleans, and authenticating with ``"True"`` would be a silent
    wrong answer. Floats are rejected for the same reason — ``1.10`` would render as ``1.1``.
    """
    if isinstance(raw, str):
        return raw
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return str(raw)
    return None


# --------------------------------------------------------------------------------------------------
# the resolver
# --------------------------------------------------------------------------------------------------


class SecretResolver:
    """Resolves ``!secret <name>`` references within a bounded time (Requirements 8.4, 8.6).

    The store is loaded lazily, once, on a daemon thread, and the outcome — success or a
    classified failure — is cached for every later reference. Nothing is read until the first
    reference is resolved, so a configuration with no references never touches the filesystem.

    Construct it against the mounted Home Assistant configuration directory::

        resolver = SecretResolver()

    against an explicit file::

        resolver = SecretResolver(tmp_path / "secrets.yaml")

    or against an in-memory mapping, which is what the parser's own tests use::

        resolver = SecretResolver.from_mapping({"idrac_a_password": "hunter2"})
    """

    __slots__ = ("_loaded", "_loader", "_lock", "_outcome", "_registry", "_thread", "_timed_out", "_timeout_s")

    def __init__(
        self,
        path: Path | None = None,
        *,
        loader: SecretStoreLoader | None = None,
        timeout_s: float = SECRET_RESOLUTION_TIMEOUT_S,
        candidates: Sequence[Path] = DEFAULT_SECRETS_PATHS,
        registry: CredentialRegistry | None = CREDENTIAL_REGISTRY,
    ) -> None:
        """
        Args:
            path: The ``secrets.yaml`` to read. Ignored when ``loader`` is given.
            loader: An alternative source of secrets, called at most once on a worker thread.
            timeout_s: The per-resolution bound. Defaults to
                :data:`SECRET_RESOLUTION_TIMEOUT_S`, which is the 5 seconds Requirement 8.4
                fixes.
            candidates: Probed in order when neither ``path`` nor ``loader`` is given.
            registry: Every resolved value is registered here so that it is redacted from any
                later log record (Requirement 8.2). Defaults to the process-wide registry;
                pass ``None`` to opt out, which only tests should need.
        """
        self._loader: SecretStoreLoader = (
            loader if loader is not None else lambda: load_secret_store(path, candidates=candidates)
        )
        self._timeout_s = timeout_s
        self._registry = registry
        self._lock = threading.Lock()
        self._loaded = threading.Event()
        self._thread: threading.Thread | None = None
        self._outcome: _LoadOutcome | None = None
        self._timed_out = False

    @classmethod
    def from_mapping(
        cls,
        secrets: Mapping[str, object],
        *,
        timeout_s: float = SECRET_RESOLUTION_TIMEOUT_S,
        registry: CredentialRegistry | None = CREDENTIAL_REGISTRY,
    ) -> SecretResolver:
        """A resolver over an in-memory store, for tests and for a caller holding secrets already."""
        snapshot: dict[str, object] = dict(secrets)
        return cls(loader=lambda: snapshot, timeout_s=timeout_s, registry=registry)

    # -- resolution --------------------------------------------------------------------

    def resolve(self, name: str) -> SecretResolution:
        """Resolve one reference name, returning the value or a described failure.

        Never raises: a failure is a value the Configuration_Parser turns into one
        ``EntryRejection`` while the remaining entries carry on (Requirement 8.6).
        """
        reference = name.strip()
        if not reference:
            return UnresolvedSecret(
                name="",
                failure=SecretFailure.MALFORMED_REFERENCE,
                detail=f"the value is written as a {SECRET_TAG} reference but names no secret",
            )
        if len(reference) > MAX_REFERENCE_LENGTH:
            return UnresolvedSecret(
                name=reference[:MAX_REFERENCE_LENGTH],
                failure=SecretFailure.MALFORMED_REFERENCE,
                detail=f"the reference name is longer than {MAX_REFERENCE_LENGTH} characters",
            )

        store = self._store()
        if isinstance(store, _StoreFailure):
            return UnresolvedSecret(name=reference, failure=store.failure, detail=store.detail)

        if reference not in store:
            return UnresolvedSecret(
                name=reference,
                failure=SecretFailure.NOT_FOUND,
                detail="the secret store holds no entry with this name",
            )

        raw = store[reference]
        plaintext = _coerce_secret_value(raw)
        if plaintext is None:
            return UnresolvedSecret(
                name=reference,
                failure=SecretFailure.UNSUPPORTED_VALUE,
                detail=f"the entry is a {type(raw).__name__}, which cannot be used as a credential",
            )

        if self._registry is not None:
            # Registered before the value leaves this method, so there is no window in which a
            # log record could carry it unredacted (Requirements 8.2, 8.7).
            self._registry.register(plaintext)
        return ResolvedSecret(name=reference, value=SecretStr(plaintext))

    def substitute(self, value: object) -> tuple[object, UnresolvedSecret | None]:
        """Replace a reference with its value, leaving a literal untouched.

        This is the operation the Configuration_Parser applies to each credential-bearing
        option before validation (Requirement 8.4).

        Returns:
            A pair of the value to validate and the failure, if any. On success the failure is
            ``None`` and the value is the resolved plaintext. On failure the returned value is
            the original, so a caller that reports the rejection and stops never sees a
            half-substituted mapping.
        """
        name = parse_reference(value)
        if name is None:
            return value, None

        outcome = self.resolve(name)
        if isinstance(outcome, UnresolvedSecret):
            return value, outcome
        return outcome.plaintext, None

    # -- the bounded, cached load ------------------------------------------------------

    def _store(self) -> _LoadOutcome:
        """Return the loaded store or a cached failure, waiting at most the budget."""
        self._start_load()

        if not self._loaded.is_set():
            with self._lock:
                gave_up = self._timed_out
            if gave_up:
                # The budget was already spent by an earlier reference. Waiting again would
                # multiply the bound by the number of references in the configuration.
                return _StoreFailure(SecretFailure.TIMEOUT, self._timeout_detail())
            if not self._loaded.wait(self._timeout_s):
                with self._lock:
                    self._timed_out = True
                return _StoreFailure(SecretFailure.TIMEOUT, self._timeout_detail())

        outcome = self._outcome
        if outcome is None:
            # The event is set but no outcome was published, which the loader thread cannot
            # produce. Reported as a timeout rather than trusted, because there is no store.
            return _StoreFailure(SecretFailure.TIMEOUT, self._timeout_detail())
        return outcome

    def _start_load(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            thread = threading.Thread(target=self._load, name="secret-store-load", daemon=True)
            self._thread = thread
        thread.start()

    def _load(self) -> None:
        """Run the loader and publish its outcome. Runs on the worker thread; never raises."""
        outcome: _LoadOutcome
        try:
            outcome = self._loader()
        except SecretStoreError as error:
            outcome = _StoreFailure(error.failure, error.detail)
        except Exception as error:
            # A loader is allowed to be anything, including a plugin-supplied callable, and an
            # escaping exception would leave the outcome unpublished so every reference blamed
            # a timeout it did not cause. The class name is reported and the message is not,
            # because an arbitrary exception may well have interpolated store content into it.
            outcome = _StoreFailure(
                SecretFailure.STORE_UNREADABLE,
                f"the secret store could not be loaded: {type(error).__name__}",
            )
        self._outcome = outcome
        self._loaded.set()

    def _timeout_detail(self) -> str:
        return f"the secret store did not load within {self._timeout_s:g} seconds"
