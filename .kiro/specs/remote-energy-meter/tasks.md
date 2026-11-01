# Implementation Plan: Remote Energy Meter

## Overview

Implementation proceeds outward from pure data to I/O, so the project is buildable and testable at every step:

1. **Scaffolding** — Python project, tooling, addon manifest, container packaging.
2. **Contracts** — plugin interface, configuration models, runtime and persistence models.
3. **Pure core** — redaction and logging, configuration parse/print, plugin registry, energy integration, MQTT topic
   and payload construction. Each of these is a function over plain data and carries most of the correctness properties.
4. **I/O shell** — iDRAC Redfish client, poll scheduler, MQTT publisher.
5. **Wiring** — startup/shutdown orchestration and the process entry point.
6. **Integration, packaging, documentation** — fake Redfish server, local broker fixture, container checks, plugin
   authoring docs, CI.

Language: Python 3.12 with asyncio, aiohttp, `aiomqtt>=2.3,<3`, pydantic v2, pytest, Hypothesis, ruff, `mypy --strict`,
per the design's Technology Stack.

Every property test is tagged with the convention from the design's Testing Strategy:

```python
# Feature: remote-energy-meter, Property N: <property statement>
```

## Tasks

- [x] 0. Prepare the local test environment
  - [x] 0.1 Prepare the local Python 3.12 environment
    - Create the virtual environment from an explicit Python 3.12 interpreter, matching the design's technology
      stack and CI, rather than whatever `python3` the machine defaults to
    - Install the package plus the dev dependencies declared in `pyproject.toml` (editable install)
    - Confirm `.venv/bin/python --version` reports 3.12.x and that `pytest -m "not integration"` collects and runs
    - _Design: Testing Strategy_

- [x] 1. Scaffold the addon repository and Python project
  - [x] 1.1 Create the Python project skeleton and tooling configuration
    - Create `pyproject.toml` with package metadata, the pinned runtime dependencies (`aiohttp` 3.x,
      `aiomqtt>=2.3,<3`, `pydantic` v2) and dev dependencies (`pytest`, `pytest-asyncio`, `hypothesis` 6.x, `ruff`,
      `mypy`)
    - Configure `ruff`, `mypy --strict`, and `pytest` (including the `integration` marker and `asyncio_mode`) in
      `pyproject.toml`
    - Create the `src/remote_energy_meter/` package tree with `__init__.py` files for `config/`, `plugins/`,
      `plugins/idrac/`, `mqtt/`, `diagnostics/`, and the `tests/{unit,property,integration,conformance,fakes}/` tree
    - Make `remote_energy_meter.plugins` a discoverable package for `pkgutil.iter_modules`
    - _Requirements: 1.1, 1.2_

  - [x] 1.2 Create the Home Assistant addon manifest and container packaging
    - Create `repository.yaml` at the repo root and `remote_energy_meter/config.yaml` declaring name, version,
      architectures, `startup`, and the full options schema (poll interval, log level, MQTT section, device list)
    - Create `remote_energy_meter/build.yaml` mapping each architecture to `ghcr.io/home-assistant/{arch}-base-python`
    - Create `remote_energy_meter/Dockerfile` that installs the `src/` package and sets the entry point
    - Create the s6-overlay service files `rootfs/etc/s6-overlay/s6-rc.d/remote-energy-meter/{type,run,finish}` so the
      container exit status equals the metering process exit status
    - _Requirements: 1.1, 1.2_

- [x] 2. Define contracts and data models
  - [x] 2.1 Implement the plugin interface module
    - Write `plugins/interface.py` with `FailureClassification`, `PowerReading`, `ReadFailure`, `ReadResult`,
      `ValidationResult`, and the `DevicePlugin` protocol (`plugin_id`, `config_model`, `validate_connection`,
      `read_power`)
    - _Requirements: 3.1_

  - [x] 2.2 Implement the configuration data models
    - Write `config/models.py` with `MqttSettings`, `DeviceEntry`, `LogLevel`, and `ConfigurationObject`, including the
      device identifier pattern and length bounds, the broker port range, the device list bounds, and the interval
      bounds
    - Mark credential fields as `SecretStr` with the `credential` schema marker
    - _Requirements: 2.1, 2.11, 2.12, 5.2, 8.1, 9.6_

  - [x] 2.3 Implement the runtime, rejection, and persistence models
    - Write `ResolvedDevice`, `RejectionReason`, and `EntryRejection` alongside the config models
    - Write `DeviceEnergyState` and `EnergyStateFile` with `schema_version` for `state_store` consumption
    - _Requirements: 3.6, 5.8, 7.3, 7.6_

  - [x] 2.4 Write unit tests for model field constraints
    - Cover identifier pattern and 64/65-length boundaries, broker port bounds, device list bounds
    - Cover that `SecretStr` credential fields do not render plaintext in `repr`
    - _Requirements: 2.11, 2.12, 8.2_

