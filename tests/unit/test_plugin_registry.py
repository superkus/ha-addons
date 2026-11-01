"""Unit tests for the Plugin_Registry (task 6.1).

Discovery is exercised against real packages written to a temporary directory and imported through ``importlib``,
because the behaviour under test *is* the import machinery: recursion into subpackages, a module that raises on import,
and the ``__module__`` rule that keeps a re-exported class from registering twice. Faking ``pkgutil`` would test the
fake.

Resolution, instantiation, and startup validation are exercised against plugin classes defined here, so call counts and
instance identity are directly observable.

The exhaustive element-subset and outcome-assignment sweeps are Properties 5, 6, and 8 (tasks 6.3-6.5); the fatal
conditions get their own coverage in task 6.6.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import sys
from datetime import UTC, datetime
from itertools import count
from pathlib import Path
from types import ModuleType
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel, Field

from remote_energy_meter.config.models import DeviceEntry
from remote_energy_meter.config.runtime import EntryRejection, RejectionReason
from remote_energy_meter.plugins.interface import (
    PLUGIN_INTERFACE_ELEMENTS,
    FailureClassification,
    PowerReading,
    ReadResult,
    ValidationResult,
)
from remote_energy_meter.plugins.registry import (
    FatalRegistryReason,
    InstantiatedDevice,
    PluginRegistry,
    discover,
    missing_interface_elements,
    retained_devices,
    validate_connection,
    validate_connections,
)

TEST_LOGGER: logging.Logger = logging.getLogger("test.plugin_registry")

_PACKAGE_SEQUENCE = count()


# --------------------------------------------------------------------------------------------------
# fixtures: real packages on disk, and in-test plugin classes
# --------------------------------------------------------------------------------------------------


@pytest.fixture
def plugin_package(tmp_path: Path) -> Any:
    """Build an importable package from module sources, and clean it out of the interpreter afterwards."""
    created: list[str] = []

    def _build(modules: dict[str, str], *, init: str = "") -> ModuleType:
        name = f"synthetic_plugins_{next(_PACKAGE_SEQUENCE)}"
        root = tmp_path / name
        root.mkdir(parents=True)
        (root / "__init__.py").write_text(init, encoding="utf-8")
        for relative, source in modules.items():
            path = root / f"{relative}.py"
            path.parent.mkdir(parents=True, exist_ok=True)
            for parent in path.parents:
                if parent == root:
                    break
                init_file = parent / "__init__.py"
                if not init_file.exists():
                    init_file.write_text("", encoding="utf-8")
            path.write_text(source, encoding="utf-8")

        if str(tmp_path) not in sys.path:
            sys.path.insert(0, str(tmp_path))
        importlib.invalidate_caches()
        created.append(name)
        return importlib.import_module(name)

    yield _build

    for name in created:
        for loaded in [module for module in sys.modules if module == name or module.startswith(f"{name}.")]:
            del sys.modules[loaded]
    if str(tmp_path) in sys.path:
        sys.path.remove(str(tmp_path))


CONFORMING_SOURCE = """
from typing import ClassVar

from pydantic import BaseModel

from remote_energy_meter.plugins.interface import PowerReading, ReadResult, ValidationResult


class Params(BaseModel):
    host: str


class {class_name}:
    plugin_id: ClassVar[str] = "{plugin_id}"
    config_model: ClassVar[type[BaseModel]] = Params

    def __init__(self, device_id: str, params: BaseModel) -> None:
        self.device_id = device_id
        self.params = params

    async def validate_connection(self, timeout_s: float) -> ValidationResult:
        return ValidationResult.success()

    async def read_power(self, timeout_s: float) -> ReadResult:
        raise NotImplementedError
