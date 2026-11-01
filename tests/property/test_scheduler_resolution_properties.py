"""Properties 14 and 15: the schedule resolution rules of ``scheduler.py`` (tasks 11.5, 11.6).

Two statements about the pure functions of Section 1 of ``scheduler.py``, each over an input space
too large to enumerate:

* **Property 14** -- *for any* device-specific poll interval value (present or absent) and any global
  poll interval, the resolved interval equals the device-specific value if and only if it is present
  and within 5-3600 seconds inclusive, and equals the global interval otherwise, with the rejected
  value reported whenever a present device value is not used (Requirements 5.2, 5.7);
* **Property 15** -- *for any* poll interval in 5-3600 seconds and any absent request timeout, the
  resolved request timeout equals min(interval / 3, 10) seconds; when a request timeout is present,
  the resolved timeout equals it (Requirement 5.8).

They share a module because they share a subject: ``resolve_interval`` and ``resolve_timeout`` are
the two pure resolution functions, both run in microseconds with no clock and no I/O, and the second
is handed what the first returned. Generators and constants are local to this module rather than
shared, so this file stands alone. The cycle and loop properties are separate modules, for the same
reason their unit tests are.

## Where the expectations come from

Every bound is written out here -- 5 and 3600 for the interval range, 3 and 10 for the timeout
fallback -- read off Requirements 5.2, 5.7, and 5.8, rather than imported from ``config.models`` or
from the module under test. Importing ``MIN_POLL_INTERVAL_S``, ``MAX_POLL_INTERVAL_S``,
``TIMEOUT_INTERVAL_DIVISOR``, or ``MAX_FALLBACK_TIMEOUT_S`` to build an expectation would assert only
that the implementation equals itself: a bound changed on one side would move the generator, the
oracle, and the implementation together, and these properties would still pass. Written out, the two
disagree the moment the implementation's do, which is the only thing that makes a generated input
worth generating.

# Property 14: interval resolution (Requirements 5.2, 5.7)

## The generated interval space

A rule with two inclusive bounds fails at its bounds, so the declared interval is drawn from five
bands rather than one uniform range:

* **absent** -- ``None``, the entry that declares no interval at all. Requirement 5.2 displaces the
  global interval only ``WHERE`` an entry declares one, so absence is a distinct case and not merely
  an out-of-range value;
* **inside** -- the whole 5-3600 range, so the accepted path is exercised across its width;
* **the bounds and their neighbours** -- 4, 5, 3600, and 3601 as explicit draws. "Inclusive" is the
  one word in Requirement 5.2 that an off-by-one in either comparison would break, and uniform draws
  from a 3596-wide range land on its ends too rarely to catch it;
* **above** -- up to a day, which covers the hand-edited 86400 that reads as "poll once a day";
* **below** -- down to -3600, which covers 0 and the negative values a hand-edited configuration
  produces by typo or by unit confusion.

Non-integer declared values are deliberately out of scope. The parameter is typed ``int | None``, and
a value that is not a whole number of seconds is refused upstream by the Configuration_Parser
(Requirement 2.2), so generating one here would exercise pydantic rather than the resolution rule.

The global interval is drawn across the same accepted range and both of its bounds, **plus values
outside it**. Requirement 2.7 makes an out-of-range global interval fatal to the whole configuration,
so one cannot reach the scheduler in production -- but Property 14 quantifies over *any* global
interval, and the resolution deliberately uses it as given rather than re-clamping it, on the grounds
that clamping would mask a caller that skipped validation. Generating out-of-range globals is what
makes "used as given" observable: a clamp introduced later fails here rather than silently changing
how often a device is polled.

## Why the branch has to be witnessed, not inferred from the number

``resolved == declared`` is not evidence that the declared value was *used*. When the declared value
and the global interval happen to be equal -- which the generators produce, and which a shrinker will
find -- the two branches return the same number. So the "if and only if" is asserted against
``IntervalResolution.rejected``, which says which branch ran, and the numeric equalities are asserted
separately.

## The log record clause, and where it lives

Property 14 closes with "a log record naming the entry identifier and the rejected value whenever a
present device value is not used". ``resolve_interval`` is pure by design: it is handed no entry
identifier and emits no record, and instead returns the refused value for its caller to name. What
this property can establish, and does, is the half that lives here -- a refused value comes back
*unchanged* and distinguishable from "nothing was refused", which is exactly the information
Requirement 5.7's record needs. The record itself is written by the caller,
``Application._schedule_for`` in ``app.py``.

# Property 15: timeout resolution (Requirement 5.8)

## Presence, not value, selects the branch

Requirement 5.8 is a ``WHERE`` clause on omission, so what separates the two clauses is whether the
entry declared a timeout at all -- never what the declared number is. That distinction is the
caller's to make and cannot be recovered downstream: ``IdracParams.request_timeout_s`` defaults to
10.0, so a validated parameter model always carries a number. The schema default is therefore drawn
as a notable value, because a declared 10.0 and a capped fallback of 10.0 are the same float: a test
that inferred the branch from the resolved number would pass on both while being blind to a
resolution that had stopped telling them apart.

## The generated timeout space

* **absent** -- ``None``, the entry that declares no timeout at all;
* **inside** -- the whole 1-60 second range the iDRAC schema publishes (Requirement 4.1), so the
  present path is exercised across its width, with both bounds and the 10.0 default sampled
  explicitly;
* **out of range** -- below the minimum down to and including zero, and above the maximum up to ten
  minutes. These cannot reach the resolution through a validated schema, and that is the point:
  Requirement 5.8 caps the *fallback*, not a declared value, so a cap or a floor quietly applied to
  a present timeout shows up here rather than in production.

Non-finite values are deliberately not generated. A ``NaN`` is unequal to itself, so no equality
could be asserted against it, and a bounded float field refuses one upstream anyway.

## The generated interval space, and the cap boundary

A third of the interval reaches 10 seconds at an interval of exactly 30, so 30 is where the ``min``
changes hands and where a ``>`` written for a ``>=`` would hide. 29, 30, and 31 are drawn explicitly,
as are both ends of the 5-3600 range; uniform draws across 3596 values land on a chosen boundary far
too rarely to rely on.

5-30 is drawn as a band of its own as well, and not for symmetry: from 30 upward the answer is the
cap, so the division only decides anything below it. Of the 3596 accepted intervals, 3571 are capped
and 8 more divide by three exactly, which leaves **17** whose third has no finite binary form -- all
of them under 30. Uniform draws over the full range would reach that arithmetic roughly once in 200
examples; a band over the sub-cap region reaches it constantly.

Out-of-range intervals come in through ``global_intervals()`` rather than through a second set of
bands. Requirement 5.8 states the fallback for the interval range the resolution produces, so an
interval outside 5-3600 carries no expectation for the omission clause and is skipped there. It is
still worth drawing, because the *present* clause holds for any interval whatever -- the declared
value is used as given, neither capped nor compared against the interval -- and a comparison
introduced later fails on exactly these draws.

## Where equality is exact, and where it cannot be

``interval_s / 3`` has no finite binary form for two intervals in three, so the resolved timeout
cannot be compared against a rational expectation with plain equality everywhere. The expectation is
therefore built in exact rational arithmetic -- ``min(Fraction(interval_s, 3), 10)`` -- and split on
whether that exact value is a whole number of seconds:

* **a whole number** is exactly representable in binary, so equality is asserted **exactly**, with no
  tolerance at all. That covers every capped interval, 30 seconds and up, where the answer is 10, and
  every interval divisible by three;
* **anything else** is compared against the exact rational within **half of the last bit of the
  returned float**, taken from :func:`math.ulp`. That bound is not slack chosen to make a test pass:
  it is the whole error a correctly rounded IEEE-754 division may introduce, around 1e-15 at these
  magnitudes, so a divisor that was not three, or a division that floored to whole seconds, misses by
  fourteen orders of magnitude more than the tolerance allows.

A declared timeout undergoes no arithmetic at all, so it is asserted with exact equality.
"""

