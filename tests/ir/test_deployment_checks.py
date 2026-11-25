from hashlib import sha256
from typing import cast

import pytest

from kora.ir.agent_secrets import UNENFORCED
from kora.ir.deployment_checks import (
    INSTALLATION_HINT,
    DeploymentSelection,
    check_deployment_document,
)
from kora.ir.diagnostics import Diagnostic
from kora.ir.document import GraphDocument
from kora.ir.json import JsonObject
from tests.api.deployment_cases import (
    DEFAULT_POLICY,
    HARNESS,
    skeleton_ir,
    unresolved_predicate,
)
from tests.api.deployment_cases import SELECTION as SKELETON_SELECTION
from tests.engine.graph_output_cases import child
from tests.ir.first_slice_configuration_cases import (
    chain_document,
    installable_chain_nodes,
)

SELECTION = DeploymentSelection("claude", UNENFORCED)
EMPTY_FILE = {
    "name": "check.sh",
    "sha256": "sha256:" + sha256(b"").hexdigest(),
    "size": 0,
    "executable": True,
}


def config(nodes: dict[str, dict[str, object]], name: str) -> dict[str, object]:
    return cast("dict[str, object]", nodes[name]["config"])


def findings(
    errors: tuple[Diagnostic, ...],
) -> set[tuple[str, tuple[str | int, ...] | None]]:
    return {(error.code, error.path) for error in errors}


def test_deployment_checks_accept_the_skeleton_chain() -> None:
    document = chain_document(installable_chain_nodes())

    result = check_deployment_document(document, SELECTION, DEFAULT_POLICY)

    assert result.deployable
    assert result.complete
    assert result.diagnostics.errors == result.diagnostics.warnings == ()
    assert result.diagnostics.errors_omitted == 0


def test_deployment_checks_collect_findings_from_every_pass() -> None:
    nodes = installable_chain_nodes()
    nodes["gate"]["inputs"] = {
        "change": {"type": "VcsRef", "from": "missing", "output": "change"}
    }
    config(nodes, "agent")["harness"] = "codex"
    config(nodes, "agent")["tool_policy"] = {"allow": ["Bash("]}
    config(nodes, "agent")["skills"] = ["private-skill"]
    document = chain_document(nodes)

    result = check_deployment_document(document, SELECTION, DEFAULT_POLICY)

    codes = {error.code for error in result.diagnostics.errors}
    assert not result.deployable
    assert result.complete
    assert {"IR002", "IR013", "IR018", "IR028"} <= codes


def test_deployment_checks_refuse_unselected_providers() -> None:
    nodes = installable_chain_nodes()
    config(nodes, "agent")["harness"] = "codex"
    config(nodes, "gate")["runtime"] = "docker"
    document = chain_document(nodes, runtime_fields={"runtime": "docker"})

    result = check_deployment_document(document, SELECTION, DEFAULT_POLICY)

    assert not result.deployable
    assert findings(result.diagnostics.errors) == {
        ("IR018", ("graph", "nodes", "agent", "config", "harness")),
        ("IR028", ("graph", "nodes", "gate", "config", "runtime")),
        ("IR028", ("runtime", "runtime")),
    }


def test_deployment_checks_refuse_declared_command_files() -> None:
    nodes = installable_chain_nodes()
    config(nodes, "gate")["resources"] = [EMPTY_FILE]
    document = chain_document(nodes)

    result = check_deployment_document(document, SELECTION, DEFAULT_POLICY)

    assert not result.deployable
    assert ("IR028", ("graph", "nodes", "gate", "config", "resources")) in findings(
        result.diagnostics.errors
    )


def test_deployment_checks_accept_an_empty_command_file_list() -> None:
    nodes = installable_chain_nodes()
    config(nodes, "gate")["resources"] = []
    document = chain_document(nodes)

    result = check_deployment_document(document, SELECTION, DEFAULT_POLICY)

    assert result.deployable


def test_deployment_checks_report_unresolved_work_as_incomplete() -> None:
    document = GraphDocument.model_validate(unresolved_predicate())

    result = check_deployment_document(
        document, DeploymentSelection(HARNESS, UNENFORCED), DEFAULT_POLICY
    )

    assert not result.complete
    assert not result.deployable
    assert result.diagnostics.errors == ()


APPROVAL_PATH = ("graph", "nodes", "approve")


