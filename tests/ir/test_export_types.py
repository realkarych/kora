import json

import pytest
from pydantic import MISSING

from kora.ir import (
    export_ir,
    validate_export_types,
    validate_graph_links,
    validate_type_declarations,
)
from tests.ir.document_cases import NODE_CASES
from tests.ir.edge_type_cases import nested_type, reference
from tests.ir.export_type_cases import (
    ancestry_exports,
    export_body,
    exports,
    shared_exports,
    unused_exports,
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
        ("StructuredOutput", "StructuredOutput", True),
        ("AgentSession", "AgentSession", True),
        ("CommandResult", "CommandResult", True),
        ("example/Session", "AgentSession", True),
    ],
)
@pytest.mark.parametrize("depth", [0, 1, 2])
def test_export_types_follow_saved_shape_and_ancestry(
    produced: str, expected: str, valid: bool, depth: int
) -> None:
    saved: dict[str, dict[str, object]] = {
        "example/Base": custom(),
        "example/Child": custom("example/Base"),
        "example/Grandchild": custom("example/Child"),
        "example/Sibling": custom("example/Base"),
        "example/Other": custom(),
        "example/Session": custom("AgentSession"),
        **{name: {"builtin_revision": "1"} for name in BUILTINS},
    }
    body = export_body(nested_type(produced, depth), nested_type(expected, depth))
    body["source"] = {"file": "body.py", "line": 7}
    document = document_for(contracts=saved, bodies={"unused": body})
    before = export_ir(document)

    result = validate_export_types(document)

    assert result.export_types_valid is valid
    assert result.definitions_checked == result.exports_checked == 1
    assert result.exports_compared == 1
    assert result.exports_unresolved == 0
    assert result.contracts_checked == result.types_indexed == 10
    assert export_ir(document) == before
    assert len(result.diagnostics.errors) == int(not valid)
    for error in result.diagnostics.errors:
        assert error.code == "IR003"
        assert (
            error.message == "The producer type is not compatible with the export type."
        )
        assert error.path == ("graph", "subgraphs", "unused", "outputs", "e0", "type")
        assert error.node is None
        assert error.offset is None
        assert error.location is not None
        assert (error.location.file, error.location.line) == ("body.py", 7)


@pytest.mark.parametrize("producer_kind", NODE_CASES)
def test_exports_cover_all_producer_node_kinds(producer_kind: str) -> None:
    role, type_name = {
        "agent": ("session", "AgentSession"),
        "command": ("result", "CommandResult"),
        "human_input": ("response", "StructuredOutput"),
        "wait_event": ("event", "StructuredOutput"),
    }.get(producer_kind, ("value", "VcsRef"))
    document = document_for(
        contracts={name: {"builtin_revision": "1"} for name in BUILTINS},
        bodies={
            "body": {
                "nodes": {
                    "producer": node(producer_kind, outputs={role: {"type": type_name}})
                },
                "outputs": {"value": reference(type_name, output=role)},
            }
        },
    )

    result = validate_export_types(document)

    assert result.export_types_valid
    assert result.exports_compared == result.exports_checked == 1


@pytest.mark.parametrize(
    ("produced_depth", "expected_depth"),
    [(0, 1), (1, 0), (1, 2), (2, 1), (2000, 1999), (2000, 2000), (2000, 2001)],
)
def test_export_list_depth_is_checked_without_recursion(
    produced_depth: int, expected_depth: int
) -> None:
    document = document_for(
        contracts={"example/Base": custom(), "example/Child": custom("example/Base")},
        bodies={
            "body": export_body(
                nested_type("example/Child", produced_depth),
                nested_type("example/Base", expected_depth),
            )
        },
    )

    result = validate_export_types(document)

    assert result.export_types_valid is (produced_depth == expected_depth)
    assert result.exports_compared == 1
    assert result.exports_unresolved == 0
    assert len(result.diagnostics.errors) == int(produced_depth != expected_depth)


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
def test_export_type_prerequisites_remain_unresolved(
    spelling: str, producer_invalid: bool, other_depth: int
) -> None:
    unknown = nested_type(spelling, 2)
    known = nested_type("example/A", other_depth)
    document = document_for(
        contracts={
            "example/A": custom(),
            "example/Cycle": custom("example/Cycle"),
            "Unqualified": custom(),
            "VcsRef": {"builtin_revision": "2"},
            "example/Descendant": custom("example/Missing"),
        },
        bodies={
            "body": export_body(
                unknown if producer_invalid else known,
                known if producer_invalid else unknown,
            )
        },
    )

    result = validate_export_types(document)
    declarations = validate_type_declarations(document)

    assert result.export_types_valid
    assert result.diagnostics.errors == ()
    assert result.exports_checked == result.exports_unresolved == 1
    assert result.exports_compared == 0
    assert not declarations.declarations_valid


