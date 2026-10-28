from datetime import UTC, datetime

import pytest

from kora.cel import (
    CheckedExpression,
    Environment,
    JsonValue,
    ResultKind,
    SchemaTypes,
    Variable,
    check,
    check_typed,
)
from kora.cel.checked import SEAL
from kora.cel.types import BOOL, DOUBLE, DYN, INT, CelType, Record, RecordField
from kora.cel_eval.binding import load_binding
from tests.cel_eval.test_cel_conversion import expression, no_documents

AT: datetime = datetime(2026, 10, 21, tzinfo=UTC)


def clock() -> datetime:
    return AT


@pytest.mark.parametrize(
    ("source", "kind"),
    [
        ("{}['missing'] == 1", "missing_field"),
        ("null.missing == 1", "no_overload"),
        ("[1][4] == 1", "index_out_of_range"),
        ("1 / 0 == 1", "division_by_zero"),
        ("9223372036854775807 + 1 == 0", "overflow"),
        ("timestamp('bad') == timestamp('bad')", "invalid_argument"),
        ("int('abc') == 1", "invalid_argument"),
        ("uint('-1') == 1u", "invalid_argument"),
        ("double('x') == 1.0", "invalid_argument"),
        ("duration('1x') == duration('1s')", "invalid_argument"),
        ("int(dyn([1])) == 1", "invalid_argument"),
        ("string(dyn(true)) == 'true'", "invalid_argument"),
        ("dyn(1).startsWith('a')", "invalid_argument"),
    ],
)
def test_cel_binding_exceptions_map_to_error_kinds(source: str, kind: str) -> None:
    from kora.cel_eval.errors import ErrorContext, EvaluationError
    from kora.cel_eval.evaluate import Machine, ResultType

    machine = Machine(load_binding(), clock)
    context = ErrorContext("nodes.approval.approve_when", event_id="evt-1")
    checked = expression(source)

    result = machine.evaluate(checked, {}, {}, context, ResultType.BOOL)

    assert isinstance(result, EvaluationError)
    assert result.kind.value == kind
    assert result.fault is None
    assert result.field == context.field
    assert result.event_id == "evt-1"
    assert result.at == AT
    assert result.offset is None


@pytest.mark.parametrize("value", [1, "yes", None, [], {}])
def test_cel_dyn_result_requires_exact_bool(value: object) -> None:
    from kora.cel_eval.errors import ErrorContext, ErrorKind, EvaluationError
    from kora.cel_eval.evaluate import Machine, ResultType

    machine = Machine(load_binding(), clock)

    result = machine.evaluate(
        expression("payload"),
        {"payload": value},
        {},
        ErrorContext("when"),
        ResultType.BOOL,
    )

    assert isinstance(result, EvaluationError)
    assert result.kind is ErrorKind.WRONG_RESULT_TYPE


def test_cel_dyn_selection_needs_no_guard() -> None:
    from kora.cel_eval.errors import ErrorContext, ErrorKind, EvaluationError
    from kora.cel_eval.evaluate import Machine, ResultType

    machine = Machine(load_binding(), clock)
    checked = expression("observation.body.ready == true")

    ready = machine.evaluate(
        checked,
        {"observation": {"body": {"ready": True}}},
        {},
        ErrorContext("until"),
        ResultType.BOOL,
    )
    missing = machine.evaluate(
        checked,
        {"observation": {"body": {}}},
        {},
        ErrorContext("until"),
        ResultType.BOOL,
    )

    assert ready is True
    assert isinstance(missing, EvaluationError)
    assert missing.kind is ErrorKind.MISSING_FIELD


@pytest.mark.parametrize("cel_type", [INT, DOUBLE, DYN])
def test_cel_numeric_conversion_reaches_the_real_binding(cel_type: CelType) -> None:
    from kora.cel_eval.errors import ErrorContext
    from kora.cel_eval.evaluate import Machine, ResultType

    machine = Machine(load_binding(), clock)
    expected = "int" if cel_type is INT else "double"
    checked = expression(f"type(inputs.n) == {expected}")

    result = machine.evaluate(
        checked,
        {"inputs": {"n": 100}},
        {"inputs": Record({"n": RecordField(cel_type, True)})},
        ErrorContext("when"),
        ResultType.BOOL,
    )

    assert result is True


