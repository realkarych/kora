import asyncio
import base64
import json
import logging
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlsplit

from aiohttp import ClientSession, WSMsgType, WSServerHandshakeError, web

from kora.plugins.harness.codex.app_server_rpc import (
    Notification,
    decode_object,
    rpc_integer,
)
from kora.plugins.harness.codex.launch_configuration import CODEX_VERSION
from tests.plugins.harness.codex.selected_executor_peer import ExecutorPeerWitness

GuardKind = Literal["bash", "patch"]
REFUSAL = "kora fixture withheld the executor effect"
MALFORMED_HEADER_CANARY = "kora-executor-guard-malformed-header-canary"
HookOutcome = tuple[str, tuple[str, str] | None]


class FrameworkDiagnostics(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        sys.stderr.write(
            '{"level":"WARNING","event":"probe.executor_guard_http_error"}\n'
        )


class GuardFailure(Exception):
    pass


@dataclass(frozen=True, repr=False, slots=True)
class GuardAction:
    kind: GuardKind
    command: str
    call_id: str
    workspace: Path
    marker: str
    patch_bytes: bytes


def object_fields(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise GuardFailure("invalid executor fixture fields")
    return cast("dict[str, object]", value)


def encoded_message(frame: dict[str, object]) -> bytes:
    return json.dumps(frame, separators=(",", ":")).encode() + b"\n"


def exact_fields(actual: object, expected: object) -> bool:
    return json.dumps(actual, sort_keys=True) == json.dumps(expected, sort_keys=True)


class ExecutorGuard:
    def __init__(
        self,
        backend_reader: asyncio.StreamReader,
        backend_writer: asyncio.StreamWriter,
        token: str,
        action: GuardAction,
        permit: Path,
        deadline: float,
        max_frame_bytes: int,
        max_messages: int,
        hook_outcome: HookOutcome,
    ) -> None:
        self._backend_reader = backend_reader
        self._backend_writer = backend_writer
        self._token = token
        self._action = action
        self._permit = permit
        self._deadline = deadline
        self._max_frame_bytes = max_frame_bytes
        self._max_messages = max_messages
        self._hook_status, entry = hook_outcome
        self._hook_entries = (
            [] if entry is None else [{"kind": entry[0], "text": entry[1]}]
        )
        self._watched_paths = frozenset(
            (parent / ".git").as_uri()
            for parent in (action.workspace, *action.workspace.parents)
        ) | {(action.workspace / ".agents" / "skills").as_uri()}
        self._marker_uri = (action.workspace / action.marker).as_uri()
        self._initialized = False
        self._init_requested = False
        self._settled = False
        self._thread: str | None = None
        self._turn: str | None = None
        self._hook: tuple[str, str, object] | None = None
        self._changed = asyncio.Event()
        self._ids: set[int] = set()
        self._admitted = False
        self._closed = False
        self._active: web.WebSocketResponse | None = None
        self._peer_witness: ExecutorPeerWitness | None = None
        self._pump: asyncio.Task[None] | None = None
        self._process_id: str | None = None
        self.failure: str | None = None
        self.attempted = 0
        self.forwarded = 0
        self.withheld = 0
        self.native_frames = 0
        app = web.Application()
        app.router.add_get("/", self._handle)
        logger = logging.Logger("executor_fixture", logging.WARNING)
        logger.addHandler(FrameworkDiagnostics())
        self._runner = web.AppRunner(
            app, access_log=None, logger=logger, shutdown_timeout=1
        )

    async def start(self) -> str:
        await self._runner.setup()
        await web.TCPSite(self._runner, "127.0.0.1", 0).start()
        addresses: object = self._runner.addresses
        match addresses:
            case [("127.0.0.1", int(port))] if 0 < port <= 65_535:
                return f"ws://127.0.0.1:{port}"
            case _:
                raise GuardFailure("executor fixture listener identity mismatch")

    def bind_peer(self, witness: ExecutorPeerWitness) -> None:
        if self._peer_witness is not None or self._closed or self._admitted:
            raise GuardFailure("executor fixture peer binding unavailable")
        self._peer_witness = witness

    def set_thread(self, thread: str) -> None:
        if self._thread is not None or self._closed:
            raise GuardFailure("executor fixture thread was already set")
        self._thread = thread

    def started(self, turn: str) -> None:
        if self._turn is not None or self._closed:
            raise GuardFailure("executor fixture turn was already set")
        self._turn = turn
        self._changed.set()

    def observe(self, note: Notification) -> None:
        if note.method != "hook/completed":
            return
        fields = object_fields(note.params)
        run = object_fields(fields.get("run"))
        turn = fields.get("turnId")
        status = run.get("status")
        identity = {
            "id": f"pre-tool-use:0:/opt/kora-probe:{self._action.call_id}",
            "eventName": "preToolUse",
            "handlerType": "command",
            "executionMode": "sync",
            "scope": "turn",
            "sourcePath": "/opt/kora-probe",
            "source": "system",
            "displayOrder": 0,
        }
        if (
            self._hook is not None
            or fields.get("threadId") != self._thread
            or not isinstance(turn, str)
            or not 0 < len(turn) <= 256
            or status != self._hook_status
            or not exact_fields(run.get("entries"), self._hook_entries)
            or not exact_fields({key: run.get(key) for key in identity}, identity)
        ):
            raise GuardFailure("executor fixture hook identity mismatch")
        self._hook = (turn, self._hook_status, run.get("entries"))
        self._changed.set()

    def hook_matches(self, turn: str) -> bool:
        return self._turn == turn and self._hook == (
            turn,
            self._hook_status,
            self._hook_entries,
        )

    def permit_matches(self) -> bool:
        try:
            with self._permit.open("rb") as permit:
                raw = permit.read(16_385)
        except FileNotFoundError:
            return False
        if len(raw) > 16_384:
            return False
        return decode_object(raw) == {
            "thread": self._thread,
            "turn": self._turn,
            "call": self._action.call_id,
            "tool": "Bash" if self._action.kind == "bash" else "apply_patch",
            "command": self._action.command,
        }

    async def wait_turn(self) -> None:
        while not self._closed and self._turn is None:
            self._changed.clear()
            await self._changed.wait()
        if self._closed:
            raise GuardFailure("executor fixture turn did not open")

    async def _approved(self) -> bool:
        while not self._closed and (self._turn is None or self._hook is None):
            self._changed.clear()
            await self._changed.wait()
        return (
            not self._closed
            and asyncio.get_running_loop().time() < self._deadline
            and self._hook == (self._turn, "completed", [])
            and self.permit_matches()
        )

    def _matches_process(self, values: dict[str, object]) -> bool:
        process_id = values.get("processId")
        if (
            not isinstance(process_id, str)
            or not process_id.isascii()
            or not 0 < len(process_id) <= 64
        ):
            return False
        self._process_id = process_id
        return exact_fields(
            values,
            {
                "arg0": None,
                "argv": ["/bin/bash", "-c", self._action.command],
                "cwd": self._action.workspace.as_uri(),
                "enforceManagedNetwork": False,
                "env": {
                    "CODEX_APPLY_PATCH_PRESERVE_LINE_ENDINGS": "1",
                    "CODEX_CI": "1",
                    "CODEX_SESSION_ID": self._thread,
                    "CODEX_THREAD_ID": self._thread,
                    "CODEX_TOOL_CALL_ID": self._action.call_id,
                    "CODEX_VERSION": CODEX_VERSION,
                    "COLORTERM": "",
                    "GH_PAGER": "cat",
                    "GIT_PAGER": "cat",
                    "LC_ALL": "C.UTF-8",
                    "LC_CTYPE": "C.UTF-8",
                    "NO_COLOR": "1",
                    "PAGER": "cat",
                    "TERM": "dumb",
                },
                "envPolicy": {
                    "inherit": "all",
                    "ignoreDefaultExcludes": True,
                    "exclude": [
                        "CODEX_API_KEY",
                        "CODEX_EXEC_SERVER_URL",
                        "CODEX_PERMISSION_PROFILE",
                        "CODEX_VERSION",
                        "CODEX_APPLY_PATCH_PRESERVE_LINE_ENDINGS",
                        "CODEX_PLUGIN_METRICS_OUTPUT",
                    ],
                    "set": {},
                    "includeOnly": [],
                },
                "managedNetwork": None,
                "metadata": {
                    "threadId": self._thread,
                    "toolCallId": self._action.call_id,
                },
                "pipeStdin": False,
                "processId": process_id,
                "sandbox": None,
                "tty": False,
            },
        )

    def _matches_effect(self, method: str, values: dict[str, object]) -> bool:
        if self._action.kind == "bash":
            return method == "process/start" and self._matches_process(values)
        return method == "fs/writeFile" and exact_fields(
            values,
            {
                "path": self._marker_uri,
                "dataBase64": base64.b64encode(self._action.patch_bytes).decode(),
                "sandbox": None,
            },
        )

    def _setup_matches(self, method: str, params: object) -> bool:
        if method == "initialize":
            if self._init_requested or not exact_fields(
                params, {"clientName": "codex-environment", "resumeSessionId": None}
            ):
                return False
            self._init_requested = True
            return True
        if method == "initialized":
            if not self._init_requested or self._initialized or params != {}:
                return False
            self._initialized = True
            return True
        if not self._initialized:
            return False
        if method in {"environment/info", "environment/status"}:
            return params is None
        values = object_fields(params)
        if method == "environmentConfig/read":
            return exact_fields(
                values,
                {
                    "cwd": self._action.workspace.as_uri(),
                    "configPaths": [["mcp_servers"]],
                    "requirementsPaths": [["mcp_servers"]],
                },
            )
        if method == "fs/watchMissing":
            missing = values.get("missing")
            watch_id = values.get("watchId")
            if not isinstance(missing, list):
                return False
            paths = cast("list[object]", missing)
            return (
                values.keys() == {"watchId", "missing", "present"}
                and type(watch_id) is int
                and 0 <= watch_id < 1 << 64
                and values["present"] is None
                and len(paths) == len(self._watched_paths)
                and all(
                    isinstance(path, str) and path in self._watched_paths
                    for path in paths
                )
                and len(frozenset(cast("list[str]", paths))) == len(paths)
            )
        path = values.get("path")
        if not isinstance(path, str) or not exact_fields(
            values, {"path": path, "sandbox": None}
        ):
            return False
        if method == "fs/getMetadata":
            return path in self._watched_paths or path == self._marker_uri
        return method == "fs/readFile" and path == self._marker_uri

    async def _forward(self, frame: dict[str, object], *, effect: bool) -> None:
        data = encoded_message(frame)
        if (
            self._closed
            or len(data) > self._max_frame_bytes
            or asyncio.get_running_loop().time() >= self._deadline
        ):
            raise GuardFailure("executor fixture cannot forward frame")
        self._backend_writer.write(data)
        self.native_frames += 1
        if effect:
            self.forwarded += 1
        await self._backend_writer.drain()

    async def _frame(self, ws: web.WebSocketResponse, data: str) -> None:
        frame = decode_object(data.encode())
        if frame is None or frame.keys() - {"method", "params", "id"}:
            raise GuardFailure("invalid executor fixture request")
        method = frame.get("method")
        identity = frame.get("id")
        if not isinstance(method, str) or not method:
            raise GuardFailure("invalid executor fixture method")
        if method == "initialized":
            if "id" in frame:
                raise GuardFailure("invalid executor fixture notification")
        else:
            if (
                "params" not in frame
                or not rpc_integer(identity)
                or cast("int", identity) in self._ids
            ):
                raise GuardFailure("invalid or repeated executor fixture identity")
            self._ids.add(cast("int", identity))
        params = frame.get("params", {})
        effect = method in {"process/start", "fs/writeFile"}
        if effect:
            self.attempted += 1
            if (
                not self._initialized
                or self.attempted != 1
                or not self._matches_effect(method, object_fields(params))
            ):
                raise GuardFailure("unexpected executor fixture effect")
            if not await self._approved():
                await ws.send_str(
                    encoded_message(
                        {"id": identity, "error": {"code": -32600, "message": REFUSAL}}
                    ).decode()
                )
                self.withheld += 1
                return
        elif method == "process/terminate":
            if (
                self.forwarded != 1
                or self._process_id is None
                or not exact_fields(params, {"processId": self._process_id})
            ):
                raise GuardFailure("unsupported executor fixture termination")
        elif not self._setup_matches(method, params):
            raise GuardFailure("unsupported executor fixture setup request")
        await self._forward(frame, effect=effect)

    async def _output(self, ws: web.WebSocketResponse) -> None:
        for _ in range(self._max_messages):
            line = await self._backend_reader.readline()
            if not line:
                return
            if len(line) > self._max_frame_bytes or decode_object(line) is None:
                raise GuardFailure("invalid executor fixture response")
            await ws.send_str(line.decode())
        raise GuardFailure("executor fixture response limit reached")

    async def _handle(
        self, request: web.Request
    ) -> web.WebSocketResponse | web.Response:
        offered = request.headers.getall("Authorization", [])
        if len(offered) != 1 or not secrets.compare_digest(
            offered[0], f"Bearer {self._token}"
        ):
            return web.Response(status=401)
        if self._closed or self._admitted:
            return web.Response(status=409)
        ws = web.WebSocketResponse(
            max_msg_size=self._max_frame_bytes, compress=False, timeout=1
        )
        if (
            "Sec-WebSocket-Protocol" in request.headers
            or not ws.can_prepare(request).ok
        ):
            return web.Response(status=400)
        if self._peer_witness is not None and (
            not self._peer_witness.admits(request)
            or asyncio.get_running_loop().time() >= self._deadline
        ):
            return web.Response(status=403)
        self._admitted = True
        try:
            async with asyncio.timeout_at(self._deadline):
                await ws.prepare(request)
                self._active = ws
                self._pump = asyncio.create_task(self._output(ws))
                received = 0
                async for message in ws:
                    received += 1
                    data: object = message.data
                    if (
                        received > self._max_messages
                        or message.type is not WSMsgType.TEXT
                        or not isinstance(data, str)
                    ):
                        raise GuardFailure("invalid executor fixture message")
                    await self._frame(ws, data)
        except (GuardFailure, OSError, ValueError, TimeoutError):
            self.failure = "executor fixture transport failed"
            if ws.prepared:
                await ws.close(code=1011, drain=False)
        finally:
            if self._pump is not None:
                self._pump.cancel()
                (result,) = await asyncio.gather(self._pump, return_exceptions=True)
                if isinstance(result, BaseException) and not isinstance(
                    result, asyncio.CancelledError
                ):
                    self.failure = "executor fixture backend failed"
        return ws

    async def close_connection(self) -> None:
        if self._active is None:
            raise GuardFailure("executor fixture connection missing")
        await self._active.close(drain=False)
        if not self._active.closed:
            raise GuardFailure("executor fixture connection did not close")

    def fence(self) -> None:
        self._closed = True
        self._changed.set()

    async def close(self) -> None:
        if self._settled:
            return
        self.fence()
        try:
            if self._active is not None:
                await self.close_connection()
        finally:
            await self._runner.cleanup()
        if self._runner.addresses or (self._pump is not None and not self._pump.done()):
            raise GuardFailure("executor fixture did not settle")
        self._settled = True


async def verify_non_upgrade_refused(endpoint: str, token: str) -> None:
    async with (
        ClientSession() as client,
        client.get(
            endpoint.replace("ws://", "http://", 1),
            headers={"Authorization": f"Bearer {token}"},
        ) as response,
    ):
        if response.status != 400:
            raise GuardFailure("unexpected executor fixture HTTP status")


async def verify_malformed_header_refused(endpoint: str) -> None:
    address = urlsplit(endpoint)
    if address.hostname != "127.0.0.1" or address.port is None:
        raise GuardFailure("unexpected executor fixture HTTP endpoint")
    reader, writer = await asyncio.open_connection(address.hostname, address.port)
    try:
        writer.write(
            (
                f"GET / HTTP/1.1\r\nAuthorization {MALFORMED_HEADER_CANARY}\r\n\r\n"
            ).encode()
        )
        await writer.drain()
        if not (await reader.readline()).startswith(
            (b"HTTP/1.0 400 ", b"HTTP/1.1 400 ")
        ):
            raise GuardFailure("malformed executor fixture header was accepted")
    finally:
        writer.close()
        await writer.wait_closed()


async def verify_upgrade_refused(
    endpoint: str, token: str | None, status: int, *, subprotocol: str | None = None
) -> None:
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    if subprotocol is not None:
        headers["Sec-WebSocket-Protocol"] = subprotocol
    async with ClientSession() as client:
        try:
            async with client.ws_connect(endpoint, headers=headers):
                raise GuardFailure("unexpected executor fixture connection")
        except WSServerHandshakeError as error:
            if error.status != status:
                raise GuardFailure(
                    "unexpected executor fixture upgrade status"
                ) from None
