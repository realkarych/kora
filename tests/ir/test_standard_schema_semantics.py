import json
from typing import cast

import pytest

from kora.ir.standard_schema_policy import SchemaDomain
from kora.ir.standard_schema_validation import validate_standard_schema
from tests.ir.standard_schema_cases import binding, source
from tests.ir.structured_schema_cases import (
    HostileArray,
    HostileDictionary,
    HostileHeader,
    HostileHeaderString,
    HostileInteger,
    HostileString,
)


@pytest.mark.parametrize("domain", list(SchemaDomain))
@pytest.mark.parametrize(
    ("schema", "value", "valid"),
    [
        ({"const": 0.3}, 0.3, True),
        ({"const": 0.3}, 0.1 + 0.2, False),
        ({"enum": [0.3, 1]}, 1.0, True),
        ({"multipleOf": 0.1}, 0.3, True),
        ({"multipleOf": 0.1}, 0.30000000000000004, False),
        ({"minimum": 0.3, "maximum": 0.3}, 0.3, True),
        ({"exclusiveMinimum": 0.3}, 0.3, False),
        ({"exclusiveMaximum": 0.3}, 0.3, False),
        ({"type": "integer"}, 1.0, True),
        ({"type": "integer"}, 1.5, False),
        ({"type": "number"}, True, False),
        ({"type": "integer"}, False, False),
        ({"const": -0.0}, 0, True),
        ({"enum": [False]}, 0.0, False),
        ({"const": {"x": [True]}}, {"x": [1]}, False),
        ({"enum": [{"x": [1.0]}]}, {"x": [1]}, True),
        ({"multipleOf": 5e-324}, 5e-324, True),
        ({"minimum": -1.7976931348623157e308}, -1.7976931348623157e308, True),
        ({"maximum": 1.7976931348623157e308}, 1.7976931348623157e308, True),
        ({"uniqueItems": True}, [[1], [True]], True),
        ({"uniqueItems": True}, [[1], [True], [1]], False),
        ({"uniqueItems": True}, [[False], [0], [False]], False),
        ({"uniqueItems": True}, [0.3, 0.30000000000000004], True),
        ({"uniqueItems": True}, [{"x": -0.0}, {"x": 0}], False),
        ({"uniqueItems": True}, [], True),
        ({"uniqueItems": True}, [1.0], True),
        ({"uniqueItems": False}, [1, 1], True),
        ({"minItems": 1.0, "maxItems": 1.0}, [None], True),
    ],
)
def test_standard_schema_numeric_view_preserves_decimal_semantics(
    domain: SchemaDomain, schema: object, value: object, valid: bool
) -> None:
    candidate = source(schema)
    original = json.dumps([candidate, value], separators=(",", ":"))

    result = validate_standard_schema(value, candidate, domain, binding(domain))

    assert result.outcome == ("valid" if valid else "invalid")
    assert result.reason == "none"
    assert json.dumps([candidate, value], separators=(",", ":")) == original


@pytest.mark.parametrize(
    ("schema", "value", "valid"),
    [
        (True, None, True),
        (False, None, False),
        ({}, "a", True),
        ({"type": ["null", "string"]}, None, True),
        ({"type": ["null", "string"]}, [], False),
        ({"type": "object"}, {}, True),
        ({"type": "array"}, [], True),
        ({"type": "boolean"}, True, True),
        ({"enum": []}, None, False),
        ({"minLength": 2, "maxLength": 2}, "а😀", True),
        ({"minLength": 2}, "😀", False),
        ({"maxLength": 1}, "ab", False),
        ({"minItems": 1}, [], False),
        ({"maxItems": 0}, [None], False),
        ({"properties": {"x": {"type": "number"}}}, {"x": 1}, True),
        ({"properties": {"x": False}}, {"x": 1}, False),
        ({"required": ["x"]}, {}, False),
        ({"required": []}, {}, True),
        ({"minProperties": 1}, {}, False),
        ({"maxProperties": 0}, {"x": 1}, False),
        ({"additionalProperties": False}, {"x": 1}, False),
        ({"properties": {"x": True}, "additionalProperties": False}, {"x": 1}, True),
        ({"additionalProperties": {"type": "integer"}}, {"x": 1.0}, True),
        ({"additionalProperties": {"type": "integer"}}, {"x": "a"}, False),
        ({"propertyNames": {"const": "x"}}, {"x": 1}, True),
        ({"propertyNames": {"const": "x"}}, {"y": 1}, False),
        ({"dependentRequired": {"x": ["y"]}}, {"x": 1}, False),
        ({"dependentRequired": {"x": ["y"]}}, {"y": 1}, True),
        ({"dependentSchemas": {"x": False}}, {"x": 1}, False),
        ({"dependentSchemas": {"x": False}}, {"y": 1}, True),
        ({"items": {"type": "integer"}}, [1.0, 2], True),
        ({"items": {"type": "integer"}}, [1, True], False),
        ({"prefixItems": [True], "items": False}, [1, 2], False),
        ({"prefixItems": [{"const": 1}], "items": False}, [1], True),
        ({"contains": {"const": 0.3}, "minContains": 2.0}, [0.3], False),
        ({"contains": {"const": 0.3}, "maxContains": 1.0}, [0.3, 0.3], False),
        ({"contains": False, "minContains": 0}, [], True),
        ({"allOf": [True, False]}, None, False),
        ({"anyOf": [False, True]}, None, True),
        ({"anyOf": [False, False]}, None, False),
        ({"oneOf": [False, True]}, None, True),
        ({"oneOf": [True, True]}, None, False),
        ({"oneOf": [False, False]}, None, False),
        ({"not": False}, None, True),
        ({"not": True}, None, False),
        ({"if": True, "then": False, "else": True}, None, False),
        ({"if": False, "then": False, "else": True}, None, True),
        ({"then": False, "else": False}, None, True),
        ({"format": "ipv4", "contentEncoding": "base64"}, "not-an-address", True),
        ({"$recursiveRef": "https://example.invalid", "unknown": False}, None, True),
    ],
)
def test_standard_schema_supported_keyword_families(
    schema: object, value: object, valid: bool
) -> None:
    candidate = source(schema)

    result = validate_standard_schema(value, candidate, SchemaDomain.OUTPUT, binding())

    assert result.outcome == ("valid" if valid else "invalid")
    assert result.accounting.expanded_calls >= 1


