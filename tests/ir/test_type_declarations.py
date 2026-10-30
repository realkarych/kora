import json
from itertools import permutations
from pathlib import Path

import pytest

from kora.ir import TypeDeclarationResult, validate_type_declarations
from tests.ir.document_cases import NODE_CASES
from tests.ir.graph_link_cases import node
from tests.ir.type_declaration_cases import custom, declarations, model, parent_chain

BUILTINS: tuple[str, ...] = (
    "VcsRef",
    "StructuredOutput",
    "AgentSession",
    "CommandResult",
)


def findings(
    result: TypeDeclarationResult,
) -> set[tuple[str, tuple[str | int, ...] | None]]:
    return {(item.code, item.path) for item in result.diagnostics.errors}


def test_empty_type_closure_is_valid_for_a_timer_graph() -> None:
    document = model()

    result = validate_type_declarations(document)

    assert result.declarations_valid
    assert result.contracts_checked == result.declarations_checked == 0


@pytest.mark.parametrize("depth", [0, 1, 2, 2_000])
def test_type_declarations_accept_balanced_lists_without_a_depth_cap(
    depth: int,
) -> None:
    document = declarations(1, depth=depth)

    result = validate_type_declarations(document)

    assert result.declarations_valid
    assert result.declarations_checked == result.contracts_checked == 1


@pytest.mark.parametrize(
    "spelling",
    [
        "List[example/Value]]",
        "List[List[example/Value]",
        "List[" * 2000 + "example/Value]",
    ],
)
def test_unbalanced_type_spelling_is_ir003_without_a_secondary_missing_type(
    spelling: str,
) -> None:
    document = model(nodes={"timer": node(inputs={"value": {"type": spelling}})})

    result = validate_type_declarations(document)

    assert findings(result) == {
        ("IR003", ("graph", "nodes", "timer", "inputs", "value", "type"))
    }
    assert not result.declarations_valid


@pytest.mark.parametrize("kind", list(NODE_CASES))
def test_type_declarations_visit_every_node_kind(kind: str) -> None:
    contracts: dict[str, dict[str, object]] = {
        name: {"builtin_revision": "1"} for name in BUILTINS
    }
    document = model(
        contracts,
        {"consumer": node(kind, inputs={"value": {"type": "example/Missing"}})},
    )

    result = validate_type_declarations(document)

    assert findings(result) == {
        ("IR022", ("graph", "nodes", "consumer", "inputs", "value", "type"))
    }


def test_type_declarations_cover_body_interfaces_exports_and_node_outputs() -> None:
    missing = {"type": "example/Missing"}
    document = model(
        nodes={"root": node(outputs={"data": missing})},
        bodies={
            "unused": {
                "inputs": {"input": missing},
                "nodes": {
                    "child": node(inputs={"input": missing}, outputs={"data": missing})
                },
                "outputs": {"output": {**missing, "from": "child", "output": "data"}},
            }
        },
    )

    result = validate_type_declarations(document)

    prefix = ("graph", "subgraphs", "unused")
    assert findings(result) == {
        ("IR022", ("graph", "nodes", "root", "outputs", "data", "type")),
        ("IR022", prefix + ("inputs", "input", "type")),
        ("IR022", prefix + ("nodes", "child", "inputs", "input", "type")),
        ("IR022", prefix + ("nodes", "child", "outputs", "data", "type")),
        ("IR022", prefix + ("outputs", "output", "type")),
    }
    assert result.declarations_checked == 5


@pytest.mark.parametrize("name", BUILTINS)
def test_builtin_type_recognition_matches_the_published_manifest(name: str) -> None:
    path = Path("knowledge/specs/artifacts") / f"{name}.revision-1.json"
    manifest = json.loads(path.read_text())
    document = model(
        {name: {"builtin_revision": manifest["builtin_revision"]}},
        {"consumer": node(inputs={"value": {"type": name}})},
    )

    result = validate_type_declarations(document)

    assert manifest["name"] == name
    assert result.declarations_valid
    assert result.contracts_checked == result.declarations_checked == 1


@pytest.mark.parametrize(
    ("name", "revision"),
    [
        ("VcsRef", "2"),
        ("VcsRef", "01"),
        ("StructuredOutput", "future"),
        ("AgentSession", "2"),
        ("CommandResult", "2"),
        ("ReviewBundle", "1"),
        ("PreviousAttempt", "1"),
        ("example/VcsRef", "1"),
    ],
)
def test_unknown_builtin_names_and_revisions_are_ir022(
    name: str, revision: str
) -> None:
    document = model({name: {"builtin_revision": revision}})

    result = validate_type_declarations(document)

    assert findings(result) == {("IR022", ("type_contracts", name, "builtin_revision"))}


@pytest.mark.parametrize("name", [*BUILTINS, "Unqualified"])
def test_custom_declarations_cannot_replace_builtin_names(name: str) -> None:
    document = model({name: custom()})

    result = validate_type_declarations(document)

    assert findings(result) == {("IR022", ("type_contracts", name))}


def test_custom_parent_chains_use_saved_names_without_schema_or_behavior_checks() -> (
    None
):
    child = custom("example/Parent")
    child["schema"] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$ref": "https://unresolved.invalid/private-schema",
    }
    child["behavior"] = {
        "validation": "unavailable",
        "digest_fields": "unavailable",
        "summary": "unavailable",
    }
    document = model(
        {
            "example/Child": child,
            "example/Parent": custom("StructuredOutput"),
            "StructuredOutput": {"builtin_revision": "1"},
        },
        {"consumer": node(inputs={"value": {"type": "List[example/Child]"}})},
    )
    original = document.model_dump_json()

    result = validate_type_declarations(document)

    assert result.declarations_valid
    assert result.contracts_checked == 3
    assert document.model_dump_json() == original


