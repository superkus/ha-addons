"""Generic IPMI SDR sensor plugin."""

from remote_energy_meter.plugins.ipmi.parser import parse_sensor_output
from remote_energy_meter.plugins.ipmi.plugin import IpmiPlugin
from remote_energy_meter.plugins.ipmi.schema import IpmiParams

__all__ = ["IpmiParams", "IpmiPlugin", "parse_sensor_output"]
