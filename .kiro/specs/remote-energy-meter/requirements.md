# Requirements Document

## Introduction

Remote Energy Meter is a Home Assistant addon that measures the electrical power consumption of network-attached
devices that do not expose power data to Home Assistant natively. The addon polls each configured device over the
network, converts the reading into a normalized measurement, and publishes the measurement to Home Assistant as a
sensor entity.

The addon is built around a plugin architecture: each device family is supported by one plugin that knows how to
authenticate against and read power data from that family. The first supported family is Dell PowerEdge servers
managed through iDRAC, covering both servers in the target environment (hosts 192.0.2.10 and 192.0.2.21, with
iDRAC management interfaces at 192.0.2.9 and 192.0.2.20) with a single shared plugin.

In addition to instantaneous power in watts, the addon derives cumulative energy in kilowatt-hours so that Home
Assistant Energy Dashboard integration is possible.

## Glossary

- **Remote_Energy_Meter**: The Home Assistant addon as a whole, comprising all components defined in this glossary.
- **Addon_Configuration**: The set of user-supplied options provided through the Home Assistant addon options
  interface, stored by Home Assistant at `/data/options.json`.
- **Configuration_Parser**: The component that reads Addon_Configuration text and produces a Configuration_Object.
- **Configuration_Printer**: The component that serializes a Configuration_Object back into Addon_Configuration text.
- **Configuration_Object**: The in-memory, validated representation of Addon_Configuration.
- **Device_Entry**: One element of the device list inside a Configuration_Object, identifying a single physical device,
  the plugin that reads the device, and the plugin-specific connection parameters for the device.
- **Device_Plugin**: A software module that implements the Plugin_Interface for one device family.
- **Plugin_Interface**: The contract every Device_Plugin implements, consisting of a plugin identifier, a
  configuration schema, a connection validation operation, and a power reading operation.
- **Plugin_Registry**: The component that discovers available Device_Plugin modules and resolves a Device_Entry to a
  Device_Plugin instance.
- **iDRAC_Plugin**: The Device_Plugin that reads power consumption from Dell PowerEdge servers through the iDRAC
  Redfish API.
- **Power_Reading**: A normalized measurement produced by a Device_Plugin, containing the device identifier, the
  instantaneous power value in watts, and the UTC timestamp of the measurement.
- **Poll_Scheduler**: The component that triggers a Power_Reading request for each Device_Entry on a repeating interval.
- **Energy_Accumulator**: The component that integrates successive Power_Reading values into a cumulative energy total
  in kilowatt-hours per device.
- **MQTT_Publisher**: The component that publishes Power_Reading values, cumulative energy totals, and availability
  state to the MQTT broker used by Home Assistant.
- **MQTT_Discovery**: The Home Assistant convention by which a device and its entities are created automatically from
  a configuration payload published to the MQTT broker.
- **Diagnostics_Logger**: The component that writes structured log records to the addon log stream.
- **Availability_State**: The published per-device state indicating whether the most recent Power_Reading attempt
  succeeded, expressed as `online` or `offline`.

## Requirements

### Requirement 1: Addon Packaging and Startup

**User Story:** As a Home Assistant administrator, I want Remote Energy Meter to install and run as a Home Assistant
addon, so that power metering runs on the same host as Home Assistant without separate infrastructure.

#### Acceptance Criteria

1. THE Remote_Energy_Meter SHALL provide a Home Assistant addon manifest declaring the addon name, a version value, at
   least one target architecture, the startup type, and an options schema that covers every Addon_Configuration field
   the Configuration_Parser reads.
2. THE Remote_Energy_Meter SHALL publish a container image whose entry point starts the metering process, and the
   container exit status SHALL equal the exit status of the metering process.
3. WHEN the Home Assistant Supervisor starts the Remote_Energy_Meter, THE Remote_Energy_Meter SHALL complete the
   startup steps in the order load the Addon_Configuration, initialize the Plugin_Registry, start the Poll_Scheduler,
   and SHALL record a startup-complete log record naming the count of accepted Device_Entry values within 30 seconds of
   process start.
4. IF a startup step named in criterion 3 fails, THEN THE Remote_Energy_Meter SHALL record a log record at error
   severity naming the failed startup step and the failure classification, SHALL NOT start the Poll_Scheduler, and
   SHALL exit with a non-zero status.
5. WHEN the Remote_Energy_Meter receives a SIGTERM signal, THE Remote_Energy_Meter SHALL stop the Poll_Scheduler,
   publish `offline` as the Availability_State for every accepted Device_Entry, and exit with status 0 within 10 seconds
   of receiving the signal.
6. IF the shutdown steps named in criterion 5 have not completed within 10 seconds of receiving the SIGTERM signal,
   THEN THE Remote_Energy_Meter SHALL terminate the metering process without further publication and record a log
   record at warning severity indicating that the shutdown deadline was exceeded.
