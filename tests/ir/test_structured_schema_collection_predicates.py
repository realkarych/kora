import gc
from dataclasses import replace
from typing import Literal, cast

import pytest

from kora.ir.structured_output import StructuredOutput, checked_value
from kora.ir.structured_schema_collection_predicates import (
    dependent_required_present,
    required_names_present,
    unique_items_matches,
)
from kora.ir.structured_schema_cursors import (
    ArenaCursor,
    NativeCursor,
    NativeObjectIndex,
    arena_cursor,
    build_native_object_index,
    native_cursor,
)
from kora.ir.structured_schema_evaluation_policy import EvaluationRefused
from kora.ir.structured_schema_evaluation_work import EvaluationMeter
from kora.ir.structured_schema_input import Scalar
from tests.ir.test_structured_schema_equality import (
    FLOAT_PAIR_ADMINISTRATION_WORK,
    TENTHS_EQUALITY_WORK,
    frozen_literal,
    tracked_equality_owners,
)
from tests.ir.test_structured_schema_evaluation_work import (
    Dimension,
    counters,
    remaining_meter,
)

Operation = Literal["unique", "required", "dependent"]
Outcome = Literal["true", "false", "refusal", "unexpected"]
Borrowed = NativeCursor | NativeObjectIndex


def predicate_arguments(
    operation: Operation, value: object, rule: object, meter: EvaluationMeter
) -> tuple[Borrowed, ArenaCursor]:
    tree = checked_value(StructuredOutput(value))
    borrowed: Borrowed = (
        native_cursor(tree, meter)
        if operation == "unique"
        else build_native_object_index(cast("dict[str, object]", tree), meter)
    )
    literal = frozen_literal(rule)
    return borrowed, arena_cursor(literal.nodes, literal.root, meter)


def predicate(
    operation: Operation,
    borrowed: Borrowed,
    rule: ArenaCursor,
    meter: EvaluationMeter,
) -> bool:
    if operation == "unique":
        return unique_items_matches(cast("NativeCursor", borrowed), rule, meter)
    if operation == "required":
        return required_names_present(cast("NativeObjectIndex", borrowed), rule, meter)
    return dependent_required_present(cast("NativeObjectIndex", borrowed), rule, meter)


def consumed(
    meter: EvaluationMeter, before: tuple[int, int, int]
) -> tuple[int, int, int]:
    return (
        meter.work - before[0],
        meter.auxiliary_bytes - before[1],
        meter.auxiliary_entries - before[2],
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param([], True, id="empty"),
        pytest.param([None], True, id="singleton"),
        pytest.param([None, None], False, id="null-duplicate"),
        pytest.param([True, 1], True, id="boolean-is-distinct"),
        pytest.param([1, 1.0], False, id="numerical-cross-kind"),
        pytest.param([-0.0, 0], False, id="signed-zero"),
        pytest.param([10**23, 1e23], False, id="canonical-decimal"),
        pytest.param([int(1e23), 1e23], True, id="binary-rounding-distinct"),
        pytest.param(["\u00e9", "e\u0301"], True, id="no-normalization"),
        pytest.param(["nul\x00", "nul\x00"], False, id="nul-duplicate"),
        pytest.param([[None], [None]], False, id="nested-array"),
        pytest.param(
            [{"z": [0.3], "a": None}, {"a": None, "z": [0.3]}],
            False,
            id="canonical-object-keys",
        ),
    ],
)
def test_unique_items_preserves_pair_order_and_exact_json_meaning(
    value: object, expected: bool
) -> None:
    meter = EvaluationMeter()
    borrowed, rule = predicate_arguments("unique", value, True, meter)

    matched = predicate("unique", borrowed, rule, meter)

    assert matched is expected


def test_unique_items_advances_to_the_later_outer_row_without_identity_discount() -> (
    None
):
    meter = EvaluationMeter()
    borrowed, rule = predicate_arguments("unique", [None, True, True], True, meter)
    before = counters(meter)

    matched = predicate("unique", borrowed, rule, meter)

    assert not matched
    assert consumed(meter, before) == (200, 0, 15)


