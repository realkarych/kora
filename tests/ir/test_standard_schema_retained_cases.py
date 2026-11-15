import json

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
        ({"type": ["array", "null"]}, None, True),
        ({"type": ["array", "boolean"]}, None, False),
        ({"type": ["null", "integer"]}, 1, True),
        ({"type": ["array", "null"]}, 1, False),
        ({"type": ["integer", "number"]}, 1.5, True),
        ({"type": ["integer", "number"]}, 1.0, True),
        ({"type": ["number", "integer"]}, 1.0, True),
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
        ({"required": ["a", "b"]}, {"a": False}, False),
        ({"required": ["b", "a"]}, {"a": False}, False),
        (
            {"required": ["prefixx"]},
            {"prefix": None, "prefixx": False, "q" * 37: 0, "z": []},
            True,
        ),
        (
            {"required": ["prefixy"]},
            {"prefix": None, "prefixx": False, "q" * 37: 0, "z": []},
            False,
        ),
        ({"required": ["😀", "nul\x00"]}, {"nul\x00": None, "😀": False, "é": 0}, True),
        ({"required": ["e\u0301"]}, {"nul\x00": None, "😀": False, "é": 0}, False),
        ({"dependentRequired": {"a": []}}, {"a": False}, True),
        ({"dependentRequired": {"a": ["b"]}}, {"a": None, "b": False}, True),
        ({"dependentRequired": {"a": ["b"]}}, {"a": 0, "b": False}, True),
        ({"dependentRequired": {"a": ["b"]}}, {"a": 0}, False),
        ({"dependentRequired": {"a": ["b"]}}, {"a": [], "b": False}, True),
        ({"dependentRequired": {"a": ["b"]}}, {"a": []}, False),
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


@pytest.mark.parametrize(
    "name", ["array", "boolean", "integer", "null", "number", "object", "string"]
)
@pytest.mark.parametrize(
    ("value", "accepted"),
    [
        (None, ("null",)),
        (False, ("boolean",)),
        (True, ("boolean",)),
        (0, ("integer", "number")),
        (1, ("integer", "number")),
        (-0.0, ("integer", "number")),
        (1.0, ("integer", "number")),
        (1e23, ("integer", "number")),
        (1.5, ("number",)),
        (5e-324, ("number",)),
        ("private-assertion\x00\U0001f600", ("string",)),
        ([], ("array",)),
        ({}, ("object",)),
    ],
)
def test_standard_schema_retains_scalar_type_cross_product(
    value: object, accepted: tuple[str, ...], name: str
) -> None:
    candidate = source({"type": name})

    result = validate_standard_schema(value, candidate, SchemaDomain.OUTPUT, binding())

    assert result.outcome == ("valid" if name in accepted else "invalid")
    assert result.reason == "none"


@pytest.mark.parametrize(
    ("keyword", "orders"),
    [
        ("minimum", (0, 1)),
        ("maximum", (-1, 0)),
        ("exclusiveMinimum", (1,)),
        ("exclusiveMaximum", (-1,)),
    ],
)
@pytest.mark.parametrize(
    ("value", "bound", "order"),
    [
        (1, 1, 0),
        (0, 1, -1),
        (2, 1, 1),
        (-0.0, 0, 0),
        (10**23, 1e23, 0),
        (10**23 - 1, 1e23, -1),
        (10**23 + 1, 1e23, 1),
        (int(1e23), 1e23, -1),
        (-0.3, -0.2, -1),
        (-(1 << 13_999), 5e-324, -1),
        (1 << 13_999, -5e-324, 1),
    ],
)
def test_standard_schema_retains_numeric_bound_order(
    keyword: str,
    orders: tuple[int, ...],
    value: int | float,
    bound: int | float,
    order: int,
) -> None:
    candidate = source({keyword: bound})

    result = validate_standard_schema(value, candidate, SchemaDomain.OUTPUT, binding())

    assert result.outcome == ("valid" if order in orders else "invalid")
    assert result.reason == "none"


@pytest.mark.parametrize(
    ("value", "divisor", "valid"),
    [
        (0.3, 0.1, True),
        (10**400, 0.5, True),
        (-0.3, 0.1, True),
        (0.3, 0.2, False),
        (0, 5e-324, True),
        (5e-324, 1e-323, False),
        (1e-323, 5e-324, True),
        ((1 << 13_999) - 2, 3, True),
    ],
)
def test_standard_schema_retains_divisibility_extremes(
    value: int | float, divisor: int | float, valid: bool
) -> None:
    candidate = source({"multipleOf": divisor})

    result = validate_standard_schema(value, candidate, SchemaDomain.OUTPUT, binding())

    assert result.outcome == ("valid" if valid else "invalid")
    assert result.reason == "none"


