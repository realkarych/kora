import pytest

from kora.ir.standard_schema_policy import SchemaDomain
from kora.ir.standard_schema_support import check_standard_schema_source
from kora.ir.standard_schema_validation import validate_standard_schema
from tests.ir.standard_schema_cases import binding, source


@pytest.mark.parametrize(
    ("schema", "value", "valid"),
    [
        ({"minimum": 1e23}, 10**23, True),
        ({"minimum": 1e23}, int(1e23), False),
        ({"maximum": 0}, 0.1, False),
        ({"exclusiveMaximum": 0.3}, 0.2, True),
        ({"exclusiveMinimum": 0.2}, 0.3, True),
        ({"minProperties": 2}, {"a": 1, "b": 2}, True),
        ({"maxProperties": 1}, {"a": 1}, True),
        ({"minLength": 2}, "e\u0301", True),
        ({"minLength": 2}, "😀", False),
        ({"maxLength": 1}, "😀", True),
        ({"maxLength": 1}, "e\u0301", False),
        ({"type": "integer"}, 10**400, True),
        ({"type": "number"}, 10**400, True),
    ],
)
def test_standard_schema_retains_exact_primitive_boundaries(
    schema: object, value: object, valid: bool
) -> None:
    candidate = source(schema)

    result = validate_standard_schema(value, candidate, SchemaDomain.OUTPUT, binding())

    assert result.outcome == ("valid" if valid else "invalid")
    assert result.reason == "none"


@pytest.mark.parametrize("domain", list(SchemaDomain))
@pytest.mark.parametrize(
    "schema",
    [
        {"minimum": 1, "maximum": -1},
        {"exclusiveMinimum": 1, "exclusiveMaximum": -1},
        {"multipleOf": 2},
        {"minLength": 1, "maxLength": 0},
        {"minItems": 1, "maxItems": 0},
        {"minProperties": 1, "maxProperties": 0},
        {"required": ["a"]},
        {"dependentRequired": {"a": ["b"]}},
        {"uniqueItems": True},
    ],
)
def test_standard_schema_retains_inapplicable_assertions(
    domain: SchemaDomain, schema: object
) -> None:
    candidate = source(schema)

    result = validate_standard_schema(None, candidate, domain, binding(domain))

    assert result.outcome == "valid"
    assert result.reason == "none"


@pytest.mark.parametrize("domain", list(SchemaDomain))
@pytest.mark.parametrize(
    ("schema", "value", "valid"),
    [
        ({"required": ["a"]}, {"a": None}, True),
        ({"required": ["a"]}, {"a": False}, True),
        ({"required": ["a"]}, {"b": None}, False),
        ({"dependentRequired": {"a": ["b"]}}, {"a": False, "b": 0}, True),
        ({"dependentRequired": {"a": ["b"]}}, {"a": False}, False),
        ({"dependentRequired": {"a": ["b"]}}, {"a": None}, False),
        ({"dependentRequired": {"a": ["b"]}}, {}, True),
        ({"required": []}, {"a": False}, True),
        ({"dependentRequired": {}}, {"a": False}, True),
        (
            {
                "$defs": {"child": {"required": ["b"]}},
                "$ref": "#/$defs/child",
                "required": ["a"],
            },
            {"a": False, "b": None},
            True,
        ),
        (
            {
                "$defs": {"child": {"required": ["b"]}},
                "$ref": "#/$defs/child",
                "required": ["a"],
            },
            {"a": False},
            False,
        ),
        (
            {
                "$defs": {"child": {"required": ["b"]}},
                "$ref": "#/$defs/child",
                "required": ["a"],
            },
            {"b": None},
            False,
        ),
    ],
)
def test_standard_schema_retains_membership_without_truthiness(
    domain: SchemaDomain, schema: object, value: object, valid: bool
) -> None:
    candidate = source(schema)

    result = validate_standard_schema(value, candidate, domain, binding(domain))

    assert result.outcome == ("valid" if valid else "invalid")
    assert result.reason == "none"


