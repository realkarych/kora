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
def test_standard_schema_source_position_boundary_includes_boolean_children(
    delta: int,
) -> None:
    count = 2048 + delta

    result = decide({"allOf": [True] * (count - 1)})

    assert result.reason == ("evaluation_exhausted" if delta > 0 else "none")
    if delta <= 0:
        assert result.accounting.schema_positions == count
