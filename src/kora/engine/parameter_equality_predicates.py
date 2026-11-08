import sys
from enum import StrEnum

from kora.cel_eval.errors import raise_safe
from kora.ir.structured_schema_assertions import check_enum_membership
from kora.ir.structured_schema_collection_predicates import check_unique_items
from kora.ir.structured_schema_cursors import ArenaCursor
from kora.ir.structured_schema_equality import compare_json_cursors
from kora.ir.structured_schema_evaluation_policy import EvaluationRefused
from kora.ir.structured_schema_evaluation_work import BoundedEvaluationMeter
from kora.ir.structured_schema_input import SchemaNode


class ParameterEqualityPredicate(StrEnum):
    CONST = "const"
    ENUM = "enum"
    UNIQUE_ITEMS = "uniqueItems"


class ParameterEqualityFailure(StrEnum):
    INVALID = "invalid parameter equality operand"
    CAPACITY = "parameter equality evaluation capacity exceeded"


class ParameterEqualityRejected(ValueError):
    def __init__(self, reason: ParameterEqualityFailure) -> None:
        self.reason: ParameterEqualityFailure = reason
        super().__init__(reason.value)


def _valid_cursor_shape(cursor: ArenaCursor) -> bool:
    return (
        type(cursor) is ArenaCursor
        and type(cursor.nodes) is tuple
        and type(cursor.node) is int
        and 0 <= cursor.node < len(cursor.nodes)
        and type(cursor.nodes[cursor.node]) is SchemaNode
    )


def _check(
    predicate: ParameterEqualityPredicate,
    instance: ArenaCursor,
    operand: ArenaCursor,
    meter: BoundedEvaluationMeter,
    max_pair_frames: int,
) -> bool:
    if type(meter) is not BoundedEvaluationMeter:
        raise ParameterEqualityRejected(ParameterEqualityFailure.INVALID)
    meter.reserve()
    if (
        type(predicate) is not ParameterEqualityPredicate
        or type(max_pair_frames) is not int
        or not 0 <= max_pair_frames <= sys.maxsize
        or not _valid_cursor_shape(instance)
        or not _valid_cursor_shape(operand)
    ):
        raise ParameterEqualityRejected(ParameterEqualityFailure.INVALID)
    match predicate:
        case ParameterEqualityPredicate.CONST:
            return compare_json_cursors(
                instance, operand, meter, max_pair_frames=max_pair_frames
            )
        case ParameterEqualityPredicate.ENUM:
            return check_enum_membership(
                instance, operand, meter, max_pair_frames=max_pair_frames
            )
        case ParameterEqualityPredicate.UNIQUE_ITEMS:
            return check_unique_items(
                instance, operand, meter, max_pair_frames=max_pair_frames
            )


def check_parameter_equality_predicate(
    predicate: ParameterEqualityPredicate,
    instance: ArenaCursor,
    operand: ArenaCursor,
    *,
    meter: BoundedEvaluationMeter,
    max_pair_frames: int,
) -> bool:
    try:
        return _check(predicate, instance, operand, meter, max_pair_frames)
    except ParameterEqualityRejected as rejected:
        reason = rejected.reason
    except EvaluationRefused as rejected:
        reason = (
            ParameterEqualityFailure.CAPACITY
            if rejected.reason == "evaluation_exhausted"
            else ParameterEqualityFailure.INVALID
        )
    except MemoryError:
        reason = ParameterEqualityFailure.CAPACITY
    del predicate, instance, operand, meter, max_pair_frames
    raise_safe(ParameterEqualityRejected(reason))
