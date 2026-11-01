import gc
from typing import Literal

import pytest

from kora.ir import validate_type_schemas
from kora.ir.json import JsonObject, JsonValue
from kora.ir.structured_schema_assertions import (
    max_items_matches,
    max_properties_matches,
    min_items_matches,
    min_properties_matches,
)
from kora.ir.structured_schema_cursors import ArenaCursor, NativeCursor
from kora.ir.structured_schema_evaluation_policy import PARAMETERS, EvaluationRefused
from kora.ir.structured_schema_evaluation_work import EvaluationMeter
from kora.ir.structured_schema_input import SchemaNode
from tests.ir.test_structured_schema_assertions import (
    LeafAssertion,
    Outcome,
    assertion_cursors,
    consumed,
)
from tests.ir.test_structured_schema_equality import tracked_equality_owners
from tests.ir.test_structured_schema_evaluation_work import (
    Dimension,
    counters,
    remaining_meter,
)
from tests.ir.type_schema_cases import document_for

ContainerKind = Literal["array", "object"]
COUNT_ASSERTIONS: tuple[tuple[LeafAssertion, ContainerKind], ...] = (
    (min_items_matches, "array"),
    (max_items_matches, "array"),
    (min_properties_matches, "object"),
    (max_properties_matches, "object"),
)
MINIMUM_ASSERTIONS: tuple[LeafAssertion, ...] = (
    min_items_matches,
    min_properties_matches,
)


@pytest.mark.parametrize(
    "keyword", ["minItems", "maxItems", "minProperties", "maxProperties"]
)
@pytest.mark.parametrize(
    ("bound", "accepted"),
    [
        pytest.param(0, True, id="zero"),
        pytest.param(1.0, True, id="integral-float"),
        pytest.param(-0.0, True, id="signed-zero"),
        pytest.param(True, False, id="boolean"),
        pytest.param(-1, False, id="negative"),
        pytest.param(1.5, False, id="fractional"),
    ],
)
def test_container_count_bounds_preserve_structural_domain(
    keyword: str, bound: JsonValue, accepted: bool
) -> None:
    schema: JsonObject = {keyword: bound}
    document = document_for(schema)

    result = validate_type_schemas(document)

    assert result.schemas_valid is accepted
    assert all(error.code == "IR022" for error in result.diagnostics.errors)


@pytest.mark.parametrize(("operation", "kind"), COUNT_ASSERTIONS)
@pytest.mark.parametrize(
    ("array", "mapping", "bound", "minimum", "maximum"),
    [
        pytest.param([], {}, 0, True, True, id="empty-zero"),
        pytest.param([], {}, 1, False, True, id="empty-positive"),
        pytest.param([False], {"a": False}, 0, True, False, id="one-above-zero"),
        pytest.param([False], {"a": False}, 1, True, True, id="one-exact"),
        pytest.param(
            [1, 1], {"\x00": 1, "\U0001f600": 1}, 2, True, True, id="duplicates"
        ),
        pytest.param(
            [None, False, 0],
            {"a": None, "b": False, "c": 0},
            3,
            True,
            True,
            id="values",
        ),
        pytest.param([1, 2, 3], {"a": 1, "b": 2, "c": 3}, 2, True, False, id="above"),
        pytest.param([1, 2, 3], {"a": 1, "b": 2, "c": 3}, 4, False, True, id="below"),
        pytest.param([None], {"a": None}, 1.0, True, True, id="integral-float"),
        pytest.param([], {}, -0.0, True, True, id="negative-floating-zero"),
        pytest.param([None], {"a": None}, 1e23, False, True, id="large-integral-float"),
        pytest.param(
            [None], {"a": None}, (1 << 14_000) - 1, False, True, id="large-integer"
        ),
    ],
)
def test_container_count_assertions_use_native_cardinality_and_inclusive_bounds(
    operation: LeafAssertion,
    kind: ContainerKind,
    array: list[JsonValue],
    mapping: JsonObject,
    bound: int | float,
    minimum: bool,
    maximum: bool,
) -> None:
    value: JsonValue = array if kind == "array" else mapping
    meter = EvaluationMeter()
    instance, argument = assertion_cursors(value, bound, meter)
    original_container = instance.value
    original_nodes = argument.nodes
    original_bound = argument.nodes[argument.node].value
    original_spelling = argument.nodes[argument.node].spelling

    matched = operation(instance, argument, meter)

    assert matched is (minimum if operation in MINIMUM_ASSERTIONS else maximum)
    assert instance.value is original_container
    assert instance.value == value
    assert argument.nodes is original_nodes
    assert argument.nodes[argument.node].value is original_bound
    assert type(original_bound) is type(bound)
    assert argument.nodes[argument.node].spelling == original_spelling