@pytest.mark.parametrize(
    ("changes", "expected_path"),
    [
        ({"inputs": {}}, ("inputs",)),
        (
            {
                "inputs": {
                    "gate": {
                        "type": "CommandResult",
                        "from": "gate",
                        "output": "result",
                    },
                    "other": {
                        "type": "CommandResult",
                        "from": "gate",
                        "output": "result",
                    },
                }
            },
            ("inputs",),
        ),
        ({"input": {"from": "agent"}}, ("inputs", "gate")),
        ({"input": {"from": "missing"}}, ("inputs", "gate")),
        ({"input": {"type": "StructuredOutput"}}, ("inputs", "gate")),
        ({"input": {"type": "VcsRef"}}, ("inputs", "gate")),
        ({"input": {"output": "other"}}, ("inputs", "gate")),
        ({"input": {"path": "exit_code"}}, ("inputs", "gate")),
        ({"input": {"optional": True}}, ("inputs", "gate")),
        ({"config": {"context": "inputs.gate.exit_code"}}, ("config", "context")),
        ({"config": {"channel": "slack"}}, ("config", "channel")),
        ({"config": {"binds": {}}}, ("config", "binds")),
        ({}, None),
        ({"config": {"context": "inputs.gate"}}, None),
        ({"config": {"context": "inputs['gate']"}}, None),
        ({"input": {"optional": False}}, None),
    ],
    ids=[
        "empty-inputs",
        "second-input",
        "agent-source",
        "missing-source",
        "structured-output",
        "other-type",
        "other-output",
        "input-path",
        "optional",
        "context-suffix",
        "channel",
        "binds",
        "omitted-context",
        "member",
        "string-index",
        "required-input",
    ],
)
def test_cli_approval_rechecks_exact_authored_context(
    changes: JsonObject, expected_path: tuple[str, ...] | None
) -> None:
    ir = skeleton_ir()
    approval = child(ir, "graph", "nodes", "approve")
    approval["depends_on"] = ["gate"]
    if "inputs" in changes:
        approval["inputs"] = changes["inputs"]
    if "input" in changes:
        child(approval, "inputs", "gate").update(child(changes, "input"))
    if "config" in changes:
        child(approval, "config").update(child(changes, "config"))
    document = GraphDocument.model_validate(ir)
    authored = document.model_dump()

    result = check_deployment_document(document, SKELETON_SELECTION, DEFAULT_POLICY)

    findings = [error for error in result.diagnostics.errors if error.code == "IR028"]
    assert [(error.code, error.path) for error in findings] == (
        [] if expected_path is None else [("IR028", APPROVAL_PATH + expected_path)]
    )
    assert all(error.hint == INSTALLATION_HINT for error in findings)
    assert document.model_dump() == authored
    if expected_path is None:
        assert result.deployable


def test_cli_approval_requires_an_input_declaration() -> None:
    ir = skeleton_ir()
    approval = child(ir, "graph", "nodes", "approve")
    del approval["inputs"]
    approval["depends_on"] = ["gate"]
    document = GraphDocument.model_validate(ir)

    result = check_deployment_document(document, SKELETON_SELECTION, DEFAULT_POLICY)

    assert [
        (error.code, error.path)
        for error in result.diagnostics.errors
        if error.code == "IR028"
    ] == [("IR028", APPROVAL_PATH + ("inputs",))]


def test_cli_approval_preserves_malformed_context_diagnostics() -> None:
    ir = skeleton_ir()
    child(ir, "graph", "nodes", "approve", "config")["context"] = "inputs["
    document = GraphDocument.model_validate(ir)

    result = check_deployment_document(document, SKELETON_SELECTION, DEFAULT_POLICY)

    assert [(error.code, error.path) for error in result.diagnostics.errors] == [
        ("IR011", APPROVAL_PATH + ("config", "context"))
    ]


@pytest.mark.parametrize(
    "context", ["params.gate", "inputs.other", "inputs[0]", "inputs"]
)
def test_cli_approval_leaves_invalid_context_paths_to_their_own_check(
    context: str,
) -> None:
    ir = skeleton_ir()
    child(ir, "graph", "nodes", "approve", "config")["context"] = context
    document = GraphDocument.model_validate(ir)

    result = check_deployment_document(document, SKELETON_SELECTION, DEFAULT_POLICY)

    assert [(error.code, error.path) for error in result.diagnostics.errors] == [
        ("IR002", APPROVAL_PATH + ("config", "context"))
    ]
