"""Properties 5, 6, and 8: plugin resolution, incomplete-module reporting, and startup validation
(tasks 6.3, 6.4, 6.5).

Three properties over one module, because they are the three questions the ``Plugin_Registry`` answers
and each one is a sweep over a space the examples in ``tests/unit/test_plugin_registry.py`` can only
sample:

* **Property 5** (Requirements 3.3, 3.4) — resolution is byte-equality, and it *partitions* a device
  list: the devices admitted to polling are exactly the resolvable entries, in order, and every
  unresolved entry produces one error record carrying enough to fix it.
* **Property 6** (Requirements 3.9, 10.4, 10.5) — for any non-empty subset of the four
  ``Plugin_Interface`` elements a candidate fails to satisfy, both reporting paths name *every*
  unsatisfied element: the registry's exclusion record and the shippable conformance suite's failed
  report.
* **Property 8** (Requirement 3.10) — validation runs exactly once per plugin instance and never gates
  retention, whatever the plugin returns, raises, or refuses to finish.

## Why two mechanisms for generating plugins, not one

Properties 5 and 8 generate **classes in-process**. Nothing they assert depends on the import
machinery: resolution is a mapping lookup and validation is a method call, so writing a package to
disk for each of a few hundred examples would buy no coverage and cost the whole run.

Property 6's discovery half generates **real importable packages**, because module exclusion *is* an
import-time behaviour — the ``__module__`` rule, the candidate rule, and the walk all operate on
imported modules, so a fake would test the fake. Its example count and package size are kept modest
for that reason, and its conformance half runs in-process against the same generated element subsets.

Both mechanisms are driven by one :class:`ClassPlan`, so the two halves of Property 6 sweep the same
space and the shared element vocabulary of Requirements 3.9 and 10.4 is asserted rather than assumed.

## The one case the reported-exclusion assertion excludes

``registry.py`` documents a deliberate trade: a class that satisfies *none* of the four elements is
indistinguishable from an ordinary support type by inspection, so it is skipped silently rather than
reported as an incomplete plugin. Property 6 therefore asserts a reported exclusion only when at least
one element is satisfied, and asserts the *silence* otherwise — the trade is covered, not stepped
around. The conformance suite has no such trade, because its input is a class the author named, so its
half of the property covers the empty-satisfaction case too.

Generators and helpers are local to this module rather than shared, so this file stands alone.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import string
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from itertools import count
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType
from typing import Any, ClassVar, Final, cast

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import BaseModel, Field

from remote_energy_meter.config.models import MAX_PLUGIN_ID_LENGTH, DeviceEntry
from remote_energy_meter.config.runtime import RejectionReason
from remote_energy_meter.diagnostics.logger import DEVICE_ID_FIELD
from remote_energy_meter.plugins.conformance import ConformanceCheck, check_conformance
from remote_energy_meter.plugins.interface import (
    PLUGIN_INTERFACE_ELEMENTS,
    DevicePlugin,
    FailureClassification,
    PowerReading,
    ReadResult,
    ValidationResult,
)
from remote_energy_meter.plugins.registry import (
    ConnectionValidation,
    InstantiatedDevice,
    PluginRegistry,
    discover,
    missing_interface_elements,
    retained_devices,
    validate_connections,
)

#: Identifier alphabet. A subset of the device identifier character class Requirement 2.12 accepts, so
#: a generated identifier is usable as both a ``Device_Entry`` identifier and a plugin identifier.
_ID_ALPHABET: Final = string.ascii_letters + string.digits + "_-"

#: The connection parameters every generated plugin's schema accepts.
VALID_PARAMS: Final[Mapping[str, Any]] = {"host": "192.0.2.10"}

#: The class name every generated plugin carries, so an exclusion record can be located by it.
GENERATED_CLASS_NAME: Final = "GeneratedPlugin"

#: The two elements that are class attributes, and the two that are operations. Declared separately
#: because a generated class body has to put the attributes before ``__init__`` and the operations
#: after it, and reading that split off :data:`PLUGIN_INTERFACE_ELEMENTS` by index would break the
#: moment the interface grows a fifth element.
_ATTRIBUTE_ELEMENTS: Final[tuple[str, ...]] = ("plugin_id", "config_model")
_OPERATION_ELEMENTS: Final[tuple[str, ...]] = ("validate_connection", "read_power")


# --------------------------------------------------------------------------------------------------
# log capture
# --------------------------------------------------------------------------------------------------


#: An isolated logger name, so configuring it cannot disturb the root logger or pytest's capture.
RECORDING_LOGGER_NAME: Final = "remote_energy_meter_property_registry"


class _Recorder(logging.Handler):
    """Collects records rather than formatting them, so assertions read the record's own fields.

    ``caplog`` would be the obvious choice and cannot be used here: it is a function-scoped fixture,
    and Hypothesis reuses a function-scoped fixture across every example of one test, so the records
    of example *n* would still be present when example *n+1* counted them.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextmanager
