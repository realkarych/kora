from typing import Final, get_args

import pytest

from kora.ir import ErrorClass
from kora.protocols.harness import RunnerCause, UnconfirmedSample
from kora.runner.outcome import CAUSE_CLASSES, Bypass, final_class

NO_BYPASS: Final[Bypass] = Bypass(False, UnconfirmedSample(items=(), total=0))
CONFIRMED: Final[Bypass] = Bypass(True, UnconfirmedSample(items=(), total=0))
STOPS: Final[tuple[RunnerCause, ...]] = tuple(
    cause for cause in get_args(RunnerCause) if cause != "completed"
)


def test_final_class_precedence_covers_every_stop() -> None:
    covered = set(CAUSE_CLASSES)

    assert covered == set(STOPS)


@pytest.mark.parametrize(
    ("cause", "expected"),
    [
        ("max_events", ErrorClass.EVENTS_EXHAUSTED),
        ("budget", ErrorClass.BUDGET_EXHAUSTED),
        ("stall", ErrorClass.STALLED),
        ("deadline", ErrorClass.TIMEOUT),
        ("cancel", ErrorClass.CANCELLED),
        ("provider_limit", ErrorClass.PROVIDER_LIMITED),
        ("policy_bypass", ErrorClass.HARNESS_ERROR),
        ("version_mismatch", ErrorClass.CONFIG_ERROR),
        ("plugin_mismatch", ErrorClass.CONFIG_ERROR),
        ("integrity_mismatch", ErrorClass.INTEGRITY_ERROR),
        ("config_check_failed", ErrorClass.CONFIG_ERROR),
        ("policy_error", ErrorClass.CONFIG_ERROR),
        ("policy_timeout", ErrorClass.INFRASTRUCTURE),
        ("journal_unavailable", ErrorClass.INFRASTRUCTURE),
        ("cli_exit_without_result", ErrorClass.HARNESS_ERROR),
        ("oversized_message", ErrorClass.HARNESS_ERROR),
        ("launch_rejected", ErrorClass.CONFIG_ERROR),
        ("launch_failed", ErrorClass.INFRASTRUCTURE),
        ("launcher_died", ErrorClass.INFRASTRUCTURE),
    ],
)
def test_final_class_precedence_table(cause: RunnerCause, expected: ErrorClass) -> None:
    error_class = final_class(cause, None, NO_BYPASS, False)

    assert error_class is expected


@pytest.mark.parametrize("cause", get_args(RunnerCause))
def test_final_class_precedence_bypass_wins_over_every_stop(
    cause: RunnerCause,
) -> None:
    error_class = final_class(cause, None, CONFIRMED, True)

    assert error_class is ErrorClass.HARNESS_ERROR


@pytest.mark.parametrize(
    ("result_class", "provider_limit_seen", "expected"),
    [
        (None, False, None),
        (None, True, None),
        (ErrorClass.HARNESS_ERROR, False, ErrorClass.HARNESS_ERROR),
        (ErrorClass.HARNESS_ERROR, True, ErrorClass.PROVIDER_LIMITED),
    ],
)
def test_final_class_precedence_provider_limit_row(
    result_class: ErrorClass | None,
    provider_limit_seen: bool,
    expected: ErrorClass | None,
) -> None:
    error_class = final_class("completed", result_class, NO_BYPASS, provider_limit_seen)

    assert error_class is expected