- [x] 3. Implement diagnostics and credential redaction
  - [x] 3.1 Implement the credential registry and redaction filter
    - Write `diagnostics/redaction.py` with `CredentialRegistry` and a `logging.Filter` that rewrites message, args,
      and `exc_info` text
    - Implement 4-gram shielding expanded to the maximal matching span, replaced with the fixed marker `***REDACTED***`
    - _Requirements: 8.1, 8.2, 8.7_

  - [x] 3.2 Implement the diagnostics logger
    - Write `diagnostics/logger.py` emitting one structured record per event to stdout with UTC timestamp, exactly one
      severity, optional device identifier, and a message truncated to 512 characters
    - Implement level resolution (configured value in set, `info` when omitted, `info` plus a warning when out of set)
      and install the redaction filter on the root logger
    - Implement the total mapping from the seven internal failure classifications onto the four logged values
    - _Requirements: 9.1, 9.2, 9.3, 9.6, 9.7_

  - [x] 3.3 Write property test for credential containment
    - **Property 32: No credential fragment escapes to any sink**
    - **Validates: Requirements 8.2, 8.3, 8.7**

  - [x] 3.4 Write property test for log record shape
    - **Property 33: Every log record has the required shape**
    - **Validates: Requirements 9.1**

  - [x] 3.5 Write property test for level filtering and fallback
    - **Property 34: Level filtering and level fallback are exact**
    - **Validates: Requirements 9.2, 9.7**

  - [x] 3.6 Write property test for failure classification mapping
    - **Property 35: Failure classification maps onto exactly one logged value**
    - **Validates: Requirements 9.3**

  - [x] 3.7 Write unit tests for logger defaults and message truncation
    - Cover the `info` default when the log level is omitted and the 512-character truncation boundary
    - _Requirements: 9.1, 9.6_

- [x] 4. Implement configuration parsing and printing
  - [x] 4.1 Implement Home Assistant secret reference resolution
    - Write `config/secrets.py` with a `SecretResolver` that resolves `!secret <name>` references under a 5 second
      per-resolution bound and reports unresolved references without the referenced value
    - _Requirements: 8.4, 8.6_

  - [x] 4.2 Implement the configuration parser
    - Write `config/parser.py` returning `ParseOutcome` (config, fatal error, per-entry rejections)
    - Resolve secrets before validation, then validate each `DeviceEntry` against the plugin-published schema, sorting
      pydantic errors by declaration index so "the first failing field" is deterministic
    - Implement fatal outcomes: unreadable or unparseable text, out-of-range global interval, missing or out-of-range
      MQTT host/port
    - Implement per-entry rejections: schema violation, malformed identifier, case-insensitive duplicate identifier
      where the first occurrence wins, empty credential, credential over 256 characters, unresolved secret reference
    - Apply the 30 second global interval default and the log level default/fallback in the parser
    - _Requirements: 2.1, 2.2, 2.3, 2.5, 2.6, 2.7, 2.10, 2.11, 2.12, 8.1, 8.5, 8.8, 9.6, 9.7_

  - [x] 4.3 Implement the configuration printer
    - Write `config/printer.py` emitting every field explicitly, including fields holding default values, with
      resolved literal credential values, returned as a string and never routed to a log or MQTT sink
    - _Requirements: 2.8_

  - [x] 4.4 Write property test for the configuration round trip
    - **Property 1: Configuration print-parse round trip**
    - **Validates: Requirements 2.1, 2.8, 2.9**

  - [x] 4.5 Write property test for entry acceptance
    - **Property 2: Accepted entries are exactly the valid entries, in order**
    - **Validates: Requirements 2.2, 2.3, 2.12, 8.5, 8.6, 8.8**

  - [x] 4.6 Write property test for duplicate identifier resolution
    - **Property 3: Duplicate identifiers resolve to the first occurrence**
    - **Validates: Requirements 2.5**

  - [x] 4.7 Write property test for global setting rejection
    - **Property 4: Out-of-range global settings reject the whole configuration**
    - **Validates: Requirements 2.7, 2.11**

  - [x] 4.8 Write unit tests for fatal configuration paths and defaults
    - Cover unreadable options, the read-versus-parse discriminator, zero accepted entries, and the 30 second default
    - _Requirements: 1.7, 2.4, 2.6, 2.10_

  - [x] 4.9 Write the schema agreement test
    - Assert `config.yaml` options schema covers every field the parser reads, field for field
    - _Requirements: 1.1_

