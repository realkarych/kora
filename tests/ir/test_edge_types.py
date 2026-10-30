import json

import pytest
from pydantic import MISSING

from kora.ir import (
    export_ir,
    validate_edge_types,
    validate_graph_links,
    validate_type_declarations,
)
from tests.ir.document_cases import NODE_CASES
from tests.ir.edge_type_cases import (
    ancestry_edges,
    edge_nodes,
    edges,
    reference,
    shared_body,
)
from tests.ir.graph_link_cases import node
from tests.ir.inline_schema_cases import document_for
from tests.ir.type_declaration_cases import custom

BUILTINS: tuple[str, ...] = (
    "VcsRef",
    "StructuredOutput",
    "AgentSession",
    "CommandResult",
)


@pytest.mark.parametrize(
    ("produced", "expected", "valid"),
    [
        ("example/Base", "example/Base", True),
        ("example/Child", "example/Base", True),
        ("example/Grandchild", "example/Base", True),
        ("example/Base", "example/Child", False),
        ("example/Child", "example/Sibling", False),
        ("example/Child", "example/Other", False),
        ("VcsRef", "VcsRef", True),
        ("VcsRef", "CommandResult", False),
    ],
)
@pytest.mark.parametrize("nested", [False, True])
def test_direct_edge_types_follow_saved_ancestry_direction(
    produced: str, expected: str, valid: bool, nested: bool
) -> None:
    saved: dict[str, dict[str, object]] = {
        "example/Base": custom(),
        "example/Child": custom("example/Base"),
        "example/Grandchild": custom("example/Child"),
        "example/Sibling": custom("example/Base"),
        "example/Other": custom(),
        **{name: {"builtin_revision": "1"} for name in BUILTINS},
    }
    nodes = edge_nodes(produced, expected)
    document = document_for(
        contracts=saved,
        nodes={"timer": node()} if nested else nodes,
        bodies={"unused": {"nodes": nodes}} if nested else None,
    )
    before = export_ir(document)
    prefix = ("graph", "subgraphs", "unused") if nested else ("graph",)

    result = validate_edge_types(document)

    assert result.edge_types_valid is valid
    assert result.inputs_checked == result.edges_checked == 1
    assert result.edges_unresolved == result.edges_excluded == 0
    assert result.contracts_checked == result.types_indexed == 9
    assert result.nodes_checked == (3 if nested else 2)
    assert result.definitions_checked == int(nested)
    assert export_ir(document) == before
    assert len(result.diagnostics.errors) == int(not valid)
    assert all(
        error.code == "IR003"
        and error.path == (*prefix, "nodes", "consumer", "inputs", "value", "type")
        for error in result.diagnostics.errors
    )


@pytest.mark.parametrize("producer_kind", NODE_CASES)
@pytest.mark.parametrize("consumer_kind", NODE_CASES)
def test_direct_edges_cover_all_producer_and_consumer_node_kinds(
    producer_kind: str, consumer_kind: str
) -> None:
    role, type_name = {
        "agent": ("session", "AgentSession"),
        "command": ("result", "CommandResult"),
        "human_input": ("response", "StructuredOutput"),
        "wait_event": ("event", "StructuredOutput"),
    }.get(producer_kind, ("value", "VcsRef"))
    document = document_for(
        contracts={name: {"builtin_revision": "1"} for name in BUILTINS},
        nodes={
            "producer": node(producer_kind, outputs={role: {"type": type_name}}),
            "consumer": node(
                consumer_kind, inputs={"data": reference(type_name, output=role)}
            ),
        },
    )

    result = validate_edge_types(document)

    assert result.edge_types_valid
    assert result.nodes_checked == 2
    assert result.inputs_checked == result.edges_checked == 1


@pytest.mark.parametrize("path", ["", "field", "items[0]"])
def test_any_authored_path_excludes_whole_artifact_comparison(path: str) -> None:
    document = document_for(
        contracts={"example/A": custom(), "example/B": custom()},
        nodes=edge_nodes("example/A", "example/B", path=path),
    )
    before = export_ir(document)

    result = validate_edge_types(document)

    assert result.edge_types_valid
    assert result.inputs_checked == result.edges_excluded == 1
    assert result.edges_checked == result.edges_unresolved == 0
    assert export_ir(document) == before


