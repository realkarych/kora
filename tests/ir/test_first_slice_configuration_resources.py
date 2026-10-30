import gc
import json
import weakref
from collections.abc import Callable

import pytest
from pydantic import MISSING

from kora.ir import validate_first_slice_configuration
from tests.ir.first_slice_configuration_cases import chain_document, chain_nodes
from tests.ir.graph_link_cases import node


def test_shared_and_recursive_definitions_are_visited_once_without_expansion() -> None:
    nodes = chain_nodes()
    nodes.update(
        {f"n{index}": node("subgraph", config={"body": "b0"}) for index in range(1000)}
    )
    definitions = {
        f"b{index}": {
            "nodes": {
                "call": node("subgraph", config={"body": f"b{(index + 1) % 1000}"})
            },
            "retry": {"max_attempts": 2},
        }
        for index in range(1000)
    }
    document = chain_document(nodes, subgraphs=definitions)

    result = validate_first_slice_configuration(document)

    assert result.nodes_checked == 2003
    assert result.definitions_checked == 1000
    assert len(result.diagnostics.errors) == 1000
    assert result.diagnostics.errors_omitted == 2002
    assert not result.authored_configuration_supported
    assert result.ordering_checked == result.ordering_unresolved == 0
    assert result.diagnostics.errors[0].path == ("graph", "nodes")


def test_dense_independent_refusals_preserve_byte_bound_and_source_ownership() -> None:
    nodes = {
        f"n{index}": node(
            "agent", retry={"max_attempts": 2}, source={"file": "s" * 1000, "line": 1}
        )
        for index in range(1000)
    }
    first = chain_document(nodes)
    second = chain_document(dict(reversed(tuple(nodes.items()))))

    result = validate_first_slice_configuration(first)
    reversed_result = validate_first_slice_configuration(second)
    encoded = json.dumps(
        {
            "errors": [
                error.model_dump(mode="json") for error in result.diagnostics.errors
            ],
            "warnings": [],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()

    assert result == reversed_result
    assert 0 < len(result.diagnostics.errors) < 1000
    assert len(result.diagnostics.errors) + result.diagnostics.errors_omitted == 1001
    assert len(encoded) <= 1_048_576


@pytest.mark.parametrize("invalid", [False, True])
def test_scoped_result_does_not_retain_input_objects(invalid: bool) -> None:
    nodes = chain_nodes()
    if invalid:
        nodes["agent"]["retry"] = {"max_attempts": 2}
    nodes["agent"]["source"] = {"file": "node.py", "line": 7}
    document = chain_document(nodes)
    agent = document.graph.nodes["agent"]
    assert agent.source is not MISSING
    input_references: list[Callable[[], object | None]] = [
        weakref.ref(document),
        weakref.ref(document.graph),
        weakref.ref(document.runtime),
        weakref.ref(agent),
        weakref.ref(agent.config),
        weakref.ref(agent.source),
    ]
    del nodes, agent

    result = validate_first_slice_configuration(document)
    del document
    gc.collect()
    released_with_live_result = all(
        reference() is None for reference in input_references
    )
    diagnostic_references = [weakref.ref(error) for error in result.diagnostics.errors]
    source_references = [
        weakref.ref(error.location)
        for error in result.diagnostics.errors
        if error.location is not None
    ]
    result_reference = weakref.ref(result)
    supported = result.authored_configuration_supported
    del result
    gc.collect()

    assert supported is not invalid
    assert released_with_live_result
    assert all(
        reference() is None for reference in diagnostic_references + source_references
    )
    assert result_reference() is None