def recording_logger() -> Iterator[tuple[logging.Logger, list[logging.LogRecord]]]:
    """A logger that captures every record handed to it, and nothing else."""
    logger = logging.getLogger(RECORDING_LOGGER_NAME)
    saved_level, saved_propagate = logger.level, logger.propagate
    logger.handlers.clear()
    logger.filters.clear()
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    recorder = _Recorder()
    logger.addHandler(recorder)
    try:
        yield logger, recorder.records
    finally:
        logger.removeHandler(recorder)
        logger.setLevel(saved_level)
        logger.propagate = saved_propagate


def errors_naming(records: Sequence[logging.LogRecord], fragment: str) -> list[logging.LogRecord]:
    """Every error record whose message mentions ``fragment``."""
    return [record for record in records if record.levelno == logging.ERROR and fragment in record.getMessage()]


# --------------------------------------------------------------------------------------------------
# generated plugins: one plan, two mechanisms
# --------------------------------------------------------------------------------------------------


class Mutation(StrEnum):
    """How a generated class treats one ``Plugin_Interface`` element."""

    SATISFIED = "satisfied"
    """Present and usable."""

    ABSENT = "absent"
    """Not declared at all — the omission Requirement 3.9 is written about."""

    UNUSABLE = "unusable"
    """Declared under the right name but in a form nothing can use: an identifier that is not a
    string, a schema that is not a pydantic model, an operation that is not callable. Distinguished
    from :attr:`ABSENT` because it is the harder case — the name is there, so the class *is* a
    candidate, and only the usability check keeps it out."""


@dataclass(frozen=True, slots=True)
class ClassPlan:
    """One generated plugin class: what it does with each element, and the identifier it declares.

    ``mutations`` is positional against :data:`PLUGIN_INTERFACE_ELEMENTS` so the plan stays hashable
    and Hypothesis can shrink it.
    """

    mutations: tuple[Mutation, ...]
    plugin_id: str

    @classmethod
    def conforming(cls, plugin_id: str) -> ClassPlan:
        """A plan that satisfies every element."""
        return cls(tuple(Mutation.SATISFIED for _ in PLUGIN_INTERFACE_ELEMENTS), plugin_id)

    @property
    def by_element(self) -> Mapping[str, Mutation]:
        """The mutation chosen for each element, by element name."""
        return dict(zip(PLUGIN_INTERFACE_ELEMENTS, self.mutations, strict=True))

    @property
    def unsatisfied(self) -> tuple[str, ...]:
        """Every element this plan does not satisfy, in interface declaration order.

        The same order :func:`missing_interface_elements` reports in, so the two are directly
        comparable rather than compared as sets.
        """
        chosen = self.by_element
        return tuple(element for element in PLUGIN_INTERFACE_ELEMENTS if chosen[element] is not Mutation.SATISFIED)

    @property
    def satisfied_count(self) -> int:
        """How many elements this plan satisfies. Zero is the silently-skipped case."""
        return len(PLUGIN_INTERFACE_ELEMENTS) - len(self.unsatisfied)


class GeneratedParams(BaseModel):
    """The configuration schema every generated plugin publishes: one required field."""

    host: str = Field(min_length=1)


def _generated_init(self: Any, device_id: str, params: BaseModel) -> None:
    """Bind a generated instance to one entry, as :meth:`DevicePlugin.__init__` requires."""
    self.device_id = device_id
    self.params = params


async def _generated_validate_connection(self: Any, timeout_s: float) -> ValidationResult:
    """A validation operation that always answers usably, so only the element sweep can fail."""
    return ValidationResult.success()


async def _generated_read_power(self: Any, timeout_s: float) -> ReadResult:
    """A read operation returning a reading the conformance suite accepts in full."""
    return PowerReading(device_id=self.device_id, watts=214.0, measured_at=datetime.now(tz=UTC))


