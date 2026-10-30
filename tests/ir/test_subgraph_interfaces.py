import json

import pytest
from pydantic import MISSING

from kora.ir import (
    SubgraphInterfaceResult,
    export_ir,
    validate_export_types,
    validate_graph_links,
    validate_subgraph_interfaces,
    validate_type_declarations,
)
from tests.ir.edge_type_cases import nested_type
from tests.ir.graph_link_cases import node
from tests.ir.inline_schema_cases import document_for
from tests.ir.subgraph_interface_cases import (
    deep_ancestry_interfaces,
    interface_body,
    interface_wrapper,
    interfaces,
    unused_interfaces,
)
from tests.ir.type_declaration_cases import custom


def assert_partition(result: SubgraphInterfaceResult) -> None:
    assert result.inputs_checked + result.outputs_checked == (
        result.types_compared + result.types_unresolved + result.names_mismatched
    )


@pytest.mark.parametrize(
    ("wrapper", "body", "valid"),
    [
        ("example/Base", "example/Base", True),
        ("example/Child", "example/Base", True),
        ("example/Grandchild", "example/Base", True),
        ("example/Base", "example/Child", False),
        ("example/Child", "example/Sibling", False),
        ("example/Child", "example/Other", False),
        ("VcsRef", "VcsRef", True),
        ("AgentSession", "AgentSession", True),
        ("CommandResult", "CommandResult", True),
        ("StructuredOutput", "StructuredOutput", True),
        ("example/Session", "AgentSession", True),
        ("VcsRef", "CommandResult", False),
    ],
)
@pytest.mark.parametrize("depth", [0, 1, 2])
def test_subgraph_input_types_follow_saved_ancestry(
    wrapper: str, body: str, valid: bool, depth: int
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
                    "AgentSession",
                    "CommandResult",
                    "StructuredOutput",
                )
            },
        },
        nodes={"call": interface_wrapper(nested_type(wrapper, depth), "VcsRef")},
        bodies={"body": interface_body(nested_type(body, depth), "VcsRef")},
    )
    before = export_ir(document)

    result = validate_subgraph_interfaces(document)

    assert result.interfaces_valid is valid
    assert result.nodes_checked == result.types_compared == 2
    assert result.wrappers_checked == result.definitions_checked == 1
    assert result.wrappers_unresolved == result.types_unresolved == 0
    assert result.inputs_checked == result.outputs_checked == 1
    assert result.contracts_checked == result.types_indexed == 10
    assert export_ir(document) == before
    assert_partition(result)
    assert len(result.diagnostics.errors) == int(not valid)
    for error in result.diagnostics.errors:
        assert error.code == "IR003"
        assert error.path == ("graph", "nodes", "call", "inputs", "incoming", "type")
        assert error.node == "call"
        assert error.offset is error.location is None


@pytest.mark.parametrize(
    ("body", "wrapper", "valid"),
    [
        ("example/Base", "example/Base", True),
        ("example/Child", "example/Child", True),
        ("example/Child", "example/Base", False),
        ("example/Base", "example/Child", False),
        ("example/Child", "example/Sibling", False),
        ("VcsRef", "VcsRef", True),
        ("VcsRef", "CommandResult", False),
    ],
)
@pytest.mark.parametrize("depth", [0, 1, 2])
def test_subgraph_outputs_preserve_declared_export_type(
    body: str, wrapper: str, valid: bool, depth: int
) -> None:
    document = document_for(
        contracts={
            "example/A": custom(),
            "example/Base": custom(),
            "example/Child": custom("example/Base"),
            "example/Sibling": custom("example/Base"),
            "VcsRef": {"builtin_revision": "1"},
            "CommandResult": {"builtin_revision": "1"},
        },
        nodes={"call": interface_wrapper(exported=nested_type(wrapper, depth))},
        bodies={"body": interface_body(exported=nested_type(body, depth))},
    )

    result = validate_subgraph_interfaces(document)

    assert result.interfaces_valid is valid
    assert result.types_compared == 2
    assert_partition(result)
    assert len(result.diagnostics.errors) == int(not valid)
    for error in result.diagnostics.errors:
        assert error.path == ("graph", "nodes", "call", "outputs", "value", "type")
        assert error.message == (
            "The wrapper output type must match the declared body export type."
        )


