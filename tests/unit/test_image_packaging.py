"""Base-image and architecture agreement between the Dockerfile, the manifest, and CI.

The Dockerfile's single ``FROM`` is the source of truth for the add-on image (Supervisor
passes ``BUILD_FROM`` only to add-ons that still ship a ``build.yaml``). CI only inspects
that line remotely, so these checks make the pre-commit gate catch an unpinned base, a
Python-minor drift of the base tag, or a manifest ``arch`` list that no longer matches the
image build matrix.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import yaml

ROOT: Final = Path(__file__).resolve().parents[2]
DOCKERFILE: Final = ROOT / "remote_energy_meter" / "Dockerfile"
MANIFEST: Final = ROOT / "remote_energy_meter" / "config.yaml"
CI_WORKFLOW: Final = ROOT / ".github" / "workflows" / "ci.yaml"
PYPROJECT: Final = ROOT / "pyproject.toml"

BASE_IMAGE_REPOSITORY: Final = "ghcr.io/home-assistant/base-python"
SUPPORTED_ARCHITECTURES: Final = frozenset({"amd64", "aarch64"})

FROM_INSTRUCTION: Final = re.compile(r"^\s*from\s+(?P<image>.*?)\s*$", re.IGNORECASE)
PINNED_BASE_IMAGE: Final = re.compile(
    rf"^{re.escape(BASE_IMAGE_REPOSITORY)}:(?P<tag>[A-Za-z0-9_.-]+)@sha256:(?P<digest>[0-9a-f]{{64}})$"
)


def _mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, Mapping)
    return {str(key): item for key, item in value.items()}


def _from_instructions() -> list[str]:
    matches = (FROM_INSTRUCTION.match(line) for line in DOCKERFILE.read_text(encoding="utf-8").splitlines())
    return [match["image"] for match in matches if match]


def _base_image_tag() -> str:
    instructions = _from_instructions()
    assert len(instructions) == 1, instructions
    match = PINNED_BASE_IMAGE.match(image := instructions[0])
    assert match, image
    return match["tag"]


def _ci_workflow() -> Mapping[str, object]:
    return _mapping(yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8")))


def _project_python_version() -> str:
    """The Python minor the whole repository is pinned to (`.kiro/steering/workflow.md`: 3.12 everywhere)."""
    ci_version = _mapping(_ci_workflow()["env"])["PYTHON_VERSION"]
    mypy_version = _mapping(_mapping(tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["tool"])["mypy"])[
        "python_version"
    ]
    assert isinstance(ci_version, str) and re.fullmatch(r"\d+\.\d+", ci_version), ci_version
    assert ci_version == mypy_version, "ci.yaml PYTHON_VERSION and [tool.mypy].python_version disagree"
    return ci_version


def test_dockerfile_has_exactly_one_from_pinned_by_digest_to_the_official_python_base() -> None:
    """A tag alone floats: the image Supervisor builds would stop being the image CI tested."""
    instructions = _from_instructions()
    assert len(instructions) == 1, instructions

    (image,) = instructions
    assert PINNED_BASE_IMAGE.match(image), f"FROM is not `{BASE_IMAGE_REPOSITORY}:<tag>@sha256:<64 hex>`: {image}"
    # `_from_instructions` and the CI step accept any case and leading whitespace, so a re-cased or
    # indented FROM would pass both. Keep the canonical upper-case, column-0 form anyway: the Dockerfile
    # header describes "this FROM line", and `tests/integration/test_container.py::_pinned_base_image`
    # finds it with `line.startswith("FROM ")`.
    assert any(line.startswith("FROM ") for line in DOCKERFILE.read_text(encoding="utf-8").splitlines())


def test_base_image_python_minor_matches_the_project_python_version() -> None:
    """Dependabot proposes `3.13-...` tags too; the runtime interpreter must stay the one the tools target."""
    expected = _project_python_version()
    tag = _base_image_tag()
    assert tag.startswith(f"{expected}-"), f"base image tag {tag!r} is not Python {expected}"


def test_image_license_label_covers_mit_code_and_gfdl_word_list_data() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert 'org.opencontainers.image.licenses="MIT AND GFDL-1.2-only"' in dockerfile


def test_manifest_arch_list_matches_the_supported_set_and_the_ci_image_matrix() -> None:
    """An arch in config.yaml that CI never builds advertises an image nobody has verified, and vice versa."""
    manifest = _mapping(yaml.safe_load(MANIFEST.read_text(encoding="utf-8")))
    manifest_arch = manifest["arch"]
    assert isinstance(manifest_arch, list)
    assert len(manifest_arch) == len(set(manifest_arch)), "duplicate arch entries"
    assert set(manifest_arch) == SUPPORTED_ARCHITECTURES

    image_job = _mapping(_mapping(_ci_workflow()["jobs"])["image"])
    include = _mapping(_mapping(image_job["strategy"])["matrix"])["include"]
    assert isinstance(include, list)
    matrix_arch = [_mapping(entry)["arch"] for entry in include]
    assert len(matrix_arch) == len(set(matrix_arch)), "duplicate matrix entries"
    assert set(matrix_arch) == set(manifest_arch)