#: The value a generated class carries for an element it declares unusably. Chosen so each one fails
#: its own usability check for a different reason: wrong type, wrong kind of type, not callable.
_UNUSABLE_VALUES: Final[Mapping[str, object]] = {
    "plugin_id": 3,
    "config_model": dict,
    "validate_connection": 7,
    "read_power": 7,
}


def build_plugin_class(plan: ClassPlan, *, name: str = GENERATED_CLASS_NAME) -> type:
    """A plugin class built in-process from ``plan``.

    Returned as ``type`` rather than ``type[DevicePlugin]``, because whether it satisfies the protocol
    is exactly what the caller is about to find out.
    """
    satisfied: Mapping[str, object] = {
        "plugin_id": plan.plugin_id,
        "config_model": GeneratedParams,
        "validate_connection": _generated_validate_connection,
        "read_power": _generated_read_power,
    }
    namespace: dict[str, Any] = {"__module__": __name__, "__qualname__": name, "__init__": _generated_init}
    for element, mutation in plan.by_element.items():
        if mutation is Mutation.SATISFIED:
            namespace[element] = satisfied[element]
        elif mutation is Mutation.UNUSABLE:
            namespace[element] = _UNUSABLE_VALUES[element]
    return type(name, (), namespace)


def conforming_plugin_class(plugin_id: str, *, name: str = GENERATED_CLASS_NAME) -> type[DevicePlugin]:
    """A fully conforming plugin class declaring ``plugin_id``."""
    return cast("type[DevicePlugin]", build_plugin_class(ClassPlan.conforming(plugin_id), name=name))


# --------------------------------------------------------------------------------------------------
# generated plugin modules, for the discovery half of Property 6
# --------------------------------------------------------------------------------------------------


_MODULE_PREAMBLE: Final = '''"""A generated plugin module."""

from datetime import UTC, datetime
from typing import ClassVar

from pydantic import BaseModel

from remote_energy_meter.plugins.interface import PowerReading, ReadResult, ValidationResult


class Params(BaseModel):
    host: str


'''

_INIT_SOURCE: Final = """    def __init__(self, device_id: str, params: BaseModel) -> None:
        self.device_id = device_id
        self.params = params

"""

#: Source for an element the class satisfies. Only ``plugin_id`` carries a placeholder, and it is the
#: only one formatted, so a brace in any other template could never be mistaken for one.
_SATISFIED_SOURCE: Final[Mapping[str, str]] = {
    "plugin_id": '    plugin_id: ClassVar[str] = "{plugin_id}"\n',
    "config_model": "    config_model: ClassVar[type[BaseModel]] = Params\n",
    "validate_connection": (
        "    async def validate_connection(self, timeout_s: float) -> ValidationResult:\n"
        "        return ValidationResult.success()\n\n"
    ),
    "read_power": (
        "    async def read_power(self, timeout_s: float) -> ReadResult:\n"
        "        return PowerReading(\n"
        "            device_id=self.device_id, watts=214.0, measured_at=datetime.now(tz=UTC)\n"
        "        )\n\n"
    ),
}

#: Source for an element the class declares unusably, mirroring :data:`_UNUSABLE_VALUES`.
_UNUSABLE_SOURCE: Final[Mapping[str, str]] = {
    "plugin_id": "    plugin_id: ClassVar[int] = 3\n",
    "config_model": "    config_model: ClassVar[type] = dict\n",
    "validate_connection": "    validate_connection: ClassVar[int] = 7\n",
    "read_power": "    read_power: ClassVar[int] = 7\n",
}


def _element_source(element: str, plan: ClassPlan) -> str:
    """The class-body source for one element under ``plan``."""
    match plan.by_element[element]:
        case Mutation.SATISFIED:
            if element == "plugin_id":
                return _SATISFIED_SOURCE[element].format(plugin_id=plan.plugin_id)
            return _SATISFIED_SOURCE[element]
        case Mutation.UNUSABLE:
            return _UNUSABLE_SOURCE[element]
        case _:
            return ""


def plugin_module_source(plan: ClassPlan, *, class_name: str = GENERATED_CLASS_NAME) -> str:
    """A module defining one plugin class under ``plan``, plus an unrelated support model.

    The support model (``Params``) is deliberately always present: it satisfies no interface element,
    so it must be skipped silently, and its presence keeps the generated module shaped like a real
    one — where a family's schema sits beside its plugin.
    """
    return "".join(
        [
            _MODULE_PREAMBLE,
            f"class {class_name}:\n",
            *(_element_source(element, plan) for element in _ATTRIBUTE_ELEMENTS),
            _INIT_SOURCE,
            *(_element_source(element, plan) for element in _OPERATION_ELEMENTS),
        ]
    )


