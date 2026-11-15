import json

import pytest

from kora.ir.standard_schema_policy import SchemaDomain
from kora.ir.standard_schema_validation import validate_standard_schema
from tests.ir.standard_schema_cases import binding, source
from tests.ir.structured_output_cases import nested


@pytest.mark.parametrize(
    ("schema", "value", "valid"),
    [
        ({"multipleOf": 2.0}, 2**63 - 1, False),
        ({"multipleOf": 2.0}, -(2**63), True),
        ({"const": float(2**63)}, 2**63 - 1, False),
        ({"minimum": 1.0}, 1, True),
        ({"maximum": -1}, 1, False),
        ({"exclusiveMinimum": -1}, 1, True),
        ({"exclusiveMaximum": 0.30000000000000004}, 0.3, True),
        ({"type": "boolean"}, True, True),
        ({"type": "null"}, None, True),
        ({"type": "string"}, "text", True),
        ({"type": "array"}, [], True),
        ({"type": "object"}, {}, True),
        ({"type": ["array", "string"]}, "text", True),
        ({"type": ["number", "integer"]}, None, False),
        ({"minLength": 4}, "a😀e\u0301", True),
        ({"maxLength": 4.0}, "a😀e\u0301", True),
        ({"minLength": 5}, "a😀e\u0301", False),
        ({"maxLength": 3}, "a😀e\u0301", False),
        ({"minItems": 2}, [[1, 2], None], True),
        ({"maxItems": 2}, [[1, 2], None], True),
        ({"minItems": 3}, [[1, 2], None], False),
        ({"maxItems": 1}, [[1, 2], None], False),
        ({"minProperties": 2}, {"\x00": [1], "é": {}}, True),
        ({"maxProperties": 2}, {"\x00": [1], "é": {}}, True),
        ({"minProperties": 10**400}, {"a": 1}, False),
        ({"maxProperties": 1}, {"a": 1, "b": 2}, False),
        ({"maxItems": 10**400}, [False], True),
        ({"required": ["e\u0301"]}, {"é": None}, False),
        ({"required": ["é"]}, {"é": False}, True),
        ({"required": ["😀", "\x00", "𐀀"]}, {"😀": False, "\x00": None, "𐀀": []}, True),
    ],
)
def test_standard_parameters_retain_scalar_shape_and_presence(
    schema: object, value: object, valid: bool
) -> None:
    candidate = source(schema)
    profile = binding(SchemaDomain.PARAMETERS)

    result = validate_standard_schema(
        value, candidate, SchemaDomain.PARAMETERS, profile
    )

    assert result.outcome == ("valid" if valid else "invalid")
    assert result.reason == "none"


@pytest.mark.parametrize(
    ("schema", "value", "valid"),
    [
        (
            {"const": {"a": {"\x00": 1}, "z": [None]}},
            {"z": [None], "a": {"\x00": 1.0}},
            True,
        ),
        ({"const": [1, False]}, [False, 1], False),
        ({"const": 10**23}, 1e23, True),
        ({"const": int(1e23)}, 1e23, False),
        ({"const": ["e\u0301"]}, ["é"], False),
        ({"const": {"a": 0.3, "y": True}}, {"a": 0.3, "z": True}, False),
        ({"enum": [1, 1.0]}, 1, True),
        ({"enum": [True, 2]}, 1, False),
        ({"uniqueItems": True}, [1, 1.0, 1], False),
        ({"const": nested(18)}, nested(18), True),
    ],
)
def test_standard_parameters_retain_recursive_equality(
    schema: object, value: object, valid: bool
) -> None:
    candidate = source(schema)
    profile = binding(SchemaDomain.PARAMETERS)
    original = json.dumps([candidate, value], separators=(",", ":"))

    result = validate_standard_schema(
        value, candidate, SchemaDomain.PARAMETERS, profile
    )

    assert result.outcome == ("valid" if valid else "invalid")
    assert result.reason == "none"
    assert json.dumps([candidate, value], separators=(",", ":")) == original


