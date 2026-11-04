import gc
import weakref
from collections.abc import Callable

import pytest
from pydantic import MISSING

from kora.cel import CheckedExpression, Environment, SchemaTypes
from kora.cel.cost import ValueBound, Variable
from kora.cel.tree import Node
from kora.cel.types import Record
from kora.ir import HumanInputNode, validate_approval_response_predicates
from kora.ir.approval_inputs import DirectInputContexts, InputFailure, InputView
from kora.ir.approval_predicates import ApprovalPredicateChecks
from kora.ir.json import JsonObject
from tests.ir.approval_default_cases import gate, saved_form
from tests.ir.approval_input_cases import (
    BUILTINS,
    direct_document,
    producer,
    unavailable_inputs,
)
from tests.ir.graph_link_cases import node
from tests.ir.inline_schema_cases import document_for


@pytest.mark.parametrize("invalid", [False, True])
def test_approval_direct_inputs_reuse_shared_mapping_and_failures(
    invalid: bool,
) -> None:
    schema: JsonObject = {"properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
    if invalid:
        schema["properties"] = {
            "allocated": {"properties": {"ok": {"type": "boolean"}}},
            "missing": {"$ref": "urn:kora:artifact:missing"},
        }
    for _ in range(1000):
        schema = {"allOf": [schema]}
    contracts = {**BUILTINS, "example/Shared": saved_form(schema)}
    documents = [
        document_for(
            contracts=contracts,
            nodes={
                **{
                    f"source{index}": producer("example/Shared")
                    for index in range(count)
                },
                **{
                    f"gate{index}": gate(
                        None,
                        "inputs.data.ok",
                        inputs={
                            "data": {
                                "type": "example/Shared",
                                "from": f"source{index}",
                                "output": "value",
                            }
                        },
                    )
                    for index in range(count)
                },
            },
        )
        for count in (1, 1000)
    ]

    single, many = [
        validate_approval_response_predicates(document) for document in documents
    ]

    assert single.approval_predicates_valid is not invalid
    assert many.approval_predicates_valid is not invalid
    assert many.predicates_seen == 1000
    assert many.predicates_checked == (0 if invalid else 1000)
    assert many.schema_unresolved == (1000 if invalid else 0)
    assert many.schema_steps == single.schema_steps
    assert many.checker_steps == single.checker_steps * 1000


def test_approval_direct_inputs_cache_producer_roles_and_views() -> None:
    source = node(
        "agent",
        outputs={"session": {"type": "AgentSession"}, "change": {"type": "VcsRef"}},
    )
    document = document_for(
        contracts=BUILTINS,
        nodes={
            "source": source,
            "gate": gate(
                None,
                "true",
                inputs={
                    "session": {
                        "type": "AgentSession",
                        "from": "source",
                        "output": "session",
                    },
                    "change": {"type": "VcsRef", "from": "source", "output": "change"},
                },
            ),
        },
    )
    checks = ApprovalPredicateChecks(document.type_contracts, document.graph.params)
    subject = document.graph.nodes["gate"]
    assert isinstance(subject, HumanInputNode)
    first = checks.inputs.context(document.graph, subject)
    source_steps = checks.inputs.source_steps
    schema_steps = checks.schemas.meter.steps

    repeated = [checks.inputs.context(document.graph, subject) for _ in range(1000)]

    assert isinstance(first, Variable)
    assert all(isinstance(value, Variable) for value in repeated)
    assert source_steps == 4
    assert checks.inputs.source_steps == source_steps
    assert checks.schemas.meter.steps == schema_steps
    assert len(checks.inputs.producers) == 1
    assert len(checks.inputs.sources) == 2
    assert isinstance(checks.inputs.session_variable, Variable)
    assert len(checks.inputs.variables) == 1


def test_approval_direct_inputs_map_only_after_complete_prerequisites() -> None:
    schema: JsonObject = {
        "properties": {"broken": {"$ref": "urn:kora:artifact:missing"}}
    }
    document = document_for(
        contracts=BUILTINS,
        nodes={
            "producer": producer(schema=schema),
            "gate": gate(
                None,
                "inputs.data.ok",
                inputs={
                    "data": {
                        "type": "StructuredOutput",
                        "from": "producer",
                        "output": "value",
                    },
                    **unavailable_inputs(),
                },
            ),
        },
    )
    checks = ApprovalPredicateChecks(document.type_contracts, document.graph.params)
    subject = document.graph.nodes["gate"]
    assert isinstance(subject, HumanInputNode)

    result = checks.inputs.context(document.graph, subject)

    assert result is InputFailure.CONTEXT
    assert checks.schemas.meter.steps == 0
    assert checks.inputs.variables == {}


def test_approval_direct_inputs_skip_unused_schema_work() -> None:
    schema: JsonObject = {"$ref": "urn:kora:artifact:missing"}
    document = direct_document(
        "response.decision == 'approve'", producer(schema=schema)
    )
    without = document_for(nodes={"gate": gate(None, "response.decision == 'approve'")})

    actual = validate_approval_response_predicates(document)
    baseline = validate_approval_response_predicates(without)

    assert actual.approval_predicates_valid
    assert actual.schema_steps == baseline.schema_steps
    assert actual.checker_steps == baseline.checker_steps


@pytest.mark.parametrize("ending", ["valid", "invalid", "syntax", "context", "schema"])
def test_approval_direct_inputs_release_invocation_state(ending: str) -> None:
    schema: JsonObject = {
        "properties": {
            "items": {
                "type": "array",
                "maxItems": 2,
                "items": {"type": "string", "maxLength": 3},
            }
        },
        "required": ["items"],
    }
    failed: JsonObject = {
        "properties": {
            "allocated": schema,
            "missing": {"$ref": "urn:kora:artifact:missing"},
        }
    }
    bindings: dict[str, object] = {
        "data": {"type": "StructuredOutput", "from": "producer", "output": "value"}
    }
    if ending == "context":
        bindings.update(unavailable_inputs())
    if ending == "schema":
        bindings["second"] = {
            "type": "StructuredOutput",
            "from": "failed",
            "output": "value",
        }
    predicate = {"invalid": "inputs.data.items == 0", "syntax": "inputs.data &&"}.get(
        ending, "inputs.data.items.all(item, item == 'ok')"
    )
    document = document_for(
        contracts=BUILTINS,
        nodes={
            "producer": producer(schema=schema),
            "failed": producer(schema=failed),
            "gate": gate(None, predicate, inputs=bindings),
        },
    )
    references: list[Callable[[], object | None]] = [
        weakref.ref(document),
        weakref.ref(document.graph),
    ]
    for subject in document.graph.nodes.values():
        references.extend((weakref.ref(subject), weakref.ref(subject.config)))
        if subject.inputs is not MISSING:
            references.extend(
                weakref.ref(binding) for binding in subject.inputs.values()
            )
        del subject
    owned_types = (
        DirectInputContexts
        | InputView
        | ApprovalPredicateChecks
        | SchemaTypes
        | CheckedExpression
        | Environment
        | Variable
        | ValueBound
        | Record
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
    assert result.predicates_checked == int(ending in {"valid", "invalid"})
    assert result.syntax_unresolved == int(ending == "syntax")
    assert result.context_unresolved == int(ending == "context")
    assert result.schema_unresolved == int(ending == "schema")
    assert len(result.diagnostics.errors) == int(ending == "invalid")


def test_approval_direct_inputs_cache_role_schema_equality() -> None:
    schema: JsonObject = {"properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
    source = node(
        "agent",
        config={
            "harness": "claude",
            "prompt": "",
            "workspace": {"repo": ""},
            "output_schema": schema,
        },
        outputs={
            "session": {"type": "AgentSession"},
            "value": {"type": "StructuredOutput", "schema": schema},
        },
    )
    document = direct_document("inputs.data.ok", source)
    checks = ApprovalPredicateChecks(document.type_contracts, document.graph.params)
    subject = document.graph.nodes["gate"]
    assert isinstance(subject, HumanInputNode)
    first = checks.inputs.context(document.graph, subject)
    source_steps = checks.inputs.source_steps
    schema_steps = checks.schemas.meter.steps

    repeated = [checks.inputs.context(document.graph, subject) for _ in range(1000)]

    assert isinstance(first, Variable)
    assert all(isinstance(value, Variable) for value in repeated)
    assert source_steps > 3
    assert checks.inputs.source_steps == source_steps
    assert checks.schemas.meter.steps == schema_steps
