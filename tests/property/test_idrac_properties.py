"""Properties 9 through 12 for the iDRAC plugin's read path (tasks 10.4 through 10.7).

*For any* power value that is numeric and within 0-10000 watts inclusive, embedded in any Redfish power
payload shape that carries it, the iDRAC plugin returns a Power_Reading whose device identifier equals the
Device_Entry identifier, whose watts value equals the payload value, and whose timestamp is timezone-aware
UTC and not earlier than the moment the request was issued (Requirement 4.4).

*For any* unusable response -- the power member absent (a response-format failure naming the requested
path), a present value that is non-numeric or outside 0-10000 W (an invalid-measurement failure), or an
unsuccessful HTTP status other than 401 and 403 (a communication failure naming the host) -- the plugin
*returns* that classified failure rather than raising, and nothing reaches the Energy_Accumulator
(Property 10, Requirements 4.5, 4.11, 4.12).

*For any* Redfish response, the connection validation operation issues exactly one request and returns a
successful result if and only if a read of that *same* response would yield a Power_Reading -- and when it
does not succeed it carries the classification that read would have carried (Property 11, Requirement 4.13).

## Scope of this module, and what will be appended to it

This is the property module for the iDRAC plugin's response handling, and it now carries Properties 9, 10,
and 11 over one subject; task 10.7 adds Property 12 next -- each read carries its own entry's credentials --
and it belongs here rather than in a module of its own, because all four quantify over generated Redfish
payloads against one plugin. The file is laid out by concern under section banners: shared constants and the
HTTP double first, then the generators, then one section per property. Property 9 needs *valid, carrying*
payloads; Property 10 produces the absent, mistyped, and out-of-range payloads Property 9 deliberately
excludes, so the two generator families sit side by side rather than one subsuming the other; Property 11
draws from both families at once and adds the refused and abandoned transports, because agreement between
the two operations only means anything if it is checked across usable and unusable responses alike.

## Two subjects, one property

The faithful-value core is the pure function
:func:`~remote_energy_meter.plugins.idrac.redfish.extract_watts`: given a decoded payload it returns the
watt value or a classified failure, with no clock and no I/O, which is what lets the value be checked across
the whole input space cheaply. The reading half is
:meth:`~remote_energy_meter.plugins.idrac.plugin.IdracPlugin.read_power` driven by an in-process double that
satisfies the plugin's own ``RedfishSession`` protocol -- no socket, no TLS, no real HTTP -- so the
Power_Reading's device identifier and timestamp can be asserted without a server. Both are exercised over
the same generated payload, because Property 9 is one statement that reaches from the pure extractor through
to the reading.

## What "any shape that carries it" generates

A generated payload places the value in exactly one of the three locations the plugin accepts, drawn as a
:class:`Placement`:

* **STANDARD** -- ``PowerControl[k].PowerConsumedWatts``, the ordinary iDRAC shape, with decoy members
  before and after the carrier so "the first member that carries the value" and "skip members lacking it"
  are both exercised;
* **OEM_MEMBER** -- ``PowerControl[k].Oem.Dell.PowerConsumedWatts``, the documented Dell fallback consulted
  inside a member when no member carries the standard key;
* **OEM_TOP** -- top-level ``Oem.Dell.PowerConsumedWatts``, where firmware publishing no usable
  ``PowerControl`` array puts it.

Decoy members and decoy top-level keys are drawn only from :data:`DECOY_KEYS`, a set that excludes
``PowerConsumedWatts``, ``PowerControl``, ``Oem``, and ``Dell`` outright. That is the load-bearing
generator invariant: the power key appears **exactly once** in the whole payload, at the intended location,
so which value the extractor locates is never ambiguous and never depends on a search order this test would
otherwise have to assume. :func:`test_each_valid_payload_carries_exactly_one_power_value` machine-checks
that invariant with an independent recursive walk rather than trusting it in prose.

## Why the expectation is independent of the implementation

The expected watts is ``float(value)`` computed from the value this module generated and embedded -- never
from :func:`extract_watts`, which is the thing under test. The 0-10000 W range is written out here as
:data:`MIN_VALID_WATTS` and :data:`MAX_VALID_WATTS` rather than imported from ``redfish.py``: a bound
imported from the implementation would move the generator and the oracle together with any change on the
other side, and the property would keep passing while faithfulness quietly broke. Written out, the two
disagree the moment the implementation's range does.

"Faithful" is asserted with **exact** equality -- no rounding, clamping, or distortion -- for both integer
and floating-point values, including the inclusive bounds 0 and 10000 in both forms. The reading path sends
the payload through ``json.dumps``/``json.loads`` as a real response would; a finite float survives that
round trip bit-for-bit in CPython (``json`` renders the shortest string that reparses to the same double),
so the value the plugin sees is the value generated here and ``float(value)`` remains the right expectation.

## The timestamp clause

The plugin reads its clock at response *receipt*, so the timestamp is naturally not earlier than the moment
the request was issued (Requirement 4.4). This module leaves the plugin on its real UTC clock rather than
injecting a fixed one, and has the double record the instant it is handed the request; the assertion then
bounds the reading's timestamp between that issue instant and a wall-clock reading taken after the call
returns, and checks it is timezone-aware UTC. A fixed clock would make the ordering vacuous, because it
would no longer relate to when the request was actually issued.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import TracebackType
from typing import Any, Final, NamedTuple

import aiohttp
from hypothesis import event, given, settings
from hypothesis import strategies as st
from pydantic import SecretStr

from remote_energy_meter.energy import EnergyAccumulator
from remote_energy_meter.persistence import DeviceEnergyState
from remote_energy_meter.plugins.idrac.plugin import IdracPlugin, RedfishResponse
from remote_energy_meter.plugins.idrac.redfish import extract_watts
from remote_energy_meter.plugins.idrac.schema import IdracParams
from remote_energy_meter.plugins.interface import (
    FailureClassification,
    PowerReading,
    ReadFailure,
    ReadResult,
    ValidationResult,
)

# --------------------------------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------------------------------

#: The Device_Entry identifier the plugin carries onto every reading; a hyphen is exercised on purpose.
DEVICE_ID: Final = "poweredge-a"

#: Connection parameters for the double. The credentials are arbitrary: Property 9 is about the value and
#: the timestamp, and Property 12 (task 10.7) is where per-entry credentials become the subject.
IDRAC_HOST: Final = "192.0.2.9"
IDRAC_USERNAME: Final = "root"
IDRAC_PASSWORD: Final = "s3cr3t-idrac-pw"

#: The resource path handed to the extractor and requested by the read. Any string serves; a literal keeps
#: this module from importing the implementation's default only to feed it back in.
REQUESTED_POWER_PATH: Final = "/redfish/v1/Chassis/System.Embedded.1/Power"

#: A per-request bound the double never reaches, since it answers instantly.
REQUEST_TIMEOUT_S: Final = 5.0

#: The status a successful Redfish read returns.
HTTP_OK: Final = 200

#: Requirement 4.4's accepted range, in watts, inclusive at both ends. Written out rather than imported
#: from ``redfish.py`` so the oracle cannot drift with the implementation -- see the module docstring.
MIN_VALID_WATTS: Final = 0.0
MAX_VALID_WATTS: Final = 10000.0

#: The same upper bound as a whole number, for the integer arm of the value generator.
MAX_VALID_WATTS_INT: Final = 10000

#: Watt values worth drawing on purpose: both inclusive bounds in integer and float form, a couple of
#: exactly representable fractions, and mid-range values. Full-range coverage comes from the strategies in
#: :func:`valid_watts`; these pin the boundaries a uniform draw reaches too rarely.
NOTABLE_WATTS: Final[tuple[int | float, ...]] = (
    0,
    MAX_VALID_WATTS_INT,
    0.0,
    MAX_VALID_WATTS,
    1,
    224,
    224.5,
    0.5,
    9999.5,
    5000.0,
)

#: The Redfish/Dell wire names this module builds payloads from. These are the device's published contract,
#: not the implementation's private choices, so they are spelled out here and the generator stays readable.
POWER_CONTROL_KEY: Final = "PowerControl"
POWER_WATTS_KEY: Final = "PowerConsumedWatts"
OEM_KEY: Final = "Oem"
OEM_VENDOR_KEY: Final = "Dell"

#: Keys a decoy member or a decoy top-level object may carry. The set excludes every key the extractor
#: keys off -- ``PowerConsumedWatts``, ``PowerControl``, ``Oem``, ``Dell`` -- so a decoy can never introduce
#: a second power value and the "exactly one occurrence" invariant holds by construction.
DECOY_KEYS: Final[tuple[str, ...]] = (
    "@odata.id",
    "MemberId",
    "Name",
    "PowerCapacityWatts",
    "PowerAllocatedWatts",
    "PhysicalContext",
    "PowerLimit",
)


class Placement(StrEnum):
    """Which of the three accepted locations a generated payload carries the value in."""

    STANDARD = "standard"
    OEM_MEMBER = "oem_member"
    OEM_TOP = "oem_top"


class ValidPayload(NamedTuple):
    """One generated case: the value, the payload carrying it, and where it was placed (for reporting)."""

    value: int | float
    payload: dict[str, Any]
    placement: Placement


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

    async def read(self) -> bytes:
        return self._body


class _StubRequest(AbstractAsyncContextManager[RedfishResponse]):
    """The context manager one ``get`` call returns, yielding the configured response.

    A configured ``error`` is raised on entry instead of the response being yielded, which is where a real
    transport failure surfaces: the request has gone out, and awaiting its response is what fails. Property
    11 (task 10.6) uses that to reach the abandoned-request arm without a slow socket.

    With ``pause`` set, one event-loop tick passes between the request being recorded and the response being
    handed over. Property 12 (task 10.7) gathers reads across several plugin instances and needs their
    requests to genuinely overlap: with the pause, every gathered read is suspended here, its request already
    recorded, before any of them is answered.
    """

    __slots__ = ("_error", "_pause", "_response")

    def __init__(
        self,
        response: RedfishResponse,
        *,
        error: BaseException | None = None,
        pause: bool = False,
    ) -> None:
        self._response = response
        self._error = error
        self._pause = pause

    async def __aenter__(self) -> RedfishResponse:
        if self._pause:
            await asyncio.sleep(0)
        if self._error is not None:
            raise self._error
        return self._response

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        return None


class _RecordedCall(NamedTuple):
    """One ``get`` call as the double received it: the URL and every keyword the plugin passed.

    Recorded so a property can ask what a request *carried* rather than only how many there were. Property 12
    (task 10.7) reads the ``url`` and the ``auth`` of every call; the remaining fields are kept because they
    arrive together and recording them costs nothing.
    """

    url: str
    auth: aiohttp.BasicAuth
    ssl: bool
    allow_redirects: bool
    timeout: aiohttp.ClientTimeout
    headers: Mapping[str, str]


class _StubSession:
    """A session that answers every request with one JSON body, recording when, how often, and with what.

    Satisfies ``RedfishSession`` structurally, which ``mypy --strict`` checks at the construction site: the
    double is held to the exact call ``read_power`` makes, not to an arbitrary one. With ``error`` set it
    answers no request at all and fails the attempt instead, standing in for a transport that never replied.
    With ``pause`` set, each request yields the event loop once before being answered, so reads gathered
    across several sessions overlap instead of running to completion one after another.
    """

    __slots__ = ("_body", "_closed", "_error", "_pause", "_status", "calls", "issued_at", "request_count")

    def __init__(
        self,
        body: bytes,
        *,
        status: int = HTTP_OK,
        error: BaseException | None = None,
        pause: bool = False,
    ) -> None:
        self._body = body
        self._status = status
        self._error = error
        self._pause = pause
        self._closed = False
        self.issued_at: datetime | None = None
        self.request_count = 0
        #: Every request this session was handed, in order (Property 12 reads its credentials and its URL).
        self.calls: list[_RecordedCall] = []

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
        # The instant the request is issued: Requirement 4.4 requires the reading's timestamp to be no
        # earlier than this moment, and the plugin stamps it later, at response receipt.
        self.issued_at = datetime.now(tz=UTC)
        self.request_count += 1
        # The request is counted and recorded before a configured transport error surfaces, so both "exactly
        # one request" and what that request carried stay observable on an attempt that never got a response.
        self.calls.append(
            _RecordedCall(
                url=url,
                auth=auth,
                ssl=ssl,
                allow_redirects=allow_redirects,
                timeout=timeout,
                headers=headers,
            )
        )
        return _StubRequest(_StubResponse(self._status, self._body), error=self._error, pause=self._pause)


def _idrac_params() -> IdracParams:
    """Valid iDRAC parameters for the double. Credentials are wrapped as the schema declares them."""
    return IdracParams(
        host=IDRAC_HOST,
        username=SecretStr(IDRAC_USERNAME),
        password=SecretStr(IDRAC_PASSWORD),
    )


async def _read_once(payload: dict[str, Any]) -> tuple[ReadResult, _StubSession]:
    """Drive one ``read_power`` against a double serving ``payload`` as a JSON body.

    Returns the outcome together with the session, so the caller can assert the request was issued exactly
    once and read the instant it was issued.
    """
    session = _StubSession(body=json.dumps(payload).encode())
    plugin = IdracPlugin(DEVICE_ID, _idrac_params(), session_factory=lambda: session)
    try:
        result = await plugin.read_power(REQUEST_TIMEOUT_S)
    finally:
        await plugin.aclose()
    return result, session


# --------------------------------------------------------------------------------------------------
# generators
# --------------------------------------------------------------------------------------------------


def valid_watts() -> st.SearchStrategy[int | float]:
    """A power value that is numeric and within 0-10000 W inclusive (Requirement 4.4).

    Three arms: the whole integer range, the whole float range, and the notable boundary and fraction
    values a uniform draw would reach too rarely. ``bool`` is excluded -- it is an ``int`` subclass the
    plugin rejects as malformed -- and so are the non-finite floats, which are out of range anyway.
    """
    return st.one_of(
        st.integers(min_value=0, max_value=MAX_VALID_WATTS_INT),
        st.floats(
            min_value=MIN_VALID_WATTS,
            max_value=MAX_VALID_WATTS,
            allow_nan=False,
            allow_infinity=False,
        ),
        st.sampled_from(NOTABLE_WATTS),
    )


def _decoy_scalars() -> st.SearchStrategy[Any]:
    """A JSON-expressible scalar for a decoy field. Never a power value the extractor would read."""
    return st.one_of(
        st.none(),
        st.booleans(),
        st.integers(min_value=-1000, max_value=20000),
        st.floats(allow_nan=False, allow_infinity=False),
        st.sampled_from(("n/a", "OK", "System.Embedded.1", "")),
    )


@st.composite
def _decoy_object(draw: st.DrawFn) -> dict[str, Any]:
    """An object built only from :data:`DECOY_KEYS`, so it can carry no power value.

    Used for members that must not carry the power key, for sibling keys next to it, and for top-level
    decorations. Possibly empty, which is a valid member too.
    """
    keys = draw(st.lists(st.sampled_from(DECOY_KEYS), unique=True, max_size=len(DECOY_KEYS)))
    return {key: draw(_decoy_scalars()) for key in keys}


@st.composite
def _standard_payload(draw: st.DrawFn, value: object) -> dict[str, Any]:
    """A payload carrying ``value`` at ``PowerControl[k].PowerConsumedWatts``.

    Decoy members precede and follow the carrier, none of them carrying the power key, so the carrier is
    the sole member that does and the "first carrying member wins" rule resolves to it.
    """
    lead = draw(st.lists(_decoy_object(), max_size=2))
    carrier = {**draw(_decoy_object()), POWER_WATTS_KEY: value}
    trail = draw(st.lists(_decoy_object(), max_size=2))
    payload = draw(_decoy_object())
    payload[POWER_CONTROL_KEY] = [*lead, carrier, *trail]
    return payload


@st.composite
def _oem_member_payload(draw: st.DrawFn, value: object) -> dict[str, Any]:
    """A payload carrying ``value`` at ``PowerControl[k].Oem.Dell.PowerConsumedWatts``.

    No member carries the standard key -- every member but the carrier is a decoy, and the carrier holds
    the value only under ``Oem.Dell`` -- so the extractor falls through to the Dell variant and finds it in
    the one member that has it.
    """
    lead = draw(st.lists(_decoy_object(), max_size=2))
    dell = {**draw(_decoy_object()), POWER_WATTS_KEY: value}
    carrier = {**draw(_decoy_object()), OEM_KEY: {OEM_VENDOR_KEY: dell}}
    trail = draw(st.lists(_decoy_object(), max_size=2))
    payload = draw(_decoy_object())
    payload[POWER_CONTROL_KEY] = [*lead, carrier, *trail]
    return payload


@st.composite
def _oem_top_payload(draw: st.DrawFn, value: object) -> dict[str, Any]:
    """A payload carrying ``value`` at top-level ``Oem.Dell.PowerConsumedWatts``.

    The ``PowerControl`` array is either absent or present with decoy members only, so no member carries
    the standard key or the Dell variant, and the top-level Dell block is the sole source.
    """
    dell = {**draw(_decoy_object()), POWER_WATTS_KEY: value}
    payload = draw(_decoy_object())
    if draw(st.booleans()):
        payload[POWER_CONTROL_KEY] = draw(st.lists(_decoy_object(), max_size=3))
    payload[OEM_KEY] = {OEM_VENDOR_KEY: dell}
    return payload


@st.composite
def valid_power_payloads(draw: st.DrawFn) -> ValidPayload:
    """A valid Redfish power payload carrying one in-range value at one accepted location."""
    value = draw(valid_watts())
    placement = draw(st.sampled_from(Placement))
    if placement is Placement.STANDARD:
        payload = draw(_standard_payload(value))
    elif placement is Placement.OEM_MEMBER:
        payload = draw(_oem_member_payload(value))
    else:
        payload = draw(_oem_top_payload(value))
    return ValidPayload(value=value, payload=payload, placement=placement)


# --------------------------------------------------------------------------------------------------
# the independent oracle for the generator invariant
# --------------------------------------------------------------------------------------------------


def _power_values(node: object) -> list[object]:
    """Every value stored under a ``PowerConsumedWatts`` key anywhere in ``node``.

    A recursive walk written here rather than borrowed from the extractor, so the generator invariant is
    checked against an independent reading of the payload. A correct generator leaves exactly one such
    value; more than one would make the located value ambiguous.
    """
    found: list[object] = []
    if isinstance(node, Mapping):
        for key, child in node.items():
            if key == POWER_WATTS_KEY:
                found.append(child)
            found.extend(_power_values(child))
    elif isinstance(node, list):
        for item in node:
            found.extend(_power_values(item))
    return found


# --------------------------------------------------------------------------------------------------
# generator guard
# --------------------------------------------------------------------------------------------------


@settings(max_examples=150, deadline=None)
@given(case=valid_power_payloads())
def test_each_valid_payload_carries_exactly_one_power_value(case: ValidPayload) -> None:
    """The invariant the property rests on: one power value, at the location this module chose.

    Machine-checked with :func:`_power_values` rather than asserted in prose, so a generator that grew a
    second occurrence -- a decoy that reached the power key, a carrier duplicated -- fails here for the
    generator's reasons instead of contaminating Property 9.
    """
    values = _power_values(case.payload)

    assert len(values) == 1
    assert values[0] == case.value
    # Same type as generated, so an ``int`` was not silently rewritten to a ``float`` in the payload.
    assert type(values[0]) is type(case.value)


# --------------------------------------------------------------------------------------------------
# Property 9: valid Redfish responses yield faithful readings
# --------------------------------------------------------------------------------------------------


# Feature: remote-energy-meter, Property 9: Valid Redfish responses yield faithful readings
# Validates: Requirements 4.4
@settings(max_examples=150, deadline=None)
@given(case=valid_power_payloads())
def test_valid_redfish_responses_yield_faithful_readings(case: ValidPayload) -> None:
    """Requirement 4.4, over every valid value and every carrying payload shape.

    The pure extraction yields the exact wattage, and a successful read carries that wattage, the device
    identifier, and a timezone-aware UTC timestamp no earlier than the moment the request was issued.
    """
    event(f"placement: {case.placement.value}")
    event(f"value type: {type(case.value).__name__}")

    # The expectation, built independently of the implementation: the plain float of the generated value.
    expected = float(case.value)

    # --- the faithful-value core: the pure extractor returns the exact watts, undistorted ---
    watts = extract_watts(case.payload, device_id=DEVICE_ID, power_path=REQUESTED_POWER_PATH)
    assert not isinstance(watts, ReadFailure), watts
    # Exact equality: no rounding, no clamping, no coercion, for both integer and float inputs.
    assert watts == expected

    # --- a successful read carries that wattage, the device id, and a UTC receipt timestamp ---
    result, session = asyncio.run(_read_once(case.payload))
    after = datetime.now(tz=UTC)

    assert not isinstance(result, ReadFailure), result
    assert result.device_id == DEVICE_ID
    assert result.watts == expected

    # Exactly one request produced the reading (the read half of Requirement 4.3, pinned in full by
    # Property 12); asserted here so the reading is known to have come through the HTTP path.
    assert session.request_count == 1

    # The timestamp is timezone-aware UTC ...
    assert result.measured_at.tzinfo is not None
    assert result.measured_at.utcoffset() == timedelta(0)

    # ... and not earlier than the moment the request was issued, bounded above by a reading taken after
    # the call returned. ``<=`` throughout, because the wall clock can report the same microsecond twice.
    assert session.issued_at is not None
    assert session.issued_at <= result.measured_at <= after


# --------------------------------------------------------------------------------------------------
# Property 10: the taxonomy of unusable responses, and the expectation each is given
# --------------------------------------------------------------------------------------------------
#
# The three unusable shapes Requirements 4.5, 4.11, and 4.12 draw. The classification and the named
# detail field are assigned *here*, from the shape this module builds -- never by asking the
# implementation what it would return. A case tagged RESPONSE_FORMAT is tagged so because this module
# omitted the power member, not because ``extract_watts`` said so; the test then fails the moment the
# implementation's own mapping drifts from that independent reading of the requirements.


class UnusableKind(StrEnum):
    """Which unusable shape a generated case carries, and hence which classification it must be given."""

    ABSENT = "absent"  # -> RESPONSE_FORMAT (Requirement 4.5)
    INVALID = "invalid"  # -> INVALID_MEASUREMENT (Requirement 4.11)
    HTTP_STATUS = "http_status"  # -> COMMUNICATION (Requirement 4.12)


class DetailNames(StrEnum):
    """Whether the failure detail must name the requested resource path (4.5, 4.11) or the host (4.12)."""

    PATH = "path"
    HOST = "host"


class AbsentShape(StrEnum):
    """The structurally distinct ways a payload can omit the power member, each a RESPONSE_FORMAT cause."""

    NON_OBJECT = "non_object"
    NO_POWER_CONTROL = "no_power_control"
    POWER_CONTROL_NOT_ARRAY = "power_control_not_array"
    POWER_CONTROL_EMPTY = "power_control_empty"
    MEMBERS_WITHOUT_POWER = "members_without_power"


class AbsentCase(NamedTuple):
    """A payload with no power member anywhere, tagged with the shape that made it absent (for reporting)."""

    shape: AbsentShape
    payload: object


class InvalidCase(NamedTuple):
    """A payload carrying exactly one present-but-unusable power value, at one accepted location."""

    value: object
    payload: dict[str, Any]
    placement: Placement


class UnusableCase(NamedTuple):
    """One Property 10 case across all three arms, carrying the independently assigned expectation.

    ``payload`` is the decoded body the extractor and the read see; ``status`` is the HTTP status the read's
    double answers with -- :data:`HTTP_OK` for the two body arms, an unsuccessful status for the third.
    """

    kind: UnusableKind
    expected: FailureClassification
    names: DetailNames
    payload: object
    status: int
    label: str


# --------------------------------------------------------------------------------------------------
# generators: the absent shapes (RESPONSE_FORMAT)
# --------------------------------------------------------------------------------------------------


def _non_object_body() -> st.SearchStrategy[object]:
    """A JSON body that decodes to something other than an object: null, a scalar, or an array.

    The extractor reads power only from an object, so any of these carries none. Every draw is JSON
    round-trippable, so the same value reaches the read path through ``json.dumps``/``json.loads`` unchanged.
    """
    return st.one_of(
        st.none(),
        st.booleans(),
        st.integers(min_value=-1_000_000, max_value=1_000_000),
        st.floats(allow_nan=False, allow_infinity=False, min_value=-1e6, max_value=1e6),
        st.sampled_from(("", "n/a", "PowerControl", "224", "not-a-json-object")),
        st.lists(_decoy_scalars(), max_size=4),
    )


@st.composite
def _no_power_control(draw: st.DrawFn) -> dict[str, Any]:
    """An object with no ``PowerControl`` at all, optionally with a Dell block that still carries no power."""
    payload = draw(_decoy_object())
    if draw(st.booleans()):
        payload[OEM_KEY] = {OEM_VENDOR_KEY: draw(_decoy_object())}
    return payload


@st.composite
def _power_control_not_array(draw: st.DrawFn) -> dict[str, Any]:
    """An object whose ``PowerControl`` is present but is not an array, so no member can be searched."""
    payload = draw(_decoy_object())
    payload[POWER_CONTROL_KEY] = draw(
        st.one_of(
            _decoy_object(),
            st.sampled_from(("PowerControl", "", "1")),
            st.integers(min_value=-10, max_value=10),
            st.none(),
            st.booleans(),
        )
    )
    return payload


@st.composite
def _power_control_empty(draw: st.DrawFn) -> dict[str, Any]:
    """An object whose ``PowerControl`` is an empty array: a chassis publishing no control member."""
    payload = draw(_decoy_object())
    payload[POWER_CONTROL_KEY] = []
    return payload


@st.composite
def _members_without_power(draw: st.DrawFn) -> dict[str, Any]:
    """A ``PowerControl`` array of decoy members, none carrying the standard key or a Dell power variant."""
    members: list[dict[str, Any]] = []
    for _ in range(draw(st.integers(min_value=1, max_value=4))):
        member = draw(_decoy_object())
        if draw(st.booleans()):
            member[OEM_KEY] = {OEM_VENDOR_KEY: draw(_decoy_object())}
        members.append(member)
    payload = draw(_decoy_object())
    payload[POWER_CONTROL_KEY] = members
    if draw(st.booleans()):
        payload[OEM_KEY] = {OEM_VENDOR_KEY: draw(_decoy_object())}
    return payload


@st.composite
def absent_payloads(draw: st.DrawFn) -> AbsentCase:
    """A payload from which no power value can be read, in any of the distinct absent shapes (Requirement 4.5)."""
    shape = draw(st.sampled_from(AbsentShape))
    if shape is AbsentShape.NON_OBJECT:
        payload: object = draw(_non_object_body())
    elif shape is AbsentShape.NO_POWER_CONTROL:
        payload = draw(_no_power_control())
    elif shape is AbsentShape.POWER_CONTROL_NOT_ARRAY:
        payload = draw(_power_control_not_array())
    elif shape is AbsentShape.POWER_CONTROL_EMPTY:
        payload = draw(_power_control_empty())
    else:
        payload = draw(_members_without_power())
    return AbsentCase(shape=shape, payload=payload)


# --------------------------------------------------------------------------------------------------
# generators: the present-but-unusable values (INVALID_MEASUREMENT)
# --------------------------------------------------------------------------------------------------


def _non_numeric_values() -> st.SearchStrategy[object]:
    """A present power value that is not a JSON number: null, a boolean, a string, or a container.

    ``bool`` is included deliberately -- it is an ``int`` subclass the plugin rejects rather than reading as
    1 W. Containers are built from decoys, so the power key still appears exactly once, at the carrier.
    """
    return st.one_of(
        st.none(),
        st.booleans(),
        st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126), max_size=24),
        st.sampled_from(("224", "n/a", "", "1e3", "NaN", "1,024", " 100 ")),
        st.lists(_decoy_scalars(), max_size=3),
        _decoy_object(),
    )


def _out_of_range_numbers() -> st.SearchStrategy[object]:
    """A present power value that is a number strictly below 0 W or strictly above 10000 W.

    Integer magnitudes are unbounded on the out-of-range side, including values past the IEEE-754 double range: a
    JSON integer has no width limit, so ``json.loads`` hands the extractor a Python ``int`` of any size, and such a
    value is out of range in exactly the sense Requirement 4.11 means. The two explicitly sampled 401-digit values
    guarantee that arm is drawn rather than left to chance.
    """
    return st.one_of(
        st.integers(max_value=-1),
        st.integers(min_value=MAX_VALID_WATTS_INT + 1),
        st.floats(max_value=MIN_VALID_WATTS, exclude_max=True, allow_nan=False, allow_infinity=False),
        st.floats(min_value=MAX_VALID_WATTS, exclude_min=True, allow_nan=False, allow_infinity=False),
        st.sampled_from((-1, -0.5, -10000, 10001, 10000.5, 99999.0, 1_000_000, 10**400, -(10**400))),
    )


def _non_finite_values() -> st.SearchStrategy[object]:
    """A present power value that is a float but not finite: NaN or an infinity (Requirement 4.11)."""
    return st.sampled_from((float("nan"), float("inf"), float("-inf")))


def invalid_watts() -> st.SearchStrategy[object]:
    """A present power value the plugin must reject as an invalid measurement (Requirement 4.11)."""
    return st.one_of(_non_numeric_values(), _out_of_range_numbers(), _non_finite_values())


@st.composite
def invalid_payloads(draw: st.DrawFn) -> InvalidCase:
    """A payload carrying one present-but-unusable value at one of the three accepted locations.

    Reuses Property 9's placement builders, so the invalid value sits exactly where a valid one would, and
    the located-then-rejected path is exercised rather than an absent-member path.
    """
    value = draw(invalid_watts())
    placement = draw(st.sampled_from(Placement))
    if placement is Placement.STANDARD:
        payload = draw(_standard_payload(value))
    elif placement is Placement.OEM_MEMBER:
        payload = draw(_oem_member_payload(value))
    else:
        payload = draw(_oem_top_payload(value))
    return InvalidCase(value=value, payload=payload, placement=placement)


# --------------------------------------------------------------------------------------------------
# generators: unsuccessful HTTP statuses (COMMUNICATION)
# --------------------------------------------------------------------------------------------------

#: The two authentication statuses, and hence the boundary of the communication arm, written out here
#: rather than imported from the plugin so the oracle for "which status is a communication failure" stays
#: independent of the implementation's own status constants.
_AUTH_STATUSES: Final = frozenset({401, 403})


def unsuccessful_statuses() -> st.SearchStrategy[int]:
    """An HTTP status that is neither a 2xx success nor a 401/403 refusal (Requirement 4.12).

    Drawn from 300-599 so every value lies outside the success band by construction, with 401 and 403
    removed; the 3xx values exercise the plugin's documented decision to treat a redirect as a communication
    failure because it follows none.
    """
    notable = st.sampled_from((300, 301, 302, 307, 400, 402, 404, 405, 409, 429, 500, 502, 503, 504))
    ranged = st.integers(min_value=300, max_value=599).filter(lambda status: status not in _AUTH_STATUSES)
    return st.one_of(notable, ranged)


@st.composite
def unusable_responses(draw: st.DrawFn) -> UnusableCase:
    """One unusable response across all three arms, each tagged with its independently assigned expectation."""
    kind = draw(st.sampled_from(UnusableKind))
    if kind is UnusableKind.ABSENT:
        absent = draw(absent_payloads())
        return UnusableCase(
            kind=kind,
            expected=FailureClassification.RESPONSE_FORMAT,
            names=DetailNames.PATH,
            payload=absent.payload,
            status=HTTP_OK,
            label=f"absent:{absent.shape.value}",
        )
    if kind is UnusableKind.INVALID:
        invalid = draw(invalid_payloads())
        return UnusableCase(
            kind=kind,
            expected=FailureClassification.INVALID_MEASUREMENT,
            names=DetailNames.PATH,
            payload=invalid.payload,
            status=HTTP_OK,
            label=f"invalid:{invalid.placement.value}:{type(invalid.value).__name__}",
        )
    status = draw(unsuccessful_statuses())
    return UnusableCase(
        kind=kind,
        expected=FailureClassification.COMMUNICATION,
        names=DetailNames.HOST,
        payload=draw(_decoy_object()),
        status=status,
        label=f"status:{status}",
    )


# --------------------------------------------------------------------------------------------------
# driving the subjects without raising, and observing that nothing accumulates
# --------------------------------------------------------------------------------------------------


def _extract(payload: object) -> float | ReadFailure:
    """Call the pure extractor, turning any raised exception into an assertion failure.

    Property 10 forbids raising: an unusable payload must be *returned* as a classified failure, because an
    exception reaching the scheduler is flattened to a generic plugin failure (Requirement 3.7) and loses the
    response-format / invalid-measurement distinction this property draws.
    """
    try:
        outcome = extract_watts(payload, device_id=DEVICE_ID, power_path=REQUESTED_POWER_PATH)
    except Exception as exc:
        raise AssertionError(f"extract_watts raised for an unusable payload: {exc!r}") from exc
    return outcome


def _read_body(payload: object, *, status: int = HTTP_OK) -> tuple[ReadResult, _StubSession]:
    """Drive one ``read_power`` against a double serving ``payload`` as JSON with ``status``, never raising.

    Returns the outcome and the session, so the caller can assert exactly one request was issued. Any
    exception escaping ``read_power`` is converted to an assertion failure, since Property 10 requires a
    returned failure rather than a raised one.
    """

    async def _run() -> tuple[ReadResult, _StubSession]:
        session = _StubSession(body=json.dumps(payload).encode(), status=status)
        plugin = IdracPlugin(DEVICE_ID, _idrac_params(), session_factory=lambda: session)
        try:
            result = await plugin.read_power(REQUEST_TIMEOUT_S)
        finally:
            await plugin.aclose()
        return result, session

    try:
        outcome = asyncio.run(_run())
    except Exception as exc:
        raise AssertionError(f"read_power raised for an unusable response: {exc!r}") from exc
    return outcome


#: A cumulative total the demonstration accumulator is seeded with, so "unchanged" is a value distinct from
#: zero that a wrongly-returned reading could be seen to move off of.
SEED_TOTAL_KWH: Final = 12.5

#: A live baseline for that seed, so the accumulator holds a real baseline rather than an empty one.
BASELINE_WATTS: Final = 200.0
BASELINE_AT: Final = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)

#: The resolved poll interval the demonstration device is registered with, in seconds.
ACCUMULATOR_INTERVAL_S: Final = 300.0


def _total_unchanged_by(result: ReadResult) -> bool:
    """Whether feeding ``result`` to a live Energy_Accumulator leaves the device's total exactly where it was.

    The scheduler hands the Energy_Accumulator a Power_Reading and nothing else; a failed cycle passes it
    nothing (Requirement 5.6). Modelling that admission rule against the real accumulator -- seeded with a
    nonzero total and a live baseline -- makes "leaves the cumulative energy total unchanged" (Requirements
    4.11, 4.12) observable: a classified ReadFailure is not a Power_Reading, so it is never recorded.
    """
    accumulator = EnergyAccumulator()
    accumulator.register(DEVICE_ID, ACCUMULATOR_INTERVAL_S, restored=DeviceEnergyState(total_kwh=SEED_TOTAL_KWH))
    accumulator.record(PowerReading(device_id=DEVICE_ID, watts=BASELINE_WATTS, measured_at=BASELINE_AT))
    before = accumulator.total_kwh(DEVICE_ID)
    if isinstance(result, PowerReading):
        accumulator.record(result)
    return accumulator.total_kwh(DEVICE_ID) == before


def _assert_classified(
    result: float | ReadResult,
    *,
    expected: FailureClassification,
    names: DetailNames,
) -> None:
    """Assert the outcome is the expected classified failure, names the right field, and accumulates nothing."""
    # A classified failure was returned, never raised and never a reading (float from the extractor, or a
    # Power_Reading from the read): reaching this line at all is the "never raises" half of the property.
    assert isinstance(result, ReadFailure), f"expected a classified ReadFailure, got {result!r}"
    assert result.classification is expected, f"expected {expected}, got {result.classification}"
    needle = REQUESTED_POWER_PATH if names is DetailNames.PATH else IDRAC_HOST
    assert needle in result.detail, f"detail does not name {needle!r}: {result.detail!r}"
    assert _total_unchanged_by(result), "a classified failure must not move the cumulative energy total"


# --------------------------------------------------------------------------------------------------
# generator guards for the Property 10 families
# --------------------------------------------------------------------------------------------------


@settings(max_examples=150, deadline=None)
@given(case=absent_payloads())
def test_each_absent_payload_carries_no_power_value(case: AbsentCase) -> None:
    """The RESPONSE_FORMAT arm's invariant: no ``PowerConsumedWatts`` occurs anywhere in the payload.

    Checked with the same independent recursive walk Property 9 uses, so a generator that let a decoy reach
    the power key fails here for the generator's reasons rather than contaminating Property 10.
    """
    event(f"absent shape: {case.shape.value}")
    assert _power_values(case.payload) == []


@settings(max_examples=150, deadline=None)
@given(case=invalid_payloads())
def test_each_invalid_payload_carries_exactly_one_power_value(case: InvalidCase) -> None:
    """The INVALID_MEASUREMENT arm's invariant: exactly one power member, holding the generated value.

    A second occurrence would let the extractor read a different member than the one this case placed, so the
    located-then-rejected outcome could no longer be attributed to the generated value.
    """
    event(f"invalid placement: {case.placement.value}")
    event(f"invalid value type: {type(case.value).__name__}")
    values = _power_values(case.payload)
    assert len(values) == 1
    found = values[0]
    # ``is`` first, so a generated NaN -- which is never equal to itself -- still matches by identity.
    assert found is case.value or found == case.value


# --------------------------------------------------------------------------------------------------
# Property 10: unusable Redfish responses are classified, never raised, and never accumulate
# --------------------------------------------------------------------------------------------------


# Feature: remote-energy-meter, Property 10: Unusable Redfish responses are classified, never raised, and never accumulate  # noqa: E501 - fixed tagging comment exceeds line length
# Validates: Requirements 4.5, 4.11, 4.12
@settings(max_examples=150, deadline=None)
@given(case=unusable_responses())
def test_unusable_redfish_responses_are_classified(case: UnusableCase) -> None:
    """Requirements 4.5, 4.11, and 4.12, over every unusable shape.

    An absent power member is a response-format failure naming the requested path; a present but non-numeric
    or out-of-range value is an invalid-measurement failure naming that path; an unsuccessful status other
    than 401 and 403 is a communication failure naming the host. In every case a classified ReadFailure is
    *returned*, no exception is raised, and nothing reaches the Energy_Accumulator.
    """
    event(f"kind: {case.kind.value}")
    event(f"case: {case.label}")

    if case.kind is UnusableKind.HTTP_STATUS:
        # Only the HTTP layer sees a status, so this arm is the plugin's alone -- the pure extractor never runs.
        result, session = _read_body(case.payload, status=case.status)
        assert session.request_count == 1
        _assert_classified(result, expected=case.expected, names=case.names)
        return

    # Body arms: the pure extractor is the primary subject (Requirements 4.5, 4.11) ...
    _assert_classified(_extract(case.payload), expected=case.expected, names=case.names)

    # ... and the plugin surfaces the same classification through a real read, producing no reading and
    # issuing exactly one request.
    result, session = _read_body(case.payload, status=HTTP_OK)
    assert session.request_count == 1
    _assert_classified(result, expected=case.expected, names=case.names)


# --------------------------------------------------------------------------------------------------
# Property 11: what "agrees" is, and how the expectation is arrived at
# --------------------------------------------------------------------------------------------------
#
# Requirement 4.13 makes connection validation issue one request whose success requires the same payload
# shape as a successful read. So the two operations must never disagree about a response, and the way to
# show that is to put *one* response in front of both and check them against a third, independent reading
# of it: this module classifies the response it generated, then asserts that validation and the read each
# match that classification. Predicting one operation by calling the other would only demonstrate that the
# code agrees with itself, which is true of any implementation, including a wrong one.
#
# Six arms, spanning both sides of the biconditional. Three come from the body, reusing Property 9's and
# Property 10's generator families unchanged -- a usable payload, an absent power member, a present but
# unusable value. Three come from the transport, and each of those carries a body a read *would* have
# accepted, so a wrongly-lenient validation cannot pass by reading the payload and ignoring the status or
# the abandoned attempt.


class AgreementKind(StrEnum):
    """Which response an agreement case carries, and hence what both operations must conclude about it."""

    USABLE = "usable"  # -> a reading, and a successful validation
    ABSENT = "absent"  # -> RESPONSE_FORMAT (Requirement 4.5)
    INVALID = "invalid"  # -> INVALID_MEASUREMENT (Requirement 4.11)
    UNSUCCESSFUL = "unsuccessful"  # -> COMMUNICATION (Requirement 4.12)
    AUTH = "auth"  # -> AUTHENTICATION (Requirement 4.6)
    TIMEOUT = "timeout"  # -> TIMEOUT (Requirement 4.7)


class AgreementCase(NamedTuple):
    """One response both operations are driven against, carrying the independently assigned expectation.

    ``expected`` is ``None`` exactly when a read of this response must yield a Power_Reading, and otherwise
    the classification both operations must report. ``timed_out`` makes the transport fail before any
    response arrives, which is the one arm no status or body can express.
    """

    kind: AgreementKind
    expected: FailureClassification | None
    payload: object
    status: int
    timed_out: bool
    label: str


#: The transport error the abandoned-request arm raises. ``TimeoutError`` is what both aiohttp's own total
#: timeout and :func:`asyncio.timeout` raise, so the plugin sees the same exception a real expiry produces.
STUB_TIMEOUT_MESSAGE: Final = "stub transport did not answer"


def authentication_statuses() -> st.SearchStrategy[int]:
    """A status that means the credentials were refused (Requirement 4.6): HTTP 401 or HTTP 403.

    Drawn from the same :data:`_AUTH_STATUSES` set Property 10's communication arm excludes, so the two
    properties partition the unsuccessful statuses between them rather than each naming its own boundary.
    """
    return st.sampled_from(sorted(_AUTH_STATUSES))


@st.composite
def agreement_responses(draw: st.DrawFn) -> AgreementCase:
    """A Redfish response of any of the six kinds, tagged with what both operations must conclude."""
    kind = draw(st.sampled_from(AgreementKind))

    if kind is AgreementKind.USABLE:
        usable = draw(valid_power_payloads())
        return AgreementCase(
            kind=kind,
            expected=None,
            payload=usable.payload,
            status=HTTP_OK,
            timed_out=False,
            label=f"usable:{usable.placement.value}",
        )
    if kind is AgreementKind.ABSENT:
        absent = draw(absent_payloads())
        return AgreementCase(
            kind=kind,
            expected=FailureClassification.RESPONSE_FORMAT,
            payload=absent.payload,
            status=HTTP_OK,
            timed_out=False,
            label=f"absent:{absent.shape.value}",
        )
    if kind is AgreementKind.INVALID:
        invalid = draw(invalid_payloads())
        return AgreementCase(
            kind=kind,
            expected=FailureClassification.INVALID_MEASUREMENT,
            payload=invalid.payload,
            status=HTTP_OK,
            timed_out=False,
            label=f"invalid:{invalid.placement.value}:{type(invalid.value).__name__}",
        )

    # The transport arms deliberately carry a body a read would have accepted, so the response is unusable
    # only because of the status or the abandoned attempt -- and an operation that decided usability from the
    # payload alone would disagree with the other one here.
    readable_body = draw(valid_power_payloads()).payload
    if kind is AgreementKind.UNSUCCESSFUL:
        status = draw(unsuccessful_statuses())
        return AgreementCase(
            kind=kind,
            expected=FailureClassification.COMMUNICATION,
            payload=readable_body,
            status=status,
            timed_out=False,
            label=f"status:{status}",
        )
    if kind is AgreementKind.AUTH:
        status = draw(authentication_statuses())
        return AgreementCase(
            kind=kind,
            expected=FailureClassification.AUTHENTICATION,
            payload=readable_body,
            status=status,
            timed_out=False,
            label=f"auth:{status}",
        )
    return AgreementCase(
        kind=kind,
        expected=FailureClassification.TIMEOUT,
        payload=readable_body,
        status=HTTP_OK,
        timed_out=True,
        label="timeout",
    )


# --------------------------------------------------------------------------------------------------
# driving both operations against one response
# --------------------------------------------------------------------------------------------------


class AgreementRun(NamedTuple):
    """What both operations returned for one response, with the requests each of them issued."""

    validation: ValidationResult
    validation_requests: int
    read: ReadResult
    read_requests: int


def _validate_then_read(case: AgreementCase) -> AgreementRun:
    """Drive ``validate_connection`` and then ``read_power`` against one plugin and one stub response.

    A single plugin instance over a single session, in the order the addon uses them -- validation once at
    startup, then reads -- so "the same response" is literal rather than two doubles configured alike. The
    request counter is sampled between the calls, which is what makes "exactly one request" a statement about
    each operation separately. Any escaping exception becomes an assertion failure: neither operation may
    raise, since an exception reaching the registry or the scheduler loses the classification entirely
    (Requirements 3.7, 3.10).
    """

    async def _run() -> AgreementRun:
        error = TimeoutError(STUB_TIMEOUT_MESSAGE) if case.timed_out else None
        session = _StubSession(body=json.dumps(case.payload).encode(), status=case.status, error=error)
        plugin = IdracPlugin(DEVICE_ID, _idrac_params(), session_factory=lambda: session)
        try:
            validation = await plugin.validate_connection(REQUEST_TIMEOUT_S)
            after_validation = session.request_count
            read = await plugin.read_power(REQUEST_TIMEOUT_S)
        finally:
            await plugin.aclose()
        return AgreementRun(
            validation=validation,
            validation_requests=after_validation,
            read=read,
            read_requests=session.request_count - after_validation,
        )

    try:
        outcome = asyncio.run(_run())
    except Exception as exc:
        raise AssertionError(f"an operation raised for a generated response: {exc!r}") from exc
    return outcome


# --------------------------------------------------------------------------------------------------
# Property 11: connection validation agrees with reading
# --------------------------------------------------------------------------------------------------


# Feature: remote-energy-meter, Property 11: Connection validation agrees with reading
# Validates: Requirements 4.13
@settings(max_examples=150, deadline=None)
@given(case=agreement_responses())
def test_connection_validation_agrees_with_reading(case: AgreementCase) -> None:
    """Requirement 4.13, over usable, malformed, refused, and abandoned responses alike.

    Validation issues exactly one request, succeeds if and only if a read of the same response yields a
    Power_Reading, and otherwise reports the classification that read reports -- each checked against this
    module's own reading of the generated response rather than against the other operation's output.
    """
    event(f"kind: {case.kind.value}")
    event(f"case: {case.label}")

    run = _validate_then_read(case)

    # One request per operation: Requirement 4.13 for the validation, Requirement 4.3 for the read.
    assert run.validation_requests == 1, f"validation issued {run.validation_requests} requests"
    assert run.read_requests == 1, f"the read issued {run.read_requests} requests"

    # The biconditional itself, stated once and without reference to the expectation: validation succeeded
    # exactly when the read of the very same response produced a reading.
    produced_reading = isinstance(run.read, PowerReading)
    assert run.validation.ok is produced_reading, (
        f"validation ok={run.validation.ok} but the read returned {run.read!r}"
    )

    if case.expected is None:
        # A response this module built to be usable: the read reads it, and validation says so.
        assert produced_reading, f"expected a reading for {case.label}, got {run.read!r}"
        assert run.validation.ok
        assert run.validation.classification is None
        return

    # An unusable response: both operations must reach the classification assigned from its shape here.
    assert isinstance(run.read, ReadFailure), f"expected a classified failure for {case.label}"
    assert run.read.classification is case.expected, f"the read gave {run.read.classification}"
    assert not run.validation.ok
    assert run.validation.classification is case.expected, (
        f"validation gave {run.validation.classification}, the read gave {run.read.classification}"
    )


# --------------------------------------------------------------------------------------------------
# Property 12: what "its own entry's credentials" has to rule out
# --------------------------------------------------------------------------------------------------
#
# Requirement 4.3 makes two statements about one read: exactly one HTTPS request, and HTTP Basic
# authentication carrying the username and password *of that Device_Entry*. Neither is observable from a
# single plugin instance in isolation -- one instance holding one credential pair sends that pair no matter
# where it keeps it. So this property builds several instances whose credentials, hosts, and resource paths
# are all distinct, gathers reads across them so their requests are in flight together, and then asks of
# every request the doubles recorded: did it carry the credentials of the entry that issued it?
#
# That is what rules out the failures a single-instance test cannot see -- a credential cached on the class
# rather than the instance, a session or an auth object shared between instances, a login and a password
# crossed between two entries, a URL built from another entry's host. Every generated value is distinct
# across the whole set, and no username equals another entry's username *or* password, so a request bearing
# a foreign credential can never be mistaken for a correct one by coincidence.
#
# The response each entry is answered with is drawn from Property 11's family, which pins the request count
# on the failure paths too: a refused credential, an unsuccessful status, a malformed body, and an abandoned
# attempt each still have to be exactly one request. Each read's outcome is checked to carry its own device
# identifier as well, which is how a response crossed between two overlapping reads would show up.


#: How many Device_Entry values one case interleaves. Requirement 4.2 admits 1 to 32 entries for this
#: plugin; two is the fewest that can leak a credential from one entry to another, and a handful keeps the
#: gathered read count of a single example small.
MIN_ENTRIES: Final = 2
MAX_ENTRIES: Final = 4

#: The longest credential stem drawn. With the role-and-index prefix added below, a username stays well
#: inside Requirement 4.1's 64 characters and a password well inside its 128.
MAX_CREDENTIAL_STEM: Final = 24

#: Printable ASCII, which is what an iDRAC account actually accepts.
_PRINTABLE = st.characters(min_codepoint=32, max_codepoint=126)

#: The same, minus the one character HTTP Basic cannot express in a login (RFC 1945 section 11.1).
#: ``aiohttp.BasicAuth`` refuses such a login at construction, which the plugin reports as an authentication
#: failure *before* issuing any request; this property is about the request that does go out, so the
#: generated usernames stay inside what Basic can carry.
_PRINTABLE_NO_COLON = st.characters(min_codepoint=32, max_codepoint=126, exclude_characters=":")


class HostShape(StrEnum):
    """The authority forms a Device_Entry may configure, each of which yields one HTTPS URL."""

    IPV4 = "ipv4"
    HOSTNAME = "hostname"
    IPV4_PORT = "ipv4_port"


def _host_for(index: int, shape: HostShape) -> str:
    """A host for entry ``index``, in the given form. Distinct across entries because ``index`` is."""
    if shape is HostShape.IPV4:
        return f"192.0.2.{index + 10}"
    if shape is HostShape.HOSTNAME:
        return f"idrac-{index}.example"
    return f"192.0.2.{index + 10}:8443"


class CredentialEntry(NamedTuple):
    """One generated Device_Entry: its identity, its own transport parameters, and the response it gets."""

    device_id: str
    host: str
    username: str
    password: str
    power_path: str | None
    verify_tls: bool
    response: AgreementCase

    @property
    def expected_path(self) -> str:
        """The resource path a request for this entry must name, applying Requirement 4.10 when omitted."""
        return self.power_path if self.power_path is not None else REQUESTED_POWER_PATH

    @property
    def expected_url(self) -> str:
        """The absolute HTTPS URL a request for this entry must be issued to.

        Plain concatenation here rather than a call to the plugin's own ``power_url``: the generated hosts are
        bare authorities with no scheme, no trailing slash, and no IPv6 literal, so the URL is exactly this --
        and an oracle borrowed from the subject would move together with it.
        """
        return f"https://{self.host}{self.expected_path}"

    @property
    def params(self) -> IdracParams:
        """This entry's connection parameters, with the credentials wrapped as the schema declares them."""
        return IdracParams(
            host=self.host,
            username=SecretStr(self.username),
            password=SecretStr(self.password),
            verify_tls=self.verify_tls,
            power_path=self.power_path,
        )


