# Writing a device plugin

A plugin teaches the Remote Energy Meter how to read instantaneous power from one family of network-attached devices.
It is the only device-specific code in the system: the metering core imports
`remote_energy_meter.plugins.interface` and nothing else, so scheduling, retries, energy integration, MQTT discovery,
logging, and credential redaction are already done for you and stay done.

Adding a family is writing one module and dropping it into `remote_energy_meter.plugins`. There is no core edit and no
registration call.

## The contract in one page

```python
from datetime import UTC, datetime
from typing import ClassVar

from pydantic import BaseModel, Field, SecretStr

from remote_energy_meter.config.models import CREDENTIAL_MARKER
from remote_energy_meter.plugins.interface import (
    FailureClassification,
    PowerReading,
    ReadFailure,
    ReadResult,
    ValidationResult,
)


class AcmeParams(BaseModel):
    host: str = Field(min_length=1)
    username: SecretStr = Field(json_schema_extra={CREDENTIAL_MARKER: True})
    password: SecretStr = Field(json_schema_extra={CREDENTIAL_MARKER: True})
    verify_tls: bool = True


class AcmePlugin:
    plugin_id: ClassVar[str] = "acme"
    config_model: ClassVar[type[BaseModel]] = AcmeParams

    def __init__(self, device_id: str, params: BaseModel) -> None:
        assert isinstance(params, AcmeParams)
        self._device_id = device_id
        self._params = params

    async def validate_connection(self, timeout_s: float) -> ValidationResult:
        ...  # one probe, one result, no raising

    async def read_power(self, timeout_s: float) -> ReadResult:
        ...  # one request, then a PowerReading or a ReadFailure
```

Three boundaries hold for every plugin, and everything else in this document follows from them.

**A plugin never sleeps, never retries, and never chooses its own timeout.** It receives `timeout_s` and returns one
result. Retry policy, backoff, and the poll schedule belong to the `Poll_Scheduler`, which retries a failed read up to
twice (waiting 1 s then 2 s) for every classification except `authentication`. A plugin that implemented its own retry
loop would multiply those attempts and blow the cycle budget.

**Failures are returned, never raised.** Every operation returns a value that says what went wrong. An exception that
escapes anyway is caught at the scheduler boundary and converted into the `plugin` classification, so a defective
plugin degrades one device rather than the process — but a returned failure carries a specific classification and a
detail string, and an exception does not.

**One instance serves one device, for the life of the process.** The registry constructs your class once per accepted
`Device_Entry` with `(device_id, params)`. Per-device transport state — a connection pool, a TLS context — belongs on
the instance, so a hung socket on one device cannot occupy another's connection slot.

## The Plugin_Interface

Four elements, named in `PLUGIN_INTERFACE_ELEMENTS`. A class that provides all four is a plugin; a class that provides
some of them is reported as incomplete, naming each element it is missing.

### plugin_id

The identifier a `Device_Entry` names in its `plugin` field.

- **Type:** a class-level `str`, 1 to 64 characters (`PLUGIN_ID_MIN_LENGTH`, `PLUGIN_ID_MAX_LENGTH`).
- **Matching:** byte-for-byte and case-sensitive. `iDRAC` does not resolve to `idrac`. Case folding was rejected because
  it would make two plugins named `foo` and `Foo` collide only sometimes, depending on which one discovery found first.
- **Uniqueness:** globally unique across all discovered plugins. Two plugins declaring the same identifier is fatal at
  startup — the process logs the identifier and both declaring locations, then exits non-zero, because every entry
  naming that identifier would otherwise be ambiguous.
- **Failure to satisfy:** a non-string value, an empty string, or more than 64 characters counts as a missing
  `plugin_id`, which excludes the class from discovery.

### config_model

The published configuration schema for this family's connection parameters. The `params` mapping of a `Device_Entry` is
validated against it.

- **Type:** a class-level `type[BaseModel]` — a pydantic model *class*, not an instance and not a dict.
- **Inputs:** the raw `params` mapping from the addon configuration, after `!secret` references have been resolved.
- **Outputs:** a validated model instance, passed to your constructor. A `ValidationError` excludes that one entry with
  a log record naming the first failing field in declaration order, and the rest of the device list is unaffected.
- **Credentials:** declare every credential field as `SecretStr` carrying the `credential` marker
  (`Field(json_schema_extra={CREDENTIAL_MARKER: True})`). That marker is the whole mechanism by which the core
  identifies credentials without knowing anything about your device family: it drives log and payload redaction, and
  the parser's empty and oversized credential checks. Any other marker leaves your passwords unredacted in the log
  stream.
- **Ranges and defaults:** put them in the schema (`Field(ge=..., le=..., max_length=...)`). The parser reports a
  violation naming the field, which is a better diagnostic than a failure deep inside your read path.
- **Extra keys:** ignored by pydantic's default, and the shipped `IdracParams` keeps that behaviour, because an unknown
  key has no declaration index and so cannot be ordered in a "first failing field" report.
