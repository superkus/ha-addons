# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Added pure Python protocol-v1 primitives for strict, allocation-bounded canonical base64url and JSON, bounded
  transcripts, public P-256 JWKs and RFC 7638 identities, RFC 9530 Content-Digest, low-S P1363 verification, and the
  narrow reporter/control RFC 9421 profiles. Verification handles HTTP field names case-insensitively, uses trimmed
  field values in the signature base, rejects ambiguous framing metadata, binds active/proposed keys by signature role,
  and returns both the trusted stable identity and rotatable signing key IDs with an immutable snapshot of the exact
  request for later replay and business-state layers; no listener, route handler, state, or business logic is included.
- Added executable unit, property, and vector-backed conformance coverage for the Python primitives, including every
  fixed signature, high-S twin, primitive-layer one-field HTTP mutation, every PGP word-list index in all six SAS
  positions, strict phrase tamper rejection, and wheel package-data inclusion.
- Added the server/Python half of the six-word interactive-enrollment SAS: transcript-hash-salted HKDF-SHA-256,
  alternating exact Even/Odd rendering, strict six-word parsing, and constant-time authentication-byte comparison.
  Vendored the two RFC-cited 256-entry ASCII PGP word lists as isolated GFDL 1.2 data with source-byte and list hashes,
  complete license and attribution, reviewed extraction details, and pinned PGPfone/thblt comparison results. Wheel
  metadata and the add-on's OCI license label identify both the MIT and GFDL-1.2-only portions of the shipped artifact.
  The published interactive vector now contains its exact rendered phrase, and the Rust reporter reproduces it from
  the same pinned tables.
- Added Rust-to-Python interoperability conformance for protocol v1: a reporter request signed by the Rust reporter
  verifies in Python with the same stable identity, signature input, and signature base; every one-byte mutation of
  its request bytes is rejected with the exact code pinned for its field, every one-byte mutation of its signature or
  public key is rejected, and a substituted public key is rejected. The fixture is pinned by reporter commit, SHA-256,
  and expected identity. With the reporter's Rust implementation passing the primitive-layer subset of the same
  server-owned vectors, specification Tasks 3.2 and 3.3 are complete at the primitive layer; the declared
  `interactive.sas` vector mutations are not yet executed by either implementation. The profile remains a
  draft that must not be distributed until the Task 3.4 independent security review approves it.
- Added the fail-closed storage foundation for the upcoming reporter and control security state
  (`remote_energy_meter.secure_store`); nothing uses it yet. Store directories must be real directories owned by the
  add-on user with mode exactly `0700`, and documents regular files with mode exactly `0600`; symlinks, FIFOs, foreign
  owners, other modes, oversize documents, non-canonical or foreign-schema envelopes, and documents from another store
  are rejected and never repaired. Each document is a canonical-JSON envelope whose generation rises by exactly one per
  save; a running store detects a rolled-back, vanished, or concurrently written document before writing. A save
  whose directory sync fails is reported as failed. Added a strict public canonical-JSON renderer for protocol v1
  whose output the strict parser always accepts unchanged.

### Changed

- The energy state file is now written through a shared crash-safe writer (`remote_energy_meter.atomic_file`) that the
  upcoming reporter PKI, roster, and token stores will reuse instead of a parallel writer. Behaviour is unchanged: an
  exclusive owner-only temporary file in the same directory, flushed and synced, atomically renamed over the
  document, best-effort directory sync, and no leftover temporary file on failure.
- The shared writer now sets each document's mode to exactly `0600` before writing it, so an unusual process umask can
  no longer leave the energy state file with fewer owner permissions; group and others still never have access.
- Corrected the draft protocol-v1 reporter signature profile to omit client wall-clock parameters entirely. Reporter
  `Signature-Input` now contains exactly `keyid`, `alg`, and `tag`; freshness remains exclusively bound to server-issued
  pending-enrollment, challenge, session, and sequence state. Regenerated the affected public reporter HTTP vectors
  with in-memory ephemeral P-256 keys and replaced impossible one-member query reorder mutations with executable
  duplicate-member rejection cases.