class CredentialCase(NamedTuple):
    """A set of entries and the order their reads are issued in, as indices into ``entries``."""

    entries: tuple[CredentialEntry, ...]
    order: tuple[int, ...]


@st.composite
def _credential_entries(draw: st.DrawFn) -> list[CredentialEntry]:
    """Two to four Device_Entry values whose credentials, hosts, and resource paths are all distinct.

    Distinctness is structural rather than lucky: the role letter and the entry index are prefixed onto every
    credential, so no drawn value can coincide with another entry's username or password however the stems
    fall. That is what makes a crossed or shared credential *detectable* -- were two entries to share a
    password by chance, a request bearing the wrong one would still satisfy the assertion.
    """
    count = draw(st.integers(min_value=MIN_ENTRIES, max_value=MAX_ENTRIES))
    entries: list[CredentialEntry] = []
    for index in range(count):
        stem_u = draw(st.text(alphabet=_PRINTABLE_NO_COLON, max_size=MAX_CREDENTIAL_STEM))
        stem_p = draw(st.text(alphabet=_PRINTABLE, max_size=MAX_CREDENTIAL_STEM))
        entries.append(
            CredentialEntry(
                device_id=f"poweredge-{index}",
                host=_host_for(index, draw(st.sampled_from(HostShape))),
                username=f"u{index}-{stem_u}",
                password=f"p{index}-{stem_p}",
                # ``None`` on some entries on purpose: the default chassis path (Requirement 4.10) is
                # resolved per entry too, so an entry that omits the path must still be requested at its own
                # URL rather than at a neighbour's.
                power_path=draw(st.one_of(st.none(), st.just(f"/redfish/v1/Chassis/Sled.{index}/Power"))),
                # Drawn per entry as well, so a shared transport shows up in more than the credentials.
                verify_tls=draw(st.booleans()),
                response=draw(agreement_responses()),
            )
        )
    return entries


