"""The IPMI plugin's local SDR cache: build, reuse, throttling, invalidation, and deadlines.

Background: ``ipmitool sdr get NAME`` without ``-S`` walks the controller's whole Sensor Data Repository on
every read. Against a legacy iDRAC 6 that measured 6-37 s per read under a 10 s budget, and every timeout
orphaned an RMCP+ session on a controller with five slots. The plugin therefore dumps the repository once,
outside the per-read budget, and reads with ``-S CACHE``.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import SecretStr

from remote_energy_meter.plugins.interface import FailureClassification, PowerReading, ReadFailure
from remote_energy_meter.plugins.ipmi import plugin as ipmi_plugin
from remote_energy_meter.plugins.ipmi.plugin import (
    IPMI_PASSWORD_ENV,
    SDR_CACHE_DIR_ENV,
    CommandResult,
    CommandTimeoutError,
    IpmiPlugin,
    default_sdr_cache_dir,
)
from remote_energy_meter.plugins.ipmi.schema import IpmiParams

DEVICE_ID: Final = "legacy_bmc"
PASSWORD: Final = "never-in-argv-or-detail"
PARAMS: Final = IpmiParams(host="192.0.2.20", username=SecretStr("root"), password=SecretStr(PASSWORD))
READING_OUTPUT: Final = b"Sensor ID              : System Level (0x98)\n Sensor Reading        : 119 (+/- 0) Watts\n"
SESSION_FAILURE: Final = b"Error: Unable to establish IPMI v2 / RMCP+ session\n"


@dataclass(slots=True)
class FakeController:
    """Answers ``sdr dump`` by writing the target file and ``sdr get`` with a reading, both configurable."""

    dump_delay_s: float = 0.0
    dump_result: CommandResult | BaseException = field(default_factory=lambda: CommandResult(0, b"Dumping", b""))
    dump_writes_file: bool = True
    read_result: CommandResult | BaseException = field(default_factory=lambda: CommandResult(0, READING_OUTPUT, b""))
    read_delay_s: float = 0.0
    calls: list[tuple[tuple[str, ...], float]] = field(default_factory=list)

    @property
    def dumps(self) -> list[tuple[str, ...]]:
        return [argv for argv, _ in self.calls if argv[-2:-1] == ("dump",)]

    @property
    def reads(self) -> list[tuple[tuple[str, ...], float]]:
        return [(argv, timeout_s) for argv, timeout_s in self.calls if argv[-3:-1] == ("sdr", "get")]

    async def run(self, argv: tuple[str, ...], env: Mapping[str, str], timeout_s: float) -> CommandResult:
        assert PASSWORD not in argv, "the password must never reach argv"
        assert env[IPMI_PASSWORD_ENV] == PASSWORD
        self.calls.append((argv, timeout_s))
        if argv[-2:-1] == ("dump",):
            await asyncio.sleep(self.dump_delay_s)
            if isinstance(self.dump_result, BaseException):
                raise self.dump_result
            if self.dump_writes_file and self.dump_result.returncode == 0:
                # Off the event loop, as a real child process would be.
                await asyncio.to_thread(Path(argv[-1]).write_bytes, b"sdr-repository")
            return self.dump_result
        if self.read_delay_s > timeout_s:
            await asyncio.sleep(timeout_s)
            raise CommandTimeoutError("ipmitool exceeded its operation timeout")
        await asyncio.sleep(self.read_delay_s)
        if isinstance(self.read_result, BaseException):
            raise self.read_result
        return self.read_result


def make_plugin(controller: FakeController, cache_dir: Path, **overrides: Any) -> IpmiPlugin:
    params = PARAMS.model_copy(update=overrides) if overrides else PARAMS
    return IpmiPlugin(DEVICE_ID, params, command_runner=controller, sdr_cache_dir=cache_dir)


def flag(argv: tuple[str, ...], name: str) -> str:
    return argv[argv.index(name) + 1]


def staging_files(directory: Path) -> list[Path]:
    """Leftover ``*.tmp`` files from an interrupted dump (synchronous, so async tests stay non-blocking)."""
    return list(directory.glob("*.tmp"))


# --------------------------------------------------------------------------------------------------
# build and reuse
# --------------------------------------------------------------------------------------------------


async def test_a_cold_start_dumps_once_then_reads_from_the_cache(tmp_path: Path) -> None:
    controller = FakeController()
    plugin = make_plugin(controller, tmp_path)

    first = await plugin.read_power(10.0)
    second = await plugin.read_power(10.0)

    assert isinstance(first, PowerReading) and first.watts == 119.0
    assert isinstance(second, PowerReading)
    assert len(controller.dumps) == 1, "the repository is dumped exactly once"
    assert len(controller.reads) == 2
    assert plugin.sdr_cache_path.read_bytes() == b"sdr-repository"
    for argv, _ in controller.reads:
        assert flag(argv, "-S") == str(plugin.sdr_cache_path)
    assert not staging_files(tmp_path), "the staging file is renamed into place, never left behind"


async def test_the_dump_uses_short_retransmissions_verbose_output_and_its_own_bound(tmp_path: Path) -> None:
    controller = FakeController()
    await make_plugin(controller, tmp_path).read_power(10.0)

    dump_argv = controller.dumps[0]
    dump_timeout = next(timeout for argv, timeout in controller.calls if argv == dump_argv)
    assert (flag(dump_argv, "-N"), flag(dump_argv, "-R")) == ("2", "2")
    assert "-v" in dump_argv
    assert "-S" not in dump_argv
    assert dump_timeout == ipmi_plugin.SDR_CACHE_BUILD_TIMEOUT_S


async def test_a_persisted_cache_is_reused_by_a_new_instance_without_a_dump(tmp_path: Path) -> None:
    await make_plugin(FakeController(), tmp_path).read_power(10.0)
    controller = FakeController()

    result = await make_plugin(controller, tmp_path).validate_connection(10.0)

    assert result.ok
    assert controller.dumps == [], "a cache that survived a restart is used as is"


async def test_a_short_attempt_still_waits_for_an_instant_build(tmp_path: Path) -> None:
    """A sub-second budget must not be consumed entirely by the reserve held back for the read."""
    controller = FakeController()

    result = await make_plugin(controller, tmp_path).read_power(1.0)

    assert isinstance(result, PowerReading), result


async def test_a_slow_build_survives_the_attempt_that_started_it(tmp_path: Path) -> None:
    """The scheduler's timeout or shutdown cancels an attempt; the dump it started must keep going."""
    controller = FakeController(dump_delay_s=0.3)
    plugin = make_plugin(controller, tmp_path)

    early = await plugin.read_power(0.2)
    assert isinstance(early, ReadFailure)
    assert early.classification is FailureClassification.COMMUNICATION
    assert "building the local IPMI SDR cache" in early.detail

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await plugin.read_power(10.0)

    await asyncio.sleep(0.4)
    assert plugin.sdr_cache_path.is_file(), "cancelling a waiting attempt must not cancel the build"
    assert isinstance(await plugin.read_power(10.0), PowerReading)
    assert len(controller.dumps) == 1, "concurrent and later attempts share one build"


