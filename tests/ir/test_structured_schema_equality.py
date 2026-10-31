import gc
import weakref
from collections.abc import Callable
from dataclasses import replace
from typing import Literal, cast

import pytest

from kora.ir import structured_schema_equality
from kora.ir.json import JsonValue
from kora.ir.structured_output import (
    StructuredOutput,
    StructuredOutputValidationError,
    canonical_structured_output,
    checked_value,
    structured_output_digest,
)
from kora.ir.structured_schema_cursors import (
    ArenaCursor,
    NativeCursor,
    NativeObjectIndex,
    arena_cursor,
    build_native_object_index,
    native_cursor,
)
from kora.ir.structured_schema_equality import json_equal
from kora.ir.structured_schema_evaluation_policy import PARAMETERS, EvaluationRefused
from kora.ir.structured_schema_evaluation_work import EvaluationMeter
from kora.ir.structured_schema_input import FrozenSchemaArena, SchemaArena
from kora.ir.structured_schema_numbers import DecimalNumber, numeric_equal
from kora.ir.structured_schema_work import PreparationMeter
from tests.ir.structured_output_cases import nested
from tests.ir.test_structured_schema_evaluation_work import counters, remaining_meter

Operand = Literal["native", "arena"]
TENTHS_CONVERSION_WORK: int = 4 + (2 + 4096) + (1 + 3) + (1 + 24) + 1 + (1 + 2 * 2) + 9
TENTHS_EQUALITY_WORK: int = 2 * TENTHS_CONVERSION_WORK + 29
FLOAT_PAIR_ADMINISTRATION_WORK: int = 2 + 12 + 4 + 2 + 8 + 8 + 1 + 1 + 3 + 3 + 2 + 1 + 1


def frozen_literal(value: object) -> FrozenSchemaArena:
    arena = SchemaArena(PreparationMeter())
    root = arena.snapshot(value)
    return arena.freeze(root)


def pair_cursors(
    values: list[JsonValue], operand: Operand, meter: EvaluationMeter
) -> tuple[NativeCursor, NativeCursor | ArenaCursor]:
    left = native_cursor(values[0], meter)
    if operand == "native":
        return left, native_cursor(values[1], meter)
    literal = frozen_literal(values[1])
    return left, arena_cursor(literal.nodes, literal.root, meter)


@pytest.mark.parametrize("operand", ["native", "arena"])
@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        pytest.param(None, None, True, id="null-root"),
        pytest.param(True, True, True, id="boolean-root"),
        pytest.param(True, False, False, id="unequal-booleans"),
        pytest.param(True, 1, False, id="boolean-versus-number"),
        pytest.param(1, 1.0, True, id="exact-numerical-cross-kind"),
        pytest.param(-0.0, 0, True, id="signed-zero"),
        pytest.param(10**23, 1e23, True, id="exact-decimal-spelling"),
        pytest.param(
            int(1e23), 1e23, False, id="binary-rounding-is-not-decimal-equality"
        ),
        pytest.param(0.3, 0.3, True, id="decimal-fraction"),
        pytest.param("nul\x00\U0001f600", "nul\x00\U0001f600", True, id="escaped-nul"),
        pytest.param("\u00e9", "e\u0301", False, id="no-unicode-normalization"),
        pytest.param([], [], True, id="empty-array-root"),
        pytest.param({}, {}, True, id="empty-object-root"),
        pytest.param([None, False, 1], [None, False, 1.0], True, id="array-root"),
        pytest.param([1, 2], [2, 1], False, id="array-index-order"),
        pytest.param(
            {"z": [], "a": None}, {"a": None, "z": []}, True, id="object-order"
        ),
        pytest.param(
            {"\U0001f600": [{"\x00": 1}], "\uffff": True},
            {"\uffff": True, "\U0001f600": [{"\x00": 1.0}]},
            True,
            id="nested-codepoint-order",
        ),
        pytest.param(None, [], False, id="unlike-kinds"),
    ],
)
def test_recursive_equality_uses_one_admitted_tree_and_original_arena(
    left: object, right: object, expected: bool, operand: Operand
) -> None:
    model = StructuredOutput([left, right])
    original_bytes = canonical_structured_output(model)
    original_digest = structured_output_digest(model)
    values = cast("list[JsonValue]", checked_value(model))
    literal = frozen_literal(values[1])
    original_nodes = literal.nodes
    literal_bytes = literal.encode(PreparationMeter())
    meter = EvaluationMeter()
    first = native_cursor(values[0], meter)
    second = (
        native_cursor(values[1], meter)
        if operand == "native"
        else arena_cursor(literal.nodes, literal.root, meter)
    )

    same = json_equal(first, second, meter)

    assert same is expected
    assert canonical_structured_output(model) == original_bytes
    assert structured_output_digest(model) == original_digest
    assert literal.nodes is original_nodes
    assert literal.encode(PreparationMeter()) == literal_bytes
    assert first.value is values[0]
    assert repr(first) == "NativeCursor()"
    assert type(values[0]) is type(left)
    if type(second) is ArenaCursor:
        assert second.nodes is original_nodes
        assert repr(second) == "ArenaCursor()"


