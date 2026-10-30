import json

import pytest
from pydantic import MISSING

from kora.ir import (
    export_ir,
    validate_export_types,
    validate_fan_out_types,
    validate_graph_links,
    validate_type_declarations,
)
from tests.ir.document_cases import NODE_CASES
from tests.ir.edge_type_cases import nested_type, reference
from tests.ir.export_type_cases import export_body
from tests.ir.fan_out_type_cases import (
    ancestry_fan_outs,
    fan_out_node,
    fan_outs,
    stored_fan_outs,
)
from tests.ir.graph_link_cases import node
from tests.ir.inline_schema_cases import document_for
from tests.ir.type_declaration_cases import custom


@pytest.mark.parametrize("depth", [0, 1, 2, 2000])
@pytest.mark.parametrize(
    "source", ["producer", "$params", "$inputs", "$previous_attempt"]
)
@pytest.mark.parametrize("path", [MISSING, "nested.value"])
def test_over_checks_local_declared_list_without_upstream_resolution(
    depth: int, source: str, path: str | MISSING
) -> None:
    document = document_for(
        contracts={"example/A": custom()},
        nodes={
            "fan": fan_out_node(
                inputs={
                    "items": reference(
                        nested_type("example/A", depth), source, path=path
                    )
                },
                outputs={},
            )
        },
    )
    before = export_ir(document)

    result = validate_fan_out_types(document)

    assert result.fan_out_types_valid is (depth > 0)
    assert result.nodes_checked == result.fan_outs_checked == 1
    assert result.mapped_inputs_checked == 1
    assert result.mapped_inputs_unresolved == result.collections_checked == 0
    assert export_ir(document) == before
    assert len(result.diagnostics.errors) == int(depth == 0)
    for error in result.diagnostics.errors:
        assert error.code == "IR004"
        assert error.message == "The mapped input type is not a list."
        assert error.path == ("graph", "nodes", "fan", "config", "over")
        assert error.node == "fan"
        assert error.offset is None


@pytest.mark.parametrize(
    ("exported", "expected", "valid"),
    [
        ("example/Base", "example/Base", True),
        ("example/Child", "example/Base", True),
        ("example/Grandchild", "example/Base", True),
        ("example/Base", "example/Child", False),
        ("example/Child", "example/Sibling", False),
        ("example/Child", "example/Other", False),
        ("VcsRef", "VcsRef", True),
        ("VcsRef", "CommandResult", False),
        ("StructuredOutput", "StructuredOutput", True),
        ("AgentSession", "AgentSession", True),
        ("CommandResult", "CommandResult", True),
        ("example/Session", "AgentSession", True),
    ],
)
@pytest.mark.parametrize("depth", [0, 1, 2])
def test_collect_adds_one_list_level_with_saved_ancestry(
    exported: str, expected: str, valid: bool, depth: int
) -> None:
    document = document_for(
        contracts={
            "example/Base": custom(),
            "example/Child": custom("example/Base"),
            "example/Grandchild": custom("example/Child"),
            "example/Sibling": custom("example/Base"),
            "example/Other": custom(),
            "example/Session": custom("AgentSession"),
            **{
                name: {"builtin_revision": "1"}
                for name in (
                    "VcsRef",
                    "StructuredOutput",
                    "AgentSession",
                    "CommandResult",
                )
            },
        },
        nodes={
            "fan": fan_out_node("List[example/Base]", nested_type(expected, depth + 1))
        },
        bodies={
            "body": export_body(
                nested_type(exported, depth), nested_type(exported, depth)
            )
        },
    )
    before = export_ir(document)

    result = validate_fan_out_types(document)

    assert result.fan_out_types_valid is valid
    assert result.nodes_checked == 2
    assert result.definitions_checked == result.fan_outs_checked == 1
    assert result.collections_checked == result.collections_compared == 1
    assert result.collections_unresolved == result.mapped_inputs_unresolved == 0
    assert result.contracts_checked == result.types_indexed == 10
    assert export_ir(document) == before
    assert len(result.diagnostics.errors) == int(not valid)
    for error in result.diagnostics.errors:
        assert error.code == "IR003"
        assert error.message == (
            "The collected export type is not compatible with the output type."
        )
        assert error.path == ("graph", "nodes", "fan", "outputs", "c0", "type")
        assert error.node == "fan"
        assert error.offset is None