@pytest.mark.parametrize("source", ["$params", "$inputs", "$previous_attempt"])
def test_reserved_sources_are_excluded_without_resolving_payloads(source: str) -> None:
    document = document_for(
        nodes={"consumer": node(inputs={"data": reference("example/Missing", source)})}
    )

    result = validate_edge_types(document)

    assert result.edge_types_valid
    assert result.edges_excluded == result.inputs_checked == 1
    assert result.edges_checked == result.edges_unresolved == 0


@pytest.mark.parametrize(
    "spelling",
    ["List[VcsRef]", "List[List[VcsRef]]", "List[VcsRef]]", "List[" * 2000 + "VcsRef]"],
)
@pytest.mark.parametrize("producer_list", [False, True])
def test_all_list_endpoints_are_excluded_including_invalid_spelling(
    spelling: str, producer_list: bool
) -> None:
    document = document_for(
        contracts={"VcsRef": {"builtin_revision": "1"}},
        nodes=edge_nodes(
            spelling if producer_list else "VcsRef",
            "VcsRef" if producer_list else spelling,
        ),
    )

    result = validate_edge_types(document)

    assert result.edge_types_valid
    assert result.edges_excluded == result.inputs_checked == 1
    assert result.edges_checked == result.edges_unresolved == 0


@pytest.mark.parametrize(
    "bad_link",
    [
        {"from": "missing"},
        {"output": "missing"},
        {"from": MISSING},
        {"output": MISSING},
    ],
)
def test_missing_link_prerequisites_do_not_create_secondary_type_errors(
    bad_link: dict[str, object],
) -> None:
    value = reference("example/A")
    value.update(bad_link)
    value = {name: item for name, item in value.items() if item is not MISSING}
    document = document_for(
        contracts={"example/A": custom(), "example/B": custom()},
        nodes={
            "producer": node(outputs={"value": {"type": "example/A"}}),
            "consumer": node(inputs={"unknown": value, "bad": reference("example/B")}),
        },
    )

    result = validate_edge_types(document)
    links = validate_graph_links(document)

    assert not result.edge_types_valid
    assert not links.links_valid
    assert result.inputs_checked == 2
    assert result.edges_checked == result.edges_unresolved == 1
    assert result.edges_excluded == 0
    (error,) = result.diagnostics.errors
    assert error.path == ("graph", "nodes", "consumer", "inputs", "bad", "type")


@pytest.mark.parametrize("ending", ["missing", "cycle", "unqualified", "unpublished"])
def test_invalid_same_name_ancestry_preserves_independent_mismatches(
    ending: str,
) -> None:
    saved: dict[str, dict[str, object]] = {
        "example/Bad": custom(
            {
                "missing": "example/Missing",
                "cycle": "example/Bad",
                "unqualified": "Unqualified",
                "unpublished": "VcsRef",
            }[ending]
        ),
        "Unqualified": custom(),
        "VcsRef": {"builtin_revision": "2"},
        "example/A": custom(),
        "example/B": custom(),
    }
    document = document_for(
        contracts=saved,
        nodes={
            "producer": node(
                outputs={"value": {"type": "example/A"}, "bad": {"type": "example/Bad"}}
            ),
            "consumer": node(
                inputs={
                    "unknown": reference("example/Bad", output="bad"),
                    "bad": reference("example/B"),
                }
            ),
        },
    )

    result = validate_edge_types(document)
    declarations = validate_type_declarations(document)

    assert not declarations.declarations_valid
    assert not result.edge_types_valid
    assert result.edges_checked == result.edges_unresolved == 1
    assert len(result.diagnostics.errors) == 1
    assert result.diagnostics.errors[0].path == (
        "graph",
        "nodes",
        "consumer",
        "inputs",
        "bad",
        "type",
    )


@pytest.mark.parametrize(
    ("produced", "expected"),
    [
        ("example/Missing", "example/Missing"),
        ("example/A", "example/Missing"),
        ("example/Missing", "example/A"),
    ],
)
def test_missing_endpoint_contract_is_unresolved(produced: str, expected: str) -> None:
    document = document_for(
        contracts={"example/A": custom()}, nodes=edge_nodes(produced, expected)
    )

    result = validate_edge_types(document)

    assert result.edge_types_valid
    assert result.edges_unresolved == 1
    assert result.edges_checked == result.edges_excluded == 0


@pytest.mark.parametrize("optional", [False, True])
def test_optional_input_does_not_waive_declared_type_compatibility(
    optional: bool,
) -> None:
    document = document_for(
        contracts={"example/A": custom(), "example/B": custom()},
        nodes=edge_nodes("example/A", "example/B", optional=optional),
    )

    result = validate_edge_types(document)

    assert not result.edge_types_valid
    assert result.edges_checked == 1


