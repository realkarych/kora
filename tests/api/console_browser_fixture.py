import asyncio
import errno
import json
import os
import re
import secrets
import select
import shlex
import shutil
import signal
import socket
import sqlite3
import ssl
import sys
import tempfile
from collections.abc import AsyncGenerator
from contextlib import ExitStack, asynccontextmanager, closing
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Literal

import httpx
from sqlalchemy import update

from kora.db.connection import Database, DatabaseSettings
from kora.db.tables import api_token
from kora.db.tokens import revoke_token
from kora.ir.json import JsonObject, parse_json_object
from kora.models.run_events import RunEventPage
from tests.api.console_browser_children import (
    BrowserStderrOverflow,
    adopt_fixture_children,
    fixture_pidfd,
    release_adopted,
    run_node,
    signal_owned,
    wait_stopped,
)
from tests.api.test_packaging import (
    HTTPS_ORIGIN,
    LOOPBACK,
    SERVE_TIMEOUT,
    TLS_EVENTS_NAMESPACE,
    WEB_ROOT,
    api_command,
    api_environment,
    assert_private_channels,
    cli_event_page,
    diagnostics,
    https_client_environment,
    https_environment,
    installed_cli,
    installed_core,
    installed_operating_mcp,
    mcp_event_page,
    observed_page,
    raw_event_page,
    read_bound_port,
    terminate_installed_api,
    unused_loopback_port,
)
from tests.client.support import TlsMaterial, write_tls_material
from tests.db.seed import START, Seeder
from tests.db.test_run_event_reads import SYSTEM, EventScope, seed_event, seed_run
from tests.plugins.authentication.support import seed_case
from tests.support.databases import copy_sqlite_template, prepare_sqlite_template

PAGE_SIZE = 20
PRIVATE_PAYLOAD = "private-installed-browser-event-payload"
INERT_EVENT = "<img src=x onerror=alert(1)>"
OPENER_PRIVATE_MARKER = "private-installed-browser-mcp-override"
TRUST_BUNDLE = Path("/etc/ssl/certs/ca-certificates.crt")
REDEMPTION = re.compile(r"/api/v1/auth/session/[0-9a-f]{64}")
SECRET_PATH = re.compile(rb"/api/v1/auth/session/[0-9a-f]{64}")
SAFE_CAPTURE = re.compile(
    r"(?:report\.json|timeline-(?:light|dark)-(?:1280|768|375)\.png)"
)
OWNED_HELPERS = (
    "awk",
    "basename",
    "cat",
    "cut",
    "dirname",
    "egrep",
    "find",
    "grep",
    "head",
    "id",
    "mkdir",
    "readlink",
    "sed",
    "sort",
    "touch",
    "tr",
    "uname",
    "which",
    "xdg-mime",
)


@dataclass(frozen=True)
class BrowserData:
    run_id: str
    empty_run: str
    denied_run: str
    first_credential: str
    second_credential: str
    first_token: str
    second_token: str
    event_ids: tuple[str, ...]


async def seed_data(database: Database) -> BrowserData:
    personal = await seed_case(database, TLS_EVENTS_NAMESPACE)
    successor = await seed_case(database, "installed_browser_successor")
    owner = personal.principal
    first_credential, second_credential = secrets.token_hex(32), secrets.token_hex(32)
    async with database.transaction() as writer:
        for principal, credential in (
            (owner, first_credential),
            (successor.principal, second_credential),
        ):
            await writer.execute(
                update(api_token)
                .where(api_token.c.id == principal.token_id)
                .values(
                    digest=sha256(credential.encode()).hexdigest(),
                    namespaces=owner.model_dump(mode="json")["namespaces"],
                )
            )
        namespace = owner.namespaces[0].namespace_id
        seed = Seeder(writer)
        graph = await seed.graph(namespace)
        version = await seed.graph_version(graph)
        run = await seed.run(namespace, graph, version)
        empty_run = await seed.run(namespace, graph, version)
        node = await seed.node(run, "installed")
        attempt = await seed.attempt(node)
        denied = await seed_run(writer, "installed_browser_denied")
        scope = EventScope(namespace, run, node, attempt)
        attribution: JsonObject = {
            "kind": "user",
            "owner": owner.owner,
            "token_id": owner.token_id,
        }
        event_ids = tuple(f"{index:026d}" for index in range(1, PAGE_SIZE + 2))
        for index, event_id in enumerate(event_ids):
            await seed_event(
                writer,
                scope,
                attribution if index % 2 else SYSTEM,
                0,
                event_id=event_id,
                payload={"private": PRIVATE_PAYLOAD},
                kind=INERT_EVENT if index == PAGE_SIZE else "run.created",
            )
    return BrowserData(
        f"run_{run}",
        f"run_{empty_run}",
        f"run_{denied.run_id}",
        first_credential,
        second_credential,
        owner.token_id,
        successor.principal.token_id,
        tuple(f"event_{event_id}" for event_id in reversed(event_ids)),
    )