@pytest.mark.parametrize(
    ("schema", "value", "valid"),
    [
        (True, 1, True),
        (False, 1, False),
        ({}, 1, True),
        ({"allOf": [True, {"type": "integer"}]}, 1, True),
        ({"allOf": [True, False]}, 1, False),
        ({"anyOf": [False, {"minimum": 1}]}, 1, True),
        ({"anyOf": [False, {"exclusiveMaximum": 1}]}, 1, False),
        ({"oneOf": [False, True, False]}, 1, True),
        ({"oneOf": [False, False]}, 1, False),
        ({"oneOf": [True, True]}, 1, False),
        ({"not": {"type": "string"}}, 1, True),
        ({"not": {"type": "number"}}, 1, False),
        ({"if": True, "then": False, "else": True}, 1, False),
        ({"if": False, "then": True, "else": False}, 1, False),
        ({"if": False, "then": False}, 1, True),
        ({"if": True, "else": False}, 1, True),
        ({"if": {"not": False}}, 1, True),
        ({"properties": {"a": False}}, {"a": 1}, False),
        (
            {
                "$recursiveRef": "#",
                "dependencies": {"a": False},
                "then": False,
                "else": False,
                "minContains": 1,
                "x-extension": {"$ref": "#"},
            },
            {"a": 1},
            True,
        ),
        (
            {"prefixItems": [{"type": "string"}], "items": {"type": "integer"}},
            ["first", 2, 3],
            True,
        ),
        (
            {"prefixItems": [{"type": "string"}], "items": {"type": "integer"}},
            ["first", 2, "tail"],
            False,
        ),
        (
            {"prefixItems": [{"const": 0}, {"type": "string"}], "items": {"const": 0}},
            [0, "a", 0],
            True,
        ),
        ({"prefixItems": [{"const": 1}]}, [1, 2], True),
        ({"items": {"allOf": [{"type": "integer"}, {"minimum": 1}]}}, [1, 2], True),
        ({"items": {"items": {"type": "integer"}}}, [[1], [2]], True),
        ({"items": {"items": False}}, [list[object]()], True),
        ({"items": {"items": {"type": "integer"}}}, [["x"]], False),
        ({"prefixItems": [True], "items": False}, [1, 2], False),
        ({"contains": {"type": "integer"}}, [1, "unvisited"], True),
        (
            {"contains": {"const": 1}, "minContains": 1.0, "maxContains": 1.0},
            [1, 0, 0],
            True,
        ),
        ({"contains": {"const": 1}, "maxContains": 1e23}, [1], True),
        (
            {"contains": {"const": True}, "minContains": 0, "maxContains": 0},
            [False, False],
            True,
        ),
        (
            {"contains": True, "minContains": 10**23 + 1, "maxContains": 1e23},
            [0],
            False,
        ),
    ],
)
def test_standard_parameters_retain_logical_and_array_decisions(
    schema: object, value: object, valid: bool
) -> None:
    candidate = source(schema)
    profile = binding(SchemaDomain.PARAMETERS)

    result = validate_standard_schema(
        value, candidate, SchemaDomain.PARAMETERS, profile
    )

    assert result.outcome == ("valid" if valid else "invalid")
    assert result.reason == "none"


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "string", "$ref": "#"},
        {"if": True, "then": True, "else": {"$ref": "#"}},
        {"prefixItems": [True, {"$ref": "#"}]},
        {"contains": {"$ref": "#"}, "minContains": 0},
        {"contains": True, "minContains": -1},
        {"contains": True, "maxContains": 1.5},
        {"contains": True, "maxContains": False},
        {"prefixItems": "unentered"},
        {"required": [False]},
        {"dependentRequired": {"absent": "unentered"}},
        {"maxItems": "unentered"},
        {"uniqueItems": "unentered"},
        {"multipleOf": False},
        {"multipleOf": 0},
        {"multipleOf": -1},
        {"multipleOf": -0.0},
    ],
)
def test_standard_parameters_refuse_invalid_source_before_applicability(
    schema: object,
) -> None:
    candidate = source(schema)
    profile = binding(SchemaDomain.PARAMETERS)

    result = validate_standard_schema(None, candidate, SchemaDomain.PARAMETERS, profile)

    assert result.outcome == "refused"
    assert result.reason == "unsupported_schema"
    assert result.accounting.expanded_calls == 0
