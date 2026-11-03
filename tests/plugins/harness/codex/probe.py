import asyncio
import json
import os
import platform
import re
import signal
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal, cast

Case = Literal["allow", "deny", "stall", "interactive"]
ProviderCase = Literal[
    "implicit_account", "provider_key_present", "provider_key_absent"
]
ProbeCase = Case | ProviderCase

CODEX = "/usr/local/bin/codex"
PROVIDER_CASES = frozenset(
    {"implicit_account", "provider_key_present", "provider_key_absent"}
)
SYNTHETIC_BEARER = "kora-probe-synthetic-value"
MISSING_PROVIDER_KEY = "Missing environment variable: `CODEX_API_KEY`."
CONTROL = Path("/var/lib/kora-hook-probe/control")
RECEIPTS = CONTROL / "receipts.jsonl"
MARKER = "kora-probe.marker"
COMMAND = f"touch {MARKER}"
CALL_ID = "kora-hook-probe-call"
STDIN_CALL_ID = "kora-hook-probe-stdin"
STDIN_READY = "kora-stdin-ready"
STDIN_WRITTEN = "kora-stdin-written"
STDIN_COMMAND = (
    f"printf '{STDIN_READY}\\n'; IFS= read -r token && "
    'test "$token" = kora-stdin-token && '
    f"printf '{STDIN_WRITTEN}\\n' > {MARKER} && cat {MARKER}"
)
SESSION_ID = re.compile(r"^Process running with session ID ([0-9]{1,10})$")
DENIAL = (
    "Command blocked by PreToolUse hook: "
    f"kora managed hook probe denied the marker command. Command: {COMMAND}"
)
TURN_SECONDS = 30
CLEANUP_SECONDS = 5
MAX_FRAME_BYTES = 131_072
MAX_MESSAGES = 256
HTTP_FIELD_NAME = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+")


class ProbeFailure(Exception):
    pass


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise ProbeFailure(reason)


def as_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ProbeFailure("expected_json_object")
    return cast(dict[str, object], value)


def as_list(value: object, reason: str) -> list[object]:
    if not isinstance(value, list):
        raise ProbeFailure(reason)
    return cast(list[object], value)


def parse_object(raw: bytes) -> dict[str, object]:
    return as_object(json.loads(raw))


def diagnostic(level: str, event: str, detail: str) -> None:
    sys.stderr.write(
        json.dumps({"level": level, "event": event, "detail": detail}) + "\n"
    )


def sse(response_id: str, item: dict[str, object]) -> bytes:
    events = (
        {"type": "response.created", "response": {"id": response_id}},
        {"type": "response.output_item.done", "item": item},
        {
            "type": "response.completed",
            "response": {
                "id": response_id,
                "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            },
        },
    )
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
    ).encode()


def command_response(command: str, interactive: bool = False) -> bytes:
    arguments: dict[str, object] = {"cmd": command, "login": False}
    if interactive:
        arguments.update(tty=True, yield_time_ms=1_000)
    return sse(
        "kora-probe-response-1",
        {
            "type": "function_call",
            "call_id": CALL_ID,
            "name": "exec_command",
            "arguments": json.dumps(arguments),
        },
    )


def stdin_response(session_id: int) -> bytes:
    return sse(
        "kora-probe-stdin",
        {
            "type": "function_call",
            "call_id": STDIN_CALL_ID,
            "name": "write_stdin",
            "arguments": json.dumps(
                {"session_id": session_id, "chars": "kora-stdin-token\n"}
            ),
        },
    )


def tool_output(request: dict[str, object], call_id: str) -> str:
    items = [
        as_object(item)
        for item in as_list(request.get("input"), "responses_input_missing")
    ]
    outputs = [
        item.get("output")
        for item in items
        if item.get("type") == "function_call_output" and item.get("call_id") == call_id
    ]
    if len(outputs) != 1 or not isinstance(output := outputs[0], str):
        raise ProbeFailure("missing_correlated_tool_output")
    return output


def interactive_session_id(request: dict[str, object]) -> int:
    lines = tool_output(request, CALL_ID).splitlines()
    require(STDIN_READY in lines, "interactive_command_not_ready")
    matches = [match for line in lines if (match := SESSION_ID.fullmatch(line))]
    require(len(matches) == 1, "missing_interactive_session_id")
    session_id = int(matches[0][1])
    require(0 < session_id < 2**31, "invalid_interactive_session_id")
    return session_id