@pytest.mark.parametrize(("operation", "kind"), COUNT_ASSERTIONS)
@pytest.mark.parametrize(
    ("value", "kind_work"), [(None, 5), (False, 6), (1, 7), ("x", 9), ("opposite", 0)]
)
def test_container_count_assertions_pay_applicability_and_guard_order(
    operation: LeafAssertion, kind: ContainerKind, value: object, kind_work: int
) -> None:
    if value == "opposite":
        value = {} if kind == "array" else []
        kind_work = 11 if kind == "array" else 10
    meter = EvaluationMeter()
    instance, admitted = assertion_cursors(value, None, meter)
    argument = ArenaCursor(admitted.nodes, len(admitted.nodes))
    before = counters(meter)

    matched = operation(instance, argument, meter)

    assert matched
    assert consumed(meter, before) == (kind_work + 2, 0, 0)


@pytest.mark.parametrize(("operation", "kind"), COUNT_ASSERTIONS)
@pytest.mark.parametrize("bound", [None, False, "1"])
def test_container_count_visited_guard_precedes_count_and_conversion(
    operation: LeafAssertion, kind: ContainerKind, bound: object
) -> None:
    value: JsonValue = [None] if kind == "array" else {"a": None}
    meter = EvaluationMeter()
    instance, argument = assertion_cursors(value, bound, meter)
    before = counters(meter)
    argument_work = 12 if bound is None else 13 if bound is False else 16

    with pytest.raises(EvaluationRefused) as rejected:
        operation(instance, argument, meter)

    assert rejected.value.reason == "unsupported_schema"
    assert consumed(meter, before) == (12 + int(kind == "object") + argument_work, 0, 0)


@pytest.mark.parametrize(("operation", "kind"), COUNT_ASSERTIONS)
@pytest.mark.parametrize("short", [False, True], ids=["exact", "one-short"])
def test_container_count_guard_branch_is_paid_before_unsupported_refusal(
    operation: LeafAssertion, kind: ContainerKind, short: bool
) -> None:
    value: JsonValue = [None] if kind == "array" else {"a": None}
    required = 5 + 25 + int(kind == "object")
    meter = remaining_meter("work", required - int(short))
    before = counters(meter)
    instance, argument = assertion_cursors(value, False, meter)

    with pytest.raises(EvaluationRefused) as rejected:
        operation(instance, argument, meter)

    assert rejected.value.reason == (
        "evaluation_exhausted" if short else "unsupported_schema"
    )
    assert consumed(meter, before) == (required - int(short), 0, 2)


@pytest.mark.parametrize(("operation", "kind"), COUNT_ASSERTIONS)
@pytest.mark.parametrize("short", [False, True], ids=["count-paid", "one-short"])
def test_native_count_reservation_is_atomic_before_field_and_length_access(
    operation: LeafAssertion, kind: ContainerKind, short: bool
) -> None:
    value: JsonValue = [None] if kind == "array" else {"a": None}
    prefix = 36 + int(kind == "object")
    meter = remaining_meter("work", 5 + prefix - int(short))
    before = counters(meter)
    instance, argument = assertion_cursors(value, 1, meter)

    with pytest.raises(EvaluationRefused) as rejected:
        operation(instance, argument, meter)

    assert rejected.value.reason == "evaluation_exhausted"
    assert consumed(meter, before) == (5 + prefix - 2 * int(short), 0, 2)


