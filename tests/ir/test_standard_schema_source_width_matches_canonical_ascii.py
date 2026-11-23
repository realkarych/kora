import json

from kora.ir.standard_schema_policy import SchemaDomain
from kora.ir.standard_schema_support import check_standard_schema_source
from tests.ir.standard_schema_cases import binding, source


def nested(depth: int) -> object:
    value: object = None
    for _ in range(depth):
        value = [value]
    return value


def test_standard_schema_source_width_matches_canonical_ascii() -> None:
    metadata: dict[str, object] = {
        "🌍\n": [None, True, False, 0, -0.0, 0.3, 1e-308, 2**63 - 1, "\x00\x1f\x7f é"],
        "": {},
    }
    candidate = source(True, metadata=metadata)
    canonical = json.dumps(
        candidate, ensure_ascii=True, separators=(",", ":"), allow_nan=False
    )

    result = check_standard_schema_source(candidate, SchemaDomain.OUTPUT, binding())

    assert result.outcome == "source_checks_completed"
    assert result.accounting.source_bytes == len(canonical)
