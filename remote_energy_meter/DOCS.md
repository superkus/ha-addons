# Remote Energy Meter

Remote Energy Meter is a Home Assistant add-on that polls network management controllers, integrates cumulative
energy, and transports power, energy, attributes, and availability over MQTT. Generic MQTT entity configuration is no
longer published; the add-on emits a credential-free schema-1 inventory consumed by the included companion integration.

> **Companion integration status:** `custom_components/remote_energy_meter` is included in this repository and is
> packaged separately from the add-on image. The build and validation workflows do not copy it into Home Assistant,
> restart Core, deploy the add-on, or touch a live broker.

The image includes two device plugins:

- `idrac` reads Dell iDRAC 7 and later through the Redfish HTTPS API.
- `ipmi` reads a named IPMI SDR sensor, including the `System Level` sensor validated on a legacy iDRAC 6 controller.

The add-on image includes the `ipmitool` executable used by the `ipmi` plugin. Neither plugin requires SNMP. The IPMI
plugin also does not require DCMI.

## Prerequisites

Before installation, provide all of the following:

- A Supervisor-managed Home Assistant installation that supports add-ons.
- An MQTT broker reachable from the add-on. The Mosquitto broker add-on is the usual choice.
- Network routing and firewall rules from the add-on network to the selected management interfaces:
  - MQTT uses the configured TCP port. `1883` is conventional without TLS and `8883` is conventional with TLS; the
    broker may use another port.
  - Redfish uses HTTPS, normally TCP port `443`.
  - IPMI-over-LAN uses UDP port `623`.
- For `idrac`, Redfish enabled on an iDRAC 7 or later controller and a readable chassis power resource.
- For `ipmi`, IPMI-over-LAN enabled and a named SDR sensor whose `Sensor Reading` is in watts. DCMI is not needed.
- A dedicated least-privilege account on each controller. Grant only login/session and sensor-reading access required
  by the chosen protocol. Do not reuse an operating-system account or a broad controller administrator account when a
  narrower account is available.

Keep iDRAC and IPMI management interfaces on an isolated management network. Do not expose Redfish or IPMI-over-LAN
directly to the internet.

## Local add-on and companion integration delivery

Create both deterministic copy-ready artifacts on local storage with one non-deploying command:

```bash
python3 tools/build_local_addon_bundle.py \
  --output /private/tmp/remote-energy-meter-local-addon \
  --integration-output /private/tmp/remote-energy-meter-local-integration
```

The builder validates both artifacts and writes installation guides; it does not copy files into Home Assistant,
restart Core, deploy the add-on, or access a live broker. For a separately approved manual installation:

1. Copy the generated integration directory to `/config/custom_components/remote_energy_meter`.
2. Restart Home Assistant Core so it loads the custom integration.
3. Copy the generated local add-on directory to `/addons/remote_energy_meter`, then reload **Local add-ons** in the
   Add-on Store and install/start **Remote Energy Meter**. The add-on announces itself through Supervisor and publishes
   retained inventory; its image never contains or copies the integration.
4. Open **Settings → Devices & services**, select the discovered **Remote Energy Meter** card, and confirm setup. If
   Supervisor discovery is unavailable, add the integration manually with only the MQTT base topic.

Review each generated `LOCAL_INSTALL.md` before installation. GitHub/HACS is an optional later path; the local add-on
and integration artifacts are the primary route.

### Selected branding and refresh behavior

The user selected the original **Pulse Dial** artwork. The add-on uses `icon.png` (128×128) and `logo.png` (250×100)
beside `config.yaml`; these files are Supervisor presentation metadata and are not copied into the running image. The
separate companion integration uses the four local files under
`custom_components/remote_energy_meter/brand/` (`icon.png`, `icon@2x.png`, `logo.png`, and `logo@2x.png`). Home
Assistant Core 2026.3 and newer supports that local brand directory and does not require an icon field in
`manifest.json`.

After an integration replacement and Core restart, refresh the browser if Devices & services still displays a cached
image. After an add-on source replacement, reload **Local add-ons**, rebuild the add-on, and refresh the browser so
Supervisor and the frontend re-read its presentation files. The add-on never installs the companion integration, and
no artwork is published to GitHub or `home-assistant/brands` by these workflows.

## Home Assistant secrets and configuration mapping

