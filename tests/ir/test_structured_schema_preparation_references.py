from typing import cast

import pytest

from tests.ir.structured_schema_cases import (
    builtin,
    contract,
    normalized_input,
    preparation_input,
    prepared,
    refusal,
    stored_field,
)


def test_preparation_orders_reference_records_and_retains_original_decoded_paths() -> (
    None
):
    reference = "#/$defs/a~1b~0c"
    candidate = preparation_input(
        {
            "allOf": [{"$ref": reference, "$dynamicRef": reference}],
            "$defs": {"a/b~c": False},
        }
    )

    result = prepared(candidate)
    normalized = cast("dict[str, object]", normalized_input(result))

    assert normalized["reference_targets"] == [
        {
            "document": 1,
            "path": ["allOf", "0"],
            "keyword": keyword,
            "target_document": 1,
            "target_path": ["$defs", "a/b~c"],
        }
        for keyword in ("$dynamicRef", "$ref")
    ]
    assert result.accounting.references == 2
    assert len(cast("tuple[tuple[int, int], ...]", stored_field(result, "_edges"))) == 3


def test_preparation_keeps_unused_reference_closure_outside_constraints() -> None:
    declared = contract(
        {"$defs": {"unused": {"$ref": "urn:kora:artifact:example:Shared"}}}
    )
    reference_parent = contract(True, "example/ReferenceParent")
    shared = contract(False, "example/Shared", "example/ReferenceParent")
    candidate = preparation_input(
        {}, records=[shared, declared, builtin(), reference_parent]
    )

    result = prepared(candidate)
    normalized = cast("dict[str, object]", normalized_input(result))

    assert result.accounting.documents == 4
    assert result.accounting.positions == 5
    assert normalized["constraint_documents"] == [0, 1]
    assert normalized["checker_document"] == 1
    assert normalized["reference_targets"] == [
        {
            "document": 1,
            "path": ["$defs", "unused"],
            "keyword": "$ref",
            "target_document": 3,
            "target_path": [],
        }
    ]


def test_preparation_counts_shared_schema_occurrences_as_distinct_positions() -> None:
    shared: dict[str, object] = {"type": "string"}
    candidate = preparation_input(
        {
            "$defs": {"left": shared, "right": shared},
            "allOf": [{"$ref": "#/$defs/left"}, {"$ref": "#/$defs/right"}],
        }
    )

    result = prepared(candidate)
    normalized = cast("dict[str, object]", normalized_input(result))

    assert result.accounting.positions == 6
    references = cast("list[dict[str, object]]", normalized["reference_targets"])
    assert [reference["target_path"] for reference in references] == [
        ["$defs", "left"],
        ["$defs", "right"],
    ]


@pytest.mark.parametrize(
    ("reference", "schema"),
    [
        ("#/$defs/leaf", {"$defs": {"leaf": True}}),
        ("#/$defs/a~1b~0c", {"$defs": {"a/b~c": False}}),
        ("#/$defs/%F0%9F%8C%8D", {"$defs": {"🌍": {}}}),
        ("#/$defs/a%20b", {"$defs": {"a b": {}}}),
        ("#/$defs/", {"$defs": {"": True}}),
        ("#/$defs/%00", {"$defs": {"\x00": False}}),
        ("#/allOf/0", {"allOf": [True]}),
    ],
    ids=["local", "tilde", "unicode", "percent", "empty-key", "nul-key", "array"],
)
@pytest.mark.parametrize("keyword", ["$ref", "$dynamicRef"])
def test_structured_output_saved_reference_authority_is_closed(
    reference: str, schema: dict[str, object], keyword: str
) -> None:
    candidate = preparation_input({**schema, keyword: reference})

    result = prepared(candidate)

    assert result.accounting.documents == 2
    assert result.accounting.positions == 3
    assert result.accounting.references == 1


@pytest.mark.parametrize("target", [True, False], ids=["true", "false"])
def test_structured_output_preparation_binds_original_saved_boolean_roots(
    target: bool,
) -> None:
    records = [
        contract({"$ref": "urn:kora:artifact:example:Target"}),
        contract(target, "example/Target"),
        builtin(),
    ]
    candidate = preparation_input({}, records=records)

    result = prepared(candidate)

    assert result.accounting.documents == 3
    assert result.accounting.positions == 3
    assert result.accounting.references == 1


