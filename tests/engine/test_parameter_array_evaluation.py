from dataclasses import replace
from traceback import format_exception, walk_tb

import pytest

from kora.engine.parameter_schema_evaluation import (
    ParameterSchemaEvaluationFailure,
    ParameterSchemaEvaluationRejected,
    evaluate_parameter_schema,
)
from kora.ir.structured_schema_evaluation_work import (
    BoundedEvaluationMeter,
    EvaluationLimits,
)
from tests.engine.test_parameter_schema_evaluation import (
    WIDE_LIMITS,
    checked_schema,
    owned_continuations,
)

PRIVATE = "private-parameter-array-evaluation"
DEFERRED = {"$ref": "#"}


@pytest.mark.parametrize(
    ("value", "schema", "expected"),
    [
        (
            ["first", 2, 3],
            {"prefixItems": [{"type": "string"}], "items": {"type": "integer"}},
            True,
        ),
        (
            ["first", 2, "tail"],
            {"prefixItems": [{"type": "string"}], "items": {"type": "integer"}},
            False,
        ),
        (
            [0, "a", 0],
            {"prefixItems": [{"const": 0}, {"type": "string"}], "items": {"const": 0}},
            True,
        ),
        ([0], {"prefixItems": [{"const": 0}, DEFERRED], "items": {"$ref": ""}}, True),
        ([0, "a"], {"prefixItems": [False, DEFERRED], "items": {"$ref": ""}}, False),
        ([1, 2], {"prefixItems": [{"const": 1}]}, True),
        ([1, 2], {"items": {"allOf": [{"type": "integer"}, {"minimum": 1}]}}, True),
        ([[1], [2]], {"items": {"items": {"type": "integer"}}}, True),
        ([list[object]()], {"items": {"items": False}}, True),
        ([["x"]], {"items": {"items": {"type": "integer"}}}, False),
        ([1, 2], {"prefixItems": [True], "items": False}, False),
        (list[object](), {"prefixItems": [DEFERRED], "items": {"$ref": ""}}, True),
        ("not an array", {"prefixItems": "unentered", "items": DEFERRED}, True),
    ],
)
def test_parameter_items_use_exact_elements_and_skip_unused_operands(
    value: object, schema: object, expected: bool
) -> None:
    rule, instance = checked_schema(value, schema)
    meter = BoundedEvaluationMeter(limits=WIDE_LIMITS)

    matches = evaluate_parameter_schema(
        rule, instance, meter=meter, max_schema_frames=8, max_pair_frames=8
    )

    assert matches is expected


@pytest.mark.parametrize(
    ("value", "schema", "expected"),
    [
        ([], {"contains": DEFERRED}, False),
        ([], {"contains": DEFERRED, "minContains": 0, "maxContains": 0}, True),
        ([0, 1], {"contains": DEFERRED, "minContains": 0}, True),
        ([0, 1], {"contains": DEFERRED, "minContains": 0, "maxContains": 2}, True),
        ([0, 1], {"contains": DEFERRED, "minContains": 3}, False),
        ([0, 1], {"contains": DEFERRED, "minContains": 2, "maxContains": 1}, False),
        ([1, "unvisited"], {"contains": {"type": "integer"}}, True),
        (
            [1, 0, 0],
            {"contains": {"const": 1}, "minContains": 1.0, "maxContains": 1.0},
            True,
        ),
        ([1], {"contains": {"const": 1}, "maxContains": 1e23}, True),
        (
            [False, False],
            {"contains": {"const": True}, "minContains": 0, "maxContains": 0},
            True,
        ),
        (
            {"x": 1},
            {"contains": DEFERRED, "minContains": PRIVATE, "maxContains": PRIVATE},
            True,
        ),
        ([1], {"minContains": 3, "maxContains": 0}, True),
    ],
)
def test_parameter_contains_evaluates_exact_bounds_and_initial_interval(
    value: object, schema: object, expected: bool
) -> None:
    rule, instance = checked_schema(value, schema)
    meter = BoundedEvaluationMeter(limits=WIDE_LIMITS)

    matches = evaluate_parameter_schema(
        rule, instance, meter=meter, max_schema_frames=4, max_pair_frames=2
    )

    assert matches is expected


@pytest.mark.parametrize(
    ("value", "minimum", "maximum", "expected"),
    [
        ([0, 99, 99], 0, 2, True),
        ([0, 99, 99], 3, 3, False),
        ([1, 1, 99], 1, 1, False),
    ],
    ids=["remaining-cannot-exceed-max", "remaining-cannot-meet-min", "max-exceeded"],
)
def test_parameter_contains_settles_after_each_completed_element(
    value: object, minimum: int, maximum: int, expected: bool
) -> None:
    rule, instance = checked_schema(
        value,
        {
            "contains": {"if": {"const": 99}, "then": DEFERRED, "else": {"const": 1}},
            "minContains": minimum,
            "maxContains": maximum,
        },
    )
    meter = BoundedEvaluationMeter(limits=WIDE_LIMITS)

    matches = evaluate_parameter_schema(
        rule, instance, meter=meter, max_schema_frames=4, max_pair_frames=2
    )

    assert matches is expected


