import gc
import json
import math
import weakref
from collections.abc import Callable
from dataclasses import replace
from typing import Literal, cast

import pytest

from kora.ir.structured_output import (
    StructuredOutput,
    StructuredOutputValidationError,
    canonical_structured_output,
    checked_value,
    parse_structured_output_json,
    structured_output_digest,
)
from kora.ir.structured_schema_assertions import (
    const_matches,
    enum_matches,
    exclusive_maximum_matches,
    exclusive_minimum_matches,
    maximum_matches,
    minimum_matches,
    multiple_of_matches,
    type_matches,
)
from kora.ir.structured_schema_cursors import (
    ArenaCursor,
    NativeCursor,
    arena_cursor,
    native_cursor,
)
from kora.ir.structured_schema_evaluation_policy import PARAMETERS, EvaluationRefused
from kora.ir.structured_schema_evaluation_work import EvaluationMeter
from kora.ir.structured_schema_input import FrozenSchemaArena, Scalar, SchemaArena
from kora.ir.structured_schema_work import PreparationMeter, PreparationRefused
from tests.ir.structured_output_cases import ARTIFACTS
from tests.ir.test_structured_schema_equality import (
    frozen_literal,
    tracked_equality_owners,
)
from tests.ir.test_structured_schema_evaluation_work import (
    Dimension,
    counters,
    remaining_meter,
)

LeafAssertion = Callable[[NativeCursor, ArenaCursor, EvaluationMeter], bool]
Outcome = Literal["true", "false", "refusal", "unexpected"]
LEAF_ASSERTIONS: tuple[LeafAssertion, ...] = (
    type_matches,
    minimum_matches,
    maximum_matches,
    exclusive_minimum_matches,
    exclusive_maximum_matches,
    multiple_of_matches,
    const_matches,
    enum_matches,
)
NUMERIC_ASSERTIONS: tuple[LeafAssertion, ...] = LEAF_ASSERTIONS[1:6]


def assertion_cursors(
    value: object, argument: object, meter: EvaluationMeter
) -> tuple[NativeCursor, ArenaCursor]:
    tree = checked_value(StructuredOutput(value))
    literal = frozen_literal(argument)
    return native_cursor(tree, meter), arena_cursor(literal.nodes, literal.root, meter)


def consumed(
    meter: EvaluationMeter, before: tuple[int, int, int]
) -> tuple[int, int, int]:
    return (
        meter.work - before[0],
        meter.auxiliary_bytes - before[1],
        meter.auxiliary_entries - before[2],
    )


@pytest.mark.parametrize(
    "name", ["array", "boolean", "integer", "null", "number", "object", "string"]
)
@pytest.mark.parametrize(
    ("value", "accepted"),
    [
        (None, ("null",)),
        (False, ("boolean",)),
        (True, ("boolean",)),
        (0, ("integer", "number")),
        (1, ("integer", "number")),
        (-0.0, ("integer", "number")),
        (1.0, ("integer", "number")),
        (1e23, ("integer", "number")),
        (1.5, ("number",)),
        (5e-324, ("number",)),
        ("private-assertion\x00\U0001f600", ("string",)),
        ([], ("array",)),
        ({}, ("object",)),
    ],
)
def test_leaf_type_matches_exact_kinds_and_saved_order(
    value: object, accepted: tuple[str, ...], name: str
) -> None:
    meter = EvaluationMeter()
    instance, argument = assertion_cursors(value, name, meter)

    matched = type_matches(instance, argument, meter)

    assert matched is (name in accepted)


@pytest.mark.parametrize(
    ("value", "name", "work", "entries", "expected"),
    [
        (None, "null", 54, 0, True),
        (False, "boolean", 47, 0, True),
        (1, "integer", 69, 1, True),
        (1, "number", 69, 0, True),
        (False, "integer", 56, 0, False),
        ([], "array", 42, 0, True),
        ({}, "object", 81, 0, True),
        ("a", "string", 87, 0, True),
    ],
)
def test_leaf_type_pays_the_literal_ladder_and_applicable_integer_primitive(
    value: object, name: str, work: int, entries: int, expected: bool
) -> None:
    meter = EvaluationMeter()
    instance, argument = assertion_cursors(value, name, meter)
    before = counters(meter)

    matched = type_matches(instance, argument, meter)

    assert matched is expected
    assert consumed(meter, before) == (work, 0, entries)