def verify_one_shot_schema(command_tool: dict[str, object]) -> None:
    parameters = as_object(command_tool.get("parameters"))
    properties = as_object(parameters.get("properties"))
    require(
        "timeout_ms" in properties and not {"tty", "yield_time_ms"} & properties.keys(),
        "one_shot_command_schema_missing",
    )


TOOL_RESPONSE = command_response(COMMAND)
FINAL_RESPONSE = sse(
    "kora-probe-response-2",
    {
        "type": "message",
        "role": "assistant",
        "id": "kora-probe-message",
        "content": [{"type": "output_text", "text": "Probe turn finished."}],
    },
)


class ResponsesFixture:
    def __init__(self, case: ProbeCase, one_shot: bool) -> None:
        self.case: ProbeCase = case
        self.one_shot = one_shot
        self.request_limit: int
        if case in PROVIDER_CASES:
            self.request_limit = int(case == "provider_key_present")
        else:
            self.request_limit = (
                3 if case == "interactive" or (one_shot and case == "allow") else 2
            )
        self.connections: int = 0
        self.requests = 0
        self.failure: str | None = None
        self.clients: list[tuple[asyncio.StreamWriter, asyncio.Task[None]]] = []

    def accept(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.connections += 1
        if len(self.clients) >= self.request_limit:
            self.failure = "unexpected_responses_connection"
            writer.close()
            return
        self.clients.append((writer, asyncio.create_task(self.handle(reader, writer))))

    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            async with asyncio.timeout(TURN_SECONDS):
                raw_headers = await reader.readuntil(b"\r\n\r\n")
                request_line, *headers = raw_headers[:-4].split(b"\r\n")
                require(
                    request_line == b"POST /v1/responses HTTP/1.1",
                    "unexpected_responses_route",
                )
                fields = self.verify_headers(headers)
                size = int(fields.get(b"content-length", b"0"))
                require(0 < size <= MAX_FRAME_BYTES, "invalid_responses_size")
                request = parse_object(await reader.readexactly(size))
                response = self.respond(request)
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                    b"Connection: close\r\nContent-Length: "
                    + str(len(response)).encode()
                    + b"\r\n\r\n"
                    + response
                )
                await writer.drain()
        except ProbeFailure as error:
            self.failure = str(error)
        except TimeoutError:
            self.failure = "responses_request_deadline"
        except (
            OSError,
            ValueError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
        ):
            self.failure = "invalid_responses_request"
        finally:
            writer.close()

    def verify_headers(self, headers: list[bytes]) -> dict[bytes, bytes]:
        fields: dict[bytes, bytes] = {}
        authorization_count = 0
        for header in headers:
            key, separator, value = header.partition(b":")
            require(
                separator == b":" and HTTP_FIELD_NAME.fullmatch(key) is not None,
                "invalid_responses_header",
            )
            name = key.lower()
            if name == b"authorization":
                authorization_count += 1
            fields[name] = value.strip(b" \t")
        require(
            not {
                b"cookie",
                b"proxy-authorization",
                b"api-key",
                b"x-api-key",
                b"x-openai-actor-authorization",
            }
            & fields.keys(),
            "unexpected_responses_credentials",
        )
        if self.case == "provider_key_present":
            require(
                authorization_count == 1
                and fields[b"authorization"] == f"Bearer {SYNTHETIC_BEARER}".encode(),
                "unexpected_provider_authorization",
            )
        else:
            require(authorization_count == 0, "unexpected_responses_credentials")
        return fields

    def respond(self, request: dict[str, object]) -> bytes:
        require(request.get("stream") is True, "responses_stream_not_requested")
        self.requests += 1
        if self.case == "provider_key_present":
            require(self.requests == 1, "unexpected_responses_request")
            return FINAL_RESPONSE
        if self.requests == 1:
            tools = [
                as_object(tool)
                for tool in as_list(request.get("tools"), "responses_tools_missing")
            ]
            command_tools = [
                tool for tool in tools if tool.get("name") == "exec_command"
            ]
            require(len(command_tools) == 1, "exec_command_advertisement_count")
            names = [tool.get("name") for tool in tools]
            if self.case == "interactive":
                require("write_stdin" in names, "interactive_stdin_not_advertised")
                return command_response(STDIN_COMMAND, interactive=True)
            if self.one_shot:
                require("write_stdin" not in names, "one_shot_stdin_advertised")
                verify_one_shot_schema(command_tools[0])
                if self.case == "allow":
                    return stdin_response(1)
            return TOOL_RESPONSE

        require(self.requests <= self.request_limit, "unexpected_responses_request")
        require(self.case != "stall", "stalled_hook_returned")
        if self.case == "interactive":
            if self.requests == 2:
                return stdin_response(interactive_session_id(request))
            require(
                STDIN_WRITTEN in tool_output(request, STDIN_CALL_ID).splitlines(),
                "stdin_did_not_complete_marker_write",
            )
            return FINAL_RESPONSE
        if self.one_shot and self.case == "allow" and self.requests == 2:
            require(
                tool_output(request, STDIN_CALL_ID) == "unsupported call: write_stdin",
                "one_shot_stdin_refusal_missing",
            )
            return TOOL_RESPONSE

        require(
            (DENIAL in tool_output(request, CALL_ID)) == (self.case == "deny"),
            "unexpected_hook_denial_result",
        )
        return FINAL_RESPONSE

    async def close(self, server: asyncio.Server) -> None:
        server.close()
        for writer, task in self.clients:
            writer.close()
            task.cancel()
        if self.clients:
            await asyncio.wait([task for _, task in self.clients])
        for writer, task in self.clients:
            if not task.cancelled():
                task.result()
            await writer.wait_closed()
        await server.wait_closed()