The add-on's Configuration tab is stored by Supervisor as `/data/options.json`. The manifest maps the Home Assistant
configuration directory into the container read-only as `homeassistant_config:ro`. The add-on looks for
`secrets.yaml` at `/homeassistant/secrets.yaml` first and `/config/secrets.yaml` for older Supervisor layouts. It never
writes to that mapping.

Put credentials in `secrets.yaml` using placeholder-free values of your own. This example contains no real
credentials:

```yaml
# Home Assistant /config/secrets.yaml
rem_mqtt_username: "replace-with-dedicated-broker-user"
rem_mqtt_password: "replace-with-dedicated-broker-password"
rem_redfish_username: "replace-with-read-only-redfish-user"
rem_redfish_password: "replace-with-redfish-password"
rem_ipmi_username: "replace-with-read-only-ipmi-user"
rem_ipmi_password: "replace-with-ipmi-password"
```

Reference a secret in the add-on Configuration tab as `!secret <name>`. When editing JSON rather than YAML, use the
string form `"!secret <name>"`. References are resolved only for fields marked as credentials by the MQTT or selected
plugin schema. Do not put a secret in an unrecognized field: it will not be treated as a credential. Restart the
add-on after changing a referenced secret.

A missing, malformed, unsupported, or slow secret-store lookup is reported by reference name, never by secret value.
An unresolved MQTT credential rejects the whole configuration because the broker connection cannot be interpreted
safely. An unresolved device credential rejects that device entry while other valid entries continue. Secret loading
is bounded to five seconds.

Credential fields use protected in-memory representations and registered credential values are redacted from
structured logs with a fixed marker. The retained inventory, state, availability, Supervisor discovery, and diagnostic
payloads are built from a credential-free device view. For IPMI, the password is passed to the child process only
through `IPMI_PASSWORD` and `ipmitool -E`, never as a command-line argument, and the child receives *only* `PATH`,
`LC_ALL`, and `IPMI_PASSWORD`: it never inherits the add-on's environment (which carries the Supervisor token), so a
compromised `ipmitool` cannot read it. The IPMI username is necessarily visible in the child's arguments; `ipmitool`
offers no other way to pass it. A Redfish response larger than 1 MiB is refused as a response-format failure before it
is parsed. Configuration values still exist in Supervisor's protected options and in process memory; restrict access
to Home Assistant backups, the host, add-on configuration, and diagnostic exports.

### What the add-on can reach, and why

Like most Home Assistant add-ons, the metering process runs as root inside its container, and `map:
homeassistant_config:ro` mounts your whole Home Assistant configuration directory read-only so that `!secret`
references can be resolved from `secrets.yaml`. Supervisor cannot map a single file, so this is all-or-nothing: the
process could read anything in that directory, not only the secrets it needs. The add-on asks for nothing else: no
host network, no privileged mode, no Docker or Home Assistant API, and the Supervisor token it receives is scoped to
discovery announcements. The hardening above (minimal child environment, response caps, strict host and sensor-name
validation, hash-locked dependencies) exists to keep a compromised controller or library from turning that mount into
a disclosure. If you prefer not to grant it, put the credentials directly in the add-on options instead of `!secret`
references; Supervisor stores those in the add-on's own protected `/data/options.json`, and you can then remove the
`map:` entry from `config.yaml` in a local copy of the add-on.

## Full configuration: Redfish (`idrac`)

This is a complete, independently valid configuration for the Redfish-validated system. Every credential is a
`!secret` reference. The validated controller used a self-signed certificate, so this copy/paste example explicitly
uses `verify_tls: false`; read the TLS warning before retaining that setting.

```yaml
poll_interval_s: 30
log_level: info
mqtt:
  host: core-mosquitto
  port: 1883
  username: !secret rem_mqtt_username
  password: !secret rem_mqtt_password
  client_id: remote_energy_meter_redfish
  base_topic: remote_energy_meter
  discovery_prefix: homeassistant
  tls: false
devices:
  - device_id: poweredge_redfish
    plugin: idrac
    display_name: PowerEdge Redfish
    poll_interval_s: 30
    params:
      host: 192.0.2.9
      username: !secret rem_redfish_username
      password: !secret rem_redfish_password
      verify_tls: false
      power_path: /redfish/v1/Chassis/System.Embedded.1/Power
      request_timeout_s: 10
```

## Full configuration: IPMI (`ipmi`)

This is a complete, independently valid configuration for the IPMI-validated system. It contains no Redfish-only
parameters and every credential is a `!secret` reference.