@pytest.mark.parametrize("keyword", ["$ref", "$dynamicRef"])
@pytest.mark.parametrize("fragment", ["", "#/$defs/leaf"], ids=["root", "fragment"])
def test_preparation_refuses_raw_slash_urn_aliases_with_a_present_saved_target(
    keyword: str, fragment: str
) -> None:
    target = contract({"$defs": {"leaf": False}}, "example/Target")
    declared = contract({keyword: "urn:kora:artifact:example/Target" + fragment})
    candidate = preparation_input({}, records=[builtin(), declared, target])

    reason = refusal(candidate)

    assert reason == "unsupported_schema"


@pytest.mark.parametrize("keyword", ["$ref", "$dynamicRef"])
def test_preparation_keeps_fragment_slashes_in_canonical_saved_urns(
    keyword: str,
) -> None:
    target = contract({"$defs": {"a/b": False}}, "example/Target")
    declared = contract({keyword: "urn:kora:artifact:example:Target#/$defs/a~1b"})
    candidate = preparation_input({}, records=[builtin(), declared, target])

    result = prepared(candidate)
    normalized = cast("dict[str, object]", normalized_input(result))

    assert normalized["reference_targets"] == [
        {
            "document": 1,
            "path": [],
            "keyword": keyword,
            "target_document": 2,
            "target_path": ["$defs", "a/b"],
        }
    ]


def test_structured_output_preparation_keeps_equal_local_bases_distinct() -> None:
    schema: dict[str, object] = {
        "$defs": {"leaf": False},
        "$ref": "#/$defs/leaf",
    }
    declared = {
        "allOf": [
            {"$ref": "urn:kora:artifact:example:Left"},
            {"$ref": "urn:kora:artifact:example:Right"},
        ]
    }
    records = [
        builtin(),
        contract(declared),
        contract(schema, "example/Left"),
        contract(schema, "example/Right"),
    ]
    candidate = preparation_input({}, records=records)

    result = prepared(candidate)

    assert result.accounting.documents == 4
    assert result.accounting.positions == 8
    assert result.accounting.references == 4


@pytest.mark.parametrize(
    "reference",
    [
        "https://example.invalid/schema#/$defs/leaf",
        "file:///schema#/$defs/leaf",
        "other.json#/$defs/leaf",
        "urn:kora:artifact:example:Missing",
        "urn:kora:artifact:example:Alias",
        "#named",
        "#/$defs/absent",
        "#/$defs/leaf~",
        "#/$defs/leaf~2",
        "#/$defs/%",
        "#/$defs/%GG",
        "#/$defs/%FF",
        "#/$defs/%ED%A0%80",
        "#/allOf/00",
        "#/allOf/+0",
        "#/allOf/-0",
        "#/allOf/０",
        "#/allOf/1",
        "#/allOf/999999999999999999999999",
        "#/const",
        "#/enum/0",
        "#/examples/0",
        "#/annotation",
        "#/properties",
    ],
    ids=[
        "https",
        "file",
        "relative",
        "missing",
        "root-alias",
        "anchor",
        "missing-pointer",
        "trailing-tilde",
        "invalid-tilde",
        "percent-short",
        "percent-hex",
        "invalid-utf8",
        "surrogate-utf8",
        "leading-zero",
        "plus-index",
        "negative-zero",
        "unicode-index",
        "index-bound",
        "huge-index",
        "const-data",
        "enum-data",
        "examples-data",
        "annotation-data",
        "schema-map",
    ],
)
def test_structured_output_preparation_rejects_unauthorized_targets(
    reference: str,
) -> None:
    schema: dict[str, object] = {
        "$id": "urn:kora:artifact:example:Alias",
        "$defs": {"leaf": True},
        "allOf": [False],
        "const": True,
        "enum": [True],
        "examples": [True],
        "annotation": True,
        "properties": {},
        "$ref": reference,
    }
    candidate = preparation_input(schema)

    reason = refusal(candidate)

    assert reason == "unsupported_schema"


@pytest.mark.parametrize("position", ["$defs", "definitions", "properties"])
def test_structured_output_preparation_rejects_unused_nested_resource_ids(
    position: str,
) -> None:
    candidate = preparation_input(
        {position: {"unused": {"$id": "urn:kora:artifact:example:Nested"}}}
    )

    reason = refusal(candidate)

    assert reason == "unsupported_schema"


