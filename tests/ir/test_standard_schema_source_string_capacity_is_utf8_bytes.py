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
@pytest.mark.parametrize("text_kind", ["ascii", "unicode", "key"])
def test_standard_schema_source_string_capacity_is_utf8_bytes(
    delta: int, text_kind: str
) -> None:
    size = 65536 + delta
    text = (
        "é" * (size // 2) + "x" * (size % 2) if text_kind == "unicode" else "x" * size
    )
    metadata: object = {text: None} if text_kind == "key" else text
    candidate = source(True, metadata=metadata)

    result = check_standard_schema_source(candidate, SchemaDomain.OUTPUT, binding())

    assert result.reason == ("evaluation_exhausted" if delta > 0 else "none")
