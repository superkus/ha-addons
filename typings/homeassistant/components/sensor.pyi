from enum import StrEnum

class SensorDeviceClass(StrEnum):
    POWER = "power"
    ENERGY = "energy"

class SensorStateClass(StrEnum):
    MEASUREMENT = "measurement"
    TOTAL_INCREASING = "total_increasing"

class SensorEntity: ...