7. IF the Addon_Configuration is absent or cannot be read at startup, THEN THE Remote_Energy_Meter SHALL record a log
   record at error severity indicating that the Addon_Configuration is unreadable and exit with a non-zero status
   without starting the Poll_Scheduler.

### Requirement 2: Configuration Parsing and Validation

**User Story:** As a Home Assistant administrator, I want the addon to validate the configuration I provide, so that
mistakes are reported clearly instead of producing silent metering gaps.

#### Acceptance Criteria

1. WHEN the Remote_Energy_Meter starts, THE Configuration_Parser SHALL read the Addon_Configuration from the location
   supplied by the Home Assistant Supervisor and produce a Configuration_Object containing exactly one global poll
   interval expressed in whole seconds, exactly one MQTT connection section, and a list of 1 to 50 Device_Entry values.
2. WHEN the Configuration_Parser processes a Device_Entry, THE Configuration_Parser SHALL validate every field of the
   Device_Entry against the configuration schema published by the Device_Plugin named in the Device_Entry, treating a
   missing required field, an empty required field, a field whose type differs from the type declared by the schema, a
   field whose value falls outside the range declared by the schema, and a device-specific poll interval outside the
   range of 5 seconds to 3600 seconds inclusive as a validation failure.
3. IF a Device_Entry fails schema validation, THEN THE Configuration_Parser SHALL exclude that Device_Entry from the
   Configuration_Object, record one log record at error severity naming the Device_Entry identifier and the first
   failing field, retain every previously accepted Device_Entry, and continue validating the remaining Device_Entry
   values.
4. IF the Configuration_Object contains zero accepted Device_Entry values after every Device_Entry has been validated,
   THEN THE Remote_Energy_Meter SHALL record a log record at error severity indicating that no Device_Entry was
   accepted and exit with a non-zero status within 10 seconds.
5. IF two or more Device_Entry values declare the same device identifier, compared without regard to letter case, THEN
   THE Configuration_Parser SHALL accept only the Device_Entry that appears first in the Addon_Configuration device
   list, exclude every later Device_Entry declaring that identifier, and record one log record at error severity naming
   the duplicated identifier.
6. WHERE the Addon_Configuration omits the global poll interval, THE Configuration_Parser SHALL set the global poll
   interval of the Configuration_Object to 30 seconds.
7. IF the Addon_Configuration supplies a global poll interval that is not a whole number of seconds or that falls
   outside the range of 5 seconds to 3600 seconds inclusive, THEN THE Remote_Energy_Meter SHALL reject the
   Addon_Configuration, record a log record at error severity naming the supplied value and the accepted range, and
   exit with a non-zero status within 10 seconds.
8. WHEN the Configuration_Printer serializes a Configuration_Object, THE Configuration_Printer SHALL produce
   Addon_Configuration text that the Configuration_Parser accepts without recording any validation failure.
9. WHEN the Configuration_Parser parses the Addon_Configuration text that the Configuration_Printer produced from a
   valid Configuration_Object, THE Configuration_Parser SHALL produce a Configuration_Object whose global poll
   interval, MQTT connection section fields, and ordered list of Device_Entry values are field-by-field equal to those
   of the original Configuration_Object (round-trip property).
10. IF the Addon_Configuration cannot be read or cannot be parsed as structured configuration text, THEN THE
    Remote_Energy_Meter SHALL record a log record at error severity indicating whether the read or the parse failed and
    exit with a non-zero status within 10 seconds, leaving the Addon_Configuration unmodified.
11. IF the MQTT connection section omits the broker host address, omits the broker port, or declares a broker port
    outside the range of 1 to 65535 inclusive, THEN THE Remote_Energy_Meter SHALL reject the Addon_Configuration,
    record a log record at error severity naming the offending field, and exit with a non-zero status within 10 seconds.
12. IF a Device_Entry declares a device identifier that is empty, that exceeds 64 characters, or that contains a
    character other than a letter, a digit, a hyphen, or an underscore, THEN THE Configuration_Parser SHALL exclude
    that Device_Entry from the Configuration_Object and record one log record at error severity naming the rejected
    identifier and the accepted identifier form.

### Requirement 3: Plugin Architecture

**User Story:** As a developer, I want each device family supported by an independent plugin, so that support for a new
device type is added without modifying the metering core.

#### Acceptance Criteria

1. THE Plugin_Interface SHALL define a plugin identifier of 1 to 64 characters, a configuration schema that enumerates
   each connection parameter with its name, its data type, and whether it is required or optional, a connection
   validation operation that returns either success or a failure classification, and a power reading operation that
   returns either a Power_Reading or a read failure carrying a failure classification.
