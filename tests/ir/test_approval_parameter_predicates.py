import pytest

from kora.ir import export_ir, validate_approval_response_predicates
from kora.ir.json import JsonObject
from tests.ir.approval_default_cases import gate, saved_form
from tests.ir.graph_link_cases import node
from tests.ir.inline_schema_cases import document_for


@pytest.mark.parametrize(
    ("predicate", "valid"),
    [
        ("response.decision == params.decision", True),
        ("params.decision == 'unknown'", False),
        ("params.count == 1", True),
        ("params.count == 1.0", False),
        ("params.ratio == 1.0", True),
        ("params.missing", False),
        ("params.nested.ok", True),
        ("params.optional", False),
        ("has(params.optional) && params.optional", True),
        ("params.values.all(value, value > 0)", True),
        ("params.values.all(value, value == 'wrong')", False),
        ("[true].all(params, params)", False),
        ("[true].all(response, response)", False),
        ("params.count", False),
        ("unknown.ok && params.nested.ok", False),
    ],
)
@pytest.mark.parametrize("nested", [False, True])
def test_declared_approval_parameters_use_complete_schema(
    predicate: str, valid: bool, nested: bool
) -> None:
    parameters: JsonObject = {
        "properties": {
            "decision": {"enum": ["approve", "reject"]},
            "count": {"type": "integer"},
            "ratio": {"type": "number"},
            "nested": {
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
            },
            "optional": {"type": "boolean"},
            "values": {"type": "array", "items": {"type": "integer"}, "maxItems": 3},
        },
        "required": ["decision", "count", "ratio", "nested", "values"],
    }
    nodes = {"gate": gate(None, predicate)}
    document = document_for(
        params=parameters,
        nodes={"root": node()} if nested else nodes,
        bodies={"unused": {"nodes": nodes}} if nested else None,
    )
    before = export_ir(document)

    result = validate_approval_response_predicates(document)

    assert result.approval_predicates_complete
    assert result.approval_predicates_valid is valid
    assert result.predicates_seen == result.predicates_checked == 1
    assert result.context_unresolved == result.schema_unresolved == 0
    assert result.definitions_checked == int(nested)
    assert len(result.diagnostics.errors) == int(not valid)
    assert all(error.code == "IR011" for error in result.diagnostics.errors)
    assert export_ir(document) == before


@pytest.mark.parametrize(
    "predicate",
    [
        "params.ok && inputs.ok",
        "inputs.ok && params.ok",
        "true ? params.ok : inputs.ok",
        "[true].all(inputs, params.ok)",
    ],
)
def test_declared_approval_parameters_do_not_complete_input_context(
    predicate: str,
) -> None:
    document = document_for(
        params={"properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
        nodes={"gate": gate(None, predicate)},
    )

    result = validate_approval_response_predicates(document)

    assert result.predicates_seen == result.context_unresolved == 1
    assert result.predicates_checked == result.schema_steps == 0
    assert not result.approval_predicates_complete
    assert not result.approval_predicates_valid
    assert result.diagnostics.valid


@pytest.mark.parametrize(
    "parameters",
    [
        {"$ref": "#/$defs/missing"},
        {"$ref": "urn:kora:artifact:example:Missing"},
        {"$ref": "https://unavailable.invalid/parameters"},
        {
            "properties": {
                "ok": {"type": "boolean"},
                "unused": {"type": "string", "maxLength": True},
            },
            "required": ["ok"],
        },
    ],
)
def test_declared_approval_parameter_schema_failure_is_scoped(
    parameters: JsonObject,
) -> None:
    document = document_for(
        params=parameters,
        nodes={
            "parameters": gate(None, "params.ok"),
            "response": gate(None, "response.decision == 'approve'"),
            "syntax": gate(None, "params.ok &&"),
            "inputs": gate(None, "inputs.ok && params.ok"),
        },
    )

    result = validate_approval_response_predicates(document)

    assert result.predicates_seen == 4
    assert result.predicates_checked == 1
    assert result.syntax_unresolved == result.context_unresolved == 1
    assert result.schema_unresolved == 1
    assert result.diagnostics.valid
    assert not result.approval_predicates_complete
    assert not result.approval_predicates_valid


@pytest.mark.parametrize("saved", [False, True])
def test_declared_approval_parameters_keep_separate_reference_roots(
    saved: bool,
) -> None:
    parameters: JsonObject = {
        "$defs": {"value": {"type": "integer"}},
        "properties": {"value": {"$ref": "#/$defs/value"}},
        "required": ["value"],
    }
    form: JsonObject = {
        "$defs": {"value": {"type": "boolean"}},
        "properties": {"value": {"$ref": "#/$defs/value"}},
        "required": ["value"],
    }
    document = document_for(
        params={"$ref": "urn:kora:artifact:example:Parameters"}
        if saved
        else parameters,
        contracts={"example/Parameters": saved_form(parameters)} if saved else None,
        nodes={"gate": gate(form, "response.value && params.value == 1")},
    )

    result = validate_approval_response_predicates(document)

    assert result.approval_predicates_valid
    assert result.predicates_checked == 1
    assert result.schema_unresolved == 0


@pytest.mark.parametrize(
    ("parameters", "predicate"),
    [
        ({}, "params.anything"),
        ({"$ref": "#"}, "params.anything"),
        (
            {
                "properties": {"next": {"$ref": "#"}, "ok": {"type": "boolean"}},
                "required": ["next", "ok"],
            },
            "params.next.next.ok",
        ),
    ],
)
def test_declared_approval_parameters_preserve_dynamic_and_recursive_mapping(
    parameters: JsonObject, predicate: str
) -> None:
    document = document_for(params=parameters, nodes={"gate": gate(None, predicate)})

    result = validate_approval_response_predicates(document)

    assert result.approval_predicates_valid
    assert result.predicates_checked == 1
    assert result.schema_unresolved == 0


@pytest.mark.parametrize("bounded", [False, True])
def test_declared_approval_parameter_bounds_reach_cost_check(bounded: bool) -> None:
    parameters: JsonObject = {
        "properties": {
            "items": {
                "type": "array",
                "items": {"type": "string", "maxLength": 2},
                **({"maxItems": 1} if bounded else {}),
            }
        },
        "required": ["items"],
    }
    predicate = "params.items.map(x, params.items.all(y, true)).size() > 0"
    document = document_for(params=parameters, nodes={"gate": gate(None, predicate)})

    result = validate_approval_response_predicates(document)

    assert result.approval_predicates_complete
    assert result.approval_predicates_valid is bounded
    assert result.predicates_checked == 1
    assert [error.code for error in result.diagnostics.errors] == (
        [] if bounded else ["IR027"]
    )