@pytest.mark.parametrize("wrapper", ["example/Base", "example/Child"])
def test_subgraph_output_uses_export_interface_instead_of_internal_producer(
    wrapper: str,
) -> None:
    document = document_for(
        contracts={
            "example/A": custom(),
            "example/Base": custom(),
            "example/Child": custom("example/Base"),
        },
        nodes={"call": interface_wrapper(exported=wrapper)},
        bodies={
            "body": interface_body(exported="example/Base", produced="example/Child")
        },
    )

    result = validate_subgraph_interfaces(document)
    exports = validate_export_types(document)

    assert exports.export_types_valid
    assert result.interfaces_valid is (wrapper == "example/Base")
    assert result.types_compared == 2


@pytest.mark.parametrize("output", [False, True])
@pytest.mark.parametrize(
    ("wrapper_depth", "body_depth"),
    [(0, 1), (1, 0), (2, 1), (2000, 2000), (2000, 1999)],
)
def test_subgraph_interfaces_compare_list_depth_without_recursion(
    output: bool, wrapper_depth: int, body_depth: int
) -> None:
    wrapper_type = nested_type("example/A", wrapper_depth)
    body_type = nested_type("example/A", body_depth)
    document = document_for(
        contracts={"example/A": custom()},
        nodes={
            "call": interface_wrapper(
                **{("exported" if output else "incoming"): wrapper_type}
            )
        },
        bodies={
            "body": interface_body(
                **{("exported" if output else "incoming"): body_type}
            )
        },
    )

    result = validate_subgraph_interfaces(document)

    assert result.interfaces_valid is (wrapper_depth == body_depth)
    assert result.types_compared == 2
    assert_partition(result)


def test_subgraph_names_preserve_complete_sets_and_diagnostic_ownership() -> None:
    body = interface_body()
    body["inputs"] = {"a": {"type": "example/A"}, "b": {"type": "example/A"}}
    body["outputs"] = {
        name: {"type": "example/A", "from": "producer", "output": "value"}
        for name in ("x", "y")
    }
    wrapper = node(
        "subgraph",
        config={"body": "body"},
        inputs={
            name: {"type": "example/A", "from": "$params", "optional": True}
            for name in ("a", "c")
        },
        outputs={name: {"type": "example/B"} for name in ("x", "z")},
    )
    document = document_for(
        contracts={"example/A": custom(), "example/B": custom()},
        nodes={"call": wrapper},
        bodies={"body": body},
    )

    result = validate_subgraph_interfaces(document)
    links = validate_graph_links(document)

    assert not result.interfaces_valid
    assert not links.links_valid
    assert {(error.code, error.path) for error in links.diagnostics.errors} == {
        ("IR002", ("graph", "nodes", "call", "outputs", "z"))
    }
    assert {(error.code, error.path) for error in result.diagnostics.errors} == {
        ("IR003", ("graph", "nodes", "call", "inputs", "b")),
        ("IR003", ("graph", "nodes", "call", "inputs", "c")),
        ("IR003", ("graph", "nodes", "call", "outputs", "x", "type")),
        ("IR003", ("graph", "nodes", "call", "outputs", "y")),
    }
    assert result.inputs_checked == result.outputs_checked == 3
    assert result.types_compared == 2
    assert result.types_unresolved == 1
    assert result.names_mismatched == 3
    assert_partition(result)


def test_extra_output_alone_remains_graph_link_owned() -> None:
    document = document_for(
        nodes={
            "call": node(
                "subgraph",
                config={"body": "body"},
                outputs={"extra": {"type": "VcsRef"}},
            )
        },
        bodies={"body": {"nodes": {"timer": node()}}},
    )

    result = validate_subgraph_interfaces(document)
    links = validate_graph_links(document)

    assert result.interfaces_valid
    assert result.diagnostics.errors == ()
    assert result.outputs_checked == result.types_unresolved == 1
    assert result.names_mismatched == result.types_compared == 0
    assert len(links.diagnostics.errors) == 1
    assert links.diagnostics.errors[0].code == "IR002"
    assert_partition(result)


