import pytest

from kora.ir.standard_schema_policy import SchemaDomain, domain_limits
from kora.ir.standard_schema_scan import SourceScan
from kora.ir.standard_schema_support import check_standard_schema_source
from tests.ir.standard_schema_cases import binding, source


def nested(depth: int) -> object:
    value: object = None
    for _ in range(depth):
        value = [value]
    return value


@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_standard_schema_source_node_boundary_counts_repeated_occurrences(
    delta: int,
) -> None:
    arrays: list[list[object]] = [[], [], [], []]
    candidate = source(True, metadata=arrays)
    limits = domain_limits(SchemaDomain.OUTPUT)
    baseline = SourceScan(limits).measure(candidate).tree.nodes
    remaining = 16384 + delta - baseline
    for items in arrays:
        count = min(remaining, 4096)
        items.extend([None] * count)
        remaining -= count
    assert remaining == 0

    result = check_standard_schema_source(candidate, SchemaDomain.OUTPUT, binding())

    assert result.reason == ("evaluation_exhausted" if delta > 0 else "none")
    if delta <= 0:
        assert result.accounting.source_nodes == 16384 + delta
