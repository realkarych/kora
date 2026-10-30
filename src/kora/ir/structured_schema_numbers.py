import math
from dataclasses import dataclass
from typing import Literal

from kora.ir.structured_schema_evaluation_policy import PARAMETERS, refuse
from kora.ir.structured_schema_evaluation_work import EvaluationMeter


@dataclass(frozen=True, slots=True, repr=False)
class DecimalNumber:
    coefficient: int
    exponent: int

    def __repr__(self) -> str:
        return "DecimalNumber()"


def _float_number(value: float, meter: EvaluationMeter) -> DecimalNumber:
    meter.reserve(2)
    if not math.isfinite(value):
        refuse()
    meter.reserve(4096, 24)
    spelling = repr(value)
    meter.reserve(1 + len(spelling))
    if len(spelling) > 24:
        raise RuntimeError("invalid numerical spelling")
    meter.result(24)
    meter.reserve(entries=2)
    coefficient_digits = bytearray()
    exponent_digits = bytearray()
    negative = False
    exponent_negative = False
    scientific = False
    fractional = False
    fraction_digits = 0
    for character in spelling:
        if "0" <= character <= "9":
            if scientific:
                if len(exponent_digits) == 3:
                    raise RuntimeError("invalid numerical spelling")
                exponent_digits.append(ord(character))
            else:
                if len(coefficient_digits) == 21:
                    raise RuntimeError("invalid numerical spelling")
                coefficient_digits.append(ord(character))
                fraction_digits += int(fractional)
        elif character == ".":
            fractional = True
        elif character == "e":
            scientific = True
        elif character == "-":
            if scientific:
                exponent_negative = True
            else:
                negative = True
        elif character != "+":
            raise RuntimeError("invalid numerical spelling")
    coefficient = meter.parse_digits(coefficient_digits)
    meter.reserve()
    if scientific:
        exponent = meter.parse_digits(exponent_digits)
        meter.reserve()
        if exponent_negative:
            exponent = meter.integer_negate(exponent)
    else:
        exponent = 0
    meter.reserve()
    exponent -= fraction_digits
    meter.reserve()
    if negative:
        coefficient = meter.integer_negate(coefficient)
    if meter.integer_zero(coefficient):
        exponent = 0
    meter.reserve(4)
    if coefficient.bit_length() > 57 or not -340 <= exponent <= 308:
        raise RuntimeError("invalid numerical spelling")
    meter.reserve(entries=1)
    return DecimalNumber(coefficient, exponent)


def decimal_number(value: object, meter: EvaluationMeter) -> DecimalNumber:
    meter.reserve(2)
    if type(value) is int:
        meter.reserve(2)
        if value.bit_length() > PARAMETERS["max_numeric_bits"]:
            refuse()
        meter.reserve(entries=1)
        return DecimalNumber(value, 0)
    meter.reserve(2)
    if type(value) is float:
        return _float_number(value, meter)
    refuse()


def numeric_is_integer(value: object, meter: EvaluationMeter) -> bool:
    number = decimal_number(value, meter)
    meter.reserve()
    coefficient = number.coefficient
    if meter.integer_zero(coefficient):
        return True
    meter.reserve(2)
    exponent = number.exponent
    if exponent >= 0:
        return True
    meter.reserve()
    power = meter.integer_scale(1, -exponent)
    remainder = meter.integer_remainder(coefficient, power)
    return meter.integer_zero(remainder)


def numeric_compare(
    left: object, right: object, meter: EvaluationMeter
) -> Literal[-1, 0, 1]:
    first = decimal_number(left, meter)
    second = decimal_number(right, meter)
    meter.reserve()
    left_coefficient = first.coefficient
    left_zero = meter.integer_zero(left_coefficient)
    meter.reserve()
    right_coefficient = second.coefficient
    right_zero = meter.integer_zero(right_coefficient)
    meter.reserve()
    if left_zero and right_zero:
        return 0
    left_negative = meter.integer_negative(left_coefficient)
    right_negative = meter.integer_negative(right_coefficient)
    meter.reserve()
    if left_negative != right_negative:
        meter.reserve()
        return -1 if left_negative else 1
    meter.reserve(5)
    left_exponent, right_exponent = first.exponent, second.exponent
    exponent = min(left_exponent, right_exponent)
    aligned_left = meter.integer_scale(left_coefficient, left_exponent - exponent)
    aligned_right = meter.integer_scale(right_coefficient, right_exponent - exponent)
    result = meter.integer_compare(aligned_left, aligned_right)
    meter.reserve()
    if result < 0:
        return -1
    meter.reserve()
    return 1 if result > 0 else 0


def numeric_equal(left: object, right: object, meter: EvaluationMeter) -> bool:
    result = numeric_compare(left, right, meter)
    meter.reserve()
    return result == 0


def numeric_multiple_of(left: object, right: object, meter: EvaluationMeter) -> bool:
    first = decimal_number(left, meter)
    second = decimal_number(right, meter)
    meter.reserve()
    divisor = second.coefficient
    if meter.integer_zero(divisor):
        refuse()
    if meter.integer_negative(divisor):
        refuse()
    meter.reserve(6)
    coefficient = first.coefficient
    left_exponent, right_exponent = first.exponent, second.exponent
    exponent = min(left_exponent, right_exponent)
    aligned_left = meter.integer_scale(coefficient, left_exponent - exponent)
    aligned_right = meter.integer_scale(divisor, right_exponent - exponent)
    remainder = meter.integer_remainder(aligned_left, aligned_right)
    return meter.integer_zero(remainder)
