import asyncio
import json
import os
import socket
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pytest
from anyio import create_memory_object_stream
from mcp import types
from mcp.shared.message import ServerMessageMetadata, SessionMessage

from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.client.settings import ClientSettings
from kora.conformance.capture import Capture, captured
from kora.db.connection import Database
from kora.ir.json import JsonObject, parse_json_object
from kora.mcp.results import MCP_COMPONENT
from kora.mcp.stdio import create_authoring_server
from kora.models.errors import ValidationDetails
from kora.observability import LoggingSettings
from tests.api.deployment_cases import HARNESS, NAMESPACE, preparer, skeleton_ir
from tests.api.support import api_capture, application, deployment_services
from tests.cli.test_compile_command import (
    EXAMPLE,
    PRIVATE_FORMAT,
    compile_json,
    findings_from,
)
from tests.cli.test_deploy_command import closed_origin
from tests.client.support import running_app
from tests.mcp.compile_peer import GatedCompiler
from tests.plugins.authentication.support import seed_case
from tests.support.observability import FakeClock

__all__ = ["api_capture", "preparer"]

DEADLINE_SECONDS = 60.0
PROTOCOL_VERSION = "2025-11-25"
INITIALIZE: JsonObject = {
    "protocolVersion": PROTOCOL_VERSION,
    "capabilities": {},
    "clientInfo": {"name": "kora-test", "version": "1"},
}
BUSY = "A graph compilation is already in progress."


def compile_parameters(path: Path, output_format: str = "json") -> JsonObject:
    return {
        "name": "kora_compile",
        "arguments": {"path": str(path), "format": output_format},
    }


