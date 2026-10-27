import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from kora.cel import CheckedExpression, Diagnostic, Reason, ValueBound, Variable, check
from kora.cel.cost import PROVISIONAL_BUDGET
from kora.cel.types import DYN, INT, STRING, ListOf
from tests.measurements.cel_cost_smoke import TARGET_NS

MEBIBYTE = 1024 * 1024
NANOSECONDS_PER_MILLISECOND = 1_000_000
SPECIFICATION = Path(__file__).parents[2] / "knowledge/specs/ir.md"
COST_SECTION = "### 7.6 Data bounds, cost and evaluation"
GROUPED = r"(\d{1,3}(?:,\d{3})*)"


@dataclass(frozen=True)
class Statement:
    anchor: str
    value: re.Pattern[str]
    scales: tuple[int, ...]


STEPS_BUDGET = Statement(
    "The steps budget is ",
    re.compile(rf"\*\*{GROUPED} logical charge units\*\*"),
    (1,),
)
MEMORY_BUDGET = Statement(
    "The memory budget remains ",
    re.compile(rf"\*\*(\d+) MiB \({GROUPED} logical estimated bytes\)\*\*"),
    (MEBIBYTE, 1),
)
REGRESSION_TARGET = Statement(
    "regression target is ",
    re.compile(r"\*\*(\d+) ms per evaluation interval\*\*"),
    (NANOSECONDS_PER_MILLISECOND,),
)
INTERVAL_LIMIT = Statement(
    "must be at most ",
    re.compile(rf"{GROUPED} ns\."),
    (1,),
)


def section(document: str, heading: str) -> str:
    start = document.index(f"\n{heading}\n")
    end = document.index("\n### ", start + len(heading))
    return document[start:end]


def scaled(
    match: re.Match[str] | None, scales: tuple[int, ...]
) -> tuple[int, ...] | None:
    if match is None:
        return None
    return tuple(
        int(digits.replace(",", "")) * scale
        for digits, scale in zip(match.groups(), scales, strict=True)
    )


def stated(text: str, statement: Statement) -> list[tuple[int, ...] | None]:
    return [
        scaled(statement.value.match(text, anchor.end()), statement.scales)
        for anchor in re.finditer(re.escape(statement.anchor), text)
    ]


def test_cel_cost_constants_match_specification() -> None:
    cost_section = section(SPECIFICATION.read_text(encoding="utf-8"), COST_SECTION)

    steps = stated(cost_section, STEPS_BUDGET)
    memory = stated(cost_section, MEMORY_BUDGET)
    target = stated(cost_section, REGRESSION_TARGET)
    limit = stated(cost_section, INTERVAL_LIMIT)

    assert steps == [(PROVISIONAL_BUDGET.steps,)]
    assert memory == [(PROVISIONAL_BUDGET.memory, PROVISIONAL_BUDGET.memory)]
    assert target == [(TARGET_NS,)]
    assert limit == [(TARGET_NS,)]


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
