import json

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