- [x] 5. Checkpoint - configuration core complete
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. Implement the plugin registry and conformance suite
  - [x] 6.1 Implement plugin discovery, resolution, and instantiation
    - Write `plugins/registry.py` walking `remote_energy_meter.plugins` with `pkgutil.iter_modules` and collecting
      module-level classes satisfying `DevicePlugin`
    - Emit one record per discovered plugin plus a total-count record; exclude modules missing any interface element
      with a record naming the module and each missing element; exit non-zero on duplicate plugin identifier or zero
      plugins discovered
    - Implement exact case-sensitive resolution, rejection of unknown plugin identifiers with a record naming the entry
      identifier, the unresolved identifier, and every discovered identifier, and one instance per accepted entry
    - Invoke `validate_connection` exactly once per instance, log the result, and retain the entry regardless
    - _Requirements: 3.2, 3.3, 3.4, 3.5, 3.6, 3.8, 3.9, 3.10, 10.3, 10.5_

  - [x] 6.2 Implement the shippable plugin conformance suite
    - Write `plugins/conformance.py` exposing `assert_plugin_conforms(plugin_cls, valid_params, invalid_params)`
    - Check identifier length and non-emptiness, schema acceptance of a valid entry and rejection of one with a missing
      required field, `validate_connection` returning a success or classified failure, and `read_power` returning a
      `PowerReading` with device identifier, watts in 0–100000, and a UTC timestamp
    - Report each failed check naming the unsatisfied `Plugin_Interface` element
    - _Requirements: 10.2, 10.4_

  - [x] 6.3 Write property test for plugin resolution
    - **Property 5: Plugin resolution is exact, case-sensitive, and partitions the device list**
    - **Validates: Requirements 3.3, 3.4**

  - [x] 6.4 Write property test for incomplete plugin modules
    - **Property 6: Incomplete plugin modules are excluded and fully reported**
    - **Validates: Requirements 3.9, 10.4, 10.5**

  - [x] 6.5 Write property test for connection validation invocation
    - **Property 8: Connection validation runs once and never gates retention**
    - **Validates: Requirements 3.10**

  - [x] 6.6 Write unit tests for fatal registry conditions
    - Cover duplicate plugin identifier and zero discovered plugins, each exiting non-zero before the scheduler starts
    - _Requirements: 3.5, 3.8_

- [x] 7. Implement energy accumulation and persistence
  - [x] 7.1 Implement the energy integration math and accumulator
    - Write `energy.py` with `interval_kwh` (trapezoidal, divided by 1000, rounded to 6 decimals, clamped at zero) and
      per-device state holding `total_kwh` and an optional baseline
    - Implement first-reading baselining, discard of non-advancing timestamps, and re-baselining without contribution
      when the gap exceeds 4× the device poll interval
    - _Requirements: 7.1, 7.2, 7.5, 7.7, 7.8_

  - [x] 7.2 Implement the atomic state store
    - Write `state_store.py` serializing `EnergyStateFile` to `/data/energy_state.json` via temp file, `fsync`,
      `os.replace`
    - Implement restore at startup, the 300 second periodic flush, the SIGTERM flush path bounded at 5 seconds, and
      per-device fallback to 0 kWh with a stated reason when an entry is unreadable, non-numeric, or negative
    - Retain the persisted baseline timestamp for diagnostics without restoring it as an active baseline
    - _Requirements: 7.3, 7.4, 7.6, 7.9_

  - [x] 7.3 Write property test for monotonic energy and baseline advance
    - **Property 27: Cumulative energy never decreases and baselines advance correctly**
    - **Validates: Requirements 7.2, 7.7, 7.8**

  - [x] 7.4 Write property test for integration additivity
    - **Property 28: Energy integration is additive under subdivision**
    - **Validates: Requirements 7.1**

  - [x] 7.5 Write property test for gap handling
    - **Property 29: Gaps beyond the threshold are discarded and rebaselined**
    - **Validates: Requirements 7.5**

  - [x] 7.6 Write property test for the persistence round trip
    - **Property 30: Energy state survives a persist-restore round trip**
    - **Validates: Requirements 7.4**

  - [x] 7.7 Write property test for corrupt persisted state isolation
    - **Property 31: Corrupt persisted state is isolated to the affected devices**
    - **Validates: Requirements 7.6**

  - [x] 7.8 Write unit tests for flush timing against a virtual clock
    - Cover the 300 second periodic flush and the 5 second SIGTERM persistence bound
    - _Requirements: 7.3, 7.9_