2. WHEN the Remote_Energy_Meter starts, THE Plugin_Registry SHALL complete discovery of every Device_Plugin available
   in the addon image before the Poll_Scheduler starts and SHALL record one log record per discovered Device_Plugin
   naming the plugin identifier, followed by one log record naming the total count of discovered Device_Plugin modules.
3. WHEN the Plugin_Registry resolves a Device_Entry, THE Plugin_Registry SHALL return the Device_Plugin whose plugin
   identifier is a case-sensitive exact match of the plugin identifier named in the Device_Entry.
4. IF a Device_Entry names a plugin identifier that the Plugin_Registry did not discover, THEN THE Plugin_Registry
   SHALL reject that Device_Entry, exclude the rejected Device_Entry from every subsequent poll cycle, record a log
   record at error severity naming the Device_Entry identifier, the unresolved plugin identifier, and every discovered
   plugin identifier, and resolve the remaining Device_Entry values.
5. IF two discovered Device_Plugin modules declare the same plugin identifier, THEN THE Plugin_Registry SHALL record a
   log record at error severity naming the duplicated plugin identifier and terminate the Remote_Energy_Meter with a
   non-zero exit status before the Poll_Scheduler starts.
6. THE Plugin_Registry SHALL create exactly one Device_Plugin instance per accepted Device_Entry, so that a connection
   failure, an authentication failure, or a timeout failure on one Device_Plugin instance leaves the connection state
   of every other Device_Plugin instance unchanged.
7. IF a Device_Plugin raises an unhandled exception during a power reading operation, THEN THE Poll_Scheduler SHALL
   convert the exception into a read failure classified as a plugin failure, mark that Device_Entry as failed for that
   poll cycle, continue polling the remaining Device_Entry values, and keep the Remote_Energy_Meter process running.
8. IF the Plugin_Registry discovers zero Device_Plugin modules, THEN THE Remote_Energy_Meter SHALL record a log record
   at error severity indicating that no Device_Plugin was discovered and exit with a non-zero status.
9. IF a discovered module omits any element defined by the Plugin_Interface, THEN THE Plugin_Registry SHALL exclude
   that module from the set of discovered Device_Plugin values, record a log record at error severity naming the module
   and each missing element, and continue discovering the remaining modules.
10. WHEN the Plugin_Registry creates a Device_Plugin instance for an accepted Device_Entry, THE Plugin_Registry SHALL
    invoke the connection validation operation of that instance exactly once, record a log record naming the
    Device_Entry identifier and the returned result, and retain the Device_Entry for polling regardless of the returned
    result.

### Requirement 4: Dell PowerEdge iDRAC Plugin

**User Story:** As a Home Assistant administrator, I want the power consumption of my two Dell PowerEdge servers read
through iDRAC, so that both servers report power without additional hardware meters.

#### Acceptance Criteria

1. THE iDRAC_Plugin SHALL declare a configuration schema containing a required iDRAC host address, a required iDRAC
   username of 1 to 64 characters, a required iDRAC password of 1 to 128 characters, a TLS certificate verification
   flag defaulting to true, an optional Redfish power resource path of at most 512 characters, and an optional request
   timeout in the range of 1 second to 60 seconds defaulting to 10 seconds.
2. THE iDRAC_Plugin SHALL support 1 to 32 Device_Entry values that differ only in connection parameters, so that both
   target servers are metered by one Device_Plugin implementation.
3. WHEN the Poll_Scheduler requests a Power_Reading for a Device_Entry handled by the iDRAC_Plugin, THE iDRAC_Plugin
   SHALL issue exactly one request for the Redfish power resource of the configured iDRAC host over HTTPS using HTTP
   Basic authentication with the iDRAC username and iDRAC password of that Device_Entry.
4. WHEN the iDRAC Redfish response contains a present power watts value that is numeric and within the range of 0 watts
   to 10000 watts inclusive, THE iDRAC_Plugin SHALL return a Power_Reading whose device identifier equals the device
   identifier of the Device_Entry, whose power value equals that present power watts value, and whose timestamp is the
   UTC time at which the response was received.
5. IF the iDRAC Redfish response omits a present power watts value, THEN THE iDRAC_Plugin SHALL return a read failure
   classified as a response format failure naming the requested Redfish power resource path, and SHALL NOT return a
   Power_Reading.
6. IF the iDRAC host returns HTTP status 401 or HTTP status 403, THEN THE iDRAC_Plugin SHALL return a read failure
   classified as an authentication failure.
7. IF the iDRAC host does not complete the response within the configured request timeout of the Device_Entry, THEN THE
   iDRAC_Plugin SHALL abandon the request and return a read failure classified as a timeout failure.
8. WHERE the TLS certificate verification flag is set to false, THE iDRAC_Plugin SHALL accept a self-signed TLS
   certificate presented by the iDRAC host and return a Power_Reading when the response satisfies criterion 4.