@pytest.mark.parametrize(("operation", "kind"), COUNT_ASSERTIONS)
@pytest.mark.parametrize("short", [False, True], ids=["exact", "one-short"])
@pytest.mark.parametrize(
    ("bound", "dimension", "call_work", "byte_count", "entries", "short_paid"),
    [
        pytest.param(1, "work", 76, 0, 2, (80, 0, 4), id="int-work"),
        pytest.param(1, "entries", 76, 0, 2, (50, 0, 3), id="int-entries"),
        pytest.param(1.0, "work", 4225, 51, 4, (4229, 51, 6), id="float-work"),
        pytest.param(1.0, "bytes", 4225, 51, 4, (4213, 50, 6), id="float-bytes"),
        pytest.param(1.0, "entries", 4225, 51, 4, (4192, 50, 5), id="float-entries"),
    ],
)
def test_container_count_assertions_reserve_before_actions(
    operation: LeafAssertion,
    kind: ContainerKind,
    bound: int | float,
    dimension: Dimension,
    call_work: int,
    byte_count: int,
    entries: int,
    short_paid: tuple[int, int, int],
    short: bool,
) -> None:
    extra_work = int(kind == "object")
    required = (
        5 + call_work + extra_work
        if dimension == "work"
        else byte_count
        if dimension == "bytes"
        else 2 + entries
    )
    meter = remaining_meter(dimension, required - int(short))
    before = counters(meter)
    value: JsonValue = [None] if kind == "array" else {"a": None}
    instance, argument = assertion_cursors(value, bound, meter)

    if short:
        with pytest.raises(EvaluationRefused) as rejected:
            operation(instance, argument, meter)

        assert rejected.value.reason == "evaluation_exhausted"
        assert consumed(meter, before) == (
            short_paid[0] + extra_work,
            short_paid[1],
            short_paid[2],
        )
        return
    matched = operation(instance, argument, meter)

    assert matched
    assert consumed(meter, before) == (
        5 + call_work + extra_work,
        byte_count,
        2 + entries,
    )


@pytest.mark.parametrize("kind", ["array", "object"])
def test_container_count_assertions_use_no_index_or_per_item_state(
    kind: ContainerKind,
) -> None:
    count = PARAMETERS["max_input_nodes"] + 1
    value: JsonValue
    if kind == "array":
        items: list[JsonValue] = [None] * count
        value = items
    else:
        value = {f"field-{i}": None for i in range(count)}
    meter = remaining_meter("entries", 6)
    instance, argument = assertion_cursors(value, count, meter)
    before = counters(meter)
    minimum = min_items_matches if kind == "array" else min_properties_matches
    maximum = max_items_matches if kind == "array" else max_properties_matches

    lower_matched = minimum(instance, argument, meter)
    upper_matched = maximum(instance, argument, meter)

    assert lower_matched
    assert upper_matched
    assert consumed(meter, before) == (2 * (76 + int(kind == "object")), 0, 4)


@pytest.mark.parametrize(("operation", "kind"), COUNT_ASSERTIONS)
def test_container_count_calls_share_the_meter_without_refunds_or_cache(
    operation: LeafAssertion, kind: ContainerKind
) -> None:
    meter = EvaluationMeter()
    value: JsonValue = [None] if kind == "array" else {"a": None}
    instance, argument = assertion_cursors(value, 1.0, meter)
    before = counters(meter)
    call_work = 4225 + int(kind == "object")

    first = operation(instance, argument, meter)
    after_first = counters(meter)
    second = operation(instance, argument, meter)

    assert first
    assert second
    assert consumed(meter, before) == (2 * call_work, 2 * 51, 2 * 4)
    assert consumed(meter, after_first) == (call_work, 51, 4)


