"""Suite-wide isolation shared by every test layer."""

from __future__ import annotations

import sys

# Never write bytecode while testing. Tests import `custom_components`, and a `__pycache__`
# left inside it makes the integration-package validator (correctly) reject the package as
# containing generated files -- so a run without PYTHONDONTWRITEBYTECODE failed in CI.
# Set before any first-party import below.
sys.dont_write_bytecode = True

from collections.abc import Iterator  # noqa: E402

import pytest  # noqa: E402

from remote_energy_meter.plugins.ipmi.plugin import SDR_CACHE_DIR_ENV  # noqa: E402


@pytest.fixture(autouse=True, scope="session")
def _isolated_ipmi_sdr_cache(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Keep IPMI SDR cache files out of ``/data`` and the shared system temp directory.

    A plugin built without an explicit ``sdr_cache_dir`` (the conformance suite, the registry, the
    application wiring) would otherwise persist a cache in ``tempfile.gettempdir()`` that a later test,
    or a later run, silently reuses. Session-scoped so Hypothesis tests can run under it; tests that
    care about cache state pass their own ``tmp_path``.
    """
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(SDR_CACHE_DIR_ENV, str(tmp_path_factory.mktemp("ipmi-sdr-cache")))
        yield