@pytest.mark.parametrize("integer_first", [False, True])
def test_leaf_type_saved_order_controls_work_refusal(integer_first: bool) -> None:
    names = ["integer", "number"] if integer_first else ["number", "integer"]
    meter = remaining_meter("work", 5 + 107)
    before = counters(meter)
    instance, argument = assertion_cursors(1.0, names, meter)

    if integer_first:
        with pytest.raises(EvaluationRefused) as rejected:
            type_matches(instance, argument, meter)

        assert rejected.value.reason == "evaluation_exhausted"
        assert consumed(meter, before) == (5 + 103, 0, 3)
        return
    matched = type_matches(instance, argument, meter)

    assert matched
    assert consumed(meter, before) == (5 + 107, 0, 3)


@pytest.mark.parametrize("integer_first", [False, True])
def test_leaf_type_saved_order_controls_integer_record_admission(
    integer_first: bool,
) -> None:
    names = ["integer", "number"] if integer_first else ["number", "integer"]
    meter = remaining_meter("entries", 2 + 1)
    before = counters(meter)
    instance, argument = assertion_cursors(1, names, meter)

    if integer_first:
        with pytest.raises(EvaluationRefused) as rejected:
            type_matches(instance, argument, meter)

        assert rejected.value.reason == "evaluation_exhausted"
        assert consumed(meter, before) == (5 + 100, 0, 3)
        return
    matched = type_matches(instance, argument, meter)

    assert matched
    assert consumed(meter, before) == (5 + 106, 0, 3)


@pytest.mark.parametrize(
    ("value", "names", "expected", "call_work", "call_entries"),
    [
        (None, ["array", "null"], True, 137, 2),
        (None, ["array", "boolean"], False, 131, 2),
        (1, ["null", "integer"], True, 169, 3),
        (1, ["array", "null"], False, 141, 2),
    ],
)
def test_leaf_type_pays_each_nonmatch_advance_and_final_exhaustion(
    value: object,
    names: list[str],
    expected: bool,
    call_work: int,
    call_entries: int,
) -> None:
    meter = EvaluationMeter()
    instance, argument = assertion_cursors(value, names, meter)
    before = counters(meter)

    matched = type_matches(instance, argument, meter)

    assert matched is expected
    assert consumed(meter, before) == (call_work, 0, call_entries)


def test_leaf_type_continues_after_a_completed_fractional_integer_nonmatch() -> None:
    meter = EvaluationMeter()
    instance, argument = assertion_cursors(1.5, ["integer", "number"], meter)

    matched = type_matches(instance, argument, meter)

    assert matched
    assert meter.auxiliary_entries == 2 + 2 + 3
    assert meter.auxiliary_bytes > 0


@pytest.mark.parametrize(
    ("argument_value", "expected_work", "expected_entries"),
    [([], 28, 0), (False, 20, 0), ("invalid", 83, 0), ([False], 55, 1)],
)
def test_leaf_type_rejects_empty_unknown_and_nonstring_visited_forms(
    argument_value: object, expected_work: int, expected_entries: int
) -> None:
    meter = EvaluationMeter()
    instance, argument = assertion_cursors(None, argument_value, meter)
    before = counters(meter)

    with pytest.raises(EvaluationRefused) as rejected:
        type_matches(instance, argument, meter)

    assert rejected.value.reason == "unsupported_schema"
    assert consumed(meter, before) == (expected_work, 0, expected_entries)


@pytest.mark.parametrize("later", [False, "invalid", {"private-canary": None}])
def test_leaf_type_stops_before_an_unvisited_malformed_alternative(
    later: object,
) -> None:
    meter = remaining_meter("work", 5 + 91)
    instance, argument = assertion_cursors(None, ["null", later], meter)
    before = counters(meter)

    matched = type_matches(instance, argument, meter)

    assert matched
    assert consumed(meter, before) == (91, 0, 1)


