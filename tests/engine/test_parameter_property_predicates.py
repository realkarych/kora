from dataclasses import replace
from traceback import format_exception, walk_tb

import pytest

from kora.engine.parameter_arena_predicates import (
    ParameterArenaFailure,
    ParameterArenaPredicate,
    ParameterArenaRejected,
    check_parameter_arena_predicate,
)
from kora.ir.json import JsonObject
from kora.ir.structured_schema_cursors import arena_object_lookup
from kora.ir.structured_schema_evaluation_work import (
    BoundedEvaluationMeter,
    EvaluationLimits,
)
from tests.engine.test_parameter_equality_predicates import LIMITS, checked_pair

PRIVATE = "private-property-predicate"
LONG_KEY = "z" * 600
LOOKUP_VALUE: JsonObject = {
    "prefix-z": 0.3,
    "prefix-a": None,
    "prefix-x": False,
    "prefix-c": {},
}


@pytest.mark.parametrize(
    ("predicate", "value", "operand", "expected"),
    [
        (ParameterArenaPredicate.REQUIRED, {"é": None}, ["e\u0301"], False),
        (ParameterArenaPredicate.REQUIRED, {"é": False}, ["é"], True),
        (
            ParameterArenaPredicate.REQUIRED,
            {"\U0001f600": False, "\x00": None, "\U00010000": []},
            ["\U0001f600", "\x00", "\U00010000"],
            True,
        ),
        (ParameterArenaPredicate.REQUIRED, {}, [], True),
        (ParameterArenaPredicate.REQUIRED, [], PRIVATE, True),
        (
            ParameterArenaPredicate.DEPENDENT_REQUIRED,
            {"trigger": None, "\x00": False},
            {"absent": PRIVATE, "trigger": ["\x00"]},
            True,
        ),
        (
            ParameterArenaPredicate.DEPENDENT_REQUIRED,
            {"trigger": False},
            {"trigger": ["needed"]},
            False,
        ),
        (
            ParameterArenaPredicate.DEPENDENT_REQUIRED,
            {"trigger": None},
            {"trigger": ["needed"]},
            False,
        ),
        (ParameterArenaPredicate.DEPENDENT_REQUIRED, {}, {"absent": PRIVATE}, True),
        (ParameterArenaPredicate.DEPENDENT_REQUIRED, None, PRIVATE, True),
    ],
)
def test_parameter_property_requirements_use_exact_applicable_presence(
    predicate: ParameterArenaPredicate, value: object, operand: object, expected: bool
) -> None:
    instance, schema = checked_pair(predicate, value, operand)
    meter = BoundedEvaluationMeter(limits=LIMITS)

    matches = check_parameter_arena_predicate(
        predicate, instance, schema, meter=meter, max_pair_frames=0
    )

    assert matches is expected


def test_parameter_required_preserves_saved_name_order_under_budget() -> None:
    predicate = ParameterArenaPredicate.REQUIRED
    value = {LONG_KEY: None}
    instance, missing_first = checked_pair(predicate, value, ["missing", LONG_KEY])
    _, costly_first = checked_pair(predicate, value, [LONG_KEY, "missing"])
    limits = replace(LIMITS, max_work=300)
    decisive = BoundedEvaluationMeter(limits=limits)
    refused = BoundedEvaluationMeter(limits=limits)

    matches = check_parameter_arena_predicate(
        predicate, instance, missing_first, meter=decisive, max_pair_frames=0
    )
    with pytest.raises(ParameterArenaRejected) as rejection:
        check_parameter_arena_predicate(
            predicate, instance, costly_first, meter=refused, max_pair_frames=0
        )

    assert matches is False
    assert rejection.value.reason is ParameterArenaFailure.CAPACITY