@dataclass
class PrivateChannels:
    markers: set[bytes]
    captured: list[bytes]
    stage: str = "setup"
    checks: list[str] = field(default_factory=list[str])

    def add_marker(self, value: str) -> None:
        assert len(value) > 16
        self.markers.add(value.encode())

    def publish(self, capture: Path, output: Path) -> None:
        channels = [*self.captured]
        files = sorted(capture.iterdir())
        for path in files:
            assert SAFE_CAPTURE.fullmatch(path.name)
            assert not path.is_symlink()
            assert path.is_file()
            assert path.stat().st_size <= 4 * 1024 * 1024
            channels.append(path.read_bytes())
        for channel in channels:
            assert_private_channels(channel, b"", tuple(self.markers))
            assert not SECRET_PATH.search(channel), (
                "An output channel exposed a redemption path."
            )
        for path in files:
            shutil.copyfile(path, output / path.name)


@dataclass(frozen=True)
class NativeOpener:
    conditions: dict[str, str]
    handler: Path
    handoff: Path
    override_used: Path

    @classmethod
    def provision(cls, root: Path) -> "NativeOpener":
        home, config, data, config_dirs, data_dirs, helpers = (
            root / name
            for name in (
                "opener-home",
                "opener-config",
                "opener-data",
                "opener-config-dirs",
                "opener-data-dirs",
                "opener-helpers",
            )
        )
        for directory in (home, config, data, config_dirs, data_dirs, helpers):
            directory.mkdir(mode=0o700)
        for name in OWNED_HELPERS:
            target = Path("/usr/bin") / name
            assert target.is_file()
            (helpers / name).symlink_to(target)
        handoff = data / "kora-handoff"
        receiver = helpers / "accept-url"
        receiver.write_text(
            "#!/bin/sh\n"
            'test "$#" -eq 1 || exit 78\n'
            'test "${KORA_TOKEN+x}${KORA_MCP_TOKEN+x}${BROWSER+x}" = "" || exit 78\n'
            'test "${#1}" -le 2048 || exit 78\n'
            'umask 077\nset -C\nprintf "%s\\n" "$1" > "$XDG_DATA_HOME/kora-handoff"\n'
            'if test -p "$XDG_DATA_HOME/kora-release"; then\n'
            'printf "%s\\n" "$$" > "$XDG_DATA_HOME/kora-handler"\n'
            'IFS= read -r released < "$XDG_DATA_HOME/kora-release"\nfi\n',
            encoding="utf-8",
        )
        receiver.chmod(0o700)
        override = helpers / "ambient-browser"
        override_used = data / "unexpected-opener"
        override.write_text(
            "#!/bin/sh\n"
            'printf "%s\\n" unexpected > "$XDG_DATA_HOME/unexpected-opener"\n',
            encoding="utf-8",
        )
        override.chmod(0o700)
        applications = data / "applications"
        applications.mkdir(mode=0o700)
        (config / "mimeapps.list").write_text(
            "[Default Applications]\nx-scheme-handler/https=kora-fixture.desktop;\n",
            encoding="utf-8",
        )
        handler = applications / "kora-fixture.desktop"
        handler.write_text(
            "[Desktop Entry]\nType=Application\nName=Kora fixture\n"
            f"Exec={shlex.quote(str(receiver))} %u\nNoDisplay=true\n",
            encoding="utf-8",
        )
        return cls(
            {
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(config),
                "XDG_DATA_HOME": str(data),
                "XDG_CONFIG_DIRS": str(config_dirs),
                "XDG_DATA_DIRS": str(data_dirs),
                "PATH": str(helpers),
                "XDG_CURRENT_DESKTOP": "generic",
                "DISPLAY": ":99",
                "BROWSER": str(override),
                "KORA_MCP_TOKEN": OPENER_PRIVATE_MARKER,
                "LANG": "C.UTF-8",
            },
            handler,
            handoff,
            override_used,
        )

    async def open(
        self,
        python: Path,
        directory: Path,
        environment: dict[str, str],
        run_id: str,
        channels: PrivateChannels,
        *,
        mode: Literal["open", "missing", "held"] = "open",
    ) -> str | None:
        assert not self.handoff.exists()
        disabled = self.handler.with_suffix(".disabled")
        if mode == "missing":
            self.handler.rename(disabled)
        try:
            result = await installed_cli(
                python,
                directory,
                {**environment, **self.conditions},
                ["open", run_id, "--namespace", TLS_EVENTS_NAMESPACE],
            )
        finally:
            if mode == "missing":
                disabled.rename(self.handler)
        channels.captured.extend((result.stdout, result.stderr))
        assert not self.override_used.exists()
        outcome, stderr = {
            "open": ("opened", []),
            "missing": ("opener_refused", [b"kora: could not open the browser."]),
            "held": (
                "opener_unconfirmed",
                [b"kora: browser handoff is unconfirmed. Check for the opened tab."],
            ),
        }[mode]
        assert result.code == int(mode != "open")
        assert result.stdout == b""
        assert [
            line for line in result.stderr.splitlines() if not line.startswith(b"{")
        ] == stderr
        assert [
            record["outcome"]
            for record in diagnostics(result.stderr)
            if record["event"] == "cli.open.finished"
        ] == [outcome]
        if mode == "missing":
            assert not self.handoff.exists()
            return None
        assert self.handoff.is_file()
        assert self.handoff.stat().st_mode & 0o777 == 0o600
        assert 0 < self.handoff.stat().st_size <= 2049
        url = self.handoff.read_text(encoding="ascii").removesuffix("\n")
        self.handoff.unlink()
        origin = environment["KORA_URL"]
        assert url.startswith(origin)
        assert REDEMPTION.fullmatch(url.removeprefix(origin))
        channels.add_marker(url)
        channels.add_marker(url.rsplit("/", 1)[1])
        return url

    async def check_unconfirmed(
        self,
        python: Path,
        root: Path,
        environment: dict[str, str],
        run_id: str,
        channels: PrivateChannels,
        preserve: frozenset[int],
    ) -> None:
        release = self.handoff.with_name("kora-release")
        identity = self.handoff.with_name("kora-handler")
        os.mkfifo(release, mode=0o600)
        try:
            assert await self.open(
                python, root, environment, run_id, channels, mode="held"
            )
            raw_pid = identity.read_bytes()
            assert len(raw_pid) <= 12
            pid = int(raw_pid)
            descriptor = fixture_pidfd(pid)
            try:
                assert not select.select([descriptor], [], [], 0)[0]
                async with asyncio.timeout(5):
                    while True:
                        try:
                            writer = os.open(release, os.O_WRONLY | os.O_NONBLOCK)
                        except OSError as error:
                            if error.errno != errno.ENXIO:
                                raise
                            await asyncio.sleep(0)
                        else:
                            break
                try:
                    assert os.write(writer, b"release\n") == 8
                finally:
                    os.close(writer)
                await release_adopted(preserve, terminate=False)
                assert select.select([descriptor], [], [], 0)[0] == [descriptor]
            finally:
                os.close(descriptor)
        finally:
            await release_adopted(preserve)
            release.unlink()
            identity.unlink(missing_ok=True)
        channels.checks.append("native-opener-unconfirmed-settled")