@pytest.mark.parametrize("keyword", ["$ref", "$dynamicRef"])
def test_standard_schema_adapter_survives_dialect_and_reference(keyword: str) -> None:
    dialect = "https://json-schema.org/draft/2020-12/schema"
    schema = {
        "$schema": dialect,
        "$id": "https://example.invalid/original",
        "$anchor": "authored",
        "$dynamicAnchor": "authored_dynamic",
        "$defs": {
            "a/b": {"$schema": dialect, "multipleOf": 0.1},
            "": {keyword: "#/$defs/a~1b"},
        },
        keyword: "urn:kora:artifact:example:Target#/$defs/",
    }
    candidate = {
        "documents": [
            {"name": None, "schema": {keyword: "urn:kora:artifact:example:Target"}},
            {"name": "example/Target", "schema": schema},
        ],
        "constraints": [0],
        "metadata": None,
    }
    original = json.dumps(candidate)

    result = validate_standard_schema(0.3, candidate, SchemaDomain.OUTPUT, binding())

    assert result.outcome == "valid"
    assert json.dumps(candidate) == original


def test_standard_schema_both_references_are_independent_assertions() -> None:
    schema = {
        "$defs": {"yes": True, "no": False},
        "$ref": "#/$defs/yes",
        "$dynamicRef": "#/$defs/no",
    }

    result = validate_standard_schema(
        None, source(schema), SchemaDomain.OUTPUT, binding()
    )

    assert result.outcome == "invalid"
    assert result.accounting.expanded_calls == 3


def test_standard_schema_literal_markers_and_equal_roots_remain_distinct() -> None:
    literal = {"$schema": "unknown", "$id": "private", "$ref": "external", "x": 0.3}
    first = {"const": literal}
    candidate = {
        "documents": [{"name": "a", "schema": first}, {"name": "b", "schema": first}],
        "constraints": [0, 1, 0],
        "metadata": None,
    }

    result = validate_standard_schema(
        literal, candidate, SchemaDomain.OUTPUT, binding()
    )

    assert result.outcome == "valid"
    assert result.accounting.schema_positions == 2
    assert result.accounting.expanded_calls == 3
    assert first == {"const": literal}


@pytest.mark.parametrize("domain", list(SchemaDomain))
@pytest.mark.parametrize(
    "value",
    [
        HostileHeader(),
        HostileArray(),
        HostileDictionary(),
        HostileInteger(1),
        HostileString("a"),
        HostileHeaderString("a"),
        {HostileHeaderString("key"): 1},
        {1: 1},
        (1, 2),
        b"data",
        float("nan"),
        float("inf"),
        float("-inf"),
        "\ud800",
        {"\udfff": None},
    ],
)
def test_standard_schema_native_domain_refuses_before_truth(
    domain: SchemaDomain, value: object
) -> None:
    candidate = source({"not": False})

    result = validate_standard_schema(value, candidate, domain, binding(domain))

    assert result.outcome == "refused"
    assert result.reason == "unsupported_schema"
    assert result.accounting.expanded_calls == 0


def test_standard_schema_native_cycles_refuse_without_changing_aliases() -> None:
    cyclic: list[object] = []
    cyclic.append(cyclic)
    shared: list[object] = ["leaf"]
    candidate = source(True)

    refused = validate_standard_schema(
        cyclic, candidate, SchemaDomain.OUTPUT, binding()
    )
    accepted = validate_standard_schema(
        [shared, shared], candidate, SchemaDomain.OUTPUT, binding()
    )

    assert refused.outcome == "refused"
    assert accepted.outcome == "valid"
    assert accepted.accounting.value_nodes == 5
    assert cast("list[object]", cyclic[0]) is cyclic