class Stdio:
    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.messages = 0
        self.terminal: dict[str, object] | None = None

    async def send(self, message: dict[str, object]) -> None:
        self.writer.write(json.dumps({"jsonrpc": "2.0", **message}).encode() + b"\n")
        await self.writer.drain()

    async def receive(self) -> dict[str, object]:
        self.messages += 1
        require(self.messages <= MAX_MESSAGES, "app_server_message_limit")
        line = await self.reader.readline()
        require(bool(line) and line.endswith(b"\n"), "app_server_stdio_closed")
        message = parse_object(line)
        if "method" in message and "id" in message:
            raise ProbeFailure("unexpected_app_server_request")
        if message.get("method") == "turn/completed":
            require(self.terminal is None, "duplicate_turn_completion")
            self.terminal = as_object(message.get("params"))
        return message

    async def call(
        self, request_id: int, method: str, params: dict[str, object]
    ) -> dict[str, object]:
        await self.send({"id": request_id, "method": method, "params": params})
        while True:
            response = await self.receive()
            if response.get("id") == request_id:
                require("error" not in response, f"{method}_rejected")
                return as_object(response.get("result"))

    async def observe(self, workspace: Path, case: ProbeCase) -> None:
        await self.call(
            1, "initialize", {"clientInfo": {"name": "kora-hook-probe", "version": "1"}}
        )
        await self.send({"method": "initialized"})
        if case == "implicit_account":
            account = await self.call(2, "account/read", {"refreshToken": False})
            require(
                "account" in account
                and account["account"] is None
                and account.get("requiresOpenaiAuth") is True,
                "unexpected_implicit_account_state",
            )
            return
        await self.run_turn(workspace, case)

    async def run_turn(self, workspace: Path, case: ProbeCase) -> None:
        thread_params: dict[str, object] = {"cwd": str(workspace)}
        prompt = "Run the probe marker command."
        if case in PROVIDER_CASES:
            thread_params["ephemeral"] = True
            prompt = "Complete the fixture turn."
        start = await self.call(2, "thread/start", thread_params)
        thread = as_object(start.get("thread"))
        if case in PROVIDER_CASES:
            require(thread.get("ephemeral") is True, "provider_thread_not_ephemeral")
        thread_id = thread.get("id")
        require(isinstance(thread_id, str) and bool(thread_id), "missing_thread_id")
        started = await self.call(
            3,
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt}],
            },
        )
        turn_id = as_object(started.get("turn")).get("id")
        require(isinstance(turn_id, str) and bool(turn_id), "missing_turn_id")
        while self.terminal is None:
            await self.receive()
        require(
            self.terminal.get("threadId") == thread_id, "unexpected_terminal_thread"
        )
        turn = as_object(self.terminal.get("turn"))
        require(turn.get("id") == turn_id, "unexpected_terminal_turn")
        if case == "provider_key_absent":
            require(turn.get("status") == "failed", "missing_provider_key_not_refused")
            require(
                as_object(turn.get("error")).get("message") == MISSING_PROVIDER_KEY,
                "unexpected_missing_provider_key_error",
            )
        else:
            require(turn.get("status") == "completed", "turn_not_completed")


