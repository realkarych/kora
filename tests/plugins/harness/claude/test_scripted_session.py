import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Callable, Generator
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Final, cast

import pytest

from kora.conformance.capture import Capture, captured
from kora.conformance.common import utc_now
from kora.observability import LoggingSettings
from kora.plugins.harness.claude.configuration import ClaudeEnvironmentConfiguration
from kora.plugins.harness.claude.sdk import Decision
from kora.plugins.harness.claude.session import (
    COMPONENT,
    SessionOutcome,
    close_recorded_processes,
    interpreter,
    launcher_text,
    process_rows,
    run_scripted_session,
)
from kora.protocols.activity import NoActivity
from kora.protocols.common import ProtocolContext
from kora.protocols.deadlines import NANOSECONDS_PER_SECOND
from kora.protocols.errors import KoraProtocolError
from kora.protocols.harness import Placement
from kora.protocols.time import Clock, SystemClock

PROCESS_TABLE: Final[tuple[str, ...]] = (
    "/bin/ps",
    "-axww",
    "-o",
    "pid=,ppid=,pgid=,stat=,command=",
)
PROMPT: Final[str] = "apply the tool"
KEY: Final[str] = "fixture-key-value"
SCRIPTED: Final[str] = "scripted-bytes\n"
SEEDED: Final[str] = "seeded-bytes\n"
DECISION_FAILURE: Final[str] = "decision-failure-token"
BOUND: Final[timedelta] = timedelta(seconds=45)
BODY_LIMIT: Final[int] = 8_000_000
LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset(
    {
        "127.0.0.1",
        "::1",
        "0100007F",
        "00000000000000000000000001000000",
    }
)


@pytest.fixture
def claude_capture() -> Generator[Capture]:
    with captured(COMPONENT, LoggingSettings(), "claude-session", utc_now) as capture:
        yield capture


def context_for(capture: Capture, clock: Clock) -> ProtocolContext:
    return ProtocolContext(
        namespace_id="claude-session",
        run_id="claude-run",
        attempt_id="claude-attempt",
        logger=capture.diagnostics.logger(COMPONENT),
        secrets=None,
        secret_admission=None,
        request_digest=None,
        deadline=None,
        clock=clock,
        activity=NoActivity(),
    )


def placements(root: Path) -> dict[Placement, Path]:
    return {
        "harness_home": root / "home",
        "harness_tmp": root / "home" / "tmp",
        "harness_config": root / "home" / "config",
        "harness_input": root / "input",
    }


def prepare_workspace(root: Path) -> tuple[Path, dict[str, str]]:
    paths = placements(root)
    for path in paths.values():
        path.mkdir(parents=True)
    work = root / "work"
    work.mkdir()
    (work / "seeded.txt").write_text(SEEDED, encoding="utf-8")
    configured = ClaudeEnvironmentConfiguration(
        bash_path="/bin/bash", max_retries=0, cli_telemetry=False
    )
    environment = dict(configured.launch_inputs(cwd=work, placements=paths).environment)
    environment["PATH"] = "/usr/bin:/bin"
    environment["LC_ALL"] = "C"
    return work, environment


def sse(events: list[tuple[str, dict[str, object]]]) -> bytes:
    chunks = [
        f"event: {name}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"
        for name, payload in events
    ]
    return "".join(chunks).encode("ascii")


def tool_stream(model: str, name: str, payload: dict[str, str]) -> bytes:
    message: dict[str, object] = {
        "id": "msg_scripted_tool",
        "type": "message",
        "role": "assistant",
        "content": [],
        "model": model,
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {"input_tokens": 8, "output_tokens": 1},
    }
    return sse(
        [
            ("message_start", {"type": "message_start", "message": message}),
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": "toolu_scripted",
                        "name": name,
                        "input": {},
                    },
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": json.dumps(payload, separators=(",", ":")),
                    },
                },
            ),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                    "usage": {"output_tokens": 24},
                },
            ),
            ("message_stop", {"type": "message_stop"}),
        ]
    )


