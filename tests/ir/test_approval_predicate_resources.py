import gc
import weakref
from collections.abc import Callable

import pytest
from pydantic import MISSING

from kora.cel import CheckedExpression, Environment, SchemaTypes
from kora.cel.cost import Variable
from kora.cel.tree import Node
from kora.cel.types import Record
from kora.ir import (
    CustomTypeContract,
    HumanInputNode,
    validate_approval_response_predicates,
)
from kora.ir.approval_predicates import ApprovalPredicateChecks
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
    predicate = {"syntax": "true &&", "context": "params.ok"}.get(
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
