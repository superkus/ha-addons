"""The shipped conformance suite, run against the bundled iDRAC plugin (Requirement 10.2).

This is the bundled plugin standing in for a third-party one: the same
:func:`~remote_energy_meter.plugins.conformance.assert_plugin_conforms` call that
``docs/writing-a-plugin.md`` tells an author to write, made against
:class:`~remote_energy_meter.plugins.idrac.plugin.IdracPlugin`. It establishes, for the plugin this addon ships, that
the identifier is a bounded non-empty string, that :class:`~remote_energy_meter.plugins.idrac.schema.IdracParams`
accepts a well-formed entry and rejects one with a required field removed, that ``validate_connection`` returns a
classified :class:`~remote_energy_meter.plugins.interface.ValidationResult`, and that ``read_power`` returns a
Power_Reading carrying the device identifier, watts within 0 to 100000, and a timezone-aware UTC timestamp. A failure
here is a failure of the plugin *or* of the suite, and either is worth knowing about before an author trusts the suite
against their own code.

## Why the reading is served by a double rather than by ``require_reading=False``

The suite's own affordance for a missing device is ``require_reading=False``, which accepts a classified
``ReadFailure`` and reports the reading-shape check as skipped. That is what the second test below uses. It is *not*
enough on its own for this plugin, for two reasons:

* **It would not cover Requirement 10.2's reading clause at all.** Skipping the reading-shape check is the point of
  the flag; the clause about watts and the UTC timestamp is exactly what this task has to establish for iDRAC.
* **Reaching the classified-failure path against a blackholed host would cost real seconds.** The suite backstops the
  bound it hands the plugin at ``timeout_s + CONFORMANCE_TIMEOUT_GRACE_S`` rather than at ``timeout_s``, so a plugin
  that honours the value it is handed -- this one does -- returns its own TIMEOUT ``ReadFailure`` first and the suite
  reports that. ``require_reading=False`` therefore works as documented against an unreachable host, including a
  blackholed TEST-NET-1 address. What it costs there is one wall-clock ``request_timeout_s`` per operation, spent
  waiting on a socket that will never answer, which is not what a unit-speed test should spend it on.

So both tests point the plugin's default session factory at an in-process double satisfying its ``RedfishSession``
protocol: no socket, no TLS, no resolver, nothing to blackhole. The second one has the double refuse the connection,
which is fast and deterministic and exercises the same ``require_reading=False`` path an author would run against a
real absent device. ``host`` is still a TEST-NET-1 address and ``request_timeout_s`` is still the schema minimum, so a
defect in this file's stubbing can only ever produce a fast failure against a documentation address rather than a slow
one against somebody's machine.

The double is installed by patching the module-level ``_default_session`` rather than by passing the plugin's
``session_factory`` keyword: the suite constructs the instance itself, from ``(device_id, params)`` alone, which is
precisely the constructor contract it is there to check. Subclassing to bind a factory would test a subclass.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from types import TracebackType
from typing import Any, Final

import aiohttp
import pytest

from remote_energy_meter.plugins.conformance import ConformanceCheck, assert_plugin_conforms
from remote_energy_meter.plugins.idrac import plugin as idrac_plugin
from remote_energy_meter.plugins.idrac.plugin import IdracPlugin, RedfishResponse, RedfishSession

# --------------------------------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------------------------------

#: A well-formed ``Device_Entry.params`` mapping for this family. ``host`` is a TEST-NET-1 address (RFC 5737), which
#: is reserved for documentation and routed nowhere, so a stubbing defect can never reach a real chassis.
#: ``request_timeout_s`` is the schema minimum for the same reason: fast, if it ever mattered.
VALID_PARAMS: Final[Mapping[str, Any]] = {
    "host": "192.0.2.10",
    "username": "root",
    "password": "conformance-pw",
    "verify_tls": False,
    "request_timeout_s": 1.0,
}

#: Requirement 10.2's missing-required-field case, stated explicitly rather than left to the suite's default. The
#: suite would derive its own by dropping the *first* required field, ``host``; naming ``password`` here means the two
#: tests in this module between them exercise both a dropped address and a dropped credential.
INCOMPLETE_PARAMS: Final[Mapping[str, Any]] = {key: value for key, value in VALID_PARAMS.items() if key != "password"}

#: The bound the suite applies to each operation. Well under the suite's own default: the double answers instantly,
#: and a hang would then be a defect to see quickly rather than to wait ten seconds for.
SUITE_TIMEOUT_S: Final = 1.0

#: The status a successful Redfish read returns.
HTTP_OK: Final = 200

#: A plausible chassis draw, inside both the plugin's 0-10000 W range and the suite's wider 0-100000 W one.
IDRAC_WATTS: Final = 224.5

#: A realistic iDRAC chassis ``Power`` body: the standard ``PowerControl`` member carrying ``PowerConsumedWatts``.
IDRAC_BODY: Final[bytes] = json.dumps(
    {
        "@odata.id": "/redfish/v1/Chassis/System.Embedded.1/Power",
        "PowerControl": [
            {"MemberId": "PowerControl", "PowerConsumedWatts": IDRAC_WATTS, "PowerCapacityWatts": 1600},
        ],
    }
).encode()

#: How many requests one suite run issues: one for ``validate_connection`` and one for ``read_power``, which is the
#: single request per operation of Requirements 4.3 and 4.13.
OPERATIONS_PER_RUN: Final = 2


# --------------------------------------------------------------------------------------------------
# the HTTP double (satisfies the plugin's RedfishSession / RedfishResponse protocols structurally)
# --------------------------------------------------------------------------------------------------


class _StubResponse:
    """A response carrying a fixed status and body. Satisfies ``RedfishResponse``."""

    __slots__ = ("_body", "_status")

    def __init__(self, status: int, body: bytes) -> None:
        self._status = status
        self._body = body

    @property
    def status(self) -> int:
        return self._status

    @property
    def content_length(self) -> int | None:
        return None

    async def read(self) -> bytes:
        return self._body


class _StubRequest(AbstractAsyncContextManager[RedfishResponse]):
    """The context manager one ``get`` call returns, yielding the configured response."""

    __slots__ = ("_response",)

    def __init__(self, response: RedfishResponse) -> None:
        self._response = response

    async def __aenter__(self) -> RedfishResponse:
        return self._response

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        return None


class _StubSession:
    """A session that answers every request with one JSON body, or fails every request with one error.

    Satisfies ``RedfishSession`` structurally, which ``mypy --strict`` checks where the instance is handed to
    :func:`_install_stub_session`: the double is held to the exact call the plugin makes.
    """

    __slots__ = ("_body", "_closed", "_error", "_status", "request_count")

    def __init__(self, body: bytes, *, status: int = HTTP_OK, error: Exception | None = None) -> None:
        self._body = body
        self._status = status
        self._error = error
        self._closed = False
        self.request_count = 0

    @property
    def closed(self) -> bool:
        return self._closed

    async def close(self) -> None:
        self._closed = True

    def get(
        self,
        url: str,
        /,
        *,
        auth: aiohttp.BasicAuth,
        ssl: bool,
        allow_redirects: bool,
        timeout: aiohttp.ClientTimeout,
        headers: Mapping[str, str],
    ) -> _StubRequest:
        self.request_count += 1
        if self._error is not None:
            raise self._error
        return _StubRequest(_StubResponse(self._status, self._body))


def _install_stub_session(monkeypatch: pytest.MonkeyPatch, session: RedfishSession) -> None:
    """Make every :class:`IdracPlugin` built during this test read through ``session`` instead of a socket.

    The suite constructs the instance from ``(device_id, params)``, so the seam has to be the plugin module's default
    session factory rather than the constructor keyword a test would normally use.
    """

    def factory() -> RedfishSession:
        return session

    monkeypatch.setattr(idrac_plugin, "_default_session", factory)


# --------------------------------------------------------------------------------------------------
# the suite, against a device that answers
# --------------------------------------------------------------------------------------------------


def test_the_idrac_plugin_satisfies_every_conformance_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Requirement 10.2 in full: identifier, schema both ways, classified validation, well-formed reading.

    ``require_reading`` is left at its default, so the reading-shape check is *run* rather than skipped, and the
    empty ``skipped`` assertion is what keeps this test honest -- a suite that quietly skipped half its checks would
    otherwise read as a pass.
    """
    session = _StubSession(body=IDRAC_BODY)
    _install_stub_session(monkeypatch, session)

    report = assert_plugin_conforms(
        IdracPlugin,
        valid_params=VALID_PARAMS,
        invalid_params=INCOMPLETE_PARAMS,
        timeout_s=SUITE_TIMEOUT_S,
    )

    assert report.ok, report.summary()
    assert report.skipped == (), report.summary()
    assert session.request_count == OPERATIONS_PER_RUN


