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
   when it is released, nothing is dropped). Bump the version in `remote_energy_meter/config.yaml`,
   `pyproject.toml`, and `remote_energy_meter/Dockerfile` together for an add-on release.
5. **Run the gates locally.** The hooks must be enabled (`git config core.hooksPath .githooks`) and
   `gitleaks` installed; the hooks fail closed without it. Never bypass them with `--no-verify`.
6. **Commit** focused, reviewable commits (conventional prefixes: `fix:`, `feat:`, `ci:`, `docs:`).
   Stage files by name.
7. **Push and open a PR** into `main`; wait for CI to be green and for the user to merge.

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
