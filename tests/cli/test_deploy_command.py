import asyncio
import json
import os
import signal
import socket
import sys
from pathlib import Path
from typing import Literal

import pytest

from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.conformance.capture import Capture
from kora.db.connection import Database
from kora.ir.json import JsonObject
from kora.models.errors import ApiErrorEnvelope
from kora.models.graphs import DeployResponse
from tests.api.deployment_cases import HARNESS, NAMESPACE, preparer, skeleton_ir
from tests.api.support import api_capture, application, deployment_services
from tests.client.support import running_app
from tests.plugins.authentication.support import seed_case
from tests.support.observability import ProcessResult

__all__ = ["api_capture", "preparer"]

PROCESS_DEADLINE_SECONDS = 60.0


async def stop_cli(
    launch: asyncio.Task[asyncio.subprocess.Process],
    communication: asyncio.Task[tuple[bytes, bytes]] | None,
) -> None:
    process = await launch
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    try:
        if communication is None:
            await process.communicate()
        else:
            await communication
    finally:
        await process.wait()


async def collect_cli(
    launch: asyncio.Task[asyncio.subprocess.Process], timeout: float
) -> ProcessResult:
    communication: asyncio.Task[tuple[bytes, bytes]] | None = None
    try:
        process = await asyncio.shield(launch)
        communication = asyncio.create_task(process.communicate())
        async with asyncio.timeout(timeout):
            stdout, stderr = await asyncio.shield(communication)
    except BaseException:
        cleanup = asyncio.create_task(stop_cli(launch, communication))
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                continue
        cleanup.result()
        raise
    assert process.returncode is not None
    return ProcessResult(process.returncode, stdout, stderr)


async def kora(arguments: list[str], environment: dict[str, str]) -> ProcessResult:
    launch = asyncio.create_task(
        asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-m",
            "kora",
            *arguments,
            env={"PATH": os.environ["PATH"], **environment},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    )
    return await collect_cli(launch, PROCESS_DEADLINE_SECONDS)


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["timeout", "cancel"])
async def test_cli_collection_reaps_the_child_on_interruption(
    interruption: Literal["timeout", "cancel"],
) -> None:
    launch = asyncio.create_task(
        asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-c",
            "import signal; signal.alarm(10); signal.pause()",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    )
    process = await launch
    collecting = asyncio.Event()

    async def collect() -> ProcessResult:
        collecting.set()
        return await collect_cli(launch, PROCESS_DEADLINE_SECONDS)

    try:
        if interruption == "timeout":
            with pytest.raises(TimeoutError):
                await collect_cli(launch, 0)
        else:
            collection = asyncio.create_task(collect())
            await asyncio.wait_for(collecting.wait(), PROCESS_DEADLINE_SECONDS)
            collection.cancel("original cancellation")
            asyncio.get_running_loop().call_soon(
                collection.cancel, "cancellation during cleanup"
            )
            with pytest.raises(asyncio.CancelledError, match="original cancellation"):
                await collection
        returncode = process.returncode
    finally:
        await stop_cli(launch, None)

    assert returncode == -signal.SIGKILL


def written(directory: Path, name: str, document: JsonObject) -> Path:
    path = directory / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


async def bearer(database: Database) -> str:
    case = await seed_case(database, NAMESPACE)
    authorization = case.request.authorization
    assert authorization is not None
    return authorization.removeprefix("Bearer ")


def closed_origin() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{probe.getsockname()[1]}"


@pytest.mark.asyncio
async def test_cli_deploy_without_files(
    database: Database,
    preparer: DeploymentPreparer,
    api_capture: Capture,
    tmp_path: Path,
) -> None:
    token = await bearer(database)
    app = application(
        database, api_capture, deployment_services(database, preparer, HARNESS)
    )
    graph = written(tmp_path, "skeleton.ir.json", skeleton_ir())
    refused = written(tmp_path, "refused.ir.json", skeleton_ir(harness="claude"))

    async with running_app(app) as origin:
        environment = {"KORA_URL": origin, "KORA_TOKEN": token}
        human = await kora(
            ["deploy", str(graph)], environment | {"KORA_LOG_LEVEL": "DEBUG"}
        )
        machine = await kora(["deploy", str(graph), "--json"], environment)
        refusal = await kora(["deploy", str(refused), "--json"], environment)
        plain_refusal = await kora(["deploy", str(refused)], environment)

    response = DeployResponse.model_validate_json(machine.stdout)
    envelope = ApiErrorEnvelope.model_validate_json(refusal.stdout)
    assert (human.code, machine.code, refusal.code) == (0, 0, 1)
    assert human.stdout.decode().startswith(
        f"published graph version {response.graph_version_id} (number 1"
    )
    records = {str(record["event"]): record for record in human.records()}
    assert records["cli.deploy.finished"]["outcome"] == "ok"
    assert str(records["client.request.completed"]["request_id"]).startswith("req_")
    assert response.reused
    assert envelope.error.code == "API003"
    assert plain_refusal.code == 1
    assert (
        ApiErrorEnvelope.model_validate_json(plain_refusal.stdout).error.code
        == "API003"
    )
    assert b"KORA_TOKEN" not in human.stderr + machine.stderr + refusal.stderr
    assert token.encode() not in human.stderr + machine.stderr + refusal.stderr


@pytest.mark.asyncio
async def test_cli_deploy_refuses_locally_before_sending(tmp_path: Path) -> None:
    graph = written(tmp_path, "skeleton.ir.json", skeleton_ir())
    malformed = tmp_path / "malformed.ir.json"
    malformed.write_text('{"graph": {"id": "skeleton"}, "graph": {}}', encoding="utf-8")
    origin = closed_origin()

    unconfigured = await kora(["deploy", str(graph)], {"KORA_URL": origin})
    invalid = await kora(
        ["deploy", str(malformed)], {"KORA_URL": origin, "KORA_TOKEN": "token"}
    )
    unreachable = await kora(
        ["deploy", str(graph), "--json"], {"KORA_URL": origin, "KORA_TOKEN": "token"}
    )
    refused_origin = await kora(
        ["deploy", str(graph)],
        {"KORA_URL": "http://localhost:9", "KORA_TOKEN": "token"},
    )
    levelled = await kora(
        ["deploy", str(graph)],
        {
            "KORA_URL": origin,
            "KORA_TOKEN": "token",
            "KORA_LOG_COMPONENT_LEVELS": "client=DEBUG,mcp=DEBUG",
        },
    )
    quiet = await kora(
        ["deploy", str(graph)], {"KORA_URL": origin, "KORA_TOKEN": "token"}
    )

    record = json.loads(unreachable.stdout)
    assert (unconfigured.code, unconfigured.stdout) == (78, b"")
    assert b"KORA_TOKEN: missing" in unconfigured.stderr
    assert (invalid.code, invalid.stdout) == (2, b"")
    assert b"not_json" in invalid.stderr
    assert unreachable.code == 1
    assert record["error"]["origin"] == "client"
    assert record["error"]["code"] == "CLT001"
    assert (quiet.code, quiet.stdout) == (1, b"")
    assert (refused_origin.code, refused_origin.stdout) == (78, b"")
    assert b"KORA_URL: invalid" in refused_origin.stderr
    assert levelled.code == 1
    assert b"CLT001" in quiet.stderr