# --------------------------------------------------------------------------------------------------
# failed builds
# --------------------------------------------------------------------------------------------------


async def test_a_failed_build_is_classified_and_not_retried_inside_the_throttle(tmp_path: Path) -> None:
    controller = FakeController(dump_result=CommandResult(1, b"", SESSION_FAILURE))
    plugin = make_plugin(controller, tmp_path)

    first = await plugin.read_power(10.0)
    second = await plugin.read_power(10.0)

    assert isinstance(first, ReadFailure) and isinstance(second, ReadFailure)
    assert first.classification is FailureClassification.COMMUNICATION
    assert "no IPMI session could be established" in first.detail
    assert "next build in" in second.detail
    assert len(controller.dumps) == 1, "a failing controller is not hammered with dumps"
    assert controller.reads == []


async def test_a_build_is_attempted_again_once_the_throttle_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ipmi_plugin, "SDR_CACHE_RETRY_AFTER_S", 0.05)
    controller = FakeController(dump_result=CommandResult(1, b"", SESSION_FAILURE))
    plugin = make_plugin(controller, tmp_path)
    await plugin.read_power(10.0)

    controller.dump_result = CommandResult(0, b"Dumping", b"")
    await asyncio.sleep(0.1)
    result = await plugin.read_power(10.0)

    assert isinstance(result, PowerReading), result
    assert len(controller.dumps) == 2