```yaml
poll_interval_s: 30
log_level: info
mqtt:
  host: core-mosquitto
  port: 1883
  username: !secret rem_mqtt_username
  password: !secret rem_mqtt_password
  client_id: remote_energy_meter_ipmi
  base_topic: remote_energy_meter
  discovery_prefix: homeassistant
  tls: false
devices:
  - device_id: poweredge_ipmi
    plugin: ipmi
    display_name: PowerEdge IPMI
    poll_interval_s: 30
    params:
      host: 192.0.2.20
      username: !secret rem_ipmi_username
      password: !secret rem_ipmi_password
      sensor_name: System Level
      interface: lanplus
      cipher_suite: 3
      request_timeout_s: 10
```

To meter both systems in one add-on instance, keep one global and one `mqtt` section and place both device entries in
the same `devices` list. Use one broker `client_id` for that combined instance.

## First start and protocol preview

Startup discovers plugins, validates options, restores energy state, starts the non-blocking MQTT publisher, makes one
best-effort Supervisor discovery announcement, and then starts per-device poll loops. Supervisor or broker failure is
nonfatal to polling.

On each broker connection the add-on publishes retained empty payloads to the old power and energy MQTT discovery
config topics for every accepted device. It then publishes one retained schema-1 inventory at
`<base_topic>/integration/devices` before attributes, bridge/device availability, power, or energy state. It never
publishes non-empty generic MQTT entity JSON. The inventory contains only stable identity, public metadata, and exact
topics; broker and controller connection material cannot enter its input type.

The included companion integration consumes this protocol and owns the Home Assistant devices and sensors. Its
push-only coordinator subscribes to the announced inventory and exact state topics; malformed payloads leave the last
valid typed state unchanged.

## Configuration reference

Each table is an exact configuration scope. “Rejected device” means that entry is excluded, an error record names its
first failing field, and validation continues with the remaining entries. If no device remains, startup fails. “Fatal”
means the add-on does not start polling and exits non-zero. Fields not recognized by a plugin model are ignored by
that model; never use unknown fields to carry credentials.

### Global options

| Path | Type | Required | Default | Range or form | Invalid-value behavior | Security implications |
| --- | --- | --- | --- | --- | --- | --- |
| `poll_interval_s` | Whole-number seconds | No | `30` | `5`–`3600`; `null` is treated as omitted | Fatal. A boolean, text value, fractional number, or out-of-range value rejects the configuration and names the accepted range. | Very short intervals increase management-plane and broker load. Use the slowest interval that meets monitoring needs. |
| `log_level` | `debug` \| `info` \| `warning` \| `error` | No | `info` | Exact lowercase value | An unknown value falls back to `info` and is classified as a rejected setting. | `debug` includes device IDs, hosts, watts, durations, and retry detail. Credentials remain redacted, but logs can still reveal infrastructure metadata. |
| `mqtt` | Mapping | Yes | None | Must contain the MQTT fields below | Fatal when absent, not a mapping, or when any contained value is invalid. | Contains broker credentials and transport-security choices; protect the add-on options and backups. |
| `devices` | List of mappings | Yes | None | `1`–`50` accepted entries, in order | Invalid entries are rejected individually; entries after 50 accepted devices are rejected. Missing, non-list, empty, or zero surviving entries is fatal. | Each entry contains management endpoints and plugin credentials. The order decides which case-insensitive duplicate `device_id` wins. |

### MQTT options (`mqtt`)