@pytest.mark.parametrize("earlier", [False, "invalid"])
def test_leaf_type_does_not_treat_an_earlier_refusal_as_a_nonmatch(
    earlier: object,
) -> None:
    meter = EvaluationMeter()
    instance, argument = assertion_cursors(None, [earlier, "null"], meter)

    with pytest.raises(EvaluationRefused) as rejected:
        type_matches(instance, argument, meter)

    assert rejected.value.reason == "unsupported_schema"
    assert meter.auxiliary_entries == 3


@pytest.mark.parametrize(
    ("operation", "accepted_orders"),
    [
        (minimum_matches, (0, 1)),
        (maximum_matches, (-1, 0)),
        (exclusive_minimum_matches, (1,)),
        (exclusive_maximum_matches, (-1,)),
    ],
)
@pytest.mark.parametrize(
    ("value", "bound", "order"),
    [
        (1, 1, 0),
        (0, 1, -1),
        (2, 1, 1),
        (-0.0, 0, 0),
        (10**23, 1e23, 0),
        (10**23 - 1, 1e23, -1),
        (10**23 + 1, 1e23, 1),
        (int(1e23), 1e23, -1),
        (-0.3, -0.2, -1),
        (-(1 << 13_999), 5e-324, -1),
        (1 << 13_999, -5e-324, 1),
    ],
    ids=[
        "equal-integers",
        "below-bound",
        "above-bound",
        "signed-zero",
        "canonical-decimal-equal",
        "integer-below-decimal",
        "integer-above-decimal",
        "binary-rounding",
        "negative-decimals",
        "wide-negative-positive-subnormal",
        "wide-positive-negative-subnormal",
    ],
)
def test_leaf_numeric_bounds_use_exact_decimal_decisions(
    operation: LeafAssertion,
    accepted_orders: tuple[int, ...],
    value: int | float,
    bound: int | float,
    order: int,
) -> None:
    meter = EvaluationMeter()
    instance, argument = assertion_cursors(value, bound, meter)

    matched = operation(instance, argument, meter)

    assert matched is (order in accepted_orders)


@pytest.mark.parametrize("operation", NUMERIC_ASSERTIONS[:4])
def test_leaf_numeric_bounds_propagate_real_expensive_same_sign_refusal(
    operation: LeafAssertion,
) -> None:
    meter = EvaluationMeter()
    instance, argument = assertion_cursors(-(1 << 13_999), -5e-324, meter)

    with pytest.raises(EvaluationRefused) as rejected:
        operation(instance, argument, meter)

    assert rejected.value.reason == "evaluation_exhausted"
    assert meter.work <= PARAMETERS["max_evaluation_work"]
    assert meter.auxiliary_bytes > 0


@pytest.mark.parametrize("operation", NUMERIC_ASSERTIONS)
@pytest.mark.parametrize("argument_value", [None, False, "1", [], {}])
def test_leaf_numerical_assertions_reject_applicable_nonnumerical_bounds(
    operation: LeafAssertion, argument_value: object
) -> None:
    meter = EvaluationMeter()
    instance, argument = assertion_cursors(1, argument_value, meter)
    before = counters(meter)

    with pytest.raises(EvaluationRefused) as rejected:
        operation(instance, argument, meter)

    assert rejected.value.reason == "unsupported_schema"
    assert meter.auxiliary_entries == before[2]
    assert meter.auxiliary_bytes == 0


@pytest.mark.parametrize("operation", NUMERIC_ASSERTIONS)
@pytest.mark.parametrize(
    ("value", "kind_work"), [(None, 5), (False, 6), ("1", 9), ({"a": 1}, 11)]
)
def test_leaf_numerical_applicability_precedes_bound_inspection(
    operation: LeafAssertion, value: object, kind_work: int
) -> None:
    meter = EvaluationMeter()
    instance, admitted_argument = assertion_cursors(value, None, meter)
    argument = ArenaCursor(admitted_argument.nodes, len(admitted_argument.nodes))
    before = counters(meter)

    matched = operation(instance, argument, meter)

    assert matched
    assert consumed(meter, before) == (
        kind_work + (1 if operation is multiple_of_matches else 2),
        0,
        0,
    )


