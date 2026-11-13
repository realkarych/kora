from pathlib import Path

import pytest
from fastapi import FastAPI

from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.conformance.capture import Capture
from kora.db.connection import Database
from kora.ir.json import parse_json_object
from tests.api.deployment_cases import HARNESS, preparer
from tests.api.support import api_capture, deployment_services
from tests.api.worker_support import worker_application
from tests.plugins.authentication.support import AuthorityClock
from tests.support.ast_modules import write_module
from tests.support.parity import schema_operations, uncovered_operations

__all__ = ["api_capture", "preparer"]

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src"
DEPLOY = ("POST", "/api/v1/namespaces/{}/graphs/{}/versions")
CLIENT = """
GRAPH_VERSIONS_PATH: str = "/api/v1/namespaces/{ns}/graphs/{graph}/versions"


class KoraClient:
    async def deploy(self, ns, request):
        return await self._request_json("POST", GRAPH_VERSIONS_PATH.format(ns=ns))
"""
CALLER = """
async def deploy(client, namespace, request):
    return await client.deploy(namespace, request)
"""
UNRELATED_CALLER = """
async def deploy(tools, namespace, request):
    return await tools.deploy(namespace, request)
"""
PATHS: dict[str, object] = {
    "/healthz": {"get": {}},
    "/api/v1/namespaces/{ns}/graphs/{graph}/versions": {"post": {}},
}


def synthetic_tree(root: Path, *, cli: bool, mcp: bool) -> Path:
    write_module(root, "kora.client.transport", CLIENT)
    write_module(root, "kora.__main__", "")
    write_module(root, "kora.cli.graphs", CALLER if cli else UNRELATED_CALLER)
    write_module(root, "kora.mcp.stdio", CALLER if mcp else UNRELATED_CALLER)
    return root


@pytest.mark.asyncio
async def test_parity_operations_are_reachable(
    api_capture: Capture, database: Database, preparer: DeploymentPreparer
) -> None:
    app = worker_application(
        database,
        api_capture,
        AuthorityClock(),
        deployment=deployment_services(database, preparer, HARNESS),
    )
    framework = app.application
    assert isinstance(framework, FastAPI)

    schema = parse_json_object(framework.openapi())

    paths = schema["paths"]
    assert isinstance(paths, dict)
    assert DEPLOY in schema_operations(paths)
    assert ("POST", "/api/v1/workers") in schema_operations(paths)
    assert uncovered_operations(paths, SOURCE_ROOT) == []


@pytest.mark.parametrize(
    ("cli", "mcp", "expected"),
    [
        (True, True, []),
        (False, True, [f"cli: {DEPLOY[0]} {DEPLOY[1]}"]),
        (True, False, [f"mcp: {DEPLOY[0]} {DEPLOY[1]}"]),
    ],
)
def test_parity_fails_when_a_client_mapping_is_removed(
    tmp_path: Path, cli: bool, mcp: bool, expected: list[str]
) -> None:
    root = synthetic_tree(tmp_path, cli=cli, mcp=mcp)

    uncovered = uncovered_operations(PATHS, root)

    assert uncovered == expected


def test_worker_protocol_exclusion_preserves_worker_read_parity(tmp_path: Path) -> None:
    root = synthetic_tree(tmp_path, cli=True, mcp=True)
    paths: dict[str, object] = {
        **PATHS,
        "/api/v1/workers": {"post": {}, "get": {}},
    }

    uncovered = uncovered_operations(paths, root)

    assert uncovered == ["cli: GET /api/v1/workers", "mcp: GET /api/v1/workers"]
