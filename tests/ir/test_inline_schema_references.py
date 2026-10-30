import pytest

from kora.ir import validate_inline_schemas
from kora.ir.contracts import SCHEMA_DIALECT
from kora.ir.json import JsonObject
from tests.ir.inline_schema_cases import document_for, schema_node
from tests.ir.type_declaration_cases import custom


@pytest.mark.parametrize("reverse", [False, True])
def test_inline_reference_cache_isolated_by_root_in_both_orders(reverse: bool) -> None:
    roots: list[tuple[str, JsonObject]] = [
        ("valid", {"$ref": "#/$defs/shared", "$defs": {"shared": True}}),
        ("invalid", {"$ref": "#/$defs/shared"}),
    ]
    if reverse:
        roots.reverse()
    document = document_for(
        nodes={name: schema_node("timer", schema) for name, schema in roots}
    )

    result = validate_inline_schemas(document)

    (error,) = result.diagnostics.errors
    assert error.node == "invalid"
    assert error.path == (
        "graph",
        "nodes",
        "invalid",
        "outputs",
        "value",
        "schema",
        "$ref",
    )
    assert result.inline_schemas_checked == result.references_checked == 2
    assert result.schemas_checked == 3


@pytest.mark.parametrize("keyword", ["$ref", "$dynamicRef", "$recursiveRef"])
@pytest.mark.parametrize(
    ("reference", "valid"),
    [
        ("#", True),
        ("", True),
        ("#/$defs/shared", True),
        ("#/$defs/a~1b", True),
        ("#/$defs/%E2%82%AC", True),
        ("#/allOf/0", True),
        ("#/allOf/00", False),
        ("#/allOf/" + "9" * 5000, False),
        ("#/default", False),
        ("#/$defs", False),
        ("#/$defs/nested", False),
        ("#anchor", False),
        ("#/%xx", False),
        ("#/%FF", False),
        ("#/$defs/a~2b", False),
        ("#/$defs/missing", False),
        ("file:///private/schema.json", False),
        ("https://example.invalid/schema.json", False),
        ("schema.json", False),
        ("urn:other:schema", False),
    ],
)
def test_inline_references_are_offline_schema_positions(
    keyword: str, reference: str, valid: bool
) -> None:
    document = document_for(
        {
            keyword: reference,
            "$defs": {
                "shared": True,
                "a/b": False,
                "€": {},
                "nested": {"$id": "urn:nested"},
            },
            "allOf": [True],
            "default": {},
        }
    )

    result = validate_inline_schemas(document)

    paths = {error.path for error in result.diagnostics.errors}
    assert (("graph", "params", keyword) in paths) is not valid
    assert ("graph", "params", "$defs", "<field>", "$id") in paths
    assert len(result.diagnostics.errors) == (1 if valid else 2)
    assert result.references_checked == 1


def test_inline_reference_cycles_do_not_expand_schemas() -> None:
    document = document_for({"$ref": "#/$defs/loop", "$defs": {"loop": {"$ref": "#"}}})

    result = validate_inline_schemas(document)

    assert result.inline_schemas_valid
    assert result.schemas_checked == result.references_checked == 2


@pytest.mark.parametrize("present", [False, True])
@pytest.mark.parametrize(
    "name",
    ["VcsRef", "StructuredOutput", "AgentSession", "CommandResult", "example/Value"],
)
def test_inline_urns_resolve_only_through_supplied_saved_contracts(
    name: str, present: bool
) -> None:
    contract: dict[str, object] = custom() if "/" in name else {"builtin_revision": "1"}
    document = document_for(
        {"$ref": "urn:kora:artifact:" + name.replace("/", ":")},
        contracts={name: contract} if present else {},
    )

    result = validate_inline_schemas(document)

    assert result.inline_schemas_valid is present
    assert (
        result.inline_schemas_checked
        == result.schemas_checked
        == result.references_checked
        == 1
    )


def test_inline_ids_do_not_create_reference_aliases_or_override_saved_roots() -> None:
    urn = "urn:kora:artifact:example:Value"
    document = document_for(
        {"$id": urn, "$defs": {"inline": True}, "$ref": urn + "#/$defs/saved"},
        nodes={"other": schema_node("timer", {"$ref": urn + "#/$defs/inline"})},
        contracts={
            "example/Value": {
                **custom(),
                "schema": {"$schema": SCHEMA_DIALECT, "$defs": {"saved": True}},
            }
        },
    )

    result = validate_inline_schemas(document)

    (error,) = result.diagnostics.errors
    assert error.node == "other"
    assert result.references_checked == 2


def test_inline_ids_cannot_publish_a_missing_saved_contract() -> None:
    urn = "urn:kora:artifact:example:Absent"
    document = document_for(
        {"$id": urn}, nodes={"other": schema_node("timer", {"$ref": urn})}
    )

    result = validate_inline_schemas(document)

    (error,) = result.diagnostics.errors
    assert error.node == "other"


@pytest.mark.parametrize("referenced", [False, True])
def test_inline_pass_does_not_validate_unrelated_saved_contracts(
    referenced: bool,
) -> None:
    document = document_for(
        {"$ref": "urn:kora:artifact:VcsRef"} if referenced else {},
        contracts={
            "VcsRef": {"builtin_revision": "unpublished"},
            "example/Invalid": {
                **custom("example/Missing"),
                "schema": {"$schema": SCHEMA_DIALECT, "type": "invalid"},
            },
        },
    )

    result = validate_inline_schemas(document)

    assert result.inline_schemas_valid is not referenced
    assert result.schemas_checked == 1
    assert all(
        error.path == ("graph", "params", "$ref") for error in result.diagnostics.errors
    )