@pytest.mark.parametrize("ending", ["missing", "cycle", "unqualified", "unpublished"])
@pytest.mark.parametrize("expected_depth", [0, 2])
def test_invalid_same_name_ancestry_preserves_independent_export_mismatches(
    ending: str, expected_depth: int
) -> None:
    document = document_for(
        contracts={
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
        },
        bodies={
            "body": {
                "nodes": {
                    "producer": node(
                        outputs={
                            "value": {"type": "example/A"},
                            "unknown": {"type": "List[List[example/Bad]]"},
                        }
                    )
                },
                "outputs": {
                    "unknown": reference(
                        nested_type("example/Bad", expected_depth), output="unknown"
                    ),
                    "bad": reference("example/B"),
                },
            }
        },
    )

    result = validate_export_types(document)
    declarations = validate_type_declarations(document)

    assert not declarations.declarations_valid
    assert not result.export_types_valid
    assert result.exports_compared == result.exports_unresolved == 1
    assert result.exports_checked == 2
    assert len(result.diagnostics.errors) == 1
    assert result.diagnostics.errors[0].path == (
        "graph",
        "subgraphs",
        "body",
        "outputs",
        "bad",
        "type",
    )


@pytest.mark.parametrize("bad_link", [{"from": "missing"}, {"output": "missing"}])
@pytest.mark.parametrize("depth", [0, 2])
def test_export_link_failures_do_not_hide_independent_mismatches(
    bad_link: dict[str, object], depth: int
) -> None:
    body = export_body("example/A", "example/B")
    body["outputs"] = {
        "unknown": reference(nested_type("example/A", depth), **bad_link),
        "bad": reference("example/B"),
    }
    document = document_for(
        contracts={"example/A": custom(), "example/B": custom()}, bodies={"body": body}
    )

    result = validate_export_types(document)
    links = validate_graph_links(document)

    assert not result.export_types_valid
    assert not links.links_valid
    assert result.exports_checked == 2
    assert result.exports_compared == result.exports_unresolved == 1
    assert len(result.diagnostics.errors) == 1
    assert result.diagnostics.errors[0].path == (
        "graph",
        "subgraphs",
        "body",
        "outputs",
        "bad",
        "type",
    )


@pytest.mark.parametrize("source", ["root", "sibling", "call"])
def test_export_producer_names_are_local_to_the_declaring_body(source: str) -> None:
    document = document_for(
        contracts={"example/A": custom()},
        nodes={
            "producer": node(outputs={"value": {"type": "example/A"}}),
            "call": node(
                "subgraph",
                config={"body": "body"},
                outputs={"value": {"type": "example/A"}},
            ),
        },
        bodies={
            "sibling": export_body("example/A", "example/A"),
            "body": {
                "nodes": {"timer": node()},
                "outputs": {
                    "value": reference(
                        "example/A", "call" if source == "call" else "producer"
                    )
                },
            },
        }
        if source != "root"
        else {
            "body": {
                "nodes": {"timer": node()},
                "outputs": {"value": reference("example/A")},
            }
        },
    )

    result = validate_export_types(document)

    assert result.export_types_valid
    assert result.exports_unresolved == 1
    assert result.exports_compared == int(source != "root")
    assert result.exports_checked == result.exports_compared + result.exports_unresolved


def test_stored_exports_are_checked_once_regardless_of_call_count() -> None:
    shared = shared_exports(1000)
    unused = unused_exports(1000)

    called = validate_export_types(shared)
    uncalled = validate_export_types(unused)

    assert called.export_types_valid
    assert uncalled.export_types_valid
    assert (
        called.definitions_checked
        == called.exports_checked
        == called.exports_compared
        == 1
    )
    assert (
        uncalled.definitions_checked
        == uncalled.exports_checked
        == uncalled.exports_compared
        == 1000
    )