@pytest.mark.parametrize("expensive_first", [False, True])
def test_unique_items_stops_on_the_first_pair_decision_or_refusal(
    expensive_first: bool,
) -> None:
    wide = 1 << 13_999
    value = [wide, 5e-324, wide] if expensive_first else [wide, wide, 5e-324]
    meter = EvaluationMeter()
    borrowed, rule = predicate_arguments("unique", value, True, meter)

    if expensive_first:
        with pytest.raises(EvaluationRefused) as rejected:
            predicate("unique", borrowed, rule, meter)

        assert rejected.value.reason == "evaluation_exhausted"
        return
    matched = predicate("unique", borrowed, rule, meter)

    assert not matched
    assert meter.auxiliary_bytes == 0


@pytest.mark.parametrize(
    ("value", "work"),
    [(None, 6), (False, 7), (0, 8), (0.3, 9), ("a", 10), ({}, 12)],
)
def test_unique_items_inapplicability_does_not_visit_the_argument(
    value: object, work: int
) -> None:
    meter = EvaluationMeter()
    borrowed, rule = predicate_arguments(
        "unique", value, ["invalid-internal-flag"], meter
    )
    before = counters(meter)

    matched = predicate("unique", borrowed, rule, meter)

    assert matched
    assert consumed(meter, before) == (work, 0, 0)


def test_unique_items_false_flag_does_not_traverse_a_dense_admitted_array() -> None:
    meter = EvaluationMeter()
    borrowed, rule = predicate_arguments("unique", [None] * 20_000, False, meter)
    before = counters(meter)

    matched = predicate("unique", borrowed, rule, meter)

    assert matched
    assert consumed(meter, before) == (32, 0, 0)


@pytest.mark.parametrize(
    ("names", "expected", "work", "entries"),
    [
        ([], True, 25, 0),
        (["a"], True, 73, 1),
        (["b"], False, 71, 1),
        (["a", "b"], False, 119, 2),
        (["b", "a"], False, 71, 1),
    ],
)
def test_required_names_preserve_saved_order_and_paid_lookup(
    names: list[str], expected: bool, work: int, entries: int
) -> None:
    meter = EvaluationMeter()
    borrowed, rule = predicate_arguments("required", {"a": False}, names, meter)
    before = counters(meter)

    matched = predicate("required", borrowed, rule, meter)

    assert matched is expected
    assert consumed(meter, before) == (work, 0, entries)


@pytest.mark.parametrize("found", [False, True])
def test_required_lookup_uses_the_prebuilt_longest_key_and_unicode_rows(
    found: bool,
) -> None:
    value: dict[str, object] = {
        "prefix": None,
        "prefixx": False,
        "q" * 37: 0,
        "z": [],
    }
    query = "prefixx" if found else "prefixy"
    meter = EvaluationMeter()
    borrowed, rule = predicate_arguments("required", value, [query], meter)
    index = cast("NativeObjectIndex", borrowed)
    before = counters(meter)

    matched = required_names_present(index, rule, meter)

    assert index.maximum_key == 37
    assert matched is found
    assert consumed(meter, before) == (247 if found else 245, 0, 1)


def test_required_names_match_nul_and_scalar_unicode_without_normalizing() -> None:
    meter = EvaluationMeter()
    value = {"nul\x00": None, "\U0001f600": False, "\u00e9": 0}
    borrowed, rule = predicate_arguments(
        "required", value, ["\U0001f600", "nul\x00"], meter
    )
    different = frozen_literal(["e\u0301"])
    different_rule = arena_cursor(different.nodes, different.root, meter)

    present = predicate("required", borrowed, rule, meter)
    normalized = predicate("required", borrowed, different_rule, meter)

    assert present
    assert not normalized


@pytest.mark.parametrize("present_value", [False, None, 0, []])
@pytest.mark.parametrize("has_required", [False, True])
def test_dependent_required_uses_presence_and_canonical_trigger_order(
    present_value: object, has_required: bool
) -> None:
    value = {"trigger": present_value}
    if has_required:
        value["needed"] = False
    meter = EvaluationMeter()
    borrowed, rule = predicate_arguments(
        "dependent", value, {"trigger": ["needed"]}, meter
    )

    matched = predicate("dependent", borrowed, rule, meter)

    assert matched is has_required


@pytest.mark.parametrize(
    ("value", "dependencies", "expected", "work", "entries"),
    [
        ({"a": False}, {}, True, 27, 0),
        ({}, {"a": ["b"]}, True, 46, 0),
        ({"a": False}, {"a": []}, True, 90, 1),
        ({"a": False}, {"a": ["b"]}, False, 134, 2),
    ],
)
def test_dependent_required_pays_each_trigger_and_nested_requirement(
    value: dict[str, object],
    dependencies: dict[str, object],
    expected: bool,
    work: int,
    entries: int,
) -> None:
    meter = EvaluationMeter()
    borrowed, rule = predicate_arguments("dependent", value, dependencies, meter)
    before = counters(meter)

    matched = predicate("dependent", borrowed, rule, meter)

    assert matched is expected
    assert consumed(meter, before) == (work, 0, entries)