def test_parent_cycles_exclude_descendants_and_preserve_independent_faults() -> None:
    document = model(
        {
            "example/A": custom("example/B"),
            "example/B": custom("example/A"),
            "example/Descendant": custom("example/A"),
            "example/Self": custom("example/Self"),
            "example/Missing": custom("example/Absent"),
            "VcsRef": custom("example/AnotherMissing"),
        },
        {"consumer": node(inputs={"value": {"type": "example/Undeclared"}})},
    )

    result = validate_type_declarations(document)

    assert findings(result) == {
        ("IR022", ("type_contracts", "example/A", "parent")),
        ("IR022", ("type_contracts", "example/B", "parent")),
        ("IR022", ("type_contracts", "example/Self", "parent")),
        ("IR022", ("type_contracts", "example/Missing", "parent")),
        ("IR022", ("type_contracts", "VcsRef")),
        ("IR022", ("type_contracts", "VcsRef", "parent")),
        ("IR022", ("graph", "nodes", "consumer", "inputs", "value", "type")),
    }


@pytest.mark.parametrize("cyclic", [False, True])
def test_parent_chains_beyond_recursion_depth_are_checked_iteratively(
    cyclic: bool,
) -> None:
    document = parent_chain(1200, cyclic)

    result = validate_type_declarations(document)

    assert result.declarations_valid is not cyclic
    assert result.contracts_checked == 1200
    assert result.declarations_checked == 0
    assert len(result.diagnostics.errors) == (1000 if cyclic else 0)
    assert result.diagnostics.errors_omitted == (200 if cyclic else 0)


def test_type_diagnostics_are_independent_of_contract_insertion_order() -> None:
    contracts = {
        "example/A": custom("example/B"),
        "example/B": custom("example/A"),
        "example/C": custom("example/Absent"),
    }
    documents = [model(dict(items)) for items in permutations(contracts.items())]

    results = [validate_type_declarations(document) for document in documents]

    assert all(result == results[0] for result in results)


@pytest.mark.parametrize("count", [1, 100, 1000])
def test_type_declarations_count_shared_base_types_once_per_declaration(
    count: int,
) -> None:
    document = declarations(count)

    result = validate_type_declarations(document)

    assert result.declarations_valid
    assert result.declarations_checked == count
    assert result.contracts_checked == 1


def test_shared_bodies_do_not_expand_declaration_work() -> None:
    document = model(
        nodes={
            f"call{i}": node("subgraph", config={"body": "body"}) for i in range(1000)
        },
        bodies={"body": {"nodes": {"child": node(inputs={"x": {"type": "Missing"}})}}},
    )

    result = validate_type_declarations(document)

    assert result.declarations_checked == 1
    assert len(result.diagnostics.errors) == 1


def test_dense_type_errors_remain_bounded_and_invalid() -> None:
    document = declarations(3000, invalid=True)

    result = validate_type_declarations(document)

    assert not result.declarations_valid
    assert result.declarations_checked == 3000
    assert len(result.diagnostics.errors) == 1000
    assert result.diagnostics.errors_omitted == 2000


@pytest.mark.parametrize("length", [256, 257, 10000])
def test_long_contract_names_use_a_safe_ancestor_without_truncation(
    length: int,
) -> None:
    name = "example/" + "A" * (length - len("example/"))
    document = model({name: custom("example/Missing")})

    result = validate_type_declarations(document)

    expected = (
        ("type_contracts", name, "parent") if length == 256 else ("type_contracts",)
    )
    assert findings(result) == {("IR022", expected)}
    assert result.diagnostics.errors[0].location is None


@pytest.mark.parametrize(
    "source",
    [
        {"file": "/private-content", "line": 1},
        {"file": "https://private-content", "line": 1},
        {"file": "x" * 1025, "line": 1},
        {"file": "\ud800", "line": 1},
        {"file": "private-content", "line": 2**53},
    ],
)
def test_type_diagnostics_exclude_unsafe_sources_and_authored_data(
    source: dict[str, object],
) -> None:
    document = model(
        nodes={
            "consumer": node(
                "agent",
                inputs={"value": {"type": "example/Missing"}},
                source=source,
                config={
                    "harness": "claude",
                    "prompt": "private-content",
                    "workspace": {"repo": "repo", "rev": "main"},
                },
                **{"x-private-content": "private-content"},
            )
        }
    )

    result = validate_type_declarations(document)

    assert result.diagnostics.errors
    assert all(item.location is None for item in result.diagnostics.errors)
    assert all(
        "private-content" not in item.model_dump_json()
        for item in result.diagnostics.errors
    )


def test_type_diagnostics_preserve_node_and_body_source_metadata() -> None:
    source = {"file": "graphs/example.py", "line": 23}
    document = model(
        nodes={"consumer": node(inputs={"value": {"type": "Missing"}}, source=source)},
        bodies={
            "body": {
                "nodes": {"timer": node()},
                "inputs": {"value": {"type": "Missing"}},
                "source": source,
            }
        },
    )

    result = validate_type_declarations(document)

    assert len(result.diagnostics.errors) == 2
    assert {item.node for item in result.diagnostics.errors} == {"consumer", None}
    assert all(
        item.location is not None
        and item.location.file == "graphs/example.py"
        and item.location.line == 23
        for item in result.diagnostics.errors
    )
