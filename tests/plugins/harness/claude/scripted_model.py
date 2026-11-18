import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Final, cast

KEY: Final[str] = "fixture-key-value"
BODY_LIMIT: Final[int] = 8_000_000


def sse(events: list[tuple[str, dict[str, object]]]) -> bytes:
    chunks = [
        f"event: {name}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"
        for name, payload in events
    ]
    return "".join(chunks).encode("ascii")


def tool_stream(
    model: str, name: str, payload: dict[str, object], index: int = 0
) -> bytes:
    message: dict[str, object] = {
        "id": f"msg_scripted_tool_{index}",
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
                        "id": f"toolu_scripted_{index}",
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


def tool_results(document: dict[str, object]) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    for message in objects(document.get("messages")) or []:
        for block in objects((mapping(message) or {}).get("content")) or []:
            fields = mapping(block)
            if fields is not None and fields.get("type") == "tool_result":
                found.append(fields)
    return found


class ScriptedState:
    def __init__(self, calls: list[tuple[str, dict[str, object]]]) -> None:
        self.calls = calls
        self.bodies: list[str] = []
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
        with server.state.lock:
            server.state.bodies.append(body.decode("utf-8"))
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
        index = len(tool_results(parsed))
        if index >= len(server.state.calls):
            payload = end_stream(model_name)
        else:
            name, inputs = server.state.calls[index]
            payload = tool_stream(model_name, name, inputs, index)
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