async def bounded_channel(reader: asyncio.StreamReader) -> bytes:
    captured = bytearray()
    while chunk := await reader.read(8192):
        captured.extend(chunk)
        assert len(captured) <= 64 * 1024
    return bytes(captured)


@asynccontextmanager
async def installed_listener(
    python: Path,
    root: Path,
    settings: DatabaseSettings,
    material: TlsMaterial,
    channels: PrivateChannels,
) -> AsyncGenerator[tuple[str, int]]:
    port = unused_loopback_port()
    profile = https_environment(material, port)
    process = await asyncio.create_subprocess_exec(
        *api_command(python, port),
        cwd=root,
        env={**api_environment(str(settings.url)), **profile, "KORA_API_CONSOLE": "1"},
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    collector: asyncio.Task[bytes] | None = None
    early = b""
    try:
        bound_port, early = await read_bound_port(process)
        assert bound_port == port
        collector = asyncio.create_task(bounded_channel(process.stderr))
        yield profile[HTTPS_ORIGIN], process.pid
    finally:
        if collector is None:
            stdout, trailing = await terminate_installed_api(process)
        else:
            if process.returncode is None:
                process.terminate()
            try:
                async with asyncio.timeout(SERVE_TIMEOUT):
                    stdout, trailing = (
                        await bounded_channel(process.stdout),
                        await collector,
                    )
                    await process.wait()
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
        stderr = early + trailing
        channels.captured.extend((stdout, stderr))
        assert stdout == b""
        assert process.returncode == 0
        received = diagnostics(stderr)
        assert sum(record["event"] == "api.serving_stopped" for record in received) == 1
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((LOOPBACK, port))
            probe.listen()


async def canonical_page(
    python: Path,
    root: Path,
    client: httpx.AsyncClient,
    environment: dict[str, str],
    data: BrowserData,
) -> RunEventPage:
    async with installed_operating_mcp(python, root, environment) as mcp:
        observations = (
            await observed_page(
                raw_event_page(
                    client, data.run_id, data.first_credential, None, PAGE_SIZE
                )
            ),
            await observed_page(
                cli_event_page(python, root, environment, data.run_id, None, PAGE_SIZE)
            ),
            await observed_page(mcp_event_page(mcp, data.run_id, None, PAGE_SIZE)),
        )
    canonical = observations[0].page
    assert canonical.next_cursor
    assert tuple(event.id for event in canonical.items) == data.event_ids[:PAGE_SIZE]
    for observed in observations:
        assert observed.began <= observed.page.observed_at <= observed.ended
        assert observed.page.items == canonical.items
        assert observed.page.next_cursor == canonical.next_cursor
    return canonical


async def send_json(writer: asyncio.StreamWriter, value: JsonObject) -> None:
    line = json.dumps(value, separators=(",", ":")).encode() + b"\n"
    assert len(line) <= 64 * 1024
    writer.write(line)
    await writer.drain()


async def browser_exchange(
    process: asyncio.subprocess.Process,
    launch: JsonObject,
    database: Database,
    python: Path,
    root: Path,
    opener: NativeOpener,
    environment: dict[str, str],
    second_environment: dict[str, str],
    data: BrowserData,
    channels: PrivateChannels,
) -> None:
    assert process.stdin is not None
    assert process.stdout is not None
    await send_json(process.stdin, launch)
    next_action = ["relogin", "expire", "revoke", "finish"]
    async with asyncio.timeout(125):
        while line := await process.stdout.readline():
            message = parse_json_object(json.loads(line))
            if message.get("type") == "markers":
                markers = message.get("values")
                assert isinstance(markers, list)
                assert len(markers) == 2
                for marker in markers:
                    assert isinstance(marker, str)
                    assert re.fullmatch(r"[0-9a-f]{64}", marker)
                    channels.add_marker(marker)
                continue
            channels.captured.append(line)
            assert next_action
            assert message == {"type": next_action.pop(0)}
            action = message["type"]
            if action == "expire":
                async with database.transaction() as writer:
                    await writer.execute(
                        update(api_token)
                        .where(api_token.c.id == data.first_token)
                        .values(expires_at=START)
                    )
            if action in ("relogin", "expire"):
                url = await opener.open(
                    python,
                    root,
                    environment if action == "relogin" else second_environment,
                    data.run_id,
                    channels,
                )
                assert url is not None
                await send_json(process.stdin, {"url": url})
            elif action == "revoke":
                async with database.transaction() as writer:
                    await revoke_token(writer, data.second_token)
                await send_json(process.stdin, {"revoked": True})
    assert not next_action


async def check_browser_recovery(
    node: str,
    root: Path,
    environment: dict[str, str],
    channels: PrivateChannels,
    mode: Literal["overflow", "forced"],
) -> None:
    home = root / f"ownership-{mode}"
    home.mkdir(mode=0o700)
    children: list[asyncio.subprocess.Process] = []
    browser_descriptors: list[int] = []
    with ExitStack() as descriptors:

        async def exercise(
            process: asyncio.subprocess.Process, node_descriptor: int | None
        ) -> None:
            assert process.stdout is not None
            assert process.stdin is not None
            children.append(process)
            line = await asyncio.wait_for(process.stdout.readline(), SERVE_TIMEOUT)
            assert len(line) <= 12
            browser_pid = int(line)
            assert os.getpgid(browser_pid) == browser_pid
            descriptor = fixture_pidfd(browser_pid)
            descriptors.callback(os.close, descriptor)
            browser_descriptors.append(descriptor)
            assert not select.select([descriptor], [], [], 0)[0]
            if mode == "overflow":
                process.stdin.write(b"x")
                await process.stdin.drain()
                await process.stdout.read(1)
            else:
                assert node_descriptor is not None
                signal_owned(node_descriptor, signal.SIGSTOP)
                await wait_stopped(node_descriptor)
                raise asyncio.CancelledError

        try:
            await run_node(
                node,
                WEB_ROOT / "tests/browser/installed-ownership-probe.ts",
                (mode,),
                root,
                {**environment, "HOME": str(home), "TMPDIR": str(home)},
                exercise,
                grace=1,
            )
        except BrowserStderrOverflow:
            assert mode == "overflow"
            expected = -signal.SIGTERM
        except asyncio.CancelledError:
            assert mode == "forced"
            expected = -signal.SIGKILL
        else:
            raise AssertionError("Browser failure probe unexpectedly completed.")
        [child] = children
        [descriptor] = browser_descriptors
        assert child.returncode == expected
        assert select.select([descriptor], [], [], 0)[0] == [descriptor]
    channels.checks.append(f"browser-{mode}-settled")


async def exercise_browser(
    python: Path,
    root: Path,
    output: Path,
    settings: DatabaseSettings,
    material: TlsMaterial,
    channels: PrivateChannels,
) -> None:
    channels.stage = "database"
    capture = root / "browser-evidence"
    capture.mkdir(mode=0o700)
    database = Database(settings)
    node = shutil.which("node")
    assert node is not None
    variables = {
        "HOME": str(root),
        "PATH": os.defpath,
        "LANG": "C.UTF-8",
        "PLAYWRIGHT_BROWSERS_PATH": os.environ.get(
            "PLAYWRIGHT_BROWSERS_PATH",
            str(Path.home() / ".cache/ms-playwright"),
        ),
    }
    trust = sha256(TRUST_BUNDLE.read_bytes()).digest()
    try:
        data = await seed_data(database)
        for credential in (data.first_credential, data.second_credential):
            channels.add_marker(credential)
            channels.add_marker(sha256(credential.encode()).hexdigest())
        channels.stage = "browser-recovery"
        await check_browser_recovery(node, root, variables, channels, "overflow")
        await check_browser_recovery(node, root, variables, channels, "forced")
        channels.stage = "listener"
        async with installed_listener(python, root, settings, material, channels) as (
            origin,
            server_pid,
        ):
            preserve = frozenset({server_pid})
            channels.stage = "surface-parity"
            environment = https_client_environment(
                origin,
                data.first_credential,
                bundle=material.authority,
            )
            second_environment = https_client_environment(
                origin,
                data.second_credential,
                bundle=material.authority,
            )
            context = ssl.create_default_context(cafile=str(material.authority))
            async with httpx.AsyncClient(
                base_url=origin,
                verify=context,
                trust_env=False,
                timeout=SERVE_TIMEOUT,
            ) as client:
                canonical = await canonical_page(
                    python, root, client, environment, data
                )
                assert canonical.next_cursor is not None
                earlier = await raw_event_page(
                    client,
                    data.run_id,
                    data.first_credential,
                    canonical.next_cursor,
                    PAGE_SIZE,
                )
                assert (
                    tuple(event.id for event in earlier.items)
                    == data.event_ids[PAGE_SIZE:]
                )
                assert earlier.next_cursor is None
            channels.stage = "native-opener"
            opener = NativeOpener.provision(root)
            await opener.open(
                python, root, environment, data.run_id, channels, mode="missing"
            )
            await opener.check_unconfirmed(
                python, root, environment, data.run_id, channels, preserve
            )
            url = await opener.open(python, root, environment, data.run_id, channels)
            assert url is not None
            channels.stage = "chromium"
            launch: JsonObject = {
                "origin": origin,
                "url": url,
                "authority": str(material.authority),
                "root": str(root),
                "output": str(capture),
                "run_id": data.run_id,
                "empty_run": data.empty_run,
                "denied_run": data.denied_run,
                "namespace": TLS_EVENTS_NAMESPACE,
                "inert_event": INERT_EVENT,
                "page": parse_json_object(canonical.model_dump(mode="json")),
                "earlier_page": parse_json_object(earlier.model_dump(mode="json")),
            }

            async def exchange(
                process: asyncio.subprocess.Process, _descriptor: int | None
            ) -> None:
                await browser_exchange(
                    process,
                    launch,
                    database,
                    python,
                    root,
                    opener,
                    environment,
                    second_environment,
                    data,
                    channels,
                )

            code, stderr = await run_node(
                node,
                WEB_ROOT / "tests/browser/run-history.ts",
                (),
                root,
                variables,
                exchange,
                preserve=preserve,
            )
            channels.captured.append(stderr)
            assert code == 0
            channels.stage = "shutdown"
    finally:
        await database.close()
        assert sha256(TRUST_BUNDLE.read_bytes()).digest() == trust
        channels.publish(capture, output)
    assert settings.sqlite_path is not None
    with closing(sqlite3.connect(settings.sqlite_path, timeout=0)) as observer:
        assert observer.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
        assert observer.execute("SELECT count(*) FROM event").fetchone() == (
            PAGE_SIZE + 1,
        )


async def run(
    python: Path,
    root: Path,
    output: Path,
    settings: DatabaseSettings,
    material: TlsMaterial,
    channels: PrivateChannels,
) -> None:
    task = asyncio.current_task()
    assert task is not None
    loop = asyncio.get_running_loop()
    for number in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(number, task.cancel)
    adopt_fixture_children()
    try:
        await exercise_browser(python, root, output, settings, material, channels)
    finally:
        await release_adopted()


def test_installed_console_browser(core_wheel: Path) -> None:
    output_root = Path(
        os.environ.get("KORA_BROWSER_OUTPUT", WEB_ROOT.parent / "measurements/browser")
    ).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix="installed-", dir=output_root))
    channels = PrivateChannels(
        {PRIVATE_PAYLOAD.encode(), OPENER_PRIVATE_MARKER.encode()}, []
    )
    try:
        with tempfile.TemporaryDirectory(prefix="kora-installed-browser-") as private:
            root = Path(private)
            python = installed_core(core_wheel, root, serve=True, mcp=True)
            settings = copy_sqlite_template(
                prepare_sqlite_template(root), root / "browser.sqlite"
            )
            material = write_tls_material(root)
            asyncio.run(run(python, root, output, settings, material, channels))
    except (
        AssertionError,
        OSError,
        RuntimeError,
        ValueError,
        TimeoutError,
        httpx.HTTPError,
        asyncio.CancelledError,
    ):
        status = "failed"
    else:
        status = "passed"
    report = json.dumps(
        {
            "event": "installed-browser",
            "status": status,
            "stage": channels.stage,
            "checks": channels.checks,
        }
    )
    (output / "fixture-report.json").write_text(report + "\n", encoding="ascii")
    sys.stdout.write(report + "\n")
    assert status == "passed"
