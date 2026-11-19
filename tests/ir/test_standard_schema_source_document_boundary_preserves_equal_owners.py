import json

import pytest

from kora.ir.standard_schema_policy import SchemaDomain
from kora.ir.standard_schema_support import check_standard_schema_source
from tests.ir.standard_schema_cases import binding, source


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
def test_standard_schema_source_document_boundary_preserves_equal_owners(
    delta: int,
) -> None:
    count = 64 + delta
    candidate = source(True)
    candidate["documents"] = [{"name": None, "schema": True} for _ in range(count)]

    result = check_standard_schema_source(candidate, SchemaDomain.OUTPUT, binding())

    assert result.reason == ("evaluation_exhausted" if delta > 0 else "none")
    if delta <= 0:
        assert result.accounting.schema_documents == count
        assert result.accounting.schema_positions == count
