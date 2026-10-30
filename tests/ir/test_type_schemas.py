import json

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError

from kora.ir import TypeSchemaResult, validate_type_schemas
from kora.ir.json import JsonObject, JsonValue
from kora.ir.schema_locations import LISTED_SCHEMAS, NAMED_SCHEMAS, SINGLE_SCHEMAS
from tests.ir.type_declaration_cases import custom, model
from tests.ir.type_schema_cases import document_for, nested_schema, wide_schema


def paths(result: TypeSchemaResult) -> set[tuple[str | int, ...] | None]:
    return {error.path for error in result.diagnostics.errors}


def rendered(result: TypeSchemaResult) -> str:
    return json.dumps(
        [error.model_dump(mode="json") for error in result.diagnostics.errors]
    )


SCHEMA_CORPUS: list[JsonObject] = [
    {},
    {"type": "object"},
    {"type": ["null", "object"]},
    {"type": []},
    {"type": ["string", "string"]},
    {"type": ["unknown", {}]},
    {"maxItems": 1.0},
    {"maxItems": 1.5},
    {"maxItems": True},
    {"maxItems": -1},
    {"minimum": -(10**5000)},
    {"multipleOf": 0},
    {"minItems": -1, "maxItems": -1},
    {"required": ["x", "x"]},
    {"required": ["x", 1]},
    {"required": "x"},
    {"title": {}},
    {"uniqueItems": 1},
    {"deprecated": False},
    {"readOnly": None},
    {"pattern": "["},
    {"pattern": "[a-z]+"},
    {"patternProperties": {"[": True}},
    {"patternProperties": {"a": False}},
    {"dependentRequired": {"x": ["y", "y"]}},
    {"dependentRequired": {"x": {}}},
    {"dependencies": {"x": ["y"], "y": False}},
    {"dependencies": {"x": 1}},
    {"$vocabulary": {"urn:example:vocabulary": False}},
    {"$vocabulary": {"x": 1}},
    {"$anchor": "valid_name"},
    {"$anchor": "not an anchor"},
    {"$dynamicAnchor": "a"},
    {"$recursiveAnchor": True},
    {"enum": []},
    {"enum": [1, 1]},
    {"enum": {}},
    {"examples": [nested_schema(50)]},
    {"examples": {}},
    {"const": nested_schema(50)},
    {"default": nested_schema(50)},
    {"extension": {"type": "invalid", "$ref": "https://example.invalid"}},
    *({keyword: {"type": "invalid"}} for keyword in sorted(SINGLE_SCHEMAS)),
    *({keyword: {"a": {"type": "invalid"}}} for keyword in sorted(NAMED_SCHEMAS)),
    *({keyword: [{"type": "invalid"}]} for keyword in sorted(LISTED_SCHEMAS)),
    *({keyword: []} for keyword in sorted(LISTED_SCHEMAS)),
    *(
        {keyword: "invalid"}
        for keyword in sorted(SINGLE_SCHEMAS | NAMED_SCHEMAS | LISTED_SCHEMAS)
    ),
]


@pytest.mark.parametrize("schema", SCHEMA_CORPUS)
def test_type_schema_matches_real_metaschema(schema: JsonObject) -> None:
    document = document_for(schema)
    try:
        Draft202012Validator.check_schema(
            schema, format_checker=FormatChecker(formats=("regex",))
        )
        expected = True
    except SchemaError:
        expected = False

    result = validate_type_schemas(document)

    assert result.schemas_valid is expected
    assert all(error.code == "IR022" for error in result.diagnostics.errors)


@pytest.mark.parametrize(
    "keyword",
    sorted(SINGLE_SCHEMAS | NAMED_SCHEMAS | LISTED_SCHEMAS | {"dependencies"}),
)
def test_type_schema_visits_every_schema_position(keyword: str) -> None:
    child: JsonObject = {"$ref": "urn:kora:artifact:missing:Value"}
    value: JsonValue = (
        child
        if keyword in SINGLE_SCHEMAS
        else (
            [child, False]
            if keyword in LISTED_SCHEMAS
            else {"secret-field": child, "other": False}
        )
    )
    document = document_for({keyword: value})

    result = validate_type_schemas(document)

    assert not result.schemas_valid
    assert result.references_checked == 1
    assert result.schemas_checked == (2 if keyword in SINGLE_SCHEMAS else 3)
    assert "secret-field" not in rendered(result)