def environment(root: Path, port: int, case: ProbeCase) -> dict[str, str]:
    home = root / "home"
    codex_home = root / "codex"
    home.mkdir()
    codex_home.mkdir()
    provider = "openai" if case == "implicit_account" else "kora_probe"
    account_settings = ""
    if case in PROVIDER_CASES:
        account_settings = 'cli_auth_credentials_store = "file"\n'
        require(not os.path.lexists(codex_home / "auth.json"), "provider_store_exists")
    if case == "implicit_account":
        account_settings += f'openai_base_url = "http://127.0.0.1:{port}/v1"\n'
    config = f"""model = "gpt-5.5"
model_provider = "{provider}"
approval_policy = "never"
sandbox_mode = "danger-full-access"
web_search = "disabled"
{account_settings}
[analytics]
enabled = false

[features]
hooks = true
unified_exec = true
"""

    if case != "implicit_account":
        config += f"""
[model_providers.kora_probe]
name = "Kora loopback fixture"
base_url = "http://127.0.0.1:{port}/v1"
wire_api = "responses"
requires_openai_auth = false
supports_websockets = false
request_max_retries = 0
stream_max_retries = 0
"""
        if case in PROVIDER_CASES:
            config += 'env_key = "CODEX_API_KEY"\n'
    (codex_home / "config.toml").write_text(config, encoding="utf-8")
    child_env = {
        "HOME": str(home),
        "CODEX_HOME": str(codex_home),
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "SHELL": "/bin/bash",
        "LANG": "C.UTF-8",
    }
    if case in {"implicit_account", "provider_key_present"}:
        child_env["CODEX_API_KEY"] = SYNTHETIC_BEARER
    return child_env


def hook_pid(command: str = COMMAND) -> int:
    require(RECEIPTS.is_file(), "hook_receipt_missing")
    with RECEIPTS.open("rb") as file:
        raw = file.read(4097)
    require(len(raw) <= 4096, "hook_receipt_size_limit")
    lines = raw.splitlines()
    require(len(lines) == 1, "hook_receipt_count")
    receipt = parse_object(lines[0])
    pid = receipt.pop("pid", None)
    require(
        receipt
        == {"event": "PreToolUse", "tool": "Bash", "call": CALL_ID, "command": command},
        "unexpected_hook_receipt",
    )
    if type(pid) is not int or pid <= 1:
        raise ProbeFailure("invalid_hook_pid")
    return pid


def verify_live_hook() -> None:
    pid = hook_pid()
    try:
        state = (
            Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rpartition(") ")[2]
        )
    except FileNotFoundError:
        raise ProbeFailure("stalled_hook_exited_early") from None
    require(state.startswith(("S ", "R ", "D ")), "stalled_hook_not_live")


def signal_app_server(process: asyncio.subprocess.Process, sig: signal.Signals) -> None:
    if process.returncode is None:
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass


async def drain(reader: asyncio.StreamReader) -> None:
    while await reader.read(16_384):
        pass


async def stop_app_server(process: asyncio.subprocess.Process) -> None:
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.close()
    signal_app_server(process, signal.SIGTERM)
    try:
        async with asyncio.timeout(2):
            await asyncio.gather(process.wait(), drain(process.stdout))
    except TimeoutError:
        signal_app_server(process, signal.SIGKILL)
        await asyncio.gather(process.wait(), drain(process.stdout))


async def drive(fixture: ResponsesFixture, root: Path, workspace: Path) -> None:
    deadline = asyncio.get_running_loop().time() + TURN_SECONDS
    async with asyncio.timeout_at(deadline):
        server = await asyncio.start_server(
            fixture.accept, "127.0.0.1", 0, limit=16_384
        )
    process: asyncio.subprocess.Process | None = None
    try:
        port = int(server.sockets[0].getsockname()[1])
        args = ["app-server", "--listen", "stdio://"]
        if fixture.case in PROVIDER_CASES:
            args.append("--strict-config")
        async with asyncio.timeout_at(deadline):
            process = await asyncio.create_subprocess_exec(
                CODEX,
                *args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                limit=MAX_FRAME_BYTES,
                start_new_session=True,
                cwd=workspace,
                env=environment(root, port, fixture.case),
            )
            assert process.stdin is not None
            assert process.stdout is not None
            await Stdio(process.stdout, process.stdin).observe(workspace, fixture.case)
        require(fixture.case != "stall", "stalled_turn_completed")
    except TimeoutError:
        if fixture.case != "stall":
            raise ProbeFailure(fixture.failure or "turn_deadline_expired") from None
        require(
            fixture.failure is None and fixture.requests == 1, "stalled_fixture_state"
        )
        verify_live_hook()
    except ProbeFailure:
        if fixture.failure is not None:
            raise ProbeFailure(fixture.failure) from None
        raise
    finally:
        cleanup_deadline = asyncio.get_running_loop().time() + CLEANUP_SECONDS
        if process is not None and fixture.case in PROVIDER_CASES:
            try:
                async with asyncio.timeout_at(cleanup_deadline):
                    await stop_app_server(process)
            finally:
                async with asyncio.timeout_at(cleanup_deadline):
                    await fixture.close(server)
        else:
            async with asyncio.timeout_at(cleanup_deadline):
                if process is None:
                    await fixture.close(server)
                else:
                    outcomes = await asyncio.gather(
                        stop_app_server(process),
                        fixture.close(server),
                        return_exceptions=True,
                    )
                    for outcome in outcomes:
                        if isinstance(outcome, BaseException):
                            raise outcome