@pytest.mark.parametrize(
    ("operation", "value", "rule_value", "expected"),
    [
        ("required", {}, ["missing", 1], False),
        ("dependent", {}, {"absent": 1}, True),
        ("dependent", {"a": False, "z": False}, {"z": 1, "a": ["missing"]}, False),
    ],
)
def test_membership_short_circuits_do_not_certify_unvisited_internal_forms(
    operation: Operation, value: object, rule_value: object, expected: bool
) -> None:
    meter = EvaluationMeter()
    borrowed, rule = predicate_arguments(operation, value, rule_value, meter)

    matched = predicate(operation, borrowed, rule, meter)

    assert matched is expected


def test_membership_predicates_share_one_real_index_without_new_sort_state() -> None:
    tree = cast("dict[str, object]", checked_value(StructuredOutput({"a": False})))
    meter = EvaluationMeter()
    index = build_native_object_index(tree, meter)
    required_value: list[str] = ["a"]
    dependent_value: dict[str, list[str]] = {"a": []}
    literal = frozen_literal([required_value, dependent_value])
    first, second = literal.nodes[literal.root].children
    required = arena_cursor(literal.nodes, first, meter)
    dependent = arena_cursor(literal.nodes, second, meter)
    original_row = index.row(0, meter)
    before = counters(meter)

    first_result = required_names_present(index, required, meter)
    second_result = dependent_required_present(index, dependent, meter)
    used = consumed(meter, before)
    retained_row = index.row(0, meter)

    assert first_result
    assert second_result
    assert used == (163, 0, 2)
    assert retained_row is original_row
    assert retained_row[1] is tree["a"]


@pytest.mark.parametrize("short", [False, True], ids=["exact", "one-short"])
@pytest.mark.parametrize(
    ("operation", "value", "rule_value", "expected", "setup", "work"),
    [
        ("unique", [None, None], True, False, 5, 90),
        ("required", {"a": False}, ["a"], True, 15, 73),
        ("dependent", {"a": False}, {"a": []}, True, 15, 90),
    ],
)
def test_collection_predicates_reserve_shared_capacity_before_actions(
    operation: Operation,
    value: object,
    rule_value: object,
    expected: bool,
    setup: int,
    work: int,
    short: bool,
) -> None:
    meter = remaining_meter("work", setup + work - int(short))
    borrowed, rule = predicate_arguments(operation, value, rule_value, meter)
    before = counters(meter)

    if short:
        with pytest.raises(EvaluationRefused) as rejected:
            predicate(operation, borrowed, rule, meter)

        assert rejected.value.reason == "evaluation_exhausted"
        assert consumed(meter, before)[0] == work - 1
        return
    matched = predicate(operation, borrowed, rule, meter)

    assert matched is expected
    assert consumed(meter, before)[0] == work


@pytest.mark.parametrize(
    (
        "operation",
        "value",
        "rule_value",
        "setup_entries",
        "call_entries",
        "paid_work",
        "paid_entries",
    ),
    [
        ("unique", [None, None], True, 2, 5, 56, 3),
        ("required", {"a": False}, ["a"], 8, 1, 33, 0),
        ("dependent", {"a": False}, {"a": ["b"]}, 8, 2, 95, 1),
    ],
)
def test_collection_handle_refusal_keeps_atomic_paid_prefixes(
    operation: Operation,
    value: object,
    rule_value: object,
    setup_entries: int,
    call_entries: int,
    paid_work: int,
    paid_entries: int,
) -> None:
    meter = remaining_meter("entries", setup_entries + call_entries - 1)
    borrowed, rule = predicate_arguments(operation, value, rule_value, meter)
    before = counters(meter)

    with pytest.raises(EvaluationRefused) as rejected:
        predicate(operation, borrowed, rule, meter)

    assert rejected.value.reason == "evaluation_exhausted"
    assert consumed(meter, before) == (paid_work, 0, paid_entries)


