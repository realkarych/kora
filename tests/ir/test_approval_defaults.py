import json

import pytest
from pydantic import MISSING

from kora.ir import (
    HumanInputNode,
    export_ir,
    validate_approval_defaults,
    validate_expression_syntax,
    validate_inline_schemas,
)
from kora.ir.approval_form import default_approval_form
from kora.ir.json import JsonObject, JsonValue
from tests.ir.approval_default_cases import (
    decision_form,
    gate,
    saved_form,
    stored_bodies,
)
from tests.ir.document_cases import NODE_CASES
from tests.ir.graph_link_cases import node
from tests.ir.inline_schema_cases import document_for


def test_default_approval_form_and_expression_eligibility_preserves_omissions() -> None:
    document = document_for(nodes={"gate": gate()})
    before = export_ir(document)
    subject = document.graph.nodes["gate"]
    assert isinstance(subject, HumanInputNode)
    fields = subject.config.model_fields_set.copy()

    result = validate_approval_defaults(document)

    assert result.approval_defaults_valid
    assert result.nodes_checked == result.human_inputs_checked == 1
    assert result.predicates_checked == result.expressions_checked == 1
    assert result.predicates_unresolved == result.definitions_checked == 0
    assert result.schema_steps > 0
    assert result.checker_steps > 0
    assert subject.config.form is MISSING
    assert subject.config.approve_when is MISSING
    assert subject.config.default_value is MISSING
    assert subject.config.model_fields_set == fields
    assert export_ir(document) == before


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize(
    "form",
    [{}, {"type": "object"}, {"properties": {"comments": {"type": "string"}}}],
)
def test_custom_form_without_decision_requires_approve_when(
    form: JsonObject, nested: bool
) -> None:
    subject = gate(form, source={"file": "node.py", "line": 9})
    document = document_for(
        nodes={"timer": node()} if nested else {"gate": subject},
        bodies={"unused": {"nodes": {"gate": subject}}} if nested else None,
    )
    before = export_ir(document)
    prefix = ("graph", "subgraphs", "unused") if nested else ("graph",)

    result = validate_approval_defaults(document)

    assert not result.approval_defaults_valid
    assert result.predicates_checked == 1
    assert result.expressions_checked == result.predicates_unresolved == 0
    (error,) = result.diagnostics.errors
    assert error.code == "IR011"
    assert error.message == "The form requires an explicit approval predicate."
    assert error.path == (*prefix, "nodes", "gate", "config", "approve_when")
    assert error.node == "gate"
    assert error.offset is None
    assert error.location is not None
    assert (error.location.file, error.location.line) == ("node.py", 9)
    assert export_ir(document) == before


@pytest.mark.parametrize(
    ("member", "required", "valid"),
    [
        ({"type": "string"}, True, True),
        ({"enum": ["approve", "reject"]}, True, True),
        ({"const": "approve"}, True, True),
        ({"enum": ["reject"]}, True, False),
        ({"const": "reject"}, True, False),
        ({"type": "string"}, False, False),
        ({"type": "integer"}, True, False),
        ({"type": "number"}, True, False),
        ({"type": "boolean"}, True, False),
        ({"type": "array"}, True, False),
        ({"type": "object"}, True, False),
        ({"type": ["string", "null"]}, True, True),
        (False, True, True),
        (True, True, True),
        ({}, True, True),
        ({"enum": ["reject", 1]}, True, True),
        ({"enum": [True, 1]}, True, True),
        ({"enum": [1, 2]}, True, False),
        ({"enum": [True, False]}, True, False),
        ({"type": "string", "enum": [True]}, True, True),
    ],
)
def test_default_eligibility_follows_checker_without_evaluating_schema_truth(
    member: JsonValue, required: bool, valid: bool
) -> None:
    document = document_for(nodes={"gate": gate(decision_form(member, required))})

    result = validate_approval_defaults(document)

    assert result.approval_defaults_valid is valid
    assert result.predicates_checked == result.expressions_checked == 1
    assert result.predicates_unresolved == 0
    assert len(result.diagnostics.errors) == int(not valid)
    assert all(
        error.code == "IR011" and error.offset is None
        for error in result.diagnostics.errors
    )


@pytest.mark.parametrize(
    "predicate", ["response.decision == 'reject'", "true", "0", "unknown &&"]
)
@pytest.mark.parametrize(
    "form", [{}, {"$ref": "https://unavailable.invalid/schema"}, None]
)
def test_explicit_predicates_are_preserved_without_checking(
    predicate: str, form: JsonObject | None
) -> None:
    document = document_for(nodes={"gate": gate(form, predicate)})
    before = export_ir(document)

    result = validate_approval_defaults(document)

    assert result.approval_defaults_valid
    assert result.human_inputs_checked == 1
    assert result.predicates_checked == result.predicates_unresolved == 0
    assert (
        result.expressions_checked == result.schema_steps == result.checker_steps == 0
    )
    assert export_ir(document) == before


@pytest.mark.parametrize("valid", [False, True])
@pytest.mark.parametrize(
    "wrapper", ["ref", "allOf", "saved", "decision_ref", "builtin"]
)
def test_approval_default_resolves_only_existing_mapper_schema_rules(
    valid: bool, wrapper: str
) -> None:
    member: JsonObject = {"enum": ["approve", "reject"] if valid else ["reject"]}
    form: JsonObject = decision_form(member)
    saved: dict[str, dict[str, object]] = {}
    if wrapper == "ref":
        form = {"$defs": {"form": form}, "$ref": "#/$defs/form", "type": "boolean"}
    elif wrapper == "allOf":
        form = {"allOf": [form], "type": "boolean"}
    elif wrapper == "saved":
        saved["example/Form"] = saved_form(form)
        form = {"$ref": "urn:kora:artifact:example:Form"}
    elif wrapper == "decision_ref":
        form = {
            **decision_form({"$ref": "#/$defs/decision"}),
            "$defs": {"decision": member},
        }
    else:
        saved["VcsRef"] = {"builtin_revision": "1"}
        form = decision_form({"$ref": "urn:kora:artifact:VcsRef#/properties/branch"})
    document = document_for(nodes={"gate": gate(form)}, contracts=saved)

    result = validate_approval_defaults(document)

    assert result.approval_defaults_valid is (valid or wrapper == "builtin")
    assert result.predicates_checked == result.expressions_checked == 1
    assert result.predicates_unresolved == 0