_PACKAGE_SEQUENCE: Final = count()


@contextmanager
def importable_package(modules: Mapping[str, str]) -> Iterator[ModuleType]:
    """Write ``modules`` as a real package, import it, and clean it out of the interpreter after.

    Built under the system temporary directory rather than a pytest ``tmp_path``, for two reasons: a
    function-scoped fixture is shared across every Hypothesis example of one test, and the workspace
    may sit on a network mount where a few hundred small writes are the slowest thing in the run.
    """
    with TemporaryDirectory() as location:
        root = Path(location)
        name = f"generated_plugins_{next(_PACKAGE_SEQUENCE)}"
        package = root / name
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        for relative, source in modules.items():
            (package / f"{relative}.py").write_text(source, encoding="utf-8")

        sys.path.insert(0, str(root))
        importlib.invalidate_caches()
        try:
            yield importlib.import_module(name)
        finally:
            for loaded in [module for module in sys.modules if module == name or module.startswith(f"{name}.")]:
                del sys.modules[loaded]
            if str(root) in sys.path:
                sys.path.remove(str(root))


# --------------------------------------------------------------------------------------------------
# scripted plugin, for Property 8
# --------------------------------------------------------------------------------------------------


class ValidationOutcome(StrEnum):
    """What a scripted plugin does when its validation operation is called.

    Five behaviours, covering every route into a :class:`ConnectionValidation`: the two the interface
    documents, and the three ``registry._invoke_validation`` has to convert because a third-party
    plugin's annotation is a claim rather than a guarantee.
    """

    SUCCESS = "success"
    """Returns a successful result."""

    FAILURE = "failure"
    """Returns a failure carrying one classification — the device is up and refusing."""

    RAISE = "raise"
    """Raises where a classified result belongs; converted to a plugin failure."""

    BAD_RETURN = "bad_return"
    """Returns something that is not a :class:`ValidationResult`; also a plugin failure."""

    STALL = "stall"
    """Ignores the timeout it was handed; cut off and converted to a timeout failure."""


#: Long enough that a stalling plugin can only ever be ended by the bound the registry imposes.
_STALL_S: Final = 3600.0

#: The bound handed to a stalling device, and to every other one. A stalling device gets a small
#: bound so the cut-off is the cheap outcome it should be; the rest get a bound they never approach,
#: so a slow machine cannot turn a success into a timeout.
STALL_TIMEOUT_S: Final = 0.01
NORMAL_TIMEOUT_S: Final = 5.0

#: The identifier the scripted plugin declares, and the one every generated entry names.
SCRIPTED_PLUGIN_ID: Final = "scripted"


class ScriptedParams(BaseModel):
    """Connection parameters that carry the scripted behaviour.

    The behaviour travels in the entry's own parameters rather than in mutable class state, so each
    device's outcome is reachable from its own instance and two examples cannot interfere.
    """

    host: str = Field(min_length=1)
    outcome: ValidationOutcome = ValidationOutcome.SUCCESS
    classification: FailureClassification | None = None


class ScriptedPlugin:
    """A conforming plugin whose validation operation does whatever its parameters say, and records
    every call so "exactly once per instance" is directly observable."""

    plugin_id: ClassVar[str] = SCRIPTED_PLUGIN_ID
    config_model: ClassVar[type[BaseModel]] = ScriptedParams

    def __init__(self, device_id: str, params: BaseModel) -> None:
        self.device_id = device_id
        self.params = cast("ScriptedParams", params)
        self.validate_calls: list[float] = []

    async def validate_connection(self, timeout_s: float) -> ValidationResult:
        self.validate_calls.append(timeout_s)
        outcome = self.params.outcome
        if outcome is ValidationOutcome.SUCCESS:
            return ValidationResult.success()
        if outcome is ValidationOutcome.FAILURE:
            classification = self.params.classification
            assert classification is not None, "a scripted failure always carries a classification"
            return ValidationResult.failure(classification)
        if outcome is ValidationOutcome.RAISE:
            raise RuntimeError("iDRAC session table is full")
        if outcome is ValidationOutcome.BAD_RETURN:
            # Exactly what a plugin whose annotation is wrong hands back.
            return cast("ValidationResult", None)
        await asyncio.sleep(_STALL_S)
        return ValidationResult.success()

    async def read_power(self, timeout_s: float) -> ReadResult:
        return PowerReading(device_id=self.device_id, watts=214.0, measured_at=datetime.now(tz=UTC))