@pytest.mark.parametrize("dimension", ["work", "bytes", "entries"])
@pytest.mark.parametrize("short", [False, True], ids=["exact", "one-short"])
def test_unique_items_preserves_equality_work_bytes_and_entry_admission(
    dimension: Dimension, short: bool
) -> None:
    call_work = 53 + FLOAT_PAIR_ADMINISTRATION_WORK + TENTHS_EQUALITY_WORK
    required = {"work": 5 + call_work, "bytes": 100, "entries": 13}
    meter = remaining_meter(dimension, required[dimension] - int(short))
    borrowed, rule = predicate_arguments("unique", [0.3, 0.3], True, meter)
    before = counters(meter)

    if short:
        with pytest.raises(EvaluationRefused) as rejected:
            predicate("unique", borrowed, rule, meter)

        assert rejected.value.reason == "evaluation_exhausted"
        if dimension == "work":
            assert consumed(meter, before) == (call_work - 1, 100, 11)
        elif dimension == "bytes":
            assert consumed(meter, before)[1:] == (98, 10)
        else:
            assert consumed(meter, before)[1:] == (100, 10)
        return
    matched = predicate("unique", borrowed, rule, meter)

    assert not matched
    assert consumed(meter, before) == (call_work, 100, 11)


def test_repeated_membership_calls_never_reset_or_refund_the_shared_meter() -> None:
    meter = remaining_meter("work", 15 + 2 * 73)
    borrowed, rule = predicate_arguments("required", {"a": False}, ["a"], meter)
    before = counters(meter)

    first = predicate("required", borrowed, rule, meter)
    second = predicate("required", borrowed, rule, meter)
    completed = counters(meter)
    with pytest.raises(EvaluationRefused) as rejected:
        predicate("required", borrowed, rule, meter)

    assert first
    assert second
    assert consumed(meter, before) == (146, 0, 2)
    assert counters(meter) == completed
    assert rejected.value.reason == "evaluation_exhausted"


@pytest.mark.parametrize("operation", ["required", "dependent"])
def test_collection_predicates_reject_visited_hostile_forms_without_callbacks(
    operation: Operation,
) -> None:
    calls: list[str] = []

    class AuthoredIndex(NativeObjectIndex):
        def find(self, query: str, meter: EvaluationMeter) -> int:
            calls.append("find")
            raise AssertionError("private-index-canary")

    index = AuthoredIndex([], 0)
    literal = frozen_literal([] if operation == "required" else {})
    meter = EvaluationMeter()
    rule = arena_cursor(literal.nodes, literal.root, meter)
    before = counters(meter)

    with pytest.raises(EvaluationRefused) as rejected:
        predicate(operation, index, rule, meter)

    assert rejected.value.reason == "unsupported_schema"
    assert rejected.value.__cause__ is None
    assert rejected.value.__context__ is None
    assert consumed(meter, before) == (2, 0, 0)
    assert calls == []
    assert "private-index-canary" not in str(rejected.value)


@pytest.mark.parametrize(
    ("operation", "value", "rule_value"),
    [
        ("unique", [], 1),
        ("required", {}, {}),
        ("required", {}, [None]),
        ("dependent", {}, []),
        ("dependent", {"a": False}, {"a": None}),
    ],
)
def test_collection_predicates_refuse_malformed_visited_internal_shapes(
    operation: Operation, value: object, rule_value: object
) -> None:
    meter = EvaluationMeter()
    borrowed, rule = predicate_arguments(operation, value, rule_value, meter)

    with pytest.raises(EvaluationRefused) as rejected:
        predicate(operation, borrowed, rule, meter)

    assert rejected.value.reason == "unsupported_schema"


@pytest.mark.parametrize("operation", ["unique", "required", "dependent"])
def test_visited_arena_content_never_invokes_authored_callbacks(
    operation: Operation,
) -> None:
    calls: list[str] = []

    class AuthoredString(str):
        def __len__(self) -> int:
            calls.append("length")
            raise AssertionError("private-schema-canary")

        def __lt__(self, other: str) -> bool:
            calls.append("comparison")
            raise AssertionError("private-schema-canary")

    value: list[object] | dict[str, object] = [] if operation == "unique" else {}
    rule_value: object = True if operation == "unique" else ["a"]
    if operation == "dependent":
        rule_value = {"a": []}
    meter = EvaluationMeter()
    borrowed, rule = predicate_arguments(operation, value, rule_value, meter)
    rows = list(rule.nodes)
    target = rule.node
    if operation == "required":
        target = rows[rule.node].children[0]
    elif operation == "dependent":
        target = rows[rule.node].members[0][0]
    rows[target] = replace(
        rows[target], value=cast("Scalar", AuthoredString("private-schema-canary"))
    )
    corrupted = ArenaCursor(tuple(rows), rule.node)

    with pytest.raises(EvaluationRefused) as rejected:
        predicate(operation, borrowed, corrupted, meter)

    assert rejected.value.reason == "unsupported_schema"
    assert calls == []
    assert "private-schema-canary" not in str(rejected.value)


