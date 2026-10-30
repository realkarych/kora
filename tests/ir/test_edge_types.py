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
    nested_type,
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
@pytest.mark.parametrize("depth", [0, 2])
def test_any_authored_path_excludes_whole_artifact_comparison(
    path: str, depth: int
) -> None:
    document = document_for(
        contracts={"example/A": custom(), "example/B": custom()},
        nodes=edge_nodes(
            nested_type("example/A", depth), nested_type("example/B", depth), path=path
        ),
    )
    before = export_ir(document)

    result = validate_edge_types(document)

    assert result.edge_types_valid
    assert result.inputs_checked == result.edges_excluded == 1
    assert result.edges_checked == result.edges_unresolved == 0
    assert export_ir(document) == before


@pytest.mark.parametrize("source", ["$params", "$inputs", "$previous_attempt"])
@pytest.mark.parametrize("depth", [0, 2])
def test_reserved_sources_are_excluded_without_resolving_payloads(
    source: str, depth: int
) -> None:
    document = document_for(
        nodes={
            "consumer": node(
                inputs={
                    "data": reference(nested_type("example/Missing", depth), source)
                }
            )
        }
    )

    result = validate_edge_types(document)

    assert result.edge_types_valid
    assert result.edges_excluded == result.inputs_checked == 1
    assert result.edges_checked == result.edges_unresolved == 0


@pytest.mark.parametrize(
    ("produced", "expected", "producer_depth", "consumer_depth", "valid"),
    [
        ("example/Base", "example/Base", 1, 1, True),
        ("example/Child", "example/Base", 1, 1, True),
        ("example/Child", "example/Base", 2, 2, True),
        ("example/Grandchild", "example/Base", 2, 2, True),
        ("example/Base", "example/Child", 1, 1, False),
        ("example/Child", "example/Sibling", 1, 1, False),
        ("example/Base", "example/Other", 1, 1, False),
        ("example/Child", "example/Base", 0, 1, False),
        ("example/Child", "example/Base", 1, 0, False),
        ("example/Child", "example/Base", 1, 2, False),
        ("example/Child", "example/Base", 2, 1, False),
        ("VcsRef", "CommandResult", 1, 1, False),
        ("AgentSession", "AgentSession", 1, 1, True),
        ("CommandResult", "CommandResult", 2, 2, True),
        ("StructuredOutput", "StructuredOutput", 2, 2, True),
    ],
)
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("optional", [False, True])
def test_list_edge_types_follow_saved_shape_and_ancestry(
    produced: str,
    expected: str,
    producer_depth: int,
    consumer_depth: int,
    valid: bool,
    nested: bool,
    optional: bool,
) -> None:
    saved: dict[str, dict[str, object]] = {
        "example/Base": custom(),
        "example/Child": custom("example/Base"),
        "example/Grandchild": custom("example/Child"),
        "example/Sibling": custom("example/Base"),
        "example/Other": custom(),
        **{name: {"builtin_revision": "1"} for name in BUILTINS},
    }
    nodes = edge_nodes(
        nested_type(produced, producer_depth),
        nested_type(expected, consumer_depth),
        optional=optional,
    )
    nodes["consumer"]["source"] = {"file": "consumer.py", "line": 7}
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
    assert result.nodes_checked == 2 + int(nested)
    assert result.definitions_checked == int(nested)
    assert result.contracts_checked == result.types_indexed == 9
    assert export_ir(document) == before
    assert len(result.diagnostics.errors) == int(not valid)
    for error in result.diagnostics.errors:
        assert error.code == "IR003"
        assert (
            error.message == "The producer type is not compatible with the input type."
        )
        assert error.path == (*prefix, "nodes", "consumer", "inputs", "value", "type")
        assert error.node == "consumer"
        assert error.offset is None
        assert error.location is not None
        assert error.location.file == "consumer.py"