async def run_case(case: Case, one_shot: bool = False) -> None:
    RECEIPTS.unlink(missing_ok=True)
    (CONTROL / "decision").write_text(case, encoding="utf-8")
    with TemporaryDirectory(prefix="kora-codex-", dir="/tmp") as scratch:
        root = Path(scratch)
        workspace = root / "workspace"
        workspace.mkdir()
        marker = workspace / MARKER
        fixture = ResponsesFixture(case, one_shot)
        await drive(fixture, root, workspace)
        require(fixture.failure is None, fixture.failure or "unexpected_fixture_state")
        if case != "stall":
            require(
                fixture.requests == fixture.request_limit,
                "unexpected_fixture_request_count",
            )
            hook_pid(STDIN_COMMAND if case == "interactive" else COMMAND)
        if case in {"allow", "interactive"}:
            require(
                marker.is_file() and not marker.is_symlink(), "allowed_marker_missing"
            )
        else:
            require(not os.path.lexists(marker), "blocked_marker_exists")
        if case == "interactive":
            with (CONTROL / "invocations").open("rb") as invocations:
                require(
                    invocations.read(2) == b"1", "interactive_hook_invocation_count"
                )
            with marker.open("rb") as contents:
                expected = f"{STDIN_WRITTEN}\n".encode()
                require(
                    contents.read(len(expected) + 1) == expected,
                    "interactive_marker_content",
                )
    diagnostic("INFO", "probe.case_verified", f"one_shot_{case}" if one_shot else case)


async def run_provider_case(case: ProviderCase) -> None:
    with TemporaryDirectory(prefix="kora-codex-", dir="/tmp") as scratch:
        root = Path(scratch)
        workspace = root / "workspace"
        workspace.mkdir()
        fixture = ResponsesFixture(case, one_shot=False)
        await drive(fixture, root, workspace)
        require(fixture.failure is None, fixture.failure or "unexpected_fixture_state")
        require(
            fixture.connections == fixture.requests == fixture.request_limit,
            "unexpected_provider_request_count",
        )
        require(
            not os.path.lexists(root / "codex" / "auth.json"),
            "provider_store_created",
        )
    diagnostic("INFO", "probe.case_verified", case)


async def run() -> None:
    require(
        sys.platform == "linux" and platform.machine() == "x86_64",
        "unexpected_platform",
    )
    CONTROL.mkdir(mode=0o700)
    if sys.argv[1:] == ["--check-cleanup"]:
        await run_case("stall")
    elif sys.argv[1:] == ["--stdin-control"]:
        await run_case("interactive")
    elif sys.argv[1:] == ["--stdin-one-shot"]:
        await run_case("allow", one_shot=True)
        await run_case("deny", one_shot=True)
    elif sys.argv[1:] == ["--provider-account"]:
        await run_provider_case("implicit_account")
    elif sys.argv[1:] == ["--provider-env-key"]:
        await run_provider_case("provider_key_present")
        await run_provider_case("provider_key_absent")
    else:
        require(not sys.argv[1:], "unexpected_arguments")
        await run_case("allow")
        await run_case("deny")


def main() -> int:
    try:
        asyncio.run(run())
    except ProbeFailure as error:
        diagnostic("ERROR", "probe.case_failed", str(error))
        return 1
    except TimeoutError:
        diagnostic("ERROR", "probe.case_failed", "cleanup_deadline_expired")
        return 1
    except (OSError, ValueError, IndexError):
        diagnostic("ERROR", "probe.case_failed", "invalid_fixture_io")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