def test_parameter_dependent_requirements_use_canonical_trigger_order() -> None:
    predicate = ParameterArenaPredicate.DEPENDENT_REQUIRED
    value = {LONG_KEY: False, "a": None}
    instance, decisive_first = checked_pair(
        predicate, value, {LONG_KEY: ["unentered"], "a": ["missing"]}
    )
    _, reaches_costly_trigger = checked_pair(
        predicate, value, {LONG_KEY: ["unentered"], "a": []}
    )
    limits = replace(LIMITS, max_work=300)
    decisive = BoundedEvaluationMeter(limits=limits)
    refused = BoundedEvaluationMeter(limits=limits)

    matches = check_parameter_arena_predicate(
        predicate, instance, decisive_first, meter=decisive, max_pair_frames=0
    )
    with pytest.raises(ParameterArenaRejected) as rejection:
        check_parameter_arena_predicate(
            predicate,
            instance,
            reaches_costly_trigger,
            meter=refused,
            max_pair_frames=0,
        )

    assert matches is False
    assert rejection.value.reason is ParameterArenaFailure.CAPACITY


def test_parameter_property_lookup_prepays_exact_late_row_comparisons() -> None:
    predicate = ParameterArenaPredicate.REQUIRED
    instance, schema = checked_pair(predicate, LOOKUP_VALUE, ["prefix-z"])
    exact = BoundedEvaluationMeter(limits=EvaluationLimits(162, 0, 2, 64, 64))
    final_short = BoundedEvaluationMeter(limits=EvaluationLimits(161, 0, 2, 64, 64))
    comparison_short = BoundedEvaluationMeter(
        limits=EvaluationLimits(158, 0, 2, 64, 64)
    )

    matches = check_parameter_arena_predicate(
        predicate, instance, schema, meter=exact, max_pair_frames=0
    )
    with pytest.raises(ParameterArenaRejected) as rejection:
        check_parameter_arena_predicate(
            predicate, instance, schema, meter=comparison_short, max_pair_frames=0
        )
    with pytest.raises(ParameterArenaRejected) as final_rejection:
        check_parameter_arena_predicate(
            predicate, instance, schema, meter=final_short, max_pair_frames=0
        )

    assert matches is True
    assert (exact.work, exact.auxiliary_bytes, exact.auxiliary_entries) == (162, 0, 2)
    assert comparison_short.work == 150
    assert final_short.work == 161
    assert rejection.value.reason is ParameterArenaFailure.CAPACITY
    assert final_rejection.value.reason is ParameterArenaFailure.CAPACITY


def test_parameter_property_lookup_returns_sorted_member_ordinals() -> None:
    predicate = ParameterArenaPredicate.REQUIRED
    instance, _ = checked_pair(predicate, LOOKUP_VALUE, [])
    meter = BoundedEvaluationMeter(limits=LIMITS)
    lookup = arena_object_lookup(instance, meter)
    assert lookup is not None

    ordinals = [lookup.find(key, meter) for key in ("prefix-z", "prefix-y", "z")]

    assert ordinals == [3, -1, -1]


@pytest.mark.parametrize(
    ("predicate", "operand"),
    [
        (ParameterArenaPredicate.REQUIRED, [False]),
        (ParameterArenaPredicate.DEPENDENT_REQUIRED, {"trigger": PRIVATE}),
    ],
)
def test_parameter_entered_malformed_property_requirement_clears_context(
    predicate: ParameterArenaPredicate, operand: object
) -> None:
    instance, schema = checked_pair(predicate, {"trigger": PRIVATE}, operand)
    meter = BoundedEvaluationMeter(limits=LIMITS)

    try:
        raise RuntimeError(PRIVATE)
    except RuntimeError:
        with pytest.raises(ParameterArenaRejected) as rejection:
            check_parameter_arena_predicate(
                predicate, instance, schema, meter=meter, max_pair_frames=0
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
    assert {"_check", "_required_names_present", "find"}.isdisjoint(frames)
    assert PRIVATE not in "".join(format_exception(rejection.value))
