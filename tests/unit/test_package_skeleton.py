"""Skeleton checks: the package imports and its plugin package is walkable.

These guard the two structural assumptions the rest of the suite rests on: the
``src`` layout is importable, and ``Plugin_Registry`` can discover plugins by walking
``remote_energy_meter.plugins`` with :func:`pkgutil.iter_modules`.
"""

import importlib
import pkgutil

import remote_energy_meter


def test_package_imports() -> None:
    assert remote_energy_meter.__version__


def test_subpackages_import() -> None:
    for name in ("config", "plugins", "plugins.idrac", "plugins.ipmi", "mqtt", "diagnostics"):
        assert importlib.import_module(f"remote_energy_meter.{name}") is not None


def test_plugins_package_is_walkable() -> None:
    plugins = importlib.import_module("remote_energy_meter.plugins")

    # A path-bearing package is the precondition for pkgutil-based discovery.
    assert hasattr(plugins, "__path__")
    module_names = {info.name for info in pkgutil.iter_modules(plugins.__path__)}

    assert {"idrac", "ipmi"} <= module_names