async def test_a_build_rejected_for_credentials_is_authentication(tmp_path: Path) -> None:
    stderr = b"> RAKP 2 HMAC is invalid\n" + SESSION_FAILURE
    plugin = make_plugin(FakeController(dump_result=CommandResult(1, b"", stderr)), tmp_path)

    result = await plugin.read_power(10.0)

    assert isinstance(result, ReadFailure)
    assert result.classification is FailureClassification.AUTHENTICATION
    assert PASSWORD not in result.detail


async def test_a_dump_that_writes_nothing_is_not_trusted(tmp_path: Path) -> None:
    plugin = make_plugin(FakeController(dump_writes_file=False), tmp_path)

    result = await plugin.read_power(10.0)

    assert isinstance(result, ReadFailure)
    assert result.classification is FailureClassification.RESPONSE_FORMAT
    assert not plugin.sdr_cache_path.exists()


async def test_a_dump_timeout_is_a_timeout(tmp_path: Path) -> None:
    plugin = make_plugin(FakeController(dump_result=CommandTimeoutError("slow")), tmp_path)

    result = await plugin.read_power(10.0)

    assert isinstance(result, ReadFailure)
    assert result.classification is FailureClassification.TIMEOUT


async def test_a_defect_during_the_build_is_contained_and_throttled(tmp_path: Path) -> None:
    controller = FakeController(dump_result=RuntimeError("implementation defect"))
    plugin = make_plugin(controller, tmp_path)

    first = await plugin.read_power(10.0)
    second = await plugin.read_power(10.0)

    assert isinstance(first, ReadFailure) and first.classification is FailureClassification.PLUGIN
    assert "implementation defect" not in first.detail
    assert isinstance(second, ReadFailure) and "next build in" in second.detail
    assert len(controller.dumps) == 1


