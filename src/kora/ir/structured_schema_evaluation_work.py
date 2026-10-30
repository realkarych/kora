from dataclasses import dataclass

from kora.ir.structured_schema_evaluation_policy import PARAMETERS, refuse


def _limbs(bits: int) -> int:
    return max(1, (bits + 29) // 30)


def _admit_bits(bits: int) -> None:
    if bits > PARAMETERS["max_numeric_intermediate_bits"]:
        refuse("evaluation_exhausted")


@dataclass(slots=True)
class EvaluationMeter:
    work: int = 0
    auxiliary_bytes: int = 0
    auxiliary_entries: int = 0

    def __post_init__(self) -> None:
        self.reserve(0)

    def reserve(self, work: int = 1, byte_count: int = 0, entries: int = 0) -> None:
        values = (
            work,
            byte_count,
            entries,
            self.work,
            self.auxiliary_bytes,
            self.auxiliary_entries,
        )
        if any(type(value) is not int or value < 0 for value in values):
            raise ValueError("invalid evaluation reservation")
        if (
            self.work + work > PARAMETERS["max_evaluation_work"]
            or self.auxiliary_bytes + byte_count > PARAMETERS["max_auxiliary_bytes"]
            or self.auxiliary_entries + entries > PARAMETERS["max_auxiliary_entries"]
        ):
            refuse("evaluation_exhausted")
        self.work += work
        self.auxiliary_bytes += byte_count
        self.auxiliary_entries += entries

    def result(self, size: int) -> None:
        if type(size) is not int or size < 0:
            raise ValueError("invalid evaluation result size")
        self.reserve(1 + size, size)

    def integer_zero(self, value: int) -> bool:
        self.reserve()
        return value == 0

    def integer_negative(self, value: int) -> bool:
        self.reserve()
        return value < 0

    def integer_compare(self, left: int, right: int) -> int:
        left_bits, right_bits = left.bit_length(), right.bit_length()
        _admit_bits(max(left_bits, right_bits))
        self.reserve(1 + _limbs(left_bits) + _limbs(right_bits))
        return (left > right) - (left < right)

    def integer_negate(self, value: int) -> int:
        if self.integer_zero(value):
            return 0
        bits = value.bit_length()
        _admit_bits(bits)
        self.reserve(_limbs(bits), (bits + 7) // 8)
        return -value

    def integer_add(self, left: int, right: int) -> int:
        left_bits, right_bits = left.bit_length(), right.bit_length()
        bits = max(left_bits, right_bits) + 1
        _admit_bits(bits)
        self.reserve(1 + _limbs(left_bits) + _limbs(right_bits), (bits + 7) // 8)
        return left + right

    def integer_subtract(self, left: int, right: int) -> int:
        left_bits, right_bits = left.bit_length(), right.bit_length()
        bits = max(left_bits, right_bits) + 1
        _admit_bits(bits)
        self.reserve(1 + _limbs(left_bits) + _limbs(right_bits), (bits + 7) // 8)
        return left - right

    def integer_multiply(self, left: int, right: int) -> int:
        if self.integer_zero(left) or self.integer_zero(right):
            return 0
        left_bits, right_bits = left.bit_length(), right.bit_length()
        bits = left_bits + right_bits
        _admit_bits(bits)
        self.reserve(
            (_limbs(left_bits) + _limbs(right_bits)) ** 2 - 1,
            (bits + 7) // 8,
        )
        return left * right

    def integer_divmod(self, left: int, right: int) -> tuple[int, int]:
        left_bits, right_bits = left.bit_length(), right.bit_length()
        _admit_bits(max(left_bits, right_bits))
        self.reserve(
            1 + (_limbs(left_bits) + _limbs(right_bits) + 1) ** 2,
            (left_bits + 7) // 8 + (right_bits + 7) // 8,
        )
        return divmod(left, right)

    def integer_remainder(self, left: int, right: int) -> int:
        return self.integer_divmod(left, right)[1]

    def integer_scale(self, value: int, exponent: int) -> int:
        self.reserve()
        if exponent < 0:
            refuse()
        if self.integer_zero(value):
            return 0
        bits = value.bit_length()
        if exponent > (PARAMETERS["max_numeric_intermediate_bits"] - bits - 1) // 4:
            refuse("evaluation_exhausted")
        for _ in range(exponent):
            value = self.integer_multiply(value, 10)
        return value

    def parse_digits(self, value: str | bytearray) -> int:
        count = len(value)
        if count < 1 or count > 21:
            raise RuntimeError("invalid numerical spelling")
        bits = 4 * count + 1
        _admit_bits(bits)
        self.reserve(1 + count * count, (bits + 7) // 8)
        if not value.isascii() or not value.isdigit():
            raise RuntimeError("invalid numerical spelling")
        return int(value)