def scripted_plugin_of(device: InstantiatedDevice) -> ScriptedPlugin:
    """The scripted instance created for ``device``."""
    plugin = device.plugin
    assert isinstance(plugin, ScriptedPlugin)
    return plugin


def scripted_timeout(device: InstantiatedDevice) -> float:
    """The per-device bound handed to ``validate_connections``."""
    stalling = scripted_plugin_of(device).params.outcome is ValidationOutcome.STALL
    return STALL_TIMEOUT_S if stalling else NORMAL_TIMEOUT_S


def expected_classification(params: ScriptedParams) -> FailureClassification | None:
    """The classification Requirement 3.10's log record and result must carry for ``params``.

    ``None`` means the validation succeeded. A ``match`` over the outcome rather than a mapping, so
    ``mypy --strict`` catches an added outcome that nothing predicts.
    """
    match params.outcome:
        case ValidationOutcome.SUCCESS:
            return None
        case ValidationOutcome.FAILURE:
            return params.classification
        case ValidationOutcome.STALL:
            return FailureClassification.TIMEOUT
        case ValidationOutcome.RAISE | ValidationOutcome.BAD_RETURN:
            return FailureClassification.PLUGIN


# --------------------------------------------------------------------------------------------------
# generators
# --------------------------------------------------------------------------------------------------


def plugin_identifiers() -> st.SearchStrategy[str]:
    """Plugin identifiers within the 1 to 64 character bound of Requirement 3.1."""
    return st.text(alphabet=_ID_ALPHABET, min_size=1, max_size=16)


def device_identifiers() -> st.SearchStrategy[str]:
    """Device identifiers in the form Requirement 2.12 accepts."""
    return st.text(alphabet=_ID_ALPHABET, min_size=1, max_size=16)


def _near_misses(plugin_id: str) -> list[str]:
    """Identifiers a case-folding, trimming, or prefix-matching resolver would wrongly accept.

    Kept within the ``Device_Entry.plugin`` length bound, since a value outside it never reaches
    resolution — the configuration model rejects it first.
    """
    candidates = [
        plugin_id.upper(),
        plugin_id.lower(),
        plugin_id.swapcase(),
        plugin_id.capitalize(),
        f" {plugin_id}",
        f"{plugin_id} ",
        plugin_id[:-1],
        f"{plugin_id}x",
        plugin_id * 2,
    ]
    return [value for value in candidates if 1 <= len(value) <= MAX_PLUGIN_ID_LENGTH]


def named_plugins(discovered: Sequence[str]) -> st.SearchStrategy[str]:
    """The plugin identifier an entry names: an exact match, a near miss, or something unrelated.

    Weighted toward the boundary on purpose. A uniformly random identifier would almost never collide
    with a discovered one, so the resolvable half of the partition would go essentially untested.
    """
    exact = st.sampled_from(list(discovered))
    return st.one_of(
        exact,
        exact.flatmap(lambda plugin_id: st.sampled_from(_near_misses(plugin_id))),
        plugin_identifiers(),
    )


@dataclass(frozen=True, slots=True)
class ResolutionScenario:
    """A set of discovered plugin identifiers and a device list drawn against it."""

    discovered: tuple[str, ...]
    entries: tuple[DeviceEntry, ...]

    @property
    def resolvable(self) -> tuple[DeviceEntry, ...]:
        """Exactly the entries whose named identifier is byte-equal to a discovered one, in order."""
        return tuple(entry for entry in self.entries if entry.plugin in self.discovered)

    @property
    def unresolvable(self) -> tuple[DeviceEntry, ...]:
        """The rest, in order."""
        return tuple(entry for entry in self.entries if entry.plugin not in self.discovered)


@st.composite
def resolution_scenarios(draw: st.DrawFn) -> ResolutionScenario:
    """Any set of discovered identifiers, and any device list naming identifiers against it."""
    discovered = tuple(draw(st.lists(plugin_identifiers(), min_size=1, max_size=4, unique=True)))
    device_ids = draw(st.lists(device_identifiers(), min_size=1, max_size=6, unique=True))
    named = draw(st.lists(named_plugins(discovered), min_size=len(device_ids), max_size=len(device_ids)))
    entries = tuple(
        DeviceEntry(device_id=device_id, plugin=plugin, params=dict(VALID_PARAMS))
        for device_id, plugin in zip(device_ids, named, strict=True)
    )
    return ResolutionScenario(discovered=discovered, entries=entries)