9. WHERE the TLS certificate verification flag is set to true, THE iDRAC_Plugin SHALL reject a TLS certificate that
   fails chain validation and return a read failure classified as a TLS validation failure.
10. WHERE the Redfish power resource path is omitted from the Device_Entry, THE iDRAC_Plugin SHALL use the default
    Redfish chassis power resource path.
11. IF the iDRAC Redfish response contains a present power watts value that is non-numeric or outside the range of
    0 watts to 10000 watts inclusive, THEN THE iDRAC_Plugin SHALL return a read failure classified as an invalid
    measurement failure and SHALL leave the cumulative energy total of that Device_Entry unchanged.
12. IF the iDRAC host returns an unsuccessful HTTP response status other than 401 and other than 403, or the connection
    to the iDRAC host cannot be established, THEN THE iDRAC_Plugin SHALL return a read failure classified as a
    communication failure naming the iDRAC host address.
13. WHEN the connection validation operation of the iDRAC_Plugin is invoked for a Device_Entry, THE iDRAC_Plugin SHALL
    issue exactly one request for the Redfish power resource within the configured request timeout and return a
    validation result that is successful only when the response satisfies criterion 4.

### Requirement 5: Polling and Scheduling

**User Story:** As a Home Assistant administrator, I want each device polled on a predictable interval, so that power
values in Home Assistant stay current without overloading the managed devices.

#### Acceptance Criteria

1. WHILE the Remote_Energy_Meter is running, THE Poll_Scheduler SHALL request one Power_Reading for every accepted
   Device_Entry once per poll interval of that Device_Entry, starting the first request within 1 second of
   Poll_Scheduler start and starting every subsequent request within 1 second of its scheduled time.
2. WHERE a Device_Entry declares a device-specific poll interval within the range of 5 seconds to 3600 seconds, THE
   Poll_Scheduler SHALL use the device-specific poll interval for that Device_Entry instead of the global poll interval.
3. THE Poll_Scheduler SHALL issue the power reading operations of all accepted Device_Entry values of one poll cycle
   concurrently and SHALL hold at most one power reading operation in flight per Device_Entry, so that the elapsed
   duration of the poll cycle does not exceed the duration of the slowest single power reading operation of that cycle
   plus 1 second.
4. IF a single power reading attempt for a Device_Entry does not complete within the request timeout of that
   Device_Entry, THEN THE Poll_Scheduler SHALL cancel that attempt and classify the attempt as a timeout failure.
5. IF a power reading attempt for a Device_Entry fails with a failure classification other than authentication failure,
   THEN THE Poll_Scheduler SHALL retry the operation up to 2 additional times within the same poll cycle, waiting
   1 second before the first retry and 2 seconds before the second retry, and SHALL make no further attempt in that
   poll cycle once the elapsed time of the poll cycle for that Device_Entry, including attempts and retry delays,
   reaches the poll interval of that Device_Entry.
6. IF no successful Power_Reading is obtained for a Device_Entry within a poll cycle, THEN THE Poll_Scheduler SHALL
   report that Device_Entry to the MQTT_Publisher as an unavailable device, SHALL pass no Power_Reading for that poll
   cycle to the Energy_Accumulator, and SHALL continue with the next poll cycle at the next scheduled time.
7. IF a Device_Entry declares a device-specific poll interval outside the range of 5 seconds to 3600 seconds, THEN THE
   Poll_Scheduler SHALL use the global poll interval for that Device_Entry and record a log record naming the
   Device_Entry identifier and the rejected poll interval value.
8. WHERE a Device_Entry omits a request timeout, THE Poll_Scheduler SHALL apply a request timeout equal to one third of
   the poll interval of that Device_Entry, capped at a maximum of 10 seconds.
9. IF the poll cycle of a Device_Entry is still in progress when the next scheduled poll time of that Device_Entry
   arrives, THEN THE Poll_Scheduler SHALL skip the newly scheduled poll cycle for that Device_Entry, record a log
   record naming the Device_Entry identifier and the skipped scheduled time, and resume at the following scheduled time.

### Requirement 6: Home Assistant Publication

**User Story:** As a Home Assistant administrator, I want metered devices to appear as Home Assistant entities
automatically, so that no manual YAML configuration is needed in Home Assistant.

#### Acceptance Criteria

1. WHILE the connection to the MQTT broker is established, WHEN the Remote_Energy_Meter accepts a Device_Entry, THE
   MQTT_Publisher SHALL publish, within 5 seconds of that acceptance, one MQTT_Discovery configuration payload that
   creates one Home Assistant device with exactly one power sensor entity and exactly one energy sensor entity for that
   Device_Entry.
2. THE MQTT_Publisher SHALL set the power sensor entity device class to `power`, the unit of measurement to `W`, and
   the state class to `measurement`.
