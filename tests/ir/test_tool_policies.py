import json

import pytest
from pydantic import MISSING

from kora.ir import ToolPolicy, export_ir, validate_tool_policies
from kora.ir.globs import utf8_size
from kora.ir.tool_patterns import tool_policy_refusals
from tests.ir.graph_link_cases import node
from tests.ir.inline_schema_cases import document_for
from tests.ir.tool_policy_cases import (
    byte_boundary_patterns,
    folding_boundary_patterns,
    piece_boundary_patterns,
    policy_agent,
    policy_document,
)


@pytest.mark.parametrize(
    ("pattern", "reason"),
    [
        ("", "the tool pattern is malformed"),
        ("Bash()", "the tool pattern is malformed"),
        ("Bash(a(b))", "the tool pattern is malformed"),
        ("Bash(x)tail", "the tool pattern is malformed"),
        ("Ba sh", "the tool pattern is malformed"),
        ("\u0392ash", "the tool pattern is malformed"),
        ("Bash(x\n)", "the tool pattern is malformed"),
        ("Bash(x\x00)", "the tool pattern is malformed"),
        (
            "Bash(npm run:*)",
            "the tool pattern uses a harness form kora does not support",
        ),
        ("Read(~/x)", "the tool pattern uses a harness form kora does not support"),
        ("Edit(//x)", "the tool pattern uses a harness form kora does not support"),
        ("Edit(/abs)", "the file-edit pattern argument is not a relative path pattern"),
        (
            "Write(../x)",
            "the file-edit pattern argument is not a relative path pattern",
        ),
        (
            "apply_patch(src//x)",
            "the file-edit pattern argument is not a relative path pattern",
        ),
        (
            "NotebookEdit(./x)",
            "the file-edit pattern argument is not a relative path pattern",
        ),
        ("R" * 97 + "*", "a wildcard piece of the tool pattern is too long"),
        (
            "Bash(" + "\u00df" * 49 + "*)",
            "a wildcard piece of the tool pattern is too long",
        ),
        (
            "Write(gates/x\u00e1*)",
            "a wildcard of the deny pattern touches a combining mark",
        ),
        (
            "Bash(rm *\u0301x)",
            "a wildcard of the deny pattern touches a combining mark",
        ),
        (
            "Write(gates/\u1fb3*)",
            "a wildcard of the deny pattern touches a combining mark",
        ),
        ("Bash(" + "x" * 4091 + ")", "the tool pattern is too long"),
    ],
)
def test_tool_policy_patterns_follow_shared_grammar(pattern: str, reason: str) -> None:
    document = policy_document({"deny": ["Read", pattern]})
    before = export_ir(document)

    result = validate_tool_policies(document)

    assert not result.tool_policies_valid
    (error,) = result.diagnostics.errors
    assert error.code == "IR013"
    assert error.message == reason
    assert error.path == ("graph", "nodes", "n0", "config", "tool_policy", "deny", 1)
    assert error.node == "n0"
    assert error.offset is error.location is None
    assert result.nodes_checked == result.policies_checked == 1
    assert result.patterns_checked == 2
    assert export_ir(document) == before


@pytest.mark.parametrize("array", ["allow", "deny"])
@pytest.mark.parametrize("pattern", ["StructuredOutput", "StructuredOutput(value)"])
def test_tool_policy_output_channel_is_kora_owned(array: str, pattern: str) -> None:
    policy = ToolPolicy.model_validate({array: [pattern]})

    found = list(tool_policy_refusals(policy))

    assert [(path, refusal.value) for path, refusal in found] == [
        ((array, 0), "the tool pattern names the reserved output channel")
    ]


@pytest.mark.parametrize("array", ["allow", "deny"])
def test_tool_policy_wildcards_and_supported_forms_remain_valid(array: str) -> None:
    patterns = [
        "Structured*",
        "*Output",
        "*",
        "structuredoutput",
        "mcp__server__StructuredOutput",
        "mcp__my-server",
        "Edit(src/**)",
        "E*(/abs/x)",
        "Bash(git *)",
        "R" * 96 + "*",
        "Bash(" + "\u00df" * 48 + "*)",
        "Bash(" + "a" * 4090 + ")",
        "Bash(\ud800)",
    ]
    document = policy_document({array: patterns})

    result = validate_tool_policies(document)

    assert result.tool_policies_valid
    assert result.patterns_checked == len(patterns)


def test_tool_policy_allow_wildcards_keep_combining_marks() -> None:
    patterns = ["Write(gates/x\u00e1*)", "Bash(rm *\u0301x)", "Write(gates/\u1fb3*)"]

    result = validate_tool_policies(policy_document({"allow": patterns}))

    assert result.tool_policies_valid
    assert result.patterns_checked == 3


@pytest.mark.parametrize("array", ["allow", "deny"])
@pytest.mark.parametrize("bound", ["count", "bytes", "pieces", "folding"])
def test_tool_policy_bounds_preserve_inclusive_limits(array: str, bound: str) -> None:
    at_bound = {
        "count": ["Read"] * 128,
        "bytes": byte_boundary_patterns(),
        "pieces": piece_boundary_patterns(),
        "folding": folding_boundary_patterns(),
    }[bound]
    over_bound = (
        at_bound + ["Read"] if bound != "folding" else folding_boundary_patterns(1)
    )
    if bound == "bytes":
        assert utf8_size(at_bound) == 65536
    documents = [
        policy_document({array: patterns}) for patterns in (at_bound, over_bound)
    ]

    valid, invalid = [validate_tool_policies(document) for document in documents]

    assert valid.tool_policies_valid
    assert not invalid.tool_policies_valid
    (error,) = invalid.diagnostics.errors
    assert error.path == ("graph", "nodes", "n0", "config", "tool_policy") + (
        (array,) if bound in ("count", "bytes") else ()
    )
    assert (
        error.message
        == {
            "count": "the pattern array is too large",
            "bytes": "the pattern array is too large",
            "pieces": "the tool policy has too many wildcard pieces",
            "folding": "the tool policy has too many characters outside ASCII",
        }[bound]
    )
    assert invalid.patterns_checked == len(over_bound)