- [x] 8. Implement the pure MQTT construction layer
  - [x] 8.1 Implement topic and unique identifier derivation
    - Write `mqtt/topics.py` producing the bridge availability, device availability, power state, energy state, power
      attributes, and discovery config topics, plus `rem_{device_id.lower()}_{power|energy}` unique identifiers derived
      from the device identifier and entity role only
    - _Requirements: 6.11_

  - [x] 8.2 Implement discovery payload construction
    - Write `mqtt/discovery.py` building the power and energy configuration payloads from a `PublishableDevice` view
      containing only identifier, display name, manufacturer, model, and plugin id, so credential fields are absent by
      type
    - Set power `device_class`/unit/state class to `power`/`W`/`measurement`, energy to `energy`/`kWh`/
      `total_increasing`, a shared `device.identifiers` block, and `availability_mode: all` listing the bridge and
      device availability topics
    - _Requirements: 6.1, 6.2, 6.3, 6.6, 8.3_

  - [x] 8.3 Write property test for discovery payload structure
    - **Property 20: Discovery payloads create one device with one power and one energy entity**
    - **Validates: Requirements 6.1, 6.2, 6.3, 6.6**

  - [x] 8.4 Write property test for unique identifier determinism
    - **Property 25: Entity unique identifiers are deterministic and derived only from the device identifier**
    - **Validates: Requirements 6.11**

- [x] 9. Checkpoint - pure core complete
  - Ensure all tests pass, ask the user if questions arise.

- [x] 10. Implement the iDRAC plugin
  - [x] 10.1 Implement the iDRAC connection parameter schema
    - Write `plugins/idrac/schema.py` with `IdracParams`: required host, required username and password as credential
      `SecretStr` fields, `verify_tls` defaulting to true, optional `power_path` of at most 512 characters, optional
      `request_timeout_s` in 1–60 defaulting to 10
    - _Requirements: 4.1_

  - [x] 10.2 Implement pure Redfish payload extraction
    - Implement `extract_watts(payload) -> float | ReadFailure` reading `PowerConsumedWatts` from the first
      `PowerControl` element that carries it, falling back to the `Oem.Dell` variant only when the standard member is
      absent
    - Return `RESPONSE_FORMAT` naming the requested path when the member is absent, `INVALID_MEASUREMENT` when the
      value is non-numeric or outside 0–10000 W
    - _Requirements: 4.4, 4.5, 4.11_

  - [x] 10.3 Implement the iDRAC HTTP read and connection validation
    - Write `plugins/idrac/plugin.py` issuing exactly one `aiohttp` HTTPS GET per read with `BasicAuth` from that
      entry's credentials, its own `ClientSession`, and the default chassis power path
      `/redfish/v1/Chassis/System.Embedded.1/Power` when `power_path` is omitted
    - Classify 401/403 as `AUTHENTICATION`, timeout as `TIMEOUT`, chain validation failure as `TLS_VALIDATION`, other
      unsuccessful status or connect failure as `COMMUNICATION` naming the host; pass `ssl=False` when `verify_tls` is
      false
    - Stamp the timestamp from the UTC clock at response receipt; implement `validate_connection` as one request whose
      success requires the same payload shape as a successful read
    - _Requirements: 4.2, 4.3, 4.6, 4.7, 4.8, 4.9, 4.10, 4.12, 4.13_

  - [x] 10.4 Write property test for faithful readings
    - **Property 9: Valid Redfish responses yield faithful readings**
    - **Validates: Requirements 4.4**

  - [x] 10.5 Write property test for unusable response classification
    - **Property 10: Unusable Redfish responses are classified, never raised, and never accumulate**
    - **Validates: Requirements 4.5, 4.11, 4.12**

  - [x] 10.6 Write property test for validation agreeing with reading
    - **Property 11: Connection validation agrees with reading**
    - **Validates: Requirements 4.13**

  - [x] 10.7 Write property test for per-entry credentials and single request
    - **Property 12: Each read uses exactly one request bearing its own entry's credentials**
    - **Validates: Requirements 4.3**

  - [x] 10.8 Run the conformance suite against the iDRAC plugin
    - Write `tests/conformance/test_idrac_conforms.py` calling `assert_plugin_conforms(IdracPlugin, ...)`
    - _Requirements: 10.2_

  - [x] 10.9 Write unit tests for iDRAC schema defaults and fixed statuses
    - Cover schema requiredness and defaults, the default power path, and the 401/403 and timeout classifications
    - _Requirements: 4.1, 4.6, 4.7, 4.10_

