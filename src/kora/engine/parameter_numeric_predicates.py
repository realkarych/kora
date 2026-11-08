import math
from enum import StrEnum

from kora.cel_eval.errors import raise_safe
from kora.ir.structured_schema_evaluation_policy import EvaluationRefused
from kora.ir.structured_schema_evaluation_work import BoundedEvaluationMeter
from kora.ir.structured_schema_numbers import (
    numeric_compare,
    numeric_equal,
    numeric_is_integer,
    numeric_multiple_of,
)


class ParameterNumericPredicate(StrEnum):
    INTEGER = "integer"
    EQUAL = "equal"
    MINIMUM = "minimum"
    EXCLUSIVE_MINIMUM = "exclusiveMinimum"
    MAXIMUM = "maximum"
    EXCLUSIVE_MAXIMUM = "exclusiveMaximum"
    MULTIPLE_OF = "multipleOf"


class ParameterNumericFailure(StrEnum):
    INVALID = "invalid parameter numeric operand"
    CAPACITY = "parameter numeric evaluation capacity exceeded"


class ParameterNumericRejected(ValueError):
    def __init__(self, reason: ParameterNumericFailure) -> None:
        self.reason: ParameterNumericFailure = reason
        super().__init__(reason.value)


def _is_number(value: object) -> bool:
    kind = type(value)
    if kind is int:
        return True
    if type(value) is float:
        if not math.isfinite(value):
            raise ParameterNumericRejected(ParameterNumericFailure.INVALID)
        return True
    if value is None or kind is bool or kind is str or kind is list or kind is dict:
        return False
    raise ParameterNumericRejected(ParameterNumericFailure.INVALID)


def _check(
    predicate: ParameterNumericPredicate,
    instance: object,
    operand: object,
    meter: BoundedEvaluationMeter,
) -> bool:
    if type(meter) is not BoundedEvaluationMeter:
        raise ParameterNumericRejected(ParameterNumericFailure.INVALID)
    meter.reserve()
    if type(predicate) is not ParameterNumericPredicate:
        raise ParameterNumericRejected(ParameterNumericFailure.INVALID)
    is_number = _is_number(instance)
    if predicate is ParameterNumericPredicate.INTEGER:
        if operand is not None:
            raise ParameterNumericRejected(ParameterNumericFailure.INVALID)
        return is_number and numeric_is_integer(instance, meter)
    if predicate is ParameterNumericPredicate.EQUAL:
        operand_is_number = _is_number(operand)
        return (
            is_number and operand_is_number and numeric_equal(instance, operand, meter)
        )
    if not is_number:
        return True
    if not _is_number(operand):
        raise ParameterNumericRejected(ParameterNumericFailure.INVALID)
    if predicate is ParameterNumericPredicate.MULTIPLE_OF:
        return numeric_multiple_of(instance, operand, meter)
    comparison = numeric_compare(instance, operand, meter)
    match predicate:
        case ParameterNumericPredicate.MINIMUM:
            return comparison >= 0
        case ParameterNumericPredicate.EXCLUSIVE_MINIMUM:
            return comparison > 0
        case ParameterNumericPredicate.MAXIMUM:
            return comparison <= 0
        case ParameterNumericPredicate.EXCLUSIVE_MAXIMUM:
            return comparison < 0
    raise ParameterNumericRejected(ParameterNumericFailure.INVALID)


def check_parameter_numeric_predicate(
    predicate: ParameterNumericPredicate,
    instance: object,
    operand: object,
    *,
    meter: BoundedEvaluationMeter,
) -> bool:
    try:
        return _check(predicate, instance, operand, meter)
    except ParameterNumericRejected as rejected:
        reason = rejected.reason
    except EvaluationRefused as rejected:
        reason = (
            ParameterNumericFailure.CAPACITY
            if rejected.reason == "evaluation_exhausted"
            else ParameterNumericFailure.INVALID
        )
    except MemoryError:
        reason = ParameterNumericFailure.CAPACITY
    del predicate, instance, operand, meter
    raise_safe(ParameterNumericRejected(reason))
