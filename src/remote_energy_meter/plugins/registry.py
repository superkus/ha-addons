"""The Plugin_Registry: discovery, resolution, instantiation, and one-shot connection validation.

Presence in ``remote_energy_meter.plugins`` *is* registration. :func:`discover` walks that package with
:func:`pkgutil.walk_packages` and collects module-level classes that satisfy every element of the ``Plugin_Interface``,
so adding support for a new device family is dropping a module into the package — no core edit, no register call
(Requirements 3.2, 10.3).

Four decisions in here are load-bearing, and each one is the answer to a question the requirements ask twice:

* **What counts as a plugin candidate.** A class defined *in* the walked module (``cls.__module__`` matches), that is
  neither a ``Protocol`` nor abstract, and that satisfies at least one element of
  :data:`~remote_energy_meter.plugins.interface.PLUGIN_INTERFACE_ELEMENTS`. Satisfying one element makes a class a
  candidate; the ones it does not satisfy are what the exclusion record names. That split is what lets a class carrying
  ``plugin_id = 3`` alongside three working elements be reported as missing ``plugin_id``, rather than silently
  registered under a non-identifier (Requirements 3.9, 10.5).

  Two exclusions fall out of the rule rather than being special-cased. ``plugins.interface`` itself defines the
  ``DevicePlugin`` protocol, which provides the two method names but not ``plugin_id`` or ``config_model``; it is
  skipped because it *is* a protocol, so it never reaches the missing-element report. And a support module that defines
  only pydantic schemas or helpers provides no element at all, so it is not a candidate and produces no log noise. A
  module that re-exports another module's plugin class — ``from .plugin import IdracPlugin`` in a package
  ``__init__`` — does not double-register it, because the class's ``__module__`` names where it was defined.

* **Which failures are fatal, and who exits.** A duplicate plugin identifier (Requirement 3.5) and zero discovered
  plugins (Requirement 3.8) are fatal, because both make polling either ambiguous or pointless. The registry *reports*
  them as :class:`FatalRegistryError` and never calls :func:`sys.exit`: the startup orchestration owns exit codes, and a
  library that exits cannot be tested for the condition that made it exit.

* **Which failures degrade one entry.** An unresolved plugin identifier, parameters the plugin's own schema rejects,
  and a constructor that raises all exclude exactly one ``Device_Entry`` and leave the rest of the device list intact
  (Requirements 3.4, 3.6). Sixteen of seventeen devices still get metered when one is misconfigured.

* **What connection validation may and may not do.** It runs exactly once per instance, its result is logged with the
  entry identifier, and the entry is retained *regardless of the result* (Requirement 3.10). A server that is powered
  off when the addon starts still gets its entities and starts reporting when it comes up, so validation is a
  diagnostic, not a gate. It is also bounded by :func:`asyncio.timeout` and shielded from plugin exceptions, because 50
  devices validating sequentially at 10 seconds each would blow the 30 second startup budget of Requirement 1.3.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import pkgutil
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType, ModuleType
from typing import Final, cast

from pydantic import BaseModel, ValidationError

from remote_energy_meter.config.models import DeviceEntry
from remote_energy_meter.config.runtime import EntryRejection, RejectionReason, ResolvedDevice
from remote_energy_meter.diagnostics.logger import DEVICE_ID_FIELD, get_logger
from remote_energy_meter.plugins.interface import (
    PLUGIN_ID_MAX_LENGTH,
    PLUGIN_ID_MIN_LENGTH,
    PLUGIN_INTERFACE_ELEMENTS,
    DevicePlugin,
    FailureClassification,
    ValidationResult,
)

__all__ = [
    "PLUGINS_PACKAGE",
    "ConnectionValidation",
    "DiscoveryResult",
    "ExcludedModule",
    "FatalRegistryError",
    "FatalRegistryReason",
    "InstantiatedDevice",
    "InstantiationOutcome",
    "PluginRegistry",
    "discover",
    "missing_interface_elements",
    "retained_devices",
    "validate_connection",
    "validate_connections",
]

#: The package whose submodules are walked. Its ``__path__`` is extended with :func:`pkgutil.extend_path`, so a plugin
#: shipped by a separate distribution is discovered by the same walk (Requirement 10.3).
PLUGINS_PACKAGE: Final = "remote_energy_meter.plugins"

_LOG: Final = get_logger(__name__)


# --------------------------------------------------------------------------------------------------
# interface element inspection (Requirements 3.9, 10.5)
# --------------------------------------------------------------------------------------------------


def _satisfies_element(candidate: type, element: str) -> bool:
    """Whether ``candidate`` provides ``element`` in a *usable* form.

    "Usable" is checked, not merely "present", because a class carrying ``plugin_id = 3`` or
    ``config_model = dict`` would otherwise be registered and then fail deep inside the parser or the scheduler, where
    the cause is no longer visible. The checks stay deliberately shallow — an identifier that is a bounded string, a
    schema that is a pydantic model, two operations that are callable — since anything deeper (are the operations
    awaitable? does the schema accept a valid entry?) is the conformance suite's job, run by the plugin author with a
    plugin-specific fixture.
    """
    value = getattr(candidate, element, None)
    if value is None:
        return False
    match element:
        case "plugin_id":
            return isinstance(value, str) and PLUGIN_ID_MIN_LENGTH <= len(value) <= PLUGIN_ID_MAX_LENGTH
        case "config_model":
            return isinstance(value, type) and issubclass(value, BaseModel)
        case _:
            return callable(value)


def missing_interface_elements(candidate: type) -> tuple[str, ...]:
    """The ``Plugin_Interface`` elements ``candidate`` does not satisfy, in interface declaration order.

    Empty for a conforming class. Shared with the conformance suite through
    :data:`~remote_energy_meter.plugins.interface.PLUGIN_INTERFACE_ELEMENTS`, so the registry's exclusion record
    (Requirement 3.9) and the suite's failure report (Requirement 10.4) name the same element strings — an author
    reading one and then the other should not have to translate.
    """
    return tuple(element for element in PLUGIN_INTERFACE_ELEMENTS if not _satisfies_element(candidate, element))


def _is_candidate(value: object, module_name: str) -> bool:
    """Whether ``value`` is a class that claims, however partially, to be a plugin.

    Claiming is *satisfying* at least one interface element, not merely carrying one of the names. Carrying the name
    was the first rule tried and it was too loose: a dataclass with a ``plugin_id`` field carries the name as a slot
    descriptor, and a class with a ``plugin_id`` property carries it as a property object, so ordinary support types in
    this very package were reported as incomplete plugins on every startup. A log full of records about classes that
    were never plugins is a log nobody reads, and a report nobody reads does not help the author it was written for.

    The trade is narrow and explicit: a class that carries all four names but satisfies none of them — an identifier
    that is not a string, a schema that is not a model, two operations that are not callable — is skipped silently
    rather than reported. It is indistinguishable from a support type by inspection, and the conformance suite is where
    a plugin author who wrote one finds out (Requirement 10.4).
    """
    if not isinstance(value, type):
        return False
    if value.__module__ != module_name:
        return False
    if getattr(value, "_is_protocol", False) or inspect.isabstract(value):
        return False
    return any(_satisfies_element(value, element) for element in PLUGIN_INTERFACE_ELEMENTS)


def _candidate_classes(module: ModuleType) -> list[type]:
    """The plugin candidates defined in ``module``, in definition order and each one once."""
    seen: set[int] = set()
    candidates: list[type] = []
    for value in vars(module).values():
        if not _is_candidate(value, module.__name__) or id(value) in seen:
            continue
        seen.add(id(value))
        candidates.append(cast("type", value))
    return candidates


# --------------------------------------------------------------------------------------------------
# discovery results
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExcludedModule:
    """One module or candidate class kept out of the discovered set, with everything needed to fix it.

    Attributes:
        module_name: The fully qualified module name (Requirements 3.9, 10.5).
        class_name: The candidate class that fell short, or ``None`` when the module could not be imported at all and
            so has no classes to name.
        missing_elements: Every unsatisfied ``Plugin_Interface`` element, not just the first. A plugin author fixing
            one omission per startup would need four startups.
        detail: Why the module could not be imported, when that is the reason. ``None`` for an incomplete class.
    """

    module_name: str
    class_name: str | None
    missing_elements: tuple[str, ...]
    detail: str | None = None


class FatalRegistryReason(StrEnum):
    """The two discovery outcomes that must stop the process before the Poll_Scheduler starts."""

    DUPLICATE_PLUGIN_ID = "duplicate_plugin_id"
    """Two discovered plugins declared the same identifier (Requirement 3.5).

    Fatal while a duplicate *device* identifier is not, and the asymmetry is deliberate: a duplicate device identifier
    has an unambiguous resolution — first occurrence wins — whereas a duplicate plugin identifier makes every entry
    naming it ambiguous, and guessing would silently meter the wrong device.
    """

    NO_PLUGINS_DISCOVERED = "no_plugins_discovered"
    """The walk produced no usable plugin, so no device could ever be read (Requirement 3.8)."""


@dataclass(frozen=True, slots=True)
class FatalRegistryError:
    """A fatal discovery outcome, reported rather than acted on.

    Returned to the startup orchestration, which turns it into a non-zero exit and the error record of Requirement 1.4.
    The registry does not exit by itself: a component that calls :func:`sys.exit` cannot be tested for the condition
    that made it exit, and the exit code belongs to one place in the process.

    Attributes:
        reason: Which fatal condition occurred.
        plugin_id: The duplicated identifier, for :data:`FatalRegistryReason.DUPLICATE_PLUGIN_ID`.
        owners: The ``module.ClassName`` locations that declared the duplicated identifier, in discovery order.
    """

    reason: FatalRegistryReason
    plugin_id: str | None = None
    owners: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    """What the walk found: usable plugins, excluded modules, and any fatal condition.

    Attributes:
        plugins: Discovered plugin classes by identifier, in discovery order. When a duplicate identifier made the
            result fatal, the first declaration is kept here so diagnostics can still name it; nothing polls, because
            the caller exits on :attr:`fatal`.
        excluded: One entry per module or candidate class kept out (Requirements 3.9, 10.5).
        fatal: The fatal condition, or ``None``.
    """

    plugins: Mapping[str, type[DevicePlugin]]
    excluded: tuple[ExcludedModule, ...] = ()
    fatal: FatalRegistryError | None = None

    @property
    def ok(self) -> bool:
        """True when the process may proceed to start the Poll_Scheduler."""
        return self.fatal is None

    @property
    def plugin_ids(self) -> tuple[str, ...]:
        """Every discovered plugin identifier, in discovery order."""
        return tuple(self.plugins)


# --------------------------------------------------------------------------------------------------
# discovery (Requirements 3.2, 3.5, 3.8, 3.9, 10.3, 10.5)
# --------------------------------------------------------------------------------------------------


def _resolve_package(package: ModuleType | str | None) -> ModuleType:
    """Import ``package`` if needed and confirm it is walkable."""
    module = importlib.import_module(package) if isinstance(package, str) else package
    if module is None:
        module = importlib.import_module(PLUGINS_PACKAGE)
    if not hasattr(module, "__path__"):
        raise ValueError(f"{module.__name__} is not a package and cannot be walked for plugins")
    return module


def _iter_module_names(package: ModuleType, log: logging.Logger, excluded: list[ExcludedModule]) -> Iterator[str]:
    """Every submodule name under ``package``, recursing into subpackages.

    Recursive rather than flat because a family's plugin naturally lives at ``plugins/<family>/plugin.py`` beside its
    schema; a flat walk would import the family package and find nothing in it.
    """

    def _on_error(name: str) -> None:
        log.error("plugin package could not be imported during discovery: %s", name, extra={"plugin_module": name})
        excluded.append(
            ExcludedModule(
                module_name=name,
                class_name=None,
                missing_elements=PLUGIN_INTERFACE_ELEMENTS,
                detail="package import failed",
            )
        )

    yield from (info.name for info in pkgutil.walk_packages(package.__path__, f"{package.__name__}.", _on_error))


def _import_plugin_module(module_name: str, log: logging.Logger, excluded: list[ExcludedModule]) -> ModuleType | None:
    """Import one candidate module, reporting rather than propagating a failure.

    A third-party plugin module that raises on import — a missing transitive dependency, a syntax error under a
    different Python version — excludes itself and nothing else. The alternative, letting the exception escape the
    walk, would let one broken module stop every other family from being discovered.
    """
    try:
        return importlib.import_module(module_name)
    # A plugin module may fail to import for any reason at all, including ones this process cannot anticipate.
    except Exception as exc:
        log.exception(
            "plugin module could not be imported during discovery: %s",
            module_name,
            extra={"plugin_module": module_name},
        )
        excluded.append(
            ExcludedModule(
                module_name=module_name,
                class_name=None,
                missing_elements=PLUGIN_INTERFACE_ELEMENTS,
                detail=f"{type(exc).__name__}: {exc}",
            )
        )
        return None


def discover(package: ModuleType | str | None = None, *, logger: logging.Logger | None = None) -> DiscoveryResult:
    """Walk ``package`` and collect every conforming plugin class.

    Emits one info record per discovered plugin naming its identifier, followed by one record naming the total count
    (Requirement 3.2); one error record per excluded module naming the module and *every* unsatisfied element
    (Requirements 3.9, 10.5); and one error record per duplicated identifier (Requirement 3.5).

    :param package: the package to walk, or ``None`` for :data:`PLUGINS_PACKAGE`.
    :param logger: where records go; the module logger by default.
    :returns: a :class:`DiscoveryResult` whose :attr:`~DiscoveryResult.fatal` the caller turns into a non-zero exit.
    """
    log = logger if logger is not None else _LOG
    walked = _resolve_package(package)

    plugins: dict[str, type[DevicePlugin]] = {}
    owners: dict[str, str] = {}
    excluded: list[ExcludedModule] = []
    duplicates: list[FatalRegistryError] = []

    for module_name in _iter_module_names(walked, log, excluded):
        module = _import_plugin_module(module_name, log, excluded)
        if module is None:
            continue
        for candidate in _candidate_classes(module):
            location = f"{module_name}.{candidate.__qualname__}"
            missing = missing_interface_elements(candidate)
            if missing:
                log.error(
                    "plugin module %s excluded: %s does not satisfy %s",
                    module_name,
                    candidate.__qualname__,
                    ", ".join(missing),
                    extra={"plugin_module": module_name, "missing_elements": list(missing)},
                )
                excluded.append(ExcludedModule(module_name, candidate.__qualname__, missing))
                continue

            plugin_cls = cast("type[DevicePlugin]", candidate)
            plugin_id = plugin_cls.plugin_id
            if plugin_id in plugins:
                claimants = (owners[plugin_id], location)
                log.error(
                    "duplicate plugin identifier %s declared by %s",
                    plugin_id,
                    " and ".join(claimants),
                    extra={"plugin_id": plugin_id, "owners": list(claimants)},
                )
                duplicates.append(FatalRegistryError(FatalRegistryReason.DUPLICATE_PLUGIN_ID, plugin_id, claimants))
                continue

            plugins[plugin_id] = plugin_cls
            owners[plugin_id] = location
            log.info(
                "device plugin discovered: %s",
                plugin_id,
                extra={"plugin_id": plugin_id, "plugin_module": module_name},
            )

    log.info(
        "device plugin discovery complete: %d plugin(s)",
        len(plugins),
        extra={"plugin_count": len(plugins), "plugin_ids": list(plugins)},
    )

    fatal: FatalRegistryError | None = None
    if duplicates:
        fatal = duplicates[0]
    elif not plugins:
        log.error("no device plugin was discovered in %s", walked.__name__, extra={"plugin_module": walked.__name__})
        fatal = FatalRegistryError(FatalRegistryReason.NO_PLUGINS_DISCOVERED)

    return DiscoveryResult(plugins=MappingProxyType(plugins), excluded=tuple(excluded), fatal=fatal)


# --------------------------------------------------------------------------------------------------
# resolution and instantiation (Requirements 3.3, 3.4, 3.6)
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InstantiatedDevice:
    """One accepted ``Device_Entry`` and the plugin instance created for it, and for nothing else.

    Deliberately *not* a :class:`~remote_energy_meter.config.runtime.ResolvedDevice`: the poll interval and request
    timeout come from the scheduler's resolution rules, which the registry has no business restating.
    :meth:`with_schedule` is the one-line bridge once the caller has resolved them.
    """

    entry: DeviceEntry
    plugin: DevicePlugin

    @property
    def device_id(self) -> str:
        """The device identifier every log record and MQTT topic for this device derives from."""
        return self.entry.device_id

    @property
    def plugin_id(self) -> str:
        """The plugin identifier named by the entry."""
        return self.entry.plugin

    def with_schedule(self, interval_s: int, timeout_s: float) -> ResolvedDevice:
        """Pair this instance with its resolved poll interval and request timeout."""
        return ResolvedDevice(entry=self.entry, plugin=self.plugin, interval_s=interval_s, timeout_s=timeout_s)


@dataclass(frozen=True, slots=True)
class InstantiationOutcome:
    """The result of instantiating a device list: the usable devices, and one rejection per excluded entry."""

    devices: tuple[InstantiatedDevice, ...] = ()
    rejections: tuple[EntryRejection, ...] = ()


def _first_failing_field(model_cls: type[BaseModel], error: ValidationError) -> str | None:
    """The failing field that comes first in ``model_cls``'s declaration order.

    Requirement 2.3 reports "the first failing field", so the choice has to be deterministic. Sorting by declaration
    index rather than trusting the validator's own error order is what makes the record reproducible, and therefore
    testable, across pydantic versions.
    """
    order = {name: index for index, name in enumerate(model_cls.model_fields)}
    best: tuple[int, str] | None = None
    for entry in error.errors():
        location = entry.get("loc") or ()
        if not location:
            continue
        name = str(location[0])
        rank = order.get(name, len(order))
        if best is None or rank < best[0]:
            best = (rank, name)
    return None if best is None else best[1]


class PluginRegistry:
    """Resolves a ``Device_Entry`` to a plugin class, and creates one instance per entry.

    Holds the discovered mapping and nothing else: no device state, no connection state, no lifecycle. Resolution is a
    byte-for-byte, case-sensitive lookup (Requirement 3.3) — ``iDRAC`` does not resolve to ``idrac``, because a
    case-folded match would make two plugins named ``foo`` and ``Foo`` collide only sometimes, depending on which the
    walk found first.
    """

    __slots__ = ("_log", "_plugins")

    def __init__(
        self,
        plugins: Mapping[str, type[DevicePlugin]],
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._plugins: Mapping[str, type[DevicePlugin]] = MappingProxyType(dict(plugins))
        self._log = logger if logger is not None else _LOG

    @classmethod
    def from_discovery(cls, result: DiscoveryResult, *, logger: logging.Logger | None = None) -> PluginRegistry:
        """Build a registry over a discovery result.

        The caller checks :attr:`DiscoveryResult.fatal` first; this does not, because refusing to build would hide the
        discovered identifiers that the fatal record needs to name.
        """
        return cls(result.plugins, logger=logger)

    @property
    def plugins(self) -> Mapping[str, type[DevicePlugin]]:
        """The discovered plugin classes by identifier. Read-only."""
        return self._plugins

    @property
    def plugin_ids(self) -> tuple[str, ...]:
        """Every discovered plugin identifier, in discovery order."""
        return tuple(self._plugins)

    def resolve(self, plugin_id: str) -> type[DevicePlugin] | None:
        """The plugin class whose identifier is byte-equal to ``plugin_id``, or ``None`` (Requirement 3.3)."""
        return self._plugins.get(plugin_id)

    def resolve_entry(self, entry: DeviceEntry) -> type[DevicePlugin] | EntryRejection:
        """Resolve one entry, rejecting it with one error record when its plugin was never discovered.

        The record names the entry identifier, the unresolved plugin identifier, *and* every discovered identifier
        (Requirement 3.4), because the usual cause is a typo or a case difference and the answer is then visible in the
        same line as the question.
        """
        plugin_cls = self.resolve(entry.plugin)
        if plugin_cls is not None:
            return plugin_cls
        discovered = self.plugin_ids
        self._log.error(
            "device entry names an unknown plugin %s; discovered plugins: %s",
            entry.plugin,
            ", ".join(discovered) if discovered else "(none)",
            extra={
                DEVICE_ID_FIELD: entry.device_id,
                "plugin_id": entry.plugin,
                "discovered_plugin_ids": list(discovered),
            },
        )
        return EntryRejection(entry.device_id, RejectionReason.UNKNOWN_PLUGIN)

    def instantiate(
        self, entry: DeviceEntry, *, params: BaseModel | None = None
    ) -> InstantiatedDevice | EntryRejection:
        """Create the one plugin instance that belongs to ``entry`` (Requirement 3.6).

        One instance per entry means one transport per device: its own connection pool and its own TLS context, so a
        hung socket on one device cannot occupy another's connection slot, and an authentication failure on one leaves
        every other device's connection state untouched.

        :param params: an instance of the plugin's own ``config_model`` when the caller has already validated it;
            otherwise the entry's raw parameters are validated here against that schema.
        :returns: the instance, or an :class:`EntryRejection` excluding this entry and this entry only.
        """
        resolved = self.resolve_entry(entry)
        if isinstance(resolved, EntryRejection):
            return resolved

        model = params
        if model is None:
            try:
                model = resolved.config_model.model_validate(entry.params)
            except ValidationError as error:
                field_name = _first_failing_field(resolved.config_model, error)
                self._log.error(
                    "device entry rejected by the %s configuration schema: first failing field %s",
                    entry.plugin,
                    field_name if field_name is not None else "(unknown)",
                    extra={DEVICE_ID_FIELD: entry.device_id, "plugin_id": entry.plugin, "field": field_name},
                )
                return EntryRejection(entry.device_id, RejectionReason.SCHEMA_VIOLATION, field_name)

        try:
            instance = resolved(entry.device_id, model)
        # A third-party constructor may raise anything; one entry is excluded, the device list survives.
        except Exception as exc:
            self._log.error(
                "plugin %s refused to accept device entry (%s: %s)",
                entry.plugin,
                type(exc).__name__,
                exc,
                extra={DEVICE_ID_FIELD: entry.device_id, "plugin_id": entry.plugin},
            )
            return EntryRejection(entry.device_id, RejectionReason.SCHEMA_VIOLATION)

        return InstantiatedDevice(entry=entry, plugin=instance)

    def instantiate_all(self, entries: Iterable[DeviceEntry]) -> InstantiationOutcome:
        """Instantiate a whole device list, keeping order and reporting one rejection per excluded entry.

        Never raises on account of one entry: an unresolvable, unschematic, or constructor-hostile entry is excluded
        and the remaining entries are instantiated (Requirements 3.4, 3.6).
        """
        devices: list[InstantiatedDevice] = []
        rejections: list[EntryRejection] = []
        for entry in entries:
            outcome = self.instantiate(entry)
            if isinstance(outcome, EntryRejection):
                rejections.append(outcome)
            else:
                devices.append(outcome)
        return InstantiationOutcome(devices=tuple(devices), rejections=tuple(rejections))


# --------------------------------------------------------------------------------------------------
# startup connection validation (Requirement 3.10)
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConnectionValidation:
    """One device's startup validation outcome.

    The device is retained for polling whichever way this went, so this type carries a *result*, never a verdict on
    whether to keep the device.
    """

    device: InstantiatedDevice
    result: ValidationResult
    detail: str | None = field(default=None, compare=False)

    @property
    def device_id(self) -> str:
        """The identifier of the validated device."""
        return self.device.device_id

    @property
    def ok(self) -> bool:
        """Whether the device answered in a shape a reading could be taken from."""
        return self.result.ok


async def _invoke_validation(device: InstantiatedDevice, timeout_s: float) -> tuple[ValidationResult, str | None]:
    """Call one plugin's validation operation, converting every failure mode into a classified result.

    Bounded by :func:`asyncio.timeout` rather than trusting the plugin to honour the timeout it was handed: a plugin
    that blocks forever would otherwise hold up startup indefinitely, and Requirement 1.3 gives startup 30 seconds for
    every device together. ``CancelledError`` is re-raised untouched so shutdown during startup stays prompt.
    """
    try:
        async with asyncio.timeout(timeout_s):
            result = await device.plugin.validate_connection(timeout_s)
    except TimeoutError:
        return ValidationResult.failure(FailureClassification.TIMEOUT), f"exceeded {timeout_s:g}s"
    except asyncio.CancelledError:
        raise
    # A plugin defect degrades one device, never the process.
    except Exception as exc:
        return ValidationResult.failure(FailureClassification.PLUGIN), f"{type(exc).__name__}: {exc}"
    # A third-party plugin can return whatever it likes whatever its annotation says, so the type is checked rather
    # than trusted: an unannounced ``None`` here would otherwise surface as an attribute error inside the log call.
    returned: object = result
    if not isinstance(returned, ValidationResult):
        return ValidationResult.failure(FailureClassification.PLUGIN), f"returned {type(returned).__name__}"
    return returned, None


async def validate_connection(
    device: InstantiatedDevice, timeout_s: float, *, logger: logging.Logger | None = None
) -> ConnectionValidation:
    """Validate one device once, log the outcome, and retain the device either way (Requirement 3.10).

    Exactly one record per device, naming the entry identifier and the returned result. A failure is logged at warning
    because it is worth noticing and is *not* an error: a powered-off server at addon start is an ordinary state that
    resolves itself when the server comes up.
    """
    log = logger if logger is not None else _LOG
    result, detail = await _invoke_validation(device, timeout_s)

    extra: dict[str, object] = {DEVICE_ID_FIELD: device.device_id, "plugin_id": device.plugin_id}
    if detail is not None:
        extra["detail"] = detail
    if result.ok:
        log.info("connection validation succeeded", extra=extra)
    else:
        classification = result.classification
        extra["classification"] = classification.value if classification is not None else None
        log.warning(
            "connection validation failed: %s (device retained for polling)",
            classification.value if classification is not None else "unknown",
            extra=extra,
        )
    return ConnectionValidation(device=device, result=result, detail=detail)


async def validate_connections(
    devices: Sequence[InstantiatedDevice],
    timeout_for: Callable[[InstantiatedDevice], float] | float,
    *,
    logger: logging.Logger | None = None,
) -> tuple[ConnectionValidation, ...]:
    """Validate every device concurrently, exactly once per plugin instance.

    Concurrent because sequential validation of 50 devices at a 10 second timeout each cannot fit the 30 second startup
    budget of Requirement 1.3. Deduplicated by instance identity, so a device list that names the same instance twice
    still calls its validation operation once and reuses the result — "exactly once per instance" holds regardless of
    what the caller passes.

    :param timeout_for: the per-attempt timeout, either a constant or a function of the device.
    :returns: one validation per device in ``devices``, in order. Every device is present whatever its outcome; the
        caller retains them all (Requirement 3.10). :func:`retained_devices` spells that out.
    """
    resolve_timeout = timeout_for if callable(timeout_for) else (lambda _device: float(timeout_for))

    unique: list[InstantiatedDevice] = []
    seen: set[int] = set()
    for device in devices:
        if id(device.plugin) in seen:
            continue
        seen.add(id(device.plugin))
        unique.append(device)

    validations = await asyncio.gather(
        *(validate_connection(device, resolve_timeout(device), logger=logger) for device in unique)
    )
    by_instance = {id(validation.device.plugin): validation for validation in validations}
    return tuple(
        ConnectionValidation(
            device=device,
            result=by_instance[id(device.plugin)].result,
            detail=by_instance[id(device.plugin)].detail,
        )
        for device in devices
    )


def retained_devices(validations: Sequence[ConnectionValidation]) -> tuple[InstantiatedDevice, ...]:
    """Every validated device, in order, regardless of its validation result (Requirement 3.10).

    A one-line function that exists to make the requirement legible at the call site: startup validation is a
    diagnostic, not a filter, and a reader of the startup code should not have to infer that from the absence of a
    filter.
    """
    return tuple(validation.device for validation in validations)
