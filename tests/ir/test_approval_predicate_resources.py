import gc
import weakref
from collections.abc import Callable

import pytest
from pydantic import MISSING

from kora.cel import CheckedExpression, Environment, SchemaTypes
from kora.cel.cost import ValueBound, Variable
from kora.cel.tree import Node
from kora.cel.types import Record
from kora.ir import (
    CustomTypeContract,
    HumanInputNode,
    validate_approval_response_predicates,
)
from kora.ir.approval_predicates import ApprovalPredicateChecks
from kora.ir.json import JsonObject
from tests.ir.approval_default_cases import gate
from tests.ir.approval_predicate_cases import shared_predicates
from tests.ir.graph_link_cases import node
from tests.ir.inline_schema_cases import document_for


@pytest.mark.parametrize("invalid", [False, True])
def test_explicit_approval_reuses_deep_saved_schema_mapping(invalid: bool) -> None:
    one = shared_predicates(1, 1000, invalid=invalid)
    many = shared_predicates(1000, 1000, invalid=invalid)

    single = validate_approval_response_predicates(one)
    repeated = validate_approval_response_predicates(many)

    assert single.approval_predicates_valid is not invalid
    assert repeated.approval_predicates_valid is not invalid
    assert repeated.predicates_seen == repeated.predicates_checked == 1000
    assert repeated.schema_steps == single.schema_steps + 999
    assert single.schema_steps < 3 * 1000 + 20
    assert repeated.checker_steps == single.checker_steps * 1000


def test_explicit_approval_deep_mapping_does_not_recurse_on_python_stack() -> None:
    document = shared_predicates(1, 5000)

    result = validate_approval_response_predicates(document)

    assert result.approval_predicates_valid
    assert result.approval_predicates_complete
    assert result.schema_steps < 3 * 5000 + 20
    assert result.predicates_checked == 1


@pytest.mark.parametrize("ending", ["valid", "invalid", "schema", "syntax", "context"])
@pytest.mark.parametrize("nested", [False, True])
def test_explicit_approval_releases_owned_state(ending: str, nested: bool) -> None:
    predicate = {"syntax": "true &&", "context": "inputs.ok"}.get(
        ending, "response.decision == 'approve'"
    )
    document = shared_predicates(3, 100, predicate, ending == "invalid", nested)
    if ending == "schema":
        gate_node = gate(
            {"$ref": "urn:kora:artifact:example:Missing"},
            "true",
            source={"file": "node.py", "line": 1},
        )
        document = document_for(
            nodes={"root": node()} if nested else {"gate": gate_node},
            bodies={"unused": {"nodes": {"gate": gate_node}}} if nested else None,
        )
    references: list[Callable[[], object | None]] = [weakref.ref(document)]
    bodies = {} if document.graph.subgraphs is MISSING else document.graph.subgraphs
    for body in (document.graph, *bodies.values()):
        references.append(weakref.ref(body))
        for subject in body.nodes.values():
            references.extend((weakref.ref(subject), weakref.ref(subject.config)))
            if isinstance(subject, HumanInputNode):
                assert subject.config.form is not MISSING
                references.append(weakref.ref(subject.config.form))
            if subject.source is not MISSING:
                references.append(weakref.ref(subject.source))
            del subject
        del body
    del bodies
    for contract in document.type_contracts.values():
        assert isinstance(contract, CustomTypeContract)
        references.extend((weakref.ref(contract), weakref.ref(contract.schema_)))
        del contract
    gc.collect()
    owned_types = (
        SchemaTypes
        | ApprovalPredicateChecks
        | CheckedExpression
        | Record
        | Environment
        | Variable
        | Node
    )
    baseline = sum(isinstance(value, owned_types) for value in gc.get_objects())

    result = validate_approval_response_predicates(document)
    diagnostic_references = [weakref.ref(error) for error in result.diagnostics.errors]
    del document
    gc.collect()
    released = all(reference() is None for reference in references)
    remaining = sum(isinstance(value, owned_types) for value in gc.get_objects())
    result_reference = weakref.ref(result)
    del result
    gc.collect()

    assert released
    assert remaining == baseline
    assert bool(diagnostic_references) is (ending == "invalid")
    assert all(reference() is None for reference in diagnostic_references)
    assert result_reference() is None


