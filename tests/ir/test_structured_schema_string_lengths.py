import gc

import pytest

from kora.ir import validate_type_schemas
from kora.ir.json import JsonObject, JsonValue
from kora.ir.structured_output import StructuredOutput, checked_value
from kora.ir.structured_schema_assertions import (
    max_length_matches,
    min_length_matches,
)
from kora.ir.structured_schema_cursors import (
    ArenaCursor,
    NativeCursor,
    arena_cursor,
    native_cursor,
)
from kora.ir.structured_schema_evaluation_policy import EvaluationRefused
from kora.ir.structured_schema_evaluation_work import EvaluationMeter
from kora.ir.structured_schema_input import SchemaNode
from kora.ir.structured_schema_work import PreparationMeter
from tests.ir.test_structured_schema_assertions import (
    LeafAssertion,
    Outcome,
    assertion_cursors,
    consumed,
)
from tests.ir.test_structured_schema_equality import (
    frozen_literal,
    tracked_equality_owners,
)
from tests.ir.test_structured_schema_evaluation_work import (
    Dimension,
    counters,
    remaining_meter,
)
from tests.ir.type_schema_cases import document_for

LENGTH_ASSERTIONS: tuple[LeafAssertion, ...] = (
    min_length_matches,
    max_length_matches,
)


@pytest.mark.parametrize("keyword", ["minLength", "maxLength"])
@pytest.mark.parametrize(
    ("bound", "accepted"),
    [
        pytest.param(0, True, id="zero"),
        pytest.param(1, True, id="integer"),
        pytest.param(1.0, True, id="integral-float"),
        pytest.param(-0.0, True, id="signed-floating-zero"),
        pytest.param(1e23, True, id="large-integral-float"),
        pytest.param((1 << 14_000) - 1, True, id="large-integer"),
        pytest.param(True, False, id="true"),
        pytest.param(False, False, id="false"),
        pytest.param(-1, False, id="negative-integer"),
        pytest.param(-1.0, False, id="negative-integral-float"),
        pytest.param(1.5, False, id="fractional-float"),
        pytest.param(-0.1, False, id="negative-fraction"),
    ],
)
def test_string_length_bounds_preserve_structural_count_domain(
    keyword: str, bound: JsonValue, accepted: bool
) -> None:
    schema: JsonObject = {keyword: bound}
    document = document_for(schema)

    result = validate_type_schemas(document)

    assert result.schemas_valid is accepted
    assert all(error.code == "IR022" for error in result.diagnostics.errors)


@pytest.mark.parametrize("operation", LENGTH_ASSERTIONS)
@pytest.mark.parametrize(
    ("text", "bound", "minimum", "maximum"),
    [
        pytest.param("", 0, True, True, id="empty-zero"),
        pytest.param("", 1, False, True, id="empty-positive"),
        pytest.param("\x00", 0, True, False, id="nul"),
        pytest.param("\U0001f600", 1, True, True, id="supplementary-codepoint"),
        pytest.param("e\u0301", 1, True, False, id="combining-is-two-codepoints"),
        pytest.param("e\u0301", 2, True, True, id="combining-exact"),
        pytest.param("\u00e9", 2, False, True, id="no-normalization"),
        pytest.param("\x00\U0001f600e\u0301", 4, True, True, id="mixed-codepoints"),
        pytest.param("x", 1.0, True, True, id="integral-float"),
        pytest.param("", -0.0, True, True, id="negative-floating-zero"),
        pytest.param("x", 1e23, False, True, id="large-integral-float"),
        pytest.param("x", (1 << 14_000) - 1, False, True, id="large-integer"),
    ],
)
def test_string_length_assertions_use_codepoint_length_and_inclusive_bounds(
    operation: LeafAssertion,
    text: str,
    bound: int | float,
    minimum: bool,
    maximum: bool,
) -> None:
    tree = checked_value(StructuredOutput(text))
    literal = frozen_literal(bound)
    original_nodes = literal.nodes
    original_bound = literal.nodes[literal.root].value
    original_bytes = literal.encode(PreparationMeter())
    meter = EvaluationMeter()
    instance = native_cursor(tree, meter)
    argument = arena_cursor(literal.nodes, literal.root, meter)

    matched = operation(instance, argument, meter)

    assert matched is (minimum if operation is min_length_matches else maximum)
    assert argument.nodes is original_nodes
    assert literal.nodes[literal.root].value is original_bound
    assert type(original_bound) is type(bound)
    assert literal.encode(PreparationMeter()) == original_bytes
    assert instance.value is tree