def end_stream(model: str) -> bytes:
    message: dict[str, object] = {
        "id": "msg_scripted_end",
        "type": "message",
        "role": "assistant",
        "content": [],
        "model": model,
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {"input_tokens": 12, "output_tokens": 1},
    }
    return sse(
        [
            ("message_start", {"type": "message_start", "message": message}),
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "done"},
                },
            ),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 4},
                },
            ),
            ("message_stop", {"type": "message_stop"}),
        ]
    )


def mapping(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    parsed: dict[str, object] = {}
    for key, item in cast("dict[object, object]", value).items():
        if not isinstance(key, str):
            return None
        parsed[key] = item
    return parsed


def objects(value: object) -> list[object] | None:
    if not isinstance(value, list):
        return None
    return list(cast("list[object]", value))


def has_tool_result(body: bytes) -> bool:
    try:
        value: object = json.loads(body)
    except json.JSONDecodeError:
        return False
    document = mapping(value)
    if document is None:
        return False
    messages = objects(document.get("messages"))
    if messages is None:
        return False
    for message in messages:
        content = objects((mapping(message) or {}).get("content"))
        if content is None:
            continue
        for block in content:
            fields = mapping(block)
            if fields is not None and fields.get("type") == "tool_result":
                return True
    return False


class ScriptedState:
    def __init__(self, tool: str, payload: dict[str, str]) -> None:
        self.tool = tool
        self.payload = payload
        self.paths: list[str] = []
        self.clients: list[str] = []
        self.authorized: list[bool] = []
        self.lock = threading.Lock()


class ScriptedServer(ThreadingHTTPServer):
    def __init__(self, state: ScriptedState) -> None:
        self.state = state
        super().__init__(("127.0.0.1", 0), ScriptedHandler)


class ScriptedHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        return None

    def do_POST(self) -> None:
        server = self.server
        if not isinstance(server, ScriptedServer):
            self._send(500, b"", "text/plain")
            return
        host = self.client_address[0]
        length_text = self.headers.get("content-length", "")
        if not length_text.isdecimal():
            self._record(server, host, False)
            self._send(400, b"", "text/plain")
            return
        length = int(length_text)
        if length > BODY_LIMIT:
            self._record(server, host, False)
            self._send(413, b"", "text/plain")
            return
        body = self.rfile.read(length)
        authorized = self.headers.get("x-api-key") == KEY
        self._record(server, host, authorized)
        if not authorized:
            self._send(401, b"", "text/plain")
            return
        if self.path.startswith("/v1/messages/count_tokens"):
            self._send(200, b'{"input_tokens":1}', "application/json")
            return
        if not self.path.startswith("/v1/messages"):
            self._send(404, b"", "text/plain")
            return
        try:
            value: object = json.loads(body)
        except json.JSONDecodeError:
            self._send(400, b"", "text/plain")
            return
        parsed = mapping(value)
        if parsed is None or parsed.get("stream") is not True:
            self._send(400, b"", "text/plain")
            return
        model = parsed.get("model")
        model_name = model if isinstance(model, str) else "claude-sonnet-4-5"
        payload = (
            end_stream(model_name)
            if has_tool_result(body)
            else tool_stream(model_name, server.state.tool, server.state.payload)
        )
        self._send(200, payload, "text/event-stream")

    def _record(self, server: ScriptedServer, host: str, authorized: bool) -> None:
        with server.state.lock:
            server.state.paths.append(self.path)
            server.state.clients.append(host)
            server.state.authorized.append(authorized)

    def _send(self, status: int, payload: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(payload)))
        self.send_header("connection", "close")
        self.end_headers()
        self.wfile.write(payload)


def serve(state: ScriptedState) -> tuple[ScriptedServer, threading.Thread, str]:
    server = ScriptedServer(state)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{port}"


