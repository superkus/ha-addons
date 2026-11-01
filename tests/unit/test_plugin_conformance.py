"""Unit tests for the shippable plugin conformance suite (task 6.2).

Every check is exercised twice: once against a plugin that satisfies it, and once against a plugin that fails exactly
that check while satisfying the others, so a failure report can be attributed to one cause. Requirement 10.4 wants each
failed check named with its ``Plugin_Interface`` element, so the assertions are about *which element* was named rather
than only about the verdict.

The stub plugins live here rather than in ``remote_energy_meter.plugins.conformance``, because discovery walks that
package: a stub carrying a usable ``plugin_id`` inside the shipped module would either register as a real plugin or be
reported as an incomplete one on every addon startup.

The exhaustive sweep over element subsets is Property 6 (task 6.4); running the suite against the bundled iDRAC plugin
is task 10.8.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, ClassVar, Final, cast

import pytest
from pydantic import BaseModel, Field

from remote_energy_meter.plugins.conformance import (
    MAX_READING_WATTS,
    MIN_READING_WATTS,
    ConformanceCheck,
    ConformanceReport,
    assert_plugin_conforms,
    assert_plugin_conforms_async,
    check_conformance,
    check_conformance_async,
)
from remote_energy_meter.plugins.interface import (
    PLUGIN_ID_MAX_LENGTH,
    PLUGIN_INTERFACE_ELEMENTS,
    FailureClassification,
    PowerReading,
    ReadFailure,
    ReadResult,
    ValidationResult,
)

DEVICE_ID: Final = "conformance_device"
VALID_PARAMS: Final[dict[str, Any]] = {"host": "192.0.2.10"}


class Params(BaseModel):
    """A minimal published schema: one required field, one bounded optional one."""

    host: str = Field(min_length=1)
    port: int = Field(default=443, ge=1, le=65535)


class ConformingPlugin:
    """The reference: satisfies every element and returns a well-formed reading."""

    plugin_id: ClassVar[str] = "stub"
    config_model: ClassVar[type[BaseModel]] = Params

    def __init__(self, device_id: str, params: BaseModel) -> None:
        self.device_id = device_id
        self.params = params
        self.validate_calls = 0
        self.read_calls = 0

    async def validate_connection(self, timeout_s: float) -> ValidationResult:
        self.validate_calls += 1
        return ValidationResult.success()

    async def read_power(self, timeout_s: float) -> ReadResult:
        self.read_calls += 1
        return PowerReading(device_id=self.device_id, watts=214.0, measured_at=datetime.now(tz=UTC))


def plugin_reading(result: object) -> type:
    """A conforming plugin whose read operation returns ``result``, whatever that is."""

    class _Plugin(ConformingPlugin):
        async def read_power(self, timeout_s: float) -> Any:
            self.read_calls += 1
            return result

    return _Plugin


def reading(
    *,
    device_id: str = DEVICE_ID,
    watts: object = 214.0,
    measured_at: object | None = None,
) -> PowerReading:
    """A ``PowerReading`` built past its annotations, so malformed values can be handed to the suite."""
    stamp = datetime.now(tz=UTC) if measured_at is None else measured_at
    return PowerReading(
        device_id=device_id,
        watts=cast("float", watts),
        measured_at=cast("datetime", stamp),
    )


def failed_elements_for(check: ConformanceCheck, report: ConformanceReport) -> tuple[str, ...]:
    """The elements named by failures of one check."""
    return tuple(failure.element for failure in report.failures if failure.check is check)


def detail_for(check: ConformanceCheck, report: ConformanceReport) -> str:
    """The detail of the single failure of one check."""
    details = [failure.detail for failure in report.failures if failure.check is check]
    assert len(details) == 1, f"expected exactly one {check.value} failure, got {details}"
    return details[0]


def skipped_checks(report: ConformanceReport) -> tuple[ConformanceCheck, ...]:
    return tuple(skip.check for skip in report.skipped)


# --------------------------------------------------------------------------------------------------
# the conforming case
# --------------------------------------------------------------------------------------------------


def test_a_conforming_plugin_passes_every_check() -> None:
    report = check_conformance(ConformingPlugin, VALID_PARAMS)

    assert report.ok, report.summary()
    assert report.failures == ()
    assert report.skipped == ()
    assert report.failed_elements == ()
    assert report.failed_checks == ()


def test_the_report_names_where_the_class_was_defined() -> None:
    report = check_conformance(ConformingPlugin, VALID_PARAMS)

    assert report.plugin == f"{ConformingPlugin.__module__}.ConformingPlugin"
    assert report.summary() == f"{report.plugin} satisfies the Plugin_Interface"


def test_assert_plugin_conforms_returns_the_passing_report() -> None:
    """The documented one-call entry point for a plugin author (Requirement 10.2)."""
    report = assert_plugin_conforms(ConformingPlugin, VALID_PARAMS)

    assert report.ok


def test_the_plugin_is_constructed_with_the_device_id_and_its_own_validated_schema() -> None:
    """The instance sees an instance of its own ``config_model``, not the raw mapping."""
    instances: list[ConformingPlugin] = []

    class Recording(ConformingPlugin):
        def __init__(self, device_id: str, params: BaseModel) -> None:
            super().__init__(device_id, params)
            instances.append(self)

    report = check_conformance(Recording, {"host": "198.51.100.1", "port": 8443}, device_id="poweredge_a")

    assert report.ok, report.summary()
    assert len(instances) == 1, "one instance, shared by both operation checks"
    assert instances[0].device_id == "poweredge_a"
    assert isinstance(instances[0].params, Params)
    assert instances[0].params.port == 8443
    assert instances[0].validate_calls == 1
    assert instances[0].read_calls == 1


def test_the_reading_must_carry_the_device_id_the_instance_was_given() -> None:
    report = check_conformance(ConformingPlugin, VALID_PARAMS, device_id="poweredge_a")

    assert report.ok, report.summary()


# --------------------------------------------------------------------------------------------------
# element 1: plugin_id (Requirement 10.2 identifier bounds)
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("declared", "expected_in_detail"),
    [
        (None, "absent"),
        ("", "found 0"),
        ("x" * (PLUGIN_ID_MAX_LENGTH + 1), f"found {PLUGIN_ID_MAX_LENGTH + 1}"),
        (3, "expected a string"),
    ],
)
def test_an_unusable_identifier_fails_naming_plugin_id(declared: object, expected_in_detail: str) -> None:
    """Requirement 10.2: a non-empty identifier of at most 64 characters, reported as the ``plugin_id`` element."""

    class BadIdentifier(ConformingPlugin):
        plugin_id: ClassVar[Any] = declared

    report = check_conformance(BadIdentifier, VALID_PARAMS)

    assert not report.ok
    assert report.failed_elements == ("plugin_id",)
    assert failed_elements_for(ConformanceCheck.INTERFACE_ELEMENT, report) == ("plugin_id",)
    assert expected_in_detail in detail_for(ConformanceCheck.INTERFACE_ELEMENT, report)


def test_an_identifier_at_the_length_boundary_is_accepted() -> None:
    class LongestIdentifier(ConformingPlugin):
        plugin_id: ClassVar[str] = "x" * PLUGIN_ID_MAX_LENGTH

    assert check_conformance(LongestIdentifier, VALID_PARAMS).ok


# --------------------------------------------------------------------------------------------------
# element 2: config_model (Requirement 10.2 schema acceptance and rejection)
# --------------------------------------------------------------------------------------------------


def test_a_schema_that_is_not_a_model_fails_and_skips_everything_downstream() -> None:
    """With nothing to validate against, the parameter and operation checks have no premise."""

    class NotAModel(ConformingPlugin):
        config_model: ClassVar[Any] = dict

    report = check_conformance(NotAModel, VALID_PARAMS)

    assert report.failed_elements == ("config_model",)
    assert "BaseModel subclass" in detail_for(ConformanceCheck.INTERFACE_ELEMENT, report)
    assert skipped_checks(report) == (
        ConformanceCheck.SCHEMA_ACCEPTS_VALID,
        ConformanceCheck.SCHEMA_REJECTS_INCOMPLETE,
        ConformanceCheck.VALIDATION_RESULT,
        ConformanceCheck.READ_RESULT,
        ConformanceCheck.READING_SHAPE,
    )


def test_a_schema_that_rejects_the_valid_parameters_fails_naming_config_model() -> None:
    report = check_conformance(ConformingPlugin, {"host": "192.0.2.10", "port": 0})

    assert report.failed_elements == ("config_model",)
    assert failed_elements_for(ConformanceCheck.SCHEMA_ACCEPTS_VALID, report) == ("config_model",)
    assert "port" in detail_for(ConformanceCheck.SCHEMA_ACCEPTS_VALID, report)


def test_the_missing_field_case_is_derived_from_the_valid_parameters() -> None:
    """Requirement 10.2: the schema must reject a mapping with a required field removed."""
    report = check_conformance(ConformingPlugin, VALID_PARAMS)

    assert report.ok, report.summary()
    assert ConformanceCheck.SCHEMA_REJECTS_INCOMPLETE not in skipped_checks(report)


def test_a_schema_that_accepts_the_supplied_invalid_parameters_fails() -> None:
    """An author-supplied mapping the schema does not reject is the failure Requirement 10.2 is after."""
    report = check_conformance(ConformingPlugin, VALID_PARAMS, {"host": "192.0.2.11", "unknown": 1})

    assert report.failed_elements == ("config_model",)
    assert failed_elements_for(ConformanceCheck.SCHEMA_REJECTS_INCOMPLETE, report) == ("config_model",)


def test_a_schema_with_no_required_field_skips_the_missing_field_check() -> None:
    """Nothing can be removed from an all-optional schema, so the check is skipped rather than invented."""

    class AllOptional(BaseModel):
        host: str = "192.0.2.10"

    class NothingRequired(ConformingPlugin):
        config_model: ClassVar[type[BaseModel]] = AllOptional

    report = check_conformance(NothingRequired, VALID_PARAMS)

    assert report.ok, report.summary()
    assert skipped_checks(report) == (ConformanceCheck.SCHEMA_REJECTS_INCOMPLETE,)
    assert "no required field" in report.skipped[0].reason


# --------------------------------------------------------------------------------------------------
# construction
# --------------------------------------------------------------------------------------------------


def test_a_constructor_that_raises_fails_both_operation_checks() -> None:
    """Neither operation could be exercised, so each names its own element (Requirement 10.4)."""

    class HostileConstructor(ConformingPlugin):
        def __init__(self, device_id: str, params: BaseModel) -> None:
            raise RuntimeError("cannot open keyring")

    report = check_conformance(HostileConstructor, VALID_PARAMS)

    assert report.failed_elements == ("validate_connection", "read_power")
    assert report.failed_checks == (ConformanceCheck.VALIDATION_RESULT, ConformanceCheck.READ_RESULT)
    for failure in report.failures:
        assert "cannot open keyring" in failure.detail
    assert skipped_checks(report) == (ConformanceCheck.READING_SHAPE,)


# --------------------------------------------------------------------------------------------------
# element 3: validate_connection
# --------------------------------------------------------------------------------------------------


def test_a_classified_validation_failure_satisfies_the_check() -> None:
    """Requirement 10.2 accepts either a success result or a classified failure."""

    class Unreachable(ConformingPlugin):
        async def validate_connection(self, timeout_s: float) -> ValidationResult:
            return ValidationResult.failure(FailureClassification.AUTHENTICATION)

    assert check_conformance(Unreachable, VALID_PARAMS).ok


def test_a_validation_operation_that_is_not_callable_fails_naming_its_element() -> None:
    class NotCallable(ConformingPlugin):
        validate_connection: ClassVar[Any] = 7

    report = check_conformance(NotCallable, VALID_PARAMS)

    assert report.failed_elements == ("validate_connection",)
    assert "expected a callable" in detail_for(ConformanceCheck.INTERFACE_ELEMENT, report)
    assert skipped_checks(report) == (ConformanceCheck.VALIDATION_RESULT,)


def test_a_validation_operation_that_raises_fails_naming_its_element() -> None:
    class Raising(ConformingPlugin):
        async def validate_connection(self, timeout_s: float) -> ValidationResult:
            raise RuntimeError("session table is full")

    report = check_conformance(Raising, VALID_PARAMS)

    assert report.failed_elements == ("validate_connection",)
    assert "RuntimeError: session table is full" in detail_for(ConformanceCheck.VALIDATION_RESULT, report)


def test_a_validation_operation_returning_something_else_fails() -> None:
    class WrongType(ConformingPlugin):
        async def validate_connection(self, timeout_s: float) -> Any:
            return True

    report = check_conformance(WrongType, VALID_PARAMS)

    assert report.failed_elements == ("validate_connection",)
    assert "returned bool" in detail_for(ConformanceCheck.VALIDATION_RESULT, report)


def test_a_validation_operation_that_ignores_its_timeout_is_cut_off() -> None:
    """The bound is the suite's, not the plugin's: a stalling plugin fails the run rather than hanging it."""

    class Stalling(ConformingPlugin):
        async def validate_connection(self, timeout_s: float) -> ValidationResult:
            await asyncio.sleep(3600)
            return ValidationResult.success()

    report = check_conformance(Stalling, VALID_PARAMS, timeout_s=0.01)

    assert report.failed_elements == ("validate_connection",)
    assert "did not complete within" in detail_for(ConformanceCheck.VALIDATION_RESULT, report)


# --------------------------------------------------------------------------------------------------
# element 4: read_power
# --------------------------------------------------------------------------------------------------


def test_a_read_operation_that_is_not_callable_fails_naming_its_element() -> None:
    class NotCallable(ConformingPlugin):
        read_power: ClassVar[Any] = 7

    report = check_conformance(NotCallable, VALID_PARAMS)

    assert report.failed_elements == ("read_power",)
    assert skipped_checks(report) == (ConformanceCheck.READ_RESULT, ConformanceCheck.READING_SHAPE)


def test_a_read_operation_that_raises_fails_naming_its_element() -> None:
    class Raising(ConformingPlugin):
        async def read_power(self, timeout_s: float) -> ReadResult:
            raise ZeroDivisionError("division by zero")

    report = check_conformance(Raising, VALID_PARAMS)

    assert report.failed_elements == ("read_power",)
    assert "ZeroDivisionError" in detail_for(ConformanceCheck.READ_RESULT, report)


def test_a_read_operation_returning_something_else_fails_and_skips_the_shape_check() -> None:
    report = check_conformance(plugin_reading(214.0), VALID_PARAMS)

    assert report.failed_elements == ("read_power",)
    assert "returned float" in detail_for(ConformanceCheck.READ_RESULT, report)
    assert skipped_checks(report) == (ConformanceCheck.READING_SHAPE,)


def test_a_read_operation_that_ignores_its_timeout_is_cut_off() -> None:
    class Stalling(ConformingPlugin):
        async def read_power(self, timeout_s: float) -> ReadResult:
            await asyncio.sleep(3600)
            raise AssertionError("unreachable")

    report = check_conformance(Stalling, VALID_PARAMS, timeout_s=0.01)

    assert report.failed_elements == ("read_power",)
    assert "did not complete within" in detail_for(ConformanceCheck.READ_RESULT, report)


# --------------------------------------------------------------------------------------------------
# the suite's deadline is a backstop, not a race the plugin loses
# --------------------------------------------------------------------------------------------------

#: How far past its bound a stub runs before returning its own classified failure. A plugin that honours ``timeout_s``
#: decides at the bound and returns just after it; this is that "just after", kept far below
#: :data:`CONFORMANCE_TIMEOUT_GRACE_S` so the test is deterministic rather than a race.
PAST_THE_BOUND_S: Final = 0.02


class HonoursItsBound(ConformingPlugin):
    """Times out at the bound it was handed and returns the classified failure the interface asks for."""

    async def read_power(self, timeout_s: float) -> ReadResult:
        self.read_calls += 1
        await asyncio.sleep(timeout_s + PAST_THE_BOUND_S)
        return ReadFailure(
            device_id=self.device_id,
            classification=FailureClassification.TIMEOUT,
            detail=f"no response within {timeout_s:g}s",
        )


def test_a_plugin_that_honours_its_bound_reports_its_own_timeout_rather_than_a_suite_deadline() -> None:
    """The suite's deadline must exceed the bound it hands out, or a correct plugin is reported as non-conforming.

    With the two equal the outer deadline always fired first, so ``require_reading=False`` could never reach the
    plugin's own ``TIMEOUT`` ``ReadFailure`` against a device that is slow rather than fast to refuse -- which is
    exactly the no-device case that flag exists for -- and Requirement 10.4 named a suite deadline breach for
    something the plugin got right.
    """
    report = check_conformance(HonoursItsBound, VALID_PARAMS, timeout_s=0.05, require_reading=False)

    assert report.ok, report.summary()
    assert skipped_checks(report) == (ConformanceCheck.READING_SHAPE,)
    assert "timeout" in report.skipped[0].reason


def test_a_plugin_that_honours_its_bound_still_fails_a_run_that_requires_a_reading() -> None:
    """Its own failure surfacing does not make a classified failure count as a reading: the verdict is unchanged."""
    report = check_conformance(HonoursItsBound, VALID_PARAMS, timeout_s=0.05)

    assert report.failed_checks == (ConformanceCheck.READING_SHAPE,)
    assert "require_reading=False" in detail_for(ConformanceCheck.READING_SHAPE, report)


def test_a_plugin_that_never_returns_still_fails_naming_read_power() -> None:
    """The backstop stays a backstop: giving the plugin room is not the same as waiting on it forever."""

    class NeverReturns(ConformingPlugin):
        async def read_power(self, timeout_s: float) -> ReadResult:
            await asyncio.sleep(3600)
            raise AssertionError("unreachable")

    report = check_conformance(NeverReturns, VALID_PARAMS, timeout_s=0.01, require_reading=False)

    assert report.failed_elements == ("read_power",)
    assert failed_elements_for(ConformanceCheck.READ_RESULT, report) == ("read_power",)
    assert "read_power did not complete within" in detail_for(ConformanceCheck.READ_RESULT, report)


# --------------------------------------------------------------------------------------------------
# read_power: a classified failure, and the run that will not accept one
# --------------------------------------------------------------------------------------------------

UNREACHABLE: Final = ReadFailure(
    device_id=DEVICE_ID,
    classification=FailureClassification.COMMUNICATION,
    detail="192.0.2.10 refused the connection",
)


def test_a_classified_read_failure_fails_the_shape_check_by_default() -> None:
    """Requirement 10.2 wants a reading; the report says how to ask for less rather than pretending it got one."""
    report = check_conformance(plugin_reading(UNREACHABLE), VALID_PARAMS)

    assert report.failed_elements == ("read_power",)
    assert report.failed_checks == (ConformanceCheck.READING_SHAPE,)
    assert "require_reading=False" in detail_for(ConformanceCheck.READING_SHAPE, report)


def test_a_classified_read_failure_is_accepted_when_no_reading_is_required() -> None:
    """For an author running the suite without a reachable device or a fake."""
    report = check_conformance(plugin_reading(UNREACHABLE), VALID_PARAMS, require_reading=False)

    assert report.ok, report.summary()
    assert skipped_checks(report) == (ConformanceCheck.READING_SHAPE,)
    assert "communication" in report.skipped[0].reason


def test_a_read_failure_naming_another_device_fails_even_when_no_reading_is_required() -> None:
    other = ReadFailure(
        device_id="someone_else",
        classification=FailureClassification.TIMEOUT,
        detail="exceeded 10s",
    )

    report = check_conformance(plugin_reading(other), VALID_PARAMS, require_reading=False)

    assert report.failed_elements == ("read_power",)
    assert failed_elements_for(ConformanceCheck.READ_RESULT, report) == ("read_power",)
    assert "someone_else" in detail_for(ConformanceCheck.READ_RESULT, report)


def test_a_read_failure_carrying_a_foreign_classification_fails() -> None:
    foreign = ReadFailure(
        device_id=DEVICE_ID,
        classification=cast("FailureClassification", "went wrong"),
        detail="",
    )

    report = check_conformance(plugin_reading(foreign), VALID_PARAMS, require_reading=False)

    assert report.failed_elements == ("read_power",)
    assert "FailureClassification" in detail_for(ConformanceCheck.READ_RESULT, report)


# --------------------------------------------------------------------------------------------------
# read_power: the shape of the reading (Requirement 10.2)
# --------------------------------------------------------------------------------------------------


def test_a_reading_naming_another_device_fails() -> None:
    report = check_conformance(plugin_reading(reading(device_id="someone_else")), VALID_PARAMS)

    assert report.failed_elements == ("read_power",)
    assert failed_elements_for(ConformanceCheck.READING_SHAPE, report) == ("read_power",)
    assert "someone_else" in detail_for(ConformanceCheck.READING_SHAPE, report)


@pytest.mark.parametrize("watts", [MIN_READING_WATTS, 214.0, MAX_READING_WATTS, 0, 1000])
def test_watts_inside_the_publishable_range_are_accepted(watts: float) -> None:
    """Requirement 10.2: 0 to 100000 W inclusive, the wider publisher-level range."""
    assert check_conformance(plugin_reading(reading(watts=watts)), VALID_PARAMS).ok


@pytest.mark.parametrize(
    "watts",
    [
        -0.1,
        MAX_READING_WATTS + 0.1,
        20000.0 * 6,
        float("nan"),
        float("inf"),
        float("-inf"),
        "220",
        None,
        True,
    ],
)
def test_watts_outside_the_publishable_range_or_not_a_number_fail(watts: object) -> None:
    report = check_conformance(plugin_reading(reading(watts=watts)), VALID_PARAMS)

    assert report.failed_elements == ("read_power",)
    assert report.failed_checks == (ConformanceCheck.READING_SHAPE,)


def test_a_naive_timestamp_fails() -> None:
    """Energy integration differences these timestamps, so a naive value silently distorts every kWh total."""
    report = check_conformance(plugin_reading(reading(measured_at=datetime.now())), VALID_PARAMS)  # noqa: DTZ005

    assert report.failed_elements == ("read_power",)
    assert "timezone-naive" in detail_for(ConformanceCheck.READING_SHAPE, report)


def test_a_non_utc_timestamp_fails() -> None:
    berlin = datetime.now(tz=timezone(timedelta(hours=2)))

    report = check_conformance(plugin_reading(reading(measured_at=berlin)), VALID_PARAMS)

    assert report.failed_elements == ("read_power",)
    assert "expected UTC" in detail_for(ConformanceCheck.READING_SHAPE, report)


def test_a_timestamp_that_is_not_a_datetime_fails() -> None:
    report = check_conformance(plugin_reading(reading(measured_at="now")), VALID_PARAMS)

    assert report.failed_elements == ("read_power",)
    assert "expected a datetime" in detail_for(ConformanceCheck.READING_SHAPE, report)


def test_several_malformed_reading_values_are_all_reported() -> None:
    """Requirement 10.4 asks for each failed check; a first-failure suite would need four runs to fix four faults."""
    malformed = reading(device_id="someone_else", watts=-5.0, measured_at=datetime.now())  # noqa: DTZ005

    report = check_conformance(plugin_reading(malformed), VALID_PARAMS)

    assert len(report.failures) == 3
    assert report.failed_elements == ("read_power",)


# --------------------------------------------------------------------------------------------------
# every element unsatisfied at once (Requirement 10.4)
# --------------------------------------------------------------------------------------------------


def test_a_class_that_is_not_a_plugin_reports_every_unsatisfied_element() -> None:
    class NotAPlugin:
        """Carries no ``Plugin_Interface`` element at all."""

    report = check_conformance(NotAPlugin, VALID_PARAMS)

    assert not report.ok
    assert report.failed_elements == PLUGIN_INTERFACE_ELEMENTS
    assert failed_elements_for(ConformanceCheck.INTERFACE_ELEMENT, report) == PLUGIN_INTERFACE_ELEMENTS
    assert all(failure.detail == "absent" for failure in report.failures)
    assert len(report.skipped) == 5, "nothing downstream of an absent schema can run"


def test_the_failure_report_names_each_element_and_check() -> None:
    class NotAPlugin:
        pass

    summary = check_conformance(NotAPlugin, VALID_PARAMS).summary()

    assert "does not satisfy the Plugin_Interface" in summary
    for element in PLUGIN_INTERFACE_ELEMENTS:
        assert f"FAIL {element}: {ConformanceCheck.INTERFACE_ELEMENT.value}" in summary


def test_assert_plugin_conforms_raises_naming_every_failed_element() -> None:
    """Requirement 10.4: a failed result reporting each failed check, not the first one."""

    class NotAPlugin:
        pass

    with pytest.raises(AssertionError) as raised:
        assert_plugin_conforms(NotAPlugin, VALID_PARAMS)

    message = str(raised.value)
    for element in PLUGIN_INTERFACE_ELEMENTS:
        assert element in message


# --------------------------------------------------------------------------------------------------
# entry points
# --------------------------------------------------------------------------------------------------


async def test_the_async_entry_points_run_inside_a_loop() -> None:
    report = await check_conformance_async(ConformingPlugin, VALID_PARAMS)
    asserted = await assert_plugin_conforms_async(ConformingPlugin, VALID_PARAMS)

    assert report.ok
    assert asserted.ok


async def test_the_async_assertion_raises_on_a_failed_report() -> None:
    class NotAPlugin:
        pass

    with pytest.raises(AssertionError):
        await assert_plugin_conforms_async(NotAPlugin, VALID_PARAMS)


@pytest.mark.parametrize("entry_point", [check_conformance, assert_plugin_conforms])
async def test_the_blocking_entry_points_refuse_to_run_inside_a_loop(entry_point: Any) -> None:
    """Failing loudly beats deadlocking on an event loop that is already running."""
    with pytest.raises(RuntimeError, match=r"await \w+_async"):
        entry_point(ConformingPlugin, VALID_PARAMS)