3. THE MQTT_Publisher SHALL set the energy sensor entity device class to `energy`, the unit of measurement to `kWh`,
   and the state class to `total_increasing`.
4. WHILE the connection to the MQTT broker is established, WHEN the Poll_Scheduler obtains a Power_Reading, THE
   MQTT_Publisher SHALL publish the power value of the Power_Reading in watts, rounded to 1 decimal place, to the state
   topic of the corresponding power sensor entity within 2 seconds of the Power_Reading being obtained.
5. THE MQTT_Publisher SHALL publish MQTT_Discovery configuration payloads with the retain flag enabled, so that Home
   Assistant recreates the entities after a Home Assistant restart.
6. WHEN the MQTT_Publisher connects to the MQTT broker, THE MQTT_Publisher SHALL register a last will message that
   sets the Availability_State of every Device_Entry to `offline`.
7. WHEN a Power_Reading succeeds for a Device_Entry, THE MQTT_Publisher SHALL publish `online` as the
   Availability_State of that Device_Entry within 2 seconds of the Power_Reading being obtained.
8. WHEN all retries within a poll cycle fail for a Device_Entry, THE MQTT_Publisher SHALL publish `offline` as the
   Availability_State of that Device_Entry within 2 seconds of the final retry failing.
9. IF the connection to the MQTT broker is lost, THEN THE MQTT_Publisher SHALL attempt reconnection repeatedly without
   an attempt limit, doubling the delay between attempts starting at 1 second and capping the delay at 60 seconds, and
   SHALL NOT terminate the Remote_Energy_Meter process.
10. WHILE the connection to the MQTT broker is unavailable, THE MQTT_Publisher SHALL retain exactly one Power_Reading
    per Device_Entry, being the most recent one, and after reconnection SHALL publish the retained Power_Reading if the
    elapsed time since its measurement timestamp is at most 4 times the poll interval of that Device_Entry, otherwise
    SHALL discard the retained Power_Reading without publishing it.
11. THE MQTT_Publisher SHALL derive each entity unique identifier from the device identifier of the Device_Entry only,
    so that two runs of the Remote_Energy_Meter using the same Configuration_Object produce identical entity unique
    identifiers and a restart reuses the existing Home Assistant entities.
12. IF the MQTT_Publisher cannot establish its first connection to the MQTT broker at startup, THEN THE MQTT_Publisher
    SHALL record a log record naming the broker address and the connection failure classification, continue
    reconnection attempts using the delay schedule of criterion 9, and THE Remote_Energy_Meter SHALL continue running
    the Poll_Scheduler without terminating.
13. WHEN the MQTT_Publisher re-establishes a connection to the MQTT broker after a connection loss, THE MQTT_Publisher
    SHALL republish the MQTT_Discovery configuration payload of every accepted Device_Entry before publishing any state
    value or Availability_State value.
14. IF the power value of a Power_Reading is not a finite number within the range of 0 W to 100000 W, THEN THE
    MQTT_Publisher SHALL discard that Power_Reading, leave the last published power sensor entity state unchanged, and
    record a log record naming the Device_Entry identifier and the rejected value.

### Requirement 7: Energy Accumulation

**User Story:** As a Home Assistant administrator, I want cumulative energy totals in addition to instantaneous power,
so that metered devices can be added to the Home Assistant Energy Dashboard.

#### Acceptance Criteria

1. WHEN the Energy_Accumulator receives a Power_Reading for a Device_Entry for which a previous Power_Reading is
   retained as the accumulation baseline, THE Energy_Accumulator SHALL increase the cumulative energy total of that
   Device_Entry by the mean of the two power values in watts multiplied by the elapsed time between the two measurement
   timestamps expressed in hours and divided by 1000, rounded to 6 decimal places of a kilowatt-hour.
2. THE Energy_Accumulator SHALL treat a computed interval contribution of less than 0 kWh as a contribution of 0 kWh,
   so that the cumulative energy total of a Device_Entry never decreases between successive updates.
3. WHEN the Remote_Energy_Meter receives a SIGTERM signal, THE Energy_Accumulator SHALL persist the cumulative energy
   total and the measurement timestamp of the most recent Power_Reading of every Device_Entry to the addon data
   directory within 5 seconds.
4. WHEN the Remote_Energy_Meter starts and a persisted cumulative energy total exists for a Device_Entry, THE
   Energy_Accumulator SHALL restore the persisted cumulative energy total as the starting cumulative energy total of
   that Device_Entry.
5. IF the elapsed time between two consecutive Power_Reading values for one Device_Entry exceeds 4 times the poll
   interval of that Device_Entry, THEN THE Energy_Accumulator SHALL discard the interval, leave the cumulative energy
   total of that Device_Entry unchanged, and retain the later Power_Reading as the accumulation baseline.
