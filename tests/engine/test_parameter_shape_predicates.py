from dataclasses import replace
from traceback import format_exception, walk_tb
from typing import cast

import pytest

from kora.engine.parameter_arena_predicates import (
    ParameterArenaFailure,
    ParameterArenaPredicate,
    ParameterArenaRejected,
    check_parameter_arena_predicate,
)
from kora.ir.structured_schema_evaluation_work import (
    BoundedEvaluationMeter,
    EvaluationLimits,
)
from tests.engine.test_parameter_equality_predicates import LIMITS, checked_pair

PRIVATE = "private-parameter-shape-operand"
UNICODE_TEXT = "a\U0001f600e\u0301"


@pytest.mark.parametrize(
    ("value", "type_operand", "expected"),
    [
        (True, "boolean", True),
        (True, "number", False),
        (None, "null", True),
        (1.0, "integer", True),
        (1.1, "integer", False),
        (1.1, "number", True),
        ("text", "string", True),
        ([], "array", True),
        ({}, "object", True),
        ("text", ["array", "string"], True),
        (None, ["number", "integer"], False),
    ],
)
def test_parameter_type_uses_exact_json_kinds(
    value: object, type_operand: object, expected: bool
) -> None:
    predicate = ParameterArenaPredicate.TYPE
    instance, operand = checked_pair(predicate, value, type_operand)
    meter = BoundedEvaluationMeter(limits=LIMITS)

    matches = check_parameter_arena_predicate(
        predicate, instance, operand, meter=meter, max_pair_frames=0
    )

    assert matches is expected


def test_parameter_type_alternatives_follow_saved_order_on_capacity_refusal() -> None:
    predicate = ParameterArenaPredicate.TYPE
    instance, cheap_first = checked_pair(predicate, 1.0, ["number", "integer"])
    _, costly_first = checked_pair(predicate, 1.0, ["integer", "number"])
    limits = replace(LIMITS, max_work=300)
    admitted = BoundedEvaluationMeter(limits=limits)
    refused = BoundedEvaluationMeter(limits=limits)

    matches = check_parameter_arena_predicate(
        predicate, instance, cheap_first, meter=admitted, max_pair_frames=0
    )
    with pytest.raises(ParameterArenaRejected) as rejection:
        check_parameter_arena_predicate(
            predicate, instance, costly_first, meter=refused, max_pair_frames=0
        )

    assert matches is True
    assert rejection.value.reason is ParameterArenaFailure.CAPACITY


@pytest.mark.parametrize(
    ("predicate", "value", "bound", "expected"),
    [
        (ParameterArenaPredicate.MIN_LENGTH, UNICODE_TEXT, 4, True),
        (ParameterArenaPredicate.MAX_LENGTH, UNICODE_TEXT, 4.0, True),
        (ParameterArenaPredicate.MIN_LENGTH, UNICODE_TEXT, 5, False),
        (ParameterArenaPredicate.MAX_LENGTH, UNICODE_TEXT, 3, False),
        (ParameterArenaPredicate.MIN_ITEMS, [[1, 2], None], 2, True),
        (ParameterArenaPredicate.MAX_ITEMS, [[1, 2], None], 2, True),
        (ParameterArenaPredicate.MIN_ITEMS, [[1, 2], None], 3, False),
        (ParameterArenaPredicate.MAX_ITEMS, [[1, 2], None], 1, False),
        (ParameterArenaPredicate.MIN_PROPERTIES, {"\x00": [1], "é": {}}, 2, True),
        (ParameterArenaPredicate.MAX_PROPERTIES, {"\x00": [1], "é": {}}, 2, True),
        (ParameterArenaPredicate.MIN_PROPERTIES, {"a": 1}, 10**400, False),
        (ParameterArenaPredicate.MAX_PROPERTIES, {"a": 1, "b": 2}, 1, False),
        (ParameterArenaPredicate.MAX_ITEMS, [False], 10**400, True),
    ],
)
def test_parameter_size_assertions_compare_exact_inclusive_counts(
    predicate: ParameterArenaPredicate, value: object, bound: object, expected: bool
) -> None:
    instance, operand = checked_pair(predicate, value, bound)
    meter = BoundedEvaluationMeter(limits=LIMITS)

    matches = check_parameter_arena_predicate(
        predicate, instance, operand, meter=meter, max_pair_frames=0
    )

    assert matches is expected


