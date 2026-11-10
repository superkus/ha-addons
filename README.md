# Remote Energy Meter

Remote Energy Meter is a Home Assistant add-on for metering network-attached devices. It polls each controller,
accumulates energy, and publishes power, cumulative energy, attributes, and availability over MQTT. The bundled
plugins support Dell iDRAC 7+ through Redfish (`idrac`) and legacy controllers through a generic IPMI SDR sensor
(`ipmi`). Neither path requires SNMP.

## Architecture

- Each accepted device has isolated polling, retry, availability, and energy state.
- MQTT publication preserves instantaneous power, retained cumulative energy, diagnostic attributes, per-device
  availability, and a bridge last will.
- On every broker connection, the add-on clears the two retained generic MQTT discovery configs created by older
  releases, then publishes one retained schema-1 inventory before any state or availability.
- After the publisher starts and before polling begins, the add-on makes one best-effort, credential-free Supervisor
  discovery announcement. Failure is warning-only and nonfatal.
- Broker and controller credentials remain in add-on options and are excluded from Supervisor and inventory payloads.

> **Companion integration included:** This repository ships `custom_components/remote_energy_meter` and a separate,
> deterministic local integration bundle builder. Building and validating artifacts never copies files into Home
> Assistant, restarts Core, deploys the add-on, or touches a live broker.

## Local add-on and companion installation

Build both deterministic local artifacts without deploying them:

```bash
python3 tools/build_local_addon_bundle.py \
  --output /private/tmp/remote-energy-meter-local-addon \
  --integration-output /private/tmp/remote-energy-meter-local-integration
```

For a separately approved installation, copy the integration to
`/config/custom_components/remote_energy_meter`, restart Home Assistant Core, copy the add-on artifact to
`/addons/remote_energy_meter`, then reload **Local add-ons** and install/start it. Open **Settings → Devices & services**
and confirm the discovered **Remote Energy Meter** card; use manual base-topic setup only if Supervisor discovery is
unavailable. Review each generated `LOCAL_INSTALL.md` first. GitHub/HACS is an optional later path; the local artifacts
are the primary installation route.

## Hardware validation

Both plugins have been validated against real Dell hardware: `idrac` against an iDRAC 7+ controller through
Redfish, and `ipmi` against a legacy iDRAC 6 through the `System Level` SDR sensor over IPMI v2/RMCP+ (`lanplus`,
cipher suite 3; DCMI was unsupported there). See the add-on's `DOCS.md` for details.

## Documentation

| Document | Audience |
| --- | --- |
| [`remote_energy_meter/DOCS.md`](remote_energy_meter/DOCS.md) | Add-on configuration, security, protocol preview, lifecycle, and troubleshooting. |
| [`docs/writing-a-plugin.md`](docs/writing-a-plugin.md) | Adding a device family. |
| [`docs/protocol/v1/`](docs/protocol/v1/README.md) | Native-reporter protocol v1 profile: wire rules, threat model, route manifest, schemas, and public vectors (draft, pending security review; no implementation ships here). |
| [`CHANGELOG.md`](CHANGELOG.md) | Release history. |

## Development

### Workflow (security first)

Every change, including urgent fixes, goes through the same path: a branch, tests, the local gates, a push to `dev`,
and a reviewed pull request into `main`. Nothing is patched directly on a running Home Assistant host. `main` is
protected on GitHub: no direct pushes or web edits (admins included), and every CI job below is a required check
before a PR can merge. CI (`.github/workflows/ci.yaml`) runs on every push and PR to `dev` and `main`:

| Job | Gate |
| --- | --- |
| `secrets` | gitleaks over the full history; any finding fails the run |
| `local-refs` | `tools/check_local_refs.py` over tracked files and the full history: no private IPs, home/volume paths, LAN hostnames, MAC or personal e-mail addresses |
| `lint` | `ruff check`, `ruff format --check`, integration-package validation |
| `typecheck` | `mypy --strict` |
| `test` | unit, property, and conformance suites with an 80% line+branch coverage gate |
| `integration` | `pytest -m integration` against a real Mosquitto |
| `audit` | `pip-audit --strict` of the hash-locked `requirements.txt` the image installs from; any advisory, or a lock that drifted from `pyproject.toml`, fails the run |
| `addon-lint` | Home Assistant add-on linter |
| `image` | amd64 and aarch64 image builds from the Dockerfile's pinned `FROM`, plus an amd64 smoke test |

Local hooks run the same gates before anything leaves your machine. Enable them once per clone:

```bash
brew install gitleaks                    # required: the hooks fail closed without it
uv venv -p 3.12 .venv && uv pip install --python .venv/bin/python -e '.[dev]'
git config core.hooksPath .githooks
```