@pytest.mark.parametrize(
    ("export_depth", "output_depth"),
    [(0, 0), (1, 1), (1, 3), (2, 1), (2000, 2000), (2000, 2001), (2000, 2002)],
)
def test_collection_depth_is_exact_and_iterative(
    export_depth: int, output_depth: int
) -> None:
    document = document_for(
        contracts={"example/A": custom()},
        nodes={"fan": fan_out_node(collected=nested_type("example/A", output_depth))},
        bodies={
            "body": export_body("example/A", nested_type("example/A", export_depth))
        },
    )

    result = validate_fan_out_types(document)

    assert result.fan_out_types_valid is (output_depth == export_depth + 1)
    assert result.collections_compared == 1
    assert result.collections_unresolved == 0


def test_collection_cannot_bypass_the_declared_export_interface() -> None:
    document = document_for(
        contracts={"example/Base": custom(), "example/Child": custom("example/Base")},
        nodes={"fan": fan_out_node("List[example/Base]", "List[example/Child]")},
        bodies={"body": export_body("example/Child", "example/Base")},
    )

    result = validate_fan_out_types(document)
    exports = validate_export_types(document)

    assert exports.export_types_valid
    assert not result.fan_out_types_valid
    assert result.collections_compared == 1
    assert result.diagnostics.errors[0].code == "IR003"


def test_unknown_types_do_not_hide_independent_known_failures() -> None:
    document = document_for(
        contracts={"example/A": custom(), "example/B": custom()},
        nodes={
            "unknown_over": fan_out_node("example/Missing", "List[example/B]"),
            "unknown_collect": fan_out_node("example/A", "List[example/Missing]"),
        },
        bodies={"body": export_body("example/A", "example/A")},
    )

    result = validate_fan_out_types(document)

    assert result.mapped_inputs_checked == result.mapped_inputs_unresolved == 1
    assert result.collections_compared == result.collections_unresolved == 1
    assert [(error.code, error.node) for error in result.diagnostics.errors] == [
        ("IR004", "unknown_collect"),
        ("IR003", "unknown_over"),
    ]


def test_collection_ignores_producer_and_schema_resolution() -> None:
    contract = custom()
    contract["schema"] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$ref": "https://example.invalid/unavailable",
    }
    contract["behavior"] = dict.fromkeys(
        ("validation", "digest_fields", "summary"), "unavailable"
    )
    body = export_body("example/A", "example/A")
    body["outputs"] = {"e0": reference("example/A", "absent")}
    document = document_for(
        contracts={"example/A": contract, "example/B": custom()},
        nodes={"fan": fan_out_node(collected="List[example/B]")},
        bodies={"body": body},
    )

    result = validate_fan_out_types(document)
    links = validate_graph_links(document)

    assert not links.links_valid
    assert result.collections_compared == 1
    assert result.collections_unresolved == 0
    assert [error.code for error in result.diagnostics.errors] == ["IR003"]


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
@pytest.mark.parametrize("invalid_endpoint", ["over", "export", "output", "both"])
@pytest.mark.parametrize("unknown_depth", [0, 2])
def test_fan_out_type_prerequisites_remain_unresolved(
    spelling: str, invalid_endpoint: str, unknown_depth: int
) -> None:
    unknown = nested_type(spelling, unknown_depth)
    document = document_for(
        contracts={
            "example/A": custom(),
            "example/Cycle": custom("example/Cycle"),
            "Unqualified": custom(),
            "VcsRef": {"builtin_revision": "2"},
            "example/Descendant": custom("example/Missing"),
        },
        nodes={
            "fan": fan_out_node(
                unknown if invalid_endpoint == "over" else "List[example/A]",
                unknown
                if invalid_endpoint in ("output", "both")
                else "List[example/A]",
            )
        },
        bodies={
            "body": export_body(
                "example/A",
                unknown if invalid_endpoint in ("export", "both") else "example/A",
            )
        },
    )

    result = validate_fan_out_types(document)
    declarations = validate_type_declarations(document)

    assert not declarations.declarations_valid
    assert result.fan_out_types_valid
    assert result.diagnostics.errors == ()
    assert result.mapped_inputs_unresolved == int(invalid_endpoint == "over")
    assert result.collections_unresolved == int(invalid_endpoint != "over")
    assert result.mapped_inputs_checked + result.mapped_inputs_unresolved == 1
    assert result.collections_compared + result.collections_unresolved == 1


