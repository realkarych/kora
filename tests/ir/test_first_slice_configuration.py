from copy import deepcopy
from typing import cast

import pytest
from pydantic import ValidationError

from kora.ir import (
    GraphDocument,
    export_ir,
    validate_expression_syntax,
    validate_first_slice_configuration,
    validate_graph_links,
)
from kora.ir.json import JsonObject
from tests.ir.first_slice_configuration_cases import chain_document, chain_nodes
from tests.ir.graph_link_cases import node

FORBIDDEN_AGENT_FIELDS: tuple[tuple[str, object], ...] = (
    ("skills", ["private-skill"]),
    ("subagents", [{"name": "", "description": "private", "prompt": "private"}]),
    ("hooks", {"private-hook": False}),
    ("mcp_servers", {"private-server": {"type": "stdio", "command": "private"}}),
    ("trust_repo_config", True),
)


@pytest.mark.parametrize("harness", ["claude", "codex", "unresolved_alias"])
@pytest.mark.parametrize("control_only", [False, True])
def test_minimal_chain_preserves_authored_document(
    harness: str, control_only: bool
) -> None:
    nodes = chain_nodes()
    config = cast("dict[str, object]", nodes["agent"]["config"])
    config["harness"] = harness
    if control_only:
        nodes["gate"].pop("inputs")
        nodes["gate"]["depends_on"] = ["agent"]
        nodes["approve"].pop("inputs")
        nodes["approve"]["depends_on"] = ["gate"]
    document = chain_document(dict(reversed(tuple(nodes.items()))))
    before = export_ir(document)

    result = validate_first_slice_configuration(document)

    assert result.authored_configuration_supported
    assert result.nodes_checked == result.ordering_checked == 3
    assert result.definitions_checked == result.triggers_checked == 0
    assert result.ordering_unresolved == 0
    assert result.diagnostics.errors == result.diagnostics.warnings == ()
    assert export_ir(document) == before


@pytest.mark.parametrize(("field", "value"), FORBIDDEN_AGENT_FIELDS)
@pytest.mark.parametrize("stored", [False, True])
def test_forbidden_agent_fields_have_exact_owned_paths(
    field: str, value: object, stored: bool
) -> None:
    nodes = chain_nodes()
    config = cast("dict[str, object]", nodes["agent"]["config"])
    config[field] = value
    nodes["agent"]["source"] = {"file": "node.py", "line": 7}
    document = (
        chain_document(subgraphs={"unused": {"nodes": nodes}})
        if stored
        else chain_document(nodes)
    )

    result = validate_first_slice_configuration(document)

    path = ("graph", "subgraphs", "unused") if stored else ("graph",)
    errors = [error for error in result.diagnostics.errors if error.node == "agent"]
    assert len(errors) == 1
    error = errors[0]
    assert error.code == "IR028"
    assert error.path == path + ("nodes", "agent", "config", field)
    assert error.location is not None
    assert (error.location.file, error.location.line) == ("node.py", 7)
    assert error.offset is None
    assert error.hint == (
        "Remove the unsupported configuration or use an installation "
        "that implements it."
    )
    assert not result.authored_configuration_supported


@pytest.mark.parametrize("owner", ["runtime", "agent", "command", "approval", "body"])
@pytest.mark.parametrize("retry", [False, True])
def test_budget_and_retry_refusals_cover_all_typed_owners(
    owner: str, retry: bool
) -> None:
    nodes = chain_nodes()
    fields: dict[str, object] = (
        {"retry": {"max_attempts": 2, "retry_on": [], "predicate": "false"}}
        if retry
        else {"limits": {"budget_usd": 1}}
    )
    graph_fields: dict[str, object] = {}
    runtime_fields: dict[str, object] = {}
    if owner == "runtime":
        runtime_fields = {"infrastructure_retry": fields["retry"]} if retry else fields
        path = ("runtime", "infrastructure_retry" if retry else "limits")
    elif owner == "body":
        graph_fields["subgraphs"] = {
            "unused": {
                "nodes": {"timer": node()},
                "source": {"file": "body.py", "line": 5},
                **fields,
            }
        }
        path = ("graph", "subgraphs", "unused", "retry" if retry else "limits")
    else:
        name = {"agent": "agent", "command": "gate", "approval": "approve"}[owner]
        nodes[name].update(fields)
        path = ("graph", "nodes", name, "retry" if retry else "limits")
    document = chain_document(nodes, runtime_fields, **graph_fields)

    result = validate_first_slice_configuration(document)

    errors = [
        error
        for error in result.diagnostics.errors
        if error.path == path + ("max_attempts" if retry else "budget_usd",)
    ]
    assert len(errors) == 1
    assert errors[0].code == "IR028"
    assert not result.authored_configuration_supported
    if owner == "body":
        assert errors[0].node is None
        assert errors[0].location is not None
        assert errors[0].location.file == "body.py"
    if owner == "runtime":
        assert errors[0].node is errors[0].location is None