@pytest.mark.parametrize(("operation", "kind"), COUNT_ASSERTIONS)
@pytest.mark.parametrize("form", ["native-subclass", "arena-subclass"])
def test_container_count_assertions_reject_callbacks(
    operation: LeafAssertion,
    kind: ContainerKind,
    form: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[str] = []

    class AuthoredMeta(type):
        def __eq__(cls, other: object) -> bool:
            calls.append("class equality")
            raise AssertionError("authored class equality invoked")

        def __hash__(cls) -> int:
            calls.append("class hash")
            raise AssertionError("authored class hashing invoked")

    class AuthoredArray(list[object], metaclass=AuthoredMeta):
        def __len__(self) -> int:
            calls.append("array length")
            raise AssertionError("private-count-canary")

    class AuthoredObject(dict[str, object], metaclass=AuthoredMeta):
        def __len__(self) -> int:
            calls.append("object length")
            raise AssertionError("private-count-canary")

    class AuthoredFloat(float, metaclass=AuthoredMeta):
        def __repr__(self) -> str:
            calls.append("float repr")
            raise AssertionError("private-count-canary")

    meter = EvaluationMeter()
    value: JsonValue = [None] if kind == "array" else {"a": None}
    instance, argument = assertion_cursors(value, 1, meter)
    if form == "native-subclass":
        hostile = AuthoredArray([None]) if kind == "array" else AuthoredObject(a=None)
        instance = NativeCursor(hostile)
    else:
        argument = ArenaCursor((SchemaNode("scalar", AuthoredFloat(1.0)),), 0)

    with pytest.raises(EvaluationRefused) as rejected:
        operation(instance, argument, meter)
    captured = capsys.readouterr()

    assert calls == []
    assert str(rejected.value) == "unsupported_schema"
    assert rejected.value.__cause__ is None
    assert "private-count-canary" not in repr(rejected.value)
    assert captured.out == captured.err == ""


def owned_container_assertion(
    operation: LeafAssertion, kind: ContainerKind, outcome: Outcome
) -> bool:
    meter = remaining_meter("bytes", 50) if outcome == "refusal" else EvaluationMeter()
    bound = 1.0
    if outcome == "false":
        bound = 2.0 if operation in MINIMUM_ASSERTIONS else 0.0
    value: JsonValue = [None] if kind == "array" else {"a": None}
    instance, argument = assertion_cursors(value, bound, meter)
    if outcome == "unexpected":
        argument = ArenaCursor(argument.nodes, len(argument.nodes))
    return operation(instance, argument, meter)


@pytest.mark.parametrize(
    ("operation", "kind", "outcome"),
    [
        (min_items_matches, "array", "true"),
        (max_items_matches, "array", "false"),
        (min_items_matches, "array", "refusal"),
        (max_items_matches, "array", "unexpected"),
        (min_properties_matches, "object", "true"),
        (max_properties_matches, "object", "false"),
        (min_properties_matches, "object", "refusal"),
        (max_properties_matches, "object", "unexpected"),
    ],
)
def test_container_count_assertions_release_state(
    operation: LeafAssertion, kind: ContainerKind, outcome: Outcome
) -> None:
    gc.collect()
    before = tracked_equality_owners()

    if outcome in ("true", "false"):
        matched = owned_container_assertion(operation, kind, outcome)
        gc.collect()
        remaining = tracked_equality_owners()

        assert matched is (outcome == "true")
        assert remaining == before
        return
    expected = EvaluationRefused if outcome == "refusal" else IndexError
    with pytest.raises(expected) as rejected:
        owned_container_assertion(operation, kind, outcome)
    gc.collect()
    held = tracked_equality_owners() - before
    if outcome == "refusal":
        assert str(rejected.value) == "evaluation_exhausted"
    del rejected
    gc.collect()
    remaining = tracked_equality_owners()

    assert held
    assert remaining == before