@pytest.mark.parametrize(
    "form",
    [
        {"$ref": "#/$defs/missing"},
        {"$ref": "urn:kora:artifact:example:Missing"},
        {"$ref": "urn:kora:artifact:StructuredOutput"},
        {"$ref": "https://unavailable.invalid/schema"},
        {"properties": {"decision": {"type": "unknown"}}, "required": ["decision"]},
        {"properties": {"decision": {"maxLength": True, "type": "string"}}},
    ],
)
def test_missing_schema_prerequisites_remain_unresolved_and_other_nodes_continue(
    form: JsonObject,
) -> None:
    document = document_for(
        nodes={"unresolved": gate(form), "invalid": gate({}), "valid": gate()},
        contracts={"StructuredOutput": {"builtin_revision": "unavailable"}},
    )

    result = validate_approval_defaults(document)
    schemas = validate_inline_schemas(document)

    assert not result.approval_defaults_valid
    assert result.human_inputs_checked == 3
    assert result.predicates_checked == 2
    assert result.expressions_checked == result.predicates_unresolved == 1
    assert len(result.diagnostics.errors) == 1
    assert result.diagnostics.errors[0].node == "invalid"
    assert not schemas.inline_schemas_valid
    assert all(error.code == "IR022" for error in schemas.diagnostics.errors)


def test_unresolved_prerequisites_cannot_report_scoped_success() -> None:
    document = document_for(
        nodes={"gate": gate({"$ref": "urn:kora:artifact:example:Missing"})}
    )

    result = validate_approval_defaults(document)

    assert result.diagnostics.valid
    assert result.predicates_unresolved == 1
    assert result.predicates_checked == 0
    assert not result.approval_defaults_valid


@pytest.mark.parametrize(
    "source", [None, "/secret.py", "https://host/secret", "x" * 1025]
)
def test_approval_default_uses_only_safe_owning_source(source: str | None) -> None:
    subject = gate({})
    if source is not None:
        subject["source"] = {"file": source, "line": 1}
    document = document_for(
        bodies={
            "unused": {
                "nodes": {"gate": subject},
                "source": {"file": "body.py", "line": 2},
            }
        },
        source={"file": "graph.py", "line": 3},
    )

    result = validate_approval_defaults(document)

    (error,) = result.diagnostics.errors
    assert error.location is None
    assert error.offset is None


def test_approval_default_visits_all_stored_bodies_once() -> None:
    document = stored_bodies(1000)

    result = validate_approval_defaults(document)

    assert result.nodes_checked == 1003
    assert result.definitions_checked == result.human_inputs_checked == 2
    assert result.predicates_checked == 2
    assert result.expressions_checked == 1
    (error,) = result.diagnostics.errors
    assert error.path == (
        "graph",
        "subgraphs",
        "unused",
        "nodes",
        "gate",
        "config",
        "approve_when",
    )


@pytest.mark.parametrize("kind", NODE_CASES)
def test_approval_default_ignores_other_kinds_and_authored_syntax(kind: str) -> None:
    document = document_for(nodes={"subject": node(kind)})

    result = validate_approval_defaults(document)
    syntax = validate_expression_syntax(document)

    assert result.approval_defaults_valid
    assert result.nodes_checked == 1
    assert (
        result.human_inputs_checked
        == result.predicates_checked
        == int(kind == "human_input")
    )
    assert syntax.expressions_checked == int(kind == "branch")


@pytest.mark.parametrize("byte_pressure", [False, True])
def test_approval_default_diagnostics_remain_bounded_and_content_free(
    byte_pressure: bool,
) -> None:
    marker = "private_schema_content"
    form = decision_form({"enum": [marker]})
    document = document_for(
        nodes={
            "n" * (60 if byte_pressure else 1) + str(index): gate(
                form, source={"file": "s" * (1000 if byte_pressure else 1), "line": 1}
            )
            for index in range(3000)
        }
    )

    result = validate_approval_defaults(document)
    payload = json.dumps(
        {
            "errors": [
                error.model_dump(mode="json") for error in result.diagnostics.errors
            ],
            "warnings": [],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()

    assert not result.approval_defaults_valid
    assert result.predicates_checked == result.expressions_checked == 3000
    assert len(result.diagnostics.errors) + result.diagnostics.errors_omitted == 3000
    assert (
        len(result.diagnostics.errors) < 1000
        if byte_pressure
        else len(result.diagnostics.errors) == 1000
    )
    assert len(payload) <= 1_048_576
    assert marker.encode() not in payload


def test_schema_annotations_never_fill_default_fields_or_change_the_form() -> None:
    form = default_approval_form()
    properties = form["properties"]
    assert isinstance(properties, dict)
    properties["decision"] = {"type": "string", "default": "approve"}
    form["required"] = []
    document = document_for(nodes={"gate": gate(form)})
    before = export_ir(document)

    result = validate_approval_defaults(document)

    assert not result.approval_defaults_valid
    assert export_ir(document) == before