6. IF the persisted cumulative energy total for a Device_Entry cannot be read, cannot be interpreted as a number, or is
   less than 0 kWh, THEN THE Energy_Accumulator SHALL start the cumulative energy total of that Device_Entry at 0 kWh
   and record a log record naming the Device_Entry identifier and the reason the persisted total was rejected.
7. WHEN the Energy_Accumulator receives the first Power_Reading for a Device_Entry after the Remote_Energy_Meter
   starts, THE Energy_Accumulator SHALL retain that Power_Reading as the accumulation baseline of the Device_Entry and
   leave the cumulative energy total of that Device_Entry unchanged.
8. IF the measurement timestamp of a Power_Reading is earlier than or equal to the measurement timestamp of the
   retained accumulation baseline of the same Device_Entry, THEN THE Energy_Accumulator SHALL discard the
   Power_Reading, leave the cumulative energy total of that Device_Entry unchanged, and keep the existing accumulation
   baseline.
9. WHILE the Remote_Energy_Meter is running, THE Energy_Accumulator SHALL persist the cumulative energy total of every
   Device_Entry to the addon data directory at least once every 300 seconds, so that a termination without SIGTERM
   loses at most 300 seconds of accumulated energy per Device_Entry.

### Requirement 8: Credential Handling

**User Story:** As a Home Assistant administrator, I want device credentials handled safely, so that iDRAC passwords
are not exposed in logs or Home Assistant state.

#### Acceptance Criteria

1. THE Remote_Energy_Meter SHALL treat as a credential value every MQTT connection username and password field and
   every Device_Entry field that the configuration schema of the named Device_Plugin marks as a credential field,
   including the iDRAC username and the iDRAC password of the iDRAC_Plugin, and SHALL read those values only from the
   Addon_Configuration options managed by the Home Assistant Supervisor.
2. THE Diagnostics_Logger SHALL replace every credential value in a log record with a fixed redaction marker whose
   length is constant and independent of the length of the credential value, so that no log record contains a
   credential value or any substring of a credential value of 4 or more characters.
3. THE MQTT_Publisher SHALL exclude credential values from every published payload, including MQTT_Discovery
   configuration payloads, power sensor state payloads, energy sensor state payloads, Availability_State payloads,
   diagnostic entity attributes, and the registered last will message.
4. WHERE the Addon_Configuration declares a Home Assistant secret reference for a credential field, THE
   Configuration_Parser SHALL resolve the reference to the secret value before validating the Device_Entry and SHALL
   complete each resolution within 5 seconds.
5. IF a credential field referenced by a Device_Entry is absent, or contains zero characters after removal of leading
   and trailing whitespace, THEN THE Configuration_Parser SHALL reject the Device_Entry, record a log record at error
   severity naming the Device_Entry identifier and the name of the empty credential field, and continue parsing the
   remaining Device_Entry values.
6. IF a Home Assistant secret reference declared for a credential field cannot be resolved within 5 seconds or does
   not exist, THEN THE Configuration_Parser SHALL reject the Device_Entry, record a log record at error severity
   naming the Device_Entry identifier and the unresolved secret reference name without the referenced value, and
   continue parsing the remaining Device_Entry values.
7. IF a Device_Plugin operation raises an exception whose text contains a credential value, THEN THE
   Remote_Energy_Meter SHALL replace that credential value with the fixed redaction marker before the exception text
   is written to the addon log stream or included in any MQTT payload.
8. IF a credential field value of a Device_Entry contains more than 256 characters, THEN THE Configuration_Parser
   SHALL reject the Device_Entry and record a log record at error severity naming the Device_Entry identifier and the
   name of the oversized credential field.

### Requirement 9: Diagnostics and Observability

**User Story:** As a Home Assistant administrator, I want clear diagnostics in the addon log, so that a device that
stops reporting can be troubleshot without reading source code.

#### Acceptance Criteria

1. THE Diagnostics_Logger SHALL write each log record to the addon log stream as one record containing a UTC timestamp
   with at least whole-second resolution, exactly one severity level from the set `debug`, `info`, `warning`, `error`, the
   Device_Entry identifier when the record concerns a single Device_Entry, and a message of 1 to 512 characters.
2. WHERE the Addon_Configuration sets the log level to one of `debug`, `info`, `warning`, `error`, THE Diagnostics_Logger
   SHALL emit only log records whose severity level is equal to or higher than the configured log level, ranked
   `debug` < `info` < `warning` < `error`.
3. WHEN a power reading operation fails, THE Diagnostics_Logger SHALL write, within 1 second of the failure, one log
   record at warning severity naming the Device_Entry identifier and exactly one failure classification from the set
   `authentication`, `timeout`, `tls_validation`, `read`.