@pytest.mark.parametrize("headroom", [0, 1, 100])
def test_authored_headroom_is_never_an_omitted_budget(headroom: int) -> None:
    document = chain_document(runtime_fields={"budget_headroom_pct": headroom})

    result = validate_first_slice_configuration(document)

    assert [error.path for error in result.diagnostics.errors] == [
        ("runtime", "budget_headroom_pct")
    ]


@pytest.mark.parametrize("unit", ["ms", "s", "m", "h", "d"])
@pytest.mark.parametrize("positive", [False, True])
def test_provider_wait_zero_is_exact_without_integer_conversion(
    unit: str, positive: bool
) -> None:
    duration = "0" * 10_000 + ("1" if positive else "0") + unit
    document = chain_document(runtime_fields={"provider_retry": {"max_wait": duration}})
    before = export_ir(document)

    result = validate_first_slice_configuration(document)

    assert result.authored_configuration_supported is not positive
    assert len(result.diagnostics.errors) == int(positive)
    assert export_ir(document) == before
    for error in result.diagnostics.errors:
        assert error.path == ("runtime", "provider_retry", "max_wait")
        assert error.node is error.location is None


@pytest.mark.parametrize("count", [None, 1])
@pytest.mark.parametrize("populated", [False, True])
def test_exact_noops_keep_dormant_fields_and_authored_omissions(
    count: int | None, populated: bool
) -> None:
    retry: dict[str, object] = {} if count is None else {"max_attempts": count}
    if populated:
        retry.update(
            retry_on=[], predicate="false", initial_interval="0s", strategy="resume"
        )
    nodes = chain_nodes()
    for value in nodes.values():
        value["retry"] = deepcopy(retry)
        value["limits"] = {}
    config = cast("dict[str, object]", nodes["agent"]["config"])
    config.update(
        skills=[], subagents=[], hooks={}, mcp_servers={}, trust_repo_config=False
    )
    document = chain_document(
        nodes,
        {"provider_retry": {}, "infrastructure_retry": retry, "limits": {}},
        subgraphs={},
        triggers=[{"kind": "manual", "params": {"skills": ["payload"]}}],
    )
    before = export_ir(document)

    result = validate_first_slice_configuration(document)

    assert result.authored_configuration_supported
    assert result.triggers_checked == 1
    assert export_ir(document) == before


@pytest.mark.parametrize(
    "kind", ["branch", "fan_out", "subgraph", "wait_event", "timer"]
)
def test_every_unsupported_node_kind_is_refused(kind: str) -> None:
    nodes = chain_nodes()
    nodes["extra"] = node(kind)
    document = chain_document(nodes)

    result = validate_first_slice_configuration(document)

    assert {error.path for error in result.diagnostics.errors} == {
        ("graph", "nodes"),
        ("graph", "nodes", "extra", "kind"),
    }
    assert all(error.code == "IR028" for error in result.diagnostics.errors)
    assert result.ordering_checked == result.ordering_unresolved == 0


@pytest.mark.parametrize("extra_kind", ["agent", "command", "human_input"])
def test_wider_supported_kinds_are_still_refused(extra_kind: str) -> None:
    nodes = chain_nodes()
    nodes["extra"] = node(extra_kind)
    document = chain_document(nodes)

    result = validate_first_slice_configuration(document)

    assert [error.path for error in result.diagnostics.errors] == [("graph", "nodes")]


@pytest.mark.parametrize("shape", ["parallel", "reverse", "skip_gate"])
def test_declared_order_is_required_even_for_known_acyclic_graphs(shape: str) -> None:
    nodes = chain_nodes()
    if shape in ("parallel", "reverse"):
        nodes["gate"].pop("inputs")
    if shape == "parallel":
        nodes["approve"].pop("inputs")
    if shape == "reverse":
        nodes["agent"]["depends_on"] = ["gate"]
    if shape == "skip_gate":
        nodes["approve"]["inputs"] = {
            "change": {"type": "VcsRef", "from": "agent", "output": "change"}
        }
    document = chain_document(nodes)
    links = validate_graph_links(document)

    result = validate_first_slice_configuration(document)

    assert links.links_valid
    assert not result.authored_configuration_supported
    assert result.ordering_checked == 3
    assert result.ordering_unresolved == 0
    assert result.diagnostics.errors
    assert all(error.code == "IR028" for error in result.diagnostics.errors)


@pytest.mark.parametrize(
    "invalid", ["self", "cycle", "producer", "output", "reserved", "missing_from"]
)
def test_unresolved_and_cyclic_links_keep_their_specific_owners(invalid: str) -> None:
    nodes = chain_nodes()
    if invalid == "self":
        nodes["gate"]["depends_on"] = ["gate"]
    elif invalid == "cycle":
        nodes["agent"]["depends_on"] = ["gate"]
    else:
        reference: dict[str, object] = {
            "type": "VcsRef",
            "from": "agent",
            "output": "change",
        }
        if invalid == "producer":
            reference["from"] = "absent"
        elif invalid == "output":
            reference["output"] = "absent"
        elif invalid == "reserved":
            reference["from"] = "$previous_attempt"
        else:
            reference.pop("from")
        nodes["gate"]["inputs"] = {"change": reference}
    document = chain_document(nodes)

    result = validate_first_slice_configuration(document)
    links = validate_graph_links(document)

    assert result.diagnostics.valid
    assert not result.authored_configuration_supported
    assert result.ordering_unresolved > 0
    assert result.ordering_checked + result.ordering_unresolved == 3
    if invalid != "reserved":
        assert not links.links_valid
        assert {error.code for error in links.diagnostics.errors} == {
            "IR001" if invalid in ("self", "cycle") else "IR002"
        }


