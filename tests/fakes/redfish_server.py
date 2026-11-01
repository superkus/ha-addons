"""Loopback-only HTTPS Redfish server used by integration tests.

The fake owns a real :mod:`aiohttp.web` transport so integration tests exercise the
same HTTP and TLS layers as an iDRAC request without contacting an external host.  A
server starts on ``127.0.0.1`` with an ephemeral port and serves only the default Dell
chassis power resource.  Its response can be switched at runtime between a valid
reading, a payload with no power member, an arbitrary HTTP status, and a cancellable
stall.

The caller supplies a directory on local storage, normally pytest's ``tmp_path``.  A
short-lived self-signed certificate and key are generated there with the local
``openssl`` executable.  The certificate has subject alternative names for both
``localhost`` and ``127.0.0.1``; certificate-verification failures therefore concern
trust, not hostname mismatch.  There is deliberately no HTTP fallback when OpenSSL is
unavailable or certificate generation fails.
"""

from __future__ import annotations

import asyncio
import shutil
import socket
import ssl
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import TracebackType
from typing import Final, Self
from uuid import uuid4

from aiohttp import web

from remote_energy_meter.plugins.idrac.schema import DEFAULT_POWER_PATH

__all__ = [
    "CertificateGenerationError",
    "FakeRedfishServer",
    "OpenSslUnavailableError",
    "RecordedRequest",
]

_LOOPBACK_HOST: Final = "127.0.0.1"
_LOCALHOST_NAME: Final = "localhost"
_DEFAULT_WATTS: Final = 200.0
_SHUTDOWN_TIMEOUT_S: Final = 1.0


class OpenSslUnavailableError(RuntimeError):
    """Raised when the fake cannot find the certificate-generation executable."""


class CertificateGenerationError(RuntimeError):
    """Raised when OpenSSL cannot generate the fake's certificate and key."""


class _Behavior(Enum):
    VALID = "valid"
    ABSENT_POWER = "absent_power"
    STATUS = "status"
    STALL = "stall"


@dataclass(frozen=True, slots=True)
class _ResponsePlan:
    behavior: _Behavior
    watts: float = _DEFAULT_WATTS
    status: int = 200


@dataclass(frozen=True, slots=True)
class RecordedRequest:
    """One request observed at the fake chassis power resource."""

    method: str
    path: str
    authorization: str | None


