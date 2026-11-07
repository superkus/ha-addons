# Delivery workflow — MANDATORY for this repository

Security comes first (see `security.md`). Every change, including an urgent fix, takes this path:

1. **Branch.** Work on `dev` (or a branch off it). `main` only receives reviewed PR merges.
2. **Understand before changing.** Reproduce the problem and measure it where possible. Record the
   evidence in the commit message and changelog in generic terms (no addresses, hostnames, or device
   names — see `security.md`).
3. **Change with tests.** A bug fix gets a regression test that fails without the fix; a feature gets
   unit tests, and property/conformance tests where the area already has them. When a change makes
   an existing test wrong, update the test's model of the behaviour, never weaken the assertion.
4. **Update docs.** `remote_energy_meter/DOCS.md` for user-facing behaviour, `README.md` for
   development, `CHANGELOG.md` (Keep a Changelog: unreleased entries move into the version section
   when it is released, nothing is dropped). Bump the version in `remote_energy_meter/config.yaml` and
   `pyproject.toml` together for an add-on release (the Dockerfile takes `BUILD_VERSION` from Supervisor).
5. **Run the gates locally.** The hooks must be enabled (`git config core.hooksPath .githooks`) and
   `gitleaks` installed; the hooks fail closed without it. Never bypass them with `--no-verify`.
6. **Commit** focused, reviewable commits (conventional prefixes: `fix:`, `feat:`, `ci:`, `docs:`).
   Stage files by name.
7. **Push and open a PR** into `main`; wait for CI to be green and for the user to merge.

## Branch protection on `main` (enforced by GitHub)

`main` is protected, so step 7 is the only way in — there are no direct pushes or web-UI edits, for
admins either:

- pull request required (0 approvals: a solo maintainer cannot approve their own PR); review
  conversations must be resolved;
- required status checks, branch up to date before merge — all 10 CI jobs: `Secret scan (gitleaks)`,
  `Local-setup reference scan`, `Dependency audit (runtime deps, blocking)`, `ruff`, `mypy`,
  `pytest (unit, property, conformance) + coverage gate`, `pytest -m integration`, `add-on linter`,
  `image (amd64)`, `image (aarch64)`;
- force pushes and deletion blocked.

When a CI job is added, removed, or **renamed**, update the required checks in the same PR
(`gh api -X PUT repos/superkus/remote_energy_meter/branches/main/protection ...`): a required check
whose name no longer reports blocks every PR. Verify protection through the API, never by pushing to
`main`.

## Gates (identical locally and in CI)

| Gate | Local | CI job |
| --- | --- | --- |
| Secrets | `gitleaks git --staged --pre-commit` / pushed range | `secrets` (full history) |
| Local-setup references | `tools/check_local_refs.py --staged` / `--history` | `local-refs` |
| Lint | `ruff check`, `ruff format --check` | `lint` |
| Types | `mypy --strict` | `typecheck` |
| Tests + coverage (≥ 80% lines+branches) | `pytest -m "not integration" --cov` | `test` |
| Integration (real Mosquitto) | `pytest -m integration` | `integration` |
| Dependency audit | `pip-audit --strict` | `audit` |
| Add-on manifest | — | `addon-lint` |
| Image build + smoke test | `docker build` | `image` |

Python is 3.12 everywhere (`.venv` created with `uv venv -p 3.12`). Keep caches out of the tree
(`PYTHONDONTWRITEBYTECODE=1`, `-p no:cacheprovider`).

## Definition of done

Not done until: the change and its tests are committed, the **full** suite passes, docs and changelog
reflect the new state, every gate is green locally, the PR's CI is green, and — for a runtime fix —
the result was verified on the target system after the reviewed change was deployed. If a criterion
could not be verified, say so explicitly.