@pytest.mark.parametrize("operation", NUMERIC_ASSERTIONS)
def test_leaf_inapplicable_dense_admitted_array_has_no_per_item_state(
    operation: LeafAssertion,
) -> None:
    count = PARAMETERS["max_input_nodes"] + 1
    call_work = 11 if operation is multiple_of_matches else 12
    meter = remaining_meter("entries", 2)
    instance, argument = assertion_cursors([None] * count, {"ignored": 0.3}, meter)
    before = counters(meter)

    matched = operation(instance, argument, meter)

    assert matched
    assert len(cast("list[object]", instance.value)) == count
    assert consumed(meter, before) == (call_work, 0, 0)


@pytest.mark.parametrize(
    ("value", "divisor", "expected"),
    [
        (0.3, 0.1, True),
        (10**400, 0.5, True),
        (-0.3, 0.1, True),
        (0.3, 0.2, False),
        (0, 5e-324, True),
        (5e-324, 1e-323, False),
        (1e-323, 5e-324, True),
        ((1 << 13_999) - 2, 3, True),
    ],
    ids=[
        "decimal-multiple",
        "wide-integer-half-multiple",
        "negative-dividend",
        "fractional-quotient",
        "zero-subnormal-divisor",
        "subnormal-fraction",
        "subnormal-double",
        "wide-integer-third-multiple",
    ],
)
def test_leaf_multiple_of_preserves_exact_schedule_and_refusal(
    value: int | float, divisor: int | float, expected: bool
) -> None:
    meter = EvaluationMeter()
    instance, argument = assertion_cursors(value, divisor, meter)

    matched = multiple_of_matches(instance, argument, meter)

    assert matched is expected


def test_leaf_multiple_of_executes_published_numerical_vectors() -> None:
    vectors = cast(
        "dict[str, list[dict[str, object]]]",
        json.loads((ARTIFACTS / "StructuredOutput.vectors.json").read_bytes()),
    )
    cases = [
        case
        for case in vectors["schema_arithmetic_cases"]
        if case["name"] in ("decimal-multiple", "wide-integer-half-multiple")
    ]
    meter = EvaluationMeter()
    results: list[bool] = []

    for case in cases:
        schema = cast("dict[str, object]", json.loads(cast("str", case["schema_text"])))
        value: object = json.loads(cast("str", case["value_text"]))
        instance, argument = assertion_cursors(value, schema["multipleOf"], meter)
        results.append(multiple_of_matches(instance, argument, meter))

    assert len(cases) == 2
    assert results == [case["accepted"] for case in cases]


@pytest.mark.parametrize("divisor", [0, 0.0, -0.0, -1, -0.1])
def test_leaf_multiple_of_propagates_nonpositive_divisor_refusal(
    divisor: int | float,
) -> None:
    meter = EvaluationMeter()
    instance, argument = assertion_cursors(1, divisor, meter)

    with pytest.raises(EvaluationRefused) as rejected:
        multiple_of_matches(instance, argument, meter)

    assert rejected.value.reason == "unsupported_schema"
    assert rejected.value.__cause__ is None
    assert rejected.value.__context__ is None


