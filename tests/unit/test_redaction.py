"""Unit tests for the credential registry and redaction filter (task 3.1).

These cover the mechanics directly: 4-gram detection, maximal-span expansion, the
constant marker, the render-then-redact path through ``msg``/``args``, ``exc_info``
rewriting, and structured-attribute scrubbing. The exhaustive statement — that *no*
substring of 4 or more characters of *any* credential reaches *any* sink — is Property 32
and belongs to task 3.3.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from pydantic import BaseModel, Field, SecretStr

from remote_energy_meter.config.models import (
    CREDENTIAL_MARKER,
    ConfigurationObject,
    DeviceEntry,
    MqttSettings,
)
from remote_energy_meter.diagnostics.redaction import (
    REDACTION_MARKER,
    SHIELD_WINDOW,
    CredentialRedactionFilter,
    CredentialRegistry,
    install_redaction_filter,
)

SECRET = "sup3rs3cr3tp4ssw0rd"


def fragments(value: str, width: int = SHIELD_WINDOW) -> list[str]:
    """Every window of ``width`` characters of ``value``."""
    return [value[i : i + width] for i in range(len(value) - width + 1)]


def assert_clean(text: str, secret: str = SECRET) -> None:
    """No window of 4 or more characters of ``secret`` survives in ``text`` (Requirement 8.2)."""
    assert secret not in text
    for window in fragments(secret):
        assert window not in text, f"leaked {window!r} in {text!r}"


@pytest.fixture
def registry() -> CredentialRegistry:
    reg = CredentialRegistry()
    reg.register(SECRET)
    return reg


# --------------------------------------------------------------------------------------------------
# registration
# --------------------------------------------------------------------------------------------------


def test_empty_registry_is_a_no_op() -> None:
    """Nothing registered means nothing rewritten, so an unconfigured process logs normally."""
    reg = CredentialRegistry()

    assert reg.is_empty is True
    assert reg.redact(SECRET) == SECRET


@pytest.mark.parametrize("ignored", [None, "", "   ", "\t\n"])
def test_absent_and_whitespace_only_values_are_not_registered(ignored: str | None) -> None:
    """Registering whitespace would redact every space in every record; the parser rejects it anyway (8.5)."""
    reg = CredentialRegistry()
    reg.register(ignored)

    assert reg.is_empty is True
    assert reg.redact("a b\tc") == "a b\tc"


def test_secret_str_is_unwrapped_for_registration() -> None:
    """Credential fields are ``SecretStr``; the registry has to see through that to shield the value."""
    reg = CredentialRegistry()
    reg.register(SecretStr(SECRET))

    assert_clean(reg.redact(f"password={SECRET}"))


def test_padded_value_registers_both_forms() -> None:
    """A credential configured with stray whitespace is caught trimmed or untrimmed."""
    reg = CredentialRegistry()
    reg.register(f"  {SECRET}  ")

    assert_clean(reg.redact(SECRET))
    assert_clean(reg.redact(f"  {SECRET}  "))


def test_register_all_takes_an_iterable() -> None:
    reg = CredentialRegistry()
    reg.register_all(["alpha-one", SecretStr("beta-two"), None])

    assert reg.redact("alpha-one and beta-two") == f"{REDACTION_MARKER} and {REDACTION_MARKER}"


def test_clear_forgets_every_value(registry: CredentialRegistry) -> None:
    registry.clear()

    assert registry.is_empty is True
    assert registry.redact(SECRET) == SECRET


# --------------------------------------------------------------------------------------------------
# 4-gram shielding
# --------------------------------------------------------------------------------------------------


def test_whole_value_is_replaced_by_one_marker(registry: CredentialRegistry) -> None:
    assert registry.redact(f"connecting with {SECRET} now") == f"connecting with {REDACTION_MARKER} now"


@pytest.mark.parametrize("window", fragments(SECRET))
def test_every_four_character_window_is_redacted(registry: CredentialRegistry, window: str) -> None:
    """Requirement 8.2 forbids any substring of 4 or more characters, not just the whole value."""
    redacted = registry.redact(f"detail: {window}")

    assert window not in redacted
    assert redacted == f"detail: {REDACTION_MARKER}"


def test_three_character_fragment_is_left_alone(registry: CredentialRegistry) -> None:
    """The shield starts at 4 characters. Redacting shorter runs would erase ordinary prose."""
    fragment = SECRET[:3]

    assert registry.redact(f"detail: {fragment}") == f"detail: {fragment}"


def test_hit_expands_to_the_maximal_span(registry: CredentialRegistry) -> None:
    """A hit on a 4-window grows to the longest surrounding run that is still part of the credential."""
    run = SECRET[2:14]  # 12 characters, so eight overlapping windows all inside one span
    assert len(run) > SHIELD_WINDOW

    redacted = registry.redact(f"[{run}]")

    # One marker, not one per window, and no residual characters of the run on either side.
    assert redacted == f"[{REDACTION_MARKER}]"
    assert redacted.count(REDACTION_MARKER) == 1


def test_expansion_stops_at_the_credential_boundary(registry: CredentialRegistry) -> None:
    """Characters that are not part of the credential are preserved, so records stay readable."""
    redacted = registry.redact(f"before-{SECRET}-after")

    assert redacted == f"before-{REDACTION_MARKER}-after"


def test_separate_runs_produce_separate_markers(registry: CredentialRegistry) -> None:
    left, right = SECRET[:6], SECRET[8:16]

    redacted = registry.redact(f"{left} ... {right}")

    assert redacted == f"{REDACTION_MARKER} ... {REDACTION_MARKER}"


@pytest.mark.parametrize("length", [4, 9, 32, 128, 256])
def test_marker_length_is_independent_of_credential_length(length: int) -> None:
    """A variable-length marker would leak the credential's length (Requirement 8.2)."""
    reg = CredentialRegistry()
    reg.register("q" * length)

    assert reg.redact("x" + "q" * length + "y") == f"x{REDACTION_MARKER}y"