@pytest.mark.parametrize("wrapper_empty", [False, True])
@pytest.mark.parametrize("body_empty", [False, True])
def test_absent_and_empty_subgraph_interfaces_are_equivalent(
    wrapper_empty: bool, body_empty: bool
) -> None:
    empty: dict[str, object] = {"inputs": {}, "outputs": {}}
    document = document_for(
        nodes={
            "call": node(
                "subgraph",
                config={"body": "body"},
                **(empty if wrapper_empty else {}),
            )
        },
        bodies={
            "body": {
                "nodes": {"timer": node()},
                **({"inputs": {}, "outputs": {}} if body_empty else {}),
            }
        },
    )
    before = export_ir(document)

    result = validate_subgraph_interfaces(document)

    assert result.interfaces_valid
    assert result.inputs_checked == result.outputs_checked == 0
    assert result.wrappers_checked == 1
    assert export_ir(document) == before


@pytest.mark.parametrize(
    "spelling",
    [
        "List[example/A]]",
        "example/Missing",
        "example/Cycle",
        "Unqualified",
        "VcsRef",
        "example/Descendant",
    ],
)
@pytest.mark.parametrize("wrapper_invalid", [False, True])
@pytest.mark.parametrize("output", [False, True])
def test_subgraph_interface_prerequisites_preserve_independent_comparisons(
    spelling: str, wrapper_invalid: bool, output: bool
) -> None:
    invalid = nested_type(spelling, 2)
    field = "exported" if output else "incoming"
    wrapper = interface_wrapper(**{field: invalid if wrapper_invalid else "example/A"})
    body = interface_body(**{field: "example/A" if wrapper_invalid else invalid})
    independent = interface_wrapper("example/B")
    independent["config"] = {"body": "known"}
    document = document_for(
        contracts={
            "example/A": custom(),
            "example/B": custom(),
            "example/Cycle": custom("example/Cycle"),
            "Unqualified": custom(),
            "VcsRef": {"builtin_revision": "2"},
            "example/Descendant": custom("example/Missing"),
        },
        nodes={"call": wrapper, "independent": independent},
        bodies={"body": body, "known": interface_body()},
    )

    result = validate_subgraph_interfaces(document)
    declarations = validate_type_declarations(document)

    assert not declarations.declarations_valid
    assert result.types_unresolved == 1
    assert result.types_compared == 3
    assert len(result.diagnostics.errors) == 1
    assert result.diagnostics.errors[0].node == "independent"
    assert_partition(result)


@pytest.mark.parametrize(
    "invalid",
    [
        "example/Missing",
        "example/Cycle",
        "Unqualified",
        "VcsRef",
        "example/Descendant",
        "List[example/A]]",
    ],
)
def test_equal_invalid_subgraph_output_types_do_not_prove_preservation(
    invalid: str,
) -> None:
    document = document_for(
        contracts={
            "example/A": custom(),
            "example/Cycle": custom("example/Cycle"),
            "Unqualified": custom(),
            "VcsRef": {"builtin_revision": "2"},
            "example/Descendant": custom("example/Missing"),
        },
        nodes={"call": interface_wrapper(exported=invalid)},
        bodies={"body": interface_body(exported=invalid)},
    )

    result = validate_subgraph_interfaces(document)

    assert result.interfaces_valid
    assert result.types_compared == result.types_unresolved == 1
    assert_partition(result)


def test_missing_links_preserve_declared_interface_mismatches() -> None:
    body = interface_body()
    body["outputs"] = {
        "value": {"type": "example/A", "from": "missing", "output": "absent"}
    }
    document = document_for(
        contracts={"example/A": custom(), "example/B": custom()},
        nodes={
            "call": interface_wrapper("example/B", "example/B"),
            "missing": node("subgraph", config={"body": "absent"}),
        },
        bodies={"body": body},
    )

    result = validate_subgraph_interfaces(document)
    links = validate_graph_links(document)

    assert not links.links_valid
    assert not result.interfaces_valid
    assert result.wrappers_checked == 2
    assert result.wrappers_unresolved == 1
    assert result.types_compared == 2
    assert len(result.diagnostics.errors) == 2
    assert_partition(result)


