"""Process entry point: signal handling, run, and exit status (Requirements 1.2, 1.5, 1.6).

The container's exit status must equal the metering process's exit status (Requirement 1.2), so this
module does exactly two things: turn a signal into an orderly shutdown, and turn the outcome into an
integer.

**Why the signal handlers are loop handlers.** ``signal.signal`` would run the handler in whatever
frame the interpreter happens to be in, which for an asyncio process means a callback that cannot
await anything. ``loop.add_signal_handler`` schedules on the loop instead, so shutdown is ordinary
async code running inside the Requirement 1.5 budget rather than a best-effort synchronous stub.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
from typing import TYPE_CHECKING, Final

from remote_energy_meter.app import EXIT_STARTUP_FAILED, Application
from remote_energy_meter.diagnostics.logger import configure_logging, get_logger

if TYPE_CHECKING:
    import logging
    from collections.abc import Sequence

#: Requirement 1.6: an orderly shutdown is a success, whatever prompted it.
EXIT_OK: Final = 0

#: The signals Supervisor uses to stop an addon.
STOP_SIGNALS: Final = (signal.SIGTERM, signal.SIGINT)


async def run() -> int:
    """Start the application, wait for a stop signal, then shut down. Returns the exit status."""
    log = get_logger(__name__)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()

    for stop_signal in STOP_SIGNALS:
        with contextlib.suppress(NotImplementedError):
            # NotImplementedError is Windows, where add_signal_handler is unavailable. The addon runs
            # in a Linux container, so this is a developer-machine concession, not a supported path.
            loop.add_signal_handler(stop_signal, _on_signal, stop_signal, stopping, log)

    application = Application(logger=log)
    failure = await application.start()
    if failure is not None:
        # Requirement 1.4: the stage and classification are already recorded by start(); the scheduler
        # was never started, so there is nothing to unwind but the collaborators start() did build.
        await application.shutdown()
        return EXIT_STARTUP_FAILED

    await stopping.wait()
    await application.shutdown()
    return EXIT_OK


def _on_signal(stop_signal: signal.Signals, stopping: asyncio.Event, log: logging.Logger) -> None:
    """Record the signal once and release the main coroutine."""
    log.info("received %s, shutting down", stop_signal.name, extra={"signal": stop_signal.name})
    stopping.set()


def main(argv: Sequence[str] | None = None) -> int:
    """Configure logging, run, and return the process exit status.

    Logging is configured twice on purpose: once here at the default level so a failure during
    configuration loading is still visible, and again inside startup once the configured level is
    known. ``configure_logging`` is idempotent, so the second call replaces the handler rather than
    doubling every record.
    """
    del argv  # accepted so this is usable as a console-script entry point
    configure_logging()
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        # Only reachable when the loop signal handler could not be installed.
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