@pytest.mark.parametrize(
    ("left", "right", "operand", "call_work", "call_entries"),
    [
        (None, None, "native", 37, 3),
        (True, True, "native", 47, 3),
        ("a", "a", "native", 55, 3),
        ([], [], "native", 59, 3),
        ({}, {}, "native", 61, 3),
        ([], [None], "native", 55, 3),
        ({}, {"a": None}, "native", 57, 3),
        ([None], [None], "native", 132, 7),
        ({"a": None}, {"a": None}, "native", 208, 7 + 2 * (3 + 4)),
        (None, None, "arena", 44, 3),
        (True, True, "arena", 57, 3),
        ("a", "a", "arena", 65, 3),
        ([], [], "arena", 65, 3),
        ({}, {}, "arena", 68, 3),
        ([None], [None], "arena", 150, 7),
        ({"a": None}, {"a": None}, "arena", 215, 7 + (3 + 4)),
    ],
)
def test_recursive_equality_charges_each_cursor_frame_and_visited_action(
    left: object,
    right: object,
    operand: Operand,
    call_work: int,
    call_entries: int,
) -> None:
    values = cast("list[JsonValue]", checked_value(StructuredOutput([left, right])))
    meter = EvaluationMeter()
    first, second = pair_cursors(values, operand, meter)
    before = counters(meter)

    same = json_equal(first, second, meter)

    assert same is (type(left) is type(right) and left == right)
    assert before == (4 if operand == "native" else 5, 0, 2)
    assert counters(meter) == (before[0] + call_work, 0, before[2] + call_entries)


@pytest.mark.parametrize("dimension", ["work", "bytes", "entries"])
@pytest.mark.parametrize("short", [False, True], ids=["exact", "one-short"])
def test_recursive_equality_shares_exact_work_byte_and_entry_admission(
    dimension: Literal["work", "bytes", "entries"], short: bool
) -> None:
    tree = checked_value(StructuredOutput(0.3))
    required_work = 2 + 2 + FLOAT_PAIR_ADMINISTRATION_WORK + TENTHS_EQUALITY_WORK
    required = {"work": required_work, "bytes": 100, "entries": 2 + 3 + 6}
    meter = remaining_meter(dimension, required[dimension] - int(short))
    before = counters(meter)
    first = native_cursor(tree, meter)
    second = native_cursor(tree, meter)

    if short:
        with pytest.raises(EvaluationRefused) as rejected:
            json_equal(first, second, meter)

        paid = {
            "work": (required_work - 1, 100, 11),
            "bytes": (4 + 44 + TENTHS_CONVERSION_WORK + 4132, 98, 10),
            "entries": (
                4 + 44 + TENTHS_CONVERSION_WORK + TENTHS_CONVERSION_WORK - 1,
                100,
                10,
            ),
        }[dimension]
        assert rejected.value.reason == "evaluation_exhausted"
        assert counters(meter) == tuple(
            initial + consumed for initial, consumed in zip(before, paid, strict=True)
        )
        return
    same = json_equal(first, second, meter)

    assert same
    assert counters(meter) == (
        before[0] + required_work,
        before[1] + 100,
        before[2] + 11,
    )


def test_repeated_equality_calls_share_one_monotone_meter_without_a_cache() -> None:
    tree = checked_value(StructuredOutput(0.3))
    call_work = FLOAT_PAIR_ADMINISTRATION_WORK + TENTHS_EQUALITY_WORK
    meter = remaining_meter("work", 4 + 2 * call_work)
    first = native_cursor(tree, meter)
    second = native_cursor(tree, meter)

    initial = json_equal(first, second, meter)
    repeated = json_equal(first, second, meter)
    exhausted = counters(meter)
    with pytest.raises(EvaluationRefused) as rejected:
        json_equal(first, second, meter)

    assert initial
    assert repeated
    assert exhausted == (PARAMETERS["max_evaluation_work"], 2 * 100, 2 + 2 * (3 + 6))
    assert counters(meter) == exhausted
    assert rejected.value.reason == "evaluation_exhausted"