| Path | Type | Required | Default | Range or form | Invalid-value behavior | Security implications |
| --- | --- | --- | --- | --- | --- | --- |
| `mqtt.host` | String | Yes | None | At least 1 character; hostname or address accepted by the broker client | Fatal when missing, empty, or not text. Connection/DNS failures are non-fatal at runtime and reconnect forever. | The address is diagnostic metadata and may appear in logs. Use a trusted broker endpoint. |
| `mqtt.port` | Integer | Yes | None | `1`–`65535`; commonly `1883` or TLS `8883` | Fatal when missing, wrong type, or out of range. The port is not changed automatically when `tls` changes. | Port `1883` normally carries credentials and payloads without transport encryption. Restrict it to a trusted network or enable TLS. |
| `mqtt.username` | Credential string or `null` | No | `null` | Prefer `!secret`; omit with `password` only for a deliberately anonymous broker | Invalid type or unresolved reference is fatal. A broker rejection is logged as `authentication` and reconnect is retried. | Credential-marked and redacted. Anonymous access usually provides less accountability and should be restricted at the broker. |
| `mqtt.password` | Credential string or `null` | No | `null` | Prefer `!secret`; normally pair with `username` | Invalid type or unresolved reference is fatal. A broker rejection is logged as `authentication` and reconnect is retried. | Credential-marked, redacted, and excluded from MQTT payloads. Without MQTT TLS it crosses the broker network without transport encryption. |
| `mqtt.client_id` | String | No | `remote_energy_meter` | Broker client identifier; use a unique non-empty value per running instance | Non-text is fatal during parsing. An empty, duplicate, or broker-invalid value may be refused or cause disconnect/reconnect at runtime. | Not a secret and visible to the broker. Reusing it can disconnect another client with the same ID. |
| `mqtt.base_topic` | String | No | `remote_energy_meter` | One or more topic levels; surrounding whitespace/slashes are trimmed; no `+`, `#`, NUL, or all-empty value | Non-text is fatal. An unusable topic prefix fails publisher startup. | Topic names and retained values are visible to authorized broker clients. Changing the prefix can leave old retained state/discovery messages behind. |
| `mqtt.discovery_prefix` | String | No | `homeassistant` | Legacy cleanup prefix; no `+`, `#`, NUL, or all-empty value | Non-text is fatal. Keep the prefix used by the previous release until its retained power/energy configs have been deleted. | Not a secret. It is used only to address retained empty migration payloads; the add-on publishes no non-empty MQTT discovery config. |
| `mqtt.tls` | Boolean | No | `false` | `true` creates a default certificate-verifying TLS context | Invalid type is fatal. Certificate/handshake failure is logged as `tls_validation` and reconnect is retried. | `false` provides no MQTT transport encryption. `true` verifies against the image trust store; there is no option here to disable verification. |

### Device envelope (`devices[]`)

| Path | Type | Required | Default | Range or form | Invalid-value behavior | Security implications |
| --- | --- | --- | --- | --- | --- | --- |
| `devices[].device_id` | String | Yes | None | `1`–`64` ASCII letters, digits, `_`, or `-`; compared case-insensitively for duplicates | Rejected device when empty, malformed, too long, or duplicated. The first declared case-insensitive duplicate wins. Avoid reserved topic node `bridge`. | Appears in MQTT topics, inventory, persisted-state keys, and logs. Do not use credentials or sensitive names. Changing it changes protocol identity. |
| `devices[].plugin` | String | Yes | None | `1`–`64` characters; exact, case-sensitive discovered ID (`idrac` or `ipmi`) | Rejected device for missing/invalid form or an undiscovered ID. If all are rejected, startup fails. | Selects the network protocol and credential schema. A typo never silently falls back to another plugin. |
| `devices[].display_name` | String or `null` | No | `device_id` | Any string; `null` or an empty string uses `device_id` for inventory | Wrong type rejects the device. | Published as credential-free inventory metadata; do not put secrets in it. |
| `devices[].poll_interval_s` | Integer seconds or `null` | No | Global `poll_interval_s` | `5`–`3600` | Wrong type or out-of-range value rejects the device; other devices continue. | Short values increase controller requests and can amplify authentication failures or account-lockout pressure. |
| `devices[].params` | Mapping | Yes | Empty mapping for validation | Exact recognized fields depend on `plugin`; required plugin fields must be present | Non-mapping or a plugin-schema failure rejects the device and names the first failing `params.<field>`. Unknown keys are ignored by current plugin models. | Only schema-marked credential fields are resolved and redacted. Never store secrets under unknown/custom keys. |

### Plugin `idrac` (`devices[].params`)

The `idrac` plugin performs one HTTPS GET with HTTP Basic authentication per attempt. It always uses HTTPS; an
`http://` prefix in `host` does not downgrade the request. A successful Redfish payload must contain a numeric
`PowerConsumedWatts` value from `0` through `10000` W.