4. WHEN a poll cycle completes, THE Diagnostics_Logger SHALL write one log record at debug severity per polled
   Device_Entry naming the Device_Entry identifier, the power value in watts, and the operation duration in whole
   milliseconds.
5. WHEN a power reading operation succeeds for a Device_Entry, THE MQTT_Publisher SHALL publish the duration of that
   operation in whole milliseconds as a diagnostic attribute of the power sensor entity of that Device_Entry.
6. WHERE the Addon_Configuration omits the log level, THE Diagnostics_Logger SHALL use `info` as the log level.
7. IF the Addon_Configuration sets the log level to a value outside the set `debug`, `info`, `warning`, `error`, THEN THE
   Diagnostics_Logger SHALL use `info` as the log level and write one log record at warning severity naming the rejected
   value.
8. IF no power reading operation has succeeded for a Device_Entry since the most recent Remote_Energy_Meter start, THEN
   THE MQTT_Publisher SHALL publish the power sensor entity of that Device_Entry without a duration diagnostic attribute
   value.

### Requirement 10: Adding a New Device Family

**User Story:** As a developer, I want a documented plugin extension path, so that I can add a new device family
without changing the metering core.

#### Acceptance Criteria

1. THE Remote_Energy_Meter SHALL provide developer documentation that describes, for each element of the
   Plugin_Interface (plugin identifier, configuration schema, connection validation operation, power reading
   operation), the accepted inputs, the returned outputs, and the failure classifications, and that describes the
   plugin discovery mechanism and every field of the Power_Reading contract (device identifier, power value in watts,
   UTC measurement timestamp).
2. THE Remote_Energy_Meter SHALL provide a reference test suite that verifies, for one Device_Plugin under test, that
   the plugin identifier is a non-empty string of at most 64 characters, that the configuration schema accepts a valid
   Device_Entry and rejects a Device_Entry with a missing required field, that the connection validation operation
   returns either a success result or a classified failure result, and that the power reading operation returns a
   Power_Reading containing the device identifier, a power value in watts within the range 0 to 100000, and a UTC
   timestamp.
3. WHEN a new Device_Plugin module conforming to the Plugin_Interface is added to the addon image and the
   Remote_Energy_Meter starts, THE Plugin_Registry SHALL discover the new Device_Plugin and record its plugin
   identifier in the addon log stream with no modification to the Poll_Scheduler, the Energy_Accumulator, or the
   MQTT_Publisher.
4. IF a Device_Plugin under test fails one or more reference test suite checks, THEN THE Remote_Energy_Meter SHALL
   complete the reference test suite with a failed result and report each failed check naming the Plugin_Interface
   element that was not satisfied.
5. IF a module discovered by the Plugin_Registry does not implement every element of the Plugin_Interface, THEN THE
   Plugin_Registry SHALL exclude the module from the set of available Device_Plugin modules, record a log record naming
   the module and each missing Plugin_Interface element, and continue discovering the remaining modules.

## Assumptions

The following assumptions were made while drafting these requirements. Correcting any assumption changes the
requirements above.

1. Publication to Home Assistant uses MQTT_Discovery through the Mosquitto broker addon rather than the Home Assistant
   REST API, because MQTT_Discovery survives Home Assistant restarts and avoids long-lived access tokens.
2. Power data is read from iDRAC through the Redfish API rather than IPMI or SNMP, because Redfish is available on
   iDRAC 7 and later and returns power in watts directly.
3. The default poll interval is 30 seconds, which balances Home Assistant graph resolution against iDRAC load.
4. Cumulative energy in kilowatt-hours is derived inside the addon rather than by a Home Assistant integration sensor,
   so that the Energy Dashboard works without extra Home Assistant configuration.
5. The addon runs on the same network segment as the iDRAC management interfaces, so no VPN or proxy traversal is required.

## Hardware Validation Addendum (2026-09-05)

This addendum corrects the original assumption that both target controllers expose usable Redfish power data. It does
not renumber or rewrite Requirements 1–10.

### Addendum Requirement A: Generic IPMI Sensor Support

1. THE Remote_Energy_Meter SHALL provide an `ipmi` Device_Plugin with required credential-marked host, username, and
   password parameters; defaults `sensor_name: System Level`, `interface: lanplus`, `cipher_suite: 3`, and
   `request_timeout_s: 10`; and bounded cipher/timeout values.
2. WHEN the IPMI plugin reads or validates a device, it SHALL use exactly one read-only `ipmitool sdr get` subprocess,
   created without a shell, and SHALL provide the password only through `IPMI_PASSWORD` with `-E`.
3. WHEN IPMI sensor output contains a finite value from 0 through 100000 W in the `Sensor Reading` field, the plugin
   SHALL return that value with a UTC receipt timestamp; malformed/missing output, invalid values, authentication,
   timeout, and communication/tool failures SHALL retain their distinct classifications.
