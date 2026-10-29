import pytest

from kora.cel import CheckedExpression, JsonValue, check_typed, cut, prepare
from kora.cel_eval.binding import load_binding
from kora.cel_eval.errors import ErrorContext, EvaluationError
from kora.cel_eval.evaluate import Machine, ResultType
from tests.cel.generator import SeededSource
from tests.cel.test_cel_projection import READS, projection_environment
from tests.cel_eval.test_cel_evaluation import clock


def projection_values(seed: int) -> list[dict[str, JsonValue]]:
    generator = SeededSource(seed)
    payloads: list[JsonValue] = [
        {"a": {"b": 1}, "unused": [1, 2]},
        {"a": 7},
        {"a": None},
        {"a": True},
        {"a": []},
        {},
        [{"a": 1}],
        5,
        None,
    ]
    values: list[dict[str, JsonValue]] = []
    for index in range(64):
        record: dict[str, JsonValue] = {
            "x": generator.randrange(3),
            "child": {"a": generator.randrange(5), "b": generator.randrange(5)},
            "l": [
                {"a": generator.randrange(5), "b": generator.randrange(5)}
                for _ in range(generator.randrange(5))
            ],
            "m": {"unused": 9},
        }
        if generator.randrange(2):
            record["y"] = generator.randrange(5)
        if generator.randrange(2):
            record["m"] = {"k": generator.randrange(5), "unused": 9}
        values.append({"r": record, "p": payloads[index % len(payloads)]})
    return values


@pytest.mark.parametrize("prepared", [False, True])
def test_cel_projection_matches_full_binding(prepared: bool) -> None:
    environment = projection_environment()
    selected = prepare(environment) if prepared else environment
    expressions = [(source, check_typed(source, selected)) for source, _ in READS]
    values = projection_values(375)
    types = {name: variable.type for name, variable in environment.variables.items()}
    machine = Machine(load_binding(), clock)
    context = ErrorContext("when")
    outcomes: list[bool | str] = []

    for source, checked in expressions:
        assert isinstance(checked, CheckedExpression), (source, checked)
        for original in values:
            projected = {
                name: cut(original[name], paths)
                for name, paths in checked.projections.items()
            }
            full = machine.evaluate(checked, original, types, context, ResultType.BOOL)
            result = machine.evaluate(
                checked, projected, types, context, ResultType.BOOL
            )
            if isinstance(full, EvaluationError):
                assert isinstance(result, EvaluationError), (
                    source,
                    original,
                    projected,
                )
                assert result.kind == full.kind, (
                    source,
                    original,
                    projected,
                    full,
                    result,
                )
                outcomes.append(full.kind.value)
            else:
                assert result is full, (source, original, projected, full, result)
                assert isinstance(full, bool)
                outcomes.append(full)

    assert True in outcomes
    assert False in outcomes
    assert "missing_field" in outcomes
    assert "no_overload" in outcomes
    assert "index_out_of_range" in outcomes