@pytest.mark.parametrize("keyword", ["const", "enum"])
@pytest.mark.parametrize(
    ("value", "literal", "valid"),
    [
        pytest.param(None, None, True, id="null-root"),
        pytest.param(True, True, True, id="boolean-root"),
        pytest.param(True, False, False, id="unequal-booleans"),
        pytest.param(True, 1, False, id="boolean-versus-number"),
        pytest.param(1, 1.0, True, id="exact-numerical-cross-kind"),
        pytest.param(-0.0, 0, True, id="signed-zero"),
        pytest.param(10**23, 1e23, True, id="exact-decimal-spelling"),
        pytest.param(
            int(1e23), 1e23, False, id="binary-rounding-is-not-decimal-equality"
        ),
        pytest.param(0.3, 0.3, True, id="decimal-fraction"),
        pytest.param("nul\x00\U0001f600", "nul\x00\U0001f600", True, id="escaped-nul"),
        pytest.param("\u00e9", "e\u0301", False, id="no-unicode-normalization"),
        pytest.param([], [], True, id="empty-array-root"),
        pytest.param({}, {}, True, id="empty-object-root"),
        pytest.param([None, False, 1], [None, False, 1.0], True, id="array-root"),
        pytest.param([1, 2], [2, 1], False, id="array-index-order"),
        pytest.param(
            {"z": [], "a": None}, {"a": None, "z": []}, True, id="object-order"
        ),
        pytest.param(
            {"\U0001f600": [{"\x00": 1}], "\uffff": True},
            {"\uffff": True, "\U0001f600": [{"\x00": 1.0}]},
            True,
            id="nested-codepoint-order",
        ),
        pytest.param(None, [], False, id="unlike-kinds"),
        pytest.param([], [None], False, id="array-count"),
        pytest.param({}, {"a": None}, False, id="object-count"),
        pytest.param(
            {"a": 0.3, "b": None}, {"a": 0.3, "c": None}, False, id="object-key"
        ),
        pytest.param([None, 0.3], [False, 0.3], False, id="first-array-value"),
        pytest.param(
            {"a": None, "b": 0.3},
            {"a": False, "b": 0.3},
            False,
            id="first-object-value",
        ),
    ],
)
def test_standard_schema_retains_recursive_literal_equality(
    keyword: str, value: object, literal: object, valid: bool
) -> None:
    candidate = source({keyword: [literal] if keyword == "enum" else literal})
    original = json.dumps([candidate, value], separators=(",", ":"))

    result = validate_standard_schema(value, candidate, SchemaDomain.OUTPUT, binding())

    assert result.outcome == ("valid" if valid else "invalid")
    assert result.reason == "none"
    assert json.dumps([candidate, value], separators=(",", ":")) == original


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        pytest.param([], True, id="empty"),
        pytest.param([None], True, id="singleton"),
        pytest.param([None, None], False, id="null-duplicate"),
        pytest.param([None, True, True], False, id="later-pair-duplicate"),
        pytest.param([True, 1], True, id="boolean-is-distinct"),
        pytest.param([1, 1.0], False, id="numerical-cross-kind"),
        pytest.param([-0.0, 0], False, id="signed-zero"),
        pytest.param([10**23, 1e23], False, id="canonical-decimal"),
        pytest.param([int(1e23), 1e23], True, id="binary-rounding-distinct"),
        pytest.param(["\u00e9", "e\u0301"], True, id="no-normalization"),
        pytest.param(["nul\x00", "nul\x00"], False, id="nul-duplicate"),
        pytest.param([[None], [None]], False, id="nested-array"),
        pytest.param(
            [{"z": [0.3], "a": None}, {"a": None, "z": [0.3]}],
            False,
            id="canonical-object-keys",
        ),
    ],
)
def test_standard_schema_retains_collection_equality(
    value: object, valid: bool
) -> None:
    candidate = source({"uniqueItems": True})

    result = validate_standard_schema(value, candidate, SchemaDomain.OUTPUT, binding())

    assert result.outcome == ("valid" if valid else "invalid")
    assert result.reason == "none"


@pytest.mark.parametrize("keyword", ["minLength", "maxLength"])
@pytest.mark.parametrize(
    ("value", "bound", "minimum", "maximum"),
    [
        pytest.param("", 0, True, True, id="empty-zero"),
        pytest.param("", 1, False, True, id="empty-positive"),
        pytest.param("\x00", 0, True, False, id="nul"),
        pytest.param("\U0001f600", 1, True, True, id="supplementary-codepoint"),
        pytest.param("e\u0301", 1, True, False, id="combining-is-two-codepoints"),
        pytest.param("e\u0301", 2, True, True, id="combining-exact"),
        pytest.param("\u00e9", 2, False, True, id="no-normalization"),
        pytest.param("\x00\U0001f600e\u0301", 4, True, True, id="mixed-codepoints"),
        pytest.param("x", 1.0, True, True, id="integral-float"),
        pytest.param("", -0.0, True, True, id="negative-floating-zero"),
        pytest.param("x", 1e23, False, True, id="large-integral-float"),
        pytest.param("x", (1 << 14_000) - 1, False, True, id="large-integer"),
    ],
)
def test_standard_schema_retains_unicode_count_boundaries(
    keyword: str, value: str, bound: int | float, minimum: bool, maximum: bool
) -> None:
    candidate = source({keyword: bound})

    result = validate_standard_schema(value, candidate, SchemaDomain.OUTPUT, binding())

    assert result.outcome == (
        "valid" if (minimum if keyword == "minLength" else maximum) else "invalid"
    )
    assert result.reason == "none"