@pytest.mark.parametrize(
    "reference",
    [
        "",
        "#",
        "#/$defs/node",
        "#/$defs/false",
        "#/$defs/a~1b~0c",
        "#/$defs/%D0%B0",
        "#/allOf/0",
        "urn:kora:artifact:example:Value#/$defs/node",
    ],
)
@pytest.mark.parametrize("keyword", ["$ref", "$dynamicRef", "$recursiveRef"])
def test_type_schema_accepts_offline_pointer_targets(
    reference: str, keyword: str
) -> None:
    document = document_for(
        {
            keyword: reference,
            "$defs": {"node": {"$ref": "#"}, "false": False, "a/b~c": {}, "а": {}},
            "allOf": [True],
        }
    )

    result = validate_type_schemas(document)

    assert result.schemas_valid
    assert result.references_checked == 2


@pytest.mark.parametrize(
    "reference",
    [
        "https://example.invalid/schema",
        "file:///etc/passwd",
        "relative.json",
        "other#/$defs/node",
        "#anchor",
        "#/$defs/missing",
        "#/default",
        "#/const/child",
        "#/examples/0",
        "#/extension",
        "#/$defs",
        "#/allOf",
        "#/allOf/01",
        "#/allOf/-",
        "#/allOf/1",
        "#/$defs/~2",
        "#/$defs/%",
        "#/$defs/%GG",
        "#/$defs/%FF",
        "#/$defs/а",
        "#/$defs/a b",
        "#/$defs/a#b",
        "#/$defs/scoped",
        "urn:kora:artifact:VcsRef",
        "urn:alias",
        "#/$defs/node/type",
    ],
)
def test_type_schema_refuses_nonclosed_or_nonschema_targets(reference: str) -> None:
    document = document_for(
        {
            "$id": "urn:alias",
            "$ref": reference,
            "$anchor": "anchor",
            "$defs": {"node": {"type": "string"}, "scoped": {"$id": "urn:scoped"}},
            "default": {},
            "const": {"child": {}},
            "examples": [{}],
            "extension": {},
            "allOf": [True],
        }
    )

    result = validate_type_schemas(document)

    assert ("type_contracts", "example/Value", "schema", "$ref") in paths(result)
    assert all(
        error.message == "The saved type schema or reference is invalid."
        for error in result.diagnostics.errors
    )


def test_type_schema_checks_unused_contracts_and_all_published_builtins() -> None:
    contract = custom()
    contract["schema"] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "allOf": [
            {"$ref": f"urn:kora:artifact:{name}"}
            for name in ("VcsRef", "StructuredOutput", "AgentSession", "CommandResult")
        ],
    }
    document = model(
        {
            "example/Unused": contract,
            **{
                name: {"builtin_revision": "1"}
                for name in (
                    "VcsRef",
                    "StructuredOutput",
                    "AgentSession",
                    "CommandResult",
                )
            },
        }
    )

    result = validate_type_schemas(document)

    assert result.schemas_valid
    assert result.contracts_checked == 5
    assert result.references_checked >= 4


@pytest.mark.parametrize(("name", "revision"), [("VcsRef", "2"), ("Unknown", "1")])
def test_type_schema_refuses_unpublished_builtin(name: str, revision: str) -> None:
    document = model({name: {"builtin_revision": revision}})

    result = validate_type_schemas(document)

    assert paths(result) == {("type_contracts", name, "builtin_revision")}
    assert result.schemas_checked == 0


def test_type_schema_checks_nested_dialects_and_resource_ids() -> None:
    document = document_for(
        {
            "properties": {
                "private": {
                    "$schema": "https://json-schema.org/draft-07/schema",
                    "$id": "urn:other",
                }
            }
        }
    )

    result = validate_type_schemas(document)

    assert paths(result) == {
        ("type_contracts", "example/Value", "schema", "properties", "<field>", keyword)
        for keyword in ("$schema", "$id")
    }


def test_type_schema_collects_independent_and_mixed_keyword_faults() -> None:
    document = document_for(
        {
            "minItems": -1,
            "maxItems": -1,
            "required": ["x", "x", {}],
            "dependentRequired": {"a": [1], "b": ["x", "x"]},
        }
    )

    result = validate_type_schemas(document)

    assert len(result.diagnostics.errors) == 6
    assert {path[-1] for path in paths(result) if path} >= {
        "minItems",
        "maxItems",
        1,
        2,
    }


