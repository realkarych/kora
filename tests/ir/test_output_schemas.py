import json

import pytest
from pydantic import MISSING

from kora.ir import export_ir, validate_output_schemas
from kora.ir.approval_form import default_approval_form
from kora.ir.json import JsonObject
from kora.ir.topology import HumanInputNode
from tests.ir.document_cases import NODE_CASES
from tests.ir.graph_link_cases import node
from tests.ir.inline_schema_cases import document_for
from tests.ir.output_schema_cases import (
    agent,
    contracts,
    deep_value,
    human,
    shared_ancestry,
    shared_body,
)
from tests.ir.type_declaration_cases import custom


@pytest.mark.parametrize(
    ("first", "second", "invalid"),
    [
        ("StructuredOutput", "StructuredOutput", True),
        ("StructuredOutput", "example/Child", True),
        ("example/Child", "example/Grandchild", True),
        ("VcsRef", "VcsRef", True),
        ("VcsRef", "example/Revision", False),
        ("StructuredOutput", "List[StructuredOutput]", False),
        ("example/Child", "List[List[example/Child]]", False),
        ("List[StructuredOutput]", "List[StructuredOutput]", False),
        ("example/Other", "example/Other", False),
    ],
)
@pytest.mark.parametrize("nested", [False, True])
def test_agent_output_roles_follow_exact_and_non_list_family_rules(
    first: str, second: str, invalid: bool, nested: bool
) -> None:
    saved = {
        **contracts(),
        "example/Child": custom("StructuredOutput"),
        "example/Grandchild": custom("example/Child"),
        "example/Revision": custom("VcsRef"),
        "example/Other": custom(),
    }
    candidate = agent({"first": {"type": first}, "second": {"type": second}})
    nodes = {"agent": candidate}
    document = document_for(
        nodes={"timer": node()} if nested else nodes,
        bodies={"unused": {"nodes": nodes}} if nested else None,
        contracts=saved,
    )
    prefix = ("graph", "subgraphs", "unused") if nested else ("graph",)

    result = validate_output_schemas(document)

    assert result.output_schemas_valid is not invalid
    assert result.outputs_checked == 3
    assert result.nodes_checked == (2 if nested else 1)
    assert result.definitions_checked == int(nested)
    assert result.comparisons_checked == result.values_checked == 0
    assert all(
        error.code == "IR003" and error.path == (*prefix, "nodes", "agent", "outputs")
        for error in result.diagnostics.errors
    )
    assert len(result.diagnostics.errors) == int(invalid)


@pytest.mark.parametrize("producer", [False, True])
@pytest.mark.parametrize("declaration", [False, True])
def test_agent_schema_comparison_requires_both_authored_fields(
    producer: bool, declaration: bool
) -> None:
    output: dict[str, object] = {"type": "StructuredOutput"}
    if declaration:
        output["schema"] = {"type": "integer"}
    document = document_for(
        nodes={
            "agent": agent({"data": output}, {"type": "string"} if producer else None)
        },
        contracts=contracts(),
    )
    before = export_ir(document)

    result = validate_output_schemas(document)

    assert result.output_schemas_valid is not (producer and declaration)
    assert result.comparisons_checked == int(producer and declaration)
    assert export_ir(document) == before
    assert all(
        error.path == ("graph", "nodes", "agent", "outputs", "data", "schema")
        for error in result.diagnostics.errors
    )


@pytest.mark.parametrize("declared_type", ["StructuredOutput", "example/Child"])
def test_agent_schema_comparison_ignores_only_object_order(declared_type: str) -> None:
    document = document_for(
        nodes={
            "agent": agent(
                {
                    "data": {
                        "type": declared_type,
                        "schema": {
                            "required": ["x"],
                            "properties": {"x": {"type": "number"}},
                        },
                    }
                },
                {"properties": {"x": {"type": "number"}}, "required": ["x"]},
            )
        },
        contracts={**contracts(), "example/Child": custom("StructuredOutput")},
    )

    result = validate_output_schemas(document)

    assert result.output_schemas_valid
    assert result.comparisons_checked == 1
    assert result.values_checked == 6