class FakeRedfishServer:
    """A reusable HTTPS fake for the iDRAC chassis power resource.

    Use it as an async context manager::

        server = FakeRedfishServer(tmp_path)
        server.respond_with_power(237.5)
        async with server:
            # Configure IdracParams(host=server.authority, verify_tls=False, ...)
            ...

    ``respond_with_power``, ``respond_without_power``, ``respond_with_status``, and
    ``stall`` may be called before startup or while the server is running.  Changing
    away from a stall also releases handlers already waiting in that stall.  Closing
    the server cancels any remaining stalled handlers before releasing the listening
    socket.
    """

    __slots__ = (
        "_active_stalls",
        "_certificate_directory",
        "_certificate_path",
        "_key_path",
        "_plan",
        "_port",
        "_requests",
        "_runner",
        "_site",
        "_stall_entered",
        "_stall_release",
    )

    def __init__(self, certificate_directory: Path, *, initial_watts: float = _DEFAULT_WATTS) -> None:
        """Prepare a fake whose certificate files live under ``certificate_directory``.

        Tests pass pytest's per-test ``tmp_path``, so the throwaway private key never lands in the
        working tree.
        """
        local_directory = certificate_directory.expanduser().resolve(strict=False)

        certificate_id = uuid4().hex
        self._certificate_directory = local_directory
        self._certificate_path = local_directory / f"redfish-{certificate_id}.crt"
        self._key_path = local_directory / f"redfish-{certificate_id}.key"
        self._plan = _ResponsePlan(_Behavior.VALID, watts=float(initial_watts))
        self._port: int | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.SockSite | None = None
        self._requests: list[RecordedRequest] = []
        self._active_stalls: set[asyncio.Task[object]] = set()
        self._stall_entered = asyncio.Event()
        self._stall_release = asyncio.Event()

    @property
    def host(self) -> str:
        """The IPv4 loopback address to which the server binds."""
        return _LOOPBACK_HOST

    @property
    def port(self) -> int:
        """The ephemeral port selected at startup.

        The last selected port remains available after shutdown so cleanup tests can
        verify that it no longer accepts connections.
        """
        if self._port is None:
            raise RuntimeError("the fake Redfish server has not been started")
        return self._port

    @property
    def authority(self) -> str:
        """The ``host:port`` value suitable for ``IdracParams.host``."""
        return f"{self.host}:{self.port}"

    @property
    def base_url(self) -> str:
        """The HTTPS origin of the running (or most recently run) server."""
        return f"https://{self.authority}"

    @property
    def url(self) -> str:
        """The absolute URL of the chassis power resource."""
        return f"{self.base_url}{DEFAULT_POWER_PATH}"

    @property
    def power_path(self) -> str:
        """The Redfish chassis power resource served by this fake."""
        return DEFAULT_POWER_PATH

    @property
    def certificate_path(self) -> Path:
        """The generated self-signed certificate path on local storage."""
        return self._certificate_path

    @property
    def key_path(self) -> Path:
        """The generated private-key path on local storage."""
        return self._key_path

    @property
    def is_running(self) -> bool:
        """Whether the HTTPS listener is active."""
        return self._runner is not None

    @property
    def requests(self) -> tuple[RecordedRequest, ...]:
        """An immutable snapshot of requests observed by the resource handler."""
        return tuple(self._requests)

    @property
    def active_stall_count(self) -> int:
        """How many resource handlers are currently waiting in stall mode."""
        return len(self._active_stalls)

    def respond_with_power(self, watts: float) -> None:
        """Answer subsequent requests with a valid ``PowerConsumedWatts`` member."""
        self._release_stalls()
        self._plan = _ResponsePlan(_Behavior.VALID, watts=float(watts))

    def respond_without_power(self) -> None:
        """Answer subsequent requests with a valid JSON document lacking the power member."""
        self._release_stalls()
        self._plan = _ResponsePlan(_Behavior.ABSENT_POWER)

    def respond_with_status(self, status: int) -> None:
        """Answer subsequent requests with any valid three-digit HTTP status."""
        if not 100 <= status <= 599:
            raise ValueError(f"HTTP status must be between 100 and 599 inclusive, got {status}")
        self._release_stalls()
        self._plan = _ResponsePlan(_Behavior.STATUS, status=status)

    def stall(self) -> None:
        """Make subsequent requests wait until switched, released, cancelled, or closed."""
        self._release_stalls()
        self._stall_entered = asyncio.Event()
        self._stall_release = asyncio.Event()
        self._plan = _ResponsePlan(_Behavior.STALL)

    def release_stalls(self) -> None:
        """Release current stalled handlers without changing the configured behavior."""
        self._stall_release.set()

    async def wait_until_stalled(self, *, timeout_s: float = 1.0) -> None:
        """Wait until at least one handler enters the current stall generation."""
        async with asyncio.timeout(timeout_s):
            await self._stall_entered.wait()

    async def start(self) -> None:
        """Generate TLS material and start the loopback HTTPS listener."""
        if self.is_running:
            raise RuntimeError("the fake Redfish server is already running")

        await self._ensure_certificate()
        tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls_context.minimum_version = ssl.TLSVersion.TLSv1_2
        tls_context.load_cert_chain(certfile=self._certificate_path, keyfile=self._key_path)

        application = web.Application()
        application.router.add_get(DEFAULT_POWER_PATH, self._handle_power)
        runner = web.AppRunner(application, access_log=None, shutdown_timeout=_SHUTDOWN_TIMEOUT_S)
        await runner.setup()

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((_LOOPBACK_HOST, 0))
        listener.setblocking(False)
        port = int(listener.getsockname()[1])
        site = web.SockSite(runner, listener, ssl_context=tls_context)

        try:
            await site.start()
        except BaseException:
            listener.close()
            await runner.cleanup()
            raise

        self._port = port
        self._runner = runner
        self._site = site

    async def close(self) -> None:
        """Cancel stalled handlers and release every aiohttp and socket resource."""
        runner = self._runner
        if runner is None:
            return

        self._runner = None
        self._site = None
        self._stall_release.set()

        stalled = tuple(self._active_stalls)
        for task in stalled:
            task.cancel()
        if stalled:
            await asyncio.gather(*stalled, return_exceptions=True)
        self._active_stalls.clear()
        await runner.cleanup()

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        await self.close()

    def _release_stalls(self) -> None:
        self._stall_release.set()

    async def _handle_power(self, request: web.Request) -> web.StreamResponse:
        self._requests.append(
            RecordedRequest(
                method=request.method,
                path=request.path,
                authorization=request.headers.get("Authorization"),
            )
        )
        plan = self._plan
        if plan.behavior is _Behavior.VALID:
            return web.json_response({"PowerControl": [{"PowerConsumedWatts": plan.watts}]})
        if plan.behavior is _Behavior.ABSENT_POWER:
            return web.json_response({"PowerControl": [{"Name": "System Chassis"}]})
        if plan.behavior is _Behavior.STATUS:
            return web.Response(status=plan.status)
        return await self._stall_response(self._stall_release)

    async def _stall_response(self, release: asyncio.Event) -> web.StreamResponse:
        task = asyncio.current_task()
        if task is not None:
            self._active_stalls.add(task)
        self._stall_entered.set()
        try:
            await release.wait()
        finally:
            if task is not None:
                self._active_stalls.discard(task)
        return web.Response(status=503, text="configured stall released")

    async def _ensure_certificate(self) -> None:
        if self._certificate_path.is_file() and self._key_path.is_file():
            return

        executable = shutil.which("openssl")
        if executable is None:
            raise OpenSslUnavailableError(
                "openssl was not found; install the OpenSSL CLI so the fake Redfish server can generate "
                "a localhost self-signed certificate (the server will not fall back to HTTP)"
            )

        self._certificate_directory.mkdir(parents=True, exist_ok=True)
        process = await asyncio.create_subprocess_exec(
            executable,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-sha256",
            "-days",
            "1",
            "-nodes",
            "-keyout",
            str(self._key_path),
            "-out",
            str(self._certificate_path),
            "-subj",
            f"/CN={_LOCALHOST_NAME}",
            "-addext",
            f"subjectAltName=DNS:{_LOCALHOST_NAME},IP:{_LOOPBACK_HOST}",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await process.communicate()
        except asyncio.CancelledError:
            process.kill()
            await process.wait()
            raise

        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise CertificateGenerationError(
                "openssl could not generate the fake Redfish TLS certificate; ensure OpenSSL 1.1.1 or newer "
                f"supports 'req -addext'. Exit {process.returncode}: {detail or 'no error output'}"
            )
