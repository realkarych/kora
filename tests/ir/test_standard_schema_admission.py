import json

import pytest

from kora.ir.standard_schema_admission import admit, call_certificate, equality_score
from kora.ir.standard_schema_policy import SchemaDomain, SchemaRefused, domain_limits
from kora.ir.standard_schema_scan import SourceScan
from kora.ir.standard_schema_support import prepare_source
from kora.ir.standard_schema_validation import validate_standard_schema
from kora.ir.standard_schema_values import Bounds, numeric_score
from tests.ir.standard_schema_cases import binding, chain, source


@pytest.mark.parametrize("domain", list(SchemaDomain))
@pytest.mark.parametrize("offset", [-1, 0, 1])
@pytest.mark.parametrize(
    "dimension", ["bytes", "string", "array", "members", "bits", "depth"]
)
def test_standard_schema_capacity_boundaries(
    domain: SchemaDomain, offset: int, dimension: str
) -> None:
    limits = domain_limits(domain)
    value: object
    if dimension == "bytes":
        count = limits["max_value_canonical_bytes"] + offset
        pieces = ["x" * 500_000, "x" * 500_000]
        if domain is SchemaDomain.PARAMETERS:
            pieces *= 4
        overhead = 3 * len(pieces) + 4
        pieces.append("x" * (count - sum(map(len, pieces)) - overhead))
        value = pieces
        assert len(json.dumps(value, separators=(",", ":"))) == count
    elif dimension == "string":
        value = "x" * (limits["max_value_string_bytes"] + offset)
        limits["max_value_canonical_bytes"] = 4194304
    elif dimension == "array":
        value = [None] * (limits["max_value_array_items"] + offset)
    elif dimension == "members":
        value = {
            str(index): None
            for index in range(limits["max_value_object_members"] + offset)
        }
    elif dimension == "bits":
        bits = limits["max_value_integer_bits"] + offset
        value = -(1 << (bits - 1))
    else:
        containers = (
            limits["max_value_depth"] + offset + int(domain is SchemaDomain.PARAMETERS)
        )
        value = None
        for _ in range(containers):
            value = [value]
    scanner = SourceScan(limits, domain, schema=False)

    if offset > 0:
        with pytest.raises(SchemaRefused) as failure:
            scanner.measure(value)
        assert failure.value.reason == "evaluation_exhausted"
    else:
        measured = scanner.measure(value)
        assert measured.tree.nodes > 0
        assert measured.tree.encoded <= limits["max_value_canonical_bytes"]


@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_standard_schema_value_node_occurrences_include_aliases(offset: int) -> None:
    limits = domain_limits(SchemaDomain.PARAMETERS)
    maximum = limits["max_value_nodes"] + offset
    block = [None] * 65535
    value = [block, block, block, [None] * (maximum - 3 * 65536 - 2)]
    scanner = SourceScan(limits, SchemaDomain.PARAMETERS, schema=False)

    if offset > 0:
        with pytest.raises(SchemaRefused, match="evaluation_exhausted"):
            scanner.measure(value)
    else:
        measured = scanner.measure(value)
        assert measured.tree.nodes == maximum


@pytest.mark.parametrize(
    ("value", "outcome"),
    [
        (-(2**63) - 1, "refused"),
        (-(2**63), "valid"),
        (2**63 - 1, "valid"),
        (2**63, "refused"),
    ],
)
def test_standard_schema_parameter_integer_domain_is_signed(
    value: int, outcome: str
) -> None:
    domain = SchemaDomain.PARAMETERS

    result = validate_standard_schema(value, source(True), domain, binding(domain))

    assert result.outcome == outcome


@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_standard_schema_repr_capacity_is_admitted_before_view(offset: int) -> None:
    value = ["x" * (699050 + offset)]

    result = validate_standard_schema(
        value, source(True), SchemaDomain.OUTPUT, binding()
    )

    assert result.outcome == ("refused" if offset > 0 else "valid")
    assert result.reason == ("evaluation_exhausted" if offset > 0 else "none")


@pytest.mark.parametrize("count", [127, 128, 129])
@pytest.mark.parametrize("reachable", [False, True])
def test_standard_schema_unique_capacity_applies_only_when_reachable(
    count: int, reachable: bool
) -> None:
    schema = {"$defs": {"unused": {"uniqueItems": True}}, "uniqueItems": reachable}

    result = validate_standard_schema(
        list(range(count)), source(schema), SchemaDomain.OUTPUT, binding()
    )

    assert result.outcome == ("refused" if count > 128 and reachable else "valid")


@pytest.mark.parametrize("count", [31, 32, 33])
def test_standard_schema_preflight_bounds_reference_depth(count: int) -> None:
    candidate = source(chain(count))

    result = validate_standard_schema(None, candidate, SchemaDomain.OUTPUT, binding())

    assert result.outcome == ("refused" if count > 32 else "valid")
    if count <= 32:
        assert result.accounting.expanded_calls == count