def stop_server(server: ScriptedServer, thread: threading.Thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join(5)
    if thread.is_alive():
        raise AssertionError("the scripted server did not stop")


def process_text() -> str:
    completed = subprocess.run(
        PROCESS_TABLE, check=False, capture_output=True, shell=False
    )
    if completed.returncode != 0:
        raise AssertionError("the process table was not readable")
    return completed.stdout.decode("utf-8", errors="replace")


def live_pid(launch_dir: Path) -> int:
    text = (launch_dir / "pids").read_text(encoding="ascii")
    found = 0
    for line in text.splitlines():
        if not line.isdecimal():
            continue
        pid = int(line)
        try:
            os.kill(pid, 0)
        except OSError:
            continue
        found = pid
    if found == 0:
        raise AssertionError("the CLI process was not alive")
    return found


def environment_has(text: str, name: str, value: str) -> bool:
    needle = f"{name}={value}"
    start = 0
    while True:
        index = text.find(needle, start)
        if index < 0:
            return False
        end = index + len(needle)
        before = index == 0 or text[index - 1] in " \n"
        after = end == len(text) or text[end] in " \n"
        if before and after:
            return True
        start = index + 1


def process_environment(pid: int) -> str:
    if sys.platform == "linux":
        raw = Path(f"/proc/{pid}/environ").read_bytes()
        return raw.replace(b"\0", b"\n").decode("utf-8", errors="replace")
    completed = subprocess.run(
        ["/bin/ps", "eww", "-p", str(pid), "-o", "command="],
        check=False,
        capture_output=True,
        shell=False,
    )
    if completed.returncode != 0:
        raise AssertionError("the CLI environment was not readable")
    return completed.stdout.decode("utf-8", errors="replace")


def linux_peers(pid: int) -> set[str]:
    inodes: set[str] = set()
    directory = Path(f"/proc/{pid}/fd")
    if not directory.is_dir():
        return set()
    for entry in directory.iterdir():
        try:
            target = os.readlink(entry)
        except OSError:
            continue
        if target.startswith("socket:[") and target.endswith("]"):
            inodes.add(target.removeprefix("socket:[").removesuffix("]"))
    peers: set[str] = set()
    for name in ("tcp", "tcp6"):
        table = Path("/proc/net") / name
        try:
            lines = table.read_text(encoding="ascii").splitlines()
        except OSError:
            continue
        for line in lines[1:]:
            parts = line.split()
            if len(parts) < 10 or parts[9] not in inodes:
                continue
            for address in (parts[1], parts[2]):
                if not address.endswith(":0000"):
                    peers.add(address)
    return peers


def macos_peers(pid: int) -> set[str]:
    completed = subprocess.run(
        ["/usr/sbin/lsof", "-nP", "-a", "-p", str(pid), "-iTCP", "-Fn"],
        check=False,
        capture_output=True,
        shell=False,
    )
    if completed.returncode not in (0, 1):
        raise AssertionError("tcp peers were not readable")
    return {
        line[1:]
        for line in completed.stdout.decode("ascii", errors="replace").splitlines()
        if line.startswith("n")
    }


def tcp_peers(pid: int) -> set[str]:
    if sys.platform == "linux":
        return linux_peers(pid)
    return macos_peers(pid)


def peer_is_loopback(value: str) -> bool:
    if "->" in value:
        return all(peer_is_loopback(part) for part in value.split("->"))
    host = value.rsplit(":", 1)[0]
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host in LOOPBACK_HOSTS


def command_running(marker: str, name: str) -> bool:
    rows = process_rows()
    if rows is None:
        return False
    return any(
        not row.stat.startswith("Z") and name in row.command and marker in row.command
        for row in rows
    )


async def until(predicate: Callable[[], bool], clock: Clock, deadline_ns: int) -> bool:
    while clock.monotonic_ns() < deadline_ns:
        if predicate():
            return True
        await clock.sleep_until(min(deadline_ns, clock.monotonic_ns() + 50_000_000))
    return predicate()


async def assert_quiet(marker: str, clock: Clock) -> None:
    deadline = clock.monotonic_ns() + 2 * NANOSECONDS_PER_SECOND
    while clock.monotonic_ns() < deadline:
        if marker not in process_text():
            return
        await clock.sleep_until(min(deadline, clock.monotonic_ns() + 50_000_000))
    if marker in process_text():
        raise AssertionError("a child process was still running")


def assert_records(
    records: list[dict[str, object]], outcome: str, secrets: tuple[str, ...]
) -> None:
    finished = [
        record
        for record in records
        if record.get("event") == "harness.claude.session_finished"
    ]
    failed = [
        record
        for record in records
        if record.get("event") == "harness.claude.session_failed"
    ]
    assert len(finished) == 1
    assert finished[0].get("outcome") == outcome
    assert failed == []
    rendered = "\n".join(json.dumps(record, sort_keys=True) for record in records)
    for secret in secrets:
        if secret in rendered:
            raise AssertionError("session content reached a diagnostic record")


def assert_exchange(state: ScriptedState, peers: set[str]) -> None:
    with state.lock:
        paths = list(state.paths)
        clients = list(state.clients)
        authorized = list(state.authorized)
    assert any(path.startswith("/v1/messages") for path in paths)
    assert clients
    assert set(clients) == {"127.0.0.1"}
    assert authorized
    assert all(authorized)
    assert all(peer_is_loopback(peer) for peer in peers)


def observe(launch_dir: Path, peers: set[str]) -> str:
    pid = live_pid(launch_dir)
    peers.update(tcp_peers(pid))
    return process_environment(pid)


@pytest.mark.asyncio
async def test_scripted_claude_write_changes_the_file_when_allowed(
    tmp_path: Path, claude_capture: Capture
) -> None:
    clock = SystemClock()
    work, environment = prepare_workspace(tmp_path)
    target = work / "target.txt"
    launch_dir = tmp_path / "launch"
    calls: list[str] = []
    peers: set[str] = set()
    observed: list[str] = []
    state = ScriptedState("Write", {"file_path": str(target), "content": SCRIPTED})
    server, thread, base_url = serve(state)

    def decide(name: str) -> Decision:
        calls.append(name)
        if name != "Write":
            return "deny"
        observed.append(observe(launch_dir, peers))
        return "allow"

    try:
        outcome = await run_scripted_session(
            context_for(claude_capture, clock),
            cwd=work,
            launch_dir=launch_dir,
            base_url=base_url,
            api_key=KEY,
            environment=environment,
            prompt=PROMPT,
            tools=("Write",),
            decide=decide,
            stop=None,
            bound=BOUND,
        )
    finally:
        stop_server(server, thread)

    assert outcome is SessionOutcome.COMPLETED
    assert target.read_text(encoding="utf-8") == SCRIPTED
    assert (work / "seeded.txt").read_text(encoding="utf-8") == SEEDED
    assert calls == ["Write"]
    assert observed
    text = observed[0]
    if not environment_has(text, "HOME", environment["HOME"]):
        raise AssertionError("the harness home was absent")
    if not environment_has(text, "ANTHROPIC_BASE_URL", base_url):
        raise AssertionError("the loopback base URL was absent")
    parent_home = os.environ.get("HOME")
    if (
        parent_home is not None
        and parent_home != environment["HOME"]
        and environment_has(text, "HOME", parent_home)
    ):
        raise AssertionError("the parent home reached the CLI")
    parent_key = os.environ.get("ANTHROPIC_API_KEY")
    if (
        parent_key is not None
        and parent_key != KEY
        and environment_has(text, "ANTHROPIC_API_KEY", parent_key)
    ):
        raise AssertionError("the parent API key reached the CLI")
    assert_exchange(state, peers)
    await assert_quiet(str(work), clock)
    await assert_quiet(str(launch_dir), clock)
    assert not (launch_dir / "environment.json").exists()

    records = claude_capture.finish()
    assert_records(records, "completed", (KEY, PROMPT, SCRIPTED, str(target)))


@pytest.mark.asyncio
async def test_scripted_claude_write_leaves_the_file_untouched_when_denied(
    tmp_path: Path, claude_capture: Capture
) -> None:
    clock = SystemClock()
    work, environment = prepare_workspace(tmp_path)
    target = work / "denied.txt"
    launch_dir = tmp_path / "launch"
    calls: list[str] = []
    state = ScriptedState("Write", {"file_path": str(target), "content": SCRIPTED})
    server, thread, base_url = serve(state)

    def decide(name: str) -> Decision:
        calls.append(name)
        return "deny"

    try:
        outcome = await run_scripted_session(
            context_for(claude_capture, clock),
            cwd=work,
            launch_dir=launch_dir,
            base_url=base_url,
            api_key=KEY,
            environment=environment,
            prompt=PROMPT,
            tools=("Write",),
            decide=decide,
            stop=None,
            bound=BOUND,
        )
    finally:
        stop_server(server, thread)

    assert outcome is SessionOutcome.COMPLETED
    assert calls == ["Write"]
    assert not target.exists()
    assert (work / "seeded.txt").read_text(encoding="utf-8") == SEEDED
    await assert_quiet(str(work), clock)
    await assert_quiet(str(launch_dir), clock)

    records = claude_capture.finish()
    assert_records(records, "completed", (KEY, PROMPT, SCRIPTED, str(target)))


@pytest.mark.asyncio
async def test_scripted_claude_decision_failure_leaves_the_file_untouched(
    tmp_path: Path, claude_capture: Capture
) -> None:
    clock = SystemClock()
    work, environment = prepare_workspace(tmp_path)
    target = work / "failed.txt"
    launch_dir = tmp_path / "launch"
    state = ScriptedState("Write", {"file_path": str(target), "content": SCRIPTED})
    server, thread, base_url = serve(state)

    def decide(_name: str) -> Decision:
        raise RuntimeError(DECISION_FAILURE)

    try:
        with pytest.raises(KoraProtocolError) as caught:
            await run_scripted_session(
                context_for(claude_capture, clock),
                cwd=work,
                launch_dir=launch_dir,
                base_url=base_url,
                api_key=KEY,
                environment=environment,
                prompt=PROMPT,
                tools=("Write",),
                decide=decide,
                stop=None,
                bound=BOUND,
            )
    finally:
        stop_server(server, thread)

    assert caught.value.code == "harness_error"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    if DECISION_FAILURE in str(caught.value):
        raise AssertionError("the decision exception reached the error text")
    assert not target.exists()
    assert (work / "seeded.txt").read_text(encoding="utf-8") == SEEDED
    await assert_quiet(str(work), clock)
    await assert_quiet(str(launch_dir), clock)
    assert not (launch_dir / "environment.json").exists()

    records = claude_capture.finish()
    failed = [
        record
        for record in records
        if record.get("event") == "harness.claude.session_failed"
    ]
    finished = [
        record
        for record in records
        if record.get("event") == "harness.claude.session_finished"
    ]
    assert finished == []
    assert len(failed) == 1
    assert failed[0].get("reason") == "session_failed"
    rendered = "\n".join(json.dumps(record, sort_keys=True) for record in records)
    if DECISION_FAILURE in rendered or KEY in rendered or SCRIPTED in rendered:
        raise AssertionError("session content reached a diagnostic record")


@pytest.mark.asyncio
async def test_scripted_claude_cancellation_closes_the_tool_child(
    tmp_path: Path, claude_capture: Capture
) -> None:
    clock = SystemClock()
    work, environment = prepare_workspace(tmp_path)
    target = work / "cancelled.txt"
    launch_dir = tmp_path / "launch"
    command = (
        "perl -e 'sleep 20; open my $f, q(>), q(" + str(target) + "); print $f q(ran)'"
    )
    calls: list[str] = []
    stop = asyncio.Event()
    state = ScriptedState("Bash", {"command": command})
    server, thread, base_url = serve(state)

    def decide(name: str) -> Decision:
        calls.append(name)
        return "allow" if name == "Bash" else "deny"

    async def arm() -> None:
        deadline = clock.monotonic_ns() + 30 * NANOSECONDS_PER_SECOND
        if not await until(lambda: command_running(str(work), "perl"), clock, deadline):
            raise AssertionError("the tool child did not start")
        stop.set()

    watcher = asyncio.create_task(arm())
    try:
        outcome = await run_scripted_session(
            context_for(claude_capture, clock),
            cwd=work,
            launch_dir=launch_dir,
            base_url=base_url,
            api_key=KEY,
            environment=environment,
            prompt=PROMPT,
            tools=("Bash",),
            decide=decide,
            stop=stop,
            bound=BOUND,
        )
        await watcher
    finally:
        if not watcher.done():
            watcher.cancel()
        stop_server(server, thread)

    assert outcome is SessionOutcome.CANCELLED
    assert calls == ["Bash"]
    assert not target.exists()
    assert (work / "seeded.txt").read_text(encoding="utf-8") == SEEDED
    await assert_quiet(str(work), clock)
    await assert_quiet(str(launch_dir), clock)

    records = claude_capture.finish()
    assert_records(records, "cancelled", (KEY, PROMPT, "ran", str(target)))


def deny_tool(_name: str) -> Decision:
    return "deny"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "base_url",
    [
        "http://example",
        "http://127.0.0.1",
        "http://localhost:9",
        "http://[::1]:9",
        "https://127.0.0.1:9",
        "http://127.0.0.1:notaport",
    ],
)
async def test_scripted_claude_session_refuses_a_non_loopback_endpoint(
    tmp_path: Path, claude_capture: Capture, base_url: str
) -> None:
    clock = SystemClock()
    work, environment = prepare_workspace(tmp_path)
    launch_dir = tmp_path / "launch"

    with pytest.raises(KoraProtocolError) as caught:
        await run_scripted_session(
            context_for(claude_capture, clock),
            cwd=work,
            launch_dir=launch_dir,
            base_url=base_url,
            api_key=KEY,
            environment=environment,
            prompt=PROMPT,
            tools=("Write",),
            decide=deny_tool,
            stop=None,
            bound=timedelta(seconds=1),
        )

    assert caught.value.code == "launch_rejected"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    if base_url in str(caught.value):
        raise AssertionError("the refused endpoint reached the error text")
    assert not launch_dir.exists()
    assert str(launch_dir) not in process_text()

    records = claude_capture.finish()
    failed = [
        record
        for record in records
        if record.get("event") == "harness.claude.session_failed"
    ]
    assert len(failed) == 1
    assert failed[0].get("reason") == "launch_rejected"
    rendered = "\n".join(json.dumps(record, sort_keys=True) for record in records)
    if KEY in rendered or base_url in rendered:
        raise AssertionError("session content reached a diagnostic record")


