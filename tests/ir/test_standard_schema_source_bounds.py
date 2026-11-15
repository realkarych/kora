import json
from typing import cast

import pytest

from kora.ir.standard_schema_policy import SchemaDomain, SchemaRefused, domain_limits
from kora.ir.standard_schema_scan import SourceScan, preparation_score
from kora.ir.standard_schema_support import check_standard_schema_source
from tests.ir.standard_schema_cases import binding, decide, source


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
def test_standard_schema_source_capacity_boundaries(delta: int) -> None:
    target = 1048576 + delta
    candidate = padded_source(target)

    result = check_standard_schema_source(candidate, SchemaDomain.OUTPUT, binding())

    assert result.reason == ("evaluation_exhausted" if delta > 0 else "none")
    if delta <= 0:
        assert result.accounting.source_bytes == target


@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_standard_schema_source_node_boundary_counts_repeated_occurrences(
    delta: int,
) -> None:
    arrays: list[list[object]] = [[], [], [], []]
    candidate = source(True, metadata=arrays)
    limits = domain_limits(SchemaDomain.OUTPUT)
    baseline = SourceScan(limits).measure(candidate).nodes
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


@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_standard_schema_source_position_boundary_includes_boolean_children(
    delta: int,
) -> None:
    count = 2048 + delta

    result = decide({"allOf": [True] * (count - 1)})

    assert result.reason == ("evaluation_exhausted" if delta > 0 else "none")
    if delta <= 0:
        assert result.accounting.schema_positions == count


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


@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_standard_schema_source_integer_capacity_precedes_decimal_formatting(
    delta: int,
) -> None:
    value = (1 << (14000 + delta)) - 1

    result = decide(True, metadata=value)

    assert result.reason == ("evaluation_exhausted" if delta > 0 else "none")


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
    assert measured.encoded < limits["max_schema_source_bytes"]
    assert measured.nodes < limits["max_schema_nodes"]
    assert saturated == limits["max_validation_score"] + 1
    assert refused.reason == "evaluation_exhausted"


def test_standard_schema_source_counts_binding_metadata_before_schema_lookup() -> None:
    candidate = source({"$ref": "https://example.invalid"}, metadata=binding())
    baseline = SourceScan(domain_limits(SchemaDomain.OUTPUT)).measure(candidate)
    metadata = cast("dict[str, object]", candidate["metadata"])
    metadata["excess"] = "x" * 65537

    result = check_standard_schema_source(candidate, SchemaDomain.OUTPUT, binding())

    assert baseline.nodes > 60
    assert result.reason == "evaluation_exhausted"