@pytest.mark.parametrize(
    "keyword",
    ["pattern", "patternProperties", "unevaluatedItems", "unevaluatedProperties"],
)
def test_structured_output_preparation_rejects_unsupported_unused_schema_positions(
    keyword: str,
) -> None:
    value: object = (
        "a" if keyword == "pattern" else {} if keyword == "patternProperties" else True
    )
    candidate = preparation_input({"$defs": {"unused": {keyword: value}}})

    reason = refusal(candidate)

    assert reason == "unsupported_schema"


@pytest.mark.parametrize(
    "keyword", ["const", "enum", "default", "examples", "annotation"]
)
def test_structured_output_preparation_preserves_schema_data(keyword: str) -> None:
    data: object = {
        "pattern": "[",
        "unevaluatedProperties": False,
        "$id": "https://example.invalid/private",
        "$ref": "https://example.invalid/private",
    }
    if keyword in {"enum", "examples"}:
        data = [data]
    candidate = preparation_input({keyword: data})

    result = prepared(candidate)

    assert result.accounting.positions == 2
    assert result.accounting.references == 0


@pytest.mark.parametrize(
    "vocabulary",
    [
        "https://example.invalid/vocabulary",
        "https://json-schema.org/draft/2020-12/vocab/format-assertion",
    ],
    ids=["unknown", "format-assertion"],
)
def test_structured_output_preparation_refuses_required_unsupported_vocabulary(
    vocabulary: str,
) -> None:
    candidate = preparation_input({"$vocabulary": {vocabulary: True}})

    reason = refusal(candidate)

    assert reason == "unsupported_schema"


def test_structured_output_preparation_keeps_annotation_vocabularies_inert() -> None:
    candidate = preparation_input(
        {
            "$vocabulary": {
                "https://json-schema.org/draft/2020-12/vocab/core": True,
                "https://json-schema.org/draft/2020-12/vocab/applicator": True,
                "https://json-schema.org/draft/2020-12/vocab/validation": True,
                "https://json-schema.org/draft/2020-12/vocab/meta-data": True,
                "https://json-schema.org/draft/2020-12/vocab/format-annotation": True,
                "https://json-schema.org/draft/2020-12/vocab/content": True,
                "https://example.invalid/optional": False,
            },
            "format": "not-a-format",
            "contentMediaType": "not-a-media-type",
            "$recursiveRef": "#",
        }
    )

    result = prepared(candidate)

    assert result.accounting.references == 0


@pytest.mark.parametrize(
    "schema",
    [
        {"$ref": "#"},
        {"allOf": [{"$ref": "#"}]},
        {"properties": {"child": {"$ref": "#"}}},
        {"if": {"$ref": "#"}},
        {"$defs": {"unused": {"$ref": "#/$defs/unused"}}},
        {"$defs": {"a": {"$ref": "#/$defs/b"}, "b": {"$ref": "#/$defs/a"}}},
    ],
    ids=["self", "all-of", "property", "conditional", "unused-self", "unused-pair"],
)
def test_structured_output_preparation_refuses_potential_call_cycles(
    schema: dict[str, object],
) -> None:
    candidate = preparation_input(schema)

    reason = refusal(candidate)

    assert reason == "unsupported_schema"


def test_structured_output_preparation_refuses_cross_document_call_cycle() -> None:
    candidate = preparation_input(
        {},
        records=[
            builtin(),
            contract({"$ref": "urn:kora:artifact:example:Other"}),
            contract({"$ref": "urn:kora:artifact:example:Declared"}, "example/Other"),
        ],
    )

    reason = refusal(candidate)

    assert reason == "unsupported_schema"


@pytest.mark.parametrize(
    "keyword", ["$defs", "definitions", "contentSchema", "dependencies"]
)
def test_preparation_has_no_call_edge_for_nonexecuting_schema_children(
    keyword: str,
) -> None:
    child = {"$ref": "#"}
    schema = {keyword: child if keyword == "contentSchema" else {"child": child}}
    candidate = preparation_input(schema)

    result = prepared(candidate)

    assert result.accounting.positions == 3
    assert result.accounting.references == 1


def test_structured_output_preparation_keeps_shared_call_dag_finite() -> None:
    schema: dict[str, object] = {
        "$defs": {"shared": False},
        "allOf": [{"$ref": "#/$defs/shared"}, {"$ref": "#/$defs/shared"}],
    }
    candidate = preparation_input(schema)

    result = prepared(candidate)

    assert result.accounting.positions == 5
    assert result.accounting.references == 2
    records = cast("list[dict[str, object]]", candidate.type_contracts)
    assert records[1]["schema"] is schema