@pytest.mark.parametrize("operand", ["native", "arena"])
@pytest.mark.parametrize("container", ["array", "object"])
def test_recursive_equality_checks_container_count_before_expensive_values(
    operand: Operand, container: str
) -> None:
    expensive = 1 << 13_999
    left: object = [expensive] if container == "array" else {"a": expensive}
    right: object = [5e-324, None] if container == "array" else {"a": 5e-324, "b": None}
    values = cast("list[JsonValue]", checked_value(StructuredOutput([left, right])))
    meter = remaining_meter("work", 1_000)
    first, second = pair_cursors(values, operand, meter)

    same = json_equal(first, second, meter)
    with pytest.raises(EvaluationRefused) as numerical_refusal:
        numeric_equal(expensive, 5e-324, EvaluationMeter())

    assert not same
    assert numerical_refusal.value.reason == "evaluation_exhausted"
    assert meter.auxiliary_entries == 2 + 1 + 2
    assert meter.auxiliary_bytes == 0


@pytest.mark.parametrize("operand", ["native", "arena"])
def test_recursive_equality_compares_every_object_key_before_any_value(
    operand: Operand,
) -> None:
    expensive = 1 << 13_999
    values = cast(
        "list[JsonValue]",
        checked_value(
            StructuredOutput([{"a": expensive, "b": None}, {"a": 5e-324, "c": None}])
        ),
    )
    meter = remaining_meter("work", 1_000)
    first, second = pair_cursors(values, operand, meter)

    same = json_equal(first, second, meter)

    assert not same
    assert meter.auxiliary_bytes == 0
    assert meter.auxiliary_entries == 5 + (2 if operand == "native" else 1) * (
        3 + 4 * 2
    )


@pytest.mark.parametrize("operand", ["native", "arena"])
@pytest.mark.parametrize("container", ["array", "object"])
def test_recursive_equality_stops_at_first_completed_value_mismatch(
    operand: Operand, container: str
) -> None:
    left: object = (
        [None, 1 << 13_999] if container == "array" else {"a": None, "b": 1 << 13_999}
    )
    right: object = (
        [False, 5e-324] if container == "array" else {"a": False, "b": 5e-324}
    )
    values = cast("list[JsonValue]", checked_value(StructuredOutput([left, right])))
    meter = remaining_meter("work", 1_000)
    first, second = pair_cursors(values, operand, meter)

    same = json_equal(first, second, meter)

    assert not same
    assert meter.auxiliary_bytes == 0


def test_recursive_equality_rebuilds_both_indexes_for_the_same_subtree() -> None:
    tree = checked_value(StructuredOutput({"a": {"b": None}}))
    meter = EvaluationMeter()
    cursor = native_cursor(tree, meter)
    before = counters(meter)

    first = json_equal(cursor, cursor, meter)
    once = counters(meter)
    second = json_equal(cursor, cursor, meter)
    twice = counters(meter)

    assert first
    assert second
    assert once[2] - before[2] == 1 + 3 * 2 + 2 * 2 + 4 * (3 + 4)
    assert twice == tuple(
        2 * after - initial for initial, after in zip(before, once, strict=True)
    )
    assert meter.auxiliary_bytes == 0


@pytest.mark.parametrize("depth", [16, 17], ids=["inclusive", "over"])
def test_native_container_depth_is_owned_by_real_model_admission(depth: int) -> None:
    candidate = nested(depth)

    if depth == 17:
        with pytest.raises(StructuredOutputValidationError) as rejected:
            StructuredOutput(candidate)

        assert rejected.value.violation.code == "SO003"
        return
    tree = checked_value(StructuredOutput(candidate))
    literal = frozen_literal(nested(depth + 1))
    meter = EvaluationMeter()
    first = native_cursor(tree, meter)
    second = arena_cursor(literal.nodes, literal.root, meter)

    assert not json_equal(first, second, meter)
    assert meter.auxiliary_entries == 1 + 4 * 17
    assert meter.auxiliary_bytes == 0


def test_dense_payload_length_mismatch_has_no_schema_node_limit_or_eager_state() -> (
    None
):
    count = PARAMETERS["max_input_nodes"] + 1
    tree = checked_value(StructuredOutput([None] * count))
    literal = frozen_literal([])
    meter = EvaluationMeter()
    first = native_cursor(tree, meter)
    second = arena_cursor(literal.nodes, literal.root, meter)
    before = counters(meter)

    same = json_equal(first, second, meter)

    assert not same
    assert len(cast("list[JsonValue]", tree)) == count
    assert before == (2 + 3, 0, 2)
    assert meter.auxiliary_entries == 5
    assert meter.auxiliary_bytes == 0


def test_invalid_visited_native_kind_refuses_without_authored_callbacks() -> None:
    calls: list[str] = []

    class AuthoredMeta(type):
        def __eq__(cls, other: object) -> bool:
            calls.append("metaclass equality")
            raise AssertionError("authored metaclass comparison invoked")

    class Authored(metaclass=AuthoredMeta):
        def __repr__(self) -> str:
            calls.append("representation")
            raise AssertionError("private-equality-value-canary")

    invalid = Authored()
    retained = weakref.ref(invalid)
    meter = EvaluationMeter()
    first = native_cursor(invalid, meter)
    second = native_cursor(None, meter)
    safe_handles = (repr(first), repr(second))

    with pytest.raises(EvaluationRefused) as rejected:
        json_equal(first, second, meter)
    reason = rejected.value.reason
    safe_failure = str(rejected.value), repr(rejected.value)
    no_chain = rejected.value.__cause__ is None and rejected.value.__context__ is None
    del first, invalid, rejected
    gc.collect()

    assert reason == "unsupported_schema"
    assert no_chain
    assert calls == []
    assert all(
        "private-equality-value-canary" not in text
        for text in safe_handles + safe_failure
    )
    assert retained() is None


