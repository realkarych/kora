import sys
from collections.abc import Callable
from enum import StrEnum

from kora.cel_eval.errors import raise_safe
from kora.ir.structured_schema_assertions import (
    check_enum_membership,
    max_items_matches,
    max_length_matches,
    max_properties_matches,
    min_items_matches,
    min_length_matches,
    min_properties_matches,
    type_matches,
)
from kora.ir.structured_schema_collection_predicates import (
    check_dependent_required_properties,
    check_required_properties,
    check_unique_items,
)
from kora.ir.structured_schema_cursors import ArenaCursor, valid_arena_cursor
from kora.ir.structured_schema_equality import compare_json_cursors
from kora.ir.structured_schema_evaluation_policy import EvaluationRefused
from kora.ir.structured_schema_evaluation_work import BoundedEvaluationMeter


class ParameterArenaPredicate(StrEnum):
    CONST = "const"
    ENUM = "enum"
    UNIQUE_ITEMS = "uniqueItems"
    TYPE = "type"
    MIN_LENGTH = "minLength"
    MAX_LENGTH = "maxLength"
    MIN_ITEMS = "minItems"
    MAX_ITEMS = "maxItems"
    MIN_PROPERTIES = "minProperties"
    MAX_PROPERTIES = "maxProperties"
    REQUIRED = "required"
    DEPENDENT_REQUIRED = "dependentRequired"


_DIRECT_HANDLERS: dict[
    ParameterArenaPredicate,
    Callable[[ArenaCursor, ArenaCursor, BoundedEvaluationMeter], bool],
] = {
    ParameterArenaPredicate.TYPE: type_matches,
    ParameterArenaPredicate.MIN_LENGTH: min_length_matches,
    ParameterArenaPredicate.MAX_LENGTH: max_length_matches,
    ParameterArenaPredicate.MIN_ITEMS: min_items_matches,
    ParameterArenaPredicate.MAX_ITEMS: max_items_matches,
    ParameterArenaPredicate.MIN_PROPERTIES: min_properties_matches,
    ParameterArenaPredicate.MAX_PROPERTIES: max_properties_matches,
    ParameterArenaPredicate.REQUIRED: check_required_properties,
    ParameterArenaPredicate.DEPENDENT_REQUIRED: check_dependent_required_properties,
}


class ParameterArenaFailure(StrEnum):
    INVALID = "invalid parameter arena predicate operand"
    CAPACITY = "parameter arena predicate evaluation capacity exceeded"


class ParameterArenaRejected(ValueError):
    def __init__(self, reason: ParameterArenaFailure) -> None:
        self.reason: ParameterArenaFailure = reason
        super().__init__(reason.value)


def _check(
    predicate: ParameterArenaPredicate,
    instance: ArenaCursor,
    operand: ArenaCursor,
    meter: BoundedEvaluationMeter,
    max_pair_frames: int,
) -> bool:
    if type(meter) is not BoundedEvaluationMeter:
        raise ParameterArenaRejected(ParameterArenaFailure.INVALID)
    meter.reserve()
    if (
        type(predicate) is not ParameterArenaPredicate
        or type(max_pair_frames) is not int
        or not 0 <= max_pair_frames <= sys.maxsize
        or not valid_arena_cursor(instance)
        or not valid_arena_cursor(operand)
    ):
        raise ParameterArenaRejected(ParameterArenaFailure.INVALID)
    match predicate:
        case ParameterArenaPredicate.CONST:
            return compare_json_cursors(
                instance, operand, meter, max_pair_frames=max_pair_frames
            )
        case ParameterArenaPredicate.ENUM:
            return check_enum_membership(
                instance, operand, meter, max_pair_frames=max_pair_frames
            )
        case ParameterArenaPredicate.UNIQUE_ITEMS:
            return check_unique_items(
                instance, operand, meter, max_pair_frames=max_pair_frames
            )
        case _:
            return _DIRECT_HANDLERS[predicate](instance, operand, meter)


def check_parameter_arena_predicate(
    predicate: ParameterArenaPredicate,
    instance: ArenaCursor,
    operand: ArenaCursor,
    *,
    meter: BoundedEvaluationMeter,
    max_pair_frames: int,
) -> bool:
    try:
        return _check(predicate, instance, operand, meter, max_pair_frames)
    except ParameterArenaRejected as rejected:
        reason = rejected.reason
    except EvaluationRefused as rejected:
        reason = (
            ParameterArenaFailure.CAPACITY
            if rejected.reason == "evaluation_exhausted"
            else ParameterArenaFailure.INVALID
        )
    except MemoryError:
        reason = ParameterArenaFailure.CAPACITY
    del predicate, instance, operand, meter, max_pair_frames
    raise_safe(ParameterArenaRejected(reason))