| Path | Type | Required | Default | Range or form | Invalid-value behavior | Security implications |
| --- | --- | --- | --- | --- | --- | --- |
| `devices[].params.host` | String | Yes | None | `1`–`253` characters; hostname, IPv4, IPv6, optional port, optional `https://` prefix and trailing `/`; default HTTPS port is `443`. Must not start with `-` or contain whitespace, `@`, `?`, `#`, or an interior `/` (these would change the request target) | Rejected device when missing, empty, or not text. DNS/connect/HTTP failures become runtime `read` failures. | Endpoint is shown in diagnostics. Keep it on the management network; HTTPS is forced so Basic credentials are not intentionally sent over clear HTTP. |
| `devices[].params.username` | Credential string | Yes | None | `1`–`64` characters; use `!secret` | Missing, blank, unresolved, wrong type, or out-of-range value rejects the device. HTTP `401`/`403` becomes `authentication` with no poll retry. | Credential-marked, redacted, and used in HTTP Basic authentication. Use a dedicated read-only Redfish account. |
| `devices[].params.password` | Credential string | Yes | None | `1`–`128` characters; use `!secret` | Missing, blank, unresolved, wrong type, or out-of-range value rejects the device. HTTP `401`/`403` becomes `authentication`. | Credential-marked, redacted, and excluded from MQTT. Its safety on the network depends on authenticated TLS. |
| `devices[].params.verify_tls` | Boolean | No | `true` | `true` validates the certificate chain; `false` accepts an untrusted/self-signed peer | Wrong type rejects the device. With `true`, chain failure is `tls_validation`; with `false`, other TLS failures are `read`. | `false` preserves encryption but removes server identity verification, enabling interception and credential theft. Prefer a trusted controller certificate and `true`. |
| `devices[].params.power_path` | String or `null` | No | `/redfish/v1/Chassis/System.Embedded.1/Power` | At most `512` characters; a missing leading `/` is added | Wrong type or overlength rejects the device. A wrong/empty path normally produces HTTP, response-format, or invalid-measurement `read` failures. | The path may appear in diagnostics. Do not put query credentials or secret material in it. |
| `devices[].params.request_timeout_s` | Number of seconds | No | `10` in the plugin schema | `1`–`60`; explicit values may be fractional | Wrong type or out of range rejects the device. At runtime expiry becomes `timeout`. If the key is omitted, the scheduler uses `min(resolved poll interval / 3, 10)`; an explicit `10` always means 10 seconds. | Larger values keep a request open longer but remain bounded by the per-device cycle budget. They do not make requests overlap. |

#### Redfish TLS decision

`verify_tls: true` is the secure default. A factory iDRAC commonly presents a self-signed certificate, which is why
the validated copy/paste example explicitly sets `false`. With verification disabled, traffic remains encrypted but
the add-on cannot prove which controller answered; a network interceptor could impersonate iDRAC and collect the Basic
credentials. Install a certificate chained to a trusted authority and return to `true` whenever possible. This field
belongs only to `idrac`; never add it to an `ipmi` entry.

### Plugin `ipmi` (`devices[].params`)

The `ipmi` plugin runs one shell-free, read-only subprocess per attempt, equivalent to:

```text
ipmitool -I INTERFACE -H HOST -U USER -E -C CIPHER -N 2 -R 2 -v -S CACHE sdr get SENSOR_NAME
```

`CACHE` is a local copy of the controller's Sensor Data Repository under `/data/ipmi-sdr-cache/`. Without it,
`sdr get` walks the whole repository on every read, which took 6–37 s on a legacy iDRAC 6 and exceeded the request
timeout. The plugin builds the cache once with `ipmitool sdr dump` in the background, with its own 180 s bound. Until
that finishes, attempts report `communication` with the detail "building the local IPMI SDR cache". After that, a read
takes well under a second. The cache survives restarts. It is rebuilt when ipmitool reports it unreadable, or when the
sensor is missing from a cache older than 10 minutes. A failed build is retried at most once a minute. `-N 2 -R 2`
retransmits a lost datagram after 2 s rather than letting it consume the whole attempt. `-v` adds the RAKP detail that
separates rejected credentials from an unreachable or saturated controller; it never prints the password.

Arguments are passed directly, not through a shell. The image installs Alpine's `ipmitool` at a pinned package
version; no host-side executable or add-on package installation is required. A successful `Sensor Reading` must contain a finite value from `0` through
`100000` W.

