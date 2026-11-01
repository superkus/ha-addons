"""Unit tests for pure interval and timeout resolution (task 11.1).

Covers both boundaries of the accepted interval range, the fallback to the global interval with the rejected value
reported, and the capped one-third timeout fallback. The same rules quantified over the whole input space are
Properties 14 and 15 (tasks 11.5, 11.6).
"""

from __future__ import annotations

from typing import Any, Final

import pytest

from remote_energy_meter.config.models import (
    DEFAULT_POLL_INTERVAL_S,
    MAX_POLL_INTERVAL_S,
    MIN_POLL_INTERVAL_S,
    DeviceEntry,
)
from remote_energy_meter.plugins.idrac.schema import IdracParams
from remote_energy_meter.scheduler import MAX_FALLBACK_TIMEOUT_S, resolve_interval, resolve_timeout

#: A minimal valid iDRAC parameter mapping: the three required fields and nothing else, so every optional field
#: (``request_timeout_s`` in particular) is absent from ``model_fields_set``.
_IDRAC_PARAMS: Final[dict[str, Any]] = {"host": "192.0.2.9", "username": "root", "password": "s3cr3t"}

# --------------------------------------------------------------------------------------------------
# interval resolution (Requirements 5.2, 5.7)
# --------------------------------------------------------------------------------------------------


def test_omitted_device_interval_uses_the_global_interval() -> None:
    """Requirement 5.2: only a device-specific interval displaces the global one."""
    resolution = resolve_interval(None, DEFAULT_POLL_INTERVAL_S)

    assert resolution.interval_s == DEFAULT_POLL_INTERVAL_S
    assert resolution.rejected_value is None
    assert resolution.rejected is False


@pytest.mark.parametrize("declared", [MIN_POLL_INTERVAL_S, 60, MAX_POLL_INTERVAL_S])
def test_device_interval_within_range_wins_including_both_boundaries(declared: int) -> None:
    """Requirement 5.2: the range is 5-3600 seconds *inclusive*."""
    resolution = resolve_interval(declared, DEFAULT_POLL_INTERVAL_S)

    assert resolution.interval_s == declared
    assert resolution.rejected is False


@pytest.mark.parametrize("declared", [MIN_POLL_INTERVAL_S - 1, MAX_POLL_INTERVAL_S + 1, 0, -30, 86400])
def test_out_of_range_device_interval_falls_back_and_reports_the_rejected_value(declared: int) -> None:
    """Requirement 5.7: the global interval is used and the refused value comes back to be logged."""
    resolution = resolve_interval(declared, DEFAULT_POLL_INTERVAL_S)

    assert resolution.interval_s == DEFAULT_POLL_INTERVAL_S
    assert resolution.rejected_value == declared
    assert resolution.rejected is True


def test_a_valid_device_interval_equal_to_the_global_one_is_not_reported_as_rejected() -> None:
    """A value that happens to match the global interval was still *used*, so nothing is owed a log record."""
    resolution = resolve_interval(DEFAULT_POLL_INTERVAL_S, DEFAULT_POLL_INTERVAL_S)

    assert resolution.rejected is False


def test_resolution_is_pure_and_carries_no_state() -> None:
    """Two calls with the same arguments agree, and the result cannot be mutated by a caller."""
    first = resolve_interval(600, DEFAULT_POLL_INTERVAL_S)
    second = resolve_interval(600, DEFAULT_POLL_INTERVAL_S)

    assert first == second
    with pytest.raises(AttributeError):
        first.interval_s = 5  # type: ignore[misc]


# --------------------------------------------------------------------------------------------------
# timeout resolution (Requirement 5.8)
# --------------------------------------------------------------------------------------------------


def test_declared_timeout_is_used_unchanged() -> None:
    assert resolve_timeout(2.5, 30) == 2.5


@pytest.mark.parametrize(
    ("interval_s", "expected"),
    [
        (MIN_POLL_INTERVAL_S, 5 / 3),
        (15, 5.0),
        (30, 10.0),
        (31, MAX_FALLBACK_TIMEOUT_S),
        (MAX_POLL_INTERVAL_S, MAX_FALLBACK_TIMEOUT_S),
    ],
)
def test_omitted_timeout_falls_back_to_a_capped_third_of_the_interval(interval_s: int, expected: float) -> None:
    """Requirement 5.8: ``min(interval_s / 3, 10.0)``, so the cap binds from a 30 second interval upward."""
    assert resolve_timeout(None, interval_s) == pytest.approx(expected)


@pytest.mark.parametrize("interval_s", [MIN_POLL_INTERVAL_S, 15, DEFAULT_POLL_INTERVAL_S, 300, MAX_POLL_INTERVAL_S])
def test_the_fallback_never_exceeds_either_bound(interval_s: int) -> None:
    """A single attempt never claims more than a third of the interval, nor more than 10 seconds.

    One attempt at a third of the interval is what leaves the cycle budget of Requirement 5.5 room for retries at
    shorter intervals; from a 30 second interval upward the cap binds instead and the budget check does the rest.
    """
    timeout = resolve_timeout(None, interval_s)

    assert timeout <= interval_s / 3
    assert timeout <= MAX_FALLBACK_TIMEOUT_S
    assert timeout > 0


@pytest.mark.parametrize(
    ("params", "expected_timeout"),
    [
        (_IDRAC_PARAMS, 5.0),
        ({**_IDRAC_PARAMS, "request_timeout_s": 10.0}, 10.0),
        ({**_IDRAC_PARAMS, "request_timeout_s": 4.0}, 4.0),
    ],
)
def test_a_plugin_schema_default_is_not_an_omission(params: dict[str, Any], expected_timeout: float) -> None:
    """A validated parameter model always carries a timeout, so presence comes from ``model_fields_set``.

    This is the caller contract :func:`resolve_timeout` documents. Reading the resolved attribute instead would turn
    every iDRAC entry into an explicit 10 seconds and make the Requirement 5.8 fallback unreachable — the first case
    below omits the field and must still fall back to a third of its 15 second interval.
    """
    model = IdracParams(**params)

    declared = model.request_timeout_s if "request_timeout_s" in model.model_fields_set else None

    assert resolve_timeout(declared, 15) == pytest.approx(expected_timeout)


def test_the_raw_parameter_mapping_carries_the_same_distinction() -> None:
    """The parser keeps ``params`` unnormalized, so a caller can read presence straight off the entry."""
    entry = DeviceEntry(device_id="poweredge_a", plugin="idrac", params=dict(_IDRAC_PARAMS))

    assert entry.params.get("request_timeout_s") is None
    assert resolve_timeout(entry.params.get("request_timeout_s"), 15) == pytest.approx(5.0)