@pytest.mark.asyncio
async def test_scripted_claude_session_kills_a_child_that_ignores_termination(
    tmp_path: Path,
) -> None:
    clock = SystemClock()
    launch_dir = tmp_path / "launch"
    launch_dir.mkdir()
    hold = launch_dir / "hold.py"
    ready = launch_dir / "ready"
    hold.write_text(
        "import pathlib\nimport signal\nimport time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "pathlib.Path(" + repr(str(ready)) + ").write_text('ready', encoding='ascii')\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    process = subprocess.Popen(
        [sys.executable, str(hold)],
        cwd=launch_dir,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    (launch_dir / "pids").write_text(f"{process.pid}\n", encoding="ascii")

    try:
        deadline = clock.monotonic_ns() + 2 * NANOSECONDS_PER_SECOND
        while not ready.is_file() and clock.monotonic_ns() < deadline:
            await clock.sleep_until(min(deadline, clock.monotonic_ns() + 50_000_000))
        if not ready.is_file():
            raise AssertionError("the child did not ignore termination")
        closed = await close_recorded_processes(launch_dir, sys.executable, clock)
        finished = process.wait(timeout=2)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)

    assert closed is True
    assert finished == -signal.SIGKILL
    assert process.returncode == -signal.SIGKILL


def test_scripted_launcher_starts_isolated_and_skips_the_version_probe(
    tmp_path: Path,
) -> None:
    executable = interpreter()
    poison = tmp_path / "poison"
    poison.mkdir()
    marker = tmp_path / "poisoned"
    (poison / "json.py").write_text(
        "import pathlib\npathlib.Path("
        + repr(str(marker))
        + ").write_text('x', encoding='utf-8')\n",
        encoding="utf-8",
    )
    launch = tmp_path / "launch"
    launch.mkdir()
    ran = tmp_path / "ran"
    child = tmp_path / "child.py"
    child.write_text(
        f"#!{executable}\nimport pathlib\nimport sys\n"
        "pathlib.Path(sys.argv[1]).write_text('ran', encoding='utf-8')\n",
        encoding="utf-8",
    )
    child.chmod(0o700)
    (launch / "binary").write_text(f"{child}\n", encoding="utf-8")
    (launch / "environment.json").write_text(
        '{"PATH": "/usr/bin:/bin"}\n', encoding="utf-8"
    )
    launcher = tmp_path / "launcher"
    launcher.write_text(launcher_text(executable), encoding="utf-8")
    launcher.chmod(0o700)
    inherited = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(poison),
        "KORA_CLAUDE_LAUNCH_DIR": str(launch),
    }

    version = subprocess.run(
        [str(launcher), "-v"],
        check=False,
        capture_output=True,
        env=inherited,
        shell=False,
    )
    assert version.returncode == 0
    assert not (launch / "pids").exists()
    assert not marker.exists()

    completed = subprocess.run(
        [str(launcher), str(ran)],
        check=False,
        capture_output=True,
        env=inherited,
        shell=False,
    )
    assert completed.returncode == 0
    assert ran.read_text(encoding="utf-8") == "ran"
    assert not marker.exists()


