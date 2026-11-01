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

## Branding

The user selected the original **Pulse Dial** candidate. The add-on artifact carries `icon.png` (128×128) and
`logo.png` (250×100) beside `config.yaml` as Supervisor presentation metadata; the Dockerfile does not copy them into
the container image. The separately installed companion integration carries `brand/icon.png`, `brand/icon@2x.png`,
`brand/logo.png`, and `brand/logo@2x.png`, which Home Assistant Core 2026.3 and newer serves as local integration
branding without a manifest icon field.

After replacing the integration, restart Core and refresh the browser if the Devices & services tile is cached. After
replacing the add-on source, reload **Local add-ons**, rebuild the add-on, and refresh the browser. No brand asset is
published to GitHub or `home-assistant/brands`, and the add-on never installs the companion integration.

## Hardware validation

Both plugins have been validated against real Dell hardware: `idrac` against an iDRAC 7+ controller through
Redfish, and `ipmi` against a legacy iDRAC 6 through the `System Level` SDR sensor over IPMI v2/RMCP+ (`lanplus`,
cipher suite 3; DCMI was unsupported there). See the add-on's `DOCS.md` for details.

## Documentation

| Document | Audience |
| --- | --- |
| [`remote_energy_meter/DOCS.md`](remote_energy_meter/DOCS.md) | Add-on configuration, security, protocol preview, lifecycle, and troubleshooting. |
| [`docs/writing-a-plugin.md`](docs/writing-a-plugin.md) | Adding a device family. |
| [`CHANGELOG.md`](CHANGELOG.md) | Release history. |

## Development

```bash
uv venv                                  # or: python3 -m venv .venv
uv pip install -e '.[dev]'
ruff check src tests tools
ruff format --check src tests tools
mypy
pytest -m "not integration"              # unit, property, and conformance
pytest -m integration                    # needs a mosquitto binary on PATH
```

The Python backend package lives in `src/remote_energy_meter/`; the add-on folder contains Supervisor/container
packaging. Building the add-on image uses the repository root as build context and copies only backend and add-on
runtime inputs:

```bash
docker buildx build -f remote_energy_meter/Dockerfile \
    --build-arg BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.12-alpine3.22 .
```

## License

MIT.
