"""Persistence of cumulative energy totals to the addon data directory.

The ``Energy_Accumulator`` holds each device's cumulative kilowatt-hour total in memory. A restart therefore loses every
total unless something writes them down, and a total that resets to zero shows up in Home Assistant as a meter reset
that corrupts long-term statistics. This module is that something: it restores totals at startup (Requirement 7.4),
flushes them at least every 300 seconds (Requirement 7.9), and flushes them again on the way out under a bound the
caller sets (Requirement 7.3).

## Why the write is atomic

A power cut in the middle of a write leaves a truncated file, and a truncated file reads as a corrupt entry, which
Requirement 7.6 answers by resetting the affected device to 0 kWh. In other words, a naive write turns a crash during a
flush into permanent data loss. Every flush therefore writes a temporary file in the *same* directory, ``fsync``s it,
and renames it over the target with ``os.replace`` semantics (via :meth:`pathlib.Path.replace`), which POSIX and Windows
both define as atomic within a filesystem. A reader afterwards sees either the whole previous document or the whole new
one. The directory entry is ``fsync``ed too where the platform allows it, so the rename itself survives a power cut and
not just the file contents.

## Why a bad entry is per-device

Construction of :class:`~remote_energy_meter.persistence.DeviceEnergyState` validates its fields, so this module reads
each device entry inside its own ``try`` and converts a failure into a rejection for *that* device. One hand-edited or
half-written entry costs one device its total (Requirement 7.6); the other forty-nine restore untouched. The reason is
reported alongside the identifier so the log record says what was wrong rather than that something was.

A **foreign** ``schema_version`` is deliberately not corruption. Requirement 7.6 resets a device whose entry cannot be
read, and if a future format change made every entry unreadable that rule would silently zero every total in the
installation. So the version is checked before any entry is examined, and a mismatch is reported as
:attr:`RestoreFailure.FOREIGN_SCHEMA` — a stated, distinguishable reason that a migration can hook into later. Note that
the next flush replaces the foreign document with a current-schema one; the totals it carried are not translated,
because this release has no way to know what its fields meant.

## Why the persisted baseline is not restored as a baseline

The document stores each device's last measured watts and timestamp (Requirement 7.3), and this module hands them back
in :attr:`RestoreResult.devices`. They are kept for *diagnostics* — "this device last reported 214 W four days ago" is
the first thing worth knowing about a device that came back with a suspicious total.
:meth:`~remote_energy_meter.energy.EnergyAccumulator.register` drops the pair on purpose, because after a restart the
gap since that timestamp is unknown and integrating across it would invent energy that may never have been drawn
(Requirement 7.7). :attr:`RestoreResult.baselines` exposes the timestamps for the diagnostic log record without
tempting a caller to feed them back into the integration.

## Bounding the shutdown flush

Requirement 7.3 gives the SIGTERM flush 5 seconds, inside the 10 second shutdown budget of Requirement 1.5. A blocking
write to a filesystem the addon does not control cannot be interrupted, so :meth:`EnergyStateStore.flush_async` runs the
write on a worker thread and the caller's ``await`` is what gets bounded. :meth:`EnergyStateStore.flush_within` applies
:data:`SHUTDOWN_FLUSH_TIMEOUT_S` for a caller that wants the bound handed to it. Either way the deadline stops the
process *waiting*; if a stalled write later completes it completes atomically, so a late flush cannot corrupt the
document.

Nothing here sleeps to implement the 300 second cadence either: :meth:`EnergyStateStore.run_periodic` takes its clock
and its sleep function by injection, which is how the cadence is tested against a virtual clock instead of by waiting
five minutes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from time import monotonic as _default_monotonic
from typing import Any, Final

from remote_energy_meter.diagnostics.logger import DEVICE_ID_FIELD, get_logger
from remote_energy_meter.energy import KWH_DECIMAL_PLACES
from remote_energy_meter.persistence import CURRENT_SCHEMA_VERSION, DeviceEnergyState, EnergyStateFile

__all__ = [
    "DEFAULT_DATA_DIR",
    "PERIODIC_FLUSH_INTERVAL_S",
    "SHUTDOWN_FLUSH_TIMEOUT_S",
    "STATE_FILE_NAME",
    "DeviceRejection",
    "EnergyStateStore",
    "FlushOutcome",
    "FlushResult",
    "RestoreFailure",
    "RestoreRejection",
    "RestoreResult",
    "build_state_file",
    "parse_state",
    "serialize_state",
]

#: Where Supervisor mounts the addon's persistent volume inside the container.
DEFAULT_DATA_DIR: Final = Path("/data")

#: The document this module reads and writes, inside the data directory.
STATE_FILE_NAME: Final = "energy_state.json"

#: Requirement 7.9: the longest gap between flushes, so an ungraceful termination loses at most this much accumulated
#: energy per device.
PERIODIC_FLUSH_INTERVAL_S: Final = 300.0

#: Requirement 7.3: the bound on the SIGTERM flush, inside the 10 second shutdown budget of Requirement 1.5.
SHUTDOWN_FLUSH_TIMEOUT_S: Final = 5.0

#: Largest state document this module will parse. Fifty devices of a few floats each is well under a kilobyte; a file
#: orders of magnitude larger is a mistake or a hostile mount, and parsing it would cost memory inside the 30 second
#: startup budget of Requirement 1.3.
MAX_DOCUMENT_BYTES: Final = 1 << 20

_TEMP_SUFFIX: Final = ".tmp"


# --------------------------------------------------------------------------------------------------
# outcomes
# --------------------------------------------------------------------------------------------------


class RestoreFailure(StrEnum):
    """Why no persisted state could be read at all.

    Document-level, as against :class:`RestoreRejection`, which is per device. Every member leaves every device starting
    at 0 kWh (Requirement 7.6) — but for distinguishable, stated reasons, because the operator response differs: a
    missing file on first start is expected, an unreadable one is a mount or permission problem, and a foreign schema
    version means a downgrade.
    """

    FILE_MISSING = "file_missing"
    """No document exists yet. The normal state of a first start, reported at ``info`` rather than as a problem."""

    FILE_UNREADABLE = "file_unreadable"
    """The document exists but could not be read: permissions, an I/O error, or oversize."""

    FILE_MALFORMED = "file_malformed"
    """The document is not UTF-8, is not JSON, is not an object, or declares no integer ``schema_version``."""

    FOREIGN_SCHEMA = "foreign_schema"
    """The document declares a ``schema_version`` this release does not read. Detected before any entry is examined, so
    a format change is never mistaken for corruption (Requirement 7.6)."""

    @property
    def is_first_start(self) -> bool:
        """Whether this outcome is the expected one for an installation that has never flushed."""
        return self is RestoreFailure.FILE_MISSING


class RestoreRejection(StrEnum):
    """Why one device's persisted entry could not be used (Requirement 7.6).

    The set separates the shapes an operator can act on. All but
    :attr:`RestoreRejection.BASELINE_UNUSABLE` reset that device's total to 0 kWh.
    """

    ENTRY_MALFORMED = "entry_malformed"
    """The entry is not an object, so it carries no fields to read."""

    TOTAL_MISSING = "total_missing"
    """The entry carries no ``total_kwh`` member."""

    TOTAL_NOT_A_NUMBER = "total_not_a_number"
    """``total_kwh`` is present but is text, a boolean, or a structure rather than a number."""

    TOTAL_OUT_OF_RANGE = "total_out_of_range"
    """``total_kwh`` is a number that cannot be a cumulative total: negative, or not finite."""

    BASELINE_UNUSABLE = "baseline_unusable"
    """The total is usable but the baseline pair is not — half present, a naive timestamp, an unparseable timestamp, or
    an unusable watt value. The total is **kept** and only the baseline is dropped: the baseline is retained for
    diagnostics and never restored as an active baseline anyway (Requirement 7.7), so discarding a good total over a bad
    diagnostic field would lose real energy for no gain."""

    @property
    def resets_total(self) -> bool:
        """Whether this rejection starts the affected device at 0 kWh."""
        return self is not RestoreRejection.BASELINE_UNUSABLE


@dataclass(frozen=True, slots=True)
class DeviceRejection:
    """One device's persisted entry that could not be used, and why (Requirement 7.6).

    Attributes:
        device_id: The identifier the entry was keyed by.
        rejection: Which shape of problem was found.
        detail: A sentence naming the offending field and what was wrong with it, for the log record.
    """

    device_id: str
    rejection: RestoreRejection
    detail: str

    @property
    def resets_total(self) -> bool:
        """Whether the affected device starts at 0 kWh."""
        return self.rejection.resets_total

    def describe(self) -> str:
        """One line naming the device, the reason, and the detail."""
        return f"device {self.device_id!r}: {self.rejection.value}: {self.detail}"


@dataclass(frozen=True, slots=True)
class RestoreResult:
    """What a restore produced.

    Attributes:
        devices: Per-device state to hand to
            :meth:`~remote_energy_meter.energy.EnergyAccumulator.register_all`. A device whose entry was rejected with a
            total-resetting reason is present with :meth:`DeviceEnergyState.zeroed`, so a caller can tell "was persisted
            and rejected" from "was never persisted" while both start at 0 kWh.
        updated_at: When the document was written, or ``None`` when it carried no usable timestamp. Diagnostics only.
        schema_version: The version the document declared, or ``None`` when none could be read.
        failure: The document-level failure, or ``None`` when the document was read.
        detail: A sentence about ``failure``, or ``None``.
        rejections: One entry per rejected device, in the order the entries were read.
    """

    devices: Mapping[str, DeviceEnergyState] = field(default_factory=dict)
    updated_at: datetime | None = None
    schema_version: int | None = None
    failure: RestoreFailure | None = None
    detail: str | None = None
    rejections: tuple[DeviceRejection, ...] = ()

    @property
    def restored(self) -> bool:
        """Whether a document was read. False does not imply an error: a first start has nothing to read."""
        return self.failure is None

    @property
    def totals(self) -> dict[str, float]:
        """The restored cumulative total of each device, in kilowatt-hours."""
        return {device_id: state.total_kwh for device_id, state in self.devices.items()}

    @property
    def baselines(self) -> dict[str, datetime]:
        """The persisted baseline timestamp of each device that had one, for diagnostics.

        Not an accumulation baseline: the accumulator re-baselines on its first reading after start (Requirement 7.7).
        """
        return {
            device_id: state.baseline_at for device_id, state in self.devices.items() if state.baseline_at is not None
        }

    def baseline_at(self, device_id: str) -> datetime | None:
        """The persisted baseline timestamp of one device, or ``None`` when it had none."""
        state = self.devices.get(device_id)
        return None if state is None else state.baseline_at


class FlushOutcome(StrEnum):
    """What one flush did."""

    WRITTEN = "written"
    """The document was replaced atomically."""

    FAILED = "failed"
    """The write did not complete. The previous document, if any, is untouched."""

    TIMED_OUT = "timed_out"
    """The caller's bound expired before the write reported back (Requirement 7.3). Whether the write later completes is
    unknown to the caller, but it can only complete atomically."""


@dataclass(frozen=True, slots=True)
class FlushResult:
    """What one flush did, and to what.

    Attributes:
        outcome: Which of the three endings applied.
        path: The document written, or that would have been written.
        device_count: How many devices the flushed snapshot carried.
        detail: A sentence about a non-successful outcome, or ``None``.
    """

    outcome: FlushOutcome
    path: Path
    device_count: int = 0
    detail: str | None = None

    @property
    def written(self) -> bool:
        """Whether the document was replaced."""
        return self.outcome is FlushOutcome.WRITTEN


# --------------------------------------------------------------------------------------------------
# serialization (pure)
# --------------------------------------------------------------------------------------------------


def build_state_file(snapshot: Mapping[str, DeviceEnergyState], updated_at: datetime) -> EnergyStateFile:
    """Wrap an accumulator snapshot in a document at the current schema version."""
    return EnergyStateFile(schema_version=CURRENT_SCHEMA_VERSION, updated_at=updated_at, devices=dict(snapshot))


def serialize_state(state: EnergyStateFile) -> str:
    """Render a document as the JSON text that goes on disk.

    Device keys are sorted so two flushes of the same state produce byte-identical text, which makes a spurious write
    visible in a diff. Every field is emitted explicitly, an absent baseline as ``null``, so the shape does not change
    with the data. ``allow_nan`` is off: a non-finite total would otherwise be written as the ``NaN`` literal, which is
    not JSON and which the next restore would have to reject.
    """
    payload: dict[str, Any] = {
        "schema_version": state.schema_version,
        "updated_at": _render_timestamp(state.updated_at),
        "devices": {device_id: _render_device(state.devices[device_id]) for device_id in sorted(state.devices)},
    }
    return json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n"


def parse_state(text: str) -> RestoreResult:
    """Read a state document from text, classifying whatever cannot be read.

    Pure and total: never raises, never touches the filesystem, and reports every problem as a
    :class:`RestoreFailure` at the document level or a :class:`DeviceRejection` per device. The order of checks is
    load-bearing — the schema version is settled before any device entry is examined, so a future format is reported as
    :attr:`RestoreFailure.FOREIGN_SCHEMA` rather than as fifty corrupt entries (Requirement 7.6).
    """
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        return RestoreResult(
            failure=RestoreFailure.FILE_MALFORMED,
            detail=f"the state document is not valid JSON (line {error.lineno}, column {error.colno})",
        )

    if not isinstance(document, Mapping):
        return RestoreResult(
            failure=RestoreFailure.FILE_MALFORMED,
            detail=f"the state document holds a {type(document).__name__}, not an object",
        )

    raw_version = document.get("schema_version")
    if isinstance(raw_version, bool) or not isinstance(raw_version, int):
        return RestoreResult(
            failure=RestoreFailure.FILE_MALFORMED,
            detail=(f"the state document declares no integer schema_version (found {type(raw_version).__name__})"),
        )
    if raw_version != CURRENT_SCHEMA_VERSION:
        return RestoreResult(
            schema_version=raw_version,
            failure=RestoreFailure.FOREIGN_SCHEMA,
            detail=(
                f"the state document declares schema_version {raw_version}; "
                f"this release reads version {CURRENT_SCHEMA_VERSION}"
            ),
        )

    raw_devices = document.get("devices", {})
    if not isinstance(raw_devices, Mapping):
        return RestoreResult(
            schema_version=raw_version,
            failure=RestoreFailure.FILE_MALFORMED,
            detail=f"the devices member holds a {type(raw_devices).__name__}, not an object",
        )

    devices: dict[str, DeviceEnergyState] = {}
    rejections: list[DeviceRejection] = []
    for raw_key, raw_entry in raw_devices.items():
        device_id = str(raw_key)
        state, rejection = _restore_device(device_id, raw_entry)
        devices[device_id] = state
        if rejection is not None:
            rejections.append(rejection)

    return RestoreResult(
        devices=devices,
        # A document-level timestamp is diagnostics only, so an unusable one is dropped rather than allowed to discard
        # every total in the file.
        updated_at=_parse_timestamp_or_none(document.get("updated_at")),
        schema_version=raw_version,
        rejections=tuple(rejections),
    )


# --------------------------------------------------------------------------------------------------
# the store
# --------------------------------------------------------------------------------------------------


class EnergyStateStore:
    """Reads and writes the persisted energy state document.

    One instance serves the whole process::

        store = EnergyStateStore()                      # /data/energy_state.json
        restored = store.restore()                      # Requirements 7.4, 7.6
        accumulator.register_all(intervals, restored.devices)
        ...
        await store.run_periodic(accumulator.snapshot)   # Requirement 7.9
        ...
        await store.flush_within(accumulator.snapshot()) # Requirement 7.3

    The path, the wall clock, the monotonic clock, and the sleep function are all injectable, so the tests exercise a
    temporary directory and a virtual clock rather than ``/data`` and five real minutes.

    Neither :meth:`restore` nor :meth:`flush` raises. Persistence is a best-effort side channel: the in-memory total
    stays authoritative, and a failed flush is a logged warning that the next flush retries (Requirement 7.9), not a
    reason to interrupt metering.
    """

    __slots__ = ("_clock", "_flush_interval_s", "_last_flush_at", "_log", "_monotonic", "_path")

    def __init__(
        self,
        path: Path | None = None,
        *,
        data_dir: Path = DEFAULT_DATA_DIR,
        flush_interval_s: float = PERIODIC_FLUSH_INTERVAL_S,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        """
        Args:
            path: The document to read and write. Defaults to :data:`STATE_FILE_NAME` inside ``data_dir``.
            data_dir: Where the document lives when ``path`` is not given. Defaults to :data:`DEFAULT_DATA_DIR`, which
                is the persistent volume Supervisor mounts.
            flush_interval_s: The periodic cadence. Must be positive and at most
                :data:`PERIODIC_FLUSH_INTERVAL_S`, because a longer cadence would lose more than the 300 seconds
                Requirement 7.9 allows.
            clock: Source of the ``updated_at`` timestamp. Must return a timezone-aware value; defaults to the UTC wall
                clock.
            monotonic: Source of the cadence measurement. Defaults to :func:`time.monotonic`, which does not jump when
                the host's wall clock is corrected.
            logger: Where records go. Defaults to this module's logger.
        """
        if flush_interval_s <= 0 or flush_interval_s > PERIODIC_FLUSH_INTERVAL_S:
            raise ValueError(
                f"flush interval must be positive and at most {PERIODIC_FLUSH_INTERVAL_S:g} seconds "
                f"to stay inside the Requirement 7.9 bound: {flush_interval_s!r}"
            )
        self._path = path if path is not None else data_dir / STATE_FILE_NAME
        self._flush_interval_s = float(flush_interval_s)
        self._clock: Callable[[], datetime] = clock if clock is not None else _utc_now
        self._monotonic: Callable[[], float] = monotonic if monotonic is not None else _default_monotonic
        self._log = logger if logger is not None else get_logger(__name__)
        self._last_flush_at: float | None = None

    @property
    def path(self) -> Path:
        """The document this store reads and writes."""
        return self._path

    @property
    def flush_interval_s(self) -> float:
        """The periodic flush cadence, in seconds (Requirement 7.9)."""
        return self._flush_interval_s

    # -- restore (Requirements 7.4, 7.6) -----------------------------------------------------------

    def restore(self) -> RestoreResult:
        """Read the persisted state, logging what could not be used.

        Never raises. A missing document is the expected first-start case and is reported at ``info``; every other
        document-level problem and every per-device rejection is reported at ``warning`` naming the reason, and the
        affected devices start at 0 kWh (Requirement 7.6).
        """
        result = self._read()
        self._log_restore(result)
        return result

    def _read(self) -> RestoreResult:
        """Read and parse the document, classifying I/O failures."""
        try:
            size = self._path.stat().st_size
        except FileNotFoundError:
            return RestoreResult(
                failure=RestoreFailure.FILE_MISSING,
                detail=f"no persisted energy state exists at {self._path}",
            )
        except OSError as error:
            return RestoreResult(
                failure=RestoreFailure.FILE_UNREADABLE,
                detail=f"the state document at {self._path} could not be inspected: {type(error).__name__}",
            )

        if size > MAX_DOCUMENT_BYTES:
            return RestoreResult(
                failure=RestoreFailure.FILE_UNREADABLE,
                detail=f"the state document at {self._path} is larger than {MAX_DOCUMENT_BYTES} bytes",
            )

        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return RestoreResult(
                failure=RestoreFailure.FILE_MISSING,
                detail=f"no persisted energy state exists at {self._path}",
            )
        except UnicodeDecodeError:
            return RestoreResult(
                failure=RestoreFailure.FILE_MALFORMED,
                detail=f"the state document at {self._path} is not valid UTF-8",
            )
        except OSError as error:
            return RestoreResult(
                failure=RestoreFailure.FILE_UNREADABLE,
                detail=f"the state document at {self._path} could not be read: {type(error).__name__}",
            )

        return parse_state(text)

    def _log_restore(self, result: RestoreResult) -> None:
        """Emit one record for the document outcome and one per rejected device (Requirement 7.6)."""
        if result.failure is RestoreFailure.FILE_MISSING:
            self._log.info("no persisted energy state found; every device starts at 0 kWh: %s", result.detail)
        elif result.failure is not None:
            self._log.warning(
                "persisted energy state unusable; every device starts at 0 kWh: %s",
                result.detail,
                extra={"reason": result.failure.value},
            )
        else:
            self._log.info(
                "restored persisted energy totals for %d device(s)",
                len(result.devices),
                extra={"reason": "restored"},
            )

        for rejection in result.rejections:
            self._log.warning(
                "persisted energy state rejected for this device: %s",
                rejection.detail,
                extra={
                    DEVICE_ID_FIELD: rejection.device_id,
                    "reason": rejection.rejection.value,
                    "total_reset": rejection.resets_total,
                },
            )

    # -- flush (Requirements 7.3, 7.9) -------------------------------------------------------------

    def flush(self, snapshot: Mapping[str, DeviceEnergyState]) -> FlushResult:
        """Write a snapshot to the document, atomically.

        Blocking, and safe to call from a worker thread; :meth:`flush_async` is the form to use from the event loop.
        Never raises: a failure is logged and returned as :attr:`FlushOutcome.FAILED`, leaving any previous document
        untouched and the in-memory totals authoritative.

        ``snapshot`` should be :meth:`~remote_energy_meter.energy.EnergyAccumulator.snapshot`, which returns an
        independent mapping of immutable states — so a device recorded while this write is in progress lands in the next
        flush rather than half in this one.
        """
        device_count = len(snapshot)
        try:
            text = serialize_state(build_state_file(snapshot, self._clock()))
            _write_atomically(self._path, text)
        except Exception as error:
            detail = f"{type(error).__name__}: {error}"
            self._log.warning(
                "could not persist energy state to %s (%s); the in-memory totals are unaffected",
                self._path,
                detail,
                extra={"reason": FlushOutcome.FAILED.value},
            )
            return FlushResult(
                outcome=FlushOutcome.FAILED,
                path=self._path,
                device_count=device_count,
                detail=detail,
            )

        self._last_flush_at = self._monotonic()
        self._log.debug(
            "persisted energy state for %d device(s) to %s",
            device_count,
            self._path,
            extra={"reason": FlushOutcome.WRITTEN.value},
        )
        return FlushResult(outcome=FlushOutcome.WRITTEN, path=self._path, device_count=device_count)

    async def flush_async(self, snapshot: Mapping[str, DeviceEnergyState]) -> FlushResult:
        """Flush from the event loop, with the write on a worker thread.

        The write itself cannot be interrupted, so this is the form a caller can bound: cancelling the ``await`` returns
        control immediately, and a write that completes afterwards still completes atomically.
        """
        return await asyncio.to_thread(self.flush, snapshot)

    async def flush_within(
        self,
        snapshot: Mapping[str, DeviceEnergyState],
        timeout_s: float = SHUTDOWN_FLUSH_TIMEOUT_S,
        *,
        shielded: bool = True,
    ) -> FlushResult:
        """Flush under a bound, for the shutdown path (Requirement 7.3).

        Returns :attr:`FlushOutcome.TIMED_OUT` instead of raising when the bound expires, so the shutdown sequence can
        carry on to the remaining steps inside its own 10 second budget (Requirement 1.5).

        ``shielded`` keeps the worker thread's result from being discarded by the cancellation, which is why a slow
        filesystem produces a *late* flush rather than no flush at all. Pass ``shielded=False`` to have the pending
        write abandoned outright.
        """
        pending = asyncio.ensure_future(self.flush_async(snapshot))
        awaited = asyncio.shield(pending) if shielded else pending
        try:
            async with asyncio.timeout(timeout_s):
                return await awaited
        except TimeoutError:
            detail = f"the flush did not complete within {timeout_s:g} seconds"
            self._log.warning(
                "persisting energy state to %s exceeded its %g second bound",
                self._path,
                timeout_s,
                extra={"reason": FlushOutcome.TIMED_OUT.value},
            )
            if not shielded:
                pending.cancel()
            return FlushResult(
                outcome=FlushOutcome.TIMED_OUT,
                path=self._path,
                device_count=len(snapshot),
                detail=detail,
            )

    # -- cadence (Requirement 7.9) -----------------------------------------------------------------

    def seconds_until_due(self) -> float:
        """How long until the next flush is due, in seconds. Zero when one is due now or overdue."""
        if self._last_flush_at is None:
            return 0.0
        elapsed = self._monotonic() - self._last_flush_at
        return max(0.0, self._flush_interval_s - elapsed)

    @property
    def is_due(self) -> bool:
        """Whether the cadence has elapsed since the last *successful* flush.

        A failed flush leaves this true, so a caller driving its own cadence retries at the next opportunity rather than
        waiting a further full interval on top of the one it already lost.
        """
        return self.seconds_until_due() <= 0.0

    async def flush_if_due(self, snapshot_source: Callable[[], Mapping[str, DeviceEnergyState]]) -> FlushResult | None:
        """Flush only when the cadence has elapsed, returning ``None`` when it has not.

        The snapshot is taken inside the check, so a caller polling this frequently does not pay for a snapshot it will
        not write.
        """
        if not self.is_due:
            return None
        return await self.flush_async(snapshot_source())

    async def run_periodic(
        self,
        snapshot_source: Callable[[], Mapping[str, DeviceEnergyState]],
        *,
        sleep: Callable[[float], Any] | None = None,
    ) -> None:
        """Flush every :attr:`flush_interval_s` seconds until cancelled (Requirement 7.9).

        Sleeps *first*, because nothing has accumulated at start and the restored document is already current. The
        snapshot is taken on the event loop and the write runs on a worker thread, so a slow filesystem delays the next
        flush rather than the poll cycles.

        ``sleep`` defaults to :func:`asyncio.sleep` and is injectable so the cadence can be verified against a virtual
        clock. Cancellation propagates: the caller's shutdown sequence cancels this task and then performs the final
        bounded flush itself (Requirement 7.3), so this loop deliberately does not flush on its way out — that would put
        an unbounded write inside the shutdown budget.
        """
        sleeper: Callable[[float], Any] = sleep if sleep is not None else asyncio.sleep
        while True:
            await sleeper(self._flush_interval_s)
            await self.flush_async(snapshot_source())


# --------------------------------------------------------------------------------------------------
# internals
# --------------------------------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _render_timestamp(moment: datetime) -> str:
    """Render a timezone-aware timestamp as UTC with a ``Z`` suffix, preserving microseconds."""
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _render_device(state: DeviceEnergyState) -> dict[str, Any]:
    """Render one device's state, every field explicit so the shape does not vary with the data."""
    return {
        "total_kwh": round(state.total_kwh, KWH_DECIMAL_PLACES),
        "baseline_watts": None if state.baseline_watts is None else float(state.baseline_watts),
        "baseline_at": None if state.baseline_at is None else _render_timestamp(state.baseline_at),
    }