@st.composite
def credential_cases(draw: st.DrawFn) -> CredentialCase:
    """A set of distinct entries together with an interleaving of reads that touches every one of them.

    A permutation of all entries, plus up to as many extra reads again, permuted once more -- so an entry can
    be read twice, reads of two entries can be adjacent or separated, and every entry issues at least one
    request and is therefore actually observed.
    """
    entries = draw(_credential_entries())
    count = len(entries)
    base = draw(st.permutations(range(count)))
    extra = draw(st.lists(st.integers(min_value=0, max_value=count - 1), max_size=count))
    order = draw(st.permutations([*base, *extra]))
    return CredentialCase(entries=tuple(entries), order=tuple(order))


# --------------------------------------------------------------------------------------------------
# driving several instances at once
# --------------------------------------------------------------------------------------------------


class CredentialRun(NamedTuple):
    """What the interleaved reads returned, and what each entry's own session recorded."""

    results: tuple[ReadResult, ...]
    sessions: tuple[_StubSession, ...]


def _fixed_session(session: _StubSession) -> Callable[[], _StubSession]:
    """A factory that hands back one specific double, so each plugin instance owns its own session."""

    def _make() -> _StubSession:
        return session

    return _make


def _read_interleaved(case: CredentialCase) -> CredentialRun:
    """Gather one ``read_power`` per scheduled read, over one plugin instance and one session per entry.

    One instance per Device_Entry is how the registry builds them (Requirement 3.6), and all reads go out
    through :func:`asyncio.gather` in the drawn order. The doubles pause one event-loop tick between
    recording a request and answering it, so every request is on the wire before any response comes back --
    the state in which a credential or a session shared between instances would show itself. Any escaping
    exception becomes an assertion failure, since a read reports its outcome rather than raising it.
    """

    async def _run() -> CredentialRun:
        sessions = tuple(
            _StubSession(
                body=json.dumps(entry.response.payload).encode(),
                status=entry.response.status,
                error=TimeoutError(STUB_TIMEOUT_MESSAGE) if entry.response.timed_out else None,
                pause=True,
            )
            for entry in case.entries
        )
        plugins = [
            IdracPlugin(entry.device_id, entry.params, session_factory=_fixed_session(session))
            for entry, session in zip(case.entries, sessions, strict=True)
        ]
        try:
            results = await asyncio.gather(*(plugins[index].read_power(REQUEST_TIMEOUT_S) for index in case.order))
        finally:
            for plugin in plugins:
                await plugin.aclose()
        return CredentialRun(results=tuple(results), sessions=sessions)

    try:
        outcome = asyncio.run(_run())
    except Exception as exc:
        raise AssertionError(f"an interleaved read raised: {exc!r}") from exc
    return outcome


