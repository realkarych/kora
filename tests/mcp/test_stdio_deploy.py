import asyncio
import json
import os
import socket
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pytest

from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.conformance.capture import Capture
from kora.db.connection import Database
from kora.ir.json import JsonObject
from tests.api.deployment_cases import HARNESS, NAMESPACE, preparer, skeleton_ir
from tests.api.support import api_capture, application, deployment_services
from tests.client.support import running_app
from tests.plugins.authentication.support import seed_case

__all__ = ["api_capture", "preparer"]

DEADLINE_SECONDS = 60.0
PROTOCOL_VERSION = "2025-11-25"


@dataclass
class StdioSession:
    process: asyncio.subprocess.Process
    lines: list[JsonObject] = field(default_factory=list[JsonObject])
    sequence: int = 0

    async def send(self, message: JsonObject) -> None:
        stdin = self.process.stdin
        assert stdin is not None
        stdin.write(json.dumps(message).encode() + b"\n")
        await stdin.drain()

    async def request(self, method: str, params: JsonObject) -> JsonObject:
        self.sequence += 1
        await self.send(
            {"jsonrpc": "2.0", "id": self.sequence, "method": method, "params": params}
        )
        stdout = self.process.stdout
        assert stdout is not None
        while True:
            line = await asyncio.wait_for(stdout.readline(), DEADLINE_SECONDS)
            assert line, "the server closed stdout before responding"
            message = cast("JsonObject", json.loads(line))
            self.lines.append(message)
            if message.get("id") == self.sequence:
                return message

    async def call(self, path: Path) -> JsonObject:
        response = await self.request(
            "tools/call", {"name": "kora_deploy", "arguments": {"path": str(path)}}
        )
        return cast("JsonObject", response["result"])

    async def close(self) -> tuple[int, bytes]:
        stdin = self.process.stdin
        assert stdin is not None
        stdin.close()
        stdout, stderr = await asyncio.wait_for(
            self.process.communicate(), DEADLINE_SECONDS
        )
        assert stdout == b""
        assert self.process.returncode is not None
        return self.process.returncode, stderr


async def start(environment: dict[str, str]) -> StdioSession:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-m",
        "kora",
        "mcp",
        "--server",
        "authoring",
        env={"PATH": os.environ["PATH"], **environment},
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    session = StdioSession(process)
    await session.request(
        "initialize",
        {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "kora-test", "version": "1"},
        },
    )
    await session.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    return session


async def bearer(database: Database, namespace: str) -> str:
    case = await seed_case(database, namespace)
    authorization = case.request.authorization
    assert authorization is not None
    return authorization.removeprefix("Bearer ")


def written(directory: Path, name: str, document: JsonObject) -> Path:
    path = directory / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_mcp_deploy_without_files(
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
    refused = written(tmp_path, "refused.ir.json", skeleton_ir(harness="claude"))

    async with running_app(app) as origin:
        session = await start(
            {"KORA_URL": origin, "KORA_MCP_TOKEN": token, "KORA_LOG_LEVEL": "DEBUG"}
        )
        listed = await session.request("tools/list", {})
        published = await session.call(graph)
        rejected = await session.call(refused)
        malformed = await session.call(tmp_path / "absent.ir.json")
        code, stderr = await session.close()

    tools = cast("list[JsonObject]", cast("JsonObject", listed["result"])["tools"])
    published_content = cast("JsonObject", published["structuredContent"])
    rejected_content = cast("JsonObject", rejected["structuredContent"])
    assert [tool["name"] for tool in tools] == ["kora_deploy"]
    assert published.get("isError") is not True
    assert cast("str", published_content["graph_version_id"]).startswith("gv_")
    assert published_content["reused"] is False
    assert rejected["isError"] is True
    assert cast("JsonObject", rejected_content["error"])["code"] == "API003"
    assert malformed["isError"] is True
    assert all(line["jsonrpc"] == "2.0" for line in session.lines)
    events = [
        cast("JsonObject", json.loads(line))
        for line in stderr.splitlines()
        if line.startswith(b"{")
    ]
    finished = [event for event in events if event["event"] == "mcp.tool.finished"]
    assert code == 0
    assert [event["outcome"] for event in finished] == [
        "ok",
        "api_error",
        "document_error",
    ]
    assert token.encode() not in stderr


@pytest.mark.asyncio
async def test_mcp_uses_only_its_own_credential(
    database: Database,
    preparer: DeploymentPreparer,
    api_capture: Capture,
    tmp_path: Path,
) -> None:
    cli_token = await bearer(database, NAMESPACE)
    other_token = await bearer(database, "other")
    app = application(
        database, api_capture, deployment_services(database, preparer, HARNESS)
    )
    graph = written(tmp_path, "skeleton.ir.json", skeleton_ir())

    async with running_app(app) as origin:
        unconfigured = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-m",
            "kora",
            "mcp",
            "--server",
            "authoring",
            env={
                "PATH": os.environ["PATH"],
                "KORA_URL": origin,
                "KORA_TOKEN": cli_token,
            },
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        startup = await asyncio.wait_for(unconfigured.communicate(), DEADLINE_SECONDS)
        refused_origin = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-m",
            "kora",
            "mcp",
            "--server",
            "authoring",
            "--url",
            "http://localhost:9",
            env={"PATH": os.environ["PATH"], "KORA_MCP_TOKEN": other_token},
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        origin_startup = await asyncio.wait_for(
            refused_origin.communicate(), DEADLINE_SECONDS
        )
        session = await start(
            {"KORA_URL": origin, "KORA_TOKEN": cli_token, "KORA_MCP_TOKEN": other_token}
        )
        refused = await session.call(graph)
        code, _ = await session.close()

    error = cast(
        "JsonObject", cast("JsonObject", refused["structuredContent"])["error"]
    )
    assert unconfigured.returncode == 78
    assert startup[0] == b""
    assert b"KORA_MCP_TOKEN: missing" in startup[1]
    assert refused_origin.returncode == 78
    assert origin_startup[0] == b""
    assert b"--url: invalid" in origin_startup[1]
    assert refused["isError"] is True
    assert error["code"] == "API005"
    assert code == 0


@pytest.mark.asyncio
async def test_mcp_refuses_invalid_calls_and_reports_transport_failures(
    tmp_path: Path,
) -> None:
    graph = written(tmp_path, "skeleton.ir.json", skeleton_ir())
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        closed = f"http://127.0.0.1:{probe.getsockname()[1]}"
    session = await start({"KORA_URL": closed, "KORA_MCP_TOKEN": "token"})

    unknown = await session.request(
        "tools/call", {"name": "kora_trigger", "arguments": {"path": str(graph)}}
    )
    extra = await session.request(
        "tools/call",
        {"name": "kora_deploy", "arguments": {"path": str(graph), "url": closed}},
    )
    null_byte = await session.call(Path(str(tmp_path) + "/\x00.json"))
    unreachable = await session.call(graph)
    code, _ = await session.close()

    unknown_result = cast("JsonObject", unknown["result"])
    extra_result = cast("JsonObject", extra["result"])
    record = cast("JsonObject", unreachable["structuredContent"])
    assert unknown_result["isError"] is True
    assert extra_result["isError"] is True
    assert null_byte["isError"] is True
    assert unreachable["isError"] is True
    assert cast("JsonObject", record["error"])["origin"] == "client"
    assert cast("JsonObject", record["error"])["code"] == "CLT001"
    assert code == 0