- **Failure to satisfy:** a value that is not a `BaseModel` subclass counts as a missing `config_model`.

Anything that is not a connection parameter does not belong here. Poll intervals live in the addon configuration, and
retry counts and delays are the scheduler's.

### validate_connection

```python
async def validate_connection(self, timeout_s: float) -> ValidationResult
```

A single reachability probe, run **exactly once per instance at startup**.

- **Inputs:** `timeout_s`, the number of seconds the whole probe may take.
- **Outputs:** `ValidationResult.success()` when the device answered in a shape a successful read could be taken from,
  or `ValidationResult.failure(classification)` with one of the seven classifications below. `ok` and `classification`
  are consistent by construction: a success carries no classification and a failure must carry one.
- **Retention:** the result is logged with the device identifier and **never gates retention**. A server that is
  powered off when the addon starts still gets its entities and starts reporting when it comes up, so this is a
  diagnostic, not an admission test.
- **Belt and braces:** the registry wraps the call in its own timeout and catches everything, because 50 devices
  validating sequentially must still fit the startup budget. A call that exceeds `timeout_s` is recorded as `timeout`;
  an exception, or a return value that is not a `ValidationResult`, is recorded as `plugin`.

Succeed only when a read would succeed. Reporting "reachable" for a device whose power resource is absent moves the
failure to the first poll cycle, where nobody is watching the log.

### read_power

```python
async def read_power(self, timeout_s: float) -> ReadResult
```

The operation that produces measurements. `ReadResult` is `PowerReading | ReadFailure`.

- **Inputs:** `timeout_s`, the number of seconds this one attempt may take. It comes from the scheduler's resolution
  rules, which is why you must not substitute your own.
- **Requests:** **exactly one request per call.** The scheduler calls `read_power` again when it wants another attempt.
- **Outputs:** a `PowerReading` (all three fields below), or a `ReadFailure` carrying `device_id`, one
  `classification`, and a `detail` string.
- **`detail`:** short, human-readable context that tells an administrator where to look — the requested resource path,
  or the host address. Treat it as untrusted text: it goes through the redaction filter, but do not put a credential in
  it deliberately.
- **Never:** sleeps, retries, backs off, or reschedules.

A failure must be *classified at the point of detection*. Everything downstream — whether to retry, whether to
accumulate energy, what to log, what availability to publish — reads the classification instead of re-inspecting the
cause.

## The Power_Reading contract

`PowerReading` is a frozen dataclass with three fields and no optional ones.

### device_id

The identifier of the `Device_Entry` this reading belongs to — the same string your constructor received, unmodified.
Every log record, MQTT topic, and entity unique identifier for the device derives from it, so a reading that carries a
different value would be attributed to the wrong device or dropped.

### watts

Instantaneous power in watts, as a `float`.

Apply your family's own plausibility range before returning: the iDRAC plugin accepts 0 to 10000 W for a chassis and
returns `invalid_measurement` outside it. The publisher applies a wider guard (0 to 100000 W) that must hold for any
plugin, including one that meters a whole rack; that guard is a backstop, not your range. Convert to watts inside the
plugin — a device that reports milliwatts or kilowatts is your problem, not the core's.

### measured_at

A timezone-aware UTC `datetime`, taken **when the response was received**, not when the request was issued.

```python
measured_at = datetime.now(UTC)   # after the response is in hand
```

Energy integration differences consecutive timestamps, so a naive value, a local-time value, or a send-time value
silently distorts every kWh total downstream. This is the single easiest field to get wrong and the hardest to notice.

## Failure classifications

`FailureClassification` has exactly seven values. Pick the most specific one that fits; the diagnostics layer collapses
them onto four logged values (`authentication`, `timeout`, `tls_validation`, and `read` for the rest), and the
scheduler keys its retry decision off the internal value.

### authentication

The device rejected the credentials — HTTP 401 or 403 for an HTTP-based family. **Not retried within a cycle:**
retrying a wrong credential achieves nothing and walks toward an account lockout. This is the only classification the
scheduler treats as final.

### timeout

The attempt exceeded its timeout and was abandoned. Return this when you bound the operation yourself; if you let the
call run long instead, the scheduler's own timeout produces the same classification, only later.

### tls_validation

Certificate chain validation failed while verification was enabled. Distinct from `communication` on purpose: the fix
is a certificate or a `verify_tls` decision, not a network investigation, and the log record keeps that distinction.

### communication

The connection could not be established, or the device answered with an unsuccessful status other than 401 and 403.
Refused connections, unresolvable hosts, resets, and 5xx responses all land here. Name the host in `detail`.

### response_format

The response was readable but carried no power value — a missing member, an unexpected shape, a body that is not the
document you asked for. Name the requested resource path in `detail`.

### invalid_measurement

A power value was present but non-numeric, or outside your family's plausible range. Keep this separate from
`response_format`: it says the device answered the right question with an answer that cannot be true.

### plugin