@pytest.mark.parametrize("keyword", ["allOf", "anyOf", "oneOf"])
def test_standard_schema_preflight_bounds_expansion(keyword: str) -> None:
    definitions: dict[str, object] = {"end": True}
    target = "end"
    for index in range(14):
        name = f"n{index}"
        definitions[name] = {
            keyword: [{"$ref": f"#/$defs/{target}"}, {"$ref": f"#/$defs/{target}"}]
        }
        target = name
    schema = {"$defs": definitions, "not": {"$ref": f"#/$defs/{target}"}}
    candidate = source(schema)
    candidate["constraints"] = [0, 0, 0]

    result = validate_standard_schema(None, candidate, SchemaDomain.OUTPUT, binding())

    assert result.outcome == "refused"
    assert result.reason == "evaluation_exhausted"
    assert result.accounting.expanded_calls == 0


@pytest.mark.parametrize(
    "schema",
    [
        {"anyOf": [True, {"pattern": "x"}]},
        {"if": False, "then": {"unevaluatedItems": False}},
        {"not": {"$ref": "#"}},
        {"$defs": {"unused": {"$ref": "#/$defs/unused"}}},
    ],
)
def test_standard_schema_hidden_support_refusal_is_never_schema_false(
    schema: object,
) -> None:
    candidate = source(schema)

    result = validate_standard_schema(None, candidate, SchemaDomain.OUTPUT, binding())

    assert result.outcome == "refused"
    assert result.reason == "unsupported_schema"


@pytest.mark.parametrize("extra", [1, 2, 3])
def test_standard_schema_expanded_call_capacity_counts_repeated_roles(
    extra: int,
) -> None:
    definitions: dict[str, object] = {"end": True}
    target = "end"
    for index in range(14):
        name = f"n{index}"
        definitions[name] = {
            "allOf": [{"$ref": f"#/$defs/{target}"}, {"$ref": f"#/$defs/{target}"}]
        }
        target = name
    schema = {"$defs": definitions, "not": {"$ref": f"#/$defs/{target}"}}
    candidate = {
        "documents": [{"name": None, "schema": schema}, {"name": None, "schema": True}],
        "constraints": [0, 0] + [1] * extra,
        "metadata": None,
    }

    result = validate_standard_schema(None, candidate, SchemaDomain.OUTPUT, binding())

    assert result.outcome == ("refused" if extra > 2 else "invalid")
    assert result.accounting.expanded_calls == (0 if extra > 2 else 131070 + extra)


def test_standard_schema_call_weights_and_root_multiplicity() -> None:
    schema = {
        "items": True,
        "contains": True,
        "additionalProperties": True,
        "propertyNames": True,
        "prefixItems": [True, True],
        "properties": {"x": True},
        "dependentSchemas": {"x": True},
    }
    value = [{"x": 1, "y": 2}, 3, 4]
    candidate = source(schema)
    candidate["constraints"] = [0, 0]
    limits = domain_limits(SchemaDomain.OUTPUT)
    measured = SourceScan(limits, schema=False).measure(value)
    statistics, sources = prepare_source(candidate, limits)

    calls, _, depth = call_certificate(
        sources, measured, statistics, SchemaDomain.OUTPUT
    )

    assert calls == 2 * (1 + 2 * 3 + 2 * 2 + 2 + 1 + 1)
    assert depth == 2


def test_standard_schema_equality_accounts_for_structure_keys_bytes_and_numbers() -> (
    None
):
    left = Bounds(nodes=3, encoded=7, key_bytes=2, bits=30, numeric=True)
    right = Bounds(nodes=5, encoded=11, key_bytes=4)

    result = equality_score(left, right, 67108865)
    saturated = equality_score(left, right, 10)

    assert result == 81 * 7 + 7 + 11 + 16 * 3 * 3
    assert saturated == 10
    assert numeric_score(30) == 144


@pytest.mark.parametrize(
    ("value", "ceiling"), [(0.3, 26), (5e-324, 2610), (-(1 << 13999), 28002)]
)
@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_standard_schema_numeric_intermediates_are_reserved_before_fraction(
    value: object, ceiling: int, offset: int
) -> None:
    limits = domain_limits(SchemaDomain.OUTPUT)
    limits["max_numeric_intermediate_bits"] = ceiling + offset
    scanner = SourceScan(limits, schema=False)

    if offset < 0:
        with pytest.raises(SchemaRefused, match="evaluation_exhausted"):
            scanner.measure(value)
    else:
        measured = scanner.measure(value)
        assert 2 * measured.tree.bits + 2 == ceiling


@pytest.mark.parametrize(
    "limit", ["max_validation_score", "max_variable_bytes", "max_auxiliary_entries"]
)
@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_standard_schema_combined_admission_refuses_before_construction(
    limit: str, offset: int
) -> None:
    value = {"a": [1.2, 3]}
    candidate = source({"properties": {"a": {"items": {"minimum": 0.1}}}})
    limits = domain_limits(SchemaDomain.OUTPUT)
    measured = SourceScan(limits, schema=False).measure(value)
    statistics, sources = prepare_source(candidate, limits)
    baseline = admit(sources, measured, statistics, SchemaDomain.OUTPUT)
    field = {
        "max_validation_score": baseline.validation_score,
        "max_variable_bytes": baseline.variable_bytes,
        "max_auxiliary_entries": baseline.auxiliary_entries,
    }[limit]
    limits[limit] = field + offset
    statistics, sources = prepare_source(candidate, limits)

    if offset < 0:
        with pytest.raises(SchemaRefused, match="evaluation_exhausted"):
            admit(sources, measured, statistics, SchemaDomain.OUTPUT)
    else:
        admitted = admit(sources, measured, statistics, SchemaDomain.OUTPUT)
        assert admitted == baseline