@pytest.mark.parametrize("missing", ["body", "exports", "export", "collect"])
def test_unresolved_collection_does_not_hide_independent_over_and_collect_errors(
    missing: str,
) -> None:
    body = export_body("example/A", "example/A")
    if missing == "exports":
        body.pop("outputs")
    outputs = {"bad": {"type": "List[example/B]", "collect": "e0"}}
    outputs["unresolved"] = {
        "type": "List[example/A]",
        **({} if missing == "collect" else {"collect": "absent"}),
    }
    document = document_for(
        contracts={"example/A": custom(), "example/B": custom()},
        nodes={"fan": fan_out_node("example/A", outputs=outputs)},
        bodies={} if missing == "body" else {"body": body},
    )

    result = validate_fan_out_types(document)
    links = validate_graph_links(document)

    compared = int(missing in ("export", "collect"))
    assert not links.links_valid
    assert not result.fan_out_types_valid
    assert result.collections_checked == 2
    assert result.collections_compared == compared
    assert result.collections_unresolved == 2 - compared
    assert [error.code for error in result.diagnostics.errors] == ["IR004"] + [
        "IR003"
    ] * compared


@pytest.mark.parametrize(
    "inputs", [MISSING, {}, {"other": reference("List[example/A]")}]
)
def test_over_names_are_local_and_do_not_hide_collection_errors(inputs: object) -> None:
    body = export_body("example/A", "example/A")
    body["inputs"] = {"items": {"type": "List[example/A]"}}
    document = document_for(
        contracts={"example/A": custom(), "example/B": custom()},
        nodes={
            "items": node(outputs={"value": {"type": "List[example/A]"}}),
            "fan": fan_out_node(collected="List[example/B]", inputs=inputs),
        },
        bodies={"body": body},
    )

    result = validate_fan_out_types(document)
    links = validate_graph_links(document)

    assert not links.links_valid
    assert result.mapped_inputs_unresolved == 1
    assert result.mapped_inputs_checked == 0
    assert result.collections_compared == 1
    assert [error.code for error in result.diagnostics.errors] == ["IR003"]


@pytest.mark.parametrize("invalid", [False, True])
def test_fan_outs_share_one_deep_saved_ancestry(invalid: bool) -> None:
    document = ancestry_fan_outs(5000, 1000, invalid)

    result = validate_fan_out_types(document)

    assert result.fan_out_types_valid
    assert result.contracts_checked == 5000
    assert result.types_indexed == (0 if invalid else 5000)
    assert result.fan_outs_checked == result.collections_checked == 1000
    assert (
        result.collections_compared
        == result.mapped_inputs_checked
        == (0 if invalid else 1000)
    )
    assert (
        result.collections_unresolved
        == result.mapped_inputs_unresolved
        == (1000 if invalid else 0)
    )


@pytest.mark.parametrize("shared", [False, True])
def test_stored_fan_outs_are_visited_once_including_unused_bodies(shared: bool) -> None:
    document = stored_fan_outs(1000, shared=shared)

    result = validate_fan_out_types(document)

    assert result.fan_out_types_valid
    assert result.nodes_checked == 1002
    assert (
        result.fan_outs_checked
        == result.collections_compared
        == (1 if shared else 1000)
    )
    assert result.definitions_checked == (2 if shared else 1001)