### Security

- Added exact runtime pins for `cryptography` 50.0.0 and `idna` 3.20 and included them in the hash-locked, audited add-on
  dependency set.

## [0.1.3]

### Added

- The Dockerfile fails the build when `BUILD_ARCH` or `BUILD_VERSION` is not passed. Supervisor and CI always pass
  both; a hand-built image must pass them too (see the README). Before, a missing argument expanded to an empty string
  and the image was labelled with an empty architecture and version.
- CI accepts exactly one `FROM` in the Dockerfile, requires it to be a bare `image:tag@sha256:...` reference, and fails
  if the tag's Python minor differs from the project's 3.12 (`PYTHON_VERSION` in `ci.yaml`). Unit tests check the same
  pin from the pre-commit gate, and that the `arch` list in `config.yaml` matches the CI image matrix.

### Changed

- The image is now built from the Dockerfile alone. Supervisor has deprecated `build.yaml` (it still reads one when
  present, with a warning, and passes `BUILD_FROM` only to add-ons that ship one); the old `ARG BUILD_FROM=...amd64...`
  default meant every architecture would have fallen back to the amd64 base once `build.yaml` goes away. The Dockerfile
  now has one multi-architecture `FROM` (`ghcr.io/home-assistant/base-python:3.12-alpine3.22-2026.10.3@sha256:...`),
  pinned by digest in the source where Supervisor reads it, and carries the OCI labels `build.yaml` used to supply. CI
  reads the pin from the Dockerfile and fails if the tag stops resolving to it. Dependabot proposes base-image rebuilds
  (same Python minor and Alpine) and ignores Python-minor and Alpine bumps, which stay deliberate PRs.
- A release bumps only `remote_energy_meter/config.yaml` and `pyproject.toml`; the image version (`BUILD_VERSION`)
  comes from Supervisor or CI, no longer from a default in the Dockerfile.

### Removed

- `armv7`. Home Assistant ended support for it in 2025.12, the official base image is no longer published for it
  (last build 2025.11), and the multi-architecture base covers only amd64 and aarch64. The add-on linter had warned
  about it since the first CI run. An existing armv7 installation is not offered this update and keeps running 0.1.2;
  to move it to a supported host, take a Supervisor backup of the add-on first so the persisted energy counters
  (`/data/energy_state.json`) are carried over.
