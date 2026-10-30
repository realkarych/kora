from itertools import permutations

import pytest

from kora.ir import GraphDocument, GraphLinkResult, validate_graph_links
from tests.ir.graph_link_cases import (
    chain,
    definition_chain,
    dense_faults,
    model,
    node,
    ref,
)


def findings(result: GraphLinkResult) -> set[tuple[str, tuple[str | int, ...] | None]]:
    return {(item.code, item.path) for item in result.diagnostics.errors}


def test_graph_links_accept_agent_command_approval_chain() -> None:
    graph = model(
        {
            "agent": node(
                "agent",
                outputs={
                    "session": {"type": "AgentSession"},
                    "change": {"type": "VcsRef"},
                },
            ),
            "command": node(
                "command", inputs={"change": ref("agent", "change", type="VcsRef")}
            ),
            "approval": node(
                "human_input",
                inputs={"gate": ref("command", "result", type="CommandResult")},
                depends_on=["agent"],
            ),
        }
    )

    result = validate_graph_links(graph)

    assert result.links_valid
    assert result.nodes_checked == 3
    assert result.diagnostics.errors == ()
    assert result.definitions_checked == 0


@pytest.mark.parametrize(
    ("reference", "field"),
    [
        ({"type": "StructuredOutput"}, "from"),
        ({"type": "StructuredOutput", "from": "source"}, "output"),
        (ref("missing"), "from"),
        (ref("source", "missing"), "output"),
        (ref("missing", optional=True), "from"),
    ],
)
def test_graph_links_refuse_unresolved_inputs(
    reference: dict[str, object], field: str
) -> None:
    graph = model(
        {
            "source": node(outputs={"value": {"type": "StructuredOutput"}}),
            "consumer": node(inputs={"data": reference}),
        }
    )

    result = validate_graph_links(graph)

    assert findings(result) == {
        ("IR002", ("graph", "nodes", "consumer", "inputs", "data", field))
    }


def test_graph_links_collect_independent_control_and_input_errors() -> None:
    graph = model(
        {
            "consumer": node(
                inputs={"data": ref("missing")},
                depends_on=["absent"],
                independent_of=["missing"],
            )
        }
    )

    result = validate_graph_links(graph)

    assert findings(result) == {
        ("IR002", ("graph", "nodes", "consumer", "inputs", "data", "from")),
        ("IR002", ("graph", "nodes", "consumer", "depends_on", 0)),
        ("IR023", ("graph", "nodes", "consumer", "independent_of", 0)),
    }


@pytest.mark.parametrize("kind", ["data", "control", "mixed", "bad_output"])
def test_graph_links_detect_only_cycle_members(kind: str) -> None:
    a = node(outputs={"value": {"type": "StructuredOutput"}})
    b = node(outputs={"value": {"type": "StructuredOutput"}})
    if kind in ("data", "mixed", "bad_output"):
        a["inputs"] = {
            "input": ref("b", "missing" if kind == "bad_output" else "value")
        }
    else:
        a["depends_on"] = ["b"]
    if kind == "data":
        b["inputs"] = {"input": ref("a")}
    else:
        b["depends_on"] = ["a"]
    graph = model(
        {
            "a": a,
            "b": b,
            "downstream": node(depends_on=["b"]),
            "unrelated": node(depends_on=["absent"]),
        }
    )

    result = validate_graph_links(graph)

    assert {
        item.node for item in result.diagnostics.errors if item.code == "IR001"
    } == {"a", "b"}
    assert ("IR002", ("graph", "nodes", "unrelated", "depends_on", 0)) in findings(
        result
    )
    if kind == "bad_output":
        assert (
            "IR002",
            ("graph", "nodes", "a", "inputs", "input", "output"),
        ) in findings(result)


def test_graph_links_detect_self_cycles_and_deduplicate_shared_edges() -> None:
    graph = model(
        {
            "source": node(
                outputs={"value": {"type": "StructuredOutput"}},
                inputs={"a": ref("source"), "b": ref("source")},
                depends_on=["source"],
            )
        }
    )

    result = validate_graph_links(graph)

    assert findings(result) == {("IR001", ("graph", "nodes", "source"))}
    assert len(result.diagnostics.errors) == 1


def test_graph_links_keep_body_and_root_scopes_separate() -> None:
    graph = model(
        {
            "outer": node(),
            "call": node("subgraph", config={"body": "body"}),
            "bad": node(depends_on=["inner"]),
        },
        {
            "body": {
                "nodes": {
                    "inner": node(depends_on=["outer"]),
                    "local": node(depends_on=["inner"]),
                }
            }
        },
    )

    result = validate_graph_links(graph)

    assert findings(result) == {
        ("IR002", ("graph", "nodes", "bad", "depends_on", 0)),
        ("IR002", ("graph", "subgraphs", "body", "nodes", "inner", "depends_on", 0)),
    }