@pytest.mark.asyncio
async def test_scripted_cleanup_leaves_a_process_that_only_names_the_workspace(
    tmp_path: Path,
) -> None:
    clock = SystemClock()
    launch_dir = tmp_path / "launch"
    launch_dir.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    hold = work / "hold.py"
    hold.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, str(hold)],
        cwd=work,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        closed = await close_recorded_processes(launch_dir, "/bundled/claude", clock)
        assert closed is True
        assert process.poll() is None
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)


async def process_gone(pid: int, clock: Clock) -> bool:
    deadline = clock.monotonic_ns() + NANOSECONDS_PER_SECOND
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        if clock.monotonic_ns() >= deadline:
            return False
        await clock.sleep_until(min(deadline, clock.monotonic_ns() + 50_000_000))


@pytest.mark.asyncio
async def test_scripted_cleanup_kills_an_unmarked_member_that_ignores_termination(
    tmp_path: Path,
) -> None:
    clock = SystemClock()
    launch_dir = tmp_path / "launch"
    launch_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    ready = outside / "ready"
    child_script = outside / "child.py"
    child_script.write_text(
        "import os\nimport pathlib\nimport signal\nimport sys\nimport time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "ready = pathlib.Path(sys.argv[1])\n"
        "staged = ready.with_suffix('.pending')\n"
        "staged.write_text(str(os.getpid()), encoding='ascii')\n"
        "staged.replace(ready)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    leader_script = outside / "leader.py"
    leader_script.write_text(
        "import subprocess\nimport sys\nimport time\n"
        "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    leader = subprocess.Popen(
        [sys.executable, str(leader_script), str(child_script), str(ready)],
        cwd=outside,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    (launch_dir / "pids").write_text(f"{leader.pid}\n", encoding="ascii")
    child_pid = 0

    try:
        deadline = clock.monotonic_ns() + 2 * NANOSECONDS_PER_SECOND
        while not ready.is_file() and clock.monotonic_ns() < deadline:
            await clock.sleep_until(min(deadline, clock.monotonic_ns() + 50_000_000))
        if not ready.is_file():
            raise AssertionError("the unmarked child did not start")
        child_pid = int(ready.read_text(encoding="ascii"))

        closed = await close_recorded_processes(launch_dir, sys.executable, clock)
        assert closed is True
        assert await process_gone(child_pid, clock)
    finally:
        if leader.poll() is None:
            os.killpg(leader.pid, signal.SIGKILL)
            leader.wait(timeout=2)
        if child_pid > 0:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
