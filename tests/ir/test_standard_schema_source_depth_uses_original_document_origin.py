import pytest

from kora.ir.standard_schema_policy import SchemaDomain
from kora.ir.standard_schema_support import check_standard_schema_source
from tests.ir.standard_schema_cases import binding, source


def nested(depth: int) -> object:
    value: object = None
    for _ in range(depth):
        value = [value]
    return value


@pytest.mark.parametrize("delta", [-1, 0, 1])
@pytest.mark.parametrize("location", ["schema", "metadata"])
def test_standard_schema_source_depth_uses_original_document_origin(
    delta: int, location: str
) -> None:
    depth = 32 + delta
    candidate = (
        source({"annotation": nested(depth - 1)})
        if location == "schema"
        else source(True, metadata=nested(depth))
    )

    result = check_standard_schema_source(candidate, SchemaDomain.OUTPUT, binding())

    assert result.reason == ("evaluation_exhausted" if delta > 0 else "none")
