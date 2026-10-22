from pathlib import Path

import pytest

from kora.cel import CheckedExpression, Diagnostic, Reason, ValueBound, Variable, check
from kora.cel.cost import PROVISIONAL_BUDGET
from kora.cel.types import DYN, INT, STRING, ListOf


def test_cel_cost_constants_match_specification() -> None:
    specification = (Path(__file__).parents[2] / "knowledge/specs/ir.md").read_text()

    budget = PROVISIONAL_BUDGET

    assert budget.steps == 100_000_000_000_000
    assert budget.memory == 16_777_216
    assert "100,000,000,000,000" in specification
    assert "100 ms" in specification


def test_cel_larger_default_budget_accepts_more_estimated_work() -> None:
    environment = {"payload": Variable(DYN)}

    result = check("payload.items.map(x, x)", environment)

    assert isinstance(result, CheckedExpression)
    assert 10_000_000_000_000 < result.estimate.steps <= PROVISIONAL_BUDGET.steps


def test_cel_larger_default_budget_still_refuses_nested_excessive_work() -> None:
    environment = {"payload": Variable(DYN)}

    result = check("payload.items.map(x, payload.items.all(y, true))", environment)

    assert isinstance(result, Diagnostic)
    assert result.reason is Reason.STEPS


@pytest.mark.parametrize(
    ("source", "environment", "expected"),
    [
        ("true", {}, (1_630_000_000, 0, 4_000_000_000)),
        ("1 + 2 * 3 == 7", {}, (1_872_500_000, 0, 20_000_000_000)),
        (
            "s == s",
            {"s": Variable(STRING, ValueBound(6, 1, 0, 4))},
            (1_801_680_000, 6, 8_000_000_000),
        ),
        (
            "s.matches('^[a-z]+$')",
            {"s": Variable(STRING, ValueBound(6, 1, 0, 4))},
            (2_318_937_840, 257, 21_000_000_000),
        ),
        (
            "l.map(x, x + 1)",
            {
                "l": Variable(
                    ListOf(INT), ValueBound(8, 4, 3, 0, ValueBound(1, 1, 0, 0))
                )
            },
            (2_556_640_000, 84, 17_000_000_000),
        ),
    ],
)
def test_cel_cost_estimate_golden_corpus(
    source: str, environment: dict[str, Variable], expected: tuple[int, int, int]
) -> None:
    result = check(source, environment)

    assert isinstance(result, CheckedExpression)
    estimate = result.estimate

    assert (estimate.steps, estimate.memory, estimate.compile_steps) == expected
