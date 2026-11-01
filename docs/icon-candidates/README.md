# Remote Energy Meter branding candidates

> **Candidate archive:** Candidate 1, **Pulse Dial**, is the user-selected system. Only its generated deployment PNGs
> are promoted into the add-on and companion-integration source trees; the editable source and all four candidate
> systems remain here for provenance and comparison. Nothing in this directory is itself installed or deployed.

All four concepts are **original artwork created for Remote Energy Meter** from geometric vector primitives. They do
not copy Home Assistant, vendor, Material Design Icons, or other third-party/trademark artwork.

## Candidate comparison

| # | Concept | Primary visual | Best quality | Trade-off |
| --- | --- | --- | --- | --- |
| 1 | **Pulse Dial** | Calibrated dial plus energy bolt | Fastest recognition as an energy meter at 32–128 px | Less explicit about servers/networking |
| 2 | **Server Current** | Three-bay server plus live trace | Most specific to the current remote-server use case | More detail at the smallest size |
| 3 | **Orbit Meter** | Network nodes around a gauge | Strongest expression of one hub monitoring remote endpoints | Abstract compared with a bolt or server |
| 4 | **Watt Link** | Connected power waveform | Simplest telemetry/network silhouette | Reads as general monitoring before it reads as a meter |

**Selected by the user:** candidate 1, **Pulse Dial**. Its gauge-and-bolt silhouette communicates both measurement
and energy without tying the application to one device family, and it remains clear in the 32 px preview. This records
the explicit user choice; candidates 2–4 remain unselected comparison artwork and are not promoted.

## Staged deliverables

The contact sheet is [`contact-sheet.png`](contact-sheet.png). Each candidate directory has this same structure:

```text
NN-concept-name/
├── source/
│   ├── icon.svg                 # editable 512x512 vector source
│   └── logo.svg                 # editable 1280x512 vector source
├── addon/
│   ├── icon.png                 # 128x128 RGBA
│   └── logo.png                 # 250x100 RGBA
└── integration/brand/
    ├── icon.png                 # 256x256 RGBA
    ├── icon@2x.png              # 512x512 RGBA
    ├── logo.png                 # 640x256 RGBA; shortest side 256
    └── logo@2x.png              # 1280x512 RGBA; shortest side 512
```

Concrete candidate roots:

- `01-pulse-dial/`
- `02-server-current/`
- `03-orbit-meter/`
- `04-watt-link/`

`generate_candidates.py` regenerates the assets and contact sheet from normalized geometry, then performs validation.
`validation-report.json` records dimensions, mode, alpha range and bounds, byte size, SHA-256, SVG view boxes, and
32 px light/dark preview metrics for every candidate.

## Current official Home Assistant findings

### Supervisor add-on / app