- `remote_energy_meter/build.yaml`.
- The OCI labels `org.opencontainers.image.revision` and `org.opencontainers.image.created`: Supervisor has never
  supplied `BUILD_REF` or `BUILD_DATE` (it passes only `BUILD_FROM`, `BUILD_VERSION`, and `BUILD_ARCH`; those two
  arguments are a Community Add-ons base-image convention filled in by that project's own CI), so the labels were
  always empty. `org.opencontainers.image.source` is now a fixed value instead of `BUILD_REPOSITORY`.

## [0.1.2]

### Security

Hardening from a code review. None of these was exploitable as shipped, but each shrinks what a compromised controller,
library, or `ipmitool` could reach.

- `ipmitool` no longer inherits the add-on's environment. The child receives only `PATH`, `LC_ALL`, and
  `IPMI_PASSWORD`; before, it also saw `SUPERVISOR_TOKEN` and every other variable of the process.
- iDRAC Redfish responses are capped at 1 MiB (declared or actual size); larger bodies are refused as
  `response_format` instead of being buffered for the whole request timeout.
- `host` (both plugins) must not start with `-` or contain whitespace, `@`, `?`, `#`, or an interior `/`, and
  `sensor_name` must not start with `-` or whitespace or contain control characters. A permuting `getopt` (glibc) would
  otherwise read such a value as an `ipmitool` option, and a URL authority would carry smuggled userinfo or a path.
  Hostnames, IPv4/IPv6 literals, ports, and the documented `https://` prefix remain accepted.
- The SDR cache staging file is now created by the add-on with `O_EXCL` (mode 0600) before `ipmitool` writes into it,
  and is checked not to be a symlink before it replaces the cache.
- `display_name` is capped at 128 characters (it is published verbatim in the retained MQTT inventory).
- `DOCS.md` now states plainly what the add-on can reach (root in its container, the whole Home Assistant
  configuration directory read-only for `!secret` resolution) and why, and how to run without that mount.
- Supply chain: the image now installs its Python dependencies only from a hash-locked `requirements.txt`
  (`pip install --require-hashes`, then the package with `--no-deps`), so what CI audits is byte-for-byte what the
  user's host builds; a mismatched or unlisted wheel fails the build. `ipmitool` is pinned to the exact Alpine package
  (`1.8.19-r1`), and every GitHub Action is pinned to a commit SHA. The Supervisor schema only allows a tag in
  `build.yaml`, so CI pins the three base images by digest itself and fails when the tag no longer resolves to that
  digest. CI's `audit` job scans the lock and fails if it drifts from `pyproject.toml`. A new `.github/dependabot.yml`
  proposes weekly bumps for actions and pip, targeting `dev`.

### Changed

- `main` is protected on GitHub: pull requests only (admins included), all 11 CI jobs are required checks on an
  up-to-date branch, and force pushes and deletion are blocked. `.kiro/steering/workflow.md` records the
  configuration and the rule to update required checks whenever a CI job is renamed.

## [0.1.1]

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
- The project moved to its own repository, `superkus/remote_energy_meter`: one repository per add-on, grouped by
  the `ha-addons` GitHub topic. Repository, add-on, and integration documentation links point there; the old
  `superkus/ha-addons` URL redirects.
- `startup` and `boot` are no longer set in the add-on manifest: they restated Supervisor's defaults
  (`application`, `auto`), which the add-on linter rejects. Start order and boot behaviour are unchanged.

### Fixed

- IPMI readings timed out on legacy iDRAC 6. `sdr get` without a local SDR cache walked the whole repository on
  every read (6–37 s against a 10 s budget). The plugin now dumps the repository once, in the background, into
  `/data/ipmi-sdr-cache/` and reads with `-S`, which takes well under a second.
- `ipmitool -N` was set to the whole request timeout, so one lost datagram consumed the entire attempt. It is now
  `-N 2 -R 2`, and the plugin ends its own child 0.5 s before the caller's deadline so the child is reaped and the
  failure classified rather than cancelled from outside.
- IPMI session-setup failures were reported as `authentication`. ipmitool prints the same "Unable to establish IPMI
  v2 / RMCP+ session" for an unreachable controller and for one whose session slots are exhausted (by sessions
  orphaned by the timeouts above). Only RAKP-level rejections, made visible with `-v`, are now `authentication`;
  everything else is `communication` with a specific detail, and is retried.
- Poll cycles overran their interval and caused "poll cycle skipped" records. The retry budget ignored the retry's
  own timeout, so a 30 s interval with a 10 s timeout started a third attempt at ~21 s that ran to ~33 s. A retry
  now starts only when its delay plus its timeout fits before the next deadline.
- Graceful shutdown now closes each plugin-owned async resource exactly once, including after partial startup, while
  isolating cleanup failures so one device cannot prevent sibling cleanup.
- Structured logging now tolerates malformed third-party exception metadata without losing the JSON log record or
  bypassing credential redaction.
- Fixed a `RUF005` lint finding in `docs/icon-candidates/generate_candidates.py` reported by current ruff releases.
- A plain `pytest` run wrote bytecode into `custom_components/`, which made the integration-package validator
  reject the package; the suite now never writes bytecode. CI lets the test broker read its generated
  configuration when the runner's AppArmor profile confines mosquitto.

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