def test_harness_only_schema_and_unrelated_outputs_grant_no_artifact_role() -> None:
    document = document_for(
        nodes={
            "agent": agent(
                {"other": {"type": "example/Other", "schema": {"type": "integer"}}},
                {"type": "string"},
            )
        },
        contracts={**contracts(), "example/Other": custom()},
    )

    result = validate_output_schemas(document)

    assert result.output_schemas_valid
    assert result.comparisons_checked == 0


@pytest.mark.parametrize("ending", ["missing", "cycle", "unpublished", "unqualified"])
def test_unknown_ancestry_suppresses_only_dependent_findings(ending: str) -> None:
    saved = contracts()
    bad_type = "example/Bad"
    saved[bad_type] = custom(
        {
            "missing": "example/Missing",
            "cycle": bad_type,
            "unpublished": "UnknownBuiltin",
            "unqualified": "Unqualified",
        }[ending]
    )
    saved["UnknownBuiltin"] = {"builtin_revision": "1"}
    saved["Unqualified"] = custom("StructuredOutput")
    document = document_for(
        nodes={
            "agent": agent(
                {
                    "bad": {"type": bad_type, "schema": {"type": "integer"}},
                    "good": {"type": "StructuredOutput", "schema": {"type": "integer"}},
                    "vcs_one": {"type": "VcsRef"},
                    "vcs_two": {"type": "VcsRef"},
                },
                {"type": "string"},
            ),
            "gate": human({}, {"type": "string"}),
        },
        contracts=saved,
    )

    result = validate_output_schemas(document)

    assert len(result.diagnostics.errors) == 3
    assert result.comparisons_checked == 2
    assert {error.path for error in result.diagnostics.errors} == {
        ("graph", "nodes", "agent", "outputs"),
        ("graph", "nodes", "agent", "outputs", "good", "schema"),
        ("graph", "nodes", "gate", "outputs", "response", "schema"),
    }


def test_unpublished_structured_root_does_not_establish_membership() -> None:
    saved = contracts()
    saved["StructuredOutput"] = {"builtin_revision": "unknown"}
    document = document_for(
        nodes={
            "agent": agent(
                {
                    "a": {"type": "StructuredOutput", "schema": {}},
                    "b": {"type": "StructuredOutput", "schema": {}},
                },
                {"type": "string"},
            )
        },
        contracts=saved,
    )

    result = validate_output_schemas(document)

    assert result.output_schemas_valid
    assert result.comparisons_checked == 0
    assert result.ancestry_steps == 2


@pytest.mark.parametrize("ending", ["valid", "missing", "cycle", "unpublished"])
def test_shared_ancestry_memoizes_valid_and_unknown_prefixes(ending: str) -> None:
    document = shared_ancestry(1000, 1000, ending)

    result = validate_output_schemas(document)

    assert result.output_schemas_valid
    assert result.ancestry_steps == (1001 if ending == "cycle" else 1002)
    assert result.outputs_checked == 2000
    assert result.comparisons_checked == (1000 if ending == "valid" else 0)


@pytest.mark.parametrize(
    ("form", "schema", "valid"),
    [
        (None, None, True),
        (None, {}, False),
        ({}, None, True),
        ({}, {}, True),
        ({"type": "string"}, {"type": "string"}, True),
        ({"type": "string"}, {"type": "integer"}, False),
        ({"const": True}, {"const": 1}, False),
    ],
)
def test_human_schema_comparison_preserves_absence_and_custom_forms(
    form: JsonObject | None, schema: JsonObject | None, valid: bool
) -> None:
    document = document_for(nodes={"gate": human(form, schema)})
    before = export_ir(document)

    result = validate_output_schemas(document)

    assert result.output_schemas_valid is valid
    assert result.comparisons_checked == int(schema is not None)
    assert export_ir(document) == before
    assert all(
        error.path == ("graph", "nodes", "gate", "outputs", "response", "schema")
        for error in result.diagnostics.errors
    )