@pytest.mark.parametrize("operation", LENGTH_ASSERTIONS)
@pytest.mark.parametrize(
    ("value", "kind_work"),
    [(None, 5), (False, 6), (1, 7), (1.0, 8), ([], 10), ({}, 11)],
)
def test_string_length_assertions_pay_applicability_and_visited_guard_order(
    operation: LeafAssertion, value: object, kind_work: int
) -> None:
    meter = EvaluationMeter()
    instance, admitted_argument = assertion_cursors(value, None, meter)
    argument = ArenaCursor(admitted_argument.nodes, len(admitted_argument.nodes))
    before = counters(meter)

    matched = operation(instance, argument, meter)

    assert matched
    assert consumed(meter, before) == (kind_work + 2, 0, 0)


@pytest.mark.parametrize("operation", LENGTH_ASSERTIONS)
@pytest.mark.parametrize(
    ("bound", "kind_work"), [(None, 12), (False, 13), ("1", 16), ([], 11), ({}, 13)]
)
def test_string_length_visited_nonnumeric_guard_precedes_length_and_conversion(
    operation: LeafAssertion, bound: object, kind_work: int
) -> None:
    meter = EvaluationMeter()
    instance, argument = assertion_cursors("x", bound, meter)
    before = counters(meter)

    with pytest.raises(EvaluationRefused) as rejected:
        operation(instance, argument, meter)

    assert rejected.value.reason == "unsupported_schema"
    assert consumed(meter, before) == (11 + kind_work, 0, 0)


@pytest.mark.parametrize("operation", LENGTH_ASSERTIONS)
@pytest.mark.parametrize("short", [False, True], ids=["exact", "one-short"])
def test_string_length_argument_guard_is_paid_before_unsupported_refusal(
    operation: LeafAssertion, short: bool
) -> None:
    meter = remaining_meter("work", 5 + 24 - int(short))
    before = counters(meter)
    instance, argument = assertion_cursors("x", False, meter)

    with pytest.raises(EvaluationRefused) as rejected:
        operation(instance, argument, meter)

    assert rejected.value.reason == (
        "evaluation_exhausted" if short else "unsupported_schema"
    )
    assert consumed(meter, before) == (5 + 24 - int(short), 0, 2)


@pytest.mark.parametrize("operation", LENGTH_ASSERTIONS)
@pytest.mark.parametrize("short", [False, True], ids=["exact", "one-short"])
@pytest.mark.parametrize(
    ("bound", "dimension", "call_work", "byte_count", "entries", "short_paid"),
    [
        pytest.param(1, "work", 75, 0, 2, (79, 0, 4), id="int-work"),
        pytest.param(1, "entries", 75, 0, 2, (49, 0, 3), id="int-entries"),
        pytest.param(1.0, "work", 4224, 51, 4, (4228, 51, 6), id="float-work"),
        pytest.param(1.0, "bytes", 4224, 51, 4, (4212, 50, 6), id="float-bytes"),
        pytest.param(1.0, "entries", 4224, 51, 4, (4191, 50, 5), id="float-entries"),
    ],
)
def test_string_length_assertions_reserve_shared_capacity_before_actions(
    operation: LeafAssertion,
    bound: int | float,
    dimension: Dimension,
    call_work: int,
    byte_count: int,
    entries: int,
    short_paid: tuple[int, int, int],
    short: bool,
) -> None:
    required = (
        5 + call_work
        if dimension == "work"
        else byte_count
        if dimension == "bytes"
        else 2 + entries
    )
    meter = remaining_meter(dimension, required - int(short))
    before = counters(meter)
    instance, argument = assertion_cursors("x", bound, meter)

    if short:
        with pytest.raises(EvaluationRefused) as rejected:
            operation(instance, argument, meter)

        assert rejected.value.reason == "evaluation_exhausted"
        assert consumed(meter, before) == short_paid
        return
    matched = operation(instance, argument, meter)

    assert matched
    assert consumed(meter, before) == (5 + call_work, byte_count, 2 + entries)