def test_cel_program_cache_is_bounded_and_validation_does_not_fill_it() -> None:
    from kora.cel_eval.errors import ErrorContext
    from kora.cel_eval.evaluate import Machine, ResultType

    machine = Machine(load_binding(), clock, cache_entries=2, cache_characters=64)
    first = expression("true")

    for index in range(10):
        assert machine.validate(expression(f"{index} == {index}")).valid
    empty = machine.cache.snapshot()
    for source in ["true", "false", "true", "1 == 1"]:
        machine.evaluate(
            expression(source), {}, {}, ErrorContext("when"), ResultType.BOOL
        )
    populated = machine.cache.snapshot()
    repeated = machine.evaluate(first, {}, {}, ErrorContext("when"), ResultType.BOOL)

    assert empty.entries == 0
    assert empty.characters == 0
    assert populated.entries == 2
    assert populated.characters <= 64
    assert populated.hits == 1
    assert populated.evictions == 1
    assert repeated is True
    assert machine.cache.snapshot().hits == 2


def test_cel_program_cache_checks_rendering_on_a_digest_collision() -> None:
    from kora.cel_eval.errors import ErrorContext, ErrorKind, EvaluationError
    from kora.cel_eval.evaluate import Machine, ResultType

    machine = Machine(load_binding(), clock)
    original = expression("true")
    other = check("false")
    assert isinstance(other, CheckedExpression)
    collision_input = CheckedExpression(
        SEAL, other.tree, other.rendering, original.digest, other.estimate, {}
    )

    accepted = machine.evaluate(original, {}, {}, ErrorContext("when"), ResultType.BOOL)
    collision = machine.evaluate(
        collision_input, {}, {}, ErrorContext("when"), ResultType.BOOL
    )

    assert accepted is True
    assert isinstance(collision, EvaluationError)
    assert collision.kind is ErrorKind.INTERNAL_FAULT


def test_cel_bool_and_string_results_preserve_exact_values() -> None:
    from kora.cel_eval.errors import ErrorContext
    from kora.cel_eval.evaluate import Machine, ResultType

    machine = Machine(load_binding(), clock)

    boolean = machine.evaluate(
        expression("payload"),
        {"payload": False},
        {"payload": BOOL},
        ErrorContext("when"),
        ResultType.BOOL,
    )
    text = machine.evaluate(
        expression('"reason"'), {}, {}, ErrorContext("reason"), ResultType.STRING
    )

    assert boolean is False
    assert text == "reason"


@pytest.mark.parametrize("fake", [False, True], ids=["native", "protocol-fake"])
@pytest.mark.parametrize(("source", "expected"), [("true", True), ("false", False)])
def test_cel_binding_protocol_conformance(
    fake: bool, source: str, expected: bool
) -> None:
    from kora.cel_eval.errors import ErrorContext
    from kora.cel_eval.evaluate import Machine, ResultType
    from tests.cel_eval.thread_scenarios import ScriptedBinding

    binding = ScriptedBinding().binding() if fake else load_binding()
    machine = Machine(binding, clock)

    result = machine.evaluate(
        expression(source), {}, {}, ErrorContext("when"), ResultType.BOOL
    )

    assert result is expected


def typed_expression(source: str, environment: Environment) -> CheckedExpression:
    checked = check_typed(source, environment)
    assert isinstance(checked, CheckedExpression)
    return checked


FR_2_5: str = (
    "inputs.review.verdict == 'approved' && "
    "inputs.bench.cpu < inputs.baseline.cpu * 0.85"
)
BENCH: JsonValue = {
    "type": "object",
    "properties": {"cpu": {"type": "number"}},
    "required": ["cpu"],
}
REVIEW: JsonValue = {
    "type": "object",
    "properties": {"verdict": {"enum": ["approved", "rejected"]}},
    "required": ["verdict"],
}


@pytest.mark.parametrize(("cpu", "expected"), [(80, True), (85, False), (90, False)])
def test_cel_schema_typed_fr_2_5_example_evaluates(cpu: int, expected: bool) -> None:
    from kora.cel_eval.errors import ErrorContext
    from kora.cel_eval.evaluate import Machine, ResultType

    schemas = SchemaTypes(no_documents)
    bench = schemas.document(BENCH)
    inputs = Record(
        {
            "review": RecordField(schemas.document(REVIEW), True),
            "bench": RecordField(bench, True),
            "baseline": RecordField(bench, True),
        }
    )
    environment = Environment(
        {"inputs": Variable(inputs)}, ResultKind.BOOL, frozenset({"inputs"})
    )
    values = {
        "inputs": {
            "review": {"verdict": "approved"},
            "bench": {"cpu": cpu},
            "baseline": {"cpu": 100},
        }
    }
    machine = Machine(load_binding(), clock)
    checked = typed_expression(FR_2_5, environment)

    result = machine.evaluate(
        checked, values, {"inputs": inputs}, ErrorContext("when"), ResultType.BOOL
    )

    assert result is expected