# --------------------------------------------------------------------------------------------------
# generator guard for the Property 12 family
# --------------------------------------------------------------------------------------------------


@settings(max_examples=150, deadline=None)
@given(case=credential_cases())
def test_each_entry_set_is_globally_distinct(case: CredentialCase) -> None:
    """The invariant Property 12 rests on: nothing that identifies an entry is shared with another entry.

    Machine-checked rather than argued for in prose. If two entries could draw the same password, a request
    bearing the wrong entry's password would satisfy Property 12's assertion and the leak would pass
    unnoticed; the same goes for two entries resolving to one URL.
    """
    event(f"entries: {len(case.entries)}")

    credentials = [value for entry in case.entries for value in (entry.username, entry.password)]
    # Usernames and passwords are pooled before the count, so a username can never equal *any* other
    # entry's password either -- which is the form a crossed credential would take.
    assert len(set(credentials)) == len(credentials)
    assert len({entry.device_id for entry in case.entries}) == len(case.entries)
    assert len({entry.host for entry in case.entries}) == len(case.entries)
    assert len({entry.expected_url for entry in case.entries}) == len(case.entries)

    # Every entry is read at least once, so no entry's requests go unobserved.
    assert set(case.order) == set(range(len(case.entries)))


# --------------------------------------------------------------------------------------------------
# Property 12: each read uses exactly one request bearing its own entry's credentials
# --------------------------------------------------------------------------------------------------


