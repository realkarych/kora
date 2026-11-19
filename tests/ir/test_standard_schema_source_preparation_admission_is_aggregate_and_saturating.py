import json

from kora.ir.standard_schema_policy import SchemaDomain, domain_limits
from kora.ir.standard_schema_scan import SourceScan, preparation_score
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


def test_standard_schema_source_preparation_admission_is_aggregate_and_saturating() -> (
    None
):
    metadata = {f"key{index:04d}": None for index in range(1000)}
    one = source(True, metadata=metadata)
    multiple = source(True, metadata=[metadata] * 4)
    limits = domain_limits(SchemaDomain.OUTPUT)
    measured = SourceScan(limits).measure(multiple)

    admitted = check_standard_schema_source(one, SchemaDomain.OUTPUT, binding())
    refused = check_standard_schema_source(multiple, SchemaDomain.OUTPUT, binding())
    saturated = preparation_score(measured, limits["max_validation_score"])

    assert admitted.outcome == "source_checks_completed"
    assert measured.tree.encoded < limits["max_schema_source_bytes"]
    assert measured.tree.nodes < limits["max_schema_nodes"]
    assert saturated == limits["max_validation_score"] + 1
    assert refused.reason == "evaluation_exhausted"