def test_parameter_contains_detects_wide_reversed_bounds_before_interval_work() -> None:
    reversed_rule, instance = checked_schema(
        [0], {"contains": DEFERRED, "minContains": 10**23 + 1, "maxContains": 1e23}
    )
    equal_rule, _ = checked_schema(
        [0], {"contains": DEFERRED, "minContains": 10**23, "maxContains": 1e23}
    )
    limits = EvaluationLimits(5_377, 180, 13, 128, 128)
    decided = BoundedEvaluationMeter(limits=limits)
    refused = BoundedEvaluationMeter(limits=limits)

    matches = evaluate_parameter_schema(
        reversed_rule, instance, meter=decided, max_schema_frames=1, max_pair_frames=0
    )
    with pytest.raises(ParameterSchemaEvaluationRejected) as refusal:
        evaluate_parameter_schema(
            equal_rule, instance, meter=refused, max_schema_frames=1, max_pair_frames=0
        )

    assert matches is False
    assert decided.work == limits.max_work
    assert refusal.value.reason is ParameterSchemaEvaluationFailure.CAPACITY


@pytest.mark.parametrize(
    ("value", "schema", "work", "payload", "entries"),
    [
        ([1, 2], {"items": True}, 1_788, 0, 18),
        ([1], {"contains": True, "minContains": 1, "maxContains": 1}, 3_122, 4, 17),
    ],
    ids=["shared-tail", "contains-interval"],
)
def test_parameter_array_children_share_exact_work_and_entry_admission(
    value: object, schema: object, work: int, payload: int, entries: int
) -> None:
    rule, instance = checked_schema(value, schema)
    limits = EvaluationLimits(work, payload, entries, 64, 64)
    exact = BoundedEvaluationMeter(limits=limits)
    short = BoundedEvaluationMeter(limits=replace(limits, max_work=work - 1))
    short_entries = BoundedEvaluationMeter(
        limits=replace(limits, max_auxiliary_entries=entries - 1)
    )

    matches = evaluate_parameter_schema(
        rule, instance, meter=exact, max_schema_frames=2, max_pair_frames=0
    )
    with pytest.raises(ParameterSchemaEvaluationRejected) as refusal:
        evaluate_parameter_schema(
            rule, instance, meter=short, max_schema_frames=2, max_pair_frames=0
        )
    with pytest.raises(ParameterSchemaEvaluationRejected) as entry_refusal:
        evaluate_parameter_schema(
            rule, instance, meter=short_entries, max_schema_frames=2, max_pair_frames=0
        )

    assert matches is True
    assert (exact.work, exact.auxiliary_bytes, exact.auxiliary_entries) == (
        work,
        payload,
        entries,
    )
    assert refusal.value.reason is ParameterSchemaEvaluationFailure.CAPACITY
    assert entry_refusal.value.reason is ParameterSchemaEvaluationFailure.CAPACITY


@pytest.mark.parametrize(
    ("value", "schema"),
    [
        ([1], {"oneOf": [True, True], "prefixItems": [DEFERRED]}),
        ([1, 0], {"prefixItems": [DEFERRED], "items": False}),
        ([1, 0], {"prefixItems": [True], "items": DEFERRED}),
        (
            [99, 1],
            {"contains": {"if": {"const": 99}, "then": DEFERRED, "else": {"const": 1}}},
        ),
    ],
)
def test_parameter_array_evaluation_propagates_entered_refusal_in_order(
    value: object, schema: object
) -> None:
    rule, instance = checked_schema(value, schema)
    meter = BoundedEvaluationMeter(limits=WIDE_LIMITS)

    with pytest.raises(ParameterSchemaEvaluationRejected) as refusal:
        evaluate_parameter_schema(
            rule, instance, meter=meter, max_schema_frames=4, max_pair_frames=2
        )

    assert refusal.value.reason is ParameterSchemaEvaluationFailure.UNAVAILABLE


@pytest.mark.parametrize(
    "schema",
    [
        {"contains": True, "minContains": -1},
        {"contains": True, "maxContains": 1.5},
        {"contains": True, "maxContains": False},
        {"prefixItems": PRIVATE},
    ],
)
def test_parameter_array_evaluation_refuses_entered_malformed_bounds(
    schema: object,
) -> None:
    rule, instance = checked_schema([1], schema)
    meter = BoundedEvaluationMeter(limits=WIDE_LIMITS)

    with pytest.raises(ParameterSchemaEvaluationRejected) as refusal:
        evaluate_parameter_schema(
            rule, instance, meter=meter, max_schema_frames=4, max_pair_frames=2
        )

    assert refusal.value.reason is ParameterSchemaEvaluationFailure.INVALID


def test_array_frame_refusal_releases_private_state() -> None:
    rule, instance = checked_schema([PRIVATE], {"items": {"const": PRIVATE}})
    meter = BoundedEvaluationMeter(limits=WIDE_LIMITS)
    before = owned_continuations()

    try:
        raise RuntimeError(PRIVATE)
    except RuntimeError:
        with pytest.raises(ParameterSchemaEvaluationRejected) as refusal:
            evaluate_parameter_schema(
                rule, instance, meter=meter, max_schema_frames=1, max_pair_frames=1
            )

    frames = {
        frame.f_code.co_name: frame.f_locals
        for frame, _ in walk_tb(refusal.value.__traceback__)
    }
    assert refusal.value.reason is ParameterSchemaEvaluationFailure.CAPACITY
    assert refusal.value.__cause__ is refusal.value.__context__ is None
    assert frames["evaluate_parameter_schema"] == {
        "reason": ParameterSchemaEvaluationFailure.CAPACITY
    }
    assert {"_evaluate", "run", "_push", "_visit", "_items", "_contains"}.isdisjoint(
        frames
    )
    assert PRIVATE not in "".join(format_exception(refusal.value))
    assert owned_continuations() == before
