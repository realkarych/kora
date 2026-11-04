import pytest

from kora.cel_eval.evaluate import is_pattern_refusal
from tests.cel_eval.children import ROOT, ChildResult, full_path, run_child
from tests.cel_eval.pattern_validation_scenarios import PRIVATE_PATTERN_MARKER


def run_pattern_scenario(scenario: str) -> ChildResult:
    code = (
        f"from tests.cel_eval.pattern_validation_scenarios import {scenario}\n"
        f"{scenario}()"
    )
    return run_child(code, full_path(ROOT))


def test_cel_native_matches_distinguishes_compile_from_match() -> None:
    scenario = "check_native_results"

    result = run_pattern_scenario(scenario)

    assert result.code == 0, result.stderr
    assert result.stdout == ""
    assert "cel.evaluator.started" in result.stderr
    assert "cel.evaluator.stopped" in result.stderr
    assert PRIVATE_PATTERN_MARKER not in result.stderr
    assert "Function 'matches' error:" not in result.stderr


def test_cel_native_matches_preserves_static_admission() -> None:
    scenario = "check_static_admission"

    result = run_pattern_scenario(scenario)

    assert result.code == 0, result.stderr
    assert result.stdout == result.stderr == ""


class UnexpectedRuntimeError(RuntimeError):
    pass


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (RuntimeError("Function 'matches' error: marker"), True),
        (RuntimeError("Internal CEL error: Function 'matches' error: marker"), False),
        (RuntimeError("Function 'other' error: marker"), False),
        (RuntimeError("Function 'matches' error: marker", 1), False),
        (RuntimeError(), False),
        (UnexpectedRuntimeError("Function 'matches' error: marker"), False),
    ],
)
def test_cel_native_matches_error_classification(
    error: RuntimeError, expected: bool
) -> None:
    result = is_pattern_refusal(error)

    assert result is expected


@pytest.mark.parametrize(
    "scenario", ["check_shared_compile_limits", "check_initial_charge"]
)
def test_cel_native_matches_shares_compile_admission(scenario: str) -> None:
    result = run_pattern_scenario(scenario)

    assert result.code == 0, result.stderr
    assert result.stdout == result.stderr == ""


def test_cel_native_matches_releases_cancelled_work() -> None:
    scenario = "check_cancellation"

    result = run_pattern_scenario(scenario)

    assert result.code == 0, result.stderr
    assert result.stdout == result.stderr == ""
