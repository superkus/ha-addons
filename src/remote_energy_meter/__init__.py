"""Remote Energy Meter: a Home Assistant addon that meters network-attached devices.

The package is split into a pure core (configuration parsing and printing, plugin
resolution, energy integration, MQTT payload construction, credential redaction) and a
thin I/O shell (Redfish HTTPS, MQTT, file persistence).
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("remote-energy-meter")
except PackageNotFoundError:  # running from a source tree without an installed dist
    __version__ = "0.0.0.dev0"

__all__ = ["__version__"]