def test_short_credential_is_redacted_by_exact_match() -> None:
    """Below the window width there are no grams, so exact match is the whole guarantee."""
    reg = CredentialRegistry()
    reg.register("ab")

    assert reg.redact("ab cd ab") == f"{REDACTION_MARKER} cd {REDACTION_MARKER}"


def test_redaction_is_idempotent(registry: CredentialRegistry) -> None:
    """An existing marker is never rescanned, so repeated passes are stable."""
    once = registry.redact(f"a {SECRET} b")

    assert registry.redact(once) == once


def test_overlapping_credentials_are_both_shielded() -> None:
    """A credential that contains another must not be fragmented into an unshielded remainder."""
    reg = CredentialRegistry()
    reg.register("outer-inner-value")
    reg.register("inner")

    redacted = reg.redact("outer-inner-value and inner alone")

    assert "inner" not in redacted
    assert redacted == f"{REDACTION_MARKER} and {REDACTION_MARKER} alone"


def test_collision_over_redacts_rather_than_under_redacts() -> None:
    """Two credentials sharing a run bleed into each other's spans. That is the safe direction.

    ``alpha-secret`` contributes the window ``a-se``, which also occurs inside
    ``beta-secret``, so shielding the first eats part of the second. The result is less
    readable and still leak-free, which is the trade the design chooses deliberately.
    """
    reg = CredentialRegistry()
    reg.register("alpha-secret")
    reg.register("beta-secret")

    redacted = reg.redact("alpha-secret and beta-secret")

    assert_clean(redacted, "alpha-secret")
    assert_clean(redacted, "beta-secret")
    assert redacted == f"{REDACTION_MARKER} and bet{REDACTION_MARKER}"