from __future__ import annotations

import math
from fractions import Fraction
from typing import Final

from hypothesis import given, settings
from hypothesis import strategies as st

from remote_energy_meter.scheduler import resolve_interval, resolve_timeout

#: The accepted device-specific poll interval range in whole seconds, read off Requirements 5.2 and
#: 5.7 and written out rather than imported from the module under test.
ACCEPTED_MIN_INTERVAL_S: Final = 5
ACCEPTED_MAX_INTERVAL_S: Final = 3600

#: One day, standing in for the plainly-too-large value a hand-edited configuration reaches for.
FAR_ABOVE_INTERVAL_S: Final = 86_400

#: The global interval when the Addon_Configuration omits it (Requirement 2.6), sampled as a notable
#: value only -- nothing in the expectation depends on it.
DEFAULT_GLOBAL_INTERVAL_S: Final = 30

#: The accepted request timeout range in seconds: the range the iDRAC plugin publishes
#: (Requirement 4.1), plus the schema default, which is the same number as the fallback cap.
ACCEPTED_MIN_TIMEOUT_S: Final = 1.0
ACCEPTED_MAX_TIMEOUT_S: Final = 60.0
SCHEMA_DEFAULT_TIMEOUT_S: Final = 10.0

#: Ten minutes, standing in for a declared timeout plainly outside any published range.
FAR_ABOVE_TIMEOUT_S: Final = 600.0