# --------------------------------------------------------------------------------------------------
# the suite, against a device that is not there
# --------------------------------------------------------------------------------------------------


def test_the_idrac_plugin_conforms_with_nothing_to_read_from(monkeypatch: pytest.MonkeyPatch) -> None:
    """The documented author path: ``require_reading=False`` accepts a classified failure and skips the shape check.

    The double refuses the connection rather than answering it -- ``ConnectionRefusedError`` is the ``OSError``
    aiohttp reports underneath a ``ClientConnectorError``, and the plugin classifies both as COMMUNICATION -- so this
    is the plugin's own failure path, reached without a socket. ``invalid_params`` is omitted here, which exercises
    the suite's own derivation of the missing-required-field case.
    """
    session = _StubSession(body=IDRAC_BODY, error=ConnectionRefusedError("connection refused"))
    _install_stub_session(monkeypatch, session)

    report = assert_plugin_conforms(
        IdracPlugin,
        valid_params=VALID_PARAMS,
        timeout_s=SUITE_TIMEOUT_S,
        require_reading=False,
    )

    assert report.ok, report.summary()
    assert [(skip.element, skip.check) for skip in report.skipped] == [
        ("read_power", ConformanceCheck.READING_SHAPE)
    ], report.summary()
    assert session.request_count == OPERATIONS_PER_RUN
