import json
from pathlib import Path
from typing import cast

import pytest

from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.conformance.capture import Capture
from kora.db.connection import Database
from kora.ir.json import JsonObject
from tests.api.deployment_cases import HARNESS, NAMESPACE, preparer, skeleton_ir
from tests.api.support import api_capture, application, deployment_services
from tests.cli.test_deploy_command import kora
from tests.client.support import running_app
from tests.mcp.test_stdio_deploy import bearer, start, written

__all__ = ["api_capture", "preparer"]


@pytest.mark.asyncio
async def test_mcp_trigger(
    database: Database,
    preparer: DeploymentPreparer,
    api_capture: Capture,
    tmp_path: Path,
) -> None:
    token = await bearer(database, NAMESPACE)
    app = application(
        database, api_capture, deployment_services(database, preparer, HARNESS)
    )
    graph = written(tmp_path, "skeleton.ir.json", skeleton_ir())
    arguments: JsonObject = {
        "namespace": NAMESPACE,
        "graph": "skeleton",
        "idempotency_key": "mcp-start",
    }

    async with running_app(app) as origin:
        deployed = await kora(
            ["deploy", str(graph)], {"KORA_URL": origin, "KORA_TOKEN": token}
        )
        session = await start(
            {"KORA_URL": origin, "KORA_MCP_TOKEN": token, "KORA_LOG_LEVEL": "DEBUG"},
            "operating",
        )
        listed = await session.request("tools/list", {})
        first = await session.request(
            "tools/call", {"name": "kora_trigger", "arguments": arguments}
        )
        changed = await session.request(
            "tools/call",
            {
                "name": "kora_trigger",
                "arguments": arguments | {"allow_unenforced_secrets": True},
            },
        )
        unknown = await session.request(
            "tools/call",
            {"name": "kora_trigger", "arguments": arguments | {"params": {}}},
        )
        code, stderr = await session.close()

    tools = cast("list[JsonObject]", cast("JsonObject", listed["result"])["tools"])
    accepted = cast("JsonObject", first["result"])
    mismatch = cast("JsonObject", changed["result"])
    refused = cast("JsonObject", unknown["result"])
    content = cast("JsonObject", accepted["structuredContent"])
    assert deployed.code == 0
    assert [tool["name"] for tool in tools] == ["kora_trigger"]
    assert accepted.get("isError") is not True
    assert (content["outcome"], content["status"]) == ("accepted", "pending")
    assert cast("str", content["run_id"]).startswith("run_")
    assert mismatch["isError"] is True
    error = cast(
        "JsonObject", cast("JsonObject", mismatch["structuredContent"])["error"]
    )
    assert error["code"] == "API004"
    assert refused["isError"] is True
    events = [
        cast("JsonObject", json.loads(line))
        for line in stderr.splitlines()
        if line.startswith(b"{")
    ]
    finished = [event for event in events if event["event"] == "mcp.tool.finished"]
    assert code == 0
    assert [(event["tool"], event["outcome"]) for event in finished] == [
        ("kora_trigger", "ok"),
        ("kora_trigger", "api_error"),
    ]
    assert token.encode() not in stderr
    assert b"mcp-start" not in stderr