- [x] 11. Implement the poll scheduler
  - [x] 11.1 Implement pure interval and timeout resolution
    - Write `resolve_interval` in `scheduler.py` preferring a device interval within 5–3600 and otherwise falling back
      to the global interval while reporting the rejected value
    - Write `resolve_timeout` returning the entry timeout when present and otherwise `min(interval_s / 3, 10.0)`
    - _Requirements: 5.2, 5.7, 5.8_

  - [x] 11.2 Implement the poll cycle executor with retry policy
    - Run each attempt under `asyncio.timeout(request_timeout)`, classifying cancellation as `TIMEOUT` and any
      unhandled plugin exception as `PLUGIN` at the scheduler boundary
    - Retry every classification except `AUTHENTICATION` up to 2 additional times at 1 s then 2 s, stopping once the
      elapsed cycle time for that device reaches its poll interval
    - On a fully failed cycle report the device as unavailable, pass no reading to the accumulator, and continue
    - Emit one debug record per polled device naming identifier, watts, and duration in whole milliseconds
    - _Requirements: 3.7, 5.4, 5.5, 5.6, 9.4_

  - [x] 11.3 Implement the per-device loop and concurrency control
    - Run one task per accepted device under a `TaskGroup` with an injected clock and sleep function
    - Advance the next scheduled time by a fixed increment so scheduling error does not accumulate, start the first
      request within 1 second of start, hold at most one read in flight per device with a per-device lock, and skip the
      newly scheduled cycle with a record naming the identifier and skipped time when the previous cycle is still running
    - _Requirements: 5.1, 5.3, 5.9_

  - [x] 11.4 Write property test for schedule drift
    - **Property 13: The poll schedule is drift-free**
    - **Validates: Requirements 5.1**

  - [x] 11.5 Write property test for interval resolution
    - **Property 14: Interval resolution prefers a valid device interval**
    - **Validates: Requirements 5.2, 5.7**

  - [x] 11.6 Write property test for timeout resolution
    - **Property 15: Timeout resolution falls back to a capped third of the interval**
    - **Validates: Requirements 5.8**

  - [x] 11.7 Write property test for poll cycle duration
    - **Property 16: A poll cycle costs no more than its slowest read**
    - **Validates: Requirements 5.3**

  - [x] 11.8 Write property test for the retry schedule
    - **Property 17: The retry schedule follows classification and budget**
    - **Validates: Requirements 5.5**

  - [x] 11.9 Write property test for fully failed cycles
    - **Property 18: A fully failed cycle reports unavailable and contributes no energy**
    - **Validates: Requirements 5.6**

  - [x] 11.10 Write property test for overrunning cycles
    - **Property 19: Overrunning cycles skip exactly the elapsed scheduled times**
    - **Validates: Requirements 5.9**

  - [x] 11.11 Write property test for device fault isolation
    - **Property 7: Device failures are isolated**
    - **Validates: Requirements 3.6, 3.7, 4.2**