@pytest.mark.parametrize("domain", list(SchemaDomain))
@pytest.mark.parametrize("value", [None, [], [None, False]])
def test_standard_schema_retains_standalone_contains_bounds(
    domain: SchemaDomain, value: object
) -> None:
    candidate = source({"minContains": 3, "maxContains": 0})

    result = validate_standard_schema(value, candidate, domain, binding(domain))

    assert result.outcome == "valid"
    assert result.reason == "none"


@pytest.mark.parametrize("domain", list(SchemaDomain))
@pytest.mark.parametrize(("value", "valid"), [(False, True), (None, False)])
def test_standard_schema_retains_sibling_type_after_both_references(
    domain: SchemaDomain, value: object, valid: bool
) -> None:
    candidate = source(
        {
            "$defs": {"leaf": True},
            "$ref": "#/$defs/leaf",
            "$dynamicRef": "#/$defs/leaf",
            "type": "boolean",
        }
    )

    result = validate_standard_schema(value, candidate, domain, binding(domain))

    assert result.outcome == ("valid" if valid else "invalid")
    assert result.reason == "none"


@pytest.mark.parametrize("domain", list(SchemaDomain))
@pytest.mark.parametrize("keyword", ["$ref", "$dynamicRef"])
@pytest.mark.parametrize("target", [False, True])
def test_standard_schema_retains_named_root_through_containment(
    domain: SchemaDomain, keyword: str, target: bool
) -> None:
    candidate = {
        "documents": [
            {
                "name": None,
                "schema": {
                    "$defs": {"leaf": not target},
                    keyword: "urn:kora:artifact:example:Target",
                },
            },
            {
                "name": "example/Target",
                "schema": {
                    "$defs": {"leaf": target},
                    "properties": {"nested": {keyword: "#/$defs/leaf"}},
                },
            },
        ],
        "constraints": [0],
        "metadata": None,
    }

    result = validate_standard_schema(
        {"nested": None}, candidate, domain, binding(domain)
    )

    assert result.outcome == ("valid" if target else "invalid")
    assert result.reason == "none"


@pytest.mark.parametrize("domain", list(SchemaDomain))
@pytest.mark.parametrize("depth", [32, 33])
@pytest.mark.parametrize("long_first", [False, True])
def test_standard_schema_retains_shared_suffix_depth(
    domain: SchemaDomain, depth: int, long_first: bool
) -> None:
    count = depth - 4
    definitions: dict[str, object] = {
        "shared": {"properties": {"leaf": True}},
        **{
            f"s{index}": {"$ref": f"#/$defs/s{index + 1}"} for index in range(count - 1)
        },
        f"s{count - 1}": {"$ref": "#/$defs/shared"},
    }
    branches = [{"$ref": "#/$defs/shared"}, {"$ref": "#/$defs/s0"}]
    if long_first:
        branches.reverse()
    candidate = source({"$defs": definitions, "allOf": branches})

    support = check_standard_schema_source(candidate, domain, binding(domain))
    result = validate_standard_schema(
        {"leaf": None}, candidate, domain, binding(domain)
    )

    assert support.outcome == ("source_checks_completed" if depth == 32 else "refused")
    assert result.outcome == ("valid" if depth == 32 else "refused")
    assert (
        support.reason
        == result.reason
        == ("none" if depth == 32 else "evaluation_exhausted")
    )
    assert (result.accounting.expanded_calls == 0) is (depth == 33)


@pytest.mark.parametrize("domain", list(SchemaDomain))
def test_standard_schema_retains_cross_document_cycle_refusal(
    domain: SchemaDomain,
) -> None:
    candidate = {
        "documents": [
            {
                "name": "example/First",
                "schema": {"$ref": "urn:kora:artifact:example:Second"},
            },
            {
                "name": "example/Second",
                "schema": {"$dynamicRef": "urn:kora:artifact:example:First"},
            },
        ],
        "constraints": [0],
        "metadata": None,
    }

    support = check_standard_schema_source(candidate, domain, binding(domain))
    result = validate_standard_schema(None, candidate, domain, binding(domain))

    assert support.outcome == result.outcome == "refused"
    assert support.reason == result.reason == "unsupported_schema"
    assert result.accounting.expanded_calls == 0