def test_edge_type_counter_buckets_partition_inputs_in_scope_order() -> None:
    document = document_for(
        contracts={"example/A": custom()},
        nodes={
            "producer": node(
                outputs={
                    "value": {"type": "example/A"},
                    "list": {"type": "List[example/A]"},
                }
            ),
            "consumer": node(
                inputs={
                    "known": reference("example/A"),
                    "missing": reference("example/A", "absent"),
                    "path": reference("example/Missing", "absent", path=""),
                    "list": reference("example/Missing", output="list"),
                    "reserved": reference("example/Missing", "$params"),
                }
            ),
        },
    )

    result = validate_edge_types(document)

    assert result.edge_types_valid
    assert result.inputs_checked == 5
    assert (result.edges_checked, result.edges_unresolved, result.edges_excluded) == (
        1,
        1,
        3,
    )
    assert (
        result.inputs_checked
        == result.edges_checked + result.edges_unresolved + result.edges_excluded
    )


def test_shared_body_is_checked_once_and_root_names_never_resolve_inside_body() -> None:
    shared = shared_body(1000)
    isolated = document_for(
        contracts={"example/A": custom()},
        nodes={"producer": node(outputs={"value": {"type": "example/A"}})},
        bodies={
            "unused": {
                "nodes": {"consumer": node(inputs={"value": reference("example/A")})}
            }
        },
    )

    result = validate_edge_types(shared)
    missing = validate_edge_types(isolated)

    assert result.nodes_checked == 1002
    assert (
        result.definitions_checked == result.inputs_checked == result.edges_checked == 1
    )
    assert missing.edge_types_valid
    assert missing.edges_unresolved == 1


@pytest.mark.parametrize("invalid", [False, True])
def test_many_distinct_edges_share_one_deep_ancestry_index(invalid: bool) -> None:
    document = ancestry_edges(5000, 1000, invalid)

    result = validate_edge_types(document)

    assert result.edge_types_valid
    assert result.contracts_checked == 5000
    assert result.types_indexed == (0 if invalid else 5000)
    assert result.inputs_checked == 1000
    assert result.edges_checked == (0 if invalid else 1000)
    assert result.edges_unresolved == (1000 if invalid else 0)


@pytest.mark.parametrize(
    "source",
    ["consumer.py", "/private/secret.py", "https://example.invalid/secret", "x" * 1025],
)
def test_edge_type_diagnostics_use_only_safe_consumer_source(source: str) -> None:
    nodes = edge_nodes("example/A", "example/B")
    nodes["consumer"]["source"] = {"file": source, "line": 7}
    nodes["producer"]["source"] = {"file": "producer.py", "line": 2}
    document = document_for(
        contracts={"example/A": custom(), "example/B": custom()},
        nodes=nodes,
        source={"file": "graph.py", "line": 1},
    )

    result = validate_edge_types(document)

    (error,) = result.diagnostics.errors
    assert error.node == "consumer"
    if source == "consumer.py":
        assert error.location is not None
        assert (error.location.file, error.location.line) == (source, 7)
    else:
        assert error.location is None


def test_nominal_comparison_does_not_validate_schema_behavior_or_cel_fields() -> None:
    child = custom("example/Base")
    child["schema"] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "invalid",
        "$ref": "https://example.invalid/private",
    }
    child["behavior"] = {
        "validation": "unavailable",
        "digest_fields": "unavailable",
        "summary": "unavailable",
    }
    document = document_for(
        contracts={"example/Base": custom(), "example/Child": child},
        nodes=edge_nodes("example/Child", "example/Base"),
    )
    before = export_ir(document)

    result = validate_edge_types(document)

    assert result.edge_types_valid
    assert result.edges_checked == 1
    assert export_ir(document) == before


def test_dense_edge_mismatches_preserve_diagnostic_bounds_and_omissions() -> None:
    document = edges(3000, invalid=True)

    result = validate_edge_types(document)

    assert not result.edge_types_valid
    assert result.edges_checked == 3000
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


def test_timer_only_graph_has_no_edge_obligations() -> None:
    document = document_for()

    result = validate_edge_types(document)

    assert result.edge_types_valid
    assert result.nodes_checked == 1
    assert (
        result.inputs_checked
        == result.edges_checked
        == result.edges_unresolved
        == result.edges_excluded
        == 0
    )