def _parse_timestamp(raw: object) -> datetime:
    """Parse a rendered timestamp back to a timezone-aware value.

    Raises :class:`TypeError` when the value is not text at all and :class:`ValueError` when it is text that does not
    render an aware instant, matching how :class:`DeviceEnergyState` separates the two.
    """
    if not isinstance(raw, str):
        raise TypeError(f"timestamp is a {type(raw).__name__}, not text")
    moment = datetime.fromisoformat(raw)
    if moment.tzinfo is None:
        raise ValueError("timestamp carries no timezone offset")
    return moment


def _parse_timestamp_or_none(raw: object) -> datetime | None:
    """Parse a timestamp for a diagnostics-only field, yielding ``None`` when it cannot be read."""
    if raw is None:
        return None
    try:
        return _parse_timestamp(raw)
    except (TypeError, ValueError):
        return None


def _coerce_total(raw: object) -> float:
    """Return ``raw`` as a total, raising :class:`TypeError` when it is not a number at all."""
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise TypeError(f"total_kwh is a {type(raw).__name__}, not a number")
    return float(raw)


def _restore_device(device_id: str, raw: object) -> tuple[DeviceEnergyState, DeviceRejection | None]:
    """Read one device's entry, falling back to 0 kWh with a stated reason (Requirement 7.6).

    Returns the state to register and the rejection to report, if any. A bad *baseline* keeps the total and drops only
    the baseline, because the baseline is diagnostics and the total is real energy.
    """
    if not isinstance(raw, Mapping):
        return _rejected(
            device_id,
            RestoreRejection.ENTRY_MALFORMED,
            f"the entry is a {type(raw).__name__}, not an object",
        )

    if "total_kwh" not in raw:
        return _rejected(device_id, RestoreRejection.TOTAL_MISSING, "the entry carries no total_kwh member")

    try:
        total_kwh = _coerce_total(raw["total_kwh"])
    except TypeError as error:
        return _rejected(device_id, RestoreRejection.TOTAL_NOT_A_NUMBER, str(error))

    try:
        # DeviceEnergyState validates the range, so the accepted set is defined in exactly one place.
        total_only = DeviceEnergyState(total_kwh=total_kwh)
    except ValueError as error:
        return _rejected(device_id, RestoreRejection.TOTAL_OUT_OF_RANGE, str(error))

    raw_watts = raw.get("baseline_watts")
    raw_at = raw.get("baseline_at")
    if raw_watts is None and raw_at is None:
        return total_only, None
    if raw_watts is None or raw_at is None:
        return _baseline_dropped(
            device_id,
            total_only,
            "baseline_watts and baseline_at must both be present or both be absent",
        )

    try:
        state = DeviceEnergyState(
            total_kwh=total_kwh,
            baseline_watts=_coerce_total(raw_watts),
            baseline_at=_parse_timestamp(raw_at),
        )
    except (TypeError, ValueError) as error:
        return _baseline_dropped(device_id, total_only, str(error))
    return state, None