- [x] 12. Implement the MQTT publisher
  - [x] 12.1 Implement the broker connection lifecycle
    - Write `mqtt/publisher.py` connecting with `aiomqtt`, registering the last will on the bridge availability topic,
      and publishing discovery payloads with the retain flag
    - Implement the reconnect loop as its own task with delays doubling from 1 second capped at 60 seconds and no
      attempt limit, so a failed first connection logs the broker address and classification without terminating the
      process
    - _Requirements: 6.5, 6.6, 6.9, 6.12_

  - [x] 12.2 Implement state, availability, and diagnostic publication
    - Publish watts rounded to 1 decimal place and `online` availability within 2 seconds of a reading, `offline`
      within 2 seconds of a final retry failure, and the cumulative energy total on the retained energy topic
    - Guard non-finite values and values outside 0–100000 W by discarding, leaving the last published state unchanged,
      and logging the rejected value
    - Publish the operation duration in whole milliseconds as a diagnostic attribute, and publish the power entity with
      no duration attribute value before the first success since start
    - _Requirements: 6.4, 6.7, 6.8, 6.14, 9.5, 9.8_

  - [x] 12.3 Implement rediscovery ordering and the retained reading buffer
    - On reconnection republish every accepted device's discovery payloads before any state or availability publication
    - Retain exactly one most-recent reading per device while disconnected and publish it on reconnection only when its
      age is at most 4× that device's poll interval, otherwise discard it
    - _Requirements: 6.10, 6.13_

  - [x] 12.4 Write property test for power state publication
    - **Property 21: Power state is published if and only if the value is usable**
    - **Validates: Requirements 6.4, 6.14**

  - [x] 12.5 Write property test for published availability
    - **Property 22: Published availability reflects the most recent completed outcome**
    - **Validates: Requirements 1.5, 6.7, 6.8**

  - [x] 12.6 Write property test for the reconnect delay schedule
    - **Property 23: Reconnect delays double from 1 second and cap at 60 seconds**
    - **Validates: Requirements 6.9**

  - [x] 12.7 Write property test for the retained reading buffer
    - **Property 24: At most one reading is retained per device and published only while fresh**
    - **Validates: Requirements 6.10**

  - [x] 12.8 Write property test for rediscovery ordering
    - **Property 26: Rediscovery precedes every state publication after reconnection**
    - **Validates: Requirements 6.13**

  - [x] 12.9 Write property test for per-cycle accounting
    - **Property 36: Every polled device is accounted for once per cycle**
    - **Validates: Requirements 9.4, 9.5**

  - [x] 12.10 Write unit tests for fixed MQTT behaviors
    - Cover the retain flag on discovery publishes, last will registration, a non-fatal first-connection failure, and
      the power entity published without a duration attribute before the first success
    - _Requirements: 6.5, 6.6, 6.12, 9.8_

- [x] 13. Checkpoint - components complete
  - Ensure all tests pass, ask the user if questions arise.

- [x] 14. Wire the application together
  - [x] 14.1 Implement startup orchestration
    - Write `app.py` running named stages in order: load configuration, configure the logger level, register credential
      values for redaction, initialize the plugin registry, resolve and instantiate plugins, start the publisher
      without blocking on connection success, start the scheduler, then log startup complete with the accepted device
      count
    - On a stage failure log one error record naming the failed stage and classification, skip scheduler start, and
      exit non-zero
    - _Requirements: 1.3, 1.4, 1.7, 2.4, 3.5, 3.8, 6.12_

  - [x] 14.2 Implement the process entry point and shutdown sequence
    - Write `__main__.py` installing SIGTERM/SIGINT handlers and propagating the exit status
    - Run shutdown under an outer 10 second budget: cancel scheduler tasks, persist energy state under a 5 second
      bound, publish `offline` for every accepted device, disconnect cleanly, exit 0
    - On budget expiry log a warning naming the exceeded deadline and exit immediately without further publication
    - _Requirements: 1.2, 1.5, 1.6, 7.3_

  - [x] 14.3 Write unit tests for startup and shutdown behavior
    - Cover the ordered startup stages and the startup-complete record, per-stage failure handling with a non-zero
      exit, and the exceeded shutdown deadline
    - _Requirements: 1.3, 1.4, 1.6_

  - [x] 14.4 Write property test for conforming plugins against the unmodified core
    - **Property 37: Any conforming plugin works against the unmodified core**
    - **Validates: Requirements 10.3**