@pytest.mark.parametrize("operation", [const_matches, enum_matches])
@pytest.mark.parametrize(
    ("value", "literal", "expected"),
    [
        (None, None, True),
        (True, 1, False),
        (1, 1.0, True),
        (-0.0, 0, True),
        (10**23, 1e23, True),
        (int(1e23), 1e23, False),
        ("nul\x00\U0001f600", "nul\x00\U0001f600", True),
        ("\u00e9", "e\u0301", False),
        ([None, False, 1], [None, False, 1.0], True),
        ([1, 2], [2, 1], False),
        ({"z": [], "a": {"b": 1}}, {"a": {"b": 1.0}, "z": []}, True),
        ({"a": 1}, {"a": False}, False),
    ],
)
def test_leaf_const_and_enum_preserve_recursive_meaning_and_order(
    operation: LeafAssertion, value: object, literal: object, expected: bool
) -> None:
    model = StructuredOutput(value)
    tree = checked_value(model)
    original_bytes = canonical_structured_output(model)
    original_digest = structured_output_digest(model)
    arena = frozen_literal([literal] if operation is enum_matches else literal)
    original_nodes = arena.nodes
    original_arena_bytes = arena.encode(PreparationMeter())
    meter = EvaluationMeter()
    instance = native_cursor(tree, meter)
    argument = arena_cursor(arena.nodes, arena.root, meter)

    matched = operation(instance, argument, meter)

    assert matched is expected
    assert instance.value is tree
    assert argument.nodes is original_nodes
    assert canonical_structured_output(model) == original_bytes
    assert structured_output_digest(model) == original_digest
    assert arena.encode(PreparationMeter()) == original_arena_bytes
    assert repr(instance) == "NativeCursor()"
    assert repr(argument) == "ArenaCursor()"


@pytest.mark.parametrize(
    ("candidates", "expected", "call_work", "call_entries"),
    [
        ([], False, 23, 0),
        ([None, False], True, 79, 4),
        ([False, None], True, 133, 8),
        ([False, False], False, 131, 8),
        ([False, False, None], True, 187, 12),
    ],
)
def test_leaf_enum_charges_empty_duplicate_and_saved_order_candidates(
    candidates: list[object], expected: bool, call_work: int, call_entries: int
) -> None:
    meter = EvaluationMeter()
    instance, argument = assertion_cursors(None, candidates, meter)
    before = counters(meter)

    matched = enum_matches(instance, argument, meter)

    assert matched is expected
    assert consumed(meter, before) == (call_work, 0, call_entries)


@pytest.mark.parametrize("argument_value", [None, False, 1, "array", {}])
def test_leaf_enum_requires_an_array_before_comparing_candidates(
    argument_value: object,
) -> None:
    meter = EvaluationMeter()
    instance, argument = assertion_cursors(None, argument_value, meter)
    before = counters(meter)

    with pytest.raises(EvaluationRefused) as rejected:
        enum_matches(instance, argument, meter)

    assert rejected.value.reason == "unsupported_schema"
    assert meter.auxiliary_entries == before[2]
    assert meter.auxiliary_bytes == 0


@pytest.mark.parametrize("expensive_first", [False, True])
def test_leaf_enum_first_match_stops_before_a_later_real_refusal(
    expensive_first: bool,
) -> None:
    value = 1 << 13_999
    candidates = [5e-324, value] if expensive_first else [value, 5e-324]
    meter = EvaluationMeter()
    instance, argument = assertion_cursors(value, candidates, meter)

    if expensive_first:
        with pytest.raises(EvaluationRefused) as rejected:
            enum_matches(instance, argument, meter)

        assert rejected.value.reason == "evaluation_exhausted"
        return
    matched = enum_matches(instance, argument, meter)

    assert matched
    assert meter.auxiliary_bytes == 0
    assert meter.auxiliary_entries == 2 + 1 + 3 + 2


@pytest.mark.parametrize("operation", [const_matches, enum_matches])
def test_leaf_literal_comparison_checks_all_keys_before_expensive_values(
    operation: LeafAssertion,
) -> None:
    value = {"a": 1 << 13_999, "b": None}
    literal = {"a": 5e-324, "c": None}
    meter = remaining_meter("work", 1_000)
    instance, argument = assertion_cursors(
        value, [literal] if operation is enum_matches else literal, meter
    )

    matched = operation(instance, argument, meter)

    assert not matched
    assert meter.auxiliary_bytes == 0