def test_unrelated_text_is_untouched(registry: CredentialRegistry) -> None:
    message = "poll cycle complete device_id=poweredge_a watts=214.0 duration_ms=42"

    assert registry.redact(message) == message


# --------------------------------------------------------------------------------------------------
# model-driven registration (Requirement 8.1)
# --------------------------------------------------------------------------------------------------


class _Params(BaseModel):
    host: str
    username: SecretStr = Field(json_schema_extra={CREDENTIAL_MARKER: True})
    password: SecretStr = Field(json_schema_extra={CREDENTIAL_MARKER: True})
    verify_tls: bool = True


def test_register_model_reads_the_credential_markers() -> None:
    """The core finds credential fields through the schema marker, never by parameter name."""
    reg = CredentialRegistry()
    reg.register_model(_Params(host="192.0.2.9", username=SecretStr("root"), password=SecretStr(SECRET)))

    assert_clean(reg.redact(f"basic auth {SECRET}"))
    assert reg.redact("host 192.0.2.9") == "host 192.0.2.9"


def test_register_model_walks_nested_models() -> None:
    """A ``ConfigurationObject`` carries its broker credentials one level down, in ``mqtt``."""
    config = ConfigurationObject(
        mqtt=MqttSettings(host="core-mosquitto", port=1883, username=SecretStr("addons"), password=SecretStr(SECRET)),
        devices=[DeviceEntry(device_id="poweredge_a", plugin="idrac", params={})],
    )
    reg = CredentialRegistry()
    reg.register_model(config)

    assert_clean(reg.redact(f"broker password {SECRET}"))


def test_register_model_ignores_unmarked_fields() -> None:
    """Only marked fields are credentials; redacting a hostname would make the log useless."""

    class _Unmarked(BaseModel):
        host: str

    reg = CredentialRegistry()
    reg.register_model(_Unmarked(host="core-mosquitto"))

    assert reg.is_empty is True


# --------------------------------------------------------------------------------------------------
# the logging filter
# --------------------------------------------------------------------------------------------------


class _Capture(logging.Handler):
    """Collects fully formatted records, which is what a real handler would write out."""

    def __init__(self) -> None:
        super().__init__()
        self.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        self.lines: list[str] = []
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        self.lines.append(self.format(record))


@pytest.fixture
def wired(registry: CredentialRegistry) -> Any:
    """A child logger whose handler carries the redaction filter, as the app wires it."""
    logger = logging.getLogger("test_redaction.wired")
    logger.handlers.clear()
    logger.filters.clear()
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    handler = _Capture()
    logger.addHandler(handler)
    install_redaction_filter(registry, logger)
    yield logger, handler
    logger.handlers.clear()
    logger.filters.clear()


def test_message_is_redacted(wired: Any) -> None:
    logger, handler = wired

    logger.error("connect failed for %s", SECRET)

    assert_clean(handler.lines[0])
    assert handler.lines[0] == f"ERROR connect failed for {REDACTION_MARKER}"


def test_args_are_cleared_after_folding_into_the_message(wired: Any) -> None:
    """Args are folded in and dropped, so no later render can reintroduce the plaintext."""
    logger, handler = wired

    logger.warning("password=%s", SECRET)
    record = handler.records[0]

    assert record.args is None
    assert_clean(record.getMessage())


def test_fragment_formed_only_at_a_format_boundary_is_redacted(wired: Any) -> None:
    """Rendering before redacting is the point: neither piece alone contains the window."""
    logger, handler = wired
    prefix, suffix = SECRET[:5], SECRET[5:]
    template = f"token=%s{suffix}"

    logger.error(template, prefix)

    # The pieces are individually harmless; the rendered message is not.
    assert SECRET not in template
    assert_clean(handler.lines[0])


def test_a_broken_format_string_still_redacts(wired: Any) -> None:
    """A ``%``-formatting mismatch must not be a way to slip a credential past the filter."""
    logger, handler = wired

    logger.error("no placeholders here", SECRET)

    assert_clean(handler.lines[0])