@pytest.mark.parametrize("invalid", [False, True])
def test_exports_share_one_deep_saved_ancestry(invalid: bool) -> None:
    document = ancestry_exports(5000, 1000, invalid)

    result = validate_export_types(document)

    assert result.export_types_valid
    assert result.contracts_checked == 5000
    assert result.types_indexed == (0 if invalid else 5000)
    assert result.exports_checked == 1000
    assert result.exports_compared == (0 if invalid else 1000)
    assert result.exports_unresolved == (1000 if invalid else 0)


@pytest.mark.parametrize(
    "source",
    ["body.py", "/private/secret.py", "https://example.invalid/secret", "x" * 1025],
)
def test_export_diagnostics_use_only_safe_body_source(source: str) -> None:
    body = export_body("example/A", "example/B")
    body["source"] = {"file": source, "line": 7}
    body["nodes"] = {
        "producer": node(
            outputs={"value": {"type": "example/A"}},
            source={"file": "producer.py", "line": 2},
        )
    }
    document = document_for(
        contracts={"example/A": custom(), "example/B": custom()},
        bodies={"body": body},
        source={"file": "graph.py", "line": 1},
    )

    result = validate_export_types(document)

    (error,) = result.diagnostics.errors
    assert error.node is None
    assert error.offset is None
    if source == "body.py":
        assert error.location is not None
        assert (error.location.file, error.location.line) == (source, 7)
    else:
        assert error.location is None


def test_export_comparison_does_not_validate_wrapper_mappings_or_schema_behavior() -> (
    None
):
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
        nodes={
            "call": node(
                "subgraph",
                config={"body": "body"},
                inputs={"missing": reference("example/Unknown", "absent")},
                outputs={"different": {"type": "example/Unknown"}},
            )
        },
        bodies={"body": export_body("List[example/Child]", "List[example/Base]")},
    )
    before = export_ir(document)

    result = validate_export_types(document)

    assert result.export_types_valid
    assert result.exports_compared == 1
    assert export_ir(document) == before


@pytest.mark.parametrize("absent", [False, True])
def test_absent_or_empty_definitions_have_no_export_obligations(absent: bool) -> None:
    document = document_for(subgraphs=MISSING if absent else {})

    result = validate_export_types(document)

    assert result.export_types_valid
    assert (
        result.definitions_checked
        == result.exports_checked
        == result.exports_compared
        == result.exports_unresolved
        == 0
    )


def test_definitions_without_outputs_are_still_counted() -> None:
    document = document_for(
        bodies={
            "absent": {"nodes": {"timer": node()}},
            "empty": {"nodes": {"timer": node()}, "outputs": {}},
        }
    )

    result = validate_export_types(document)

    assert result.export_types_valid
    assert result.definitions_checked == 2
    assert (
        result.exports_checked
        == result.exports_compared
        == result.exports_unresolved
        == 0
    )


@pytest.mark.parametrize("depth", [0, 2])
def test_dense_export_mismatches_preserve_diagnostic_count_bound(depth: int) -> None:
    document = exports(3000, invalid=True, list_depth=depth)

    result = validate_export_types(document)

    assert not result.export_types_valid
    assert result.exports_compared == 3000
    assert len(result.diagnostics.errors) == 1000
    assert result.diagnostics.errors_omitted == 2000


def test_export_mismatches_preserve_diagnostic_byte_bound() -> None:
    body = export_body("List[example/A]", "List[example/B]")
    body["source"] = {"file": "s" * 1000, "line": 1}
    body["outputs"] = {
        "e" * 60 + str(index): reference("List[example/B]") for index in range(1000)
    }
    document = document_for(
        contracts={"example/A": custom(), "example/B": custom()}, bodies={"body": body}
    )

    result = validate_export_types(document)
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

    assert not result.export_types_valid
    assert result.exports_compared == 1000
    assert 0 < len(result.diagnostics.errors) < 1000
    assert len(result.diagnostics.errors) + result.diagnostics.errors_omitted == 1000
    assert len(payload) <= 1_048_576
    assert b"example/A" not in payload
    assert b"example/B" not in payload