def test_graph_links_check_unused_and_shared_bodies_once() -> None:
    calls = {
        f"call{index}": node("subgraph", config={"body": "shared"})
        for index in range(100)
    }
    graph = model(
        calls,
        {
            "shared": {"nodes": {"broken": node(depends_on=["absent"])}},
            "unused": {"nodes": {"broken": node(inputs={"data": ref("absent")})}},
        },
    )

    result = validate_graph_links(graph)

    assert len(result.diagnostics.errors) == 2
    assert result.nodes_checked == 102
    assert result.definitions_checked == 2


@pytest.mark.parametrize("recursive", [False, True])
def test_graph_links_check_deep_definition_chains_iteratively(recursive: bool) -> None:
    graph = definition_chain(1_200, recursive)

    result = validate_graph_links(graph)

    assert result.links_valid is not recursive
    assert result.nodes_checked == 1_201
    assert result.definitions_checked == 1_200
    assert len(result.diagnostics.errors) == (1_000 if recursive else 0)
    assert result.diagnostics.errors_omitted == (200 if recursive else 0)


def test_definition_cycle_does_not_label_callers_or_independent_errors() -> None:
    graph = model(
        {"start": node()},
        {
            "a": {"nodes": {"call": node("subgraph", config={"body": "b"})}},
            "b": {"nodes": {"call": node("subgraph", config={"body": "a"})}},
            "caller": {"nodes": {"call": node("subgraph", config={"body": "a"})}},
            "unrelated": {
                "nodes": {"call": node("subgraph", config={"body": "absent"})}
            },
        },
    )

    result = validate_graph_links(graph)

    assert {
        item.path for item in result.diagnostics.errors if item.code == "IR001"
    } == {("graph", "subgraphs", "a"), ("graph", "subgraphs", "b")}
    assert (
        "IR002",
        ("graph", "subgraphs", "unrelated", "nodes", "call", "config", "body"),
    ) in findings(result)


@pytest.mark.parametrize(
    ("kind", "name", "decl", "config", "expected"),
    [
        ("subgraph", "value", {"type": "StructuredOutput"}, {"body": "body"}, None),
        (
            "subgraph",
            "renamed",
            {"type": "StructuredOutput", "collect": "value"},
            {"body": "body"},
            "IR002",
        ),
        (
            "fan_out",
            "values",
            {"type": "List[StructuredOutput]", "collect": "value"},
            {"body": "body", "over": "items"},
            None,
        ),
        (
            "fan_out",
            "values",
            {"type": "List[StructuredOutput]"},
            {"body": "body", "over": "items"},
            "IR004",
        ),
        (
            "fan_out",
            "values",
            {"type": "List[StructuredOutput]", "collect": "absent"},
            {"body": "body", "over": "items"},
            "IR004",
        ),
    ],
)
def test_graph_links_resolve_wrapper_outputs(
    kind: str,
    name: str,
    decl: dict[str, object],
    config: dict[str, object],
    expected: str | None,
) -> None:
    graph = model(
        {
            "call": node(
                kind,
                config=config,
                inputs={"items": ref("$params")},
                outputs={name: decl},
            )
        },
        {
            "body": {
                "nodes": {
                    "produce": node(outputs={"data": {"type": "StructuredOutput"}})
                },
                "outputs": {
                    "value": {
                        "type": "StructuredOutput",
                        "from": "produce",
                        "output": "data",
                    }
                },
            }
        },
    )

    result = validate_graph_links(graph)

    assert [item.code for item in result.diagnostics.errors] == (
        [] if expected is None else [expected]
    )


def test_graph_links_report_body_export_and_fanout_over_faults() -> None:
    graph = model(
        {"call": node("fan_out", config={"body": "body", "over": "absent"})},
        {
            "body": {
                "nodes": {"produce": node()},
                "outputs": {
                    "bad_source": {
                        "type": "StructuredOutput",
                        "from": "absent",
                        "output": "data",
                    },
                    "bad_output": {
                        "type": "StructuredOutput",
                        "from": "produce",
                        "output": "data",
                    },
                },
            }
        },
    )

    result = validate_graph_links(graph)

    assert findings(result) == {
        ("IR004", ("graph", "nodes", "call", "config", "over")),
        ("IR002", ("graph", "subgraphs", "body", "outputs", "bad_source", "from")),
        ("IR002", ("graph", "subgraphs", "body", "outputs", "bad_output", "output")),
    }


def branch(target: str, **fields: object) -> dict[str, object]:
    return node(
        "branch",
        config={"cases": [{"when": "true", "to": [target, target]}], "else": [target]},
        **fields,
    )


def test_repeated_branch_targets_have_one_owner() -> None:
    graph = model({"choose": branch("target"), "target": node(depends_on=["choose"])})

    result = validate_graph_links(graph)

    assert result.links_valid


