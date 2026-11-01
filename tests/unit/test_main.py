"""Unit tests for process exit-status propagation after startup failure (Requirement 1.4)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import remote_energy_meter.__main__ as main_module
from remote_energy_meter.app import EXIT_STARTUP_FAILED, StartupFailure, StartupStage

if TYPE_CHECKING:
    import logging


class FailingApplication:
    """A lifecycle substitute whose named startup stage has already failed."""

    def __init__(self, failure: StartupFailure) -> None:
        self._failure = failure
        self.shutdown_calls = 0

    async def start(self) -> StartupFailure:
        return self._failure

    async def shutdown(self) -> None:
        self.shutdown_calls += 1


@pytest.mark.parametrize("failed_stage", list(StartupStage))
async def test_every_named_startup_stage_failure_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, failed_stage: StartupStage
) -> None:
    """The entrypoint maps every startup failure uniformly to the nonzero process status."""
    failure = StartupFailure(
        stage=failed_stage,
        classification="injected_failure",
        detail="injected by the entrypoint unit test",
    )
    application = FailingApplication(failure)

    def application_factory(*, logger: logging.Logger) -> FailingApplication:
        del logger
        return application

    monkeypatch.setattr(main_module, "Application", application_factory)
    monkeypatch.setattr(main_module, "STOP_SIGNALS", ())

    status = await main_module.run()

    assert status == EXIT_STARTUP_FAILED
    assert status != main_module.EXIT_OK
    assert application.shutdown_calls == 1
