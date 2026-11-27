import asyncio
import csv
import json
import os
import selectors
import shutil
import signal
import socket
import sqlite3
import ssl
import subprocess
import sys
import time
from collections.abc import AsyncGenerator, Awaitable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

import httpx
import pytest
from sqlalchemy import select, text, update

from kora.api.routes import RouteName
from kora.client import KoraClient
from kora.client.errors import ClientError, Failure, Outcome
from kora.db.connection import Database, DatabaseSettings
from kora.db.tables import api_token, attempt, graph_version
from kora.db.tokens import revoke_token
from kora.engine.start_records import AcceptedStart, decode_start_receipt
from kora.ir.json import JsonObject, JsonValue, parse_json_object
from kora.models.errors import ApiErrorEnvelope
from kora.models.graphs import DeployResponse
from kora.models.run_events import RunEvent, RunEventPage
from kora.models.runs import AcceptedRun
from tests.api.deployment_cases import GRAPH, NAMESPACE, skeleton_ir
from tests.api.run_events_codegen import verify_run_events_codegen
from tests.api.serving_lifecycle_child import SLOW_RESPONSE_BYTES, serving_event
from tests.api.test_linux_serving_barrier import native_probe_required_here
from tests.api.test_worker_registration import registry_state
from tests.cli.test_deploy_command import collect_cli
from tests.client.support import (
    Reader,
    TlsMaterial,
    Writer,
    peer,
    tls_pair,
    write_tls_material,
)
from tests.db.seed import START, Seeder
from tests.db.test_run_event_reads import SYSTEM, EventScope, seed_event, seed_run
from tests.engine.graph_output_cases import child as ir_child
from tests.engine.start_cases import start_rows
from tests.mcp.test_stdio_deploy import StdioSession
from tests.plugins.authentication.support import seed_case
from tests.support.databases import copy_sqlite_template, marker_only_sqlite
from tests.support.observability import ProcessResult
from tests.support.provider_tooling import (
    copy_core_wheel,
    core_wheel_build_stop,
    core_wheel_workspace,
    create_virtual_environment,
    prepare_wheelhouse,
    run_core_wheel_build,
    run_setup,
)

WEB_ROOT = Path(__file__).resolve().parents[2] / "web"
LOOPBACK = "127.0.0.1"
SCHEMA = "/api/v1/openapi.json"
SERVE_TIMEOUT = 20
DRAIN_RECEIPT_TIMEOUT = 8
PRIVATE_MARKER = "private-installed-api-marker"
PRIVATE_PARAMETER = "private-installed-parameter-value"
DOMAIN_HARNESS = "KORA_API_HARNESS"
HTTPS_ORIGIN = "KORA_API_HTTPS_ORIGIN"
TLS_CERT_FILE = "KORA_API_TLS_CERT_FILE"
TLS_KEY_FILE = "KORA_API_TLS_KEY_FILE"
CA_BUNDLE = "KORA_CA_BUNDLE"
TLS_EVENTS_NAMESPACE = "installed_tls_events"
DEPLOY_PATH = f"/api/v1/namespaces/{NAMESPACE}/graphs/{GRAPH}/versions"
TRIGGER_PATH = f"/api/v1/namespaces/{NAMESPACE}/graphs/{GRAPH}/runs"
WORKER_ROUTES = ("/api/v1/workers", "/api/v1/workers/staged-worker/claims")

BUILD_TREE = """
import os
import signal
import subprocess
import sys
from pathlib import Path

child = subprocess.Popen(
    [sys.executable, '-c', 'import os, signal; os.write(1, b"r"); signal.pause()'],
    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
)
assert child.stdout is not None
assert child.stdout.read(1) == b'r'
child.stdout.close()

def settle(number: int, frame: object) -> None:
    child.wait(timeout=1)
    raise SystemExit(0)

signal.signal(signal.SIGTERM, settle)
Path('kora-partial.whl').write_bytes(b'partial')
with open(sys.argv[1], 'w') as ready:
    ready.write(f'{os.getpid()} {child.pid}')
if sys.argv[2] == 'failure':
    sys.stdout.write('core build stdout\\n')
    sys.stderr.write('core build stderr\\n')
    raise SystemExit(23)
signal.pause()
"""


def observe_build_tree(
    descriptor: int, workspace: Path, interruption: signal.Signals | None
) -> tuple[int, ...]:
    with selectors.DefaultSelector() as selector:
        selector.register(descriptor, selectors.EVENT_READ)
        assert selector.select(6)
        pids = tuple(int(value) for value in os.read(descriptor, 128).split())
    if interruption is not None:
        assert (workspace / "kora-partial.whl").is_file()
        os.kill(os.getpid(), interruption)
    return pids


def installed_core(
    core_wheel: Path,
    root: Path,
    *,
    serve: bool = False,
    postgres: bool = False,
    mcp: bool = False,
    copy_payload: bool = False,
) -> Path:
    wheelhouse = root / "dependencies"
    wheelhouse.mkdir()
    prepare_wheelhouse(wheelhouse, serve=serve, postgres=postgres, mcp=mcp)
    wheel = copy_core_wheel(core_wheel, wheelhouse)
    python = create_virtual_environment(root / "environment")
    extras = ",".join(
        name
        for name, enabled in (("serve", serve), ("postgres", postgres), ("mcp", mcp))
        if enabled
    )
    file_isolation = ["--link-mode", "copy"] if copy_payload else []
    run_setup(
        [
            "uv",
            "pip",
            "install",
            *file_isolation,
            "--offline",
            "--no-index",
            "--find-links",
            str(wheelhouse),
            "--python",
            str(python),
            f"{wheel}[{extras}]" if extras else str(wheel),
        ]
    )
    return python


@pytest.fixture(scope="module")
def bare_installation(
    core_wheel: Path, tmp_path_factory: pytest.TempPathFactory
) -> Path:
    return installed_core(core_wheel, tmp_path_factory.mktemp("installed-api-bare"))


@pytest.fixture(scope="module")
def serving_installation(
    core_wheel: Path, tmp_path_factory: pytest.TempPathFactory
) -> Path:
    return installed_core(
        core_wheel, tmp_path_factory.mktemp("installed-api-serving"), serve=True
    )


@pytest.fixture(scope="module")
def https_installation(
    core_wheel: Path, tmp_path_factory: pytest.TempPathFactory
) -> Path:
    return installed_core(
        core_wheel, tmp_path_factory.mktemp("installed-api-https"), serve=True, mcp=True
    )


@pytest.fixture(scope="module")
def postgres_serving_installation(
    core_wheel: Path, tmp_path_factory: pytest.TempPathFactory
) -> Path:
    return installed_core(
        core_wheel,
        tmp_path_factory.mktemp("installed-api-postgres-serving"),
        serve=True,
        postgres=True,
    )