# Feature: remote-energy-meter, Property 12: Each read uses exactly one request bearing its own entry's credentials
# Validates: Requirements 4.3
@settings(max_examples=150, deadline=None)
@given(case=credential_cases())
def test_each_read_uses_one_request_with_its_own_credentials(case: CredentialCase) -> None:
    """Requirement 4.3, over several entries with distinct credentials whose reads overlap.

    Every read issues exactly one HTTPS request -- not zero, not two -- to its own entry's host and resource
    path, bearing its own entry's HTTP Basic credentials, on the successful path and on every failure path
    alike; and no request carries a value belonging to another entry.
    """
    event(f"entries: {len(case.entries)}")
    event(f"reads: {len(case.order)}")
    for entry in case.entries:
        event(f"response: {entry.response.kind.value}")

    run = _read_interleaved(case)

    for index, (entry, session) in enumerate(zip(case.entries, run.sessions, strict=True)):
        expected_requests = case.order.count(index)

        # Exactly one request per read, whatever the response was. ``allow_redirects`` being false is what
        # makes that literal rather than nearly so: a followed 3xx would put a second request on the wire
        # under a single read.
        assert session.request_count == expected_requests, (
            f"{entry.device_id} issued {session.request_count} requests for {expected_requests} reads"
        )
        assert len(session.calls) == expected_requests

        foreign = {
            value
            for position, other in enumerate(case.entries)
            if position != index
            for value in (other.username, other.password)
        }

        for call in session.calls:
            assert call.allow_redirects is False, "a followed redirect would be a second request"

            # Requirement 4.3 fixes the scheme, and the resource is this entry's own host and path.
            assert call.url.startswith("https://"), call.url
            assert call.url == entry.expected_url, f"{entry.device_id} requested {call.url!r}"

            # Reading the plaintext is legitimate exactly here: the property *is* that this entry's own
            # secret reached this entry's own request, and that cannot be stated about the redacted form.
            assert call.auth.login == entry.username, f"{entry.device_id} sent another login"
            assert call.auth.password == entry.password, f"{entry.device_id} sent another password"

            # ... and nothing belonging to a different entry travelled on this request, in either field.
            assert call.auth.login not in foreign, f"{entry.device_id} sent a foreign entry's credential"
            assert call.auth.password not in foreign, f"{entry.device_id} sent a foreign entry's credential"

            # The per-entry TLS mode rides along with the credentials, so a shared transport would show here
            # too rather than only in the authentication.
            assert call.ssl is entry.verify_tls

    # Each read's own outcome came back to it: a response crossed between two overlapping reads, or a
    # failure attributed to the wrong instance, fails here.
    for index, result in zip(case.order, run.results, strict=True):
        entry = case.entries[index]
        assert result.device_id == entry.device_id, f"{entry.device_id} received {result!r}"
        if entry.response.expected is None:
            assert isinstance(result, PowerReading), f"{entry.device_id} expected a reading, got {result!r}"
        else:
            assert isinstance(result, ReadFailure), f"{entry.device_id} expected a failure, got {result!r}"
            assert result.classification is entry.response.expected
