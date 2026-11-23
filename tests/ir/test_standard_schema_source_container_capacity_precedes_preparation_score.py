import pytest

from kora.ir.standard_schema_policy import SchemaDomain, SchemaRefused, domain_limits
from kora.ir.standard_schema_scan import SourceScan
from kora.ir.standard_schema_support import check_standard_schema_source
from tests.ir.standard_schema_cases import binding, source


def nested(depth: int) -> object:
    value: object = None
    for _ in range(depth):
        value = [value]
    return value


@pytest.mark.parametrize("delta", [-1, 0, 1])
@pytest.mark.parametrize("kind", ["array", "object"])
def test_standard_schema_source_container_capacity_precedes_preparation_score(
    delta: int, kind: str
) -> None:
    count = 4096 + delta
    metadata: object = (
        [None] * count
        if kind == "array"
        else {str(index): None for index in range(count)}
    )
    candidate = source(True, metadata=metadata)
    scanner = SourceScan(domain_limits(SchemaDomain.OUTPUT))

    if delta > 0:
        with pytest.raises(SchemaRefused) as refused:
            scanner.measure(candidate)

        assert refused.value.reason == "evaluation_exhausted"
        return
    measured = scanner.measure(candidate)
    result = check_standard_schema_source(candidate, SchemaDomain.OUTPUT, binding())

    assert (measured.array if kind == "array" else measured.members) == count
    assert result.reason == ("evaluation_exhausted" if kind == "object" else "none")
