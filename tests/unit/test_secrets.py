"""Unit tests for Home Assistant secret reference resolution (task 4.1).

Requirement 8.4 fixes the resolution path and the 5 second per-resolution bound; Requirement 8.6
fixes what happens when a reference does not resolve, including the part that matters most: the
report names the reference and never the referenced value.

The bound is tested by injecting a loader that blocks and shrinking ``timeout_s``, so the suite
never spends a real 5 seconds. That the *default* bound is 5 seconds is asserted against the
constant instead.

Every failure path is also swept by one table-driven test that asserts the secret value appears
nowhere in the report, which is the check that would catch a future failure path added without
thinking about Requirement 8.2.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from remote_energy_meter.config.secrets import (
    DEFAULT_SECRETS_PATHS,
    MAX_REFERENCE_LENGTH,
    MAX_STORE_BYTES,
    SECRET_RESOLUTION_TIMEOUT_S,
    ResolvedSecret,
    SecretFailure,
    SecretResolver,
    SecretStoreError,
    UnresolvedSecret,
    is_reference,
    load_secret_store,
    parse_reference,
)
from remote_energy_meter.diagnostics.redaction import REDACTION_MARKER, CredentialRegistry

#: Distinctive enough that any appearance in a report or a log record is unambiguous, and long
#: enough to exercise the 4-gram shielding of the redaction registry.
SECRET_VALUE = "hunter2-idrac-pw"

#: Short bound for the tests that deliberately block the loader.
FAST_TIMEOUT_S = 0.05


def write_secrets(directory: Path, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "secrets.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def resolver_for(path: Path) -> SecretResolver:
    """A resolver over ``path`` with its own registry, so tests never touch the process-wide one."""
    return SecretResolver(path, registry=CredentialRegistry())


@pytest.fixture
def secrets_file(tmp_path: Path) -> Path:
    return write_secrets(tmp_path, f'idrac_a_password: "{SECRET_VALUE}"\nmqtt_password: broker-pw\n')


# --------------------------------------------------------------------------------------------------
# reference syntax
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("!secret idrac_a_password", "idrac_a_password"),
        ("!secret   spaced_name  ", "spaced_name"),
        ("  !secret idrac_a_password  ", "idrac_a_password"),
        ("!secret a name with spaces", "a name with spaces"),
        # Written as a reference, names nothing: reported rather than read as a literal.
        ("!secret", ""),
        ("!secret   ", ""),
        # Not references at all.
        ("!secretpassword", None),
        ("secret idrac_a_password", None),
        ("hunter2", None),
        ("", None),
        (None, None),
        (1234, None),
        (["!secret idrac_a_password"], None),
    ],
)
def test_parse_reference_recognises_only_the_tag_form(value: object, expected: str | None) -> None:
    assert parse_reference(value) == expected
    assert is_reference(value) is (expected is not None)


# --------------------------------------------------------------------------------------------------
# resolution
# --------------------------------------------------------------------------------------------------


def test_resolves_a_reference_from_the_secrets_file(secrets_file: Path) -> None:
    outcome = resolver_for(secrets_file).resolve("idrac_a_password")

    assert isinstance(outcome, ResolvedSecret)
    assert outcome.name == "idrac_a_password"
    assert outcome.plaintext == SECRET_VALUE
    # First redaction layer: the wrapper never renders its plaintext (Requirement 8.2).
    assert SECRET_VALUE not in repr(outcome)
    assert SECRET_VALUE not in str(outcome.value)


def test_substitute_resolves_a_reference_and_passes_a_literal_through(secrets_file: Path) -> None:
    resolver = resolver_for(secrets_file)

    resolved, failure = resolver.substitute("!secret idrac_a_password")
    assert (resolved, failure) == (SECRET_VALUE, None)

    literal, failure = resolver.substitute("a-literal-password")
    assert (literal, failure) == ("a-literal-password", None)

    untouched, failure = resolver.substitute(False)
    assert (untouched, failure) == (False, None)


def test_substitute_returns_the_original_value_with_the_failure(secrets_file: Path) -> None:
    value, failure = resolver_for(secrets_file).substitute("!secret absent_name")

    assert value == "!secret absent_name"
    assert failure is not None
    assert failure.failure is SecretFailure.NOT_FOUND
    assert failure.name == "absent_name"


def test_resolved_values_are_registered_for_redaction(secrets_file: Path) -> None:
    registry = CredentialRegistry()
    resolver = SecretResolver(secrets_file, registry=registry)

    assert registry.is_empty
    resolver.resolve("idrac_a_password")

    # Requirements 8.2, 8.7: the value is shielded the moment it is read, so any later log
    # record or exception rendering that embeds it is scrubbed.
    assert registry.redact(f"connecting with {SECRET_VALUE}") == f"connecting with {REDACTION_MARKER}"


def test_an_empty_secret_value_resolves_and_is_left_to_the_parser(tmp_path: Path) -> None:
    # Requirement 8.5 owns the empty-credential rejection, so resolution succeeds here and the
    # parser applies one rule to literal and resolved values alike.
    path = write_secrets(tmp_path, 'idrac_a_password: ""\n')

    outcome = resolver_for(path).resolve("idrac_a_password")

    assert isinstance(outcome, ResolvedSecret)
    assert outcome.plaintext == ""


def test_a_numeric_secret_is_rendered_but_a_boolean_is_rejected(tmp_path: Path) -> None:
    path = write_secrets(tmp_path, "numeric: 12345\nbooleanish: yes\n")
    resolver = resolver_for(path)

    numeric = resolver.resolve("numeric")
    assert isinstance(numeric, ResolvedSecret)
    assert numeric.plaintext == "12345"

    # YAML reads unquoted `yes` as True; authenticating with "True" would be a silent wrong
    # answer, so the entry is reported as unusable instead.
    booleanish = resolver.resolve("booleanish")
    assert isinstance(booleanish, UnresolvedSecret)
    assert booleanish.failure is SecretFailure.UNSUPPORTED_VALUE


def test_a_structured_secret_names_its_type_and_not_its_content(tmp_path: Path) -> None:
    path = write_secrets(tmp_path, f"structured:\n  - {SECRET_VALUE}\n")

    outcome = resolver_for(path).resolve("structured")

    assert isinstance(outcome, UnresolvedSecret)
    assert outcome.failure is SecretFailure.UNSUPPORTED_VALUE
    assert "list" in outcome.detail
    assert SECRET_VALUE not in outcome.describe()


# --------------------------------------------------------------------------------------------------
# missing references and missing stores
# --------------------------------------------------------------------------------------------------


def test_a_missing_reference_names_the_reference(secrets_file: Path) -> None:
    outcome = resolver_for(secrets_file).resolve("idrac_b_password")

    assert isinstance(outcome, UnresolvedSecret)
    assert outcome.failure is SecretFailure.NOT_FOUND
    assert outcome.name == "idrac_b_password"
    # Requirement 8.6: the report names the reference.
    assert "idrac_b_password" in outcome.describe()


def test_a_missing_secrets_file_is_reported_per_reference(tmp_path: Path) -> None:
    outcome = resolver_for(tmp_path / "absent.yaml").resolve("idrac_a_password")

    assert isinstance(outcome, UnresolvedSecret)
    assert outcome.failure is SecretFailure.STORE_MISSING


def test_an_empty_secrets_file_yields_only_missing_references(tmp_path: Path) -> None:
    path = write_secrets(tmp_path, "# nothing here yet\n")

    outcome = resolver_for(path).resolve("idrac_a_password")

    assert isinstance(outcome, UnresolvedSecret)
    assert outcome.failure is SecretFailure.NOT_FOUND


def test_a_bare_tag_is_reported_as_a_malformed_reference(secrets_file: Path) -> None:
    value, failure = resolver_for(secrets_file).substitute("!secret")

    assert value == "!secret"
    assert failure is not None
    assert failure.failure is SecretFailure.MALFORMED_REFERENCE


def test_an_over_long_reference_name_is_truncated_in_the_report(secrets_file: Path) -> None:
    outcome = resolver_for(secrets_file).resolve("n" * (MAX_REFERENCE_LENGTH + 1))

    assert isinstance(outcome, UnresolvedSecret)
    assert outcome.failure is SecretFailure.MALFORMED_REFERENCE
    assert len(outcome.name) == MAX_REFERENCE_LENGTH


# --------------------------------------------------------------------------------------------------
# the store loader
# --------------------------------------------------------------------------------------------------


def test_malformed_yaml_is_reported_without_the_offending_line(tmp_path: Path) -> None:
    # An unterminated quoted scalar: PyYAML's own message renders the offending source line,
    # which here is the secret itself. The report must be built from the error class and its
    # position instead (Requirements 8.2, 8.6).
    path = write_secrets(tmp_path, f'idrac_a_password: "{SECRET_VALUE}\n')

    outcome = resolver_for(path).resolve("idrac_a_password")

    assert isinstance(outcome, UnresolvedSecret)
    assert outcome.failure is SecretFailure.STORE_MALFORMED
    assert SECRET_VALUE not in outcome.describe()
    assert "line" in outcome.detail


def test_a_non_mapping_document_is_malformed(tmp_path: Path) -> None:
    path = write_secrets(tmp_path, f"- {SECRET_VALUE}\n")

    with pytest.raises(SecretStoreError) as caught:
        load_secret_store(path)

    assert caught.value.failure is SecretFailure.STORE_MALFORMED
    assert SECRET_VALUE not in caught.value.detail


def test_non_utf8_content_is_malformed(tmp_path: Path) -> None:
    path = tmp_path / "secrets.yaml"
    path.write_bytes(b"idrac_a_password: \xff\xfe\n")

    with pytest.raises(SecretStoreError) as caught:
        load_secret_store(path)

    assert caught.value.failure is SecretFailure.STORE_MALFORMED


def test_an_oversize_store_is_unreadable(tmp_path: Path) -> None:
    path = write_secrets(tmp_path, "a: " + "x" * (MAX_STORE_BYTES + 1) + "\n")

    with pytest.raises(SecretStoreError) as caught:
        load_secret_store(path)

    assert caught.value.failure is SecretFailure.STORE_UNREADABLE


def test_non_string_keys_are_readable_by_their_text_form(tmp_path: Path) -> None:
    path = write_secrets(tmp_path, "1234: numeric-key-pw\n")

    assert load_secret_store(path) == {"1234": "numeric-key-pw"}


def test_candidates_are_probed_in_order_and_an_explicit_path_wins(tmp_path: Path) -> None:
    absent = tmp_path / "absent" / "secrets.yaml"
    present = write_secrets(tmp_path, "from_candidate: candidate-pw\n")

    assert load_secret_store(candidates=(absent, present)) == {"from_candidate": "candidate-pw"}
    assert load_secret_store(present, candidates=(absent,)) == {"from_candidate": "candidate-pw"}

    with pytest.raises(SecretStoreError) as caught:
        load_secret_store(candidates=(absent,))
    assert caught.value.failure is SecretFailure.STORE_MISSING


def test_the_default_candidates_are_the_mounted_configuration_directory() -> None:
    # The manifest declares `map: [homeassistant_config:ro]`; current Supervisor releases mount
    # it at /homeassistant and older ones at /config.
    assert [str(path) for path in DEFAULT_SECRETS_PATHS] == [
        "/homeassistant/secrets.yaml",
        "/config/secrets.yaml",
    ]


def test_the_store_is_read_once_for_many_references(secrets_file: Path) -> None:
    calls = 0

    def counting_loader() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"idrac_a_password": SECRET_VALUE}

    resolver = SecretResolver(loader=counting_loader, registry=CredentialRegistry())
    for _ in range(5):
        assert isinstance(resolver.resolve("idrac_a_password"), ResolvedSecret)
    assert isinstance(resolver.resolve("absent"), UnresolvedSecret)

    assert calls == 1


def test_nothing_is_read_until_a_reference_is_resolved() -> None:
    def exploding_loader() -> dict[str, object]:
        raise AssertionError("the store must not be loaded before a reference needs it")

    SecretResolver(loader=exploding_loader, registry=CredentialRegistry())


def test_a_loader_raising_an_arbitrary_exception_is_classified_not_propagated() -> None:
    def broken_loader() -> dict[str, object]:
        raise RuntimeError(f"boom while reading {SECRET_VALUE}")

    outcome = SecretResolver(loader=broken_loader, registry=CredentialRegistry()).resolve("idrac_a_password")

    assert isinstance(outcome, UnresolvedSecret)
    assert outcome.failure is SecretFailure.STORE_UNREADABLE
    # The exception's own message may well have interpolated store content, so only its class
    # name is reported.
    assert SECRET_VALUE not in outcome.describe()
    assert "RuntimeError" in outcome.detail


# --------------------------------------------------------------------------------------------------
# the resolution bound (Requirement 8.4)
# --------------------------------------------------------------------------------------------------


@pytest.fixture
def blocking_loader() -> Iterator[tuple[threading.Event, threading.Event]]:
    """A loader that blocks until released, so the bound can be exercised deterministically."""
    entered = threading.Event()
    release = threading.Event()
    yield entered, release
    # Always release, so the daemon thread does not outlive the test still holding the event.
    release.set()


def test_the_default_bound_is_five_seconds() -> None:
    assert SECRET_RESOLUTION_TIMEOUT_S == 5.0


def test_a_stalled_store_times_out_within_the_bound(
    blocking_loader: tuple[threading.Event, threading.Event],
) -> None:
    entered, release = blocking_loader

    def stalling_loader() -> dict[str, object]:
        entered.set()
        release.wait(30)
        return {"idrac_a_password": SECRET_VALUE}

    resolver = SecretResolver(loader=stalling_loader, timeout_s=FAST_TIMEOUT_S, registry=CredentialRegistry())

    started = time.monotonic()
    outcome = resolver.resolve("idrac_a_password")
    elapsed = time.monotonic() - started

    assert entered.is_set()
    assert isinstance(outcome, UnresolvedSecret)
    assert outcome.failure is SecretFailure.TIMEOUT
    assert outcome.name == "idrac_a_password"
    # The wait is bounded by the budget, not by the stalled read. The upper bound is generous
    # because a loaded CI machine schedules threads late; what matters is that it is not 30 s.
    assert elapsed < 2.0


def test_the_bound_is_spent_once_not_once_per_reference(
    blocking_loader: tuple[threading.Event, threading.Event],
) -> None:
    entered, release = blocking_loader

    def stalling_loader() -> dict[str, object]:
        entered.set()
        release.wait(30)
        return {}

    resolver = SecretResolver(loader=stalling_loader, timeout_s=FAST_TIMEOUT_S, registry=CredentialRegistry())
    assert isinstance(resolver.resolve("first"), UnresolvedSecret)

    # Requirement 1.3 gives startup 30 seconds in total, so 50 devices must not each wait out
    # the budget: after the first give-up, later references fail immediately.
    started = time.monotonic()
    for name in ("second", "third", "fourth"):
        outcome = resolver.resolve(name)
        assert isinstance(outcome, UnresolvedSecret)
        assert outcome.failure is SecretFailure.TIMEOUT
    assert time.monotonic() - started < FAST_TIMEOUT_S


def test_a_late_load_is_used_by_the_next_reference(
    blocking_loader: tuple[threading.Event, threading.Event],
) -> None:
    entered, release = blocking_loader

    def stalling_loader() -> dict[str, object]:
        entered.set()
        release.wait(30)
        return {"idrac_a_password": SECRET_VALUE}

    resolver = SecretResolver(loader=stalling_loader, timeout_s=FAST_TIMEOUT_S, registry=CredentialRegistry())
    assert isinstance(resolver.resolve("idrac_a_password"), UnresolvedSecret)

    release.set()
    deadline = time.monotonic() + 5.0
    outcome = resolver.resolve("idrac_a_password")
    while isinstance(outcome, UnresolvedSecret) and time.monotonic() < deadline:
        time.sleep(0.01)
        outcome = resolver.resolve("idrac_a_password")

    # Giving up suppresses further waiting, not the result itself.
    assert isinstance(outcome, ResolvedSecret)
    assert outcome.plaintext == SECRET_VALUE


# --------------------------------------------------------------------------------------------------
# Requirement 8.6: no failure path carries the referenced value
# --------------------------------------------------------------------------------------------------


def unresolvable_cases(tmp_path: Path) -> list[tuple[str, SecretResolver, str]]:
    """One entry per failure classification the resolver can report."""
    stall = threading.Event()

    def stalling_loader() -> dict[str, object]:
        stall.wait(FAST_TIMEOUT_S * 4)
        return {}

    def broken_loader() -> dict[str, object]:
        raise RuntimeError(f"failed on {SECRET_VALUE}")

    valid = write_secrets(tmp_path / "valid", f'name: "{SECRET_VALUE}"\n')
    malformed = write_secrets(tmp_path / "malformed", f'name: "{SECRET_VALUE}\n')
    structured = write_secrets(tmp_path / "structured", f"name:\n  - {SECRET_VALUE}\n")

    return [
        ("not_found", resolver_for(valid), "absent_name"),
        ("malformed_reference", resolver_for(valid), ""),
        ("store_missing", resolver_for(tmp_path / "absent" / "secrets.yaml"), "name"),
        ("store_malformed", resolver_for(malformed), "name"),
        ("unsupported_value", resolver_for(structured), "name"),
        (
            "store_unreadable",
            SecretResolver(loader=broken_loader, registry=CredentialRegistry()),
            "name",
        ),
        (
            "timeout",
            SecretResolver(loader=stalling_loader, timeout_s=FAST_TIMEOUT_S, registry=CredentialRegistry()),
            "name",
        ),
    ]


def test_no_failure_report_contains_the_secret_value(tmp_path: Path) -> None:
    for label, resolver, reference in unresolvable_cases(tmp_path):
        outcome = resolver.resolve(reference)
        assert isinstance(outcome, UnresolvedSecret), label

        rendered = f"{outcome.describe()} {outcome!r} {outcome.detail}"
        assert SECRET_VALUE not in rendered, label
        # Requirement 8.2 forbids even a 4-character fragment of a credential, so the report is
        # checked against every window of the value rather than the value alone.
        for index in range(len(SECRET_VALUE) - 3):
            assert SECRET_VALUE[index : index + 4] not in rendered, f"{label}: leaked a fragment"
