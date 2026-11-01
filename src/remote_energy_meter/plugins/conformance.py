"""The reference conformance suite a plugin author runs against their own plugin (Requirement 10.2).

This module ships *inside* the package rather than under ``tests/``, because its audience is a third-party plugin
author who has the package installed and not this repository checked out::

    from remote_energy_meter.plugins.conformance import assert_plugin_conforms

    def test_acme_conforms() -> None:
        assert_plugin_conforms(
            AcmePlugin,
            valid_params={"host": "192.0.2.10", "username": "root", "password": "hunter2"},
        )

Four decisions in here are load-bearing:

* **Element inspection is not reimplemented.** The structural pass calls
  :func:`~remote_energy_meter.plugins.registry.missing_interface_elements`, so the registry's exclusion record
  (Requirement 3.9) and this suite's failure report (Requirement 10.4) name the *same* element strings. An author who
  reads a startup log and then runs the suite should not have to translate between two vocabularies. It also means the
  identifier check of Requirement 10.2 — non-empty, at most 64 characters — is the registry's own check rather than a
  second opinion that could drift from it.

* **Every check runs, and every failure is reported.** Requirement 10.4 asks for a failed *result* naming each
  unsatisfied element, not the first one. So checks collect findings and :func:`assert_plugin_conforms` raises once at
  the end. A suite that stopped at the first failure would make an author fix four omissions in four runs.

* **A check that cannot run is skipped, not failed.** If ``config_model`` is not a pydantic model there is nothing to
  validate parameters against, and if the parameters are rejected there is no instance to read power from. Reporting
  those as independent failures would triple one root cause and bury it. Skips are reported alongside failures so the
  gap is visible rather than silent.

* **A device that is unreachable is the author's normal case.** Requirement 10.2 wants ``read_power`` to return a
  ``Power_Reading`` with a device identifier, watts within 0 to 100000, and a UTC timestamp, which needs something at
  the other end of the socket. ``require_reading=False`` accepts a classified ``ReadFailure`` instead and reports the
  reading-shape check as skipped, so the suite is still worth running against a fake, a stub, or nothing at all. That
  has to hold for a *blackholed* address too, not only one that refuses fast, which is why the suite's own deadline
  sits a grace margin past the bound it hands the plugin rather than on top of it — see
  :data:`CONFORMANCE_TIMEOUT_GRACE_S`.

The watts range checked here is the wider publisher-level range of Requirement 10.2 (0 to 100000 W), not the iDRAC
plugin's own chassis-plausibility range of 0 to 10000 W (Requirement 4.11). The suite has to hold for any family,
including one that meters a rack.

Nothing in this module may satisfy a ``Plugin_Interface`` element. Discovery walks this package, so a helper or fake
carrying a usable ``plugin_id`` would either register as a plugin or be reported as an incomplete one on every
startup. Stub plugins for the suite's own tests are defined in the test module, not here.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Final, cast

from pydantic import BaseModel, ValidationError

from remote_energy_meter.plugins.interface import (
    PLUGIN_ID_MAX_LENGTH,
    PLUGIN_ID_MIN_LENGTH,
    PLUGIN_INTERFACE_ELEMENTS,
    DevicePlugin,
    FailureClassification,
    PowerReading,
    ReadFailure,
    ValidationResult,
)
from remote_energy_meter.plugins.registry import missing_interface_elements

__all__ = [
    "CONFORMANCE_DEVICE_ID",
    "CONFORMANCE_TIMEOUT_GRACE_S",
    "CONFORMANCE_TIMEOUT_S",
    "MAX_READING_WATTS",
    "MIN_READING_WATTS",
    "ConformanceCheck",
    "ConformanceFailure",
    "ConformanceReport",
    "ConformanceSkip",
    "assert_plugin_conforms",
    "assert_plugin_conforms_async",
    "check_conformance",
    "check_conformance_async",
]

#: The device identifier handed to the plugin under test. Satisfies the ``Device_Entry`` identifier form of
#: Requirement 2.12, so a plugin that echoes it into a topic or a log field sees a realistic value.
CONFORMANCE_DEVICE_ID: Final = "conformance_device"

#: The per-operation bound handed to the plugin. Backstopped by the suite with :func:`asyncio.timeout` rather than
#: trusted to the plugin: a plugin that ignores the timeout it was handed would otherwise hang the author's test run
#: instead of failing it.
CONFORMANCE_TIMEOUT_S: Final = 10.0

#: How much longer the suite waits than the bound it hands the plugin. The outer deadline must be *strictly greater*
#: than the inner one. Both start at the same instant, the outer one first, so equal values make the suite's own
#: deadline always the earlier of the two: a plugin that correctly honours ``timeout_s`` would be cancelled at the very
#: moment it was about to classify and return its own ``TIMEOUT`` failure, and the report would blame a suite deadline
#: breach for what the plugin did right (Requirement 10.4 asks the report to name the element that was not satisfied).
#: The margin is additive and small rather than a multiple of the bound, so the suite's budget stays predictable: an
#: operation is cut off at ``timeout_s + CONFORMANCE_TIMEOUT_GRACE_S``, never at some surprising multiple of the value
#: the author passed. It only has to cover the plugin returning a value it has already decided on.
CONFORMANCE_TIMEOUT_GRACE_S: Final = 0.5

#: Requirement 10.2: the accepted watts range for any plugin's reading. Wider than any one family's plausibility range
#: on purpose — this is the publisher-level guard of Requirement 6.14, which must hold for a future family that draws
#: far more than one chassis.
MIN_READING_WATTS: Final = 0.0
MAX_READING_WATTS: Final = 100000.0

_UTC_OFFSET: Final = timedelta(0)


class ConformanceCheck(StrEnum):
    """One verifiable claim from Requirement 10.2, or the structural precondition they all rest on."""

    INTERFACE_ELEMENT = "interface_element_is_satisfied"
    """The element is present and usable — an identifier that is a bounded non-empty string, a schema that is a
    pydantic model, an operation that is callable. Delegated to the registry, so this is also the identifier
    length and non-emptiness check of Requirement 10.2."""

    SCHEMA_ACCEPTS_VALID = "config_model_accepts_valid_parameters"
    """The published schema validates a well-formed parameter mapping for this family."""

    SCHEMA_REJECTS_INCOMPLETE = "config_model_rejects_a_missing_required_field"
    """The published schema refuses a parameter mapping with a required field removed. A schema that accepts anything
    pushes the failure to the first read, where the cause is no longer visible."""

    VALIDATION_RESULT = "validate_connection_returns_a_classified_result"
    """The validation operation returns a :class:`ValidationResult` — success, or a failure carrying one
    classification — rather than raising or returning something else."""

    READ_RESULT = "read_power_returns_a_classified_result"
    """The read operation returns a :class:`PowerReading` or a classified :class:`ReadFailure` rather than raising."""

    READING_SHAPE = "read_power_returns_a_well_formed_power_reading"
    """The returned reading carries the device identifier it was constructed with, watts within
    :data:`MIN_READING_WATTS` to :data:`MAX_READING_WATTS`, and a timezone-aware UTC timestamp."""


@dataclass(frozen=True, slots=True)
class ConformanceFailure:
    """One check the plugin under test did not satisfy.

    Attributes:
        element: The unsatisfied ``Plugin_Interface`` element, named exactly as the registry names it
            (Requirement 10.4).
        check: Which claim about that element failed.
        detail: What was observed, in terms an author can act on.
    """

    element: str
    check: ConformanceCheck
    detail: str

    def __str__(self) -> str:
        return f"{self.element}: {self.check.value} -- {self.detail}"


@dataclass(frozen=True, slots=True)
class ConformanceSkip:
    """One check that could not be run, and why.

    Reported rather than dropped: a suite that silently skipped half its checks would read as a pass.
    """

    element: str
    check: ConformanceCheck
    reason: str

    def __str__(self) -> str:
        return f"{self.element}: {self.check.value} -- not run: {self.reason}"


@dataclass(frozen=True, slots=True)
class ConformanceReport:
    """The completed result of the suite: a verdict, every failure, and every check that could not run.

    Returned rather than raised, so Requirement 10.4's "completes with a failed result" is observable without catching
    an exception. :func:`assert_plugin_conforms` is the thin wrapper that turns a failed report into one.

    Attributes:
        plugin: Where the class under test was defined, as ``module.QualName``.
        failures: Every failed check, in the order the checks ran.
        skipped: Every check that could not be run.
    """

    plugin: str
    failures: tuple[ConformanceFailure, ...] = ()
    skipped: tuple[ConformanceSkip, ...] = ()

    @property
    def ok(self) -> bool:
        """Whether every check that ran was satisfied."""
        return not self.failures

    @property
    def failed_elements(self) -> tuple[str, ...]:
        """Each ``Plugin_Interface`` element with at least one failure, once, in interface declaration order."""
        failed = {failure.element for failure in self.failures}
        return tuple(element for element in PLUGIN_INTERFACE_ELEMENTS if element in failed)

    @property
    def failed_checks(self) -> tuple[ConformanceCheck, ...]:
        """Each failed check, once, in declaration order."""
        failed = {failure.check for failure in self.failures}
        return tuple(check for check in ConformanceCheck if check in failed)

    def summary(self) -> str:
        """A multi-line report naming every failed check and its element, suitable as an assertion message."""
        if self.ok:
            headline = f"{self.plugin} satisfies the Plugin_Interface"
        else:
            headline = (
                f"{self.plugin} does not satisfy the Plugin_Interface: "
                f"{len(self.failures)} failed check(s) on {', '.join(self.failed_elements)}"
            )
        lines = [headline]
        lines.extend(f"  FAIL {failure}" for failure in self.failures)
        lines.extend(f"  SKIP {skip}" for skip in self.skipped)
        return "\n".join(lines)


# --------------------------------------------------------------------------------------------------
# internals
# --------------------------------------------------------------------------------------------------


#: The checks that depend on an instance existing, so they can be skipped together when one cannot be created.
_OPERATION_CHECKS: Final[tuple[tuple[str, ConformanceCheck], ...]] = (
    ("validate_connection", ConformanceCheck.VALIDATION_RESULT),
    ("read_power", ConformanceCheck.READ_RESULT),
    ("read_power", ConformanceCheck.READING_SHAPE),
)


class _Findings:
    """Accumulates failures and skips in the order the checks ran."""

    __slots__ = ("failures", "skipped")

    def __init__(self) -> None:
        self.failures: list[ConformanceFailure] = []
        self.skipped: list[ConformanceSkip] = []

    def fail(self, element: str, check: ConformanceCheck, detail: str) -> None:
        self.failures.append(ConformanceFailure(element, check, detail))

    def skip(self, element: str, check: ConformanceCheck, reason: str) -> None:
        self.skipped.append(ConformanceSkip(element, check, reason))


def _backstop_deadline(timeout_s: float) -> float:
    """The suite's own deadline for one operation: strictly later than the bound the plugin was handed.

    See :data:`CONFORMANCE_TIMEOUT_GRACE_S` for why the two cannot be equal.
    """
    return timeout_s + CONFORMANCE_TIMEOUT_GRACE_S


def _deadline_breached(operation: str, timeout_s: float, deadline_s: float) -> str:
    """What to report when the backstop fired: the plugin never returned at all, on-time or late.

    Names the operation as well as the numbers, so the sentence still says which ``Plugin_Interface`` element failed
    when it is read apart from the failure carrying it (Requirement 10.4).
    """
    return (
        f"{operation} did not complete within {deadline_s:g}s -- the {timeout_s:g}s bound it was handed plus a"
        f" {CONFORMANCE_TIMEOUT_GRACE_S:g}s grace margin -- so it neither honoured timeout_s nor returned a"
        " classified failure"
    )


def _element_value(plugin_cls: type, element: str) -> object:
    """The value a class provides for one interface element, or ``None`` when it provides none."""
    return getattr(plugin_cls, element, None)


def _describe_unsatisfied(plugin_cls: type, element: str) -> str:
    """Why ``element`` is not satisfied, phrased as what was expected against what was found.

    The verdict comes from :func:`missing_interface_elements`; this only explains it, so the two cannot disagree about
    *whether* an element is satisfied even though only one of them says *why*.
    """
    value = _element_value(plugin_cls, element)
    if value is None:
        return "absent"
    if element == "plugin_id":
        if not isinstance(value, str):
            return f"expected a string, found {type(value).__name__}"
        return (
            f"expected {PLUGIN_ID_MIN_LENGTH} to {PLUGIN_ID_MAX_LENGTH} characters, found {len(value)}"
            f" ({value[:PLUGIN_ID_MAX_LENGTH]!r})"
        )
    if element == "config_model":
        return f"expected a pydantic BaseModel subclass, found {value!r}"
    return f"expected a callable, found {type(value).__name__}"


def _required_field_names(schema: type[BaseModel]) -> tuple[str, ...]:
    """The schema's required field names, in declaration order."""
    return tuple(name for name, info in schema.model_fields.items() if info.is_required())