@pytest.mark.parametrize("keyword", ["title", "type", "required"])
def test_type_schema_deep_malformed_values_never_reach_native_rendering(
    keyword: str,
) -> None:
    document = document_for({keyword: nested_schema(1200), "minItems": -1})

    result = validate_type_schemas(document)

    assert len(result.diagnostics.errors) == 2
    assert result.schemas_checked == 1


@pytest.mark.parametrize(
    "pattern", ["[", "(" * 1200, "a{999999999999999999999}", "a{" + "9" * 5000 + "}"]
)
def test_type_schema_regex_refusal_preserves_other_faults(pattern: str) -> None:
    document = document_for(
        {
            "pattern": pattern,
            "maxItems": -1,
            "patternProperties": {pattern: True, "[other": False},
        }
    )

    result = validate_type_schemas(document)

    assert len(result.diagnostics.errors) == 4
    assert all(
        error.message == "The saved type schema or reference is invalid."
        for error in result.diagnostics.errors
    )
    assert all(
        isinstance(segment, int) or len(segment) < 64
        for path in paths(result)
        if path
        for segment in path
    )


def test_type_schema_deep_walk_and_opaque_data() -> None:
    document = document_for(
        nested_schema(
            1200,
            {
                "type": "invalid",
                "const": nested_schema(1200),
                "enum": [nested_schema(1200)],
                "default": {"$ref": "https://example.invalid"},
            },
        )
    )

    result = validate_type_schemas(document)

    assert result.schemas_checked == 1201
    assert len(result.diagnostics.errors) == 1
    assert result.diagnostics.errors[0].path is not None
    assert len(result.diagnostics.errors[0].path) == 64
    assert result.references_checked == 0


def test_type_schema_dense_faults_remain_bounded_and_deterministic() -> None:
    schema = wide_schema(1500, invalid=True)
    first = document_for(schema)
    second = document_for(dict(reversed(list(schema.items()))))

    results = [validate_type_schemas(document) for document in (first, second)]

    assert results[0] == results[1]
    assert results[0].schemas_checked == 1502
    assert results[0].references_checked == 1500
    assert not results[0].schemas_valid
    assert len(results[0].diagnostics.errors) == 1000
    assert results[0].diagnostics.errors_omitted == 500
    assert len(rendered(results[0]).encode()) < 1_048_576


def test_type_schema_wide_invalid_type_alternatives_are_collected_incrementally() -> (
    None
):
    document = document_for(
        {"type": [nested_schema(2) for _ in range(3000)], "maxItems": -1}
    )

    result = validate_type_schemas(document)

    assert not result.schemas_valid
    assert len(result.diagnostics.errors) == 1000
    assert result.diagnostics.errors_omitted == 2001


def test_type_schema_recursive_cross_contract_references_do_not_expand() -> None:
    first, second = custom(), custom()
    first["schema"] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$ref": "urn:kora:artifact:example:B",
    }
    second["schema"] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$ref": "urn:kora:artifact:example:A",
    }
    document = model({"example/A": first, "example/B": second})

    result = validate_type_schemas(document)

    assert result.schemas_valid
    assert result.schemas_checked == result.references_checked == 2


def test_type_schema_long_contract_name_uses_safe_ancestor() -> None:
    name = "example/" + "T" * 300
    contract = custom()
    contract["schema"] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "items": {"type": "invalid"},
    }
    document = model({name: contract})

    result = validate_type_schemas(document)

    assert paths(result) == {("type_contracts",)}
    assert name not in rendered(result)


@pytest.mark.parametrize("keyword", ["$dynamicRef", "$recursiveRef"])
def test_type_schema_legacy_and_dynamic_references_stay_offline(keyword: str) -> None:
    document = document_for({"$dynamicAnchor": "node", keyword: "#node"})

    result = validate_type_schemas(document)

    assert paths(result) == {("type_contracts", "example/Value", "schema", keyword)}


def test_type_schema_does_not_mutate_schema_or_contract_maps() -> None:
    document = document_for(wide_schema(10))
    before = document.model_dump_json(by_alias=True)

    result = validate_type_schemas(document)

    assert result.schemas_valid
    assert document.model_dump_json(by_alias=True) == before