@dataclass
class StdioSession:
    process: asyncio.subprocess.Process
    lines: list[JsonObject] = field(default_factory=list[JsonObject])
    sequence: int = 0

    async def initialize(self) -> None:
        await self.request("initialize", INITIALIZE)
        await self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    async def send(self, message: JsonObject) -> None:
        stdin = self.process.stdin
        assert stdin is not None
        stdin.write(json.dumps(message).encode() + b"\n")
        await stdin.drain()

    async def begin_request(self, method: str, params: JsonObject) -> int:
        self.sequence += 1
        await self.send(
            {"jsonrpc": "2.0", "id": self.sequence, "method": method, "params": params}
        )
        return self.sequence

    async def response(self, sequence: int) -> JsonObject:
        stdout = self.process.stdout
        assert stdout is not None
        while True:
            line = await asyncio.wait_for(stdout.readline(), DEADLINE_SECONDS)
            assert line, "the server closed stdout before responding"
            message = cast("JsonObject", json.loads(line))
            self.lines.append(message)
            if message.get("id") == sequence:
                return message

    async def request(self, method: str, params: JsonObject) -> JsonObject:
        return await self.response(await self.begin_request(method, params))

    async def call(self, path: Path) -> JsonObject:
        response = await self.request(
            "tools/call", {"name": "kora_deploy", "arguments": {"path": str(path)}}
        )
        return cast("JsonObject", response["result"])

    async def compile(self, path: Path, output_format: str = "json") -> JsonObject:
        response = await self.request(
            "tools/call", compile_parameters(path, output_format)
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


async def start(environment: dict[str, str], server: str = "authoring") -> StdioSession:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-m",
        "kora",
        "mcp",
        "--server",
        server,
        env={"PATH": os.environ["PATH"], **environment},
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    session = StdioSession(process)
    await session.initialize()
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
    assert [tool["name"] for tool in tools] == ["kora_deploy", "kora_compile"]
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


@pytest.mark.asyncio
async def test_mcp_compile_matches_cli_and_recovers_after_refusal(
    tmp_path: Path,
) -> None:
    original = EXAMPLE.read_bytes()
    deep = tmp_path / "private-source.py"
    deep.write_bytes(
        original.replace(b'"checked"', b"(" * 50 + b'"checked"' + b")" * 50)
    )
    relative = Path(os.path.relpath(EXAMPLE))
    credential = "private-mcp-credential"
    session = await start(
        {
            "KORA_URL": closed_origin(),
            "KORA_MCP_TOKEN": credential,
            "KORA_LOG_LEVEL": "DEBUG",
        }
    )

    first = await session.compile(relative)
    refused = await session.compile(deep)
    format_error = await session.compile(relative, PRIVATE_FORMAT)
    recovered = await session.compile(EXAMPLE)
    code, stderr = await session.close()
    cli_success = await compile_json(EXAMPLE)
    cli_refusal = await compile_json(deep)

    assert code == cli_success.code == 0
    assert first.get("isError") is not True
    assert first == recovered
    assert first["structuredContent"] == parse_json_object(
        json.loads(cli_success.stdout)
    )
    assert refused["isError"] is True
    mcp_findings = ValidationDetails.model_validate(refused["structuredContent"])
    assert mcp_findings == findings_from(cli_refusal)
    (finding,) = mcp_findings.errors
    assert finding.code == "DSL016"
    assert "nesting limit" in finding.message
    assert format_error["isError"] is True
    assert PRIVATE_FORMAT not in json.dumps(format_error)
    assert all(response["jsonrpc"] == "2.0" for response in session.lines)
    events = [parse_json_object(json.loads(line)) for line in stderr.splitlines()]
    completed = [
        record for record in events if record.get("event") == "mcp.tool.finished"
    ]
    assert [record["tool"] for record in completed] == ["kora_compile"] * 4
    assert [record["outcome"] for record in completed] == [
        "ok",
        "invalid",
        "usage_error",
        "ok",
    ]
    assert not any(record.get("component") == "client" for record in events)
    for private in (
        str(tmp_path).encode(),
        b"private-",
        b"Summarize the repository.",
        b"printf",
    ):
        assert private not in stderr


@pytest.mark.asyncio
async def test_mcp_compile_retains_the_slot_through_cancelled_physical_work(
    tmp_path: Path,
) -> None:
    blocked = tmp_path / "blocked.py"
    blocked.write_bytes(EXAMPLE.read_bytes())
    compiler = GatedCompiler(blocked)
    settled = asyncio.Event()
    upstream, requests = create_memory_object_stream[SessionMessage | Exception]()
    responses, downstream = create_memory_object_stream[SessionMessage]()
    settings = ClientSettings(closed_origin(), "token")

    async def record_settlement() -> None:
        settled.set()

    async def send(message: JsonObject, *, cancelled_job: bool = False) -> None:
        metadata = (
            ServerMessageMetadata(on_request_unanswered=record_settlement)
            if cancelled_job
            else None
        )
        await upstream.send(
            SessionMessage(
                types.jsonrpc_message_adapter.validate_python(message), metadata
            )
        )

    async def request(
        sequence: int, method: str, params: JsonObject, *, held: bool = False
    ) -> None:
        await send(
            {"jsonrpc": "2.0", "id": sequence, "method": method, "params": params},
            cancelled_job=held,
        )

    with captured(
        MCP_COMPONENT, LoggingSettings(), "compile-test", FakeClock().now
    ) as capture:
        async with settings.client(capture.diagnostics) as client, downstream:
            server = create_authoring_server(
                client, capture.diagnostics, compile_source=compiler
            )
            serving = asyncio.create_task(
                server.run(requests, responses, server.create_initialization_options())
            )
            try:
                async with asyncio.timeout(DEADLINE_SECONDS):
                    await request(1, "initialize", INITIALIZE)
                    await downstream.receive()
                    await send(
                        {"jsonrpc": "2.0", "method": "notifications/initialized"}
                    )
                    await request(
                        2, "tools/call", compile_parameters(blocked), held=True
                    )
                    await compiler.opened.wait()
                    await send(
                        {
                            "jsonrpc": "2.0",
                            "method": "notifications/cancelled",
                            "params": {"requestId": 2},
                        }
                    )
                    await request(3, "tools/call", compile_parameters(EXAMPLE))
                    busy = (await downstream.receive()).message
                    selected_while_blocked = list(compiler.selected)
                    still_open = not compiler.closed.is_set()
                    not_settled = not settled.is_set()
                    compiler.release.set()
                    await settled.wait()
                    await request(4, "tools/call", compile_parameters(EXAMPLE))
                    reused = (await downstream.receive()).message
            finally:
                compiler.release.set()
                await upstream.aclose()
                await asyncio.wait_for(serving, DEADLINE_SECONDS)

    assert isinstance(busy, types.JSONRPCResponse)
    assert busy.id == 3
    busy_result = types.CallToolResult.model_validate(busy.result)
    assert busy_result.is_error
    assert busy_result.content == [types.TextContent(text=BUSY)]
    assert selected_while_blocked == [blocked]
    assert still_open
    assert not_settled
    assert compiler.closed.is_set()
    assert compiler.selected == [blocked, EXAMPLE]
    assert isinstance(reused, types.JSONRPCResponse)
    assert reused.id == 4
    reused_result = types.CallToolResult.model_validate(reused.result)
    assert not reused_result.is_error
    assert reused_result.structured_content is not None


@pytest.mark.asyncio
async def test_mcp_compile_joins_the_active_read_before_stdio_eof_exit() -> None:
    controller, peer = socket.socketpair()
    with controller, peer:
        controller.setblocking(False)
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tests.mcp.compile_peer",
            str(peer.fileno()),
            str(EXAMPLE),
            cwd=EXAMPLE.parents[2],
            env={"PATH": os.environ["PATH"]},
            pass_fds=(peer.fileno(),),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        peer.close()
        session = StdioSession(process)
        loop = asyncio.get_running_loop()

        try:
            async with asyncio.timeout(DEADLINE_SECONDS):
                await session.initialize()
                request = await session.begin_request(
                    "tools/call", compile_parameters(EXAMPLE)
                )
                opened = await loop.sock_recv(controller, 1)
                assert process.stdin is not None
                process.stdin.close()
                waiting_at_eof = await loop.sock_recv(controller, 1)
                await loop.sock_sendall(controller, b"R")
                closed = await loop.sock_recv(controller, 1)
                final = await session.response(request)
                code, stderr = await session.close()
        finally:
            if process.returncode is None:
                process.kill()
                await process.communicate()

    assert (opened, waiting_at_eof, closed) == (b"O", b"E", b"C")
    assert (code, stderr) == (0, b"")
    assert "result" not in final
    assert cast("JsonObject", final["error"])["message"] == "Connection closed"