def test_non_string_message_is_redacted(wired: Any) -> None:
    logger, handler = wired

    logger.info({"password": SECRET})

    assert_clean(handler.lines[0])


def _raise_with_credential() -> None:
    """Stand-in for a plugin that embeds a credential in its exception message."""
    raise RuntimeError(f"401 for user root with password {SECRET}")


def test_exception_text_is_redacted(wired: Any) -> None:
    """Requirement 8.7: a plugin exception whose text embeds a password is scrubbed first."""
    logger, handler = wired

    try:
        _raise_with_credential()
    except RuntimeError:
        logger.exception("read failed")

    record = handler.records[0]
    assert record.exc_info is None  # the live exception cannot be re-rendered around us
    assert record.exc_text is not None
    assert_clean(record.exc_text)
    assert_clean(handler.lines[0])
    assert "RuntimeError" in handler.lines[0]  # the diagnostic value survives


def test_preformatted_exception_text_is_redacted(wired: Any) -> None:
    """A record that already carries ``exc_text`` is rewritten rather than trusted."""
    logger, _ = wired
    record = logger.makeRecord(
        logger.name, logging.ERROR, __file__, 1, "read failed", None, None, func="test", extra=None
    )
    record.exc_text = f"Traceback: password {SECRET}"

    logger.handle(record)

    assert record.exc_text is not None
    assert_clean(record.exc_text)


def test_stack_info_is_redacted(wired: Any) -> None:
    logger, _ = wired
    record = logger.makeRecord(
        logger.name, logging.ERROR, __file__, 1, "read failed", None, None, func="test", extra=None
    )
    record.stack_info = f"stack with {SECRET}"

    logger.handle(record)

    assert record.stack_info is not None
    assert_clean(record.stack_info)


def test_structured_extras_are_scrubbed(wired: Any) -> None:
    """Backstop for structured fields: a credential passed through ``extra=`` is still a leak."""
    logger, handler = wired

    logger.info("read failure", extra={"detail": f"auth as root/{SECRET}", "device_id": "poweredge_a"})
    record = handler.records[0]

    assert_clean(str(record.detail))
    assert record.device_id == "poweredge_a"


def test_extras_are_scrubbed_inside_containers(wired: Any) -> None:
    logger, handler = wired

    logger.info("read failure", extra={"payload": {"items": [SECRET, "safe"], "pair": (SECRET,)}})
    record = handler.records[0]

    assert_clean(repr(record.payload))
    assert record.payload["items"][1] == "safe"


def test_caller_containers_are_not_mutated(wired: Any) -> None:
    """Containers are rebuilt, so a caller reusing the dict it logged still sees its own values."""
    logger, _ = wired
    payload = {"detail": SECRET}

    logger.info("read failure", extra={"payload": payload})

    assert payload == {"detail": SECRET}


def test_reserved_attributes_are_left_alone(wired: Any) -> None:
    """Rewriting logger name, level, or source location would corrupt formatting for no gain."""
    logger, handler = wired

    logger.warning("poll skipped")
    record = handler.records[0]

    assert record.name == "test_redaction.wired"
    assert record.levelname == "WARNING"


def test_secret_str_attributes_are_not_unwrapped(wired: Any) -> None:
    """``SecretStr`` already renders as a mask; unwrapping it to redact it would be the leak."""
    logger, handler = wired

    logger.info("config", extra={"password": SecretStr(SECRET)})

    assert_clean(handler.lines[0])
    assert_clean(repr(handler.records[0].password))


def test_filter_never_drops_a_record(registry: CredentialRegistry) -> None:
    redaction_filter = CredentialRedactionFilter(registry)
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "msg %s", (SECRET,), None)

    assert redaction_filter.filter(record) is True


