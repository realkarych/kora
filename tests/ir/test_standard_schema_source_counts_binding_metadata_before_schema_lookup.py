import json
from typing import cast

from kora.ir.standard_schema_policy import SchemaDomain, domain_limits
from kora.ir.standard_schema_scan import SourceScan
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


def test_standard_schema_source_counts_binding_metadata_before_schema_lookup() -> None:
    candidate = source({"$ref": "https://example.invalid"}, metadata=binding())
    baseline = SourceScan(domain_limits(SchemaDomain.OUTPUT)).measure(candidate)
    metadata = cast("dict[str, object]", candidate["metadata"])
    metadata["excess"] = "x" * 65537

    result = check_standard_schema_source(candidate, SchemaDomain.OUTPUT, binding())

    assert baseline.tree.nodes > 60
    assert result.reason == "evaluation_exhausted"