4. THE addon image SHALL install `ipmitool`, discovery SHALL find the plugin without core changes, and the shipped
   configuration SHALL retain `192.0.2.9` on `idrac` while configuring `192.0.2.20` on `ipmi` without Redfish-only
   parameters.
5. Hardware acceptance observed on 2026-09-05: `192.0.2.9` returned 162 W through Redfish; `192.0.2.20` rejected
   DCMI but authenticated with IPMI v2/RMCP+ `lanplus`, cipher suite 3, and returned 112 W from `System Level`. These
   values are observations, not fixed expected measurements. Neither path requires SNMP.


## Companion Integration Addendum (2026-09)

This addendum supersedes Requirement 6 only where it assigned entity ownership to generic Home Assistant MQTT
Discovery. The add-on remains the poller, accumulator, and MQTT transport. The repository implements the complete
companion path in criteria B.1-B.13 as two separately packaged components: the add-on-side Supervisor announcement,
versioned inventory, migration cleanup, and state transport, plus `custom_components/remote_energy_meter`. Build and
validation workflows do not deploy either component or restart Home Assistant.

### Addendum Requirement B: Companion Integration and Versioned Inventory

1. THE add-on SHALL declare `discovery: [remote_energy_meter]` and SHALL best-effort POST
   `{service: remote_energy_meter, config: {instance_id, base_topic, inventory_topic, schema_version}}` to Supervisor
   `/discovery` using `SUPERVISOR_TOKEN`; failure SHALL produce a warning and SHALL NOT stop polling or MQTT.
2. THE Supervisor discovery body SHALL contain no broker or device credential, host, username, password, or secret
   reference. Home Assistant SHALL handle it through `async_step_hassio(HassioServiceInfo)` with user confirmation.
3. ON every MQTT connection, THE add-on SHALL publish one retained, versioned inventory to
   `<base_topic>/integration/devices` containing only the stable instance ID, normalized base topic, bridge availability
   topic, credential-free device metadata, and exact per-device availability, power-state, energy-state, and
   power-attribute topics.
4. BEFORE publishing inventory on every connection, THE add-on SHALL publish retained empty payloads to the two legacy
   Home Assistant MQTT discovery config topics for every accepted device; inventory SHALL precede every attribute,
   state, and availability publication, and no non-empty generic MQTT entity config SHALL be published.

### Companion runtime and distribution requirements

The following criteria define the shipped companion runtime and its separate, non-deploying local distribution
workflow.

5. THE repository SHALL provide `custom_components/remote_energy_meter` with a versioned manifest, `config_flow: true`,
   MQTT dependency, `iot_class: local_push`, `integration_type: hub`, strings, and an English translation.
6. THE integration SHALL reject unknown inventory schema versions, missing or extra fields, duplicate or invalid device
   IDs, cross-wired topics, non-finite numeric states, malformed attributes, and arbitrary malformed MQTT payloads
   without raising out of an MQTT callback or replacing the last valid state.
7. THE integration SHALL use a `DataUpdateCoordinator` with no polling interval, fed only by Home Assistant MQTT
   subscriptions; valid later inventories SHALL update existing entities and add newly announced devices without a
   config-entry reload.
8. THE sensor platform SHALL create integration-owned power and energy `SensorEntity` objects with stable unique IDs
   derived only from device ID and role, and `DeviceInfo.identifiers == {(DOMAIN, device_id)}`. Power SHALL be W,
   `measurement`, with a duration attribute; energy SHALL be kWh, `total_increasing`.
9. AN entity SHALL be available if and only if both the bridge and its device availability payloads are `online` and
   the coordinator is healthy.
10. THE integration SHALL allow exactly one config entry. Supervisor discovery SHALL use `instance_id`, never an IP
    address, display name, add-on slug, or Supervisor message UUID, as the config-entry unique ID. Rediscovery SHALL
    replace changed base/inventory metadata and reload the existing entry; manual setup SHALL require only a base topic.
11. Integration diagnostics SHALL redact nothing because this protocol contains no credentials, SHALL include config
    metadata, device IDs, and current availability, and SHALL omit raw MQTT payloads.
12. THE local artifact builder SHALL produce the add-on directory/zip and a separate deterministic integration
    directory/zip whose copy-ready path is `custom_components/remote_energy_meter`; the add-on image SHALL NOT contain
    the custom integration. A validation helper SHALL require manifest, strings, translations, compilable Python, and
    absence of credential material.
13. Local installation documentation SHALL instruct the administrator to copy the integration to
    `/config/custom_components/remote_energy_meter`, restart Core, then install/rebuild and start the add-on, confirm
    its discovery under Devices & Services, and use manual base-topic setup only as fallback. GitHub/HACS distribution
    is optional and no Home Assistant deployment or restart is performed by the build or validation workflow.