@pytest.mark.parametrize("dimension", ["work", "entries"])
@pytest.mark.parametrize("short", [False, True], ids=["exact", "one-short"])
@pytest.mark.parametrize(
    (
        "operation",
        "value",
        "argument_value",
        "expected",
        "call_work",
        "call_entries",
        "byte_count",
        "short_work_paid",
        "short_entry_paid",
    ),
    [
        (minimum_matches, 1, 2, False, 71, 2, 0, (75, 0, 4), (46, 0, 3)),
        (maximum_matches, 1, 2, True, 71, 2, 0, (75, 0, 4), (46, 0, 3)),
        (exclusive_minimum_matches, 1, 2, False, 71, 2, 0, (75, 0, 4), (46, 0, 3)),
        (exclusive_maximum_matches, 1, 2, True, 71, 2, 0, (75, 0, 4), (46, 0, 3)),
        (type_matches, 1, "integer", True, 69, 1, 0, (72, 0, 3), (69, 0, 2)),
        (const_matches, None, None, True, 44, 3, 0, (48, 0, 5), (9, 0, 3)),
        (enum_matches, None, [None], True, 79, 4, 0, (83, 0, 6), (43, 0, 4)),
        (multiple_of_matches, 0, 1, True, 69, 2, 1, (73, 1, 4), (46, 0, 3)),
    ],
)
def test_leaf_assertions_reserve_shared_capacity_before_actions(
    operation: LeafAssertion,
    value: object,
    argument_value: object,
    expected: bool,
    call_work: int,
    call_entries: int,
    byte_count: int,
    short_work_paid: tuple[int, int, int],
    short_entry_paid: tuple[int, int, int],
    dimension: Dimension,
    short: bool,
) -> None:
    required = 5 + call_work if dimension == "work" else 2 + call_entries
    meter = remaining_meter(dimension, required - int(short))
    before = counters(meter)
    instance, argument = assertion_cursors(value, argument_value, meter)

    if short:
        with pytest.raises(EvaluationRefused) as rejected:
            operation(instance, argument, meter)

        assert rejected.value.reason == "evaluation_exhausted"
        assert consumed(meter, before) == (
            short_work_paid if dimension == "work" else short_entry_paid
        )
        return
    matched = operation(instance, argument, meter)

    assert matched is expected
    assert consumed(meter, before) == (5 + call_work, byte_count, 2 + call_entries)


@pytest.mark.parametrize("short", [False, True], ids=["exact", "one-short"])
def test_leaf_multiple_of_precharges_the_full_zero_dividend_path(short: bool) -> None:
    meter = remaining_meter("bytes", 1 - int(short))
    before = counters(meter)
    instance, argument = assertion_cursors(0, 1, meter)

    if short:
        with pytest.raises(EvaluationRefused) as rejected:
            multiple_of_matches(instance, argument, meter)

        assert rejected.value.reason == "evaluation_exhausted"
        assert consumed(meter, before) == (63, 0, 4)
        return
    matched = multiple_of_matches(instance, argument, meter)

    assert matched
    assert consumed(meter, before) == (74, 1, 4)


@pytest.mark.parametrize(
    ("operation", "argument_value", "paid_work"),
    [(type_matches, ["null"], 43), (enum_matches, [None], 36)],
)
def test_leaf_alternative_handle_refusal_keeps_only_the_paid_accessor_prefix(
    operation: LeafAssertion, argument_value: object, paid_work: int
) -> None:
    meter = remaining_meter("entries", 2)
    before = counters(meter)
    instance, argument = assertion_cursors(None, argument_value, meter)

    with pytest.raises(EvaluationRefused) as rejected:
        operation(instance, argument, meter)

    assert rejected.value.reason == "evaluation_exhausted"
    assert consumed(meter, before) == (paid_work, 0, 2)