def test_default_form_comparison_never_inserts_form_predicate_or_response_values() -> (
    None
):
    document = document_for(nodes={"gate": human(schema=default_approval_form())})
    before = export_ir(document)
    candidate = document.graph.nodes["gate"]
    assert isinstance(candidate, HumanInputNode)

    result = validate_output_schemas(document)

    assert result.output_schemas_valid
    assert result.comparisons_checked == 1
    assert result.values_checked == 13
    assert candidate.config.form is MISSING
    assert candidate.config.approve_when is MISSING
    assert candidate.config.default_value is MISSING
    assert export_ir(document) == before


def test_default_form_factory_returns_detached_nested_values() -> None:
    original = default_approval_form()
    expected = default_approval_form()
    properties = original["properties"]
    assert isinstance(properties, dict)

    properties.clear()
    document = document_for(nodes={"gate": human(schema=expected)})
    result = validate_output_schemas(document)

    assert result.output_schemas_valid
    assert default_approval_form() == expected


@pytest.mark.parametrize("kind", NODE_CASES)
def test_output_consistency_visits_all_kinds_without_claiming_other_validation(
    kind: str,
) -> None:
    document = document_for(nodes={"candidate": node(kind)}, contracts=contracts())

    result = validate_output_schemas(document)

    assert result.output_schemas_valid
    assert result.nodes_checked == 1
    assert result.outputs_checked == int(kind in {"agent", "human_input"})
    assert result.comparisons_checked == 0


def test_output_consistency_visits_shared_body_once_and_unused_body() -> None:
    shared = shared_body(1000)
    unused = document_for(
        bodies={"unused": {"nodes": {"gate": human({}, {"type": "string"})}}}
    )

    shared_result = validate_output_schemas(shared)
    unused_result = validate_output_schemas(unused)

    assert shared_result.nodes_checked == 1001
    assert shared_result.definitions_checked == shared_result.comparisons_checked == 1
    (error,) = unused_result.diagnostics.errors
    assert error.path == (
        "graph",
        "subgraphs",
        "unused",
        "nodes",
        "gate",
        "outputs",
        "response",
        "schema",
    )


@pytest.mark.parametrize(
    "source",
    ["node.py", "/private/secret.py", "https://example.invalid/secret", "x" * 1025],
)
def test_output_consistency_uses_only_safe_owning_node_source(source: str) -> None:
    document = document_for(
        nodes={
            "gate": human({}, {"type": "string"}, source={"file": source, "line": 9})
        },
        source={"file": "graph.py", "line": 1},
    )

    result = validate_output_schemas(document)

    (error,) = result.diagnostics.errors
    assert error.node == "gate"
    if source == "node.py":
        assert error.location is not None
        assert (error.location.file, error.location.line) == (source, 9)
    else:
        assert error.location is None


def test_deep_output_mismatch_excludes_authored_paths_and_content() -> None:
    marker = "private_schema_content"
    left: JsonObject = {marker: deep_value(5000)}
    right: JsonObject = {marker: deep_value(5000, 1)}
    document = document_for(nodes={"gate": human(left, right)})

    result = validate_output_schemas(document)

    (error,) = result.diagnostics.errors
    assert result.values_checked == 5002
    assert error.code == "IR003"
    assert error.path == ("graph", "nodes", "gate", "outputs", "response", "schema")
    assert marker not in json.dumps(error.model_dump(mode="json"))


def test_dense_output_mismatches_preserve_collection_bounds_and_omissions() -> None:
    document = document_for(
        nodes={f"n{index}": human({}, {"type": "string"}) for index in range(3000)}
    )

    result = validate_output_schemas(document)

    assert not result.output_schemas_valid
    assert result.nodes_checked == result.comparisons_checked == 3000
    assert len(result.diagnostics.errors) == 1000
    assert result.diagnostics.errors_omitted == 2000
    assert (
        len(
            json.dumps(
                [error.model_dump(mode="json") for error in result.diagnostics.errors]
            ).encode()
        )
        < 1_048_576
    )