def owned_predicate(operation: Operation, outcome: Outcome) -> bool:
    value: object = [None, False] if outcome == "true" else [None, None]
    rule_value: object = True
    if operation == "required":
        value = {"a": False}
        rule_value = ["a"] if outcome == "true" else ["b"]
    elif operation == "dependent":
        value = {"a": False}
        rule_value = {"a": []} if outcome == "true" else {"a": ["b"]}
    meter = remaining_meter("work", 80) if outcome == "refusal" else EvaluationMeter()
    borrowed, rule = predicate_arguments(operation, value, rule_value, meter)
    if outcome == "unexpected":
        rows = list(rule.nodes)
        if operation == "required":
            rows[rule.node] = replace(rows[rule.node], children=(len(rows),))
            rule = ArenaCursor(tuple(rows), rule.node)
        elif operation == "dependent":
            key, _ = rows[rule.node].members[0]
            rows[rule.node] = replace(rows[rule.node], members=((key, len(rows)),))
            rule = ArenaCursor(tuple(rows), rule.node)
        else:
            rule = ArenaCursor(rule.nodes, len(rule.nodes))
    return predicate(operation, borrowed, rule, meter)


@pytest.mark.parametrize("operation", ["unique", "required", "dependent"])
@pytest.mark.parametrize("outcome", ["true", "false", "refusal", "unexpected"])
def test_collection_predicates_release_state_on_every_exit(
    operation: Operation, outcome: Outcome
) -> None:
    gc.collect()
    before = tracked_equality_owners()

    if outcome in ("true", "false"):
        matched = owned_predicate(operation, outcome)
        gc.collect()
        remaining = tracked_equality_owners()

        assert matched is (outcome == "true")
        assert remaining == before
        return
    expected = EvaluationRefused if outcome == "refusal" else IndexError
    with pytest.raises(expected) as rejected:
        owned_predicate(operation, outcome)
    gc.collect()
    held = tracked_equality_owners() - before
    if outcome == "refusal":
        assert str(rejected.value) == "evaluation_exhausted"
    del rejected
    gc.collect()
    remaining = tracked_equality_owners()

    assert held
    assert remaining == before


@pytest.mark.parametrize("operation", ["required", "dependent"])
@pytest.mark.parametrize("outcome", ["false", "refusal", "unexpected"])
def test_borrowed_index_survives_every_nontrue_exit_unchanged(
    operation: Operation, outcome: Outcome
) -> None:
    meter = (
        remaining_meter("entries", 8 if operation == "required" else 9)
        if outcome == "refusal"
        else EvaluationMeter()
    )
    rule_value: object = ["b"] if operation == "required" else {"a": ["b"]}
    borrowed, rule = predicate_arguments(operation, {"a": False}, rule_value, meter)
    index = cast("NativeObjectIndex", borrowed)
    original_row = index.row(0, meter)
    if outcome == "unexpected":
        rows = list(rule.nodes)
        if operation == "required":
            rows[rule.node] = replace(rows[rule.node], children=(len(rows),))
        else:
            key, _ = rows[rule.node].members[0]
            rows[rule.node] = replace(rows[rule.node], members=((key, len(rows)),))
        rule = ArenaCursor(tuple(rows), rule.node)
    gc.collect()
    before = tracked_equality_owners()

    if outcome == "false":
        matched = predicate(operation, index, rule, meter)
        gc.collect()
        remaining = tracked_equality_owners()
        retained_row = index.row(0, meter)

        assert not matched
        assert retained_row is original_row
        assert retained_row[1] is False
        assert remaining == before
        return
    expected = EvaluationRefused if outcome == "refusal" else IndexError
    with pytest.raises(expected) as rejected:
        predicate(operation, index, rule, meter)
    del rejected
    gc.collect()
    remaining = tracked_equality_owners()
    retained_row = index.row(0, meter)

    assert retained_row is original_row
    assert retained_row[1] is False
    assert remaining == before
