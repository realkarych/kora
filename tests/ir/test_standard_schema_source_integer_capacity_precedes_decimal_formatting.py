import json

import pytest

from tests.ir.standard_schema_cases import decide, source


def nested(depth: int) -> object:
    value: object = None
    for _ in range(depth):
        value = [value]
    return value


def padded_source(target: int) -> dict[str, object]:
    chunks = [""] * 17
    candidate = source(True, metadata=chunks)
    empty = len(json.dumps(candidate, ensure_ascii=True, separators=(",", ":")))
    remaining = target - empty
    for index in range(len(chunks)):
        count = min(remaining, 65536)
        chunks[index] = "x" * count
        remaining -= count
    assert remaining == 0
    return candidate


@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_standard_schema_source_integer_capacity_precedes_decimal_formatting(
    delta: int,
) -> None:
    value = (1 << (14000 + delta)) - 1

    result = decide(True, metadata=value)

    assert result.reason == ("evaluation_exhausted" if delta > 0 else "none")
