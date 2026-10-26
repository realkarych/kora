import sys

import pytest

from kora.conformance.controllers import CleanupPermit, HelperOrigin, duration_ns
from kora.protocols.lifetime import LifetimeLimits


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (5e-324, 0),
        (1e-9, 1),
        (0.1, 100_000_000),
        (1.0, 1_000_000_000),
        (sys.float_info.max, int(sys.float_info.max) * 1_000_000_000),
    ],
)
def test_duration_conversion_truncates_without_float_product_overflow(
    seconds: float, expected: int
) -> None:
    accepted = LifetimeLimits(cooperative_seconds=seconds)

    observed = duration_ns(accepted.cooperative_seconds)

    assert observed == expected


@pytest.mark.parametrize(
    ("birth", "cooperative", "term", "final", "stop", "kill"),
    [
        (0, 5, 7, 14, 5, 7),
        (5, 5, 7, 14, 7.25, 9.25),
        (7, 5, 7, 14, 8.75, 10.5),
        (12, 5, 7, 14, 12.5, 13),
        (12, 15, 17, 24, 15, 17),
    ],
)
def test_cleanup_helper_phase_floors_leave_bounded_progress_and_observation(
    birth: float,
    cooperative: float,
    term: float,
    final: float,
    stop: float,
    kill: float,
) -> None:
    origin = HelperOrigin(
        None, duration_ns(birth), CleanupPermit("a" * 32, duration_ns(final))
    )

    origin.tighten(
        duration_ns(cooperative),
        duration_ns(term),
        duration_ns(final),
        LifetimeLimits(),
    )

    assert origin.term == duration_ns(stop)
    assert origin.kill == duration_ns(kill)
    assert origin.final == duration_ns(final)
    assert origin.term is not None
    assert origin.kill is not None
    assert origin.final is not None
    assert origin.birth < origin.term < origin.kill < origin.final


def test_earlier_enclosing_phase_constraints_never_widen_after_completion() -> None:
    permit = CleanupPermit("a" * 32, 24_000_000_000)
    origin = HelperOrigin(None, 12_000_000_000, permit)
    limits = LifetimeLimits()

    origin.tighten(15_000_000_000, 17_000_000_000, 24_000_000_000, limits)
    origin.tighten(5_000_000_000, 7_000_000_000, 14_000_000_000, limits)
    origin.tighten(15_000_000_000, 17_000_000_000, 24_000_000_000, limits)

    assert origin.term == 12_500_000_000
    assert origin.kill == 13_000_000_000
    assert origin.final == 14_000_000_000
    assert permit.final == 14_000_000_000


def test_zero_allowance_and_insufficient_remaining_time_are_explicit() -> None:
    limits = LifetimeLimits(cooperative_seconds=5e-324, term_seconds=5e-324)
    zero = HelperOrigin(None, 100, CleanupPermit("a" * 32, 104))
    short = HelperOrigin(None, 100, CleanupPermit("b" * 32, 101))

    zero.tighten(100, 100, 104, limits)
    short.tighten(100, 100, 101, limits)

    assert (zero.term, zero.kill, zero.final) == (100, 101, 104)
    assert (short.term, short.kill, short.final) == (99, 100, 101)