def tracked_equality_owners() -> set[int]:
    frame_type = cast("type[object]", vars(structured_schema_equality)["_PairFrame"])
    owner_types = (
        NativeCursor,
        ArenaCursor,
        NativeObjectIndex,
        frame_type,
        DecimalNumber,
    )
    owner_type_ids = {id(kind) for kind in owner_types}
    return {
        id(value)
        for value in cast("list[object]", gc.get_objects())
        if id(type(value)) in owner_type_ids
    }


def test_owner_inventory_preserves_exact_classes_without_callbacks() -> None:
    calls: list[str] = []

    class AuthoredMeta(type):
        def __eq__(cls, other: object) -> bool:
            calls.append("equality")
            raise AssertionError("authored metaclass equality invoked")

        def __hash__(cls) -> int:
            calls.append("hash")
            raise AssertionError("authored metaclass hashing invoked")

    class AuthoredCursor(NativeCursor, metaclass=AuthoredMeta):
        pass

    class AuthoredObject(metaclass=AuthoredMeta):
        pass

    meter = EvaluationMeter()
    native = native_cursor(None, meter)
    literal = frozen_literal(None)
    arena = arena_cursor(literal.nodes, literal.root, meter)
    index = build_native_object_index({"a": False}, meter)
    frame_type = cast(
        "Callable[[NativeCursor, ArenaCursor], object]",
        vars(structured_schema_equality)["_PairFrame"],
    )
    owners: tuple[object, ...] = (
        native,
        arena,
        index,
        frame_type(native, arena),
        DecimalNumber(0, 0),
    )
    excluded: tuple[object, ...] = (AuthoredCursor(None), AuthoredObject())

    inventory = tracked_equality_owners()

    assert {id(owner) for owner in owners} <= inventory
    assert not {id(value) for value in excluded}.intersection(inventory)
    assert not calls


def owned_comparison(outcome: str) -> bool:
    values = cast(
        "list[JsonValue]",
        checked_value(
            StructuredOutput(
                [
                    {"private-equality-key-canary": [0.3]},
                    {
                        "private-equality-key-canary": [
                            0.3 if outcome != "mismatch" else False
                        ]
                    },
                ]
            )
        ),
    )
    meter = (
        remaining_meter("work", 1_000) if outcome == "refusal" else EvaluationMeter()
    )
    first = native_cursor(values[0], meter)
    literal = frozen_literal(values[1])
    if outcome == "unexpected":
        rows = list(literal.nodes)
        array_position = next(
            position for position, node in enumerate(rows) if node.kind == "array"
        )
        rows[array_position] = replace(rows[array_position], children=(len(rows),))
        literal = replace(literal, nodes=tuple(rows))
    second = arena_cursor(literal.nodes, literal.root, meter)
    return json_equal(first, second, meter)


@pytest.mark.parametrize("outcome", ["success", "mismatch"])
def test_recursive_equality_releases_all_operation_owners_on_completion(
    outcome: str,
) -> None:
    gc.collect()
    original = tracked_equality_owners()

    same = owned_comparison(outcome)
    gc.collect()
    remaining = tracked_equality_owners()

    assert same is (outcome == "success")
    assert remaining == original


@pytest.mark.parametrize("outcome", ["refusal", "unexpected"])
def test_recursive_equality_releases_owners_after_caller_discards_traceback(
    outcome: str,
) -> None:
    gc.collect()
    original = tracked_equality_owners()
    expected_error = EvaluationRefused if outcome == "refusal" else IndexError

    with pytest.raises(expected_error) as rejected:
        owned_comparison(outcome)
    gc.collect()
    held_by_caller = tracked_equality_owners() - original
    safe_owner_text = tuple(
        repr(value) for value in gc.get_objects() if id(value) in held_by_caller
    )
    safe_text = str(rejected.value), repr(rejected.value)
    if outcome == "refusal":
        assert str(rejected.value) == "evaluation_exhausted"
        assert rejected.value.__cause__ is None
        assert rejected.value.__context__ is None
    del rejected
    gc.collect()
    remaining = tracked_equality_owners()

    assert held_by_caller
    assert remaining == original
    assert all(
        "private-equality-key-canary" not in text
        for text in safe_text + safe_owner_text
    )