#: Requirement 5.8's fallback written out: one third of the poll interval, capped at 10 seconds.
#: Named apart from the implementation's ``TIMEOUT_INTERVAL_DIVISOR`` and ``MAX_FALLBACK_TIMEOUT_S``
#: on purpose -- see the module docstring on where the expectations come from.
FALLBACK_INTERVAL_DIVISOR: Final = 3
FALLBACK_TIMEOUT_CAP_S: Final = 10.0

#: Where a third of the interval reaches the cap, so where the ``min`` changes hands: 3 x 10 seconds.
CAP_BINDING_INTERVAL_S: Final = 30


# --------------------------------------------------------------------------------------------------
# generators
# --------------------------------------------------------------------------------------------------


def declared_intervals() -> st.SearchStrategy[int | None]:
    """A device-specific poll interval, present or absent, spanning the accepted range and both bounds.

    Five bands: absent, inside 5-3600, the bounds and their immediate neighbours, above the maximum,
    and below the minimum including zero and negatives.
    """
    return st.one_of(
        st.none(),
        st.integers(min_value=ACCEPTED_MIN_INTERVAL_S, max_value=ACCEPTED_MAX_INTERVAL_S),
        st.sampled_from(
            [
                ACCEPTED_MIN_INTERVAL_S - 1,
                ACCEPTED_MIN_INTERVAL_S,
                ACCEPTED_MAX_INTERVAL_S,
                ACCEPTED_MAX_INTERVAL_S + 1,
            ]
        ),
        st.integers(min_value=ACCEPTED_MAX_INTERVAL_S + 1, max_value=FAR_ABOVE_INTERVAL_S),
        st.integers(min_value=-ACCEPTED_MAX_INTERVAL_S, max_value=ACCEPTED_MIN_INTERVAL_S - 1),
    )


def global_intervals() -> st.SearchStrategy[int]:
    """A global poll interval, in and out of the range Requirement 2.7 validates it against.

    Out-of-range values are included because Property 14 quantifies over any global interval and the
    resolution uses it as given; they are what makes a later clamp visible. The two bounds and the
    default are sampled explicitly so a valid global interval is not always a mid-range number.
    """
    return st.one_of(
        st.integers(min_value=ACCEPTED_MIN_INTERVAL_S, max_value=ACCEPTED_MAX_INTERVAL_S),
        st.sampled_from([ACCEPTED_MIN_INTERVAL_S, DEFAULT_GLOBAL_INTERVAL_S, ACCEPTED_MAX_INTERVAL_S]),
        st.integers(min_value=-ACCEPTED_MAX_INTERVAL_S, max_value=ACCEPTED_MIN_INTERVAL_S - 1),
        st.integers(min_value=ACCEPTED_MAX_INTERVAL_S + 1, max_value=FAR_ABOVE_INTERVAL_S),
    )