- [x] 15. Implement integration tests
  - [x] 15.1 Build the fake Redfish server fixture
    - Write `tests/fakes/redfish_server.py` as an `aiohttp` app serving the chassis power resource with switchable
      behaviors for valid responses, absent members, arbitrary HTTP statuses, and stalls, served over HTTPS with a
      generated self-signed certificate
    - _Requirements: 4.3, 4.5, 4.8, 4.9_

  - [x] 15.2 Build the local Mosquitto broker fixture
    - Write `tests/fakes/broker.py` starting a Mosquitto subprocess on an ephemeral port, skipping with a clear message
      when the binary is absent
    - _Requirements: 6.1_

  - [x] 15.3 Write TLS verification integration tests
    - Assert a self-signed certificate is accepted when `verify_tls` is false and returns `TLS_VALIDATION` when true
    - _Requirements: 4.8, 4.9_

  - [x] 15.4 Write broker integration tests
    - Subscribe as Home Assistant would and assert discovery payloads, state and availability topics, retain flags, and
      the last will after an ungraceful client kill
    - _Requirements: 6.1, 6.5, 6.6, 6.13_

  - [x] 15.5 Write the end-to-end integration test
    - Run one addon process against two fake iDRAC endpoints and a real broker; assert two devices with four entities,
      wattage tracking the endpoints, and a monotonically growing energy total; restart the process and assert totals
      resume from persisted values with unchanged unique identifiers
    - _Requirements: 6.11, 7.4_

  - [x] 15.6 Write container-level tests
    - Build the image and assert the container exit status equals the metering process exit status, and that SIGTERM to
      the container yields `offline` availability and exit 0
    - _Requirements: 1.2, 1.5_

- [x] 16. Complete documentation and CI
  - [x] 16.1 Write the plugin authoring and user documentation
    - Write `docs/writing-a-plugin.md` covering each `Plugin_Interface` element with its inputs, outputs, and failure
      classifications, the discovery mechanism, and every `Power_Reading` field
    - Write `remote_energy_meter/DOCS.md` (including `verify_tls: false` guidance for the target iDRAC hosts),
      `README.md`, and `CHANGELOG.md`
    - _Requirements: 10.1_

  - [x] 16.2 Write the documentation completeness test
    - Assert one documented section exists per interface element, per failure classification, and per `Power_Reading`
      field
    - _Requirements: 10.1_

  - [x] 16.3 Write the CI workflow
    - Write `.github/workflows/ci.yaml` running `ruff check`, `ruff format --check`, `mypy --strict`, the default
      `pytest` suite (unit, property, conformance), `pytest -m integration` with Mosquitto installed, the addon linter
      against `config.yaml`, and a multi-arch image build
    - _Requirements: 1.1, 1.2, 10.2_

- [x] 17. Final checkpoint - full suite green
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP.
- Each task references specific requirement numbers; property test tasks reference the design's property numbers.
- Every property test carries the tagging comment
  `# Feature: remote-energy-meter, Property N: ...` and a minimum of 100 Hypothesis examples.
- Timing-sensitive property tests (13, 16, 17, 19, 23, 24) run against an injected virtual clock rather than real
  sleeps; wall-clock behavior is verified once in the integration layer.
- Shared Hypothesis generators live in `tests/property/generators.py` and are extended by whichever property task first
  needs each generator.
