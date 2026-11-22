import asyncio
import json
import os
import selectors
import shutil
import signal
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlencode

import httpx
import pytest

from kora.db.connection import Database
from kora.ir.json import JsonObject, parse_json_object
from kora.models.errors import ApiErrorEnvelope
from tests.api.serving_lifecycle_child import SLOW_RESPONSE_BYTES, serving_event
from tests.api.test_linux_serving_barrier import native_probe_required_here
from tests.client.support import tls_pair
from tests.plugins.authentication.support import seed_case
from tests.support.databases import copy_sqlite_template
from tests.support.provider_tooling import (
    copy_core_wheel,
    core_wheel_build_stop,
    core_wheel_workspace,
    create_virtual_environment,
    prepare_wheelhouse,
    run_core_wheel_build,
    run_setup,
)

LOOPBACK = "127.0.0.1"
SCHEMA = "/api/v1/openapi.json"
SERVE_TIMEOUT = 20
DRAIN_RECEIPT_TIMEOUT = 8
PRIVATE_MARKER = "private-installed-api-marker"

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
    core_wheel: Path, root: Path, *, serve: bool = False, postgres: bool = False
) -> Path:
    wheelhouse = root / "dependencies"
    wheelhouse.mkdir()
    prepare_wheelhouse(wheelhouse, serve=serve, postgres=postgres)
    wheel = copy_core_wheel(core_wheel, wheelhouse)
    python = create_virtual_environment(root / "environment")
    extras = ",".join(
        name for name, enabled in (("serve", serve), ("postgres", postgres)) if enabled
    )
    run_setup(
        [
            "uv",
            "pip",
            "install",
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
) -> subprocess.CompletedProcess[bytes]:
    environment = api_environment(database_url)
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


async def read_bound_port(process: asyncio.subprocess.Process) -> tuple[int, bytes]:
    assert process.stderr is not None
    received = bytearray()
    async with asyncio.timeout(SERVE_TIMEOUT):
        while line := await process.stderr.readline():
            received.extend(line)
            for record in diagnostics(line):
                if record["event"] == "api.serving_bound":
                    port = record["port"]
                    assert type(port) is int
                    assert 0 < port <= 65535
                    return port, bytes(received)
    raise AssertionError(f"The installed API did not report its listener: {received!r}")


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
        head_missing = await read_raw_head(port)
        head_denied = await read_raw_head(port, service_token)
        head_wrong_method = await read_raw_head(port, personal_token)
    finally:
        stdout, trailing = await terminate_installed_api(process)

    stderr = early + trailing
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
    assert set(definition["paths"]) == {"/healthz"}
    assert "servers" not in definition
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