def test_install_attaches_to_the_logger_and_its_handlers(registry: CredentialRegistry) -> None:
    """A logger-only filter would miss records propagated from child loggers."""
    logger = logging.getLogger("test_redaction.install")
    logger.handlers.clear()
    logger.filters.clear()
    handler = _Capture()
    logger.addHandler(handler)

    try:
        install_redaction_filter(registry, logger)

        assert any(isinstance(f, CredentialRedactionFilter) for f in logger.filters)
        assert any(isinstance(f, CredentialRedactionFilter) for f in handler.filters)
    finally:
        logger.handlers.clear()
        logger.filters.clear()


def test_install_is_not_repeated_on_the_same_target(registry: CredentialRegistry) -> None:
    logger = logging.getLogger("test_redaction.install_twice")
    logger.handlers.clear()
    logger.filters.clear()
    handler = _Capture()
    logger.addHandler(handler)

    try:
        install_redaction_filter(registry, logger)
        install_redaction_filter(registry, logger)

        assert len(logger.filters) == 1
        assert len(handler.filters) == 1
    finally:
        logger.handlers.clear()
        logger.filters.clear()


def test_propagated_child_records_are_redacted(registry: CredentialRegistry) -> None:
    """The handler-side attachment is what covers ``logging.getLogger("remote_energy_meter.x")``."""
    parent = logging.getLogger("test_redaction.parent")
    parent.handlers.clear()
    parent.filters.clear()
    parent.setLevel(logging.DEBUG)
    parent.propagate = False
    handler = _Capture()
    parent.addHandler(handler)

    try:
        install_redaction_filter(registry, parent)
        logging.getLogger("test_redaction.parent.child").error("failed for %s", SECRET)

        assert_clean(handler.lines[0])
    finally:
        parent.handlers.clear()
        parent.filters.clear()


def test_a_bool_exc_info_does_not_break_the_filter() -> None:
    """logging allows a bool for exc_info, and a falsy one reaches the record unnormalised.

    asyncio's default exception handler passes ``exc_info=False`` when reporting a context that carries
    no exception. ``Logger._log`` only converts truthy values, so ``record.exc_info`` really is
    ``False`` here. A filter that raises on it is a filter that does not redact, which is why this is
    checked by shape rather than by emptiness.
    """
    registry = CredentialRegistry()
    registry.register("hunter2hunter2")
    filter_ = CredentialRedactionFilter(registry=registry)
    record = logging.LogRecord(
        name="asyncio",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="context mentions hunter2hunter2",
        args=None,
        exc_info=False,  # type: ignore[arg-type]
    )

    assert filter_.filter(record) is True
    assert "hunter2hunter2" not in str(record.msg)
    assert record.exc_info is None


@pytest.mark.parametrize(
    "malformed",
    [
        pytest.param(True, id="true"),
        pytest.param(False, id="false"),
        pytest.param("bad", id="string"),
        pytest.param([], id="list"),
        pytest.param((RuntimeError,), id="short-tuple"),
        pytest.param((RuntimeError, "bad", None), id="invalid-value"),
    ],
)
def test_filter_clears_malformed_exc_info_even_when_the_registry_is_empty(malformed: object) -> None:
    """The empty-registry fast path must still leave metadata safe for downstream formatters."""
    filter_ = CredentialRedactionFilter(registry=CredentialRegistry())
    record = logging.LogRecord(
        name="third_party",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="failure",
        args=None,
        exc_info=None,
    )
    record.__dict__["exc_info"] = malformed

    assert filter_.filter(record) is True
    assert record.exc_info is None


def test_filter_preserves_a_valid_exception_tuple_when_no_redaction_is_needed() -> None:
    """Normal traceback rendering remains the formatter's job when the registry is empty."""
    exc_info = (RuntimeError, RuntimeError("ordinary traceback"), None)

    filter_ = CredentialRedactionFilter(registry=CredentialRegistry())
    record = logging.LogRecord(
        name="third_party",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="failure",
        args=None,
        exc_info=exc_info,
    )

    assert filter_.filter(record) is True
    assert record.exc_info is exc_info