@pytest.mark.parametrize("invalid", [False, True])
def test_approval_parameter_mapping_is_once_per_invocation(invalid: bool) -> None:
    parameters: JsonObject = {
        "properties": {
            f"field{index}": {
                "type": "array",
                "maxItems": 2,
                "items": {"type": "string", "maxLength": 4},
            }
            for index in range(64)
        },
        "required": ["field0"],
    }
    if invalid:
        properties = parameters["properties"]
        assert isinstance(properties, dict)
        properties["invalid"] = {"type": "string", "maxLength": True}
    predicate = "params.field0.all(value, value == 'ok')"
    one = document_for(params=parameters, nodes={"gate": gate(None, predicate)})
    many = document_for(
        params=parameters,
        nodes={f"gate{index}": gate(None, predicate) for index in range(40)},
    )

    single = validate_approval_response_predicates(one)
    repeated = validate_approval_response_predicates(many)

    assert single.approval_predicates_valid is not invalid
    assert repeated.approval_predicates_valid is not invalid
    assert repeated.predicates_seen == 40
    assert repeated.predicates_checked == (0 if invalid else 40)
    assert repeated.schema_unresolved == (40 if invalid else 0)
    assert repeated.schema_steps == single.schema_steps
    assert repeated.checker_steps == single.checker_steps * 40


def test_approval_parameter_deep_mapping_is_iterative() -> None:
    parameters: JsonObject = {
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
    }
    for _ in range(5000):
        parameters = {"allOf": [parameters]}
    document = document_for(params=parameters, nodes={"gate": gate(None, "params.ok")})

    result = validate_approval_response_predicates(document)

    assert result.approval_predicates_valid
    assert result.schema_steps < 3 * 5000 + 50
    assert result.predicates_checked == 1


@pytest.mark.parametrize("ending", ["valid", "invalid", "schema", "context"])
@pytest.mark.parametrize("omitted", [False, True])
def test_approval_parameter_context_releases_bound_state(
    ending: str, omitted: bool
) -> None:
    parameters: JsonObject = {
        "properties": {
            "values": {
                "type": "array",
                "maxItems": 3,
                "items": {"type": "string", "maxLength": 5},
            }
        },
        "required": ["values"],
    }
    if ending == "schema":
        properties = parameters["properties"]
        assert isinstance(properties, dict)
        properties["missing"] = {"$ref": "urn:kora:artifact:missing"}
    predicate = {
        "valid": "params.values.all(value, value == 'ok')",
        "invalid": "params.values == 0",
        "schema": "params.values.all(value, value == 'ok')",
        "context": "inputs.ok && params.values.all(value, value == 'ok')",
    }[ending]
    form: JsonObject | None = None
    if omitted:
        predicate = {
            "valid": "params == {}",
            "invalid": "params.missing",
            "schema": "params == {}",
            "context": "inputs.ok && params == {}",
        }[ending]
        if ending == "schema":
            form = {"$ref": "urn:kora:artifact:missing"}
    document = document_for(
        params=None if omitted else parameters,
        nodes={"gate": gate(form, predicate)},
    )
    references: list[Callable[[], object | None]] = [weakref.ref(document)]
    if document.graph.params is not MISSING:
        references.append(weakref.ref(document.graph.params))
    owned_types = (
        SchemaTypes
        | ApprovalPredicateChecks
        | CheckedExpression
        | Record
        | Environment
        | Variable
        | ValueBound
        | Node
    )
    gc.collect()
    baseline = sum(isinstance(value, owned_types) for value in gc.get_objects())

    result = validate_approval_response_predicates(document)
    del document
    gc.collect()
    remaining = sum(isinstance(value, owned_types) for value in gc.get_objects())

    assert all(reference() is None for reference in references)
    assert remaining == baseline
    assert result.approval_predicates_complete is (ending in {"valid", "invalid"})
    assert result.approval_predicates_valid is (ending == "valid")


def test_omitted_approval_parameter_mapping_is_lazy_and_once() -> None:
    predicate = "params == {}"
    one = document_for(nodes={"gate": gate(None, predicate)})
    many = document_for(
        nodes={f"gate{index}": gate(None, predicate) for index in range(40)}
    )
    response_only = document_for(nodes={"gate": gate(None, "true")})

    single = validate_approval_response_predicates(one)
    repeated = validate_approval_response_predicates(many)
    unused = validate_approval_response_predicates(response_only)

    assert single.approval_predicates_valid
    assert repeated.approval_predicates_valid
    assert unused.approval_predicates_valid
    assert repeated.predicates_seen == repeated.predicates_checked == 40
    assert repeated.schema_steps == single.schema_steps
    assert single.schema_steps > unused.schema_steps
    assert repeated.checker_steps == single.checker_steps * 40