def _without_a_required_field(
    schema: type[BaseModel], valid_params: Mapping[str, Any]
) -> tuple[dict[str, Any], str] | None:
    """``valid_params`` with its first required field removed, and that field's name.

    ``None`` when the schema declares no required field the mapping supplies, in which case Requirement 10.2's
    missing-field check has nothing to remove and is skipped rather than invented.
    """
    for name in _required_field_names(schema):
        if name in valid_params:
            return {key: value for key, value in valid_params.items() if key != name}, name
    return None


def _check_schema(
    schema: type[BaseModel],
    valid_params: Mapping[str, Any],
    invalid_params: Mapping[str, Any] | None,
    findings: _Findings,
) -> BaseModel | None:
    """Run both schema checks, returning the validated parameters when the schema accepted them."""
    element = "config_model"
    model: BaseModel | None = None
    try:
        model = schema.model_validate(dict(valid_params))
    except ValidationError as error:
        findings.fail(
            element,
            ConformanceCheck.SCHEMA_ACCEPTS_VALID,
            f"{schema.__name__} rejected the supplied valid parameters: {error.errors(include_url=False)}",
        )

    incomplete: Mapping[str, Any] | None = invalid_params
    removed: str | None = None
    if incomplete is None:
        derived = _without_a_required_field(schema, valid_params)
        if derived is None:
            findings.skip(
                element,
                ConformanceCheck.SCHEMA_REJECTS_INCOMPLETE,
                f"{schema.__name__} declares no required field present in the supplied valid parameters,"
                " so no incomplete mapping could be derived; supply invalid_params to check this explicitly",
            )
            return model
        incomplete, removed = derived

    try:
        schema.model_validate(dict(incomplete))
    except ValidationError:
        return model

    omission = f"with the required field {removed!r} removed" if removed is not None else "supplied as invalid_params"
    findings.fail(
        element,
        ConformanceCheck.SCHEMA_REJECTS_INCOMPLETE,
        f"{schema.__name__} accepted a parameter mapping {omission}",
    )
    return model


