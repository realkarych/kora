from hashlib import sha256
from typing import cast

from kora.ir.agent_secrets import UNENFORCED
from kora.ir.deployment_checks import (
    DeploymentSelection,
    check_deployment_document,
)
from kora.ir.diagnostics import Diagnostic
from kora.ir.document import GraphDocument
from tests.api.deployment_cases import DEFAULT_POLICY, HARNESS, unresolved_approval
from tests.ir.first_slice_configuration_cases import chain_document, chain_nodes

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
    document = chain_document()

    result = check_deployment_document(document, SELECTION, DEFAULT_POLICY)

    assert result.deployable
    assert result.complete
    assert result.diagnostics.errors == result.diagnostics.warnings == ()
    assert result.diagnostics.errors_omitted == 0


def test_deployment_checks_collect_findings_from_every_pass() -> None:
    nodes = chain_nodes()
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
    nodes = chain_nodes()
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
    nodes = chain_nodes()
    config(nodes, "gate")["resources"] = [EMPTY_FILE]
    document = chain_document(nodes)

    result = check_deployment_document(document, SELECTION, DEFAULT_POLICY)

    assert not result.deployable
    assert ("IR028", ("graph", "nodes", "gate", "config", "resources")) in findings(
        result.diagnostics.errors
    )


def test_deployment_checks_accept_an_empty_command_file_list() -> None:
    nodes = chain_nodes()
    config(nodes, "gate")["resources"] = []
    document = chain_document(nodes)

    result = check_deployment_document(document, SELECTION, DEFAULT_POLICY)

    assert result.deployable


def test_deployment_checks_report_unresolved_work_as_incomplete() -> None:
    document = GraphDocument.model_validate(unresolved_approval())

    result = check_deployment_document(
        document, DeploymentSelection(HARNESS, UNENFORCED), DEFAULT_POLICY
    )

    assert not result.complete
    assert not result.deployable
    assert result.diagnostics.errors == ()
