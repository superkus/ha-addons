"""The contract every device plugin implements.

Four elements, exactly as Requirement 3.1 enumerates: a plugin identifier, a
configuration schema, a connection validation operation, and a power reading
operation. The metering core never imports a device-specific module; it only ever sees
the names defined here.

Two boundaries are deliberate and load-bearing:

* **A plugin never sleeps, never retries, and never chooses its own timeout.** It
  receives a timeout and returns one result. Retry policy, backoff, and scheduling
  belong to ``Poll_Scheduler`` (Requirement 5.5), which is also where an unhandled
  plugin exception is caught and converted into a :data:`FailureClassification.PLUGIN`
  read failure (Requirement 3.7).
* **Failures are classified once, at the point of detection.** Every downstream
  decision — whether to retry, whether to accumulate, what to log — reads the
  classification rather than re-inspecting the cause. :class:`FailureClassification` is
  an enum so ``mypy --strict`` exhaustiveness checking catches an unmapped
  classification at build time rather than in a log record: the diagnostics layer
  collapses these seven internal values onto the four logged values of Requirement 9.3,
  and that mapping must stay total as the taxonomy grows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import ClassVar, Final, Protocol, runtime_checkable

from pydantic import BaseModel

__all__ = [
    "PLUGIN_ID_MAX_LENGTH",
    "PLUGIN_ID_MIN_LENGTH",
    "PLUGIN_INTERFACE_ELEMENTS",
    "DevicePlugin",
    "FailureClassification",
    "PowerReading",
    "ReadFailure",
    "ReadResult",
    "ValidationResult",
]

#: Bounds on a plugin identifier (Requirement 3.1). Enforced by the registry and by the
#: shippable conformance suite, which report an out-of-bounds identifier as an
#: unsatisfied interface element rather than raising.
PLUGIN_ID_MIN_LENGTH: Final = 1
PLUGIN_ID_MAX_LENGTH: Final = 64

#: The attribute names that make up the Plugin_Interface. Named in one place so the
#: registry (Requirement 3.9) and the conformance suite (Requirement 10.4) report the
#: *same* element names when a module omits one.
PLUGIN_INTERFACE_ELEMENTS: Final[tuple[str, ...]] = (
    "plugin_id",
    "config_model",
    "validate_connection",
    "read_power",
)


class FailureClassification(StrEnum):
    """Why a plugin operation did not produce a reading.

    The scheduler reads this to decide whether to retry, and the diagnostics layer maps
    it onto the four logged classifications of Requirement 9.3.
    """

    #: HTTP 401 or 403 (Requirement 4.6). Never retried within a cycle: retrying a
    #: wrong credential achieves nothing and walks toward an account lockout.
    AUTHENTICATION = "authentication"
    #: The attempt exceeded its timeout and was abandoned (Requirements 4.7, 5.4).
    TIMEOUT = "timeout"
    #: Certificate chain validation failed while verification was enabled (4.9).
    TLS_VALIDATION = "tls_validation"
    #: The connection could not be established, or the device answered with an
    #: unsuccessful status other than 401 and 403 (Requirement 4.12).
    COMMUNICATION = "communication"
    #: The response was readable but carried no power value (Requirement 4.5).
    RESPONSE_FORMAT = "response_format"
    #: A power value was present but non-numeric or outside the plausible range
    #: (Requirement 4.11).
    INVALID_MEASUREMENT = "invalid_measurement"
    #: The plugin raised an unhandled exception; classified at the scheduler boundary so
    #: a buggy plugin degrades one device instead of the process (Requirement 3.7).
    PLUGIN = "plugin"


@dataclass(frozen=True, slots=True)
class PowerReading:
    """One normalized measurement produced by a plugin.

    :param device_id: the identifier of the ``Device_Entry`` this reading belongs to.
    :param watts: instantaneous power in watts. The plugin has already applied its own
        plausibility range (0 to 10000 W for a chassis, Requirement 4.4); the publisher
        applies a wider guard that must hold for any plugin (Requirement 6.14).
    :param measured_at: timezone-aware UTC timestamp taken when the response was
        received, not when the request was issued (Requirement 4.4). Energy integration
        differences these timestamps, so a naive or non-UTC value silently distorts
        every kWh total downstream.
    """

    device_id: str
    watts: float
    measured_at: datetime


@dataclass(frozen=True, slots=True)
class ReadFailure:
    """A classified read failure. Returned, never raised.

    :param device_id: the identifier of the ``Device_Entry`` that failed.
    :param classification: the single reason, driving retry and logging decisions.
    :param detail: human-readable context — the requested resource path or the host
        address, per Requirements 4.5 and 4.12. Treated as untrusted text: the
        redaction filter scrubs credential fragments out of it before it reaches a log
        record (Requirements 8.2, 8.7).
    """

    device_id: str
    classification: FailureClassification
    detail: str


type ReadResult = PowerReading | ReadFailure
"""The outcome of a power reading operation: a reading, or a classified failure."""


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """The outcome of the connection validation operation.

    ``classification`` is set if and only if ``ok`` is false. Prefer the
    :meth:`success` and :meth:`failure` constructors, which cannot express the
    inconsistent combinations.

    The result is logged with the entry identifier but never gates retention: a device
    that is powered off at addon start still gets its entities and starts reporting
    when it comes up (Requirement 3.10).
    """

    ok: bool
    classification: FailureClassification | None = None

    def __post_init__(self) -> None:
        if self.ok and self.classification is not None:
            raise ValueError("a successful ValidationResult carries no classification")
        if not self.ok and self.classification is None:
            raise ValueError("a failed ValidationResult must carry a classification")

    @classmethod
    def success(cls) -> ValidationResult:
        """The device answered in a shape a successful read would accept."""
        return cls(ok=True)

    @classmethod
    def failure(cls, classification: FailureClassification) -> ValidationResult:
        """The device did not answer usably, for the given reason."""
        return cls(ok=False, classification=classification)


@runtime_checkable
class DevicePlugin(Protocol):
    """What a device family plugin must provide.

    A module is registered by being present in ``remote_energy_meter.plugins``; nothing
    calls a register function. The registry collects module-level classes that provide
    every name in :data:`PLUGIN_INTERFACE_ELEMENTS` and reports the missing ones for a
    module that provides only some (Requirements 3.9, 10.5).

    ``@runtime_checkable`` supports ``isinstance`` against an *instance*. It does not
    support ``issubclass`` against a class, because this protocol carries non-method
    members; the registry inspects the element names directly, which it has to do
    anyway in order to name each missing element.
    """

    #: Identifier matched against ``Device_Entry.plugin``, byte-for-byte and
    #: case-sensitively (Requirement 3.3). Between :data:`PLUGIN_ID_MIN_LENGTH` and
    #: :data:`PLUGIN_ID_MAX_LENGTH` characters.
    plugin_id: ClassVar[str]

    #: The published configuration schema for this family's connection parameters. A
    #: pydantic model, so the parser gets field names, types, required/optional status,
    #: and ranges for free (Requirements 2.2, 3.1). A credential field is declared as
    #: ``SecretStr`` carrying the ``credential`` marker, which is how the core
    #: identifies credentials without knowing anything about the family
    #: (Requirement 8.1).
    config_model: ClassVar[type[BaseModel]]

    def __init__(self, device_id: str, params: BaseModel) -> None:
        """Bind one instance to one ``Device_Entry``.

        The registry creates exactly one instance per accepted entry (Requirement 3.6),
        so per-device transport state — connection pool, TLS context — is not shared and
        a hung socket on one device cannot occupy another's connection slot.

        ``params`` is an instance of this plugin's own :attr:`config_model`.
        """

    async def validate_connection(self, timeout_s: float) -> ValidationResult:
        """Check reachability once, within ``timeout_s`` seconds.

        Invoked exactly once per instance at startup. Returns a result rather than
        raising, and succeeds only when the device answered in a shape from which a
        reading could be taken (Requirements 3.10, 4.13).
        """
        ...

    async def read_power(self, timeout_s: float) -> ReadResult:
        """Take one power reading, within ``timeout_s`` seconds.

        Issues exactly one request per call (Requirement 4.3) and returns either a
        :class:`PowerReading` or a classified :class:`ReadFailure`. Does not sleep, does
        not retry, and does not schedule: that is the scheduler's job (Requirement 5.5).
        An exception that escapes anyway is caught at the scheduler boundary and
        converted to a :data:`FailureClassification.PLUGIN` failure (Requirement 3.7).
        """
        ...