def test_oversized_array_preserves_independent_policy_findings() -> None:
    document = policy_document(
        {"allow": ["StructuredOutput"] * 129, "deny": ["(", "StructuredOutput"]}
    )

    result = validate_tool_policies(document)

    prefix = ("graph", "nodes", "n0", "config", "tool_policy")
    assert [(error.path, error.message) for error in result.diagnostics.errors] == [
        (prefix + ("allow",), "the pattern array is too large"),
        (prefix + ("deny", 0), "the tool pattern is malformed"),
        (prefix + ("deny", 1), "the tool pattern names the reserved output channel"),
    ]
    assert result.patterns_checked == 131


@pytest.mark.parametrize("pattern", ["StructuredOutput()", "StructuredOutput(x:*)"])
def test_malformed_reserved_patterns_have_one_grammar_diagnostic(pattern: str) -> None:
    document = policy_document({"deny": [pattern]})

    result = validate_tool_policies(document)

    assert len(result.diagnostics.errors) == 1
    assert (
        result.diagnostics.errors[0].message
        != "the tool pattern names the reserved output channel"
    )


def test_tool_policies_visit_root_unused_and_shared_definitions_once() -> None:
    body: dict[str, object] = {
        "nodes": {"agent": policy_agent({"deny": ["StructuredOutput"]})}
    }
    document = document_for(
        nodes={
            f"call{index}": node("subgraph", config={"body": "shared"})
            for index in range(1000)
        },
        bodies={"shared": body, "unused": body},
    )
    before = export_ir(document)

    result = validate_tool_policies(document)

    assert not result.tool_policies_valid
    assert result.nodes_checked == 1002
    assert (
        result.definitions_checked
        == result.policies_checked
        == result.patterns_checked
        == 2
    )
    assert {error.path for error in result.diagnostics.errors} == {
        (
            "graph",
            "subgraphs",
            name,
            "nodes",
            "agent",
            "config",
            "tool_policy",
            "deny",
            0,
        )
        for name in ("shared", "unused")
    }
    assert export_ir(document) == before


def test_tool_policy_pass_preserves_omission_empty_and_opaque_fields() -> None:
    missing = node("agent")
    missing["x-policy"] = {"tool_policy": {"deny": ["StructuredOutput"]}}
    document = document_for(
        nodes={
            "missing": missing,
            "empty": policy_agent({}),
            "extensions": policy_agent({"x-value": {"deny": ["("]}}),
            "command": node(
                "command",
                config={
                    "cmd": ["StructuredOutput("],
                    "workspace": {"repo": ""},
                    "x-policy": {"deny": ["("]},
                },
            ),
        }
    )
    before = export_ir(document)

    result = validate_tool_policies(document)

    assert result.tool_policies_valid
    assert result.nodes_checked == 4
    assert result.policies_checked == 2
    assert result.patterns_checked == result.definitions_checked == 0
    assert export_ir(document) == before
    agent = document.graph.nodes["missing"]
    assert agent.kind == "agent"
    assert agent.config.tool_policy is MISSING


@pytest.mark.parametrize(
    "source", ["node.py", "/host/path", "https://example.invalid/source", "x" * 1025]
)
def test_tool_policy_diagnostics_use_only_safe_node_source(source: str) -> None:
    document = document_for(
        nodes={"timer": node()},
        bodies={
            "unused": {
                "source": {"file": "body.py", "line": 2},
                "nodes": {
                    "agent": policy_agent(
                        {"deny": ["("]}, source={"file": source, "line": 3}
                    )
                },
            }
        },
        source={"file": "graph.py", "line": 1},
    )

    result = validate_tool_policies(document)

    (error,) = result.diagnostics.errors
    assert error.path == (
        "graph",
        "subgraphs",
        "unused",
        "nodes",
        "agent",
        "config",
        "tool_policy",
        "deny",
        0,
    )
    assert error.node == "agent"
    assert error.offset is None
    if source == "node.py":
        assert error.location is not None
        assert (error.location.file, error.location.line) == (source, 3)
    else:
        assert error.location is None


def test_tool_policy_diagnostics_preserve_count_bound() -> None:
    document = policy_document({"deny": ["("]}, 3000)

    result = validate_tool_policies(document)

    assert not result.tool_policies_valid
    assert len(result.diagnostics.errors) == 1000
    assert result.diagnostics.errors_omitted == 2000
    assert result.policies_checked == result.patterns_checked == 3000


def test_tool_policy_diagnostics_preserve_byte_bound() -> None:
    document = document_for(
        nodes={
            f"n{index}": policy_agent(
                {"deny": ["("]}, source={"file": "a" * 1021 + ".py", "line": 1}
            )
            for index in range(1000)
        }
    )

    result = validate_tool_policies(document)
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

    assert not result.tool_policies_valid
    assert 0 < len(result.diagnostics.errors) < 1000
    assert result.diagnostics.errors_omitted == 1000 - len(result.diagnostics.errors)
    assert 1_040_000 < len(encoded) <= 1_048_576