- The conformance suite ships inside the package as `remote_energy_meter.plugins.conformance`, not under `tests/`.
- Every test sub-task runs from the Python 3.12 environment prepared in task 0, the same interpreter CI uses.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2"] },
    { "id": 1, "tasks": ["0.1", "0.2", "0.3", "0.4", "2.1", "2.2", "2.3"] },
    { "id": 2, "tasks": ["2.4", "3.1", "8.1"] },
    { "id": 3, "tasks": ["3.2", "4.1", "7.1", "10.1"] },
    { "id": 4, "tasks": ["3.3", "3.4", "3.5", "3.6", "3.7", "4.2", "6.1", "7.2", "8.2", "10.2", "11.1"] },
    { "id": 5, "tasks": ["4.3", "4.9", "6.2", "6.3", "6.5", "6.6", "7.3", "7.4", "7.5", "8.3", "8.4", "10.3", "11.2", "11.5", "11.6"] },
    { "id": 6, "tasks": ["4.4", "4.5", "4.6", "4.7", "4.8", "6.4", "7.6", "7.7", "7.8", "10.4", "10.5", "10.6", "10.7", "10.8", "10.9", "11.3", "12.1"] },
    { "id": 7, "tasks": ["11.4", "11.7", "11.8", "11.9", "11.10", "11.11", "12.2"] },
    { "id": 8, "tasks": ["12.3", "12.4", "12.6", "12.10", "15.1", "15.2"] },
    { "id": 9, "tasks": ["12.7", "12.8", "12.9", "14.1"] },
    { "id": 10, "tasks": ["14.2", "15.3", "15.4"] },
    { "id": 11, "tasks": ["12.5", "14.3", "14.4", "15.5", "15.6", "16.1", "16.3"] },
    { "id": 12, "tasks": ["16.2"] }
  ]
}
```

## Hardware Validation Addendum Tasks (2026-09-05)

The following task extends the completed historical plan without renumbering Properties 1–37. Status remains under the
orchestrator's control.

- [x] 18. Add generic IPMI sensor support for the validated legacy controller
  - [x] 18.1 Implement the credential-marked IPMI schema, pure sensor parser, and production DevicePlugin
  - [x] 18.2 Add bounded shell-free subprocess execution with environment-only password delivery and cleanup
  - [x] 18.3 Integrate discovery, addon defaults/schema, Alpine `ipmitool`, and user/developer documentation
  - [x] 18.4 Add parser, schema, async operation, conformance, schema-agreement, and packaging unit coverage
  - [x] 18.5 Write property test for faithful and total IPMI sensor parsing
    - **Property 38: IPMI sensor parsing is faithful and total**
    - **Validates: Requirements A.3**
  - [x] 18.6 Run targeted and full non-integration validation, static checks, and one sanitized live IPMI read


## Companion Integration Addendum Tasks (2026-09)

The add-on protocol and companion integration implementations are present in the repository as separate artifacts.
Status remains under the orchestrator's control. Verification and artifact construction remain non-deploying: they do
not copy into Home Assistant, restart Core, deploy the add-on, or access a live broker.

- [ ] 19. Verify and finalize add-on-side companion protocol and Supervisor discovery
  - [ ] 19.1 Add retained schema-versioned credential-free inventory and exact topic construction
  - [ ] 19.2 Delete legacy retained MQTT discovery configs before inventory on every connection while preserving state,
    availability, attributes, and cumulative-energy publication
  - [ ] 19.3 Add the nonfatal injectable Supervisor `/discovery` announcement and add-on manifest declaration
  - [ ] 19.4 Update publisher/discovery/startup unit coverage for exact payload, auth, failure, retain, and ordering behavior
  - [ ] 19.5 Write property test for inventory protocol round trip
    - **Property 39: Inventory round-trip is exact and credential-free**
    - **Validates: Requirements B.2, B.3**
  - [ ] 19.6 Write property test for cleanup, inventory, and state ordering
    - **Property 40: Migration cleanup and inventory precede state on every connection**
    - **Validates: Requirements B.4**
  - [ ] 19.7 Keep requirements, design, tasks, changelog, and user documentation aligned with the shipped integration
  - [ ] 19.8 Run targeted and full non-integration tests plus Ruff, format-check, and strict mypy

- [ ] 20. Verify and finalize the Home Assistant companion integration
  - [ ] 20.1 Verify config flow, setup/unload, push-only MQTT coordinator, dynamic devices, sensors, and diagnostics
  - [ ] 20.2 Verify strict malformed-message handling and stable device/entity identity
  - [ ] 20.3 Build a separate validated integration directory/zip without changing the add-on image contents
  - [ ] 20.4 Run Home Assistant-style lifecycle, coordinator, entity, availability, malformed-message, and diagnostics tests
  - [ ] 20.5 Maintain installation/distribution documentation; perform runtime validation only when separately approved
  - [ ] 20.6 Run the package validator, targeted tests, full non-integration suite, Ruff, and strict mypy

## Branding Addendum Tasks (2026-09)

The branding workflow is intentionally split at the user-selection boundary. Candidate research and construction do not
promote files into either distributable component and do not deploy or restart Home Assistant. Status remains under the
orchestrator's control.

- [ ] 21. Add Home Assistant branding without crossing the add-on/integration artifact boundary
  - [ ] 21A Research current official branding contracts and stage four original candidate systems under
    `docs/icon-candidates/`, including editable SVG sources, exact-size add-on/integration PNGs, a contact sheet, and
    validation evidence; stop before selection or deployment
  - [ ] 21B Record the user's explicit candidate selection without choosing on the user's behalf
  - [ ] 21C Promote only the selected add-on pair to `remote_energy_meter/{icon.png,logo.png}`, update the add-on bundle
    allowlist/tests, and verify the graphics remain outside the container image filesystem
  - [ ] 21D On Home Assistant 2026.3 or newer, promote only the selected integration set to
    `custom_components/remote_energy_meter/brand/`, make package validation binary-aware, and update focused separate-
    artifact tests; use a `home-assistant/brands` contribution instead for older Core versions
  - [ ] 21E Build and validate the two separate local artifacts, then deploy to `/addons` and
    `/config/custom_components`, reload/rebuild the add-on, restart Core, or publish externally only under a separately
    approved deployment action