def _baseline_dropped(
    device_id: str,
    total_only: DeviceEnergyState,
    reason: str,
) -> tuple[DeviceEnergyState, DeviceRejection]:
    """A device whose total survived but whose baseline did not (Requirement 7.7)."""
    return total_only, DeviceRejection(
        device_id=device_id,
        rejection=RestoreRejection.BASELINE_UNUSABLE,
        detail=f"the baseline was dropped and the total kept: {reason}",
    )


def _rejected(
    device_id: str,
    rejection: RestoreRejection,
    detail: str,
) -> tuple[DeviceEnergyState, DeviceRejection]:
    """A device that starts at 0 kWh, with the reason it does (Requirement 7.6)."""
    return DeviceEnergyState.zeroed(), DeviceRejection(device_id=device_id, rejection=rejection, detail=detail)


def _write_atomically(path: Path, text: str) -> None:
    """Replace ``path`` with ``text``, atomically.

    Temp file in the same directory (so the rename cannot cross a filesystem), written, flushed, ``fsync``ed, then
    renamed over the target with ``os.replace`` semantics. A crash at any point leaves either the old document or the
    new one, never a truncated one. The temp file is removed on any failure, so a failing mount does not accumulate
    debris next to the document.

    The directory entry is ``fsync``ed afterwards where the platform supports it, so the rename itself is durable and
    not merely the file contents. Filesystems that reject a directory ``fsync`` — some network mounts among them — are
    tolerated: the contents are already durable and the rename is still atomic.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = text.encode("utf-8")

    descriptor, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=_TEMP_SUFFIX)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # Path.replace is os.replace: the atomic, same-filesystem rename this whole function exists for.
        temp_path.replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            temp_path.unlink(missing_ok=True)
        raise

    _fsync_directory(path.parent)


def _fsync_directory(directory: Path) -> None:
    """Flush a directory entry so a rename survives a power cut. Best effort by design."""
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        # Directory fsync is unsupported on some filesystems and network mounts. The document contents are already
        # durable and the rename is atomic regardless, so this is a durability nicety, not a correctness requirement.
        pass
    finally:
        os.close(descriptor)