@pytest.mark.parametrize("operation", LENGTH_ASSERTIONS)
def test_string_length_calls_share_the_meter_without_refunds_or_conversion_cache(
    operation: LeafAssertion,
) -> None:
    meter = EvaluationMeter()
    instance, argument = assertion_cursors("x", 1.0, meter)
    before = counters(meter)

    first = operation(instance, argument, meter)
    after_first = counters(meter)
    second = operation(instance, argument, meter)

    assert first
    assert second
    assert consumed(meter, before) == (2 * 4224, 2 * 51, 2 * 4)
    assert consumed(meter, after_first) == (4224, 51, 4)


@pytest.mark.parametrize("operation", LENGTH_ASSERTIONS)
@pytest.mark.parametrize("form", ["native-subclass", "arena-subclass"])
def test_string_length_assertions_reject_hostile_forms_without_callbacks(
    operation: LeafAssertion, form: str, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[str] = []

    class AuthoredMeta(type):
        def __eq__(cls, other: object) -> bool:
            calls.append("class equality")
            raise AssertionError("authored metaclass equality invoked")

        def __hash__(cls) -> int:
            calls.append("class hash")
            raise AssertionError("authored metaclass hash invoked")

    class AuthoredString(str, metaclass=AuthoredMeta):
        def __len__(self) -> int:
            calls.append("length")
            raise AssertionError("private-length-canary")

    class AuthoredFloat(float, metaclass=AuthoredMeta):
        def __repr__(self) -> str:
            calls.append("repr")
            raise AssertionError("private-length-canary")

    meter = EvaluationMeter()
    instance, argument = assertion_cursors("x", 1, meter)
    if form == "native-subclass":
        instance = NativeCursor(AuthoredString("private-length-canary"))
    else:
        argument = ArenaCursor((SchemaNode("scalar", AuthoredFloat(1.0)),), 0)

    with pytest.raises(EvaluationRefused) as rejected:
        operation(instance, argument, meter)
    captured = capsys.readouterr()

    assert calls == []
    assert str(rejected.value) == "unsupported_schema"
    assert rejected.value.__cause__ is None
    assert "private-length-canary" not in repr(rejected.value)
    assert captured.out == captured.err == ""


def owned_length_assertion(operation: LeafAssertion, outcome: Outcome) -> bool:
    meter = remaining_meter("bytes", 50) if outcome == "refusal" else EvaluationMeter()
    bound = 1.0
    if outcome == "false":
        bound = 2.0 if operation is min_length_matches else 0.0
    instance, argument = assertion_cursors("x", bound, meter)
    if outcome == "unexpected":
        argument = ArenaCursor(argument.nodes, len(argument.nodes))
    return operation(instance, argument, meter)


@pytest.mark.parametrize("operation", LENGTH_ASSERTIONS)
@pytest.mark.parametrize("outcome", ["true", "false", "refusal", "unexpected"])
def test_string_length_assertions_release_state_on_every_exit(
    operation: LeafAssertion, outcome: Outcome
) -> None:
    gc.collect()
    before = tracked_equality_owners()

    if outcome in ("true", "false"):
        matched = owned_length_assertion(operation, outcome)
        gc.collect()
        remaining = tracked_equality_owners()

        assert matched is (outcome == "true")
        assert remaining == before
        return
    expected = EvaluationRefused if outcome == "refusal" else IndexError
    with pytest.raises(expected) as rejected:
        owned_length_assertion(operation, outcome)
    gc.collect()
    held = tracked_equality_owners() - before
    if outcome == "refusal":
        assert str(rejected.value) == "evaluation_exhausted"
    del rejected
    gc.collect()
    remaining = tracked_equality_owners()

    assert held
    assert remaining == before