def _instantiate(plugin_cls: type, device_id: str, params: BaseModel, findings: _Findings) -> DevicePlugin | None:
    """Create the one instance both operation checks share.

    A constructor failure is reported against *both* operation elements, because both checks genuinely did not pass and
    each has to name its own element (Requirement 10.4). Construction is not an element of its own: inventing a fifth
    name would put a word in the report that the registry never uses.
    """
    try:
        return cast("type[DevicePlugin]", plugin_cls)(device_id, params)
    # A third-party constructor may raise anything at all; the suite reports it instead of propagating it.
    except Exception as exc:
        detail = f"the plugin could not be constructed with (device_id, params): {type(exc).__name__}: {exc}"
        findings.fail("validate_connection", ConformanceCheck.VALIDATION_RESULT, detail)
        findings.fail("read_power", ConformanceCheck.READ_RESULT, detail)
        return None


async def _check_validate_connection(plugin: DevicePlugin, timeout_s: float, findings: _Findings) -> None:
    """Requirement 10.2: the validation operation returns a success result or a classified failure."""
    element = "validate_connection"
    deadline_s = _backstop_deadline(timeout_s)
    try:
        async with asyncio.timeout(deadline_s):
            returned: object = await plugin.validate_connection(timeout_s)
    except TimeoutError:
        findings.fail(
            element,
            ConformanceCheck.VALIDATION_RESULT,
            _deadline_breached(element, timeout_s, deadline_s),
        )
        return
    except asyncio.CancelledError:
        raise
    # A plugin defect is a conformance failure, not an error in the suite.
    except Exception as exc:
        findings.fail(element, ConformanceCheck.VALIDATION_RESULT, f"raised {type(exc).__name__}: {exc}")
        return

    if not isinstance(returned, ValidationResult):
        findings.fail(
            element,
            ConformanceCheck.VALIDATION_RESULT,
            f"returned {type(returned).__name__}, expected a ValidationResult",
        )


