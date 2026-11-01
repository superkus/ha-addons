"""A local-only Mosquitto process for MQTT integration tests.

The helper deliberately has no external-broker fallback.  It writes every runtime artifact
below pytest's ``tmp_path``, binds the one listener to IPv4 loopback, probes that listener for
readiness, and reaps the process on every context-manager exit.
"""

from __future__ import annotations

import asyncio
import shutil
import socket
from contextlib import suppress
from pathlib import Path
from typing import BinaryIO, Final, Self

import pytest

__all__ = [
    "MISSING_MOSQUITTO_MESSAGE",
    "LocalMosquittoBroker",
    "MosquittoStartupError",
    "resolve_mosquitto_binary",
]

_LOOPBACK_HOST: Final = "127.0.0.1"
_DEFAULT_STARTUP_TIMEOUT_S: Final = 5.0
_CONNECT_PROBE_TIMEOUT_S: Final = 0.25
_INITIAL_PROBE_BACKOFF_S: Final = 0.01
_MAX_PROBE_BACKOFF_S: Final = 0.1
_TERMINATE_TIMEOUT_S: Final = 2.0
_KILL_TIMEOUT_S: Final = 2.0
_DIAGNOSTIC_TAIL_CHARACTERS: Final = 8_192

MISSING_MOSQUITTO_MESSAGE: Final = (
    "Local MQTT integration test skipped: the 'mosquitto' executable was not found on PATH. "
    "Install Mosquitto and ensure its binary is on PATH, then rerun the test; these tests never "
    "fall back to an external broker."
)


class MosquittoStartupError(RuntimeError):
    """Raised when a discovered Mosquitto binary cannot become ready locally."""


class _ReadinessError(RuntimeError):
    """Internal readiness failure, wrapped with captured process diagnostics by ``start``."""


def resolve_mosquitto_binary() -> str | None:
    """Return the local Mosquitto executable, or ``None`` when it is not installed."""
    return shutil.which("mosquitto")