"""


def conforming_source(plugin_id: str, class_name: str = "SyntheticPlugin") -> str:
    """Source for a module holding one fully conforming plugin class."""
    return CONFORMING_SOURCE.format(plugin_id=plugin_id, class_name=class_name)


class Params(BaseModel):
    """A minimal plugin configuration schema: one required field, one bounded optional one."""

    host: str = Field(min_length=1)
    port: int = Field(default=443, ge=1, le=65535)


class RecordingPlugin:
    """A conforming plugin that records what was asked of it, so call counts are directly observable."""

    plugin_id: ClassVar[str] = "recording"
    config_model: ClassVar[type[BaseModel]] = Params

    def __init__(self, device_id: str, params: BaseModel) -> None:
        self.device_id = device_id
        self.params = params
        self.validate_calls: list[float] = []

    async def validate_connection(self, timeout_s: float) -> ValidationResult:
        self.validate_calls.append(timeout_s)
        return ValidationResult.success()

    async def read_power(self, timeout_s: float) -> ReadResult:
        return PowerReading(device_id=self.device_id, watts=100.0, measured_at=datetime.now(tz=UTC))


class FailingValidationPlugin(RecordingPlugin):
    """Answers, but not usably: the device is up and refusing the credentials."""

    plugin_id: ClassVar[str] = "failing"

    async def validate_connection(self, timeout_s: float) -> ValidationResult:
        self.validate_calls.append(timeout_s)
        return ValidationResult.failure(FailureClassification.AUTHENTICATION)


class RaisingValidationPlugin(RecordingPlugin):
    """A plugin defect during validation: an exception where a classified result belongs."""

    plugin_id: ClassVar[str] = "raising"

    async def validate_connection(self, timeout_s: float) -> ValidationResult:
        self.validate_calls.append(timeout_s)
        raise RuntimeError("iDRAC session table is full")


class StallingValidationPlugin(RecordingPlugin):
    """A plugin that ignores the timeout it was handed."""

    plugin_id: ClassVar[str] = "stalling"

    async def validate_connection(self, timeout_s: float) -> ValidationResult:
        self.validate_calls.append(timeout_s)
        await asyncio.sleep(3600)
        return ValidationResult.success()


def entry(device_id: str, plugin: str = "recording", **params: object) -> DeviceEntry:
    """A ``Device_Entry`` naming ``plugin``, with defaults that satisfy :class:`Params`."""
    return DeviceEntry(device_id=device_id, plugin=plugin, params={"host": "192.0.2.9", **params})


# --------------------------------------------------------------------------------------------------
# discovery: what is found, and what is reported (Requirements 3.2, 3.9, 10.3, 10.5)
# --------------------------------------------------------------------------------------------------


def test_discovery_finds_a_module_dropped_into_the_package(
    plugin_package: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Requirements 3.2, 10.3: presence in the package is registration, and the identifier is logged."""
    package = plugin_package({"acme": conforming_source("acme")})

    with caplog.at_level(logging.INFO, logger=TEST_LOGGER.name):
        result = discover(package, logger=TEST_LOGGER)

    assert result.ok
    assert result.plugin_ids == ("acme",)
    assert result.plugins["acme"].plugin_id == "acme"

    messages = [record.getMessage() for record in caplog.records]
    assert sum("device plugin discovered: acme" in message for message in messages) == 1
    assert any("discovery complete: 1 plugin" in message for message in messages)