| Path | Type | Required | Default | Range or form | Invalid-value behavior | Security implications |
| --- | --- | --- | --- | --- | --- | --- |
| `devices[].params.host` | String | Yes | None | `1`–`253` characters; management hostname or address passed to `ipmitool -H`. Must not start with `-` or contain whitespace, `@`, `?`, `#`, or an interior `/` | Rejected device when missing, empty, or not text. Session/routing/tool failures become runtime `read` failures. | Endpoint is visible in process arguments and diagnostics. Restrict UDP `623` to the Home Assistant host or management subnet. |
| `devices[].params.username` | Credential string | Yes | None | `1`–`64` characters; use `!secret` | Missing, blank, unresolved, wrong type, or out-of-range value rejects the device. Session rejection becomes `authentication` with no poll retry. | Credential-marked and log-redacted, but necessarily passed as the `-U` process argument. Use a non-sensitive account name and least privilege. |
| `devices[].params.password` | Credential string | Yes | None | `1`–`128` characters; use `!secret` | Missing, blank, unresolved, wrong type, or out-of-range value rejects the device. Session rejection becomes `authentication`. | Credential-marked and redacted. Passed only through child environment variable `IPMI_PASSWORD` with `-E`, never argv or failure detail. |
| `devices[].params.sensor_name` | String | No | `System Level` | `1`–`255` characters; exact SDR sensor name passed as one argument. Must not start with `-` or whitespace, nor contain control characters (a leading `-` would read as an `ipmitool` option) | Wrong type, empty, or overlength rejects the device. A sensor-not-found result is a response-format failure logged as `read`. | Visible in process arguments and may disclose hardware inventory; never use it to carry a credential. |
| `devices[].params.interface` | String | No | `lanplus` | `1`–`32` characters matching letters, digits, `_`, `.`, `+`, or `-` | Wrong type, malformed, empty, or overlength rejects the device. Unsupported interfaces fail at runtime as `read`. | Keep `lanplus` where supported for IPMI v2/RMCP+. Do not downgrade to a weaker interface merely to bypass a configuration problem. |
| `devices[].params.cipher_suite` | Integer | No | `3` | `0`–`17`, passed to `ipmitool -C` | Wrong type or out of range rejects the device. A controller that refuses the suite normally yields `authentication` or `read`. | Suite `3` is a legacy compatibility choice. Prefer a stronger mutually supported suite and isolate any device that requires suite 3. |
| `devices[].params.request_timeout_s` | Number of seconds | No | `10` in the plugin schema | `1`–`60`; explicit values may be fractional | Wrong type or out of range rejects the device. It bounds the process and supplies bounded `-N`; timeout/cancellation terminates, then kills if necessary, and reaps the child. If omitted, scheduler fallback is `min(interval / 3, 10)`. | Prevents a stalled child from persisting. Longer values consume more of the poll-cycle budget but do not expose the password. |

#### Enabling IPMI-over-LAN and cipher-suite risk

On legacy iDRAC, enable **IPMI Over LAN** in the controller's network/security settings; exact menu wording varies by
firmware. Permit UDP `623` only from the Home Assistant/add-on host or an isolated management subnet. Create a
dedicated account with only the channel privilege needed to establish a session and read SDR sensors. Verify that the
configured sensor exists and reports watts.

Cipher suite `3` is retained because it interoperated with the validated legacy controller, not because it is a modern
security recommendation. It uses legacy cryptographic choices. Never expose such a session to the internet or an
untrusted client network. If both controller and bundled `ipmitool` support a stronger suite, configure that suite
instead. Neither DCMI nor SNMP is required.

## Polling, retries, and timeouts

- Every accepted device has an independent task, plugin instance, interval, timeout, retry budget, and energy state.
  Devices are polled concurrently and one slow or broken device does not block another.
- The first poll starts immediately. Later deadlines remain anchored to the original schedule, so ordinary operation
  does not accumulate clock drift.
- At most one read is in flight per device. If a cycle runs through a later scheduled time, that occurrence is skipped
  and logged rather than queued or overlapped.
- One attempt is one Redfish GET or one `ipmitool sdr get` process. The effective `request_timeout_s` bounds each
  attempt. When the field is omitted, the scheduler uses one third of the device interval, capped at 10 seconds.
- `authentication` failures are not retried, reducing account-lockout pressure. Every other failure can receive two
  additional attempts in the same cycle, after 1 second and then 2 seconds.
- No retry starts when its delay plus the attempt's own timeout would reach the next poll deadline, so a cycle never
  overruns its interval. A successful attempt ends the cycle immediately.
  A cycle with no success publishes the device offline, adds no energy, and remains scheduled for its next interval.
- Startup connection validation performs one operation under the same effective timeout. Its result is diagnostic;
  failure does not remove the device from regular polling.

