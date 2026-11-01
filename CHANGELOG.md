# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- User-selected original **Pulse Dial** branding: `icon.png`/`logo.png` as Supervisor presentation metadata at the
  add-on source root, plus the exact local integration brand set under
  `custom_components/remote_energy_meter/brand/` for Home Assistant Core 2026.3 and newer. Candidates 2–4 remain
  unselected and no artwork is published to GitHub or `home-assistant/brands`.
- Strict binary-aware integration package validation for only the four documented local-brand PNG names, including
  PNG structure, 8-bit RGBA format, exact dimensions, checksums in built manifests, and rejection of malformed or
  unexpected binary files.
- A retained schema-versioned, credential-free companion protocol at `<base_topic>/integration/devices` and a
  best-effort Supervisor discovery announcement.
- The separate `custom_components/remote_energy_meter` companion integration with one-entry Supervisor/manual config
  flows, a push-only MQTT coordinator, dynamic power and energy sensors, strict payload validation, and diagnostics.
- A deterministic local integration directory/zip builder plus package validation for manifest metadata, synchronized
  strings/translations, compilable Python, and absence of credential material.
- Add-on manifest discovery declaration for service `remote_energy_meter`; announcement failures are credential-safe,
  warning-only, and do not stop MQTT or polling.
- Generic `ipmi` sensor plugin for legacy controllers, using one bounded `ipmitool sdr get` process per operation with
  environment-only password delivery, classified failures, and cancellation-safe child cleanup.
- Default legacy iDRAC 6 entry for `192.0.2.20`, while retaining the existing iDRAC 7+ Redfish entry for
  `192.0.2.9`.
- Complete standalone add-on documentation with secret-only Redfish and IPMI configurations, an exhaustive
  schema-derived field reference, companion-integration installation guidance, protocol/security guidance, lifecycle
  semantics, and plugin-specific troubleshooting.
- The repository is public, so it must not describe any real installation. New `tools/check_local_refs.py` rejects
  private, CGNAT and link-local IPv4 addresses, MAC addresses, home-directory and mounted-volume paths, LAN hostnames,
  and personal e-mail addresses. It runs in CI (`local-refs` job, tracked files and full history), in the pre-commit
  hook (staged changes) and in the pre-push hook (full history). Personal terms are checked from a gitignored
  `.local-refs-denylist`.
- Repository steering files `.kiro/steering/security.md` and `.kiro/steering/workflow.md` make security the first
  priority and define the branch, test, gate and PR workflow.

### Changed

- Every MQTT connection now removes both retained legacy generic MQTT discovery configs per device before publishing
  retained inventory, bridge/device availability, power, energy, and attributes. The add-on no longer publishes
  non-empty generic MQTT Discovery JSON.
- The add-on and companion integration remain separate artifacts; build and validation workflows do not install them,
  restart Home Assistant, deploy the add-on, or access a live broker.
- Integration ownership is declared as `@superkus`, and repository/documentation links now target
  `superkus/ha-addons`.
- Examples, defaults, documentation, specs, and tests use RFC 5737 documentation addresses and RFC 2606 reserved
  names, so nothing in the repository describes a real installation.
- CI now follows a security-first model. It runs on pushes and PRs to `dev` and
  `main`, and adds a gitleaks secret scan over the full history, a blocking `pip-audit` of the runtime dependencies, an
  80% coverage gate, and an amd64 image smoke test (ipmitool present, application imports). GitHub Actions moved to
  their current major versions.
- New `.githooks/pre-commit` (gitleaks on staged changes, then lint, types, tests with coverage, and audit) and
  `.githooks/pre-push` (gitleaks over the commits being pushed). Both fail closed when gitleaks is missing.
- Dev tools are pinned to exact versions. Open ranges let a new ruff release turn an unchanged tree red.
- pytest 8.4.2 → 9.2.0 (CVE-2025-71176 / PYSEC-2026-1845, vulnerable tmpdir handling, fixed in 9.0.3), with
  pytest-asyncio 0.26.0 → 1.4.1.post1. Dev-only: the add-on image never contained pytest.

### Fixed

- Graceful shutdown now closes each plugin-owned async resource exactly once, including after partial startup, while
  isolating cleanup failures so one device cannot prevent sibling cleanup.
- Structured logging now tolerates malformed third-party exception metadata without losing the JSON log record or
  bypassing credential redaction.
- Fixed a `RUF005` lint finding in `docs/icon-candidates/generate_candidates.py` reported by current ruff releases.

### Removed

- `tools/run_tests_safely.py`, `tools/LOCAL_TESTING.md`, and the network-mount refusals in the bundle builders, test
  fakes and hooks. They existed only for one development machine's network-mounted checkout; the tests run directly
  in any local Python 3.12 environment and in CI. The builders still require an absolute, non-symlink output under the
  home or temp directory.
- Kiro's per-machine `tasks.meta.json` execution history is no longer tracked.

## [0.1.0]

Initial release.

### Added

- Home Assistant addon that meters power of network-attached devices on a configurable interval and publishes
  instantaneous power (W) and cumulative energy (kWh) through MQTT discovery, one Home Assistant device with two
  sensors per configured device.
- Plugin architecture: a device family is supported by a module present in `remote_energy_meter.plugins`, discovered at
  startup without any change to the metering core. See `docs/writing-a-plugin.md`.
- `idrac` plugin: reads chassis power from the Dell iDRAC Redfish API, with per-device host, credentials, TLS
  verification, resource path, and request timeout.
- Reference conformance suite shipped inside the package as `remote_energy_meter.plugins.conformance`, for authors to
  run against their own plugin.
- Energy accumulation persisted under `/data`, so cumulative totals survive an addon or Home Assistant restart.
- Per-device failure isolation: an invalid device entry or an unreachable device degrades that device only, with a
  classified log record naming it.
- Credential redaction across the log stream and MQTT payloads, and `!secret` resolution against the Home Assistant
  `secrets.yaml`.
