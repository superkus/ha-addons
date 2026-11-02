# Security and privacy — MANDATORY for this repository

> **SECURITY IS THE #1 PRIORITY.** It takes precedence over features, convenience, speed, and
> aesthetics. When a trade-off arises, the more secure option wins; when in doubt, choose the
> more restrictive option and surface the trade-off to the user.

This repository is **public**. Everything committed, including every past version of every file
and every commit message, is published. Treat each commit as a publication.

## Secrets

- Never commit credentials, tokens, keys, certificates, or `.env` files. Configuration examples use
  Home Assistant `!secret` references; real values live only in `secrets.yaml` / add-on options on
  the host.
- Passwords reach `ipmitool` only through `IPMI_PASSWORD` + `-E`, never argv, logs, MQTT payloads,
  or failure details. Keep it that way; the redaction tests are the guard.
- gitleaks runs on staged changes (`.githooks/pre-commit`), on the commits being pushed
  (`.githooks/pre-push`), and over the full history in CI (`secrets` job). `.gitleaks.toml` uses the
  default ruleset with **no allowlist**. A reviewed false positive is suppressed in `.gitleaksignore`
  by its commit-qualified fingerprint (`<commit>:<file>:<rule>:<line>`), with a comment saying why —
  never by a path-only fingerprint (it hides the same line in every later commit) and never by a
  regex or path allowlist, which can hide real secrets. Before suppressing, prove the value is not a
  real credential.

## No references to anyone's local setup

Never commit anything that describes a real installation:

- private, CGNAT, or link-local IPv4 addresses (`10/8`, `172.16/12`, `192.168/16`, `100.64/10`,
  `169.254/16`) and MAC addresses;
- home-directory, mounted-volume, or network-share paths (`/Users/<name>`, `/home/<name>`,
  `/Volumes/<share>`, NFS mounts);
- LAN hostnames (`*.local`, `*.lan`, `*.internal`, `*.home`, `*.home.arpa`), real public hostnames
  or domains of an installation, real host or device names, usernames, and personal e-mail
  addresses;
- names of private repositories or other private projects.

Use instead: RFC 5737 documentation addresses (`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`),
RFC 2606 reserved names (`device.example`, `example.com`, `noreply@example.invalid`), RFC 7042
documentation MACs (`00:00:5e:00:53:xx`), and neutral device IDs (`server_a`, `legacy_bmc`).

This applies to code, tests, docs, specs, changelog entries, and **commit messages**. Debugging a
live system is fine; writing its addresses, hostnames, or device names into the repository is not.

`tools/check_local_refs.py` enforces the generic rules in CI (`local-refs` job, tracked files and
full history) and in the hooks. Personal terms (own domain, hostnames, private repo names) go in the
gitignored `.local-refs-denylist` at the repository root — never into a committed rule, which would
publish the very term it forbids. A line that must describe a rule opts out visibly with the marker
`local-refs: allow`; use it only in the guard and its tests.

## Dependencies and supply chain

- Dev tools are pinned to exact versions in `pyproject.toml`; bump them deliberately in their own PR.
- `pip-audit --strict` blocks on any known vulnerability: the runtime set in CI (`audit` job), the
  whole dev environment in the pre-commit hook. Fix at the source with a targeted pin; do not
  suppress, downgrade a framework, or disable the gate. Dev-only advisories are fixed too.
- GitHub Actions use maintained, current major versions; jobs get least-privilege `permissions`.

## Live systems

Never fix a problem by patching a running Home Assistant installation. Investigating it (reading
logs, MQTT state, timing a read) is fine and often necessary; the fix itself goes through the
repository workflow in `workflow.md` and reaches the host only from a reviewed, merged change.