async def _check_read_power(
    plugin: DevicePlugin, device_id: str, timeout_s: float, *, require_reading: bool, findings: _Findings
) -> None:
    """Requirement 10.2: the read operation returns a well-formed reading, or a classified failure."""
    deadline_s = _backstop_deadline(timeout_s)
    try:
        async with asyncio.timeout(deadline_s):
            returned: object = await plugin.read_power(timeout_s)
    except TimeoutError:
        findings.fail(
            "read_power",
            ConformanceCheck.READ_RESULT,
            _deadline_breached("read_power", timeout_s, deadline_s),
        )
        return
    except asyncio.CancelledError:
        raise
    # A plugin defect is a conformance failure, not an error in the suite.
    except Exception as exc:
        findings.fail("read_power", ConformanceCheck.READ_RESULT, f"raised {type(exc).__name__}: {exc}")
        return

    if isinstance(returned, PowerReading):
        _check_reading_shape(returned, device_id, findings)
        return

    if isinstance(returned, ReadFailure):
        _check_read_failure(returned, device_id, require_reading=require_reading, findings=findings)
        return

    findings.fail(
        "read_power",
        ConformanceCheck.READ_RESULT,
        f"returned {type(returned).__name__}, expected a PowerReading or a ReadFailure",
    )
    findings.skip("read_power", ConformanceCheck.READING_SHAPE, "no PowerReading was returned")


