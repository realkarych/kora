import json

import pytest
from pydantic import MISSING

from kora.ir import (
    HumanInputNode,
    export_ir,
    validate_approval_defaults,
    validate_approval_response_predicates,
    validate_expression_syntax,
    validate_inline_schemas,
)
from kora.ir.diagnostic_collection import MAX_DIAGNOSTIC_BYTES, MAX_DIAGNOSTICS
from kora.ir.json import JsonObject
from tests.ir.approval_default_cases import decision_form, gate, saved_form
from tests.ir.graph_link_cases import node
from tests.ir.inline_schema_cases import document_for


@pytest.mark.parametrize(
    ("form", "predicate", "valid"),
    [
        (None, "response.decision == 'approve'", True),
        (None, "response.decision == 'unknown'", False),
        (None, "response.comments", False),
        ({}, "true", True),
        ({}, "response.unknown", True),
        ({}, "response == null", True),
        ({}, "size(response)", False),
        ({}, "null", False),
        ({}, "1", False),
        ({}, "observation.ok", False),
        ({}, "payload.ok", False),
        (
            {"properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
            "response.ok",
            True,
        ),
        ({"properties": {"ok": {"type": "boolean"}}}, "response.ok", False),
        (
            {"properties": {"ok": {"type": "boolean"}}},
            "has(response.ok) && response.ok",
            True,
        ),
        (
            {"properties": {"ok": {"type": ["boolean", "null"]}}, "required": ["ok"]},
            "response.ok",
            False,
        ),
        (
            {"properties": {"ok": {"type": ["boolean", "null"]}}, "required": ["ok"]},
            "response.ok == null",
            True,
        ),
        (decision_form(False), "response.decision", True),
        (decision_form(True), "response.decision", True),
        ({"type": "null"}, "response", True),
        ({"enum": [True, 1]}, "response", True),
        ({"$defs": {"value": False}, "$ref": "#/$defs/value"}, "response", True),
        (decision_form({"enum": ["reject"]}), "response.decision == 'approve'", False),
        (
            decision_form({"enum": ["approve", "reject"]}),
            "response.decision == 'approve'",
            True,
        ),
    ],
)
def test_explicit_approval_effective_form(
    form: JsonObject | None, predicate: str, valid: bool
) -> None:
    document = document_for(nodes={"gate": gate(form, predicate)})
    before = export_ir(document)
    subject = document.graph.nodes["gate"]
    assert isinstance(subject, HumanInputNode)
    fields = subject.config.model_fields_set.copy()

    result = validate_approval_response_predicates(document)

    assert result.approval_predicates_complete
    assert result.approval_predicates_valid is valid
    assert result.predicates_seen == result.predicates_checked == 1
    assert (
        result.syntax_unresolved
        == result.context_unresolved
        == result.schema_unresolved
        == 0
    )
    assert len(result.diagnostics.errors) == int(not valid)
    assert all(error.code == "IR011" for error in result.diagnostics.errors)
    assert subject.config.approve_when == predicate
    assert (subject.config.form is MISSING) is (form is None)
    assert subject.config.default_value is MISSING
    assert subject.config.model_fields_set == fields
    assert export_ir(document) == before


@pytest.mark.parametrize(
    "predicate",
    [
        "inputs.ok",
        "params.ok",
        "false && inputs.ok",
        "true ? response.ok : params.ok",
        "!inputs.ok",
        "response[params.key]",
        "[inputs.ok].size() == 1",
        "{'value': params.ok}['value']",
        "{inputs.key: true}['value']",
        "has(inputs.ok)",
        "response.text.contains(params.text)",
        "[true].all(inputs, inputs)",
        "[true].map(params, params)",
        "[true].filter(inputs, inputs).size() > 0",
    ],
)
def test_explicit_approval_context_is_incomplete(predicate: str) -> None:
    document = document_for(nodes={"gate": gate({}, predicate)})
    before = export_ir(document)

    result = validate_approval_response_predicates(document)

    assert result.diagnostics.valid
    assert not result.approval_predicates_complete
    assert not result.approval_predicates_valid
    assert result.predicates_seen == result.context_unresolved == 1
    assert (
        result.predicates_checked
        == result.syntax_unresolved
        == result.schema_unresolved
        == 0
    )
    assert result.schema_steps == 0
    assert export_ir(document) == before


@pytest.mark.parametrize(
    "predicate",
    [
        "'inputs' == 'inputs'",
        "'inputs' in {'inputs': true}",
        "response.inputs",
        "response.params",
        "[true].all(value, value)",
        "!false",
        "'params'.contains('params')",
    ],
)
def test_explicit_approval_context_scan_preserves_fields_and_literals(
    predicate: str,
) -> None:
    form: JsonObject = {
        "properties": {"inputs": {"type": "boolean"}, "params": {"type": "boolean"}},
        "required": ["inputs", "params"],
    }
    document = document_for(nodes={"gate": gate(form, predicate)})

    result = validate_approval_response_predicates(document)

    assert result.approval_predicates_complete
    assert result.approval_predicates_valid
    assert result.predicates_seen == result.predicates_checked == 1
    assert result.context_unresolved == 0


def test_explicit_approval_response_binder_remains_a_typed_error() -> None:
    document = document_for(nodes={"gate": gate({}, "[true].all(response, response)")})

    result = validate_approval_response_predicates(document)

    assert result.approval_predicates_complete
    assert not result.approval_predicates_valid
    assert result.predicates_checked == 1
    assert result.context_unresolved == 0
    assert [error.code for error in result.diagnostics.errors] == ["IR011"]


@pytest.mark.parametrize("wrapper", ["ref", "allOf", "saved", "member_ref", "builtin"])
def test_explicit_approval_uses_existing_saved_schema_resolution(wrapper: str) -> None:
    form: JsonObject = decision_form({"enum": ["approve", "reject"]})
    contracts: dict[str, dict[str, object]] = {}
    if wrapper == "ref":
        form = {"$defs": {"form": form}, "$ref": "#/$defs/form", "type": "boolean"}
    elif wrapper == "allOf":
        form = {"allOf": [form], "type": "boolean"}
    elif wrapper == "saved":
        contracts["example/Form"] = saved_form(form)
        form = {"$ref": "urn:kora:artifact:example:Form"}
    elif wrapper == "member_ref":
        form = {
            **decision_form({"$ref": "#/$defs/decision"}),
            "$defs": {"decision": {"type": "string"}},
        }
    else:
        contracts["VcsRef"] = {"builtin_revision": "1"}
        form = decision_form({"$ref": "urn:kora:artifact:VcsRef#/properties/branch"})
    document = document_for(
        nodes={"gate": gate(form, "response.decision == 'approve'")},
        contracts=contracts,
    )

    result = validate_approval_response_predicates(document)

    assert result.approval_predicates_complete
    assert result.approval_predicates_valid
    assert result.predicates_checked == 1
    assert result.schema_unresolved == 0


@pytest.mark.parametrize(
    "form",
    [
        {"$ref": "#/$defs/missing"},
        {"$ref": "urn:kora:artifact:example:Missing"},
        {"$ref": "urn:kora:artifact:StructuredOutput"},
        {"$ref": "https://unavailable.invalid/schema"},
        {"properties": {"ok": {"type": "unknown"}}},
        {"properties": {"ok": {"maxLength": True, "type": "string"}}},
    ],
)
def test_explicit_approval_schema_prerequisites_remain_incomplete(
    form: JsonObject,
) -> None:
    document = document_for(
        nodes={
            "unresolved": gate(form, "true"),
            "valid": gate({}, "true"),
            "invalid": gate({}, "0"),
        },
        contracts={"StructuredOutput": {"builtin_revision": "unavailable"}},
    )

    result = validate_approval_response_predicates(document)
    schemas = validate_inline_schemas(document)

    assert not result.approval_predicates_complete
    assert not result.approval_predicates_valid
    assert result.predicates_seen == 3
    assert result.predicates_checked == 2
    assert result.schema_unresolved == 1
    assert [error.node for error in result.diagnostics.errors] == ["invalid"]
    assert all(error.code == "IR022" for error in schemas.diagnostics.errors)


@pytest.mark.parametrize("predicate", ["true &&", "(" * 33 + "true" + ")" * 33])
def test_explicit_approval_diagnostic_ownership(predicate: str) -> None:
    document = document_for(nodes={"gate": gate({}, predicate)})

    result = validate_approval_response_predicates(document)
    syntax = validate_expression_syntax(document)

    assert not result.approval_predicates_complete
    assert not result.approval_predicates_valid
    assert result.diagnostics.valid
    assert result.predicates_seen == result.syntax_unresolved == 1
    assert result.predicates_checked == result.schema_steps == 0
    assert len(syntax.diagnostics.errors) == 1
    assert syntax.diagnostics.errors[0].code in {"IR011", "IR027"}


def test_explicit_approval_owns_typed_cost_refusal_after_successful_parsing() -> None:
    predicate = "response.items.map(x, response.items.all(y, true)).size() > 0"
    document = document_for(nodes={"gate": gate({}, predicate)})

    result = validate_approval_response_predicates(document)
    syntax = validate_expression_syntax(document)

    assert syntax.syntax_valid
    assert result.approval_predicates_complete
    assert not result.approval_predicates_valid
    assert result.predicates_seen == result.predicates_checked == 1
    assert (
        result.syntax_unresolved
        == result.context_unresolved
        == result.schema_unresolved
        == 0
    )
    (error,) = result.diagnostics.errors
    assert error.code == "IR027"
    assert error.path == ("graph", "nodes", "gate", "config", "approve_when")


def test_explicit_approval_preserves_codepoint_offset_and_owning_source() -> None:
    predicate = "'🧪' == '🧪' && response.missing"
    document = document_for(
        nodes={"gate": gate(None, predicate, source={"file": "node.py", "line": 9})}
    )

    result = validate_approval_response_predicates(document)

    (error,) = result.diagnostics.errors
    assert error.code == "IR011"
    assert error.path == ("graph", "nodes", "gate", "config", "approve_when")
    assert error.offset == predicate.index("missing")
    assert error.node == "gate"
    assert error.location is not None
    assert (error.location.file, error.location.line) == ("node.py", 9)


@pytest.mark.parametrize(
    "source", [None, "/secret.py", "https://host/secret", "x" * 1025]
)
def test_explicit_approval_never_falls_back_to_ancestor_source(
    source: str | None,
) -> None:
    subject = gate({}, "0")
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

    result = validate_approval_response_predicates(document)

    (error,) = result.diagnostics.errors
    assert error.location is None
    assert error.path == (
        "graph",
        "subgraphs",
        "unused",
        "nodes",
        "gate",
        "config",
        "approve_when",
    )
    assert error.offset == 0


def test_explicit_approval_terminal_categories_and_stored_scope_partition() -> None:
    missing: JsonObject = {"$ref": "urn:kora:artifact:example:Missing"}
    document = document_for(
        nodes={
            "syntax": gate(missing, "inputs.ok &&"),
            "context": gate(missing, "inputs.ok"),
            "schema": gate(missing, "true"),
            "invalid": gate({}, "0"),
            "valid": gate(None, "response.decision == 'approve'"),
            "omitted": gate({}),
            "wrapper": node("subgraph", config={"body": "shared"}),
        },
        bodies={
            "shared": {
                "nodes": {
                    "gate": gate({}, "true"),
                    "nested": node("subgraph", config={"body": "unused"}),
                }
            },
            "unused": {"nodes": {"gate": gate({}, "response")}},
        },
    )
    before = export_ir(document)

    result = validate_approval_response_predicates(document)
    omitted = validate_approval_defaults(document)

    assert result.nodes_checked == 10
    assert result.definitions_checked == 2
    assert result.human_inputs_checked == 8
    assert result.predicates_seen == 7
    assert result.predicates_checked == 4
    assert (
        result.syntax_unresolved
        == result.context_unresolved
        == result.schema_unresolved
        == 1
    )
    assert (
        result.predicates_seen
        == result.predicates_checked
        + result.syntax_unresolved
        + result.context_unresolved
        + result.schema_unresolved
    )
    assert [error.node for error in result.diagnostics.errors] == ["invalid"]
    assert not result.approval_predicates_complete
    assert not result.approval_predicates_valid
    assert not omitted.approval_defaults_valid
    assert omitted.predicates_checked == 1
    assert export_ir(document) == before


@pytest.mark.parametrize("large_metadata", [False, True])
def test_explicit_approval_omissions_never_report_complete(
    large_metadata: bool,
) -> None:
    count = MAX_DIAGNOSTICS + 3
    document = document_for(
        nodes={
            ("n" * 60 if large_metadata else "n") + f"{index:04d}": gate(
                {},
                "0",
                source={"file": "f" * 1000 if large_metadata else "node.py", "line": 1},
            )
            for index in range(count)
        }
    )

    result = validate_approval_response_predicates(document)

    assert result.predicates_checked == count
    assert len(result.diagnostics.errors) + result.diagnostics.errors_omitted == count
    assert result.diagnostics.errors_omitted > 0
    assert not result.approval_predicates_complete
    assert not result.approval_predicates_valid
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
    assert len(payload) <= MAX_DIAGNOSTIC_BYTES
    if large_metadata:
        assert len(result.diagnostics.errors) < MAX_DIAGNOSTICS