@st.composite
def class_plans(draw: st.DrawFn) -> ClassPlan:
    """Any assignment of mutations to the four elements, with at least one element unsatisfied.

    "At least one" is Property 6's non-empty subset. The all-satisfied assignment is what
    ``tests/unit/test_plugin_registry.py`` already covers as the discovery happy path.

    Non-emptiness is imposed by construction rather than by a filter: one drawn position is forced
    unsatisfied if the free draw left it satisfied. Every non-empty subset is still reachable — pick a
    position the free draw already left unsatisfied — and no example is discarded, so the generator
    cannot approach the too-much-filtering health check as the interface grows elements.
    """
    element_count = len(PLUGIN_INTERFACE_ELEMENTS)
    mutations = draw(st.lists(st.sampled_from(Mutation), min_size=element_count, max_size=element_count))
    forced = draw(st.integers(min_value=0, max_value=element_count - 1))
    if mutations[forced] is Mutation.SATISFIED:
        mutations[forced] = draw(st.sampled_from([Mutation.ABSENT, Mutation.UNUSABLE]))
    return ClassPlan(mutations=tuple(mutations), plugin_id=draw(plugin_identifiers()))


@dataclass(frozen=True, slots=True)
class ValidationScenario:
    """A set of accepted entries, each carrying a scripted validation outcome.

    ``repeats`` is how many times each device is handed to ``validate_connections``. A device named
    twice is not a hypothetical: "exactly once per instance" has to hold for whatever the caller
    passes, not only for a caller that deduplicates first.
    """

    entries: tuple[DeviceEntry, ...]
    repeats: tuple[int, ...]


@st.composite
def validation_scenarios(draw: st.DrawFn) -> ValidationScenario:
    """Any set of 1 to 5 accepted entries, with any assignment of validation outcomes to them."""
    device_ids = draw(st.lists(device_identifiers(), min_size=1, max_size=5, unique=True))
    entries: list[DeviceEntry] = []
    for device_id in device_ids:
        outcome = draw(st.sampled_from(ValidationOutcome))
        classification = draw(st.sampled_from(FailureClassification)) if outcome is ValidationOutcome.FAILURE else None
        entries.append(
            DeviceEntry(
                device_id=device_id,
                plugin=SCRIPTED_PLUGIN_ID,
                params={
                    **VALID_PARAMS,
                    "outcome": outcome.value,
                    "classification": None if classification is None else classification.value,
                },
            )
        )
    repeats = draw(st.lists(st.integers(min_value=1, max_value=2), min_size=len(entries), max_size=len(entries)))
    return ValidationScenario(entries=tuple(entries), repeats=tuple(repeats))


# --------------------------------------------------------------------------------------------------
# Property 5
# --------------------------------------------------------------------------------------------------


# Feature: remote-energy-meter, Property 5: For any set of discovered plugin identifiers and any
# device list, a Device_Entry resolves if and only if its named plugin identifier is byte-equal to a
# discovered identifier; the devices admitted to polling are exactly the resolvable ones, and each
# unresolved entry produces an error record naming the entry identifier, the unresolved plugin
# identifier, and every discovered plugin identifier.
# Validates: Requirements 3.3, 3.4
@settings(max_examples=200, deadline=None)
@given(scenario=resolution_scenarios())
def test_plugin_resolution_is_exact_and_partitions_the_device_list(scenario: ResolutionScenario) -> None:
    """Requirements 3.3, 3.4, over arbitrary identifier sets and device lists."""
    plugins = {plugin_id: conforming_plugin_class(plugin_id) for plugin_id in scenario.discovered}

    with recording_logger() as (logger, records):
        registry = PluginRegistry(plugins, logger=logger)

        # --- resolution is byte-equality, in both directions ---
        # ``resolve`` writes no record, so calling it for every entry cannot disturb the count below.
        assert registry.plugin_ids == scenario.discovered
        for entry in scenario.entries:
            resolved = registry.resolve(entry.plugin)
            assert (resolved is not None) is (entry.plugin in scenario.discovered)
            if resolved is not None:
                assert resolved is plugins[entry.plugin]
                assert resolved.plugin_id == entry.plugin

        outcome = registry.instantiate_all(scenario.entries)

    # --- the devices admitted to polling are exactly the resolvable entries, in order ---
    assert tuple(device.entry for device in outcome.devices) == scenario.resolvable
    assert tuple(device.plugin_id for device in outcome.devices) == tuple(entry.plugin for entry in scenario.resolvable)

    # --- every other entry is excluded, and excluded for the stated reason ---
    assert tuple(rejection.device_id for rejection in outcome.rejections) == tuple(
        entry.device_id for entry in scenario.unresolvable
    )
    for rejection in outcome.rejections:
        assert rejection.reason is RejectionReason.UNKNOWN_PLUGIN
        assert rejection.field_name is None, "an unresolved plugin is not a failing field"

    # --- one error record per unresolved entry, carrying everything needed to fix it ---
    logged = errors_naming(records, "unknown plugin")
    assert len(logged) == len(scenario.unresolvable)
    for record, entry in zip(logged, scenario.unresolvable, strict=True):
        fields = record.__dict__
        assert fields[DEVICE_ID_FIELD] == entry.device_id
        assert fields["plugin_id"] == entry.plugin
        assert fields["discovered_plugin_ids"] == list(scenario.discovered)
        message = record.getMessage()
        assert entry.plugin in message
        for plugin_id in scenario.discovered:
            assert plugin_id in message


