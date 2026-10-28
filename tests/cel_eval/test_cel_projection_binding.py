from collections.abc import Mapping

import pytest

from kora.cel import CheckedExpression, JsonValue, check_typed, cut
from kora.cel.types import CelType
from kora.cel_eval.binding import load_binding
from kora.cel_eval.errors import ErrorContext, EvaluationError
from kora.cel_eval.evaluate import Machine, ResultType
from tests.cel.generator import SeededSource
from tests.cel.projection_support import (
    any_value,
    projection_environment,
    report_value,
)
from tests.cel_eval.test_cel_evaluation import clock

EXPRESSIONS: tuple[str, ...] = (
    "inputs.review.verdict == 'approved'",
    "inputs.review.verdict == 'approved' && inputs.bench.cpu < inputs.review.cpu",
    "has(inputs.review.opt)",
    "has(inputs.review.opt) && inputs.review.opt > 1",
    "'k' in inputs.review.labels",
    "'k' in inputs.review.labels && inputs.review.labels['k'] == 'a'",
    "inputs.review.labels[inputs.bench.tags[0]] == 'a'",
    "inputs.review.tags[1] == 'x'",
    "inputs.review.tags.size() > 1",
    "'x' in inputs.review.tags",
    "inputs.review.notes.exists(n, has(n.score) && n.score > 1)",
    "inputs.review.notes.map(n, has(n.text) ? n.text : '').size() == 2",
    "inputs.review.notes.filter(n, has(n.score)).size() == 0",
    "inputs.review.facts == inputs.bench.facts",
    "has(inputs.review.facts.change) || has(inputs.review.facts.size)",
    "has(inputs.review.facts.size) && inputs.review.facts.size > 3",
    "inputs.review.labels.exists(k, k == 'x')",
    "has(inputs.review.facts.size) && string(inputs.review.facts.size) == '3'",
    "response.verdict == 'approved' ? inputs.review.cpu > 1.0 : has(response.opt)",
    "payload.a.b == 1",
    "has(payload.a)",
    "has(payload.a.b)",
    "has(payload.a) && payload.a.b == 1 && has(payload.a.m)",
    "'k' in payload.m",
    "payload[0] == 1",
    "payload == true",
    "payload.a == true",
    "payload.m.exists(x, x == 1)",
)
VALUE_SEEDS: int = 40

Outcome = bool | str


def outcome(result: bool | str | EvaluationError) -> Outcome:
    return result.kind.value if isinstance(result, EvaluationError) else result


def evaluated(
    machine: Machine,
    checked: CheckedExpression,
    values: Mapping[str, JsonValue],
    types: Mapping[str, CelType],
) -> Outcome:
    result = machine.evaluate(
        checked, values, types, ErrorContext("when"), ResultType.BOOL
    )
    return outcome(result)


def sample(seed: int) -> dict[str, JsonValue]:
    source = SeededSource(seed)
    return {
        "inputs": {"review": report_value(source), "bench": report_value(source)},
        "payload": any_value(source),
        "response": report_value(source),
    }


@pytest.mark.parametrize("source", EXPRESSIONS)
def test_cel_projection_matches_full_binding(source: str) -> None:
    environment, types = projection_environment()
    checked = check_typed(source, environment)
    assert isinstance(checked, CheckedExpression), checked
    machine = Machine(load_binding(), clock)
    samples = [sample(seed) for seed in range(VALUE_SEEDS)]

    pairs = [
        (
            evaluated(machine, checked, values, types),
            evaluated(
                machine,
                checked,
                {
                    name: cut(value, checked.paths[name])
                    for name, value in values.items()
                    if name in checked.paths
                },
                types,
            ),
        )
        for values in samples
    ]

    assert [full for full, _ in pairs] == [projected for _, projected in pairs]
    assert len({full for full, _ in pairs}) >= 2