def test_branch_ownership_is_independent_of_node_insertion_order() -> None:
    nodes = {
        "a": branch("target"),
        "b": branch("target"),
        "target": node(depends_on=["a", "b"]),
    }
    graphs = [model(dict(order)) for order in permutations(nodes.items())]

    results = [validate_graph_links(graph).diagnostics for graph in graphs]

    assert all(result == results[0] for result in results)
    assert len(results[0].errors) == 6
    assert {item.code for item in results[0].errors} == {"IR010"}


def test_branch_target_and_invariant_faults_remain_independent() -> None:
    graph = model(
        {
            "choose": branch("target", attempt_invariant=True),
            "target": node(attempt_invariant=True),
        }
    )

    result = validate_graph_links(graph)

    assert len(result.diagnostics.errors) == 5
    assert ("IR010", ("graph", "nodes", "choose", "attempt_invariant")) in findings(
        result
    )
    assert ("IR010", ("graph", "nodes", "target", "attempt_invariant")) in findings(
        result
    )


@pytest.mark.parametrize("target", ["absent", "$fail_attempt"])
def test_branch_targets_must_resolve_in_current_scope(target: str) -> None:
    graph = model({"choose": branch(target)})

    result = validate_graph_links(graph)

    assert len(result.diagnostics.errors) == 3
    assert all(item.code == "IR010" for item in result.diagnostics.errors)


def test_reserved_sources_do_not_create_dependency_edges() -> None:
    inputs = {
        "param": ref("$params"),
        "previous": ref("$previous_attempt"),
        "body": ref("$inputs"),
    }
    graph = model(
        {"root": node(inputs=inputs)},
        {
            "body": {
                "nodes": {
                    "choose": branch("$fail_attempt"),
                    "child": node(inputs=inputs),
                }
            }
        },
    )

    result = validate_graph_links(graph)

    assert findings(result) == {
        ("IR002", ("graph", "nodes", "root", "inputs", "body", "from"))
    }


def test_target_reference_uses_its_own_code_without_payload_path_checks() -> None:
    graph = model(
        {"source": node()},
        target={
            "key": {
                "from": "source",
                "output": "absent",
                "path": "private_payload_name",
            }
        },
    )

    result = validate_graph_links(graph)

    assert findings(result) == {("IR020", ("graph", "target", "key", "output"))}


@pytest.mark.parametrize("count", [1, 100, 1_000])
def test_graph_links_validate_large_sparse_graphs(count: int) -> None:
    graph = chain(count)

    result = validate_graph_links(graph)

    assert result.links_valid
    assert result.nodes_checked == count


def test_graph_links_bound_dense_fault_output_without_losing_error_status() -> None:
    graph = dense_faults(1_000)

    result = validate_graph_links(graph)

    assert not result.links_valid
    assert len(result.diagnostics.errors) == 1_000
    assert result.diagnostics.errors_omitted == 2_000


@pytest.mark.parametrize(
    "source",
    [
        {"file": "/private-payload", "line": 1},
        {"file": "https://private-payload", "line": 1},
        {"file": "x" * 1_025, "line": 1},
        {"file": "\ud800", "line": 1},
        {"file": "graph.py", "line": 2**53},
    ],
)
def test_graph_link_diagnostics_exclude_content(source: dict[str, object]) -> None:
    graph = model(
        {
            "safe": node(
                "agent",
                source=source,
                config={
                    "harness": "claude",
                    "prompt": "private-payload",
                    "workspace": {"repo": "private-payload"},
                },
                inputs={"data": ref("absent", path="private-payload")},
                **{"x-private-payload": "private-payload"},
            )
        }
    )

    result = validate_graph_links(graph)

    assert len(result.diagnostics.errors) == 1
    error = result.diagnostics.errors[0]
    assert error.location is None
    assert error.node == "safe"
    assert "private-payload" not in error.model_dump_json()


def test_graph_links_preserve_safe_source_metadata_without_mutating_models() -> None:
    graph = model(
        {
            "consumer": node(
                source={"file": "graphs/example.py", "line": 17}, depends_on=["absent"]
            )
        }
    )
    before = graph.model_dump_json()

    result = validate_graph_links(graph)

    error = result.diagnostics.errors[0]
    assert error.location is not None
    assert error.location.file == "graphs/example.py"
    assert error.location.line == 17
    assert graph.model_dump_json() == before
    assert isinstance(graph, GraphDocument)


def test_graph_links_preserve_source_for_body_exports_and_root_targets() -> None:
    graph = model(
        {"source": node()},
        {
            "body": {
                "nodes": {"source": node()},
                "outputs": {"data": ref("source")},
                "source": {"file": "body.py", "line": 12},
            }
        },
        target={"key": {"from": "source", "output": "absent", "path": "value"}},
        source={"file": "graph.py", "line": 3},
    )

    result = validate_graph_links(graph)

    assert len(result.diagnostics.errors) == 2
    assert {
        (item.location.file, item.location.line)
        for item in result.diagnostics.errors
        if item.location is not None
    } == {("body.py", 12), ("graph.py", 3)}