def _reserve_ephemeral_loopback_port() -> int:
    """Ask the kernel for an unused TCP port on IPv4 loopback.

    Mosquitto does not expose a portable way to report a kernel-selected port.  The reservation
    is therefore held until immediately before process launch, minimizing the unavoidable gap
    between selecting the port and Mosquitto binding it.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
        reservation.bind((_LOOPBACK_HOST, 0))
        address = reservation.getsockname()
        return int(address[1])


class LocalMosquittoBroker:
    """Async context manager owning one loopback-only Mosquitto subprocess.

    ``host`` and ``port`` are suitable for ``aiomqtt.Client``.  Anonymous access is enabled only
    on the listener explicitly bound to ``127.0.0.1``; no wildcard or external listener exists.
    Persistence is disabled, and config, broker log, process output, and data paths all live
    below the supplied pytest ``tmp_path``.
    """

    def __init__(self, tmp_path: Path, *, startup_timeout_s: float = _DEFAULT_STARTUP_TIMEOUT_S) -> None:
        if startup_timeout_s <= 0:
            raise ValueError("startup_timeout_s must be greater than zero")

        self._root_directory = tmp_path / "mosquitto"
        self._config_path = self._root_directory / "mosquitto.conf"
        self._log_path = self._root_directory / "mosquitto.log"
        self._process_output_path = self._root_directory / "process-output.log"
        self._data_directory = self._root_directory / "data"
        self._startup_timeout_s = startup_timeout_s
        self._port: int | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._process_output_stream: BinaryIO | None = None

    @property
    def host(self) -> str:
        """The only interface on which this broker listens."""
        return _LOOPBACK_HOST

    @property
    def port(self) -> int:
        """The ephemeral listener port, available while the broker is started."""
        if self._port is None:
            raise RuntimeError("the local Mosquitto broker has not been started")
        return self._port

    @property
    def address(self) -> str:
        """Return the ``host:port`` address of the running broker."""
        return f"{self.host}:{self.port}"

    @property
    def root_directory(self) -> Path:
        """Directory containing all broker runtime artifacts."""
        return self._root_directory

    @property
    def config_path(self) -> Path:
        """Generated Mosquitto configuration path."""
        return self._config_path

    @property
    def log_path(self) -> Path:
        """Mosquitto's configured log file."""
        return self._log_path

    @property
    def process_output_path(self) -> Path:
        """Captured stdout/stderr emitted before Mosquitto can use its configured log."""
        return self._process_output_path

    @property
    def data_directory(self) -> Path:
        """Local data directory named in the generated configuration."""
        return self._data_directory

    @property
    def is_running(self) -> bool:
        """Whether the owned subprocess has not exited."""
        return self._process is not None and self._process.returncode is None

    async def start(self) -> None:
        """Start Mosquitto and return only after its loopback listener accepts connections.

        Missing software is an environmental skip, not a failed product assertion.  A binary
        that exists but exits or misses the readiness deadline is a failure whose exception
        contains both Mosquitto's configured log and its captured process output.
        """
        if self._process is not None:
            raise RuntimeError("the local Mosquitto broker is already started")

        executable = resolve_mosquitto_binary()
        if executable is None:
            pytest.skip(MISSING_MOSQUITTO_MESSAGE)

        self._prepare_runtime_directory()
        self._port = _reserve_ephemeral_loopback_port()
        self._write_configuration()
        self._process_output_stream = self._process_output_path.open("wb")

        try:
            self._process = await asyncio.create_subprocess_exec(
                executable,
                "-c",
                str(self._config_path),
                stdout=self._process_output_stream,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as error:
            address = self.address
            self._close_process_output_stream()
            self._port = None
            raise MosquittoStartupError(self._startup_failure_message(address, error)) from error

        address = self.address
        try:
            await self._wait_until_ready()
        except asyncio.CancelledError:
            await self.close()
            raise
        except Exception as error:
            await self.close()
            raise MosquittoStartupError(self._startup_failure_message(address, error)) from error

    async def close(self) -> None:
        """Terminate and reap Mosquitto, escalating to kill within bounded deadlines.

        Cleanup is shielded from caller cancellation and awaited before cancellation propagates,
        so leaving the context cannot strand a broker process.
        """
        process = self._process
        if process is None:
            self._close_process_output_stream()
            self._port = None
            return

        cleanup_task = asyncio.create_task(self._stop_process(process))
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            await cleanup_task
            raise
        finally:
            self._process = None
            self._port = None
            self._close_process_output_stream()

    async def __aenter__(self) -> Self:
        """Start and return this broker."""
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        """Always stop and reap the owned process."""
        await self.close()

    def _prepare_runtime_directory(self) -> None:
        self._data_directory.mkdir(parents=True, exist_ok=True)

    def _write_configuration(self) -> None:
        data_location = f"{self._data_directory}/"
        configuration = "\n".join(
            (
                "# Generated for one local integration test; never use an external listener.",
                "per_listener_settings true",
                f"listener {self.port} {self.host}",
                "protocol mqtt",
                # Safe only because the sole listener above is explicitly IPv4 loopback.
                "allow_anonymous true",
                "persistence false",
                f"persistence_location {data_location}",
                "connection_messages true",
                "log_timestamp true",
                "log_type all",
                f"log_dest file {self._log_path}",
                "",
            )
        )
        self._config_path.write_text(configuration, encoding="utf-8")

    async def _wait_until_ready(self) -> None:
        process = self._process
        if process is None:
            raise RuntimeError("cannot wait for readiness before starting Mosquitto")

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._startup_timeout_s
        backoff_s = _INITIAL_PROBE_BACKOFF_S
        last_error: OSError | None = None
        exit_waiter = asyncio.create_task(process.wait())

        try:
            while True:
                if exit_waiter.done():
                    raise _ReadinessError(f"process exited with status {exit_waiter.result()}")

                remaining_s = deadline - loop.time()
                if remaining_s <= 0:
                    detail = f"; last connection error: {last_error!r}" if last_error is not None else ""
                    raise _ReadinessError(
                        f"listener {self.address} was not ready within {self._startup_timeout_s:.1f}s{detail}"
                    )

                try:
                    _reader, writer = await asyncio.wait_for(
                        asyncio.open_connection(self.host, self.port),
                        timeout=min(_CONNECT_PROBE_TIMEOUT_S, remaining_s),
                    )
                except OSError as error:
                    last_error = error
                else:
                    writer.close()
                    with suppress(OSError):
                        await writer.wait_closed()
                    if exit_waiter.done():
                        raise _ReadinessError(f"process exited with status {exit_waiter.result()}")
                    return

                remaining_s = deadline - loop.time()
                if remaining_s <= 0:
                    continue
                done, _pending = await asyncio.wait(
                    {exit_waiter},
                    timeout=min(backoff_s, remaining_s),
                )
                if exit_waiter in done:
                    raise _ReadinessError(f"process exited with status {exit_waiter.result()}")
                backoff_s = min(backoff_s * 2, _MAX_PROBE_BACKOFF_S)
        finally:
            if not exit_waiter.done():
                exit_waiter.cancel()
                with suppress(asyncio.CancelledError):
                    await exit_waiter

    async def _stop_process(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            await process.wait()
            return

        with suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=_TERMINATE_TIMEOUT_S)
        except TimeoutError:
            pass
        else:
            return

        with suppress(ProcessLookupError):
            process.kill()
        try:
            await asyncio.wait_for(process.wait(), timeout=_KILL_TIMEOUT_S)
        except TimeoutError as error:
            raise RuntimeError(
                f"Mosquitto process {process.pid} did not exit after terminate and kill deadlines"
            ) from error

    def _close_process_output_stream(self) -> None:
        if self._process_output_stream is not None:
            self._process_output_stream.close()
            self._process_output_stream = None

    def _startup_failure_message(self, address: str, error: BaseException) -> str:
        sections: list[str] = []
        for label, path in (
            ("process output", self._process_output_path),
            ("Mosquitto log", self._log_path),
        ):
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                sections.append(f"{label}:\n{text[-_DIAGNOSTIC_TAIL_CHARACTERS:]}")

        diagnostics = "\n\n".join(sections) or "no diagnostics were emitted"
        return (
            f"Mosquitto failed to start on local listener {address}: {error}. "
            f"Generated config: {self._config_path}. Diagnostics:\n{diagnostics}"
        )