# --------------------------------------------------------------------------------------------------
# Property 6
# --------------------------------------------------------------------------------------------------


# Feature: remote-energy-meter, Property 6: For any non-empty subset of Plugin_Interface elements
# removed from a candidate module, the Plugin_Registry excludes that module, emits an error record
# naming the module and every missing element, still discovers all conforming sibling modules, and the
# reference conformance suite reports a failed result naming every unsatisfied element.
# Validates: Requirements 3.9, 10.4, 10.5
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    plan=class_plans(),
    sibling_ids=st.lists(plugin_identifiers(), min_size=1, max_size=2, unique=True),
)
def test_an_incomplete_module_is_excluded_and_every_missing_element_reported(
    plan: ClassPlan, sibling_ids: list[str]
) -> None:
    """Requirements 3.9, 10.5, against real packages written to disk and imported.

    The module names are chosen so the incomplete module sorts before its siblings, which is the order
    ``pkgutil.walk_packages`` visits them in, so the discovered identifiers can be asserted as an
    ordered tuple rather than a set.
    """
    modules = {"a_incomplete": plugin_module_source(plan)} | {
        f"z_sibling_{index}": plugin_module_source(ClassPlan.conforming(plugin_id), class_name=f"Sibling{index}")
        for index, plugin_id in enumerate(sibling_ids)
    }

    with recording_logger() as (logger, records), importable_package(modules) as package:
        result = discover(package, logger=logger)

    # --- a conforming sibling is still discovered, whatever the incomplete module did ---
    assert result.ok
    assert result.plugin_ids == tuple(sibling_ids)
    assert not [item for item in result.excluded if "sibling" in item.module_name]

    excluded = [item for item in result.excluded if item.module_name.endswith(".a_incomplete")]
    element_errors = errors_naming(records, "does not satisfy")

    if plan.satisfied_count == 0:
        # The documented trade: a class satisfying nothing is indistinguishable from a support type,
        # so it is skipped in silence rather than reported as an incomplete plugin.
        assert excluded == []
        assert element_errors == []
        return

    # --- the module is excluded, and every unsatisfied element is named, in one record ---
    assert len(excluded) == 1
    report = excluded[0]
    assert report.class_name == GENERATED_CLASS_NAME
    assert report.missing_elements == plan.unsatisfied
    assert report.detail is None, "an incomplete class is not an import failure"

    assert len(element_errors) == 1
    record = element_errors[0]
    assert record.__dict__["missing_elements"] == list(plan.unsatisfied)
    message = record.getMessage()
    assert report.module_name in message
    assert GENERATED_CLASS_NAME in message
    for element in plan.unsatisfied:
        assert element in message