The plugin raised an unhandled exception. **You never return this** — it is assigned at the scheduler and registry
boundaries when something escapes your code, including a return value of the wrong type. Seeing it in a log is a
report of a bug in the plugin.

## The discovery mechanism

Presence in `remote_energy_meter.plugins` *is* registration. There is no register function, no entry point, and no
list of plugins anywhere in the core.

At startup the registry walks the package's `__path__` with `pkgutil` — `walk_packages`, the recursive form of
`iter_modules`, so a family that lives at `plugins/<family>/plugin.py` beside its schema is found — imports each
submodule, and collects module-level classes that provide every element of `PLUGIN_INTERFACE_ELEMENTS`. The package
extends its `__path__` with `pkgutil.extend_path`, so a plugin shipped by a separate distribution is discovered by the
same walk.

A class is considered a candidate when it is defined in the walked module (`cls.__module__` matches, so re-exporting a
plugin from a package `__init__` does not register it twice), is neither a `Protocol` nor abstract, and satisfies at
least one interface element. That last clause is what keeps support types out of the report: a module of pydantic
schemas or helpers satisfies nothing, so it is not a candidate and produces no log noise.

What discovery does with what it finds:

| Condition | Outcome |
| --- | --- |
| Class satisfies all four elements | registered under its `plugin_id`, one info record naming the identifier |
| Walk complete | one info record naming the total plugin count |
| Candidate class missing one or more elements | the class is excluded, one error record names the module, the class, and **every** missing element — not just the first, so one startup is enough to fix all of them |
| Module raises on import | that module is excluded with the exception in the record; every other module is still discovered |
| Two plugins declare the same `plugin_id` | fatal: error record naming the identifier and both locations, non-zero exit before polling starts |
| No plugin discovered at all | fatal: error record, non-zero exit |
| A `Device_Entry` names an unknown `plugin_id` | that entry is rejected, with a record naming the entry, the unresolved identifier, and every discovered identifier; the remaining entries are metered |

Nothing here is fatal because of *your* module except a duplicate identifier. A plugin that fails to import, fails to
validate an entry, or raises in its constructor costs one device, and the rest of the device list keeps running.

## Verifying your plugin

The reference conformance suite ships inside the package, so you can run it with the addon installed and this
repository absent:

```python
from remote_energy_meter.plugins.conformance import assert_plugin_conforms

from acme_meter.plugin import AcmePlugin


def test_acme_conforms() -> None:
    assert_plugin_conforms(
        AcmePlugin,
        valid_params={"host": "192.0.2.10", "username": "root", "password": "hunter2"},
        require_reading=False,   # no device on the other end of the socket
    )
```

It checks that `plugin_id` is a non-empty string of at most 64 characters, that `config_model` accepts a valid entry
and rejects one with a required field removed, that `validate_connection` returns a success or a classified failure,
and that `read_power` returns a `PowerReading` carrying the device identifier, watts within 0 to 100000, and a UTC
timestamp. Every check runs before it reports, and the failure message names each unsatisfied interface element using
the same element strings the startup log uses. Pass `require_reading=False` when there is nothing to read from: the
suite then accepts a classified `ReadFailure` and reports the reading-shape check as skipped. Use
`check_conformance(...)` instead of `assert_plugin_conforms(...)` if you want the report rather than an exception, and
the `_async` variants from inside a running event loop.

A plugin that passes the suite works against the unmodified core: nothing in the scheduler, the accumulator, or the
publisher knows a device family exists.

## Checklist

- `plugin_id` is a unique, 1–64 character string, matched exactly as written in the configuration.
- `config_model` is a pydantic model class; credentials are `SecretStr` with the `credential` marker.
- `__init__(device_id, params)` stores state and opens nothing that cannot be reopened.
- `validate_connection` succeeds only when a read would succeed, and returns rather than raises.
- `read_power` issues one request, honours `timeout_s`, and returns a `PowerReading` or a classified `ReadFailure`.
- `measured_at` is `datetime.now(UTC)` at response receipt.
- `watts` is watts, inside a range you decided on and documented.
- No sleeping, no retrying, no scheduling.
- The conformance suite passes.

## Bundled `ipmi` example

`remote_energy_meter.plugins.ipmi` is a second concrete example of the same contract. It publishes a Pydantic schema,
keeps output parsing pure, and injects its command runner for conformance tests. Production execution uses exactly one
`asyncio.create_subprocess_exec` call per operation and never invokes a shell. The password is passed only in the child
environment through `IPMI_PASSWORD` with `ipmitool -E`; argv, result representations, failure details, and logs omit it.
Timeout and cancellation handling terminates the child, escalates to kill when needed, and reaps it before returning.

For legacy IPMI deployments, enable IPMI-over-LAN only on an isolated management network, use a dedicated account with
the minimum sensor-reading privilege the controller permits, and treat cipher suite 3 as a compatibility fallback.
Neither the plugin nor the addon requires DCMI or SNMP.