- `.githooks/pre-commit` runs gitleaks and the local-setup reference scan on the staged changes, then lint,
  `mypy --strict`, the tests with the coverage gate, and `pip-audit`.
- `.githooks/pre-push` runs gitleaks over exactly the commits being pushed, and the local-setup reference scan over
  the full history (a push publishes every past version).

Secret scanning uses the default gitleaks ruleset with no allowlist (`.gitleaks.toml`). Reviewed false positives are
suppressed one finding at a time by commit-qualified fingerprint in `.gitleaksignore`. Never commit real credentials: use
Home Assistant `!secret` references.

This repository is public, so nothing may describe a real installation: examples use RFC 5737 documentation addresses
(`192.0.2.0/24`) and RFC 2606 names (`device.example`). Put your own personal terms (domain, hostnames, private
repository names) one per line in a `.local-refs-denylist` file at the repository root. It is gitignored, and the
hooks check it in addition to the generic rules. The full policy is in `.kiro/steering/security.md` and
`.kiro/steering/workflow.md`.

Dev tools are pinned to exact versions in `pyproject.toml`, so CI and the hooks run identical tools. Runtime
dependencies are declared as ranges in `pyproject.toml` and resolved into the hash-locked `requirements.txt` that the
Dockerfile installs with `--require-hashes`. After changing a runtime dependency, regenerate the lock:

```bash
uv pip compile pyproject.toml --python-version 3.12 --python-platform linux --generate-hashes -o requirements.txt
```

`ipmitool` is pinned to an exact Alpine package, GitHub Actions to commit SHAs, and the base image to a digest in
the Dockerfile's single multi-architecture `FROM` line (`tag@sha256:...`), which is what Supervisor builds from. CI
requires exactly one digest-pinned `FROM`, fails if that tag no longer resolves to the pinned digest, and fails if the
tag's Python minor differs from `PYTHON_VERSION` in `ci.yaml` (3.12). Dependabot opens weekly bump PRs for actions and
pip against `dev`; for the base image it proposes only rebuilds of the same Python minor and Alpine release.
Python-minor and Alpine bumps of the base, like anything else, are deliberate PRs of their own.

### Commands

```bash
uv venv -p 3.12                          # CI runs Python 3.12
uv pip install -e '.[dev]'
ruff check
ruff format --check
mypy --strict
pytest -m "not integration" --cov        # unit, property, conformance + coverage gate
pytest -m integration                    # needs a mosquitto binary on PATH
pip-audit --strict --no-deps --disable-pip -r <(pip freeze --exclude-editable)
gitleaks git --config .gitleaks.toml --redact .
python tools/check_local_refs.py --history
PYTHONDONTWRITEBYTECODE=1 pytest -p no:cacheprovider tests/conformance/test_protocol_v1_artifacts.py   # profile checksums and public P-256 signature verification
```

The Python backend package lives in `src/remote_energy_meter/`; the add-on folder contains Supervisor/container
packaging. Building the add-on image uses the repository root as build context and copies only backend and add-on
runtime inputs. `BUILD_ARCH` and `BUILD_VERSION` are required (Supervisor and CI pass them; the build fails without
both):

```bash
docker buildx build -f remote_energy_meter/Dockerfile --build-arg BUILD_ARCH=amd64 --build-arg BUILD_VERSION=dev .
```

### Protocol v1 profile

`docs/protocol/v1/` is the server-owned source of truth for the native-reporter, control, and MQTT v2 wire contract.
Once the reporter repository implements this profile, it must consume the files byte-for-byte at a pinned commit,
verify `vectors/manifest.json`, and never edit the files there. The profile is a draft until the independent security
review (spec task 3.4) approves it. No protocol implementation is included in this repository; tasks 3.2 and 3.3 and
reporter-repository consumption remain future work.

- `tests/conformance/test_protocol_v1_artifacts.py` checks the exact file set, the 30-route manifest, closed schemas,
  the RFC 9457 code/status mapping, vector structure and mutation declarations, the absence of private-key members, the
  SHA-256 checksums of every listed vector and contract file, and every public P-256 signature and high-S twin. It uses
  runtime-pinned `idna` for strict IDNA2008 checks and test-only `cryptography` for public-key verification. It does not
  execute the declared mutations against protocol implementations.
- Vectors are public, non-secret, test-only fixtures. They include public verification keys and public test-only
  symmetric values such as pairing credentials, tokens, and an MQTT key, but no named private signing-key members. The
  reported ephemeral-key generation and private-key disposal are maintainer-supplied provenance, not facts this
  repository can prove without the generator or an audit record; see the profile README. Never commit a private key,
  and never reuse a fixture value outside tests.
- After any change under `docs/protocol/v1/`, recompute the checksums in `vectors/manifest.json` last. Once version 1
  is approved its published bytes are frozen; incompatible changes go to a new `docs/protocol/v2/`.

## License

MIT.