@pytest.mark.parametrize(
    "trigger",
    [
        {"kind": "cron", "expr": ""},
        {"kind": "webhook", "name": "", "auth": {"kind": "bearer", "secret": "TOKEN"}},
        {
            "kind": "sensor",
            "poll": {"sensor": "http", "until": "false"},
            "interval": "0s",
        },
        {"kind": "graph", "graph": "other", "on": "run_finished"},
    ],
)
def test_every_nonmanual_trigger_is_refused_at_its_kind(
    trigger: dict[str, object],
) -> None:
    document = chain_document(triggers=[{"kind": "manual"}, trigger])

    result = validate_first_slice_configuration(document)

    assert [error.path for error in result.diagnostics.errors] == [
        ("graph", "triggers", 1, "kind")
    ]
    assert result.triggers_checked == 2


def test_target_and_unused_definitions_are_independent_refusals() -> None:
    document = chain_document(
        target={"key": {"param": "private"}},
        subgraphs={"unused": {"nodes": {"timer": node()}}},
    )

    result = validate_first_slice_configuration(document)

    assert {error.path for error in result.diagnostics.errors} == {
        ("graph", "target"),
        ("graph", "subgraphs"),
        ("graph", "subgraphs", "unused", "nodes", "timer", "kind"),
    }
    assert result.nodes_checked == 4
    assert result.definitions_checked == 1


@pytest.mark.parametrize(
    "source", ["node.py", "/private/source", "https://private/source", "s" * 1025]
)
def test_node_source_never_falls_back_to_ancestor_source(source: str) -> None:
    nodes = chain_nodes()
    nodes["gate"].update(source={"file": source, "line": 7}, retry={"max_attempts": 2})
    document = chain_document(nodes, source={"file": "graph.py", "line": 1})

    result = validate_first_slice_configuration(document)

    error = result.diagnostics.errors[0]
    assert error.node == "gate"
    assert (error.location is not None) is (source == "node.py")
    if error.location is not None:
        assert (error.location.file, error.location.line) == (source, 7)


def test_payload_and_metadata_words_grant_no_feature_authority() -> None:
    data = export_ir(chain_document())
    payload: JsonObject = {
        "skills": ["private"],
        "trust_repo_config": True,
        "budget_usd": 99,
    }
    data["meta"] = {"defaults": payload, "private": payload}
    data["x-private"] = payload
    graph = cast("dict[str, object]", data["graph"])
    graph["params"] = {"default": payload}
    nodes = cast("dict[str, dict[str, object]]", graph["nodes"])
    config = cast("dict[str, object]", nodes["agent"]["config"])
    config.update(
        prompt="skills hooks subagents budget_usd", output_schema={"default": payload}
    )
    document = GraphDocument.model_validate(data)
    before = export_ir(document)

    result = validate_first_slice_configuration(document)

    assert result.authored_configuration_supported
    assert export_ir(document) == before


def test_dormant_syntax_and_independent_refusal_keep_both_owners() -> None:
    nodes = chain_nodes()
    nodes["gate"]["retry"] = {"max_attempts": 2, "predicate": "("}
    document = chain_document(nodes)

    result = validate_first_slice_configuration(document)
    syntax = validate_expression_syntax(document)

    assert [error.code for error in result.diagnostics.errors] == ["IR028"]
    assert [error.code for error in syntax.diagnostics.errors] == ["IR011"]


def test_unresolved_order_does_not_hide_independent_configuration_refusals() -> None:
    nodes = chain_nodes()
    nodes["gate"]["depends_on"] = ["absent"]
    nodes["approve"]["retry"] = {"max_attempts": 2}
    config = cast("dict[str, object]", nodes["agent"]["config"])
    config.update(dict(FORBIDDEN_AGENT_FIELDS))
    document = chain_document(nodes, {"budget_headroom_pct": 0})

    result = validate_first_slice_configuration(document)

    expected = {
        ("graph", "nodes", "agent", "config", field)
        for field, _ in FORBIDDEN_AGENT_FIELDS
    } | {
        ("graph", "nodes", "approve", "retry", "max_attempts"),
        ("runtime", "budget_headroom_pct"),
    }
    assert {error.path for error in result.diagnostics.errors} == expected
    assert all(error.code == "IR028" for error in result.diagnostics.errors)
    assert result.ordering_unresolved == 2


@pytest.mark.parametrize("invalid", [None, False, 0, -1, "1"])
def test_malformed_budget_remains_a_structural_error(invalid: object) -> None:
    with pytest.raises(ValidationError):
        chain_document(runtime_fields={"limits": {"budget_usd": invalid}})