def declared_timeouts() -> st.SearchStrategy[float | None]:
    """A declared request timeout, present or absent, spanning the accepted range and beyond it.

    Five bands: absent, inside 1-60 seconds, the two bounds and the schema default, below the minimum
    down to zero, and above the maximum. Non-finite values are excluded -- a ``NaN`` is unequal to
    itself, so nothing could be asserted about it.
    """
    return st.one_of(
        st.none(),
        st.floats(
            min_value=ACCEPTED_MIN_TIMEOUT_S,
            max_value=ACCEPTED_MAX_TIMEOUT_S,
            allow_nan=False,
            allow_infinity=False,
        ),
        st.sampled_from([ACCEPTED_MIN_TIMEOUT_S, SCHEMA_DEFAULT_TIMEOUT_S, ACCEPTED_MAX_TIMEOUT_S]),
        st.floats(
            min_value=0.0,
            max_value=ACCEPTED_MIN_TIMEOUT_S,
            exclude_max=True,
            allow_nan=False,
            allow_infinity=False,
        ),
        st.floats(
            min_value=ACCEPTED_MAX_TIMEOUT_S,
            max_value=FAR_ABOVE_TIMEOUT_S,
            exclude_min=True,
            allow_nan=False,
            allow_infinity=False,
        ),
    )


def resolved_intervals() -> st.SearchStrategy[int]:
    """A poll interval as ``resolve_interval`` hands it on, concentrated around the cap boundary.

    The whole accepted range, the sub-cap region on its own, the two ends, and the three intervals
    around the point where the cap starts to bind. ``global_intervals()`` is folded in rather than
    restated, so out-of-range intervals are drawn as well: they carry no fallback expectation, but
    the declared-timeout clause holds for any interval and they are what would catch a declared
    value compared against one.
    """
    return st.one_of(
        st.integers(min_value=ACCEPTED_MIN_INTERVAL_S, max_value=ACCEPTED_MAX_INTERVAL_S),
        st.integers(min_value=ACCEPTED_MIN_INTERVAL_S, max_value=CAP_BINDING_INTERVAL_S),
        st.sampled_from(
            [
                ACCEPTED_MIN_INTERVAL_S,
                CAP_BINDING_INTERVAL_S - 1,
                CAP_BINDING_INTERVAL_S,
                CAP_BINDING_INTERVAL_S + 1,
                ACCEPTED_MAX_INTERVAL_S,
            ]
        ),
        global_intervals(),
    )


# --------------------------------------------------------------------------------------------------
# the expectation
# --------------------------------------------------------------------------------------------------


def declares_an_accepted_interval(declared_s: int | None) -> bool:
    """Whether Requirement 5.2's ``WHERE`` clause holds for this declared value.

    Two conditions, both from the requirement text: an interval is declared at all, and it lies
    within 5-3600 seconds *inclusive*. Requirement 5.7 is the complement over declared values, so
    these two together partition the whole input space and no third outcome is possible.
    """
    return declared_s is not None and ACCEPTED_MIN_INTERVAL_S <= declared_s <= ACCEPTED_MAX_INTERVAL_S


def exact_fallback_timeout_s(interval_s: int) -> Fraction:
    """Requirement 5.8's fallback for an omitted timeout, in exact rational arithmetic.

    ``min(interval / 3, 10)`` with the divisor and the cap written out here rather than imported, and
    computed over :class:`~fractions.Fraction` so the expectation carries no floating point error of
    its own. That is what lets the comparison against the resolved float be as tight as correct
    rounding allows instead of as loose as two float expressions require.
    """
    return min(Fraction(interval_s, FALLBACK_INTERVAL_DIVISOR), Fraction(FALLBACK_TIMEOUT_CAP_S))


# --------------------------------------------------------------------------------------------------
# the properties
# --------------------------------------------------------------------------------------------------