@pytest.mark.parametrize(
    ("operation", "value", "argument_value", "expected", "call_work", "call_entries"),
    [
        (type_matches, 1, "integer", True, 69, 1),
        (minimum_matches, 1, 2, False, 71, 2),
        (maximum_matches, 1, 2, True, 71, 2),
        (exclusive_minimum_matches, 1, 2, False, 71, 2),
        (exclusive_maximum_matches, 1, 2, True, 71, 2),
        (multiple_of_matches, 0, 1, True, 69, 2),
        (const_matches, None, None, True, 44, 3),
        (enum_matches, None, [False, None], True, 133, 8),
    ],
)
def test_leaf_repeated_calls_share_one_monotone_meter_without_refunds_or_cache(
    operation: LeafAssertion,
    value: object,
    argument_value: object,
    expected: bool,
    call_work: int,
    call_entries: int,
) -> None:
    meter = remaining_meter("work", 5 + 2 * call_work)
    instance, argument = assertion_cursors(value, argument_value, meter)
    before = counters(meter)

    first = operation(instance, argument, meter)
    once = consumed(meter, before)
    second = operation(instance, argument, meter)
    twice = consumed(meter, before)
    exhausted = counters(meter)
    with pytest.raises(EvaluationRefused) as rejected:
        operation(instance, argument, meter)

    assert first is expected
    assert second is expected
    assert once == (call_work, int(operation is multiple_of_matches), call_entries)
    assert twice == tuple(2 * amount for amount in once)
    assert exhausted[0] == PARAMETERS["max_evaluation_work"]
    assert counters(meter) == exhausted
    assert rejected.value.reason == "evaluation_exhausted"


@pytest.mark.parametrize("operation", LEAF_ASSERTIONS)
@pytest.mark.parametrize("hostile_side", ["native", "arena"])
def test_leaf_assertions_reject_visited_hostile_shapes_without_callbacks(
    operation: LeafAssertion, hostile_side: str
) -> None:
    calls: list[str] = []

    class AuthoredMeta(type):
        def __eq__(cls, other: object) -> bool:
            calls.append("metaclass equality")
            raise AssertionError("authored metaclass equality invoked")

    class Authored(metaclass=AuthoredMeta):
        def __repr__(self) -> str:
            calls.append("representation")
            raise AssertionError("private-assertion-value-canary")

    value = Authored()
    retained = weakref.ref(value)
    meter = EvaluationMeter()
    argument_value: object = (
        "null"
        if operation is type_matches
        else [None]
        if operation is enum_matches
        else 1
    )
    instance, argument = assertion_cursors(
        None if hostile_side == "native" else 1, argument_value, meter
    )
    rows = list(argument.nodes)
    if hostile_side == "native":
        instance = NativeCursor(value)
    else:
        target = (
            rows[argument.node].children[0]
            if operation is enum_matches
            else argument.node
        )
        rows[target] = replace(rows[target], value=cast("Scalar", value))
        argument = ArenaCursor(tuple(rows), argument.node)
    safe_handles = repr(instance), repr(argument)

    with pytest.raises(EvaluationRefused) as rejected:
        operation(instance, argument, meter)
    reason = rejected.value.reason
    safe_failure = str(rejected.value), repr(rejected.value)
    no_chain = rejected.value.__cause__ is None and rejected.value.__context__ is None
    del value, instance, argument, rejected, rows
    gc.collect()

    assert reason == "unsupported_schema"
    assert no_chain
    assert calls == []
    assert retained() is None
    assert all(
        "private-assertion-value-canary" not in text
        for text in safe_handles + safe_failure
    )


class AuthoredString(str):
    def __len__(self) -> int:
        raise AssertionError("authored string length invoked")


@pytest.mark.parametrize(
    "value",
    [
        math.nan,
        math.inf,
        -math.inf,
        "\ud800",
        {"\udfff": None},
        AuthoredString("private-assertion-value-canary"),
        1 << 14_000,
    ],
    ids=[
        "nan",
        "positive-infinity",
        "negative-infinity",
        "surrogate",
        "surrogate-key",
        "subclass",
        "integer-bits",
    ],
)
def test_leaf_inputs_require_real_model_and_arena_domain_admission(
    value: object,
) -> None:
    arena = SchemaArena(PreparationMeter())

    with pytest.raises(StructuredOutputValidationError) as model_rejected:
        StructuredOutput(value)
    with pytest.raises(PreparationRefused) as arena_rejected:
        arena.snapshot(value)

    assert model_rejected.value.violation.code == "SO003"
    assert arena_rejected.value.reason == "unsupported_schema"
    assert "private-assertion-value-canary" not in str(model_rejected.value)
    assert "private-assertion-value-canary" not in str(arena_rejected.value)