def test_nested_fan_out_declarations_are_checked_without_expansion() -> None:
    document = document_for(
        contracts={"example/A": custom(), "example/B": custom()},
        nodes={"fan": fan_out_node(collected="List[List[example/A]]")},
        bodies={
            "body": {
                "nodes": {
                    "inner": fan_out_node(
                        "example/A",
                        "List[example/B]",
                        config={"body": "leaf", "over": "items", "max_parallel": 2},
                    )
                },
                "outputs": {"e0": reference("List[example/A]", "inner", output="c0")},
            },
            "leaf": export_body("example/A", "example/A"),
        },
    )

    result = validate_fan_out_types(document)

    assert result.nodes_checked == 3
    assert result.definitions_checked == result.fan_outs_checked == 2
    assert result.collections_compared == 2
    assert [error.code for error in result.diagnostics.errors] == ["IR004", "IR003"]
    assert all(
        error.path is not None
        and error.path[:5] == ("graph", "subgraphs", "body", "nodes", "inner")
        for error in result.diagnostics.errors
    )


@pytest.mark.parametrize("kind", [name for name in NODE_CASES if name != "fan_out"])
def test_other_node_kinds_have_no_fan_out_obligations(kind: str) -> None:
    document = document_for(nodes={"other": node(kind)})

    result = validate_fan_out_types(document)

    assert result.fan_out_types_valid
    assert result.nodes_checked == 1
    assert result.fan_outs_checked == result.collections_checked == 0


@pytest.mark.parametrize("outputs", [MISSING, {}])
@pytest.mark.parametrize("definitions", [MISSING, {}])
def test_absent_or_empty_outputs_have_no_collection_obligations(
    outputs: object, definitions: object
) -> None:
    document = document_for(
        contracts={"example/A": custom()},
        nodes={"fan": fan_out_node(outputs=outputs)},
        subgraphs=definitions,
    )

    result = validate_fan_out_types(document)

    assert result.fan_out_types_valid
    assert result.mapped_inputs_checked == 1
    assert result.collections_checked == result.definitions_checked == 0


@pytest.mark.parametrize(
    "source",
    ["fan.py", "/private/secret.py", "https://example.invalid/secret", "s" * 1025],
)
def test_fan_out_diagnostics_use_only_safe_node_source(source: str) -> None:
    body = export_body("example/A", "example/A")
    body["source"] = {"file": "body.py", "line": 2}
    document = document_for(
        contracts={"example/A": custom(), "example/B": custom()},
        nodes={
            "fan": fan_out_node(
                "example/A", "List[example/B]", source={"file": source, "line": 7}
            )
        },
        bodies={"body": body},
        source={"file": "graph.py", "line": 1},
    )

    result = validate_fan_out_types(document)

    assert len(result.diagnostics.errors) == 2
    for error in result.diagnostics.errors:
        assert error.node == "fan"
        assert error.offset is None
        if source == "fan.py":
            assert error.location is not None
            assert (error.location.file, error.location.line) == (source, 7)
        else:
            assert error.location is None


def test_dense_collection_mismatches_preserve_count_bound() -> None:
    document = fan_outs(1, invalid=True, output_count=3000)

    result = validate_fan_out_types(document)

    assert not result.fan_out_types_valid
    assert result.collections_compared == 3000
    assert len(result.diagnostics.errors) == 1000
    assert result.diagnostics.errors_omitted == 2000


def test_fan_out_mismatches_preserve_byte_bound() -> None:
    document = document_for(
        contracts={"example/A": custom(), "example/B": custom()},
        nodes={
            "f" * 64: fan_out_node(
                collected="List[example/B]",
                count=1000,
                source={"file": "s" * 1000, "line": 1},
            )
        },
        bodies={"body": export_body("example/A", "example/A")},
    )

    result = validate_fan_out_types(document)
    payload = json.dumps(
        {
            "errors": [
                item.model_dump(mode="json") for item in result.diagnostics.errors
            ],
            "warnings": [],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()

    assert not result.fan_out_types_valid
    assert result.collections_compared == 1000
    assert 0 < len(result.diagnostics.errors) < 1000
    assert len(result.diagnostics.errors) + result.diagnostics.errors_omitted == 1000
    assert len(payload) <= 1_048_576
    assert b"example/A" not in payload
    assert b"example/B" not in payload