@pytest.mark.parametrize(
    "spelling",
    [
        "List[example/A]]",
        "List[" * 2000 + "example/A]",
        "example/Missing",
        "example/Cycle",
        "Unqualified",
        "VcsRef",
        "example/Descendant",
    ],
)
@pytest.mark.parametrize("producer_invalid", [False, True])
@pytest.mark.parametrize("other_depth", [0, 2])
def test_list_edge_prerequisites_remain_unresolved(
    spelling: str,
    producer_invalid: bool,
    other_depth: int,
) -> None:
    unknown = nested_type(spelling, 2)
    known = nested_type("example/A", other_depth)
    nodes = edge_nodes(
        unknown if producer_invalid else known, known if producer_invalid else unknown
    )
    document = document_for(
        contracts={
            "example/A": custom(),
            "example/Cycle": custom("example/Cycle"),
            "Unqualified": custom(),
            "VcsRef": {"builtin_revision": "2"},
            "example/Descendant": custom("example/Missing"),
        },
        nodes=nodes,
    )

    result = validate_edge_types(document)
    declarations = validate_type_declarations(document)

    assert result.edge_types_valid
    assert result.diagnostics.errors == ()
    assert result.inputs_checked == result.edges_unresolved == 1
    assert result.edges_checked == result.edges_excluded == 0
    assert not declarations.declarations_valid


@pytest.mark.parametrize("consumer_depth", [1999, 2000, 2001])
def test_deep_list_edges_are_checked_without_recursive_parsing(
    consumer_depth: int,
) -> None:
    document = document_for(
        contracts={"example/Base": custom(), "example/Child": custom("example/Base")},
        nodes=edge_nodes(
            nested_type("example/Child", 2000),
            nested_type("example/Base", consumer_depth),
        ),
    )

    result = validate_edge_types(document)

    assert result.edge_types_valid is (consumer_depth == 2000)
    assert result.edges_checked == 1
    assert result.edges_excluded == result.edges_unresolved == 0
    assert len(result.diagnostics.errors) == int(consumer_depth != 2000)


@pytest.mark.parametrize(
    "bad_link",
    [
        {"from": "missing"},
        {"output": "missing"},
        {"from": MISSING},
        {"output": MISSING},
    ],
)
@pytest.mark.parametrize("depth", [0, 2])
def test_missing_link_prerequisites_do_not_create_secondary_type_errors(
    bad_link: dict[str, object],
    depth: int,
) -> None:
    value = reference(nested_type("example/A", depth))
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
@pytest.mark.parametrize("depth", [0, 2])
def test_invalid_same_name_ancestry_preserves_independent_mismatches(
    ending: str,
    depth: int,
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
                outputs={
                    "value": {"type": "example/A"},
                    "bad": {"type": nested_type("example/Bad", depth)},
                }
            ),
            "consumer": node(
                inputs={
                    "unknown": reference(
                        nested_type("example/Bad", depth), output="bad"
                    ),
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
        2,
        2,
    )
    assert (
        result.inputs_checked
        == result.edges_checked + result.edges_unresolved + result.edges_excluded
    )


@pytest.mark.parametrize("depth", [0, 2])
def test_shared_body_is_checked_once_and_root_names_never_resolve_inside_body(
    depth: int,
) -> None:
    shared = shared_body(1000, list_depth=depth)
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
@pytest.mark.parametrize("depth", [0, 2])
def test_edge_type_diagnostics_use_only_safe_consumer_source(
    source: str, depth: int
) -> None:
    nodes = edge_nodes(nested_type("example/A", depth), nested_type("example/B", depth))
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


@pytest.mark.parametrize("depth", [0, 2])
def test_nominal_comparison_does_not_validate_schema_behavior_or_cel_fields(
    depth: int,
) -> None:
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
        nodes=edge_nodes(
            nested_type("example/Child", depth), nested_type("example/Base", depth)
        ),
    )
    before = export_ir(document)

    result = validate_edge_types(document)

    assert result.edge_types_valid
    assert result.edges_checked == 1
    assert export_ir(document) == before


@pytest.mark.parametrize("depth", [0, 2])
def test_dense_edge_mismatches_preserve_diagnostic_bounds_and_omissions(
    depth: int,
) -> None:
    document = edges(3000, invalid=True, list_depth=depth)

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


def test_list_edge_mismatches_preserve_diagnostic_byte_bound() -> None:
    nodes = {"producer": node(outputs={"value": {"type": "List[example/A]"}})}
    for i in range(1000):
        nodes["n" * 60 + str(i)] = node(
            inputs={"value": reference("List[example/B]")},
            source={"file": "s" * 1000, "line": 1},
        )
    document = document_for(
        contracts={"example/A": custom(), "example/B": custom()}, nodes=nodes
    )

    result = validate_edge_types(document)
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

    assert not result.edge_types_valid
    assert result.edges_checked == 1000
    assert 0 < len(result.diagnostics.errors) < 1000
    assert len(result.diagnostics.errors) + result.diagnostics.errors_omitted == 1000
    assert len(payload) <= 1_048_576
    assert b"example/A" not in payload
    assert b"example/B" not in payload