def test_installed_api_application_uses_only_runtime_dependencies(
    core_wheel: Path, bare_installation: Path, tmp_path: Path
) -> None:
    environment = bare_installation.parent.parent
    outside = tmp_path / "outside"
    outside.mkdir()
    probe = outside / "probe.py"
    shutil.copyfile(Path(__file__).with_name("installed_probe.py"), probe)

    process = subprocess.run(
        [str(bare_installation), "-I", str(probe), str(environment)],
        cwd=outside,
        env={"PATH": os.defpath},
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert process.returncode == 0, process.stderr
    assert process.stdout == b"installed-api-ok\n"
    assert process.stderr == b""
    assert len(list(core_wheel.parent.glob("kora-*.tar.gz"))) == 1


def test_installed_core_refuses_mcp_without_its_extra(
    bare_installation: Path, tmp_path: Path
) -> None:
    process = subprocess.run(
        [str(bare_installation), "-I", "-m", "kora", "mcp", "--server", "authoring"],
        cwd=tmp_path,
        env={
            "PATH": os.defpath,
            "KORA_URL": "http://127.0.0.1:9",
            "KORA_MCP_TOKEN": "token",
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert process.returncode == 78
    assert process.stdout == b""
    assert b"requires the kora[mcp] extra" in process.stderr


def api_environment(database_url: str | None) -> dict[str, str]:
    environment = {
        "PATH": os.defpath,
        "KORA_LOG_LEVEL": "DEBUG",
        "KORA_LOG_FORMAT": "json",
    }
    if database_url is not None:
        environment["KORA_DATABASE_URL"] = database_url
    return environment


def api_command(python: Path, port: int) -> list[str]:
    return [str(python.with_name("kora")), "api", "--port", str(port)]


def api_startup_result(
    python: Path,
    directory: Path,
    database_url: str | None,
    port: int,
    *,
    ssl_keylog: Path | None = None,
    harness: str | None = None,
) -> subprocess.CompletedProcess[bytes]:
    environment = api_environment(database_url)
    if harness is not None:
        environment[DOMAIN_HARNESS] = harness
    if ssl_keylog is not None:
        environment["SSLKEYLOGFILE"] = str(ssl_keylog)
    return subprocess.run(
        api_command(python, port),
        cwd=directory,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=SERVE_TIMEOUT,
    )


def diagnostics(stderr: bytes) -> list[JsonObject]:
    return [
        parse_json_object(json.loads(line))
        for line in stderr.splitlines()
        if line.startswith(b"{")
    ]


def refusal_reason(result: subprocess.CompletedProcess[bytes]) -> str:
    records = diagnostics(result.stderr)
    reasons = [
        record["reason"]
        for record in records
        if record["event"] == "api.serving_refused"
    ]
    assert len(reasons) == 1, result.stderr
    reason = reasons[0]
    assert isinstance(reason, str)
    assert all(record["event"] != "api.serving_bound" for record in records)
    return reason


def socket_port(bound_socket: socket.socket) -> int:
    return int(bound_socket.getsockname()[1])


def unused_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((LOOPBACK, 0))
        return socket_port(probe)


async def read_bound_port(process: asyncio.subprocess.Process) -> tuple[int, bytes]:
    assert process.stderr is not None
    received = bytearray()
    async with asyncio.timeout(SERVE_TIMEOUT):
        while line := await process.stderr.readline():
            received.extend(line)
            for record in diagnostics(line):
                if record["event"] == "api.serving_bound":
                    port = record["port"]
                    if type(port) is not int or not 0 < port <= 65535:
                        raise AssertionError(
                            "The installed API reported an invalid port."
                        )
                    return port, bytes(received)
    raise AssertionError("The installed API did not report its listener.")


async def terminate_installed_api(
    process: asyncio.subprocess.Process,
) -> tuple[bytes, bytes]:
    if process.returncode is None:
        process.send_signal(signal.SIGTERM)
    try:
        async with asyncio.timeout(SERVE_TIMEOUT):
            return await process.communicate()
    except TimeoutError:
        process.kill()
        async with asyncio.timeout(SERVE_TIMEOUT):
            await process.communicate()
        raise


async def read_raw_head(port: int, authorization: str | None = None) -> bytes:
    fields = [
        f"HEAD {SCHEMA} HTTP/1.1",
        f"Host: {LOOPBACK}:{port}",
        "Connection: close",
    ]
    if authorization is not None:
        fields.append(f"Authorization: {authorization}")
    request = "\r\n".join([*fields, "", ""]).encode("ascii")
    async with asyncio.timeout(SERVE_TIMEOUT):
        reader, writer = await asyncio.open_connection(LOOPBACK, port)
        try:
            writer.write(request)
            await writer.drain()
            return await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()


def head_headers(response: bytes, status: int) -> dict[bytes, bytes]:
    header_block, delimiter, trailing = response.partition(b"\r\n\r\n")
    assert delimiter == b"\r\n\r\n"
    assert trailing == b""
    status_line, *raw_headers = header_block.split(b"\r\n")
    assert status_line.startswith(f"HTTP/1.1 {status} ".encode("ascii"))
    headers: dict[bytes, bytes] = {}
    for line in raw_headers:
        name, separator, value = line.partition(b":")
        assert separator == b":"
        headers[name.lower()] = value.strip()
    assert b"content-length" not in headers
    assert headers.get(b"transfer-encoding") in (None, b"chunked")
    assert headers[b"x-request-id"]
    return headers


async def check_inactive_worker_routes(
    settings: DatabaseSettings, responses: list[httpx.Response]
) -> None:
    database = Database(settings)
    try:
        registry = await registry_state(database)
    finally:
        await database.close()

    for response in responses:
        assert response.status_code == 404
        error = ApiErrorEnvelope.model_validate_json(response.content).error
        assert error.code == "API001"
        assert error.request_id == response.headers["x-request-id"]
    assert all(not rows for rows in registry.values())


def assert_worker_startup_absent(stderr: bytes) -> None:
    events = diagnostics(stderr)
    assert all(
        event["event"]
        not in (
            "engine.admission_initialization.finished",
            "engine.claim_expiry_recovery.finished",
        )
        for event in events
    )


@pytest.fixture(scope="module")
def run_events_bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("run-events-typescript")
    pnpm = shutil.which("pnpm")
    if pnpm is None:
        raise ValueError("The locked pnpm executable is required.")
    result = subprocess.run(
        [
            pnpm,
            "exec",
            "vite",
            "build",
            "--ssr",
            "tests/browser/run-events-api.ts",
            "--outDir",
            str(directory),
            "--emptyOutDir",
        ],
        cwd=WEB_ROOT,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert (directory / "run-events-api.js").is_file()
    return directory


async def run_events_node(
    bundle: Path,
    data: JsonObject,
    *,
    proxy: str | None = None,
    proxy_control: bool = False,
) -> ProcessResult:
    selected_node = shutil.which("node")
    if selected_node is None:
        raise ValueError("The locked Node executable is required.")
    node = str(Path(selected_node).resolve())
    launcher = WEB_ROOT / "scripts" / "run-events-node.sh"
    if proxy_control:
        arguments = [node, str(bundle / "run-events-api.js")]
    else:
        arguments = [str(launcher), node, str(bundle)]
    environment = (
        {}
        if proxy is None
        else {"NODE_USE_ENV_PROXY": "1", "HTTP_PROXY": proxy, "NO_PROXY": ""}
    )
    if proxy is not None and not proxy_control:
        environment["NODE_OPTIONS"] = "--require /nonexistent-kora-preload.cjs"
    process = await asyncio.create_subprocess_exec(
        *arguments,
        cwd=bundle,
        env=environment,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        async with asyncio.timeout(24):
            stdout, stderr = await process.communicate(json.dumps(data).encode("utf-8"))
    finally:
        if process.returncode is None:
            os.killpg(process.pid, signal.SIGKILL)
            async with asyncio.timeout(3):
                await process.communicate()
    assert process.returncode is not None
    return ProcessResult(process.returncode, stdout, stderr)


def assert_private_channels(
    stdout: bytes, stderr: bytes, markers: tuple[bytes, ...]
) -> None:
    private_content_seen = any(
        marker in stdout or marker in stderr for marker in markers
    )
    assert not private_content_seen, "A process channel exposed private test input."


def assert_typescript_run_ok(result: ProcessResult, markers: tuple[bytes, ...]) -> None:
    assert_private_channels(result.stdout, result.stderr, markers)
    completed = result.code == 0
    expected_output = result.stdout == b"run-events-api-ok\n" and not result.stderr
    assert completed, "The TypeScript verification process did not pass."
    assert expected_output, (
        "The TypeScript verification process reported unexpected output."
    )


@pytest.mark.asyncio
async def test_run_events_client_wire_contract(run_events_bundle: Path) -> None:
    markers = (
        b"private-run-events-wire-credential",
        b"private-run-events-wire-content",
    )

    result = await run_events_node(run_events_bundle, {"mode": "wire"})

    assert_typescript_run_ok(result, markers)


@pytest.mark.asyncio
async def test_run_events_codegen_matches_installed_schema(
    serving_installation: Path, tmp_path: Path, sqlite_template: Path
) -> None:
    settings = copy_sqlite_template(
        sqlite_template, tmp_path / "typescript-schema.sqlite"
    )
    database = Database(settings)
    try:
        case = await seed_case(database, "typescript_schema")
    finally:
        await database.close()
    authorization = case.request.authorization
    assert authorization is not None

    process = await asyncio.create_subprocess_exec(
        *api_command(serving_installation, 0),
        cwd=tmp_path,
        env=api_environment(str(settings.url)),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        port, early = await read_bound_port(process)
        async with httpx.AsyncClient(
            base_url=f"http://{LOOPBACK}:{port}", trust_env=False, timeout=10
        ) as client:
            response = await client.get(
                SCHEMA, headers={"Authorization": authorization}
            )
    finally:
        stdout, stderr = await terminate_installed_api(process)

    markers = (authorization.removeprefix("Bearer ").encode(),)
    assert_private_channels(stdout, early + stderr, markers)
    assert_private_channels(response.content, b"", markers)
    expected_schema = response.status_code == 200
    installed_api_passed = process.returncode == 0 and not stdout
    assert expected_schema, "The authenticated installed schema was unavailable."
    assert installed_api_passed, "The installed API reported unexpected process output."
    verify_run_events_codegen(response.content, tmp_path)


@pytest.mark.asyncio
async def test_installed_sqlite_typescript_client_reads_run_events(
    serving_installation: Path,
    run_events_bundle: Path,
    tmp_path: Path,
    sqlite_template: Path,
) -> None:
    settings = copy_sqlite_template(
        sqlite_template, tmp_path / "typescript-events.sqlite"
    )
    namespace = "typescript_history"
    marker = "private-typescript-installed-event-body"
    database = Database(settings)
    try:
        own = await seed_case(database, "typescript_viewer")
        other = await seed_case(database, "typescript_other")
        async with database.transaction() as connection:
            scope = await seed_run(connection, namespace)
            await connection.execute(
                update(api_token)
                .where(api_token.c.id == own.principal.token_id)
                .values(
                    namespaces=[
                        {"namespace_id": scope.namespace_id, "transcripts": True}
                    ]
                )
            )
            event_ids = [
                "event_"
                + await seed_event(
                    connection, scope, SYSTEM, 0, payload={"marker": marker}
                )
                for _ in range(2)
            ]
    finally:
        await database.close()
    credential = own.request.authorization
    foreign = other.request.authorization
    assert credential is not None
    assert foreign is not None
    markers = (
        credential.removeprefix("Bearer ").encode(),
        foreign.removeprefix("Bearer ").encode(),
        marker.encode(),
    )
    selector = "run_" + scope.run_id
    connections = 0

    def proxy_response(
        _reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        nonlocal connections
        connections += 1
        writer.write(
            b"HTTP/1.1 502 Bad Gateway\r\n"
            b"Content-Length: 0\r\nConnection: close\r\n\r\n"
        )
        writer.close()

    process = await asyncio.create_subprocess_exec(
        *api_command(serving_installation, 0),
        cwd=tmp_path,
        env=api_environment(str(settings.url)),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        port, early = await read_bound_port(process)
        origin = f"http://{LOOPBACK}:{port}"
        async with KoraClient(origin, credential.removeprefix("Bearer ")) as canonical:
            first = (
                await canonical.list_run_events(namespace, selector, limit="1")
            ).response
            assert first.next_cursor is not None
            second = (
                await canonical.list_run_events(
                    namespace, selector, limit="1", cursor=first.next_cursor
                )
            ).response
        ordered: list[JsonValue] = [
            event.id for page in (first, second) for event in page.items
        ]
        payload: JsonObject = {
            "mode": "installed",
            "origin": origin,
            "namespace": namespace,
            "run": selector,
        }
        proxy = await asyncio.start_server(proxy_response, LOOPBACK, 0)
        async with proxy:
            [sock] = proxy.sockets
            proxy_url = f"http://{LOOPBACK}:{sock.getsockname()[1]}"
            control = await run_events_node(
                run_events_bundle,
                {"mode": "proxy-probe", "origin": origin},
                proxy=proxy_url,
                proxy_control=True,
            )
            proxied_connections = connections
            accepted = await run_events_node(
                run_events_bundle,
                {**payload, "authorization": credential, "expected": ordered},
                proxy=proxy_url,
            )
            direct_connections = connections
        denied = await run_events_node(
            run_events_bundle,
            {**payload, "authorization": foreign, "expected": "API005"},
        )
        authority = Database(settings)
        try:
            async with authority.transaction() as connection:
                await revoke_token(connection, own.principal.token_id)
        finally:
            await authority.close()
        revoked = await run_events_node(
            run_events_bundle,
            {**payload, "authorization": credential, "expected": "API009"},
        )
    finally:
        stdout, stderr = await terminate_installed_api(process)

    assert ordered == sorted(event_ids, reverse=True)
    assert second.next_cursor is None
    assert proxied_connections > 0
    assert direct_connections == proxied_connections
    for result in (control, accepted, denied, revoked):
        assert_typescript_run_ok(result, markers)
    assert_private_channels(stdout, early + stderr, markers)
    installed_api_passed = process.returncode == 0 and not stdout
    assert installed_api_passed, "The installed API reported unexpected process output."


@pytest.mark.asyncio
async def test_installed_sqlite_api_serves_health_and_authenticated_schema(
    serving_installation: Path, tmp_path: Path, sqlite_template: Path
) -> None:
    settings = copy_sqlite_template(sqlite_template, tmp_path / "prepared.sqlite")
    database = Database(settings)
    try:
        personal = await seed_case(database, "installed-personal")
        service = await seed_case(database, "installed-service", "service")
    finally:
        await database.close()
    personal_token = personal.request.authorization
    service_token = service.request.authorization
    assert personal_token is not None
    assert service_token is not None
    database_url = settings.url.render_as_string(hide_password=False)

    process = await asyncio.create_subprocess_exec(
        *api_command(serving_installation, 0),
        cwd=tmp_path,
        env=api_environment(database_url),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        port, early = await read_bound_port(process)
        async with httpx.AsyncClient(
            base_url=f"http://{LOOPBACK}:{port}", trust_env=False, timeout=10
        ) as client:
            health = await client.get("/healthz")
            missing = await client.get(SCHEMA)
            invalid = await client.get(
                SCHEMA, headers={"Authorization": f"Bearer {PRIVATE_MARKER}"}
            )
            denied = await client.get(SCHEMA, headers={"Authorization": service_token})
            schema = await client.get(SCHEMA, headers={"Authorization": personal_token})
            disabled = [
                await client.post(
                    path, headers={"Authorization": personal_token}, json={}
                )
                for path in (DEPLOY_PATH, TRIGGER_PATH)
            ]
            disabled_workers = [
                await client.post(path, json={}) for path in WORKER_ROUTES
            ]
            await check_inactive_worker_routes(settings, disabled_workers)
        head_missing = await read_raw_head(port)
        head_denied = await read_raw_head(port, service_token)
        head_wrong_method = await read_raw_head(port, personal_token)
    finally:
        stdout, trailing = await terminate_installed_api(process)

    stderr = early + trailing
    assert_worker_startup_absent(stderr)
    assert process.returncode == 0, stderr
    assert stdout == b""
    assert health.status_code == 200
    assert health.content == b'{"status":"ok"}'
    for unauthenticated in (missing, invalid):
        assert unauthenticated.status_code == 401
        assert unauthenticated.headers["www-authenticate"] == "Bearer"
        assert "allow" not in unauthenticated.headers
        body = ApiErrorEnvelope.model_validate_json(unauthenticated.content)
        assert body.error.code.value == "API009"
        assert body.error.request_id == unauthenticated.headers["x-request-id"]
    assert PRIVATE_MARKER not in invalid.text
    assert denied.status_code == 403
    assert "allow" not in denied.headers
    denied_body = ApiErrorEnvelope.model_validate_json(denied.content)
    assert denied_body.error.code.value == "API005"
    assert denied_body.error.request_id == denied.headers["x-request-id"]
    assert schema.status_code == 200
    definition = parse_json_object(schema.json())
    assert definition["openapi"] == "3.1.0"
    assert isinstance(definition["paths"], dict)
    assert set(definition["paths"]) == {"/healthz", RouteName.RUN_EVENTS.value}
    assert "servers" not in definition
    for response in disabled:
        assert response.status_code == 404
        error = ApiErrorEnvelope.model_validate_json(response.content)
        assert error.error.code == "API001"
        assert error.error.request_id == response.headers["x-request-id"]
    request_ids = {
        item.headers["x-request-id"]
        for item in (health, missing, invalid, denied, schema)
    }
    missing_headers = head_headers(head_missing, 401)
    denied_headers = head_headers(head_denied, 403)
    wrong_method_headers = head_headers(head_wrong_method, 405)
    assert missing_headers[b"www-authenticate"] == b"Bearer"
    assert b"allow" not in missing_headers
    assert b"www-authenticate" not in denied_headers
    assert b"allow" not in denied_headers
    assert b"www-authenticate" not in wrong_method_headers
    assert wrong_method_headers[b"allow"] == b"GET"
    request_ids.update(
        headers[b"x-request-id"].decode("ascii")
        for headers in (missing_headers, denied_headers, wrong_method_headers)
    )
    assert len(request_ids) == 8
    records = diagnostics(stderr)
    assert [record["event"] for record in records].count("api.serving_stopped") == 1
    assert all(record["event"] != "cel.binding.loaded" for record in records)
    assert all(record.get("provider") != "claude" for record in records)
    assert any(
        record["event"] == "protocols.loading.provider_loaded"
        and record["provider"] == "static_token"
        for record in records
    )
    for private in (database_url, personal_token, service_token, PRIVATE_MARKER):
        assert private.encode() not in stderr
    assert b"Uvicorn running" not in stderr
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(2)
        with pytest.raises(ConnectionRefusedError):
            probe.connect((LOOPBACK, port))


async def installed_cli(
    python: Path, directory: Path, environment: dict[str, str], arguments: list[str]
) -> ProcessResult:
    launch = asyncio.create_task(
        asyncio.create_subprocess_exec(
            str(python.with_name("kora")),
            *arguments,
            cwd=directory,
            env={"PATH": os.defpath, **environment},
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    )
    return await collect_cli(launch, SERVE_TIMEOUT)


def https_environment(material: TlsMaterial, port: int) -> dict[str, str]:
    return {
        HTTPS_ORIGIN: f"https://{LOOPBACK}:{port}",
        TLS_CERT_FILE: str(material.certificate),
        TLS_KEY_FILE: str(material.key),
    }


def https_client_environment(
    origin: str, credential: str, *, bundle: Path | None = None
) -> dict[str, str]:
    return {
        **api_environment(None),
        "KORA_URL": origin,
        "KORA_NAMESPACE": TLS_EVENTS_NAMESPACE,
        "KORA_TOKEN": credential,
        **({CA_BUNDLE: str(bundle)} if bundle is not None else {}),
    }


async def seed_installed_run_events(
    settings: DatabaseSettings,
) -> tuple[str, tuple[RunEvent, RunEvent]]:
    database = Database(settings)
    try:
        personal = await seed_case(database, TLS_EVENTS_NAMESPACE)
        authorization = personal.request.authorization
        assert authorization is not None
        namespace_id = personal.principal.namespaces[0].namespace_id
        actor: JsonObject = {
            "kind": "user",
            "owner": personal.principal.owner,
            "token_id": personal.principal.token_id,
        }
        async with database.transaction() as writer:
            seed = Seeder(writer)
            graph_id = await seed.graph(namespace_id)
            version_id = await seed.graph_version(graph_id)
            run_id = await seed.run(namespace_id, graph_id, version_id)
            node_id = await seed.node(run_id, "installed")
            attempt_id = await seed.attempt(node_id)
            scope = EventScope(namespace_id, run_id, node_id, attempt_id)
            older = await seed_event(
                writer,
                scope,
                actor,
                0,
                event_id="0" * 25 + "1",
                payload={"private": PRIVATE_MARKER},
                kind="run.created",
            )
            newer = await seed_event(
                writer,
                scope,
                SYSTEM,
                0,
                event_id="0" * 25 + "2",
                payload={"private": PRIVATE_MARKER},
            )
    finally:
        await database.close()
    expected = {
        "run_id": f"run_{run_id}",
        "node_instance_id": f"node_{node_id}",
        "attempt_id": f"att_{attempt_id}",
        "at": START,
    }
    return authorization.removeprefix("Bearer "), (
        RunEvent.model_validate(
            {
                **expected,
                "id": f"event_{newer}",
                "kind": "run.succeeded",
                "actor": SYSTEM,
            }
        ),
        RunEvent.model_validate(
            {**expected, "id": f"event_{older}", "kind": "run.created", "actor": actor}
        ),
    )


def event_arguments(run_id: str, cursor: str | None) -> list[str]:
    return [
        "events",
        run_id,
        "--json",
        "--limit",
        "1",
        *(["--cursor", cursor] if cursor is not None else []),
    ]


@asynccontextmanager
async def installed_operating_mcp(
    python: Path, directory: Path, environment: dict[str, str]
) -> AsyncGenerator[StdioSession]:
    process = await asyncio.create_subprocess_exec(
        str(python.with_name("kora")),
        "mcp",
        "--server",
        "operating",
        cwd=directory,
        env={
            **environment,
            "KORA_MCP_TOKEN": environment["KORA_TOKEN"],
            "KORA_TOKEN": PRIVATE_MARKER,
        },
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    session = StdioSession(process)
    try:
        await session.initialize()
        yield session
    finally:
        try:
            async with asyncio.timeout(SERVE_TIMEOUT):
                code, stderr = await session.close()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        assert code == 0, stderr
        assert b"process.failed" not in stderr
        for name in ("KORA_TOKEN", "KORA_URL", CA_BUNDLE):
            if name in environment:
                assert environment[name].encode() not in stderr
        assert PRIVATE_MARKER.encode() not in stderr


async def installed_mcp_event_result(
    session: StdioSession, run_id: str, cursor: str | None
) -> JsonObject:
    arguments: JsonObject = {
        "mode": "events",
        "namespace": TLS_EVENTS_NAMESPACE,
        "run_id": run_id,
        "limit": 1,
    }
    if cursor is not None:
        arguments["cursor"] = cursor
    message = await session.request(
        "tools/call", {"name": "kora_runs", "arguments": arguments}
    )
    return parse_json_object(message["result"])


async def raw_event_page(
    client: httpx.AsyncClient, run_id: str, credential: str, cursor: str | None
) -> RunEventPage:
    query = {"limit": "1"}
    if cursor is not None:
        query["cursor"] = cursor
    response = await client.get(
        f"/api/v1/namespaces/{TLS_EVENTS_NAMESPACE}/runs/{run_id}/events",
        params=query,
        headers={"Authorization": f"Bearer {credential}"},
    )
    assert response.status_code == 200, response.content
    return RunEventPage.model_validate_json(response.content)


async def cli_event_page(
    python: Path,
    directory: Path,
    environment: dict[str, str],
    run_id: str,
    cursor: str | None,
) -> RunEventPage:
    outcome = await installed_cli(
        python, directory, environment, event_arguments(run_id, cursor)
    )
    assert outcome.code == 0, outcome.stderr
    for name in ("KORA_TOKEN", "KORA_URL", CA_BUNDLE):
        assert environment[name].encode() not in outcome.stderr
    return RunEventPage.model_validate_json(outcome.stdout)


async def mcp_event_page(
    session: StdioSession, run_id: str, cursor: str | None
) -> RunEventPage:
    response = await installed_mcp_event_result(session, run_id, cursor)
    assert response["isError"] is False
    return RunEventPage.model_validate(response["structuredContent"])


@dataclass(frozen=True)
class EventObservation:
    began: datetime
    page: RunEventPage
    ended: datetime


async def observed_page(request: Awaitable[RunEventPage]) -> EventObservation:
    began = datetime.now(UTC)
    page = await request
    return EventObservation(began, page, datetime.now(UTC))


@asynccontextmanager
async def pending_tls_handshake(port: int, authority: Path) -> AsyncGenerator[Reader]:
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(authority)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    client = context.wrap_bio(incoming, outgoing, server_hostname=LOOPBACK)
    with pytest.raises(ssl.SSLWantReadError):
        client.do_handshake()

    async with asyncio.timeout(SERVE_TIMEOUT):
        reader, writer = await asyncio.open_connection(LOOPBACK, port)
    try:
        async with asyncio.timeout(SERVE_TIMEOUT):
            writer.write(outgoing.read())
            await writer.drain()
            header = await reader.readexactly(5)
            hello = await reader.readexactly(int.from_bytes(header[3:], "big"))
        assert header[0] == 22
        assert hello[0] == 2
        yield reader
    finally:
        writer.close()
        async with asyncio.timeout(SERVE_TIMEOUT):
            try:
                await writer.wait_closed()
            except ConnectionResetError:
                assert reader.exception() is not None


async def wait_for_tls_socket_close(reader: Reader) -> None:
    try:
        await reader.read()
    except ConnectionResetError:
        return


@pytest.mark.asyncio
async def test_installed_sqlite_https_preserves_event_surface_parity(
    https_installation: Path, tmp_path: Path, sqlite_template: Path
) -> None:
    sqlite_path = tmp_path / "private-event-database.sqlite"
    settings = copy_sqlite_template(sqlite_template, sqlite_path)
    credential, events = await seed_installed_run_events(settings)
    run_id = events[0].run_id
    material = write_tls_material(tmp_path)
    port = unused_loopback_port()
    profile = https_environment(material, port)
    origin = profile[HTTPS_ORIGIN]
    environment = https_client_environment(
        origin, credential, bundle=material.authority
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(material.authority)
    keylog = tmp_path / "private-tls-keylog"

    process = await asyncio.create_subprocess_exec(
        *api_command(https_installation, port),
        cwd=tmp_path,
        env={
            **api_environment(str(settings.url)),
            **profile,
            "SSLKEYLOGFILE": str(keylog),
        },
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        bound_port, early = await read_bound_port(process)
        assert bound_port == port
        async with (
            httpx.AsyncClient(
                base_url=origin, verify=context, trust_env=False, timeout=SERVE_TIMEOUT
            ) as client,
            installed_operating_mcp(https_installation, tmp_path, environment) as mcp,
        ):
            remote = await observed_page(
                raw_event_page(client, run_id, credential, None)
            )
            cli = await observed_page(
                cli_event_page(https_installation, tmp_path, environment, run_id, None)
            )
            tool = await observed_page(mcp_event_page(mcp, run_id, None))
            first = (remote, cli, tool)
            assert all(observation.page.next_cursor for observation in first)
            second = (
                await observed_page(
                    raw_event_page(client, run_id, credential, remote.page.next_cursor)
                ),
                await observed_page(
                    cli_event_page(
                        https_installation,
                        tmp_path,
                        environment,
                        run_id,
                        cli.page.next_cursor,
                    )
                ),
                await observed_page(mcp_event_page(mcp, run_id, tool.page.next_cursor)),
            )
        with closing(sqlite3.connect(sqlite_path, timeout=0)) as observer:
            with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                observer.execute("PRAGMA journal_mode=DELETE").close()
    finally:
        stdout, trailing = await terminate_installed_api(process)

    assert process.returncode == 0, early + trailing
    assert stdout == b""
    for observations, event in zip((first, second), events, strict=True):
        for observed in observations:
            assert observed.began <= observed.page.observed_at <= observed.ended
            assert observed.page.items == (event,)
        assert observations[0].page.observed_at < observations[1].page.observed_at
        assert observations[1].page.observed_at < observations[2].page.observed_at
        assert len({value.page.next_cursor for value in observations}) == 1
    assert all(value.page.next_cursor is None for value in second)
    stderr = early + trailing
    assert [record["event"] for record in diagnostics(stderr)].count(
        "api.serving_stopped"
    ) == 1
    for private in (*profile.values(), str(settings.url), credential, PRIVATE_MARKER):
        assert private.encode() not in stderr
    assert not keylog.exists()
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe,
        closing(sqlite3.connect(sqlite_path, timeout=0)) as observer,
    ):
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((LOOPBACK, port))
        probe.listen()
        assert observer.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
        assert observer.execute("SELECT count(*) FROM event").fetchone() == (2,)


@pytest.mark.asyncio
async def test_installed_sqlite_https_settles_accepted_handshake_before_lifespan_exit(
    https_installation: Path, tmp_path: Path, sqlite_template: Path
) -> None:
    settings = copy_sqlite_template(sqlite_template, tmp_path / "prepared.sqlite")
    fixture = Path(__file__).with_name("serving_lifecycle_child.py")
    material = write_tls_material(tmp_path)
    port = unused_loopback_port()
    release_read, release_write = os.pipe()

    try:
        try:
            process = await asyncio.create_subprocess_exec(
                str(https_installation),
                "-I",
                str(fixture),
                "hold_tls_shutdown",
                str(port),
                str(release_read),
                pass_fds=(release_read,),
                cwd=tmp_path,
                env={
                    **api_environment(str(settings.url)),
                    **https_environment(material, port),
                },
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        finally:
            os.close(release_read)
        try:
            bound, early = await read_bound_port(process)
            assert bound == port
            assert process.stdout is not None
            async with pending_tls_handshake(port, material.authority) as pending:
                process.send_signal(signal.SIGTERM)
                async with asyncio.timeout(DRAIN_RECEIPT_TIMEOUT):
                    barrier = await process.stdout.readline()
                    await wait_for_tls_socket_close(pending)
                    assert process.returncode is None
                    with pytest.raises(ConnectionRefusedError):
                        await asyncio.open_connection(LOOPBACK, port)
                    os.write(release_write, b"r")
                    stdout, trailing = await process.communicate()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
    finally:
        os.close(release_write)

    assert process.returncode == 0, early + trailing
    assert barrier == b"tls_lifespan_shutdown_received\n"
    assert stdout == b"tls_database_settled\n"
    assert [record["event"] for record in diagnostics(early + trailing)].count(
        "api.serving_stopped"
    ) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("problem", ["unknown_root", "wrong_address"])
async def test_installed_event_clients_refuse_unverified_tls(
    https_installation: Path, tmp_path: Path, problem: str
) -> None:
    context, bundle = tls_pair(
        tmp_path, address="127.0.0.2" if problem == "wrong_address" else LOOPBACK
    )
    sent: list[bytes] = []
    run_id = "run_" + "0" * 26
    credential = "private-tls-credential"
    expected = ClientError(Failure.TLS, Outcome.NOT_SENT).envelope()

    async def receive(reader: Reader, writer: Writer) -> None:
        sent.append(await reader.read())

    async with peer(receive, context) as server:
        environment = https_client_environment(
            server.url,
            credential,
            bundle=bundle if problem == "wrong_address" else None,
        )
        environment["SSL_CERT_FILE"] = str(bundle)
        cli = await installed_cli(
            https_installation, tmp_path, environment, event_arguments(run_id, None)
        )
        async with installed_operating_mcp(
            https_installation, tmp_path, environment
        ) as mcp:
            tool = await installed_mcp_event_result(mcp, run_id, None)

    assert cli.code == 1
    assert json.loads(cli.stdout) == expected
    assert tool["isError"] is True
    assert tool["structuredContent"] == expected
    assert sent == []
    for private in (server.url, str(bundle), credential):
        assert (
            private.encode() not in cli.stdout + cli.stderr + json.dumps(tool).encode()
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("bundle_kind", ["malformed", "pipe"])
async def test_installed_event_clients_refuse_invalid_ca_bundle(
    https_installation: Path, tmp_path: Path, bundle_kind: str
) -> None:
    bundle = tmp_path / "private-malformed-ca.pem"
    if bundle_kind == "pipe":
        os.mkfifo(bundle, 0o600)
    else:
        bundle.write_text(PRIVATE_MARKER, encoding="ascii")
    environment = https_client_environment(
        "https://127.0.0.1:9", "private-tls-credential", bundle=bundle
    )
    environment["KORA_MCP_TOKEN"] = environment["KORA_TOKEN"]

    cli = await installed_cli(
        https_installation,
        tmp_path,
        environment,
        event_arguments("run_" + "0" * 26, None),
    )
    mcp = await installed_cli(
        https_installation, tmp_path, environment, ["mcp", "--server", "operating"]
    )

    for result in (cli, mcp):
        assert result.code == 78
        assert result.stdout == b""
        assert (
            b"kora: invalid client configuration: KORA_CA_BUNDLE: invalid\n"
            in result.stderr
        )
        assert b"process.failed" not in result.stderr
        for private in (str(bundle), PRIVATE_MARKER, environment["KORA_TOKEN"]):
            assert private.encode() not in result.stderr


@pytest.mark.parametrize(
    "problem",
    [
        "missing_origin",
        "missing_certificate",
        "missing_key",
        "empty_origin",
        "wrong_scheme",
        "wrong_host",
        "wrong_port",
        "url_components",
        "port_zero",
        "missing_file",
        "bad_pem",
        "key_mismatch",
        "encrypted_key",
        "pipe",
        "harness",
        "worker",
        "scheduler",
    ],
)
def test_installed_sqlite_https_refuses_invalid_profile_before_bind(
    https_installation: Path, tmp_path: Path, sqlite_template: Path, problem: str
) -> None:
    settings = copy_sqlite_template(
        sqlite_template, tmp_path / "private-database.sqlite"
    )
    material = write_tls_material(
        tmp_path,
        password=PRIVATE_MARKER.encode() if problem == "encrypted_key" else None,
    )
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserved:
        reserved.bind((LOOPBACK, 0))
        reserved.listen()
        port = socket_port(reserved)
        profile = https_environment(material, port)
        invalid_material = tmp_path / "private-invalid-tls-material"
        changes: dict[str, tuple[str, str | None]] = {
            "missing_origin": (HTTPS_ORIGIN, None),
            "missing_certificate": (TLS_CERT_FILE, None),
            "missing_key": (TLS_KEY_FILE, None),
            "empty_origin": (HTTPS_ORIGIN, ""),
            "wrong_scheme": (HTTPS_ORIGIN, f"http://{LOOPBACK}:{port}"),
            "wrong_host": (HTTPS_ORIGIN, f"https://localhost:{port}"),
            "wrong_port": (HTTPS_ORIGIN, "https://127.0.0.1"),
            "url_components": (
                HTTPS_ORIGIN,
                f"https://{PRIVATE_MARKER}@{LOOPBACK}:{port}/",
            ),
            "port_zero": (HTTPS_ORIGIN, "https://127.0.0.1:0"),
            "missing_file": (TLS_CERT_FILE, str(invalid_material)),
            "bad_pem": (TLS_CERT_FILE, str(invalid_material)),
            "key_mismatch": (TLS_KEY_FILE, str(invalid_material)),
            "encrypted_key": (TLS_KEY_FILE, str(material.key)),
            "pipe": (TLS_KEY_FILE, str(invalid_material)),
            "harness": (DOMAIN_HARNESS, "claude"),
            "worker": ("KORA_API_WORKER_CLOCK_UNCERTAINTY_US", "1"),
            "scheduler": ("KORA_API_SCHEDULER", "first-claim"),
        }
        if problem == "bad_pem":
            invalid_material.write_bytes(PRIVATE_MARKER.encode())
        if problem == "key_mismatch":
            other = tmp_path / "different-authority"
            other.mkdir()
            invalid_material.write_bytes(write_tls_material(other).key.read_bytes())
        if problem == "pipe":
            os.mkfifo(invalid_material, 0o600)
        variable, value = changes[problem]
        if value is None:
            del profile[variable]
        else:
            profile[variable] = value

        result = subprocess.run(
            api_command(https_installation, 0 if problem == "port_zero" else port),
            cwd=tmp_path,
            env={**api_environment(str(settings.url)), **profile},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=SERVE_TIMEOUT,
        )

    assert result.returncode == 78, result.stderr
    assert result.stdout == b""
    assert refusal_reason(result) == "tls_configuration"
    assert b"process.failed" not in result.stderr
    assert b"pass phrase" not in result.stderr
    for private in (str(tmp_path), profile.get(HTTPS_ORIGIN), PRIVATE_MARKER):
        if private:
            assert private.encode() not in result.stderr


async def deployment_rows(
    settings: DatabaseSettings,
) -> tuple[dict[str, list[dict[str, object]]], str, list[str]]:
    database = Database(settings)
    try:
        rows = await start_rows(database)
        async with database.read_transaction() as reader:
            [version] = (await reader.scalars(select(graph_version.c.id))).all()
            attempts = list(await reader.scalars(select(attempt.c.id)))
        return rows, version, attempts
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_installed_sqlite_api_deploys_and_replays_manual_trigger(
    serving_installation: Path, tmp_path: Path, sqlite_template: Path
) -> None:
    settings = copy_sqlite_template(sqlite_template, tmp_path / "prepared.sqlite")
    database = Database(settings)
    try:
        seeded = await seed_case(database, NAMESPACE)
        unauthorized = await seed_case(database, "other")
    finally:
        await database.close()
    authorization = seeded.request.authorization
    foreign_authorization = unauthorized.request.authorization
    assert authorization is not None
    assert foreign_authorization is not None
    ir = skeleton_ir(harness="claude")
    ir_child(ir, "graph")["params"] = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    }
    ir_child(ir, "graph", "nodes", "approve", "config")["approve_when"] = (
        'params.name.matches("^ready$")'
    )
    graph_file = tmp_path / "graph.ir.json"
    graph_file.write_text(json.dumps(ir))
    environment = api_environment(str(settings.url)) | {DOMAIN_HARNESS: "claude"}

    process = await asyncio.create_subprocess_exec(
        *api_command(serving_installation, 0),
        cwd=tmp_path,
        env=environment,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        port, early = await read_bound_port(process)
        origin = f"http://{LOOPBACK}:{port}"
        client_environment = {
            "KORA_URL": origin,
            "KORA_TOKEN": authorization.removeprefix("Bearer "),
            "KORA_LOG_LEVEL": "DEBUG",
        }
        async with httpx.AsyncClient(
            base_url=origin, trust_env=False, timeout=10
        ) as client:
            schema = await client.get(SCHEMA, headers={"Authorization": authorization})
            disabled_workers = [
                await client.post(path, json={}) for path in WORKER_ROUTES
            ]
            await check_inactive_worker_routes(settings, disabled_workers)
            wrong_namespace = await client.post(
                DEPLOY_PATH,
                headers={"Authorization": foreign_authorization},
                json={"graph": GRAPH, "ir": ir, "resources": []},
            )
        deployed = await installed_cli(
            serving_installation,
            tmp_path,
            client_environment,
            ["deploy", str(graph_file), "--json"],
        )
        assert deployed.code == 0, deployed.stderr
        published = DeployResponse.model_validate_json(deployed.stdout)
        start = [
            "trigger",
            GRAPH,
            "--namespace",
            NAMESPACE,
            "--json",
            "--graph-version",
            published.graph_version_id,
            "--idempotency-key",
            "installed-start",
            "--param",
            'name="ready"',
            "--param",
            f'extra="{PRIVATE_PARAMETER}"',
        ]
        accepted = await installed_cli(
            serving_installation, tmp_path, client_environment, start
        )
        replay = await installed_cli(
            serving_installation, tmp_path, client_environment, start
        )
        refused_replay = await installed_cli(
            serving_installation,
            tmp_path,
            client_environment
            | {"KORA_TOKEN": foreign_authorization.removeprefix("Bearer ")},
            start,
        )
    finally:
        stdout, trailing = await terminate_installed_api(process)
    rows, version_id, attempts = await deployment_rows(settings)
    assert_worker_startup_absent(early + trailing)

    assert process.returncode == 0, early + trailing
    assert stdout == b""
    assert schema.status_code == 200
    paths = parse_json_object(schema.json())["paths"]
    assert isinstance(paths, dict)
    assert set(paths) == {
        RouteName.HEALTH.value,
        RouteName.GRAPH_VERSIONS.value,
        RouteName.GRAPH_RUNS.value,
        RouteName.RUN_EVENTS.value,
    }
    assert wrong_namespace.status_code == 403
    assert (
        ApiErrorEnvelope.model_validate_json(wrong_namespace.content).error.code
        == "API005"
    )
    assert not published.reused
    assert published.number == 1
    assert accepted.code == replay.code == 0, (accepted.stderr, replay.stderr)
    assert replay.stdout == accepted.stdout
    assert refused_replay.code == 1
    assert (
        ApiErrorEnvelope.model_validate_json(refused_replay.stdout).error.code
        == "API005"
    )
    outcome = AcceptedRun.model_validate_json(accepted.stdout)
    [saved] = rows["run"]
    [inputs] = rows["run_inputs"]
    [receipt] = rows["receipt"]
    [admission] = rows["admission"]
    assert outcome.graph_version_id == published.graph_version_id == f"gv_{version_id}"
    assert saved["graph_version_id"] == version_id
    assert saved["status"] == outcome.status == "pending"
    assert outcome.run_id == f"run_{saved['id']}"
    assert inputs["run_id"] == admission["run_id"] == saved["id"]
    assert inputs["params"] == (
        f'{{"extra":"{PRIVATE_PARAMETER}","name":"ready"}}'.encode()
    )
    assert inputs["cel_projection"] == b'{"name":"ready"}'
    assert receipt["idempotency_key"] == "installed-start"
    assert admission["receipt_id"] == receipt["id"]
    original_receipt = decode_start_receipt(receipt["start_outcome"])
    assert original_receipt.namespace_id == saved["namespace_id"]
    assert original_receipt.graph_id == saved["graph_id"]
    receipt_outcome = original_receipt.response
    assert isinstance(receipt_outcome, AcceptedStart)
    assert receipt_outcome.run_id == saved["id"]
    assert receipt_outcome.graph_version_id == version_id
    assert saved["needs_scheduling"] is True
    [created] = [event for event in rows["event"] if event["kind"] == "run.created"]
    assert created["run_id"] == saved["id"]
    assert created["actor"] == {
        "kind": "user",
        "owner": seeded.principal.owner,
        "token_id": seeded.principal.token_id,
    }
    assert not attempts
    records = diagnostics(early + trailing)
    assert any(
        record["event"] == "protocols.loading.provider_loaded"
        and record["provider"] == "claude"
        for record in records
    )
    assert sum(record["event"] == "api.serving_stopped" for record in records) == 1
    assert sum(record["event"] == "cel.binding.loaded" for record in records) == 1
    for private in (
        authorization.removeprefix("Bearer "),
        foreign_authorization.removeprefix("Bearer "),
        "installed-start",
        PRIVATE_PARAMETER,
    ):
        assert private.encode() not in (
            early + trailing + accepted.stderr + replay.stderr + refused_replay.stderr
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["", " ", PRIVATE_MARKER])
async def test_installed_sqlite_api_refuses_invalid_domain_configuration(
    serving_installation: Path,
    tmp_path: Path,
    sqlite_template: Path,
    harness: str,
) -> None:
    settings = copy_sqlite_template(sqlite_template, tmp_path / "prepared.sqlite")

    result = api_startup_result(
        serving_installation, tmp_path, str(settings.url), 0, harness=harness
    )
    await assert_domain_empty(settings)

    assert result.returncode == 78
    assert result.stdout == b""
    assert refusal_reason(result) == "domain_configuration"
    assert PRIVATE_MARKER.encode() not in result.stderr


async def assert_domain_empty(settings: DatabaseSettings) -> None:
    database = Database(settings)
    try:
        rows = await start_rows(database)
        async with database.read_transaction() as reader:
            versions = list(await reader.scalars(select(graph_version.c.id)))
    finally:
        await database.close()
    assert not any(rows.values())
    assert not versions


def installed_site_packages(python: Path) -> Path:
    result = subprocess.run(
        [
            str(python),
            "-I",
            "-c",
            "import sysconfig; print(sysconfig.get_path('purelib'))",
        ],
        capture_output=True,
        check=True,
        timeout=SERVE_TIMEOUT,
    )
    return Path(os.fsdecode(result.stdout.strip()))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "version",
        "record",
        "harness",
        "schema",
        "cel-metadata-encoding",
        "cel-record-encoding",
        "cel-record-field",
        "harness-entry-unavailable",
    ],
)
async def test_installed_sqlite_api_refuses_unavailable_domain_binding(
    core_wheel: Path,
    tmp_path: Path,
    sqlite_template: Path,
    damage: str,
) -> None:
    settings = copy_sqlite_template(sqlite_template, tmp_path / "prepared.sqlite")
    python = installed_core(core_wheel, tmp_path, serve=True, copy_payload=True)
    site = installed_site_packages(python)
    [metadata] = site.glob("common_expression_language-*.dist-info")
    if damage == "schema":
        schema = (
            site / "kora" / "ir" / "artifact_schemas" / "StructuredOutput.schema.json"
        )
        schema.write_bytes(schema.read_bytes() + b"\n")
    elif damage in {"harness", "harness-entry-unavailable"}:
        [entry_points] = site.glob("kora-*.dist-info/entry_points.txt")
        if damage == "harness":
            lines = entry_points.read_text().splitlines()
            remaining = [
                line for line in lines if not line.startswith("harness.claude ")
            ]
            assert len(remaining) == len(lines) - 1
            entry_points.write_text("\n".join(remaining) + "\n")
        else:
            entries = entry_points.read_text()
            selected = (
                "harness.claude = kora.plugins.harness.claude.adapter:provider_metadata"
            )
            assert entries.count(selected) == 1
            entry_points.write_text(
                entries.replace(
                    selected,
                    selected.removesuffix("provider_metadata") + "unavailable_provider",
                )
            )
    elif damage == "cel-record-field":
        record = metadata / "RECORD"
        record.write_bytes(record.read_bytes() + b"a" * (csv.field_size_limit() + 1))
    elif damage in {"cel-metadata-encoding", "cel-record-encoding"}:
        damaged = metadata / (
            "METADATA" if damage == "cel-metadata-encoding" else "RECORD"
        )
        damaged.write_bytes(damaged.read_bytes() + b"\xff")
    elif damage == "missing":
        shutil.rmtree(metadata)
        shutil.rmtree(site / "cel")
    elif damage == "version":
        path = metadata / "METADATA"
        source = path.read_bytes()
        assert source.count(b"Version: 0.10.0\n") == 1
        path.write_bytes(source.replace(b"Version: 0.10.0\n", b"Version: 0.0.0\n"))
    else:
        entry = site / "cel" / "__init__.py"
        entry.write_bytes(entry.read_bytes() + b"\nraise SystemExit(42)\n")

    result = api_startup_result(
        python, tmp_path, str(settings.url), 0, harness="claude"
    )
    await assert_domain_empty(settings)

    assert result.returncode == 78
    assert result.stdout == b""
    assert refusal_reason(result) == "domain_configuration"
    assert all(
        record["event"] != "cel.binding.loaded" for record in diagnostics(result.stderr)
    )


@pytest.mark.asyncio
async def test_installed_sigterm_preserves_buffered_response(
    serving_installation: Path, tmp_path: Path, sqlite_template: Path
) -> None:
    settings = copy_sqlite_template(sqlite_template, tmp_path / "prepared.sqlite")
    fixture = Path(__file__).with_name("serving_lifecycle_child.py")
    child = await asyncio.create_subprocess_exec(
        str(serving_installation),
        "-I",
        str(fixture),
        "buffered_response",
        cwd=tmp_path,
        env=api_environment(str(settings.url)),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert child.stdout is not None
    assert child.stderr is not None

    try:
        async with asyncio.timeout(SERVE_TIMEOUT):
            port, _ = await read_bound_port(child)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
                client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 32768)
                client.setblocking(False)
                await asyncio.get_running_loop().sock_connect(client, (LOOPBACK, port))
                reader, writer = await asyncio.open_connection(sock=client, limit=8192)
                try:
                    writer.write(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
                    await writer.drain()
                    buffered = await child.stdout.readline()
                    assert buffered == b"response_buffered\n"
                    idle_reader, idle_writer = await asyncio.open_connection(
                        LOOPBACK, port
                    )
                    try:
                        child.send_signal(signal.SIGTERM)
                        await serving_event(child.stderr, "api.serving_draining")
                        client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 262144)
                        idle_tail = await idle_reader.read()
                        response = await reader.read()
                    finally:
                        idle_writer.close()
                        await idle_writer.wait_closed()
                finally:
                    writer.close()
                    await writer.wait_closed()
            output, trailing = await child.communicate()
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()

    headers, separator, body = response.partition(b"\r\n\r\n")
    assert separator == b"\r\n\r\n"
    assert headers.startswith(b"HTTP/1.1 200 OK\r\n")
    assert body == b"x" * SLOW_RESPONSE_BYTES
    assert idle_tail == b""
    assert output == b"lifespan_shutdown\n"
    assert child.returncode == 0
    assert b"api.serving_stopped" in trailing
    assert b"drain_unfinished" not in trailing


@pytest.mark.asyncio
@pytest.mark.parametrize("forced", [False, True])
async def test_serving_keeps_the_original_process_until_held_native_work_settles(
    serving_installation: Path,
    tmp_path: Path,
    sqlite_template: Path,
    forced: bool,
) -> None:
    settings = copy_sqlite_template(sqlite_template, tmp_path / "prepared.sqlite")
    fixture = Path(__file__).with_name("serving_lifecycle_child.py")
    environment = api_environment(str(settings.url))
    release_read, release_write = os.pipe()
    request_writer: asyncio.StreamWriter | None = None

    try:
        try:
            child = await asyncio.create_subprocess_exec(
                str(serving_installation),
                "-I",
                str(fixture),
                "hold_native",
                str(release_read),
                pass_fds=(release_read,),
                cwd=tmp_path,
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        finally:
            os.close(release_read)
        assert child.stdout is not None
        assert child.stderr is not None
        try:
            async with asyncio.timeout(SERVE_TIMEOUT):
                bound = await serving_event(child.stderr, "api.serving_bound")
                port = bound.get("port")
                assert type(port) is int
                reader, request_writer = await asyncio.open_connection(
                    "127.0.0.1", port
                )
                request_writer.write(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
                await request_writer.drain()
                held = await child.stdout.readline()
                assert held == b"native_sqlite_held\n"
                child.send_signal(signal.SIGTERM)
                await serving_event(child.stderr, "api.serving_draining")
                if forced:
                    child.send_signal(signal.SIGINT)
                async with asyncio.timeout(DRAIN_RECEIPT_TIMEOUT):
                    original_wire = await reader.read()
                    refusal = await serving_event(child.stderr, "api.serving_refused")
                still_owns_work = child.returncode is None
                with pytest.raises(ConnectionRefusedError):
                    await asyncio.open_connection("127.0.0.1", port)
                os.write(release_write, b"r")
                output, diagnostics = await child.communicate()
        finally:
            if request_writer is not None:
                request_writer.close()
                await request_writer.wait_closed()
            if child.returncode is None:
                child.kill()
                await child.wait()
    finally:
        os.close(release_write)

    assert original_wire == b""
    assert refusal["reason"] == "drain_unfinished"
    assert still_owns_work
    assert child.returncode == 70
    assert output == (
        b"native_sqlite_returned\nnative_sqlite_result_closed\nrequest_settled\n"
        b"native_sqlite_file_closed\n"
    )
    assert b"api.serving_stopped" not in diagnostics


@pytest.mark.asyncio
async def test_serving_settles_abandoned_preparation_and_sqlite_after_forced_drain(
    serving_installation: Path,
    tmp_path: Path,
    sqlite_template: Path,
) -> None:
    settings = copy_sqlite_template(sqlite_template, tmp_path / "prepared.sqlite")
    fixture = Path(__file__).with_name("serving_lifecycle_child.py")
    release_read, release_write = os.pipe()
    request_writer: asyncio.StreamWriter | None = None

    try:
        try:
            process = await asyncio.create_subprocess_exec(
                str(serving_installation),
                "-I",
                str(fixture),
                "hold_preparation",
                str(release_read),
                pass_fds=(release_read,),
                cwd=tmp_path,
                env=api_environment(str(settings.url)),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        finally:
            os.close(release_read)
        assert process.stdout is not None
        assert process.stderr is not None
        try:
            async with asyncio.timeout(SERVE_TIMEOUT):
                port, _ = await read_bound_port(process)
                reader, request_writer = await asyncio.open_connection(LOOPBACK, port)
                request_writer.write(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
                await request_writer.drain()
                assert await process.stdout.readline() == b"preparation_held\n"
                process.send_signal(signal.SIGTERM)
                await serving_event(process.stderr, "api.serving_draining")
                process.send_signal(signal.SIGINT)
                async with asyncio.timeout(DRAIN_RECEIPT_TIMEOUT):
                    wire = await reader.read()
                    assert (
                        await process.stdout.readline() == b"request_sqlite_settled\n"
                    )
                    refused = await serving_event(process.stderr, "api.serving_refused")
                still_running = process.returncode is None
                with pytest.raises(ConnectionRefusedError):
                    await asyncio.open_connection(LOOPBACK, port)
                os.write(release_write, b"r")
                stdout, stderr = await process.communicate()
        finally:
            if request_writer is not None:
                request_writer.close()
                await request_writer.wait_closed()
            if process.returncode is None:
                process.kill()
                await process.wait()
    finally:
        os.close(release_write)

    assert wire == b""
    assert refused["reason"] == "drain_unfinished"
    assert still_running
    assert process.returncode == 70, stderr
    assert stdout == b"retained_cel_settled\ndomain_owners_settled\n"
    assert b"api.serving_stopped" not in stderr


def test_installed_core_refuses_api_without_its_extra(
    bare_installation: Path, tmp_path: Path, sqlite_template: Path
) -> None:
    settings = copy_sqlite_template(sqlite_template, tmp_path / "prepared.sqlite")

    result = api_startup_result(bare_installation, tmp_path, str(settings.url), 0)

    assert result.returncode == 78
    assert result.stdout == b""
    assert b"kora: API startup refused (dependency_unavailable)" in result.stderr
    assert all(
        record["event"] != "api.serving_bound" for record in diagnostics(result.stderr)
    )


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("missing-url", "database_configuration"),
        ("invalid-url", "database_configuration"),
        ("missing-file", "sqlite_unprepared"),
        ("unmigrated-file", "sqlite_unprepared"),
    ],
)
def test_installed_api_refuses_unprepared_sqlite_before_bind(
    serving_installation: Path, tmp_path: Path, case: str, reason: str
) -> None:
    database_file = tmp_path / f"{case}.sqlite"
    if case == "unmigrated-file":
        database_file.touch(mode=0o600)
    database_url = f"sqlite+aiosqlite:///{database_file}"
    if case == "invalid-url":
        database_url = f"invalid://{PRIVATE_MARKER}"

    result = api_startup_result(
        serving_installation,
        tmp_path,
        None if case == "missing-url" else database_url,
        0,
    )

    assert result.returncode == 78
    assert result.stdout == b""
    assert refusal_reason(result) == reason
    assert PRIVATE_MARKER.encode() not in result.stderr
    if case == "missing-file":
        assert not database_file.exists()


@pytest.mark.parametrize("marker", [b"KMT1", b"KJT1"])
def test_installed_api_and_upgrade_refuse_a_marker_only_main(
    serving_installation: Path, tmp_path: Path, marker: bytes
) -> None:
    main = tmp_path / "marker.sqlite"
    image = marker_only_sqlite(main, marker)
    url = f"sqlite+aiosqlite:///{main}"
    root = tmp_path / "storage"
    provider_probe = """
import asyncio
import sys
from pathlib import Path

from kora.plugins.storage.local import LocalArtifactStorage, LocalStorageConfiguration
from kora.protocols.errors import KoraProtocolError
from kora.protocols.storage_models import StorageLimits

root, marker = map(Path, sys.argv[1:])
configuration = LocalStorageConfiguration(
    root=str(root), limits=StorageLimits(version=1)
)
provider = LocalArtifactStorage(configuration)
asyncio.run(provider.aclose())
main = root / "control.sqlite"
image = marker.read_bytes()
main.write_bytes(image)
sidecar = root / "control.sqlite-wal"
sidecar.write_bytes(b"owned sidecar witness")
try:
    LocalArtifactStorage(configuration)
except KoraProtocolError as error:
    assert error.code == "storage_io"
else:
    raise AssertionError("the installed provider accepted the isolated marker")
assert main.read_bytes() == image
assert sidecar.read_bytes() == b"owned sidecar witness"
assert not (root / "control.sqlite-shm").exists()
assert not (root / "control.sqlite-journal").exists()
"""

    api = api_startup_result(serving_installation, tmp_path, url, 0)
    upgrade = subprocess.run(
        [str(serving_installation.with_name("kora")), "db", "upgrade"],
        cwd=tmp_path,
        env=api_environment(url),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=SERVE_TIMEOUT,
    )
    provider = subprocess.run(
        [str(serving_installation), "-I", "-c", provider_probe, str(root), str(main)],
        cwd=tmp_path,
        env={"PATH": os.defpath},
        capture_output=True,
        check=False,
        timeout=SERVE_TIMEOUT,
    )

    assert api.returncode == 78
    assert api.stdout == b""
    assert refusal_reason(api) == "sqlite_unprepared"
    assert upgrade.returncode != 0
    assert upgrade.stdout == b""
    assert provider.returncode == 0, provider.stderr
    assert main.read_bytes() == image
    assert set(tmp_path.iterdir()) == {main, root}


@pytest.mark.asyncio
@pytest.mark.parametrize("stamp", ["0034", "run_event_metadata_003", "0033", None])
async def test_installed_api_refuses_inexact_event_schema_before_bind(
    serving_installation: Path,
    tmp_path: Path,
    sqlite_template: Path,
    stamp: str | None,
) -> None:
    settings = copy_sqlite_template(sqlite_template, tmp_path / "events.sqlite")
    database = Database(settings)
    try:
        async with database.transaction() as writer:
            if stamp is None:
                await writer.execute(text("DROP INDEX ix_event_run_page_bounded"))
                await writer.execute(
                    text("CREATE INDEX ix_event_run_page_bounded ON event (run_id, id)")
                )
            else:
                await writer.execute(
                    text("UPDATE alembic_version SET version_num = :stamp"),
                    {"stamp": stamp},
                )
    finally:
        await database.close()

    result = await asyncio.to_thread(
        api_startup_result,
        serving_installation,
        tmp_path,
        str(settings.url),
        0,
    )

    assert result.returncode == 78
    assert refusal_reason(result) == "sqlite_unprepared"
    assert all(
        record["event"] != "api.serving_bound" for record in diagnostics(result.stderr)
    )


@pytest.mark.parametrize(
    "profile", ["candidate", "zero-port", "out-of-range-port", "key-log"]
)
def test_installed_api_refuses_before_bind_or_postgresql_dial(
    postgres_serving_installation: Path, tmp_path: Path, profile: str
) -> None:
    _, authority = tls_pair(tmp_path)
    keylog = tmp_path / "refused-keylog"
    expected = (
        "postgresql_activation_unavailable"
        if profile == "candidate" and native_probe_required_here()
        else "postgresql_profile_unavailable"
    )
    settings = urlencode(
        {
            "sslmode": "verify-full",
            "sslrootcert": str(authority),
            "target_session_attrs": "any",
        }
    )
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as backend,
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserved_api,
    ):
        backend.bind((LOOPBACK, 0))
        backend.listen()
        backend.setblocking(False)
        reserved_api.bind((LOOPBACK, 0))
        port = {"zero-port": 0, "out-of-range-port": 65536}.get(
            profile, socket_port(backend)
        )
        database_url = (
            f"postgresql+asyncpg://local:{PRIVATE_MARKER}@{LOOPBACK}:"
            f"{port}/metadata?{settings}"
        )

        result = api_startup_result(
            postgres_serving_installation,
            tmp_path,
            database_url,
            socket_port(reserved_api),
            ssl_keylog=keylog if profile == "key-log" else None,
            harness="claude" if profile == "candidate" else None,
        )

        with pytest.raises(BlockingIOError):
            backend.accept()[0].close()
    assert result.returncode == 78, result.stderr
    assert result.stdout == b""
    assert refusal_reason(result) == expected
    assert not keylog.exists()
    records = diagnostics(result.stderr)
    assert all(
        record["event"] != "protocols.loading.provider_loaded"
        and record["component"] != "db"
        for record in records
    )
    assert PRIVATE_MARKER.encode() not in result.stderr


def test_installed_sqlite_api_reports_occupied_port(
    serving_installation: Path, tmp_path: Path, sqlite_template: Path
) -> None:
    settings = copy_sqlite_template(sqlite_template, tmp_path / "prepared.sqlite")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind((LOOPBACK, 0))
        listener.listen()

        result = api_startup_result(
            serving_installation, tmp_path, str(settings.url), socket_port(listener)
        )

    assert result.returncode == 70
    assert result.stdout == b""
    assert refusal_reason(result) == "bind_failed"
    assert b"Traceback" not in result.stderr


@pytest.mark.parametrize(
    ("case", "interruption", "error"),
    [
        ("failure", None, ValueError),
        ("timeout", None, TimeoutError),
        ("interrupt", signal.SIGINT, KeyboardInterrupt),
        ("terminate", signal.SIGTERM, KeyboardInterrupt),
    ],
)
def test_core_wheel_build_refuses_partial_artifacts_and_settles(
    tmp_path: Path,
    case: str,
    interruption: signal.Signals | None,
    error: type[BaseException],
) -> None:
    ready = tmp_path / "ready"
    os.mkfifo(ready)
    descriptor = os.open(ready, os.O_RDWR | os.O_NONBLOCK)
    handlers = tuple(
        signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)
    )
    arguments = [sys.executable, "-c", BUILD_TREE, str(ready), case]
    try:
        with (
            core_wheel_workspace() as output,
            core_wheel_build_stop() as stop,
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            pending = executor.submit(
                observe_build_tree, descriptor, output, interruption
            )
            deadline = time.monotonic() + (2 if case == "timeout" else 10)

            with pytest.raises(error) as refusal:
                run_core_wheel_build(
                    arguments, output, dict(os.environ), deadline, stop
                )
            pids = pending.result(timeout=6)

            assert (output / "kora-partial.whl").is_file()
            assert len(set(pids)) == 2
            for pid in pids:
                with pytest.raises(ProcessLookupError):
                    os.kill(pid, 0)
    finally:
        os.close(descriptor)

    assert not output.exists()
    assert (
        tuple(signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM))
        == handlers
    )
    if case == "failure":
        assert (
            str(refusal.value)
            == "core wheel build failed (completed, 23)\ncore build stderr\n"
        )
