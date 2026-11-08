import math
import sys
from dataclasses import dataclass
from enum import StrEnum

from kora.cel_eval.errors import raise_safe
from kora.db.graph_value_json import quoted_character_width
from kora.engine.parameter_schema_references import (
    ParameterReferenceBudget,
    ParameterReferenceFailure,
    ParameterReferenceRejected,
    valid_parameter_reference_budget,
)
from kora.ir.structured_schema_input import SchemaNode
from kora.ir.structured_schema_work import (
    charged_finite_float_text,
    charged_integer_text,
)


class ParameterScalarFailure(StrEnum):
    INVALID = "invalid pinned parameter schema scalar"
    NONFINITE = "nonfinite pinned parameter schema float"
    CAPACITY = "pinned parameter schema scalar capacity exceeded"


class ParameterScalarRejected(ValueError):
    def __init__(self, reason: ParameterScalarFailure) -> None:
        self.reason: ParameterScalarFailure = reason
        super().__init__(reason.value)


@dataclass(slots=True)
class _SpellingMeter:
    budget: ParameterReferenceBudget

    def reserve(self, work: int = 1, byte_count: int = 0, entries: int = 0) -> None:
        self.budget.reserve(work, 2 * byte_count + 2 * entries, 5 * entries)

    def result(self, size: int) -> None:
        self.reserve(1 + size, size)


def _integer(value: int, max_bits: int, budget: ParameterReferenceBudget) -> SchemaNode:
    budget.reserve(2)
    bits = value.bit_length()
    if bits > max_bits:
        raise ParameterScalarRejected(ParameterScalarFailure.CAPACITY)
    budget.reserve(2, byte_count=2 * ((bits + 7) // 8 + 1), entries=1)
    spelling = charged_integer_text(value, bits, _SpellingMeter(budget))
    return SchemaNode("scalar", value, size=len(spelling), spelling=spelling)


def _float(value: float, budget: ParameterReferenceBudget) -> SchemaNode:
    budget.reserve(2)
    if not math.isfinite(value):
        raise ParameterScalarRejected(ParameterScalarFailure.NONFINITE)
    budget.reserve(2, byte_count=10, entries=1)
    spelling = charged_finite_float_text(value, _SpellingMeter(budget))
    return SchemaNode("scalar", value, size=len(spelling), spelling=spelling)


def _prepare(
    value: object, max_integer_bits: int, budget: ParameterReferenceBudget
) -> SchemaNode:
    if (
        not valid_parameter_reference_budget(budget)
        or type(max_integer_bits) is not int
        or not 0 <= max_integer_bits <= sys.maxsize
    ):
        raise ParameterScalarRejected(ParameterScalarFailure.INVALID)
    if type(value) is str:
        length = len(value)
        budget.reserve(2 + length, byte_count=4 * (length + 1), entries=1)
        width = 2 + sum(quoted_character_width(character) for character in value)
        return SchemaNode("scalar", value, size=width)
    if type(value) is int:
        return _integer(value, max_integer_bits, budget)
    if type(value) is float:
        return _float(value, budget)
    if value is None or type(value) is bool:
        budget.reserve(entries=1)
        return SchemaNode("scalar", value, size=5 if value is False else 4)
    raise ParameterScalarRejected(ParameterScalarFailure.INVALID)


def prepare_parameter_schema_scalar(
    value: object, *, max_integer_bits: int, budget: ParameterReferenceBudget
) -> SchemaNode:
    try:
        return _prepare(value, max_integer_bits, budget)
    except ParameterScalarRejected as rejected:
        reason = rejected.reason
    except ParameterReferenceRejected as rejected:
        reason = (
            ParameterScalarFailure.CAPACITY
            if rejected.reason is ParameterReferenceFailure.CAPACITY
            else ParameterScalarFailure.INVALID
        )
    except MemoryError:
        reason = ParameterScalarFailure.CAPACITY
    del value, max_integer_bits, budget
    raise_safe(ParameterScalarRejected(reason))