async def test_an_unwritable_cache_directory_is_a_communication_failure(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_bytes(b"")
    plugin = make_plugin(FakeController(), blocker / "cache")

    result = await plugin.read_power(10.0)

    assert isinstance(result, ReadFailure)
    assert result.classification is FailureClassification.COMMUNICATION
    assert "not writable" in result.detail


# --------------------------------------------------------------------------------------------------
# invalidation
# --------------------------------------------------------------------------------------------------


async def test_a_corrupt_cache_is_discarded_and_rebuilt(tmp_path: Path) -> None:
    """ipmitool exits 0 when the cache file is unreadable, so the plugin must read its output."""
    corrupt = CommandResult(0, b"", b"invalid sdr header version 72\nUnable to find sensor id 'System Level'\n")
    controller = FakeController(read_result=corrupt)
    plugin = make_plugin(controller, tmp_path)

    failed = await plugin.read_power(10.0)
    assert isinstance(failed, ReadFailure)
    assert failed.classification is FailureClassification.RESPONSE_FORMAT
    assert "will be rebuilt" in failed.detail
    assert not plugin.sdr_cache_path.exists()

    controller.read_result = CommandResult(0, READING_OUTPUT, b"")
    assert isinstance(await plugin.read_power(10.0), PowerReading)
    assert len(controller.dumps) == 2


async def test_a_sensor_missing_from_a_fresh_cache_keeps_the_cache(tmp_path: Path) -> None:
    """A misconfigured sensor name must not trigger a repository dump on every poll."""
    missing = CommandResult(0, b"", b"Unable to find sensor id 'Nope'\n")
    controller = FakeController(read_result=missing)
    plugin = make_plugin(controller, tmp_path, sensor_name="Nope")

    result = await plugin.read_power(10.0)

    assert isinstance(result, ReadFailure)
    assert result.classification is FailureClassification.RESPONSE_FORMAT
    assert result.detail == "the configured IPMI sensor was not found"
    assert plugin.sdr_cache_path.exists()


async def test_a_sensor_missing_from_an_old_cache_rebuilds_it(tmp_path: Path) -> None:
    missing = CommandResult(0, b"", b"Unable to find sensor id 'System Level'\n")
    controller = FakeController(read_result=missing)
    plugin = make_plugin(controller, tmp_path)
    await plugin.read_power(10.0)  # builds the cache, then reports the sensor missing
    old = time.time() - IpmiPlugin.CACHE_MIN_AGE_FOR_INVALIDATION_S - 60
    os.utime(plugin.sdr_cache_path, (old, old))

    await plugin.read_power(10.0)

    assert not plugin.sdr_cache_path.exists(), "the controller's repository may have changed since"


# --------------------------------------------------------------------------------------------------
# deadlines and placement
# --------------------------------------------------------------------------------------------------


async def test_a_slow_read_is_classified_by_the_plugin_before_the_callers_deadline(tmp_path: Path) -> None:
    """The plugin ends its child first, so the scheduler gets a classified failure, not a cancellation."""
    controller = FakeController(read_delay_s=99.0)
    plugin = make_plugin(controller, tmp_path)
    plugin.sdr_cache_path.parent.mkdir(parents=True, exist_ok=True)
    plugin.sdr_cache_path.write_bytes(b"sdr-repository")

    async with asyncio.timeout(1.5):
        result = await plugin.read_power(1.5)

    assert isinstance(result, ReadFailure)
    assert result.classification is FailureClassification.TIMEOUT
    assert controller.reads[0][1] <= 1.0


def test_cache_files_are_per_device_and_per_endpoint(tmp_path: Path) -> None:
    a = make_plugin(FakeController(), tmp_path)
    b = IpmiPlugin("other", PARAMS, command_runner=FakeController(), sdr_cache_dir=tmp_path)
    c = make_plugin(FakeController(), tmp_path, host="192.0.2.99")

    assert len({a.sdr_cache_path, b.sdr_cache_path, c.sdr_cache_path}) == 3
    for plugin in (a, b, c):
        assert plugin.sdr_cache_path.parent == tmp_path
        assert PASSWORD not in plugin.sdr_cache_path.name
        assert "root" not in plugin.sdr_cache_path.name, "the username is a credential and stays out of paths"


def test_the_cache_directory_honours_the_environment_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SDR_CACHE_DIR_ENV, str(tmp_path / "override"))

    assert default_sdr_cache_dir() == tmp_path / "override"
    assert IpmiPlugin(DEVICE_ID, PARAMS).sdr_cache_path.parent == tmp_path / "override"


def test_the_cache_lives_in_the_addon_data_directory_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(SDR_CACHE_DIR_ENV, raising=False)
    monkeypatch.setattr(ipmi_plugin, "_ADDON_DATA_DIR", tmp_path)

    assert default_sdr_cache_dir() == tmp_path / "ipmi-sdr-cache"


def test_without_addon_data_the_cache_falls_back_to_a_temp_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(SDR_CACHE_DIR_ENV, raising=False)
    monkeypatch.setattr(ipmi_plugin, "_ADDON_DATA_DIR", tmp_path / "absent")

    fallback = default_sdr_cache_dir()

    assert fallback.name == "remote-energy-meter-ipmi-sdr"
    assert not str(fallback).startswith(str(tmp_path / "absent"))