def test_discovery_logs_one_record_per_plugin_then_the_total(
    plugin_package: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Requirement 3.2: one record per plugin, *followed by* the count."""
    package = plugin_package(
        {
            "acme": conforming_source("acme"),
            "brand": conforming_source("brand"),
            "cyber": conforming_source("cyber"),
        }
    )

    with caplog.at_level(logging.INFO, logger=TEST_LOGGER.name):
        result = discover(package, logger=TEST_LOGGER)

    assert set(result.plugin_ids) == {"acme", "brand", "cyber"}

    discovered = [record for record in caplog.records if "device plugin discovered" in record.getMessage()]
    completed = [record for record in caplog.records if "discovery complete" in record.getMessage()]
    assert len(discovered) == 3
    assert len(completed) == 1
    assert caplog.records.index(completed[0]) > max(caplog.records.index(record) for record in discovered)
    assert completed[0].__dict__["plugin_count"] == 3


def test_discovery_recurses_into_family_subpackages(plugin_package: Any) -> None:
    """A family's plugin lives beside its schema at ``<family>/plugin.py``; a flat walk would miss it."""
    package = plugin_package({"idrac/plugin": conforming_source("idrac", class_name="IdracPlugin")})

    result = discover(package, logger=TEST_LOGGER)

    assert result.plugin_ids == ("idrac",)


def test_a_re_exported_plugin_class_is_not_registered_twice(plugin_package: Any) -> None:
    """The ``__module__`` rule: a package ``__init__`` re-export is not a second declaration."""
    package = plugin_package(
        {"idrac/plugin": conforming_source("idrac", class_name="IdracPlugin")},
        init="from .idrac.plugin import IdracPlugin\n\n__all__ = ['IdracPlugin']\n",
    )

    result = discover(package, logger=TEST_LOGGER)

    assert result.ok, "a re-export must not register the same identifier twice"
    assert result.plugin_ids == ("idrac",)


@pytest.mark.parametrize("missing", list(PLUGIN_INTERFACE_ELEMENTS))
def test_a_module_missing_one_element_is_excluded_and_reported(
    plugin_package: Any, caplog: pytest.LogCaptureFixture, missing: str
) -> None:
    """Requirements 3.9, 10.5: excluded, with a record naming the module and the missing element."""
    source = conforming_source("partial")
    if missing == "plugin_id":
        source = source.replace('    plugin_id: ClassVar[str] = "partial"\n', "")
    elif missing == "config_model":
        source = source.replace("    config_model: ClassVar[type[BaseModel]] = Params\n", "")
    else:
        start = source.index(f"    async def {missing}(")
        end = source.index("\n\n", start) + 1 if "\n\n" in source[start:] else len(source)
        source = source[:start] + source[end:]

    package = plugin_package({"partial": source, "good": conforming_source("good")})

    with caplog.at_level(logging.ERROR, logger=TEST_LOGGER.name):
        result = discover(package, logger=TEST_LOGGER)

    assert result.plugin_ids == ("good",), "a conforming sibling is still discovered"
    excluded = [item for item in result.excluded if item.module_name.endswith(".partial")]
    assert len(excluded) == 1
    assert excluded[0].missing_elements == (missing,)
    assert excluded[0].class_name == "SyntheticPlugin"

    records = [record for record in caplog.records if "partial" in record.getMessage()]
    assert len(records) == 1
    assert missing in records[0].getMessage()
    assert records[0].__dict__["missing_elements"] == [missing]


def test_every_missing_element_is_named_in_one_record(plugin_package: Any, caplog: pytest.LogCaptureFixture) -> None:
    """All unsatisfied elements at once: fixing one omission per startup would need four startups."""
    source = """
class BarelyAPlugin:
    plugin_id = ""

    async def read_power(self, timeout_s: float) -> None:
        return None
"""
    package = plugin_package({"barely": source, "good": conforming_source("good")})

    with caplog.at_level(logging.ERROR, logger=TEST_LOGGER.name):
        result = discover(package, logger=TEST_LOGGER)

    excluded = [item for item in result.excluded if item.module_name.endswith(".barely")]
    assert len(excluded) == 1
    assert excluded[0].missing_elements == ("plugin_id", "config_model", "validate_connection")
    message = next(record.getMessage() for record in caplog.records if "barely" in record.getMessage())
    for element in ("plugin_id", "config_model", "validate_connection"):
        assert element in message


def test_a_support_module_with_no_interface_element_is_skipped_silently(
    plugin_package: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A schema-only or helper module was never a plugin, so it produces no exclusion record."""
    support = """
from pydantic import BaseModel


class IdracParams(BaseModel):
    host: str


def default_power_path() -> str:
    return "/redfish/v1/Chassis/System.Embedded.1/Power"
"""
    package = plugin_package({"schema": support, "good": conforming_source("good")})

    with caplog.at_level(logging.DEBUG, logger=TEST_LOGGER.name):
        result = discover(package, logger=TEST_LOGGER)

    assert result.plugin_ids == ("good",)
    assert not [item for item in result.excluded if item.module_name.endswith(".schema")]
    assert not [record for record in caplog.records if "schema" in record.getMessage()]


def test_a_module_that_raises_on_import_excludes_itself_only(
    plugin_package: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """One broken module must not stop every other family from being discovered."""
    package = plugin_package(
        {
            "broken": "raise RuntimeError('missing transitive dependency')\n",
            "good": conforming_source("good"),
        }
    )

    with caplog.at_level(logging.ERROR, logger=TEST_LOGGER.name):
        result = discover(package, logger=TEST_LOGGER)

    assert result.plugin_ids == ("good",)
    excluded = [item for item in result.excluded if item.module_name.endswith(".broken")]
    assert len(excluded) == 1
    assert excluded[0].class_name is None
    assert excluded[0].detail is not None
    assert "RuntimeError" in excluded[0].detail
    assert any("could not be imported" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize(
    ("declaration", "expected_missing"),
    [
        ("    plugin_id = 3\n", ("plugin_id",)),
        ('    plugin_id = ""\n', ("plugin_id",)),
        (f'    plugin_id = "{"x" * 65}"\n', ("plugin_id",)),
        ("    config_model = dict\n", ("config_model",)),
        ("    read_power = 7\n", ("read_power",)),
    ],
)
def test_an_element_present_but_unusable_counts_as_missing(
    plugin_package: Any, declaration: str, expected_missing: tuple[str, ...]
) -> None:
    """Providing a name makes a candidate; providing it usably satisfies the element."""
    source = conforming_source("usable")
    if declaration.startswith("    plugin_id"):
        source = source.replace('    plugin_id: ClassVar[str] = "usable"\n', declaration)
    elif declaration.startswith("    config_model"):
        source = source.replace("    config_model: ClassVar[type[BaseModel]] = Params\n", declaration)
    else:
        source = source.replace(
            "    async def read_power(self, timeout_s: float) -> ReadResult:\n        raise NotImplementedError\n",
            declaration,
        )

    package = plugin_package({"usable": source})
    result = discover(package, logger=TEST_LOGGER)

    assert not result.plugins
    excluded = [item for item in result.excluded if item.module_name.endswith(".usable")]
    assert len(excluded) == 1
    assert excluded[0].missing_elements == expected_missing


def test_the_shipped_package_reports_no_incomplete_modules(caplog: pytest.LogCaptureFixture) -> None:
    """The core's own modules are skipped, not reported: ``plugins.interface`` defines the protocol, and
    ``plugins.registry`` defines support types that happen to carry a ``plugin_id`` attribute.

    Verified against the real package rather than asserted about a special case, because the exclusions are supposed to
    fall out of the candidate rule. Discovery is *not* asserted to be non-fatal here: whether a family plugin has been
    added yet is another task's business.
    """
    with caplog.at_level(logging.DEBUG, logger=TEST_LOGGER.name):
        result = discover(logger=TEST_LOGGER)

    assert "interface" not in result.plugin_ids
    assert result.excluded == (), f"the shipped package reported {result.excluded}"
    assert not [record for record in caplog.records if "does not satisfy" in record.getMessage()]


def test_a_support_class_carrying_an_interface_name_is_not_a_candidate(plugin_package: Any) -> None:
    """Carrying ``plugin_id`` as a dataclass field or a property does not make a class an incomplete plugin."""
    support = """
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Report:
    plugin_id: str


class Wrapper:
    @property
    def plugin_id(self) -> str:
        return "wrapped"
"""
    package = plugin_package({"support": support, "good": conforming_source("good")})

    result = discover(package, logger=TEST_LOGGER)

    assert result.plugin_ids == ("good",)
    assert result.excluded == ()


# --------------------------------------------------------------------------------------------------
# discovery: fatal conditions, reported rather than exited (Requirements 3.5, 3.8)
# --------------------------------------------------------------------------------------------------


def test_a_duplicate_plugin_identifier_is_fatal_and_named(
    plugin_package: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Requirement 3.5: fatal, with a record naming the duplicated identifier — and no ``sys.exit`` in here."""
    package = plugin_package(
        {
            "first": conforming_source("clash", class_name="FirstPlugin"),
            "second": conforming_source("clash", class_name="SecondPlugin"),
        }
    )

    with caplog.at_level(logging.ERROR, logger=TEST_LOGGER.name):
        result = discover(package, logger=TEST_LOGGER)

    assert not result.ok
    assert result.fatal is not None
    assert result.fatal.reason is FatalRegistryReason.DUPLICATE_PLUGIN_ID
    assert result.fatal.plugin_id == "clash"
    assert len(result.fatal.owners) == 2
    assert result.plugins["clash"].__name__ == "FirstPlugin", "the first declaration is kept for diagnostics"

    message = next(
        record.getMessage() for record in caplog.records if "duplicate plugin identifier" in record.getMessage()
    )
    assert "clash" in message
    assert "FirstPlugin" in message
    assert "SecondPlugin" in message


def test_zero_discovered_plugins_is_fatal(plugin_package: Any, caplog: pytest.LogCaptureFixture) -> None:
    """Requirement 3.8: nothing to poll with is a startup failure, not a quiet idle process."""
    package = plugin_package({"notes": '"""No plugin here."""\n'})

    with caplog.at_level(logging.ERROR, logger=TEST_LOGGER.name):
        result = discover(package, logger=TEST_LOGGER)

    assert not result.ok
    assert result.fatal is not None
    assert result.fatal.reason is FatalRegistryReason.NO_PLUGINS_DISCOVERED
    assert any("no device plugin was discovered" in record.getMessage() for record in caplog.records)


def test_missing_interface_elements_reports_in_declaration_order() -> None:
    """The report order follows the interface declaration order, so two reports are comparable."""

    class Nothing:
        pass

    assert missing_interface_elements(Nothing) == PLUGIN_INTERFACE_ELEMENTS
    assert missing_interface_elements(RecordingPlugin) == ()


# --------------------------------------------------------------------------------------------------
# resolution (Requirements 3.3, 3.4)
# --------------------------------------------------------------------------------------------------


@pytest.fixture
def registry() -> PluginRegistry:
    """A registry over the in-test plugin classes."""
    return PluginRegistry(
        {
            "recording": RecordingPlugin,
            "failing": FailingValidationPlugin,
        },
        logger=TEST_LOGGER,
    )


def test_resolution_is_an_exact_match(registry: PluginRegistry) -> None:
    """Requirement 3.3: byte-equal identifiers resolve."""
    assert registry.resolve("recording") is RecordingPlugin
    assert registry.resolve_entry(entry("a", "recording")) is RecordingPlugin


@pytest.mark.parametrize("named", ["Recording", "RECORDING", "recordinG", " recording", "recording "])
def test_resolution_is_case_sensitive_and_not_trimmed(registry: PluginRegistry, named: str) -> None:
    """Requirement 3.3: a case-folded or trimmed match would make two plugin identifiers collide sometimes."""
    assert registry.resolve(named) is None


def test_an_unknown_plugin_rejects_one_entry_and_names_every_alternative(
    registry: PluginRegistry, caplog: pytest.LogCaptureFixture
) -> None:
    """Requirement 3.4: the record names the entry, the unresolved identifier, and every discovered identifier."""
    with caplog.at_level(logging.ERROR, logger=TEST_LOGGER.name):
        rejection = registry.resolve_entry(entry("poweredge_a", "idrac"))

    assert rejection == EntryRejection("poweredge_a", RejectionReason.UNKNOWN_PLUGIN)
    record = next(item for item in caplog.records if "unknown plugin" in item.getMessage())
    assert record.__dict__["device_id"] == "poweredge_a"
    assert record.__dict__["plugin_id"] == "idrac"
    assert record.__dict__["discovered_plugin_ids"] == ["recording", "failing"]
    assert "idrac" in record.getMessage()
    assert "recording" in record.getMessage()


def test_unknown_plugin_entries_are_partitioned_out_of_the_device_list(registry: PluginRegistry) -> None:
    """Requirement 3.4: the remaining entries are resolved, in order."""
    outcome = registry.instantiate_all(
        [entry("a", "recording"), entry("b", "nope"), entry("c", "failing"), entry("d", "also_nope")]
    )

    assert [device.device_id for device in outcome.devices] == ["a", "c"]
    assert [rejection.device_id for rejection in outcome.rejections] == ["b", "d"]
    assert all(rejection.reason is RejectionReason.UNKNOWN_PLUGIN for rejection in outcome.rejections)


# --------------------------------------------------------------------------------------------------
# instantiation (Requirement 3.6)
# --------------------------------------------------------------------------------------------------


def test_one_instance_per_entry(registry: PluginRegistry) -> None:
    """Requirement 3.6: per-device transport state, so one hung socket cannot occupy another's slot."""
    outcome = registry.instantiate_all([entry("a"), entry("b"), entry("c")])

    instances = [device.plugin for device in outcome.devices]
    assert len(instances) == 3
    assert len({id(instance) for instance in instances}) == 3
    assert [instance.device_id for instance in instances] == ["a", "b", "c"]  # type: ignore[attr-defined]


def test_entry_parameters_are_validated_against_the_plugin_schema(registry: PluginRegistry) -> None:
    """The plugin receives an instance of its own ``config_model``, not a raw mapping."""
    outcome = registry.instantiate(entry("a", "recording", port=8443))

    assert isinstance(outcome, InstantiatedDevice)
    params = outcome.plugin.params  # type: ignore[attr-defined]
    assert isinstance(params, Params)
    assert params.host == "192.0.2.9"
    assert params.port == 8443


def test_prevalidated_parameters_are_passed_through_untouched(registry: PluginRegistry) -> None:
    """A caller that already validated the parameters is not made to pay for a second validation."""
    params = Params(host="198.51.100.1", port=1234)

    outcome = registry.instantiate(entry("a"), params=params)

    assert isinstance(outcome, InstantiatedDevice)
    assert outcome.plugin.params is params  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("params", "expected_field"),
    [
        ({"host": ""}, "host"),
        ({"port": 0}, "port"),
        ({"port": "many"}, "port"),
    ],
)
def test_parameters_the_plugin_schema_rejects_exclude_one_entry(
    registry: PluginRegistry, caplog: pytest.LogCaptureFixture, params: dict[str, object], expected_field: str
) -> None:
    """The first failing field is named, ordered by the schema's own declaration order."""
    entry_params = {"host": "192.0.2.9", **params}
    with caplog.at_level(logging.ERROR, logger=TEST_LOGGER.name):
        outcome = registry.instantiate(DeviceEntry(device_id="poweredge_a", plugin="recording", params=entry_params))

    assert outcome == EntryRejection("poweredge_a", RejectionReason.SCHEMA_VIOLATION, expected_field)
    record = next(item for item in caplog.records if "configuration schema" in item.getMessage())
    assert record.__dict__["field"] == expected_field


def test_a_constructor_that_raises_excludes_one_entry_and_keeps_the_rest(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A third-party constructor defect degrades one device, never the device list."""

    class HostileConstructorPlugin(RecordingPlugin):
        plugin_id: ClassVar[str] = "hostile"

        def __init__(self, device_id: str, params: BaseModel) -> None:
            if device_id == "bad":
                raise RuntimeError("cannot open keyring")
            super().__init__(device_id, params)

    registry = PluginRegistry({"hostile": HostileConstructorPlugin}, logger=TEST_LOGGER)

    with caplog.at_level(logging.ERROR, logger=TEST_LOGGER.name):
        outcome = registry.instantiate_all([entry("good", "hostile"), entry("bad", "hostile")])

    assert [device.device_id for device in outcome.devices] == ["good"]
    assert [rejection.device_id for rejection in outcome.rejections] == ["bad"]
    assert any("refused to accept device entry" in record.getMessage() for record in caplog.records)


def test_with_schedule_bridges_to_a_resolved_device(registry: PluginRegistry) -> None:
    """Interval and timeout resolution belong to the scheduler; the registry only pairs the result up."""
    outcome = registry.instantiate(entry("a"))
    assert isinstance(outcome, InstantiatedDevice)

    resolved = outcome.with_schedule(interval_s=60, timeout_s=10.0)

    assert resolved.device_id == "a"
    assert resolved.plugin is outcome.plugin
    assert resolved.interval_s == 60
    assert resolved.timeout_s == 10.0


# --------------------------------------------------------------------------------------------------
# startup connection validation (Requirement 3.10)
# --------------------------------------------------------------------------------------------------


async def test_validation_runs_once_per_instance_and_logs_the_result(
    registry: PluginRegistry, caplog: pytest.LogCaptureFixture
) -> None:
    """Requirement 3.10: exactly once per instance, one record naming the entry identifier and the result."""
    outcome = registry.instantiate_all([entry("a"), entry("b")])

    with caplog.at_level(logging.INFO, logger=TEST_LOGGER.name):
        validations = await validate_connections(outcome.devices, 4.0, logger=TEST_LOGGER)

    assert [validation.device_id for validation in validations] == ["a", "b"]
    assert all(validation.ok for validation in validations)
    for device in outcome.devices:
        assert device.plugin.validate_calls == [4.0]  # type: ignore[attr-defined]

    records = [record for record in caplog.records if "connection validation" in record.getMessage()]
    assert len(records) == 2
    assert {record.__dict__["device_id"] for record in records} == {"a", "b"}


async def test_a_repeated_instance_is_validated_once(registry: PluginRegistry) -> None:
    """Once per *instance* holds whatever the caller passes, not only when the caller is careful."""
    outcome = registry.instantiate(entry("a"))
    assert isinstance(outcome, InstantiatedDevice)

    validations = await validate_connections([outcome, outcome, outcome], 2.0, logger=TEST_LOGGER)

    assert len(validations) == 3
    assert outcome.plugin.validate_calls == [2.0]  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("plugin_cls", "expected"),
    [
        (RecordingPlugin, None),
        (FailingValidationPlugin, FailureClassification.AUTHENTICATION),
        (RaisingValidationPlugin, FailureClassification.PLUGIN),
    ],
)
async def test_every_outcome_retains_the_device(
    caplog: pytest.LogCaptureFixture, plugin_cls: type, expected: FailureClassification | None
) -> None:
    """Requirement 3.10: a powered-off or misconfigured device at startup is still polled."""
    registry = PluginRegistry({"under_test": plugin_cls}, logger=TEST_LOGGER)
    outcome = registry.instantiate_all([entry("poweredge_a", "under_test")])

    with caplog.at_level(logging.INFO, logger=TEST_LOGGER.name):
        validations = await validate_connections(outcome.devices, 3.0, logger=TEST_LOGGER)

    assert len(validations) == 1
    assert validations[0].result.classification == expected
    assert validations[0].ok is (expected is None)
    assert retained_devices(validations) == outcome.devices, "validation is a diagnostic, not a gate"
    assert any("connection validation" in record.getMessage() for record in caplog.records)


async def test_a_plugin_that_ignores_its_timeout_is_cut_off(caplog: pytest.LogCaptureFixture) -> None:
    """Startup has 30 seconds for every device together, so the bound cannot be the plugin's to honour."""
    registry = PluginRegistry({"stalling": StallingValidationPlugin}, logger=TEST_LOGGER)
    outcome = registry.instantiate(entry("poweredge_a", "stalling"))
    assert isinstance(outcome, InstantiatedDevice)

    with caplog.at_level(logging.WARNING, logger=TEST_LOGGER.name):
        validation = await asyncio.wait_for(validate_connection(outcome, 0.01, logger=TEST_LOGGER), timeout=5.0)

    assert validation.result.classification is FailureClassification.TIMEOUT
    assert retained_devices([validation]) == (outcome,)
    record = next(item for item in caplog.records if "connection validation failed" in item.getMessage())
    assert record.levelno == logging.WARNING
    assert record.__dict__["classification"] == "timeout"


async def test_a_per_device_timeout_function_is_honoured(registry: PluginRegistry) -> None:
    """Each device's own resolved timeout reaches its own plugin."""
    outcome = registry.instantiate_all([entry("a"), entry("b")])
    timeouts = {"a": 1.5, "b": 7.0}

    await validate_connections(outcome.devices, lambda device: timeouts[device.device_id], logger=TEST_LOGGER)

    for device in outcome.devices:
        assert device.plugin.validate_calls == [timeouts[device.device_id]]  # type: ignore[attr-defined]


async def test_validation_is_concurrent_across_devices(registry: PluginRegistry) -> None:
    """Requirement 1.3's 30 second startup budget rules out validating 50 devices one after another."""

    class SlowPlugin(RecordingPlugin):
        plugin_id: ClassVar[str] = "slow"

        async def validate_connection(self, timeout_s: float) -> ValidationResult:
            self.validate_calls.append(timeout_s)
            await asyncio.sleep(0.1)
            return ValidationResult.success()

    slow_registry = PluginRegistry({"slow": SlowPlugin}, logger=TEST_LOGGER)
    outcome = slow_registry.instantiate_all([entry(f"d{index}", "slow") for index in range(10)])

    loop = asyncio.get_running_loop()
    started = loop.time()
    validations = await validate_connections(outcome.devices, 5.0, logger=TEST_LOGGER)
    elapsed = loop.time() - started

    assert len(validations) == 10
    assert elapsed < 0.5, "ten 0.1 s validations run concurrently, not in sequence"