def _check_read_failure(failure: ReadFailure, device_id: str, *, require_reading: bool, findings: _Findings) -> None:
    """A classified failure satisfies the read contract; whether it satisfies *this run* is the caller's choice."""
    # Read through an ``object`` binding: the annotation says this is always a member, and a third-party plugin is
    # exactly the place where an annotation is a claim rather than a guarantee.
    classification: object = failure.classification
    if not isinstance(classification, FailureClassification):
        findings.fail(
            "read_power",
            ConformanceCheck.READ_RESULT,
            f"returned a ReadFailure classified {classification!r}, which is not a FailureClassification member",
        )
    if failure.device_id != device_id:
        findings.fail(
            "read_power",
            ConformanceCheck.READ_RESULT,
            f"returned a ReadFailure naming device {failure.device_id!r}, expected {device_id!r}",
        )

    described = f"the device under test answered with a classified {classification} failure: {failure.detail}"
    if require_reading:
        findings.fail(
            "read_power",
            ConformanceCheck.READING_SHAPE,
            f"{described}; point the plugin at a reachable device or a fake,"
            " or pass require_reading=False to accept a classified failure",
        )
    else:
        findings.skip("read_power", ConformanceCheck.READING_SHAPE, described)


def _check_reading_shape(reading: PowerReading, device_id: str, findings: _Findings) -> None:
    """Requirement 10.2: device identifier, watts within 0 to 100000, and a timezone-aware UTC timestamp."""
    element = "read_power"
    check = ConformanceCheck.READING_SHAPE

    if reading.device_id != device_id:
        findings.fail(element, check, f"the reading names device {reading.device_id!r}, expected {device_id!r}")

    watts: object = reading.watts
    if isinstance(watts, bool) or not isinstance(watts, int | float):
        findings.fail(element, check, f"watts is {type(watts).__name__}, expected a number")
    elif not math.isfinite(watts):
        findings.fail(element, check, f"watts is {watts!r}, expected a finite number")
    elif not MIN_READING_WATTS <= watts <= MAX_READING_WATTS:
        findings.fail(
            element,
            check,
            f"watts is {watts!r}, outside {MIN_READING_WATTS:g} to {MAX_READING_WATTS:g} inclusive",
        )

    measured_at: object = reading.measured_at
    if not isinstance(measured_at, datetime):
        findings.fail(element, check, f"measured_at is {type(measured_at).__name__}, expected a datetime")
        return
    offset = measured_at.utcoffset()
    if offset is None:
        findings.fail(element, check, "measured_at is timezone-naive, expected a timezone-aware UTC timestamp")
    elif offset != _UTC_OFFSET:
        findings.fail(element, check, f"measured_at carries a {offset} offset, expected UTC")


