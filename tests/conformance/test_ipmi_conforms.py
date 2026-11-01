"""Run the shippable DevicePlugin conformance suite against the IPMI plugin."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

import pytest

from remote_energy_meter.plugins.conformance import assert_plugin_conforms
from remote_energy_meter.plugins.ipmi import plugin as ipmi_plugin
from remote_energy_meter.plugins.ipmi.plugin import SDR_CACHE_DIR_ENV, CommandResult, CommandRunner, IpmiPlugin

VALID_PARAMS: Final[Mapping[str, Any]] = {
    "host": "192.0.2.20",
    "username": "conformance-user",
    "password": "conformance-password",
    "sensor_name": "System Level",
    "interface": "lanplus",
    "cipher_suite": 3,
}
INCOMPLETE_PARAMS: Final[Mapping[str, Any]] = {key: value for key, value in VALID_PARAMS.items() if key != "password"}


class _SuccessfulRunner:
    """A controller that answers ``sdr dump`` by writing the repository and ``sdr get`` with a reading."""

    __slots__ = ("dump_count", "read_count")

    def __init__(self) -> None:
        self.dump_count = 0
        self.read_count = 0

    async def run(
        self,
        argv: tuple[str, ...],
        env: Mapping[str, str],
        timeout_s: float,
    ) -> CommandResult:
        if argv[-2:-1] == ("dump",):
            self.dump_count += 1
            # Off the event loop, as a real child process would be.
            await asyncio.to_thread(Path(argv[-1]).write_bytes, b"conformance-sdr-repository")
            return CommandResult(0, b"Dumping Sensor Data Repository\n", b"")
        self.read_count += 1
        return CommandResult(0, b"Sensor Reading : 112 (+/- 0) Watts\n", b"")


def test_ipmi_plugin_conforms_through_an_injected_runner(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runner = _SuccessfulRunner()

    def factory() -> CommandRunner:
        return runner

    monkeypatch.setattr(ipmi_plugin, "_default_command_runner", factory)
    # The suite constructs the plugin itself, so the cache location comes from the environment.
    monkeypatch.setenv(SDR_CACHE_DIR_ENV, str(tmp_path))

    report = assert_plugin_conforms(
        IpmiPlugin,
        valid_params=VALID_PARAMS,
        invalid_params=INCOMPLETE_PARAMS,
        timeout_s=1.0,
    )

    assert report.ok, report.summary()
    assert report.skipped == (), report.summary()
    assert runner.dump_count == 1, "the SDR repository is dumped once, then reused"
    assert runner.read_count == 2, "validation and reading each issue one cached sensor read"