def test_subgraph_interfaces_visit_root_unused_shared_and_nested_wrappers_once() -> (
    None
):
    document = unused_interfaces(1000)
    assert document.graph.subgraphs is not MISSING
    document.graph.nodes["root"] = document.graph.subgraphs["unused0"].nodes["call"]
    document.graph.subgraphs["body"].nodes["recursive"] = document.graph.nodes["root"]

    result = validate_subgraph_interfaces(document)
    shared = validate_subgraph_interfaces(interfaces(1000))
    deep = validate_subgraph_interfaces(deep_ancestry_interfaces(5000, 1000))

    assert result.interfaces_valid
    assert result.definitions_checked == 1001
    assert result.nodes_checked == 1004
    assert result.wrappers_checked == 1002
    assert result.types_compared == 2004
    assert shared.nodes_checked == 1001
    assert shared.wrappers_checked == 1000
    assert shared.types_compared == 2000
    assert deep.interfaces_valid
    assert deep.types_indexed == 5000
    assert_partition(result)


def test_subgraph_interfaces_exclude_fanout_and_opaque_schema_binding_metadata() -> (
    None
):
    body = interface_body()
    body["inputs"] = {
        "incoming": {
            "type": "example/A",
            "schema": {"$ref": "https://example.invalid/never"},
        }
    }
    wrapper = interface_wrapper()
    wrapper["inputs"] = {
        "incoming": {
            "type": "example/A",
            "from": "$previous_attempt",
            "path": "not valid!",
            "optional": True,
        }
    }
    wrapper["outputs"] = {
        "value": {
            "type": "example/A",
            "collect": "absent",
            "schema": {"type": "unknown"},
        }
    }
    document = document_for(
        contracts={"example/A": custom()},
        nodes={
            "call": wrapper,
            "fanout": node("fan_out", config={"body": "absent", "over": "unknown"}),
        },
        bodies={"body": body},
    )
    before = export_ir(document)

    result = validate_subgraph_interfaces(document)

    assert result.interfaces_valid
    assert result.wrappers_checked == 1
    assert result.types_compared == 2
    assert export_ir(document) == before


@pytest.mark.parametrize(
    "source",
    [
        "wrapper.py",
        "/private/unsafe.py",
        "https://example.invalid/source",
        "a" * 1025,
    ],
)
def test_subgraph_interface_diagnostics_use_only_safe_wrapper_source(
    source: str,
) -> None:
    body = interface_body()
    body["source"] = {"file": "body.py", "line": 8}
    document = document_for(
        contracts={"example/A": custom(), "example/B": custom()},
        nodes={
            "call": interface_wrapper("example/B", source={"file": source, "line": 4})
        },
        bodies={"body": body},
        source={"file": "graph.py", "line": 9},
    )

    result = validate_subgraph_interfaces(document)

    (error,) = result.diagnostics.errors
    assert error.node == "call"
    assert error.offset is None
    assert error.path == ("graph", "nodes", "call", "inputs", "incoming", "type")
    if source == "wrapper.py":
        assert error.location is not None
        assert (error.location.file, error.location.line) == (source, 4)
    else:
        assert error.location is None


def test_dense_subgraph_interface_errors_preserve_count_bound() -> None:
    document = interfaces(3000, invalid=True)

    result = validate_subgraph_interfaces(document)

    assert not result.interfaces_valid
    assert len(result.diagnostics.errors) == 1000
    assert result.diagnostics.errors_omitted == 2000
    assert result.types_compared == 6000
    assert_partition(result)


def test_subgraph_interface_errors_preserve_byte_bound() -> None:
    source = {"file": "a" * 1018 + ".py", "line": 1}
    document = document_for(
        contracts={"example/A": custom(), "example/B": custom()},
        nodes={
            f"n{index}": interface_wrapper("example/B", source=source)
            for index in range(1000)
        },
        bodies={"body": interface_body()},
    )

    result = validate_subgraph_interfaces(document)
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

    assert not result.interfaces_valid
    assert 0 < len(result.diagnostics.errors) < 1000
    assert result.diagnostics.errors_omitted == 1000 - len(result.diagnostics.errors)
    assert 1_040_000 < len(encoded) <= 1_048_576