def _refuse_inside_a_running_loop(entry_point: str) -> None:
    """Fail loudly rather than deadlock when a blocking entry point is called from a coroutine."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError(
        f"{entry_point}() blocks on an event loop and cannot run inside one; await {entry_point}_async() instead"
    )


# --------------------------------------------------------------------------------------------------
# public entry points
# --------------------------------------------------------------------------------------------------


async def check_conformance_async(
    plugin_cls: type,
    valid_params: Mapping[str, Any],
    invalid_params: Mapping[str, Any] | None = None,
    *,
    device_id: str = CONFORMANCE_DEVICE_ID,
    timeout_s: float = CONFORMANCE_TIMEOUT_S,
    require_reading: bool = True,
) -> ConformanceReport:
    """Run every conformance check against ``plugin_cls`` and report the result (Requirements 10.2, 10.4).

    Never raises on account of the plugin: a defect anywhere in it becomes a failed check. The returned report is the
    "failed result" of Requirement 10.4, naming each unsatisfied ``Plugin_Interface`` element.

    :param plugin_cls: the plugin class under test. Deliberately untyped as ``type``, because whether it satisfies the
        ``DevicePlugin`` protocol is the question, not the premise.
    :param valid_params: a parameter mapping this family's schema should accept.
    :param invalid_params: a mapping the schema should reject. Defaults to ``valid_params`` with its first required
        field removed, which is exactly the missing-required-field case of Requirement 10.2.
    :param device_id: the identifier the instance is constructed with, and the one its results must carry.
    :param timeout_s: the bound handed to each operation. The suite backstops it at
        ``timeout_s + CONFORMANCE_TIMEOUT_GRACE_S``, so a plugin that honours the bound gets to report its own
        classified failure while one that ignores it entirely is still cut off and reported as failing that element.
    :param require_reading: when false, a classified ``ReadFailure`` is accepted from ``read_power`` and the
        reading-shape check is reported as skipped — for running the suite without a reachable device.
    """
    findings = _Findings()
    location = f"{plugin_cls.__module__}.{plugin_cls.__qualname__}"

    missing = set(missing_interface_elements(plugin_cls))
    for element in PLUGIN_INTERFACE_ELEMENTS:
        if element in missing:
            findings.fail(
                element,
                ConformanceCheck.INTERFACE_ELEMENT,
                _describe_unsatisfied(plugin_cls, element),
            )

    params: BaseModel | None = None
    if "config_model" in missing:
        for check in (ConformanceCheck.SCHEMA_ACCEPTS_VALID, ConformanceCheck.SCHEMA_REJECTS_INCOMPLETE):
            findings.skip("config_model", check, "config_model is not a pydantic model")
    else:
        schema = cast("type[BaseModel]", _element_value(plugin_cls, "config_model"))
        params = _check_schema(schema, valid_params, invalid_params, findings)

    if params is None:
        for element, check in _OPERATION_CHECKS:
            findings.skip(element, check, "config_model did not accept the supplied valid parameters")
        return ConformanceReport(location, tuple(findings.failures), tuple(findings.skipped))

    plugin = _instantiate(plugin_cls, device_id, params, findings)
    if plugin is None:
        # _instantiate already failed both operation checks; only the reading shape has nothing left to say.
        findings.skip("read_power", ConformanceCheck.READING_SHAPE, "the plugin could not be constructed")
        return ConformanceReport(location, tuple(findings.failures), tuple(findings.skipped))

    if "validate_connection" in missing:
        findings.skip("validate_connection", ConformanceCheck.VALIDATION_RESULT, "validate_connection is not callable")
    else:
        await _check_validate_connection(plugin, timeout_s, findings)

    if "read_power" in missing:
        for check in (ConformanceCheck.READ_RESULT, ConformanceCheck.READING_SHAPE):
            findings.skip("read_power", check, "read_power is not callable")
    else:
        await _check_read_power(plugin, device_id, timeout_s, require_reading=require_reading, findings=findings)

    return ConformanceReport(location, tuple(findings.failures), tuple(findings.skipped))


def check_conformance(
    plugin_cls: type,
    valid_params: Mapping[str, Any],
    invalid_params: Mapping[str, Any] | None = None,
    *,
    device_id: str = CONFORMANCE_DEVICE_ID,
    timeout_s: float = CONFORMANCE_TIMEOUT_S,
    require_reading: bool = True,
) -> ConformanceReport:
    """Blocking :func:`check_conformance_async`, for a synchronous test function.

    :raises RuntimeError: when called from inside a running event loop; await the ``_async`` variant there.
    """
    _refuse_inside_a_running_loop("check_conformance")
    return asyncio.run(
        check_conformance_async(
            plugin_cls,
            valid_params,
            invalid_params,
            device_id=device_id,
            timeout_s=timeout_s,
            require_reading=require_reading,
        )
    )


async def assert_plugin_conforms_async(
    plugin_cls: type,
    valid_params: Mapping[str, Any],
    invalid_params: Mapping[str, Any] | None = None,
    *,
    device_id: str = CONFORMANCE_DEVICE_ID,
    timeout_s: float = CONFORMANCE_TIMEOUT_S,
    require_reading: bool = True,
) -> ConformanceReport:
    """Run the suite and raise :class:`AssertionError` naming every failed check, from a coroutine.

    :returns: the passing report, so a caller can assert further on what was skipped.
    """
    report = await check_conformance_async(
        plugin_cls,
        valid_params,
        invalid_params,
        device_id=device_id,
        timeout_s=timeout_s,
        require_reading=require_reading,
    )
    if not report.ok:
        raise AssertionError(report.summary())
    return report


def assert_plugin_conforms(
    plugin_cls: type,
    valid_params: Mapping[str, Any],
    invalid_params: Mapping[str, Any] | None = None,
    *,
    device_id: str = CONFORMANCE_DEVICE_ID,
    timeout_s: float = CONFORMANCE_TIMEOUT_S,
    require_reading: bool = True,
) -> ConformanceReport:
    """Run the suite and raise :class:`AssertionError` naming every failed check.

    The one call a plugin author needs::

        def test_acme_conforms() -> None:
            assert_plugin_conforms(AcmePlugin, valid_params={"host": "192.0.2.10"}, require_reading=False)

    :returns: the passing report, so a caller can assert further on what was skipped.
    :raises AssertionError: when any check failed, with a message naming each failed check and its element.
    :raises RuntimeError: when called from inside a running event loop; await the ``_async`` variant there.
    """
    _refuse_inside_a_running_loop("assert_plugin_conforms")
    report = check_conformance(
        plugin_cls,
        valid_params,
        invalid_params,
        device_id=device_id,
        timeout_s=timeout_s,
        require_reading=require_reading,
    )
    if not report.ok:
        raise AssertionError(report.summary())
    return report