# Feature: remote-energy-meter, Property 14: For any device-specific poll interval value (present or
# absent) and any global poll interval, the resolved interval equals the device-specific value if and
# only if it is present and within 5-3600 seconds inclusive, and equals the global interval otherwise,
# with a log record naming the entry identifier and the rejected value whenever a present device value
# is not used.
# Validates: Requirements 5.2, 5.7
@settings(max_examples=300, deadline=None)
@given(declared_s=declared_intervals(), global_s=global_intervals())
def test_interval_resolution_prefers_a_valid_device_interval(declared_s: int | None, global_s: int) -> None:
    """Requirements 5.2 and 5.7, over the whole declared and global interval space."""
    accepted = declares_an_accepted_interval(declared_s)

    resolution = resolve_interval(declared_s, global_s)

    # --- which branch ran, witnessed rather than inferred from the resolved number ---
    # A declared value was *used* exactly when it was present and nothing was refused. Reading this
    # off ``interval_s == declared_s`` instead would pass vacuously whenever the declared value and
    # the global interval coincide, which is precisely where a swapped comparison hides.
    used_declared = declared_s is not None and not resolution.rejected
    assert used_declared == accepted

    # --- the resolved interval (Requirements 5.2, 5.7) ---
    if accepted:
        assert resolution.interval_s == declared_s
    else:
        # Equality with the global interval as given, not merely "not the declared value": an
        # out-of-range global that came back clamped, or replaced by a floor, fails here.
        assert resolution.interval_s == global_s

    # --- the refused value, reported for the caller's record (Requirement 5.7) ---
    if accepted or declared_s is None:
        assert resolution.rejected_value is None
    else:
        # Unchanged, so the record can name the value the administrator actually wrote.
        assert resolution.rejected_value == declared_s

    # --- the flag the caller branches on agrees with the value it reports ---
    assert resolution.rejected == (resolution.rejected_value is not None)


# Feature: remote-energy-meter, Property 15: For any poll interval in 5-3600 seconds and any absent
# request timeout, the resolved request timeout equals min(interval / 3, 10) seconds; when a request
# timeout is present, the resolved timeout equals it.
# Validates: Requirements 5.8
@settings(max_examples=300, deadline=None)
@given(declared_s=declared_timeouts(), interval_s=resolved_intervals())
def test_timeout_resolution_falls_back_to_a_capped_third_of_the_interval(
    declared_s: float | None, interval_s: int
) -> None:
    """Requirement 5.8, over the whole declared timeout space and the resolved interval range."""
    timeout_s = resolve_timeout(declared_s, interval_s)

    # --- a declared timeout is used as given, whatever the interval ---
    # Exact equality, and exact is warranted because no arithmetic touches a present value. Asserted
    # for out-of-range timeouts and out-of-range intervals too: the accepted range is the plugin
    # schema's business (Requirement 4.1) and Requirement 5.8 caps the fallback only, so a cap, a
    # floor, or a comparison against the interval introduced later fails on these draws.
    if declared_s is not None:
        assert timeout_s == declared_s
        return

    # --- an omitted timeout falls back to a capped third of the interval (Requirement 5.8) ---
    # The fallback is stated for the interval range the resolution produces, so the out-of-range
    # intervals ``global_intervals()`` contributes carry no expectation here; they were drawn for the
    # clause above, which holds for any interval.
    if not (ACCEPTED_MIN_INTERVAL_S <= interval_s <= ACCEPTED_MAX_INTERVAL_S):
        return

    expected = exact_fallback_timeout_s(interval_s)
    if expected.denominator == 1:
        # A whole number of seconds is exactly representable in binary, so nothing here is tolerated:
        # this branch is every capped interval, 30 seconds and up, plus every interval divisible by
        # three.
        assert Fraction(timeout_s) == expected
    else:
        # Otherwise the resolved value is the nearest double to a third with no finite binary form,
        # and may differ from the exact rational by half of its own last bit -- by exactly the error
        # a correctly rounded IEEE-754 division introduces, and no more. Around 1e-15 here, so a
        # divisor that was not three, or a division that floored to whole seconds, misses this by
        # fourteen orders of magnitude rather than slipping through it.
        assert abs(Fraction(timeout_s) - expected) <= Fraction(math.ulp(timeout_s)) / 2

    # --- which branch of the min ran, stated from the interval rather than read off the result ---
    # A third of the interval reaches the cap at exactly 30 seconds, so the cap binds from there
    # upward and not one second earlier.
    assert (timeout_s == FALLBACK_TIMEOUT_CAP_S) == (interval_s >= CAP_BINDING_INTERVAL_S)

    # --- and neither bound of the min is exceeded ---
    # The cap holds exactly, and a fallback of zero or less would leave an attempt no time to run.
    assert timeout_s <= FALLBACK_TIMEOUT_CAP_S
    assert timeout_s > 0.0