## MQTT inventory, availability, reconnect, and retain behavior

The schema-1 inventory advertises two availability levels to the companion integration:

- `<base_topic>/bridge/availability` describes the add-on's broker session and carries its last will.
- `<base_topic>/<device_id>/availability` describes the most recent completed poll cycle for that device.

An entity is available only when both are `online`. A failed cycle makes one device offline; an unexpected disconnect
makes every entity unavailable through the retained bridge last will. Graceful shutdown publishes each device and then
the bridge offline.

MQTT publication uses these retain rules:

| Payload | Retained | Reason |
| --- | --- | --- |
| Legacy power/energy discovery deletion (empty payload) | Yes | Removes generic MQTT-owned entities from older releases. |
| Versioned integration inventory | Yes | Preserves the exact schema-1 device/topic contract for the companion integration. |
| Bridge and device availability | Yes | Latest availability survives subscriber reconnects; bridge carries the last will. |
| Instantaneous power state | No | A stale watt value must not look current after reconnect. |
| Power diagnostic attributes | No | Operation duration belongs to one reading. |
| Cumulative energy state | Yes | The latest monotonic total is available after subscriber restart. |

A failed initial broker connection is nonfatal. Reconnect waits 1, 2, 4, 8, 16, 32, then 60 seconds indefinitely. On
every connection, all legacy deletions precede one inventory, and inventory precedes attributes, availability, and any
fresh buffered state. While disconnected, only the newest successful reading per device is held; it is replayed only
when no older than four times that device's interval.

## Energy integration, gaps, persistence, and restart

The first usable reading after process start establishes a baseline and leaves the cumulative total unchanged. Each
subsequent usable reading adds the mean of the previous and current watts multiplied by elapsed hours and divided by
1000. Contributions and totals are rounded to six decimal places and never decrease.

A failed poll contributes nothing. A non-finite/negative value or a timestamp that does not advance is discarded. If
the gap between usable readings exceeds four times the resolved device interval, the add-on does not invent energy for
the unknown period: the total is unchanged and the later reading becomes the new baseline. A gap exactly four times
the interval is still integrated.

State is stored atomically at `/data/energy_state.json`, which is on the add-on's persistent data volume. The
persistent-state policy flushes snapshots at least every 300 seconds and performs a final flush during normal
Supervisor stop/restart. On startup, a valid total is restored but its old reading is not reused as an active baseline;
the first new reading re-baselines without bridging the restart gap. A missing state file is a normal first start. A
malformed or negative entry resets only that device to `0 kWh` and logs why; other device totals survive.

An abrupt host loss can lose energy accumulated since the most recent completed snapshot. The retained MQTT energy
state helps Home Assistant display the last published total, but it is not used to reconstruct a damaged add-on state
file.

## Graceful shutdown

On `SIGTERM` or `SIGINT`, the add-on cancels polling, persists energy under a five-second bound, publishes retained
`offline` availability for every configured device, publishes the bridge offline, and disconnects. The complete
sequence has a ten-second deadline. If that deadline is exceeded, a warning is logged and remaining steps are
abandoned so Supervisor is not held indefinitely. A running IPMI child is terminated, escalated to kill after a short
grace when necessary, and reaped.

## Logs, levels, and classifications

Logs are one-line structured JSON records on standard output, as shown in the add-on Log tab. Records include a UTC
timestamp, one of `debug`, `info`, `warning`, or `error`, a message, and `device_id` for device-specific events.
Messages are bounded, and credential values/fragments are replaced before formatting.

The configured level emits that severity and higher in this order: `debug` < `info` < `warning` < `error`.

- `debug`: completed poll values, attempts, durations, and persistence detail.
- `info`: plugin discovery, restore/startup, broker connection, reconnect delay, and skipped-cycle events.
- `warning`: connection-validation failures, read classifications, broker/TLS failures, state rejection, and shutdown
  deadline issues.
- `error`: invalid/rejected configuration, startup-stage failure, malformed plugins, or an unexpected plugin defect.

Read failures expose exactly these operator-facing classifications:

| Classification | Meaning | Retry in the same poll cycle |
| --- | --- | --- |
| `authentication` | Redfish HTTP `401`/`403`, IPMI RAKP credential rejection, or broker credentials refused | No for device polling; MQTT reconnect continues |
| `timeout` | The request/process or scheduler attempt exceeded its bound | Yes, while retry and interval budgets remain |
| `tls_validation` | Redfish or MQTT certificate validation failed | Yes for device polling; MQTT reconnect continues |
| `read` | Communication, HTTP/tool error, missing/malformed response, invalid measurement, or plugin failure | Yes, while budgets remain |