The official [app presentation documentation](https://developers.home-assistant.io/docs/add-ons/presentation) places
`icon.png` and `logo.png` beside the add-on's `config.yaml`. Both are PNG. The icon must be square and 128x128 is the
recommended size; the recommended logo is about 250x100, while another sensible landscape ratio is allowed. The
published presentation contract does not document dark-specific add-on filenames or a local-image cache TTL, so these
candidate sets deliberately provide only the documented `icon.png` and `logo.png` pair. The artwork uses a transparent
canvas around an opaque, high-contrast plate that works on both light and dark UI backgrounds.

The official [local app testing guide](https://developers.home-assistant.io/docs/add-ons/testing) says remotely tested
local apps are copied to a subdirectory of `/addons`. After a future selected asset is copied, the existing project
installation flow should reload/check the Local Add-ons store and refresh the browser so Supervisor re-reads the app
folder. No live copy, reload, rebuild, or restart was performed in this task.

### Custom integration tile in Devices & services

The current [integration file-structure documentation](https://developers.home-assistant.io/docs/creating_integration_file_structure)
and [brand-image documentation](https://developers.home-assistant.io/docs/core/integration/brand_images/) allow a custom
integration on Home Assistant **2026.3 or newer** to ship images directly in
`custom_components/remote_energy_meter/brand/`. Local images take precedence over the Brands service and require no
manifest field. Supported names include `icon.png`, `icon@2x.png`, `logo.png`, `logo@2x.png`, and optional `dark_`
versions.

The official [`home-assistant/brands` guidance](https://github.com/home-assistant/brands/) requires PNG assets, square
256x256 and 512x512 icons, and landscape logos whose shortest side is 128–256 px normally and 256–512 px at high DPI.
Transparency and lossless web optimization are preferred; dark variants are optional. The staged regular assets use
an opaque navy plate plus bright outline on a transparent canvas and passed light/dark checks, so duplicate dark files
would add no visual benefit. SVG is retained only as editable source and is not a deployable Home Assistant brand file.

For Brands-service/CDN assets, the official guidance documents browser caching for seven days, edge caching for one
day, and cache flushing on each major Core release. Local custom-integration files are served before that fallback via
the local Brands API. The repository's installation flow already restarts Core after replacing a custom integration;
a browser refresh should follow. On Core older than 2026.3, local `brand/` is unsupported and integration branding
would instead require a contribution under `custom_integrations/remote_energy_meter` in the Brands repository.

The [2026.3 local-brand announcement](https://developers.home-assistant.io/blog/2026/02/24/brands-proxy-api) corroborates
the current documentation and lists the same local filenames. It is supplementary; the current reference pages above
are the primary basis for this staging plan.

_Content from official sources was rephrased for compliance with licensing restrictions._

## Packaging boundary in this repository

- The selected add-on pair is promoted to `remote_energy_meter/icon.png` and `remote_energy_meter/logo.png`.
- `tools/build_local_addon_bundle.py` copies that explicit pair to the bundle root beside `config.yaml`; its Dockerfile
  still copies only Python/package files and `rootfs`, so these Supervisor presentation files do not enter the image.
- The selected integration set is promoted only to `custom_components/remote_energy_meter/brand/`, which Home
  Assistant Core 2026.3 and newer serves as local integration branding without a manifest icon field.
- `tools/build_local_integration_bundle.py` copies the complete custom-integration directory into its separate artifact.
  `tools/validate_integration_package.py` allowlists only the four documented local-brand PNG names, validates their
  PNG structure, RGBA format, and exact dimensions, and continues credential scanning and compilation for text files.
- No asset was published to GitHub or `home-assistant/brands`, and candidates 2–4 remain only under this archive.

## Validation performed

Generation uses RGBA canvases, 4x supersampling, Lanczos downscaling, and lossless PNG compression. Validation fails on
an incorrect signature, dimensions, color mode, missing transparent/opaque pixels, invalid SVG XML/view box, a
collapsed 32 px silhouette, or insufficient luminance range on light or dark backgrounds. The contact sheet shows the
128, 64, and 32 px icon previews plus 250x100 logos in both themes.

The generated PNG encoder is non-interlaced (`IHDR` interlace method 0). Interlacing is preferred, not required, by the
Brands guidance; all files remain lossless and optimized. See `validation-report.json` for per-file evidence.

## Selected promotion and deployment scope

Candidate 1 is the only selected system. Its two add-on PNGs are copied to the add-on source root and its four
integration PNGs are copied to the local `brand/` directory. The two deterministic artifacts remain separate:
`custom_components` never enters the add-on bundle or image, and add-on Supervisor metadata never enters the
integration artifact.

Installation into `/addons` and `/config/custom_components`, a Core restart, Add-on Store reload, forced add-on
rebuild, or any publication is a separate production action requiring explicit approval and rollback backups. Browser
refresh may still be needed after deployment because the frontend can retain previously fetched images. The preview
gallery under `/config/www/remote-energy-meter-icon-candidates` should remain until the user visually confirms the
selected artwork.