# Feature: remote-energy-meter, Property 6: For any non-empty subset of Plugin_Interface elements
# removed from a candidate module, the Plugin_Registry excludes that module, emits an error record
# naming the module and every missing element, still discovers all conforming sibling modules, and the
# reference conformance suite reports a failed result naming every unsatisfied element.
# Validates: Requirements 3.9, 10.4, 10.5
@settings(max_examples=200, deadline=None)
@given(plan=class_plans())
def test_the_conformance_suite_names_every_unsatisfied_element(plan: ClassPlan) -> None:
    """Requirement 10.4, over the same element sweep, in-process.

    Asserted against the *same* element strings the registry reports, which is the point of the two
    halves sharing one plan: an author who reads a startup log and then runs the suite must not have to
    translate between two vocabularies.

    The empty-satisfaction case is included here, unlike in the discovery half. The suite's input is a
    class its caller named, so it has no candidate rule to apply and no reason to stay silent.
    """
    plugin_cls = build_plugin_class(plan)

    # The registry's verdict and the suite's are the same verdict, computed once.
    assert missing_interface_elements(plugin_cls) == plan.unsatisfied

    report = check_conformance(plugin_cls, valid_params=VALID_PARAMS)

    assert not report.ok, "a class that does not satisfy the interface cannot pass"
    named = tuple(failure.element for failure in report.failures if failure.check is ConformanceCheck.INTERFACE_ELEMENT)
    assert named == plan.unsatisfied
    assert report.failed_elements == plan.unsatisfied

    # --- the failed result is legible: every unsatisfied element appears in the summary ---
    summary = report.summary()
    for element in plan.unsatisfied:
        assert element in summary
        assert any(failure.detail for failure in report.failures if failure.element == element)


# --------------------------------------------------------------------------------------------------
# Property 8
# --------------------------------------------------------------------------------------------------


async def _validate_scenario(
    scenario: ValidationScenario, logger: logging.Logger
) -> tuple[tuple[InstantiatedDevice, ...], tuple[InstantiatedDevice, ...], tuple[ConnectionValidation, ...]]:
    """Instantiate ``scenario``'s entries and validate them, returning what each assertion needs.

    :returns: the one instance per accepted entry, the sequence actually handed to
        ``validate_connections`` (with repeats), and the validations it produced.
    """
    registry = PluginRegistry({SCRIPTED_PLUGIN_ID: ScriptedPlugin}, logger=logger)
    outcome = registry.instantiate_all(scenario.entries)
    assert outcome.rejections == (), "every generated entry names the scripted plugin with valid parameters"

    passed = tuple(
        device for device, repeats in zip(outcome.devices, scenario.repeats, strict=True) for _ in range(repeats)
    )
    validations = await validate_connections(passed, scripted_timeout, logger=logger)
    return outcome.devices, passed, validations


# Feature: remote-energy-meter, Property 8: For any set of accepted Device_Entry values and any
# assignment of validation outcomes to them, the connection validation operation is invoked exactly
# once per plugin instance, its result is logged with the entry identifier, and the set of devices
# retained for polling equals the full set of accepted entries regardless of the outcomes.
# Validates: Requirements 3.10
@settings(max_examples=150, deadline=None)
@given(scenario=validation_scenarios())
def test_connection_validation_runs_once_and_never_gates_retention(scenario: ValidationScenario) -> None:
    """Requirement 3.10, over every route a plugin has into a validation result.

    Driven through ``asyncio.run`` from a synchronous test rather than as an async test, so each
    example gets its own event loop and the property does not depend on how a loop is shared with
    Hypothesis.
    """
    with recording_logger() as (logger, records):
        devices, passed, validations = asyncio.run(_validate_scenario(scenario, logger))

    accepted_ids = {entry.device_id for entry in scenario.entries}

    # --- one plugin instance per accepted entry ---
    assert len(devices) == len(scenario.entries)
    assert len({id(device.plugin) for device in devices}) == len(scenario.entries)

    # --- exactly once per plugin instance, with that device's own bound ---
    for device in devices:
        assert scripted_plugin_of(device).validate_calls == [scripted_timeout(device)]

    # --- one validation per handed-in device, in order, carrying the classified outcome ---
    assert [validation.device_id for validation in validations] == [device.device_id for device in passed]
    for validation in validations:
        expected = expected_classification(scripted_plugin_of(validation.device).params)
        assert validation.result.classification == expected
        assert validation.ok is (expected is None)

    # --- retention is total: every device comes back, whatever its result ---
    retained = retained_devices(validations)
    assert retained == passed
    assert {device.device_id for device in retained} == accepted_ids

    # --- exactly one record per instance, naming that entry's identifier and its result ---
    logged = [record for record in records if "connection validation" in record.getMessage()]
    assert len(logged) == len(devices)
    assert {record.__dict__[DEVICE_ID_FIELD] for record in logged} == accepted_ids
    by_id = {device.device_id: device for device in devices}
    for record in logged:
        device = by_id[record.__dict__[DEVICE_ID_FIELD]]
        expected = expected_classification(scripted_plugin_of(device).params)
        assert record.levelno == (logging.INFO if expected is None else logging.WARNING)
        if expected is not None:
            assert record.__dict__["classification"] == expected.value
