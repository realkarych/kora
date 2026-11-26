from kora.ir.standard_schema_policy import SchemaDomain, domain_limits
from kora.ir.standard_schema_scan import SourceScan
from kora.ir.standard_schema_support import check_standard_schema_source
from tests.ir.standard_schema_cases import binding, source


def nested(depth: int) -> object:
    value: object = None
    for _ in range(depth):
        value = [value]
    return value


def test_standard_schema_source_reference_maximum_requires_graph_support() -> None:
    leaf = {"$ref": "#", "$dynamicRef": "#"}
    schema = {"allOf": [leaf] * 2047, **leaf}
    candidate = source(schema)
    limits = domain_limits(SchemaDomain.OUTPUT)

    measured = SourceScan(limits).measure(candidate)
    result = check_standard_schema_source(candidate, SchemaDomain.OUTPUT, binding())

    assert measured.positions == 2048
    assert measured.references == 4096
    assert result.reason == "unsupported_schema"