@pytest.mark.parametrize(
    ("predicate", "value"),
    [
        (ParameterArenaPredicate.MIN_LENGTH, []),
        (ParameterArenaPredicate.MAX_LENGTH, None),
        (ParameterArenaPredicate.MIN_ITEMS, {}),
        (ParameterArenaPredicate.MAX_ITEMS, False),
        (ParameterArenaPredicate.MIN_PROPERTIES, []),
        (ParameterArenaPredicate.MAX_PROPERTIES, "text"),
    ],
)
def test_parameter_size_assertions_do_not_enter_inapplicable_operands(
    predicate: ParameterArenaPredicate, value: object
) -> None:
    instance, operand = checked_pair(predicate, value, PRIVATE)
    meter = BoundedEvaluationMeter(limits=LIMITS)

    matches = check_parameter_arena_predicate(
        predicate, instance, operand, meter=meter, max_pair_frames=0
    )

    assert matches is True


def test_parameter_item_count_uses_exact_work_without_visiting_children() -> None:
    predicate = ParameterArenaPredicate.MAX_ITEMS
    instance, operand = checked_pair(predicate, [0.3], 1)
    exact = BoundedEvaluationMeter(limits=EvaluationLimits(83, 0, 2, 64, 64))
    short = BoundedEvaluationMeter(limits=EvaluationLimits(82, 0, 2, 64, 64))

    matches = check_parameter_arena_predicate(
        predicate, instance, operand, meter=exact, max_pair_frames=0
    )
    with pytest.raises(ParameterArenaRejected) as rejection:
        check_parameter_arena_predicate(
            predicate, instance, operand, meter=short, max_pair_frames=0
        )

    assert matches is True
    assert (exact.work, exact.auxiliary_bytes, exact.auxiliary_entries) == (83, 0, 2)
    assert rejection.value.reason is ParameterArenaFailure.CAPACITY


def test_parameter_shape_refuses_raw_string_dispatch_token() -> None:
    instance, operand = checked_pair(ParameterArenaPredicate.TYPE, 1, "integer")
    token = cast("ParameterArenaPredicate", "type")
    meter = BoundedEvaluationMeter(limits=LIMITS)

    with pytest.raises(ParameterArenaRejected) as rejection:
        check_parameter_arena_predicate(
            token, instance, operand, meter=meter, max_pair_frames=0
        )

    assert rejection.value.reason is ParameterArenaFailure.INVALID
    assert meter.work == 1


def test_parameter_shape_refusal_clears_private_operands_and_ambient_context() -> None:
    predicate = ParameterArenaPredicate.MIN_PROPERTIES
    instance, operand = checked_pair(predicate, {"a": PRIVATE}, PRIVATE)
    meter = BoundedEvaluationMeter(limits=LIMITS)

    try:
        raise RuntimeError(PRIVATE)
    except RuntimeError:
        with pytest.raises(ParameterArenaRejected) as rejection:
            check_parameter_arena_predicate(
                predicate, instance, operand, meter=meter, max_pair_frames=0
            )

    frames = {
        frame.f_code.co_name: frame.f_locals
        for frame, _ in walk_tb(rejection.value.__traceback__)
    }
    assert rejection.value.reason is ParameterArenaFailure.INVALID
    assert rejection.value.__cause__ is rejection.value.__context__ is None
    assert frames["check_parameter_arena_predicate"] == {
        "reason": ParameterArenaFailure.INVALID
    }
    assert {"_check", "_container_count_compare"}.isdisjoint(frames)
    assert PRIVATE not in "".join(format_exception(rejection.value))