## Hardware-validation observations

Both plugins have been validated against real controllers. The readings below are examples of the response
shapes to expect, not promised readings, defaults, health thresholds, or test expectations:

- iDRAC 7+ (`idrac`): Redfish returned a numeric `PowerConsumedWatts` from the default chassis power resource.
- Legacy iDRAC 6 (`ipmi`): DCMI was unsupported; IPMI v2/RMCP+ with `lanplus`, cipher suite `3`, returned
  `Sensor Reading : 112 (+/- 0) Watts` for `System Level`. Without the local SDR cache, a read took 6–37 s there.

Real power changes with workload, firmware, power state, sensor calibration, and time. A different live value is not by
itself an error.

## Troubleshooting

Start with the add-on Log tab and the `device_id`. A device rejected during configuration never reaches polling; a
device whose startup connection validation fails remains scheduled and may recover without restarting the add-on.

### No Home Assistant entities

Confirm that `custom_components/remote_energy_meter` is installed at the exact `/config` path shown above and that Home
Assistant Core was restarted afterward. Under **Settings → Devices & services**, confirm the discovered **Remote Energy
Meter** card; if Supervisor discovery is unavailable, add it manually with the configured MQTT base topic. Then verify
that the MQTT integration is loaded and the add-on has published retained schema-1 inventory. The add-on image never
copies or installs the companion integration itself.

### Redfish/iDRAC (`idrac`)

| Symptom/classification | Checks |
| --- | --- |
| `authentication` | Confirm the secret names/values, enabled account, least-privilege Redfish access, and controller lockout state. HTTP `401` and `403` are not retried. |
| `tls_validation` | Check controller time, hostname/address against the certificate, chain trust, and expiry. Prefer installing a trusted certificate; use `verify_tls: false` only as a conscious isolated-network exception. |
| `timeout` | Confirm route and TCP `443`, controller responsiveness, and `request_timeout_s`. A retry still has to fit within the poll interval. |
| `read` with HTTP/connection detail | Verify the management address, Redfish is enabled, firewall policy, and that no proxy/redirect is required; redirects are not followed. |
| `read` with resource/format detail | Confirm `power_path` identifies the chassis Power resource and its response contains numeric `PowerConsumedWatts` in `0`–`10000` W. |

### IPMI (`ipmi`)

| Symptom/classification | Checks |
| --- | --- |
| `authentication` | Confirm the secret names/values, IPMI-over-LAN channel access, account privilege, lockout state, `interface`, and mutually supported `cipher_suite`. Only RAKP-level rejections (`unauthorized name`, `RAKP 2 HMAC is invalid`) are classified this way. |
| `communication` / no IPMI session could be established | The controller did not answer session setup, or every session slot is taken. Legacy iDRACs have five; sessions orphaned by killed clients are held until the controller's idle timeout. Check `ipmitool session info all`, wait, or reset the iDRAC. |
| `communication` / building the local IPMI SDR cache | Expected for the first poll(s) after install or a cache rebuild. It clears once the background `sdr dump` completes. |
| `timeout` | Confirm UDP `623` routing/firewall, controller responsiveness, and `request_timeout_s`. The add-on cleans up the timed-out child. |
| `read` / sensor not found | Copy the exact SDR sensor name, including spaces and case used by the controller. `System Level` is a validated default, not a universal sensor name. |
| `read` / malformed measurement | The `Sensor Reading` line must be a finite `0`–`100000` watt value, optionally with a `(+/- ...)` uncertainty. Volts, `na`, and non-finite values are rejected. |
| `read` / executable unavailable | Update or rebuild the add-on image. `ipmitool` is bundled in the image; installing it on the Home Assistant host does not repair an old/broken image. |
| Session works only with suite 3 | Keep UDP `623` isolated and use a dedicated account. Suite 3 is legacy; test and select a stronger supported suite when possible. |

### Power works but energy pauses

- The first reading after start only establishes a baseline.
- All-failed cycles and unusable values add no energy.
- A gap longer than four poll intervals is intentionally excluded and re-baselined.
- Check persistence warnings if a restart returns a device to `0 kWh`.
- Use a shorter valid interval only after confirming it will not overload the controller or broker.