def test_leaf_inputs_require_real_model_and_arena_cycle_admission() -> None:
    cycle: list[object] = []
    cycle.append(cycle)
    arena = SchemaArena(PreparationMeter())

    with pytest.raises(StructuredOutputValidationError) as model_rejected:
        StructuredOutput(cycle)
    with pytest.raises(PreparationRefused) as arena_rejected:
        arena.snapshot(cycle)

    assert model_rejected.value.violation.code == "SO003"
    assert arena_rejected.value.reason == "unsupported_schema"


@pytest.mark.parametrize("raw", [b"NaN", rb'{"a":0,"\u0061":1}', rb'"\ud800"'])
def test_leaf_inputs_require_real_raw_domain_admission(raw: bytes) -> None:
    candidate = raw

    with pytest.raises(StructuredOutputValidationError) as rejected:
        parse_structured_output_json(candidate)

    assert rejected.value.violation.code == "SO002"


def owned_assertion(operation: LeafAssertion, outcome: Outcome) -> bool:
    value: object = 0.3
    argument_value: object = 0.3
    if operation is type_matches:
        value = 1.0
        argument_value = "array" if outcome == "false" else "integer"
    elif operation is minimum_matches:
        argument_value = 0.4 if outcome == "false" else 0.3
    elif operation is maximum_matches:
        argument_value = 0.2 if outcome == "false" else 0.3
    elif operation is exclusive_minimum_matches:
        argument_value = 0.3 if outcome == "false" else 0.2
    elif operation is exclusive_maximum_matches:
        argument_value = 0.3 if outcome == "false" else 0.4
    elif operation is multiple_of_matches:
        argument_value = 0.2 if outcome == "false" else 0.1
    else:
        value = {"private-assertion-key-canary": [0.3]}
        argument_value = {
            "private-assertion-key-canary": [False if outcome == "false" else 0.3]
        }
        if operation is enum_matches:
            argument_value = [argument_value]
    tree = checked_value(StructuredOutput(value))
    arena: FrozenSchemaArena = frozen_literal(argument_value)
    meter = (
        remaining_meter("work", 1_000) if outcome == "refusal" else EvaluationMeter()
    )
    first = native_cursor(tree, meter)
    second = arena_cursor(arena.nodes, arena.root, meter)
    if outcome == "unexpected":
        if operation is const_matches or operation is enum_matches:
            rows = list(arena.nodes)
            position = next(
                index for index, node in enumerate(rows) if node.kind == "array"
            )
            rows[position] = replace(rows[position], children=(len(rows),))
            second = ArenaCursor(tuple(rows), arena.root)
        else:
            second = ArenaCursor(arena.nodes, len(arena.nodes))
    return operation(first, second, meter)


@pytest.mark.parametrize("operation", LEAF_ASSERTIONS)
@pytest.mark.parametrize("outcome", ["true", "false", "refusal", "unexpected"])
def test_leaf_assertions_release_borrowed_state_on_every_exit(
    operation: LeafAssertion, outcome: Outcome
) -> None:
    gc.collect()
    original = tracked_equality_owners()

    if outcome in ("true", "false"):
        matched = owned_assertion(operation, outcome)
        gc.collect()
        remaining = tracked_equality_owners()

        assert matched is (outcome == "true")
        assert remaining == original
        return
    expected_error = EvaluationRefused if outcome == "refusal" else IndexError
    with pytest.raises(expected_error) as rejected:
        owned_assertion(operation, outcome)
    gc.collect()
    held_by_caller = tracked_equality_owners() - original
    safe_owners = tuple(
        repr(value) for value in gc.get_objects() if id(value) in held_by_caller
    )
    safe_failure = str(rejected.value), repr(rejected.value)
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
        "private-assertion-key-canary" not in text
        for text in safe_owners + safe_failure
    )