@pytest.mark.parametrize(
    "keyword", ["minItems", "maxItems", "minProperties", "maxProperties"]
)
@pytest.mark.parametrize(
    ("array", "mapping", "bound", "minimum", "maximum"),
    [
        pytest.param([], {}, 0, True, True, id="empty-zero"),
        pytest.param([], {}, 1, False, True, id="empty-positive"),
        pytest.param([False], {"a": False}, 0, True, False, id="one-above-zero"),
        pytest.param([False], {"a": False}, 1, True, True, id="one-exact"),
        pytest.param(
            [1, 1], {"\x00": 1, "\U0001f600": 1}, 2, True, True, id="duplicates"
        ),
        pytest.param(
            [None, False, 0],
            {"a": None, "b": False, "c": 0},
            3,
            True,
            True,
            id="values",
        ),
        pytest.param([1, 2, 3], {"a": 1, "b": 2, "c": 3}, 2, True, False, id="above"),
        pytest.param([1, 2, 3], {"a": 1, "b": 2, "c": 3}, 4, False, True, id="below"),
        pytest.param([None], {"a": None}, 1.0, True, True, id="integral-float"),
        pytest.param([], {}, -0.0, True, True, id="negative-floating-zero"),
        pytest.param([None], {"a": None}, 1e23, False, True, id="large-integral-float"),
        pytest.param(
            [None], {"a": None}, (1 << 14_000) - 1, False, True, id="large-integer"
        ),
    ],
)
def test_standard_schema_retains_container_count_boundaries(
    keyword: str,
    array: list[object],
    mapping: dict[str, object],
    bound: int | float,
    minimum: bool,
    maximum: bool,
) -> None:
    value = array if keyword.endswith("Items") else mapping
    candidate = source({keyword: bound})
    valid = minimum if keyword.startswith("min") else maximum

    result = validate_standard_schema(value, candidate, SchemaDomain.OUTPUT, binding())

    assert result.outcome == ("valid" if valid else "invalid")
    assert result.reason == "none"


@pytest.mark.parametrize(
    ("candidates", "valid"),
    [
        ([], False),
        ([None, False], True),
        ([False, None], True),
        ([False, False], False),
        ([False, False, None], True),
    ],
)
def test_standard_schema_retains_enum_alternative_decisions(
    candidates: list[object], valid: bool
) -> None:
    candidate = source({"enum": candidates})

    result = validate_standard_schema(None, candidate, SchemaDomain.OUTPUT, binding())

    assert result.outcome == ("valid" if valid else "invalid")
    assert result.reason == "none"


@pytest.mark.parametrize(
    "schema",
    [
        {"type": ["null", False]},
        {"type": ["null", "invalid"]},
        {"type": ["null", {"private-canary": None}]},
        {"type": [False, "null"]},
        {"type": ["invalid", "null"]},
        {"required": ["missing", 1]},
        {"dependentRequired": {"absent": 1}},
        {"dependentRequired": {"z": 1, "a": ["missing"]}},
        {"uniqueItems": ["invalid-internal-flag"]},
        {"uniqueItems": 1},
        {"required": {}},
        {"required": [None]},
        {"dependentRequired": []},
        {"dependentRequired": {"a": None}},
        {"multipleOf": 0},
        {"multipleOf": 0.0},
        {"multipleOf": -0.0},
        {"multipleOf": -1},
        {"multipleOf": -0.1},
    ],
)
def test_standard_schema_retained_unvisited_forms_refuse_whole_source(
    schema: object,
) -> None:
    candidate = source(schema)

    result = validate_standard_schema(None, candidate, SchemaDomain.OUTPUT, binding())

    assert result.outcome == "refused"
    assert result.reason == "unsupported_schema"


@pytest.mark.parametrize(
    "keyword",
    [
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "minProperties",
        "maxProperties",
    ],
)
@pytest.mark.parametrize("bound", [None, False, "1", [], {}])
def test_standard_schema_retained_numeric_argument_kinds_refuse_before_applicability(
    keyword: str, bound: object
) -> None:
    candidate = source({keyword: bound})

    result = validate_standard_schema(None, candidate, SchemaDomain.OUTPUT, binding())

    assert result.outcome == "refused"
    assert result.reason == "unsupported_schema"


@pytest.mark.parametrize("value", [None, False, 1, "array", {}])
def test_standard_schema_retained_enum_requires_array_source(value: object) -> None:
    candidate = source({"enum": value})

    result = validate_standard_schema(None, candidate, SchemaDomain.OUTPUT, binding())

    assert result.outcome == "refused"
    assert result.reason == "unsupported_schema"
