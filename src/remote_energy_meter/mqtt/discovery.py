"""Legacy module retained only to fail closed after the companion-integration migration.

Generic Home Assistant MQTT entity discovery is no longer produced.  Retained cleanup topic
construction remains in :mod:`remote_energy_meter.mqtt.topics`; credential-free device metadata
lives in :mod:`remote_energy_meter.mqtt.inventory`.
"""
