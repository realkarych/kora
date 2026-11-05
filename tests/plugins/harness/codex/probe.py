import asyncio
import hashlib
import json
import os
import platform
import re
import secrets
import select
import signal
import socket
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal, cast

from kora.plugins.harness.codex.app_server_errors import (
    HTTP_ERROR_CODES,
    NativeErrorRefused,
    TurnErrors,
    decode_error_info,
    reject_premature_error,
)
from kora.plugins.harness.codex.app_server_messages import (
    MessagePhase,
    MessageRefused,
    TurnMessages,
    reject_premature_message,
)
from kora.plugins.harness.codex.app_server_rpc import (
    AppServerRpc,
    Notification,
    RpcError,
    RpcFailure,
)
from kora.plugins.harness.codex.app_server_startup import (
    StartupError,
    StartupFailure,
    StartupIdentity,
    initialize_app_server,
)
from kora.plugins.harness.codex.app_server_turn import (
    TurnCompletion,
    TurnError,
    observe_app_server_turn,
    reject_premature_completion,
)
from kora.plugins.harness.codex.app_server_usage import (
    TokenCounts,
    TurnUsage,
    UsageChange,
    UsageError,
    decode_usage,
    reject_premature_usage,
)

Case = Literal["allow", "deny", "stall", "interactive", "usage"]
ProviderCase = Literal[
    "implicit_account", "provider_key_present", "provider_key_absent"
]
ShellCase = Literal["shell-default", "shell-login", "shell-alternate"]
ExecutorCase = Literal[
    "executor-default",
    "executor-bash",
    "executor-sh",
    "executor-login",
    "executor-deny",
    "executor-stall",
]
ClientCase = Literal["client-complete", "client-exit"]
StartupCase = Literal["startup-version", "startup-home"]
HookInputCase = Literal["patch-allow", "patch-deny", "decoder-refuse"]
InterruptCase = Literal["interrupt-model", "interrupt-command", "interrupt-kill"]
ErrorCase = Literal["error-401", "error-429", "error-503"]
MessageCase = Literal["messages-plain", "messages-stream", "messages-kill"]
OutputCase = Literal["output-valid", "output-invalid", "output-kill"]
ProbeCase = (
    Case
    | ProviderCase
    | ShellCase
    | ExecutorCase
    | ClientCase
    | StartupCase
    | HookInputCase
    | InterruptCase
    | ErrorCase
    | MessageCase
    | OutputCase
)

CODEX = "/usr/local/bin/codex"
CODEX_VERSION = "0.166.0"
MESSAGE_CASES = frozenset({"messages-plain", "messages-stream", "messages-kill"})
OUTPUT_CASES = frozenset({"output-valid", "output-invalid", "output-kill"})
OUTPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}
OUTPUT_TEXT = '{"ok":true}'
INVALID_OUTPUT_TEXT = '{"ok":true,"ok":false}'
OUTPUT_MESSAGE = "kora-output-message"
MESSAGE_TEXT = "kora-private-message-canary: незавершённый 🌲"
MESSAGE_CONTENT: tuple[tuple[MessagePhase, str], ...] = (
    ("commentary", "kora-private-message-canary: наблюдение 🌲"),
    ("partial_answer", "kora-private-message-canary: часть 🌲"),
    ("final_answer", "kora-private-message-canary: ответ 🌲"),
)
STARTUP_REFUSALS: dict[StartupCase, StartupFailure] = {
    "startup-version": StartupFailure.VERSION_MISMATCH,
    "startup-home": StartupFailure.HOME_MISMATCH,
}
PROVIDER_CASES = frozenset(
    {"implicit_account", "provider_key_present", "provider_key_absent"}
)
CLIENT_CASES = frozenset({"client-complete", "client-exit"})
PATCH_CASES = frozenset({"patch-allow", "patch-deny"})
HOOK_INPUT_CASES = PATCH_CASES | {"decoder-refuse"}
INTERRUPT_CASES = frozenset({"interrupt-model", "interrupt-command", "interrupt-kill"})
INTERRUPT_COMMAND = "exec /usr/local/bin/python3 -I /opt/kora-probe/interrupt_child.py"
INTERRUPT_PRIMER_ID = "kora-interrupt-primer"
ERROR_CASES: dict[ErrorCase, int] = {
    "error-401": 401,
    "error-429": 429,
    "error-503": 503,
}
ERROR_CANARY = "kora-private-provider-error-canary"
SYNTHETIC_BEARER = "kora-probe-synthetic-value"
MISSING_PROVIDER_KEY = "Missing environment variable: `CODEX_API_KEY`."
CONTROL = Path("/var/lib/kora-hook-probe/control")
RECEIPTS = CONTROL / "receipts.jsonl"
MARKER = "kora-probe.marker"
COMMAND = f"touch {MARKER}"
CALL_ID = "kora-hook-probe-call"
PATCH_MARKER = b"direct patch marker\n"
PATCH = (
    f"*** Begin Patch\n*** Add File: {MARKER}\n+direct patch marker\n*** End Patch\n"
)
PATCH_DENIAL = "kora managed hook probe denied the direct patch"
CAP_REFUSAL = "kora managed hook probe refused oversized input"
STDIN_CALL_ID = "kora-hook-probe-stdin"
STDIN_READY = "kora-stdin-ready"
STDIN_WRITTEN = "kora-stdin-written"
STDIN_COMMAND = (
    f"printf '{STDIN_READY}\\n'; IFS= read -r token && "
    'test "$token" = kora-stdin-token && '
    f"printf '{STDIN_WRITTEN}\\n' > {MARKER} && cat {MARKER}"
)
SHELL_COMMAND = (
    'shell_path=$(readlink -f -- "$0") && '
    'mode=posix-sh && if [ -n "${BASH_VERSION-}" ]; then '
    "mode=bash-non-login; if shopt -q login_shell; then mode=bash-login; fi; fi && "
    """printf '%s\\n' "$shell_path" "$mode" > kora-probe.marker"""
)
EXECUTOR_COMMAND = SHELL_COMMAND + (
    """ && printf '%s\\n' "${KORA_PROBE_EXECUTOR_ORIGIN-}" >> kora-probe.marker"""
)
EXECUTOR_ID = "kora-probe-executor"
SYNTHETIC_EXECUTOR_BEARER = "kora-probe-synthetic-executor-value"
EXECUTOR_ORIGIN = "kora-probe-executor-only"
EXECUTOR_BASH_OVERRIDE = "/opt/kora-probe/missing/bash"
EXECUTOR_LISTEN_URL = re.compile(rb"ws://127\.0\.0\.1:([1-9][0-9]{0,4})\n")
EXECUTOR_SHELL_POLICY = """
[shell_environment_policy]
inherit = "all"
include_only = ["HOME", "PATH", "SHELL", "LANG", "KORA_PROBE_EXECUTOR_ORIGIN"]
"""
SHELL_ARGUMENTS: dict[ProbeCase, dict[str, object]] = {
    "shell-default": {},
    "shell-login": {"login": True},
    "shell-alternate": {"shell": "/bin/sh"},
    "executor-default": {},
    "executor-bash": {"shell": EXECUTOR_BASH_OVERRIDE},
    "executor-sh": {"shell": "/bin/sh"},
    "executor-login": {"login": True},
    "executor-deny": {},
    "executor-stall": {},
}
SHELL_FLAGS: dict[str, ShellCase | ExecutorCase] = {
    "--shell-default": "shell-default",
    "--shell-login": "shell-login",
    "--shell-alternate": "shell-alternate",
    "--executor-default": "executor-default",
    "--executor-bash": "executor-bash",
    "--executor-sh": "executor-sh",
    "--executor-login": "executor-login",
    "--executor-deny": "executor-deny",
    "--executor-stall": "executor-stall",
}
EXECUTOR_CASES = frozenset(
    {
        "executor-default",
        "executor-bash",
        "executor-sh",
        "executor-login",
        "executor-deny",
        "executor-stall",
    }
)
STALLED_CASES = frozenset({"stall", "executor-stall"})
LOGIN_DISABLED = "login shell is disabled by config; omit `login` or set it to false."
SHELL_RESTRICTED = f"environment `{EXECUTOR_ID}` only supports `bash`"
EXECUTOR_DENIAL = (
    "Command blocked by PreToolUse hook: "
    f"kora managed hook probe denied the executor command. Command: {EXECUTOR_COMMAND}"
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


def sse(
    response_id: str,
    item: dict[str, object],
    *,
    usage: dict[str, object] | None = None,
) -> bytes:
    events = (
        {"type": "response.created", "response": {"id": response_id}},
        {"type": "response.output_item.done", "item": item},
        {
            "type": "response.completed",
            "response": {
                "id": response_id,
                "usage": usage
                if usage is not None
                else {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            },
        },
    )
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
    ).encode()


def message_response(*, complete: bool) -> bytes:
    response_id = "kora-message-response"
    events: list[dict[str, object]] = [
        {"type": "response.created", "response": {"id": response_id}}
    ]
    content = MESSAGE_CONTENT if complete else (("final_answer", MESSAGE_TEXT),)
    for index, (phase, text) in enumerate(content):
        identity = f"kora-message-{index}"
        events.extend(
            [
                {
                    "type": "response.output_item.added",
                    "output_index": index,
                    "item": {
                        "type": "message",
                        "id": identity,
                        "role": "assistant",
                        "phase": phase,
                        "content": [],
                    },
                },
                {
                    "type": "response.content_part.added",
                    "item_id": identity,
                    "output_index": index,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": ""},
                },
                {
                    "type": "response.output_text.delta",
                    "item_id": identity,
                    "output_index": index,
                    "content_index": 0,
                    "delta": text,
                },
            ]
        )
        if complete:
            events.append(
                {
                    "type": "response.output_item.done",
                    "output_index": index,
                    "item": {
                        "type": "message",
                        "id": identity,
                        "role": "assistant",
                        "phase": phase,
                        "content": [{"type": "output_text", "text": text}],
                    },
                }
            )
    if complete:
        events.append(
            {
                "type": "response.completed",
                "response": {
                    "id": response_id,
                    "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                },
            }
        )
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
    ).encode()


def output_response(case: ProbeCase, *, complete: bool) -> bytes:
    text = INVALID_OUTPUT_TEXT if case == "output-invalid" else OUTPUT_TEXT
    item: dict[str, object] = {
        "id": OUTPUT_MESSAGE,
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }
    if complete:
        return sse("kora-output-response", item)
    events: tuple[dict[str, object], ...] = (
        {"type": "response.created", "response": {"id": "kora-output-response"}},
        {"type": "response.output_item.done", "item": item},
    )
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
    ).encode()


def command_response(
    arguments: dict[str, object], *, usage: dict[str, object] | None = None
) -> bytes:
    return sse(
        "kora-probe-response-1",
        {
            "type": "function_call",
            "call_id": CALL_ID,
            "name": "exec_command",
            "arguments": json.dumps(arguments),
        },
        usage=usage,
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


def tool_output(
    request: dict[str, object], call_id: str, output_kind: str = "function_call_output"
) -> str:
    items = [
        as_object(item)
        for item in as_list(request.get("input"), "responses_input_missing")
    ]
    outputs = [
        item.get("output")
        for item in items
        if item.get("type") == output_kind and item.get("call_id") == call_id
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


TOOL_RESPONSE = command_response({"cmd": COMMAND, "login": False})
PATCH_RESPONSE = sse(
    "kora-probe-response-1",
    {
        "type": "custom_tool_call",
        "call_id": CALL_ID,
        "name": "apply_patch",
        "input": PATCH,
    },
)
FINAL_ITEM: dict[str, object] = {
    "type": "message",
    "role": "assistant",
    "id": "kora-probe-message",
    "content": [{"type": "output_text", "text": "Probe turn finished."}],
}
FINAL_RESPONSE = sse("kora-probe-response-2", FINAL_ITEM)
USAGE_TOOL_RESPONSE = command_response(
    {"cmd": COMMAND, "login": False},
    usage={
        "input_tokens": 100,
        "input_tokens_details": {"cached_tokens": 20, "cache_write_tokens": 30},
        "output_tokens": 10,
        "output_tokens_details": {"reasoning_tokens": 4},
        "total_tokens": 110,
    },
)
INTERRUPT_PRIMER_RESPONSE = sse(
    "kora-interrupt-primer-response",
    {
        "type": "function_call",
        "call_id": INTERRUPT_PRIMER_ID,
        "name": "exec_command",
        "arguments": json.dumps({"cmd": ":", "login": False, "timeout_ms": 60_000}),
    },
    usage={
        "input_tokens": 100,
        "input_tokens_details": {"cached_tokens": 20, "cache_write_tokens": 30},
        "output_tokens": 10,
        "output_tokens_details": {"reasoning_tokens": 4},
        "total_tokens": 110,
    },
)
INTERRUPT_TOOL_RESPONSE = command_response(
    {"cmd": INTERRUPT_COMMAND, "login": False, "timeout_ms": 60_000}
)
USAGE_FINAL_RESPONSE = sse(
    "kora-probe-response-2",
    FINAL_ITEM,
    usage={
        "input_tokens": 60,
        "input_tokens_details": {"cached_tokens": 5, "cache_write_tokens": 10},
        "output_tokens": 20,
        "output_tokens_details": {"reasoning_tokens": 6},
        "total_tokens": 80,
    },
)
EXPECTED_USAGE = (TokenCounts(100, 20, 30, 10, 4), TokenCounts(160, 25, 40, 30, 10))


class ResponsesFixture:
    def __init__(self, case: ProbeCase, one_shot: bool) -> None:
        self.case: ProbeCase = case
        self.one_shot = one_shot
        self.request_limit: int
        if case in PROVIDER_CASES:
            self.request_limit = int(case == "provider_key_present")
        elif case in ERROR_CASES or case in MESSAGE_CASES or case in OUTPUT_CASES:
            self.request_limit = 1
        elif case in INTERRUPT_CASES:
            self.request_limit = 1 if case == "interrupt-model" else 2
        elif (
            case == "executor-stall" or case in CLIENT_CASES or case in STARTUP_REFUSALS
        ):
            self.request_limit = 1
        else:
            self.request_limit = (
                3 if case == "interactive" or (one_shot and case == "allow") else 2
            )
        self.request_admitted = asyncio.Event()
        self.stream_ready = asyncio.Event()
        self.stream_closed = asyncio.Event()
        self.connections: int = 0
        self.requests = 0
        self.failure: str | None = None
        self.clients: list[tuple[asyncio.StreamWriter, asyncio.Task[None]]] = []

    def accept(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        deadline: float,
    ) -> None:
        self.connections += 1
        if len(self.clients) >= self.request_limit:
            self.failure = "unexpected_responses_connection"
            writer.close()
            return
        self.clients.append(
            (writer, asyncio.create_task(self.handle(reader, writer, deadline)))
        )

    async def handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        deadline: float,
    ) -> None:
        try:
            async with asyncio.timeout_at(deadline):
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
                self.request_admitted.set()
                if self.case == "client-exit":
                    await asyncio.Future[None]()
                if self.case in ERROR_CASES:
                    status = ERROR_CASES[self.case]
                    body = json.dumps(
                        {
                            "error": {
                                "type": "kora_probe_failure",
                                "message": ERROR_CANARY,
                            }
                        }
                    ).encode()
                    writer.write(
                        f"HTTP/1.1 {status} Fixture failure\r\n".encode()
                        + b"Content-Type: application/json\r\nConnection: close\r\n"
                        + f"Content-Length: {len(body)}\r\n\r\n".encode()
                        + body
                    )
                    await writer.drain()
                    return
                if self.case in {"interrupt-model", "messages-kill", "output-kill"}:
                    prelude = (
                        output_response(self.case, complete=False)
                        if self.case == "output-kill"
                        else message_response(complete=False)
                        if self.case == "messages-kill"
                        else b'data: {"type":"response.created","response":'
                        b'{"id":"kora-probe-open-response"}}\n\n'
                    )
                    writer.write(
                        b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                        b"Connection: close\r\n\r\n" + prelude
                    )
                    await writer.drain()
                    self.stream_ready.set()
                    require(
                        await reader.read(1) == b"", "interrupted_stream_not_closed"
                    )
                    self.stream_closed.set()
                    return
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
        require(self.case not in STARTUP_REFUSALS, "startup_unexpected_model_request")
        if self.case in OUTPUT_CASES:
            formatting = as_object(as_object(request.get("text")).get("format"))
            require(
                formatting.get("type") == "json_schema"
                and formatting.get("schema") == OUTPUT_SCHEMA,
                "native_output_schema_mismatch",
            )
            require(self.requests == 1, "unexpected_output_request")
            return output_response(self.case, complete=True)
        if self.case in MESSAGE_CASES:
            require(self.requests == 1, "unexpected_message_request")
            return (
                message_response(complete=True)
                if self.case == "messages-stream"
                else FINAL_RESPONSE
            )
        if (
            self.case == "provider_key_present"
            or self.case in CLIENT_CASES
            or self.case in ERROR_CASES
        ):
            require(self.requests == 1, "unexpected_responses_request")
            return FINAL_RESPONSE
        if self.requests == 1:
            tools = [
                as_object(tool)
                for tool in as_list(request.get("tools"), "responses_tools_missing")
            ]
            if self.case in PATCH_CASES:
                patch_tools = [
                    tool for tool in tools if tool.get("name") == "apply_patch"
                ]
                require(
                    len(patch_tools) == 1 and patch_tools[0].get("type") == "custom",
                    "direct_patch_not_advertised",
                )
                return PATCH_RESPONSE
            command_tools = [
                tool for tool in tools if tool.get("name") == "exec_command"
            ]
            require(len(command_tools) == 1, "exec_command_advertisement_count")
            names = [tool.get("name") for tool in tools]
            if self.case in INTERRUPT_CASES:
                require("write_stdin" not in names, "interrupt_stdin_advertised")
                verify_one_shot_schema(command_tools[0])
                return INTERRUPT_PRIMER_RESPONSE
            if self.case == "interactive":
                require("write_stdin" in names, "interactive_stdin_not_advertised")
                return command_response(
                    {
                        "cmd": STDIN_COMMAND,
                        "login": False,
                        "tty": True,
                        "yield_time_ms": 1_000,
                    }
                )
            if self.one_shot:
                require("write_stdin" not in names, "one_shot_stdin_advertised")
                verify_one_shot_schema(command_tools[0])
                if self.case == "allow":
                    return stdin_response(1)
            if self.case in SHELL_ARGUMENTS:
                command = (
                    EXECUTOR_COMMAND if self.case in EXECUTOR_CASES else SHELL_COMMAND
                )
                return command_response({"cmd": command, **SHELL_ARGUMENTS[self.case]})
            return USAGE_TOOL_RESPONSE if self.case == "usage" else TOOL_RESPONSE

        require(self.requests <= self.request_limit, "unexpected_responses_request")
        if self.case in INTERRUPT_CASES:
            output = tool_output(request, INTERRUPT_PRIMER_ID)
            require(
                re.search(r"(?m)^(?:Process exited with code |Exit code: )0$", output)
                is not None,
                "interrupt_primer_not_successful",
            )
            return INTERRUPT_TOOL_RESPONSE
        require(self.case not in STALLED_CASES, "stalled_hook_returned")
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

        if self.case in PATCH_CASES:
            output = tool_output(request, CALL_ID, "custom_tool_call_output")
            if self.case == "patch-allow":
                lines = output.splitlines()
                require(
                    len(lines) == 5
                    and lines[0] == "Exit code: 0"
                    and lines[1].startswith("Wall time: ")
                    and lines[2:]
                    == [
                        "Output:",
                        "Success. Updated the following files:",
                        f"A {MARKER}",
                    ],
                    "direct_patch_success_missing",
                )
            else:
                require(
                    output
                    == (
                        f"Command blocked by PreToolUse hook: {PATCH_DENIAL}. "
                        f"Command: {PATCH}"
                    ),
                    "direct_patch_refusal_missing",
                )
            return FINAL_RESPONSE

        output = tool_output(request, CALL_ID)
        if self.case == "decoder-refuse":
            require(
                output
                == (
                    f"Command blocked by PreToolUse hook: {CAP_REFUSAL}. "
                    f"Command: {COMMAND}"
                ),
                "decoder_refusal_missing",
            )
        elif self.case in {"shell-login", "executor-login"}:
            require(output == LOGIN_DISABLED, "native_login_refusal_missing")
        elif self.case == "executor-sh":
            require(output == SHELL_RESTRICTED, "native_shell_restriction_missing")
        elif self.case == "executor-deny":
            require(output == EXECUTOR_DENIAL, "executor_hook_denial_missing")
        elif self.case in SHELL_ARGUMENTS:
            header, separator, _ = output.partition("\nOutput:\n")
            status = [
                line
                for line in header.splitlines()
                if line.startswith(
                    ("Process exited with code ", "Process running with session ID ")
                )
            ]
            require(
                bool(separator) and status == ["Process exited with code 0"],
                "shell_command_failed",
            )
        else:
            require(
                (DENIAL in output) == (self.case == "deny"),
                "unexpected_hook_denial_result",
            )
        return USAGE_FINAL_RESPONSE if self.case == "usage" else FINAL_RESPONSE

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


class OutputProbe:
    def __init__(self, thread_id: str, case: ProbeCase) -> None:
        self.thread_id = thread_id
        self.expected = INVALID_OUTPUT_TEXT if case == "output-invalid" else OUTPUT_TEXT
        self.messages = TurnMessages(thread_id, max_messages=2, max_text_bytes=4096)
        self.acknowledged_turn: str | None = None
        self.item_completed = asyncio.Event()
        self.terminal_text: str | None = None

    def started(self, turn_id: str) -> None:
        self.messages.for_turn(turn_id)
        self.acknowledged_turn = turn_id

    def observe(self, notification: Notification) -> None:
        observation = self.messages.accept(notification)
        if observation is not None:
            require(
                observation.message_id == OUTPUT_MESSAGE
                and observation.text == self.expected,
                "output_message_mismatch",
            )
            require(self.messages.count == 1, "output_message_count")
            if self.acknowledged_turn is not None:
                self.messages.for_turn(self.acknowledged_turn)
            self.item_completed.set()

    def verify(self, terminal: TurnCompletion) -> None:
        selected = terminal.final_message
        if selected is None:
            raise ProbeFailure("output_terminal_summary_missing")
        require(terminal.status == "completed", "output_turn_not_completed")
        require(
            selected == self.messages.for_turn(terminal.turn_id),
            "output_summary_message_mismatch",
        )
        self.terminal_text = selected.text

    def verify_pending(self) -> None:
        require(self.acknowledged_turn is not None, "output_start_identity_missing")
        require(
            self.item_completed.is_set()
            and self.messages.for_turn(cast("str", self.acknowledged_turn)) is not None,
            "output_item_not_completed",
        )
        require(self.terminal_text is None, "output_premature_terminal_selection")


class MessageProbe:
    def __init__(self, thread_id: str, case: ProbeCase) -> None:
        self.thread_id = thread_id
        self.case = case
        self.messages = TurnMessages(thread_id, max_messages=4, max_text_bytes=4096)
        self.acknowledged_turn: str | None = None
        self.delta_turn: str | None = None
        self.delta_seen = asyncio.Event()
        self.expected: tuple[tuple[MessagePhase | None, str], ...] = (
            MESSAGE_CONTENT
            if case == "messages-stream"
            else ((None, "Probe turn finished."),)
        )

    def started(self, turn_id: str) -> None:
        self.messages.for_turn(turn_id)
        self.acknowledged_turn = turn_id

    def observe(self, notification: Notification) -> None:
        if (
            self.case == "messages-kill"
            and notification.method == "item/agentMessage/delta"
        ):
            params = as_object(notification.params)
            identity = params.get("turnId")
            require(
                params.get("threadId") == self.thread_id
                and params.get("itemId") == "kora-message-0"
                and params.get("delta") == MESSAGE_TEXT
                and isinstance(identity, str)
                and bool(identity),
                "message_delta_invalid",
            )
            self.delta_turn = cast("str", identity)
            self.delta_seen.set()
        observation = self.messages.accept(notification)
        if observation is not None:
            require(self.case != "messages-kill", "partial_message_completed")
            require(
                self.messages.count <= len(self.expected), "message_count_unexpected"
            )
            phase, text = self.expected[self.messages.count - 1]
            require(
                observation.phase == phase and observation.text == text,
                "message_content_mismatch",
            )
            if self.acknowledged_turn is not None:
                self.messages.for_turn(self.acknowledged_turn)

    def verify(self, terminal: TurnCompletion) -> None:
        require(terminal.status == "completed", "message_turn_not_completed")
        require(self.messages.count == len(self.expected), "message_count_missing")
        require(
            self.messages.for_turn(terminal.turn_id) is not None,
            "message_completion_missing",
        )

    def verify_partial(self) -> None:
        identity = self.acknowledged_turn
        require(
            identity is not None and self.delta_turn == identity,
            "message_delta_turn_mismatch",
        )
        require(
            self.messages.count == 0
            and self.messages.for_turn(cast("str", identity)) is None,
            "partial_message_adopted",
        )


class ErrorProbe:
    def __init__(self, thread_id: str, case: ErrorCase) -> None:
        self.errors: TurnErrors = TurnErrors(thread_id)
        self.case: ErrorCase = case
        self.observations: int = 0

    def observe(self, notification: Notification) -> None:
        observation = self.errors.accept(notification)
        if observation is not None:
            self.observations += 1

    def verify(self, terminal: TurnCompletion) -> None:
        observation = self.errors.for_turn(terminal.turn_id)
        require(
            terminal.status == "failed" and self.observations == 1,
            "native_error_terminal_or_count",
        )
        if observation is None:
            raise ProbeFailure("native_error_notice_missing")
        require(not observation.will_retry, "native_error_unexpected_retry")
        detail = decode_error_info(terminal.error)
        require(
            detail == observation.info
            and detail.code in HTTP_ERROR_CODES
            and detail.http_status_code == ERROR_CASES[self.case],
            "native_error_details_mismatch",
        )
        diagnostic(
            "INFO", "probe.native_error_verified", f"{self.case}:{detail.code.value}"
        )


class UsageProbe:
    def __init__(self, thread_id: str) -> None:
        self.usage = TurnUsage(thread_id)
        self.advances = 0

    def observe(self, notification: Notification) -> None:
        snapshot = decode_usage(notification)
        if snapshot is None:
            return
        change = self.usage.accept(snapshot)
        if change is UsageChange.DUPLICATE:
            return
        require(
            change is UsageChange.ADVANCED and self.advances < len(EXPECTED_USAGE),
            "unexpected_usage_transition",
        )
        require(
            snapshot.counts == EXPECTED_USAGE[self.advances],
            "unexpected_usage_cumulative_counts",
        )
        self.advances += 1

    def verify(self, turn_id: str) -> None:
        require(self.advances == len(EXPECTED_USAGE), "missing_usage_snapshot")
        snapshot = self.usage.for_turn(turn_id)
        if snapshot is None:
            raise ProbeFailure("missing_usage_snapshot")
        require(
            self.usage.accept(snapshot) is UsageChange.DUPLICATE
            and self.usage.for_turn(turn_id) == snapshot
            and snapshot.counts.input_tokens == 95
            and not self.usage.observation_gap,
            "unexpected_usage_replay",
        )


class Stdio:
    def __init__(
        self, rpc: AppServerRpc, deadline: float, identity: StartupIdentity
    ) -> None:
        self.rpc = rpc
        self.deadline = deadline
        self.identity = identity
        self.terminal: TurnCompletion | None = None
        self.waiting_terminal = asyncio.Event()
        self.messages: MessageProbe | None = None
        self.outputs: OutputProbe | None = None

    async def call(self, method: str, params: dict[str, object]) -> dict[str, object]:
        return as_object(
            await self.rpc.call(
                method,
                params,
                deadline=self.deadline,
                observe=reject_premature_completion,
            )
        )

    async def observe(
        self, workspace: Path, case: ProbeCase, executor_url: str | None = None
    ) -> None:
        await initialize_app_server(
            self.rpc,
            self.identity,
            deadline=self.deadline,
            experimental_api=executor_url is not None,
        )
        if case == "implicit_account":
            account = await self.call("account/read", {"refreshToken": False})
            require(
                "account" in account
                and account["account"] is None
                and account.get("requiresOpenaiAuth") is True,
                "unexpected_implicit_account_state",
            )
            return
        if executor_url is not None:
            await self.register_executor(executor_url)
        await self.run_turn(workspace, case, selected_executor=executor_url is not None)

    async def register_executor(self, executor_url: str) -> None:
        await self.call(
            "environment/add",
            {
                "environmentId": EXECUTOR_ID,
                "execServerUrl": executor_url,
                "authBearerToken": SYNTHETIC_EXECUTOR_BEARER,
            },
        )
        local = await self.call("environment/status", {"environmentId": "local"})
        require(local.get("status") == "unknown", "local_executor_available")
        info = await self.call("environment/info", {"environmentId": EXECUTOR_ID})
        shell = as_object(info.get("shell"))
        path = shell.get("path")
        require(
            shell.get("name") == "bash"
            and isinstance(path, str)
            and Path(path).is_absolute()
            and Path(path).resolve(strict=True)
            == Path("/bin/bash").resolve(strict=True),
            "executor_shell_mismatch",
        )

    async def run_turn(
        self, workspace: Path, case: ProbeCase, *, selected_executor: bool
    ) -> None:
        thread_params: dict[str, object] = {"cwd": str(workspace)}
        if selected_executor:
            thread_params["environments"] = [
                {"environmentId": EXECUTOR_ID, "cwd": str(workspace)}
            ]
        ephemeral = (
            case in PROVIDER_CASES
            or case in SHELL_ARGUMENTS
            or case in CLIENT_CASES
            or case in HOOK_INPUT_CASES
            or case in ERROR_CASES
            or case in MESSAGE_CASES
            or case in OUTPUT_CASES
            or case == "usage"
        )
        if ephemeral:
            thread_params["ephemeral"] = True
        prompt = "Run the probe marker command."
        if case in PATCH_CASES:
            prompt = "Apply the probe marker patch."
        elif (
            case in PROVIDER_CASES
            or case in CLIENT_CASES
            or case in ERROR_CASES
            or case in MESSAGE_CASES
            or case in OUTPUT_CASES
        ):
            prompt = "Complete the fixture turn."
        start = await self.call("thread/start", thread_params)
        thread = as_object(start.get("thread"))
        if ephemeral:
            require(thread.get("ephemeral") is True, "thread_not_ephemeral")
        thread_id = thread.get("id")
        require(isinstance(thread_id, str) and bool(thread_id), "missing_thread_id")
        thread_id = cast("str", thread_id)
        usage = UsageProbe(thread_id) if case == "usage" else None
        errors = ErrorProbe(thread_id, case) if case in ERROR_CASES else None
        messages = MessageProbe(thread_id, case) if case in MESSAGE_CASES else None
        self.messages = messages
        outputs = OutputProbe(thread_id, case) if case in OUTPUT_CASES else None
        self.outputs = outputs

        def observe(notification: Notification) -> None:
            if usage is not None:
                usage.observe(notification)
            if errors is not None:
                errors.observe(notification)
            if messages is not None:
                messages.observe(notification)
            if outputs is not None:
                outputs.observe(notification)

        def observe_before_send(notification: Notification) -> None:
            if usage is not None:
                reject_premature_usage(notification)
            if errors is not None:
                reject_premature_error(notification)
            if messages is not None:
                reject_premature_message(notification)
            if outputs is not None:
                reject_premature_message(notification)

        def observe_started(turn_id: str) -> None:
            if messages is not None:
                messages.started(turn_id)
            if outputs is not None:
                outputs.started(turn_id)

        self.terminal = await observe_app_server_turn(
            self.rpc,
            thread_id=thread_id,
            prompt=prompt,
            deadline=self.deadline,
            observe=observe,
            observe_before_send=observe_before_send,
            on_waiting=self.waiting_terminal.set,
            on_started=observe_started,
            output_schema=OUTPUT_SCHEMA if outputs is not None else None,
        )
        if outputs is not None:
            outputs.verify(self.terminal)
        if messages is not None:
            messages.verify(self.terminal)
        if errors is not None:
            errors.verify(self.terminal)
        elif case == "provider_key_absent":
            require(
                self.terminal.status == "failed", "missing_provider_key_not_refused"
            )
            require(
                as_object(self.terminal.error).get("message") == MISSING_PROVIDER_KEY,
                "unexpected_missing_provider_key_error",
            )
        else:
            require(self.terminal.status == "completed", "turn_not_completed")
        if usage is not None:
            usage.verify(self.terminal.turn_id)

    async def interrupt_turn(
        self,
        workspace: Path,
        fixture: ResponsesFixture,
        process: asyncio.subprocess.Process,
    ) -> None:
        await initialize_app_server(self.rpc, self.identity, deadline=self.deadline)
        start = await self.call(
            "thread/start", {"cwd": str(workspace), "ephemeral": True}
        )
        thread = as_object(start.get("thread"))
        require(thread.get("ephemeral") is True, "interrupt_thread_not_ephemeral")
        thread_id = thread.get("id")
        require(isinstance(thread_id, str) and bool(thread_id), "missing_thread_id")
        thread_id = cast("str", thread_id)
        usage = TurnUsage(thread_id)
        usage_ready = asyncio.Event()
        stop = asyncio.Event()
        turn_id: str | None = None

        def observe(notification: Notification) -> None:
            snapshot = decode_usage(notification)
            if snapshot is not None:
                require(
                    snapshot.counts == EXPECTED_USAGE[0],
                    "interrupt_unexpected_usage_counts",
                )
                usage.accept(snapshot)
                usage_ready.set()

        def observe_before_start(notification: Notification) -> None:
            reject_premature_usage(notification)

        def started(identity: str) -> None:
            nonlocal turn_id
            turn_id = identity

        with ExitStack() as resources:
            listener = resources.enter_context(socket.socket(socket.AF_UNIX))
            listener.setblocking(False)
            listener.bind(str(CONTROL / "interrupt.sock"))
            listener.listen(1)
            observation = asyncio.create_task(
                observe_app_server_turn(
                    self.rpc,
                    thread_id=thread_id,
                    prompt="Run the fixture action.",
                    deadline=self.deadline,
                    observe=observe,
                    observe_before_send=observe_before_start,
                    on_waiting=self.waiting_terminal.set,
                    on_started=started,
                    stop=stop,
                )
            )
            try:
                await self.waiting_terminal.wait()
                require(turn_id is not None, "interrupt_start_identity_missing")
                acknowledged_turn = cast("str", turn_id)
                child_fd: int | None = None
                loop = asyncio.get_running_loop()
                if fixture.case == "interrupt-model":
                    await fixture.stream_ready.wait()
                else:
                    child, _ = await loop.sock_accept(listener)
                    resources.enter_context(child)
                    child.setblocking(False)
                    raw_pid = bytearray()
                    while not raw_pid.endswith(b"\n"):
                        part = await loop.sock_recv(child, 16 - len(raw_pid))
                        require(bool(part), "interrupt_child_pid_missing")
                        raw_pid.extend(part)
                        require(len(raw_pid) < 16, "interrupt_child_pid_limit")
                    require(
                        re.fullmatch(rb"[1-9][0-9]{0,9}\n", raw_pid) is not None,
                        "interrupt_child_pid_invalid",
                    )
                    child_fd = open_child_pidfd(int(raw_pid))
                    resources.callback(os.close, child_fd)
                    verify_interrupt_hooks()
                    diagnostic("INFO", "probe.interrupt_barrier", "command_ready")
                    await usage_ready.wait()
                    require(
                        usage.for_turn(acknowledged_turn) is not None,
                        "interrupt_usage_missing",
                    )
                    diagnostic("INFO", "probe.interrupt_barrier", "usage_ready")
                    require(
                        not select.select([child_fd], [], [], 0)[0],
                        "interrupt_child_exited_early",
                    )
                require(not observation.done(), "interrupt_terminal_before_stop")
                require(process.returncode is None, "interrupt_app_server_exited_early")
                if fixture.case == "interrupt-kill":
                    process.kill()
                    try:
                        await observation
                    except RpcError as error:
                        require(
                            error.reason is RpcFailure.DISCONNECTED,
                            "interrupt_kill_not_eof",
                        )
                    else:
                        raise ProbeFailure("interrupt_kill_terminal_observed")
                    require(
                        await process.wait() == -signal.SIGKILL, "interrupt_kill_status"
                    )
                else:
                    stop.set()
                    self.terminal = await observation
                    require(
                        self.terminal.status == "interrupted",
                        "interrupt_status_invalid",
                    )
                    if child_fd is not None:
                        await wait_pidfd_exit(child_fd)
                    else:
                        await fixture.stream_closed.wait()
                    require(process.returncode is None, "interrupt_app_server_exited")
                snapshot = usage.for_turn(acknowledged_turn)
                require(
                    (snapshot is None)
                    if fixture.case == "interrupt-model"
                    else snapshot is not None and snapshot.counts == EXPECTED_USAGE[0],
                    "interrupt_usage_not_preserved",
                )
                require(not usage.observation_gap, "interrupt_usage_gap")
                require(
                    not os.path.lexists(workspace / MARKER), "interrupt_marker_exists"
                )
            finally:
                observation.cancel()
                await asyncio.gather(observation, return_exceptions=True)


def open_child_pidfd(pid: int) -> int:
    if sys.platform == "linux":
        return os.pidfd_open(pid)
    raise ProbeFailure("interrupt_requires_linux")


def verify_interrupt_hooks() -> None:
    with RECEIPTS.open("rb") as receipts:
        raw = receipts.read(4097)
    require(len(raw) <= 4096, "interrupt_receipt_limit")
    lines = raw.splitlines()
    require(len(lines) == 2, "interrupt_receipt_count")
    for line, call_id, command in zip(
        lines, (INTERRUPT_PRIMER_ID, CALL_ID), (":", INTERRUPT_COMMAND), strict=True
    ):
        receipt = parse_object(line)
        pid = receipt.pop("pid", None)
        require(type(pid) is int and pid > 1, "interrupt_hook_pid_invalid")
        require(
            receipt
            == {
                "event": "PreToolUse",
                "tool": "Bash",
                "call": call_id,
                "command": command,
            },
            "interrupt_receipt_invalid",
        )


async def wait_pidfd_exit(descriptor: int) -> None:
    loop = asyncio.get_running_loop()
    exited: asyncio.Future[None] = loop.create_future()

    def observe_exit() -> None:
        if not exited.done():
            exited.set_result(None)

    loop.add_reader(descriptor, observe_exit)
    try:
        await exited
    finally:
        loop.remove_reader(descriptor)


def native_environment(root: Path) -> dict[str, str]:
    home = root / "home"
    codex_home = root / "codex"
    home.mkdir(parents=True)
    codex_home.mkdir()
    return {
        "HOME": str(home),
        "CODEX_HOME": str(codex_home),
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "SHELL": "/bin/bash",
        "LANG": "C.UTF-8",
    }


def environment(root: Path, port: int, case: ProbeCase) -> dict[str, str]:
    child_env = native_environment(root)
    codex_home = Path(child_env["CODEX_HOME"])
    provider = "openai" if case == "implicit_account" else "kora_probe"
    case_settings = ""
    if case in PROVIDER_CASES:
        case_settings = 'cli_auth_credentials_store = "file"\n'
        require(not os.path.lexists(codex_home / "auth.json"), "provider_store_exists")
    if case == "implicit_account":
        case_settings += f'openai_base_url = "http://127.0.0.1:{port}/v1"\n'
    if case in SHELL_ARGUMENTS:
        case_settings = "allow_login_shell = true\n"
    config = f"""model = "gpt-5.5"
model_provider = "{provider}"
approval_policy = "never"
sandbox_mode = "danger-full-access"
web_search = "disabled"
{case_settings}
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
    if case in EXECUTOR_CASES:
        config += EXECUTOR_SHELL_POLICY
        child_env["CODEX_EXEC_SERVER_URL"] = "none"
    (codex_home / "config.toml").write_text(config, encoding="utf-8")
    if case in {"implicit_account", "provider_key_present"}:
        child_env["CODEX_API_KEY"] = SYNTHETIC_BEARER
    return child_env


async def start_executor(root: Path, workspace: Path) -> asyncio.subprocess.Process:
    child_env = native_environment(root)
    child_env["KORA_PROBE_EXECUTOR_ORIGIN"] = EXECUTOR_ORIGIN
    config = 'web_search = "disabled"\n[analytics]\nenabled = false\n'
    (Path(child_env["CODEX_HOME"]) / "config.toml").write_text(
        config + EXECUTOR_SHELL_POLICY, encoding="utf-8"
    )
    return await asyncio.create_subprocess_exec(
        CODEX,
        "exec-server",
        "--strict-config",
        "--listen",
        "ws://127.0.0.1:0",
        "--ws-auth",
        "capability-token",
        "--ws-token-sha256",
        hashlib.sha256(SYNTHETIC_EXECUTOR_BEARER.encode()).hexdigest(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        limit=128,
        start_new_session=True,
        cwd=workspace,
        env=child_env,
    )


async def read_executor_url(process: asyncio.subprocess.Process) -> str:
    assert process.stdout is not None
    line = await process.stdout.readline()
    match = EXECUTOR_LISTEN_URL.fullmatch(line)
    if match is None or int(match[1]) > 65_535:
        raise ProbeFailure("invalid_executor_readiness")
    return line[:-1].decode("ascii")


def hook_pid(
    command: str = COMMAND, *, nested_input: bool = False, tool: str = "Bash"
) -> int:
    require(RECEIPTS.is_file(), "hook_receipt_missing")
    with RECEIPTS.open("rb") as file:
        raw = file.read(4097)
    require(len(raw) <= 4096, "hook_receipt_size_limit")
    lines = raw.splitlines()
    require(len(lines) == 1, "hook_receipt_count")
    receipt = parse_object(lines[0])
    pid = receipt.pop("pid", None)
    expected_input = (
        {"tool_input": {"command": command}} if nested_input else {"command": command}
    )
    require(
        receipt
        == {"event": "PreToolUse", "tool": tool, "call": CALL_ID, **expected_input},
        "unexpected_hook_receipt",
    )
    if type(pid) is not int or pid <= 1:
        raise ProbeFailure("invalid_hook_pid")
    return pid


def verify_live_hook(pid: int) -> None:
    try:
        state = (
            Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rpartition(") ")[2]
        )
    except FileNotFoundError:
        raise ProbeFailure("stalled_hook_exited_early") from None
    require(state.startswith(("S ", "R ", "D ")), "stalled_hook_not_live")


def signal_native_process(
    process: asyncio.subprocess.Process, sig: signal.Signals
) -> None:
    if process.returncode is None:
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass


async def drain(reader: asyncio.StreamReader) -> None:
    while await reader.read(16_384):
        pass


async def stop_native_process(process: asyncio.subprocess.Process) -> None:
    assert process.stdout is not None
    writer = process.stdin
    if writer is not None:
        writer.close()
    signal_native_process(process, signal.SIGTERM)
    try:
        async with asyncio.timeout(2):
            await asyncio.gather(process.wait(), drain(process.stdout))
    except TimeoutError:
        signal_native_process(process, signal.SIGKILL)
        await asyncio.gather(process.wait(), drain(process.stdout))
    if writer is not None:
        try:
            await writer.wait_closed()
        except (BrokenPipeError, ConnectionResetError):
            require(writer.is_closing(), "native_stdin_not_closed")


async def signal_after_request(
    process: asyncio.subprocess.Process, fixture: ResponsesFixture, stdio: Stdio
) -> None:
    await fixture.request_admitted.wait()
    await stdio.waiting_terminal.wait()
    if fixture.case == "messages-kill":
        require(stdio.messages is not None, "message_observer_missing")
        messages = cast("MessageProbe", stdio.messages)
        await messages.delta_seen.wait()
        messages.verify_partial()
    if fixture.case == "output-kill":
        require(stdio.outputs is not None, "output_observer_missing")
        outputs = cast("OutputProbe", stdio.outputs)
        await outputs.item_completed.wait()
        outputs.verify_pending()
    require(process.returncode is None, "client_child_exited_before_signal")
    process.kill()


async def observe_client_exit(
    process: asyncio.subprocess.Process,
    fixture: ResponsesFixture,
    stdio: Stdio,
    workspace: Path,
) -> None:
    killer = asyncio.create_task(signal_after_request(process, fixture, stdio))
    try:
        try:
            await stdio.observe(workspace, fixture.case)
        except RpcError as error:
            require(error.reason is RpcFailure.DISCONNECTED, "client_exit_not_eof")
            require(stdio.terminal is None, "client_exit_terminal_observed")
        else:
            raise ProbeFailure("client_exit_turn_completed")
        require(killer.done(), "client_child_not_signalled")
        killer.result()
        require(await process.wait() == -signal.SIGKILL, "client_exit_status_mismatch")
        if fixture.case == "messages-kill":
            require(stdio.messages is not None, "message_observer_missing")
            cast("MessageProbe", stdio.messages).verify_partial()
            await fixture.stream_closed.wait()
        if fixture.case == "output-kill":
            require(stdio.outputs is not None, "output_observer_missing")
            cast("OutputProbe", stdio.outputs).verify_pending()
            await fixture.stream_closed.wait()
    finally:
        if not killer.done():
            killer.cancel()
            await asyncio.wait((killer,))
        if not killer.cancelled():
            killer.exception()


def startup_identity(case: ProbeCase, prepared_home: Path) -> StartupIdentity:
    expected_home = prepared_home
    if case == "startup-home":
        expected_home = prepared_home.with_name("other-codex-home")
        expected_home.mkdir()
    return StartupIdentity(
        version="0.0.0" if case == "startup-version" else CODEX_VERSION,
        platform_os="linux",
        codex_home=expected_home.resolve(strict=True),
        client_name="kora-hook-probe",
        client_version="1",
    )


async def drive(fixture: ResponsesFixture, root: Path, workspace: Path) -> None:
    deadline = asyncio.get_running_loop().time() + TURN_SECONDS
    async with asyncio.timeout_at(deadline):
        server = await asyncio.start_server(
            lambda reader, writer: fixture.accept(reader, writer, deadline),
            "127.0.0.1",
            0,
            limit=16_384,
        )
    process: asyncio.subprocess.Process | None = None
    executor: asyncio.subprocess.Process | None = None
    executor_url: str | None = None
    try:
        port = int(server.sockets[0].getsockname()[1])
        args = ["app-server", "--listen", "stdio://"]
        if (
            fixture.case in PROVIDER_CASES
            or fixture.case in SHELL_ARGUMENTS
            or fixture.case in CLIENT_CASES
            or fixture.case in STARTUP_REFUSALS
            or fixture.case in HOOK_INPUT_CASES
            or fixture.case in INTERRUPT_CASES
            or fixture.case in ERROR_CASES
            or fixture.case in MESSAGE_CASES
            or fixture.case in OUTPUT_CASES
        ):
            args.append("--strict-config")
        async with asyncio.timeout_at(deadline):
            if fixture.case in EXECUTOR_CASES:
                executor = await start_executor(root / "executor", workspace)
                executor_url = await read_executor_url(executor)
            child_env = environment(root, port, fixture.case)
            identity = startup_identity(fixture.case, Path(child_env["CODEX_HOME"]))
            process = await asyncio.create_subprocess_exec(
                CODEX,
                *args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                limit=MAX_FRAME_BYTES,
                start_new_session=True,
                cwd=workspace,
                env=child_env,
            )
            assert process.stdin is not None
            assert process.stdout is not None
            rpc = AppServerRpc(
                process.stdout,
                process.stdin,
                clock=asyncio.get_running_loop().time,
                next_request_id=lambda: secrets.randbits(63),
                max_frame_bytes=MAX_FRAME_BYTES,
                max_messages=MAX_MESSAGES,
            )
            stdio = Stdio(rpc, deadline, identity)
            if fixture.case in INTERRUPT_CASES:
                await stdio.interrupt_turn(workspace, fixture, process)
            elif fixture.case in {"client-exit", "messages-kill", "output-kill"}:
                await observe_client_exit(process, fixture, stdio, workspace)
            else:
                await stdio.observe(workspace, fixture.case, executor_url)
        require(fixture.case not in STALLED_CASES, "stalled_turn_completed")
    except (TimeoutError, RpcError) as error:
        if isinstance(error, RpcError) and error.reason is not RpcFailure.DEADLINE:
            raise
        if fixture.case not in STALLED_CASES:
            raise ProbeFailure(fixture.failure or "turn_deadline_expired") from None
        require(
            fixture.failure is None and fixture.requests == 1,
            "stalled_fixture_state",
        )
        if fixture.case == "executor-stall":
            require(fixture.connections == 1, "unexpected_stalled_executor_connections")
            require(
                process is not None
                and process.returncode is None
                and executor is not None
                and executor.returncode is None,
                "stalled_native_process_exited",
            )
            verify_live_hook(verify_shell_hook(EXECUTOR_COMMAND))
        else:
            verify_live_hook(hook_pid())
    except ProbeFailure:
        if fixture.failure is not None:
            raise ProbeFailure(fixture.failure) from None
        raise
    finally:
        cleanup_deadline = asyncio.get_running_loop().time() + CLEANUP_SECONDS
        if process is not None and (
            fixture.case in PROVIDER_CASES or fixture.case in STARTUP_REFUSALS
        ):
            try:
                async with asyncio.timeout_at(cleanup_deadline):
                    await stop_native_process(process)
            finally:
                async with asyncio.timeout_at(cleanup_deadline):
                    await fixture.close(server)
        else:
            async with asyncio.timeout_at(cleanup_deadline):
                pending = [fixture.close(server)]
                for child in (process, executor):
                    if child is not None:
                        pending.append(stop_native_process(child))
                outcomes = await asyncio.gather(*pending, return_exceptions=True)
                for outcome in outcomes:
                    if isinstance(outcome, BaseException):
                        raise outcome


async def run_case(case: Case, one_shot: bool = False) -> None:
    RECEIPTS.unlink(missing_ok=True)
    (CONTROL / "decision").write_text(
        "allow" if case == "usage" else case, encoding="utf-8"
    )
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
        if case in {"allow", "interactive", "usage"}:
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


async def run_interrupt_case(case: InterruptCase) -> None:
    RECEIPTS.unlink(missing_ok=True)
    (CONTROL / "decision").write_text("interrupt", encoding="utf-8")
    with TemporaryDirectory(prefix="kora-codex-", dir="/tmp") as scratch:
        root = Path(scratch)
        workspace = root / "workspace"
        workspace.mkdir()
        fixture = ResponsesFixture(case, one_shot=True)

        await drive(fixture, root, workspace)

        require(fixture.failure is None, fixture.failure or "interrupt_fixture_failed")
        require(fixture.requests == fixture.request_limit, "interrupt_request_count")
        require(not os.path.lexists(workspace / MARKER), "interrupt_marker_exists")
        require(
            RECEIPTS.is_file() == (case != "interrupt-model"),
            "interrupt_hook_receipt_state",
        )
    diagnostic("INFO", "probe.case_verified", case)


async def run_output_case(case: OutputCase) -> None:
    RECEIPTS.unlink(missing_ok=True)
    (CONTROL / "decision").write_text("deny", encoding="utf-8")
    with TemporaryDirectory(prefix="kora-codex-", dir="/tmp") as scratch:
        root = Path(scratch)
        workspace = root / "workspace"
        workspace.mkdir()
        fixture = ResponsesFixture(case, one_shot=False)

        await drive(fixture, root, workspace)

        require(fixture.failure is None, fixture.failure or "output_fixture_failed")
        require(fixture.requests == fixture.connections == 1, "output_request_count")
        require(not RECEIPTS.exists(), "output_hook_ran")
        require(not os.path.lexists(workspace / MARKER), "output_marker_exists")
    diagnostic("INFO", "probe.case_verified", case)


async def run_message_case(case: MessageCase) -> None:
    RECEIPTS.unlink(missing_ok=True)
    (CONTROL / "decision").write_text("deny", encoding="utf-8")
    with TemporaryDirectory(prefix="kora-codex-", dir="/tmp") as scratch:
        root = Path(scratch)
        workspace = root / "workspace"
        workspace.mkdir()
        fixture = ResponsesFixture(case, one_shot=False)

        await drive(fixture, root, workspace)

        require(fixture.failure is None, fixture.failure or "message_fixture_failed")
        require(fixture.requests == fixture.connections == 1, "message_request_count")
        require(not RECEIPTS.exists(), "message_hook_ran")
        require(not os.path.lexists(workspace / MARKER), "message_marker_exists")
    diagnostic("INFO", "probe.case_verified", case)


async def run_error_case(case: ErrorCase) -> None:
    RECEIPTS.unlink(missing_ok=True)
    (CONTROL / "decision").write_text("deny", encoding="utf-8")
    with TemporaryDirectory(prefix="kora-codex-", dir="/tmp") as scratch:
        root = Path(scratch)
        workspace = root / "workspace"
        workspace.mkdir()
        fixture = ResponsesFixture(case, one_shot=False)

        await drive(fixture, root, workspace)

        require(
            fixture.failure is None, fixture.failure or "native_error_fixture_failed"
        )
        require(
            fixture.requests == fixture.connections == 1, "native_error_request_count"
        )
        require(not RECEIPTS.exists(), "native_error_hook_ran")
        require(not os.path.lexists(workspace / MARKER), "native_error_marker_exists")
    diagnostic("INFO", "probe.case_verified", case)


async def run_hook_input_case(case: HookInputCase) -> None:
    (CONTROL / "decision").write_text(case, encoding="utf-8")
    with TemporaryDirectory(prefix="kora-codex-", dir="/tmp") as scratch:
        root = Path(scratch)
        workspace = root / "workspace"
        workspace.mkdir()
        marker = workspace / MARKER
        fixture = ResponsesFixture(case, one_shot=False)
        await drive(fixture, root, workspace)
        require(fixture.failure is None, fixture.failure or "unexpected_fixture_state")
        require(
            fixture.connections == fixture.requests == 2,
            "unexpected_hook_input_request_count",
        )
        with (CONTROL / "invocations").open("rb") as invocations:
            require(
                invocations.read(2) == b"1", "unexpected_hook_input_invocation_count"
            )
        if case == "decoder-refuse":
            require(not os.path.lexists(RECEIPTS), "decoder_refusal_receipt_exists")
        else:
            hook_pid(PATCH, tool="apply_patch")
        if case == "patch-allow":
            require(
                marker.is_file() and not marker.is_symlink(),
                "direct_patch_marker_missing",
            )
            with marker.open("rb") as contents:
                require(
                    contents.read(len(PATCH_MARKER) + 1) == PATCH_MARKER,
                    "direct_patch_marker_content",
                )
        else:
            require(not os.path.lexists(marker), "blocked_hook_input_marker_exists")
    diagnostic("INFO", "probe.case_verified", case)


async def run_provider_case(case: ProviderCase | ClientCase) -> None:
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


async def run_startup_refusal(case: StartupCase) -> None:
    with TemporaryDirectory(prefix="kora-codex-", dir="/tmp") as scratch:
        root = Path(scratch)
        workspace = root / "workspace"
        workspace.mkdir()
        fixture = ResponsesFixture(case, one_shot=False)
        try:
            await drive(fixture, root, workspace)
        except StartupError as error:
            require(
                error.reason is STARTUP_REFUSALS[case], "unexpected_startup_refusal"
            )
            reason = error.reason
        else:
            raise ProbeFailure("startup_mismatch_accepted")
        require(fixture.failure is None, fixture.failure or "unexpected_fixture_state")
        require(
            fixture.connections == fixture.requests == 0, "unexpected_startup_traffic"
        )
    diagnostic("INFO", "probe.startup_refused", reason.value)
    diagnostic("INFO", "probe.case_verified", case)


def verify_shell_hook(command: str) -> int:
    with (CONTROL / "invocations").open("rb") as invocations:
        require(invocations.read(2) == b"1", "shell_hook_invocation_count")
    return hook_pid(command, nested_input=True)


async def run_shell_case(case: ShellCase | ExecutorCase) -> None:
    selected = case in EXECUTOR_CASES
    if case == "executor-bash":
        require(not os.path.lexists(EXECUTOR_BASH_OVERRIDE), "shell_override_exists")
    decision = "shell_controls"
    if selected:
        decision = (
            case if case in {"executor-deny", "executor-stall"} else "executor-allow"
        )
    (CONTROL / "decision").write_text(decision, encoding="utf-8")
    with TemporaryDirectory(prefix="kora-codex-", dir="/tmp") as scratch:
        root = Path(scratch)
        workspace = root / "workspace"
        workspace.mkdir()
        marker = workspace / MARKER
        fixture = ResponsesFixture(case, one_shot=True)
        await drive(fixture, root, workspace)
        require(fixture.failure is None, fixture.failure or "unexpected_fixture_state")
        require(
            fixture.connections == fixture.requests == fixture.request_limit,
            "unexpected_shell_request_count",
        )
        verify_shell_hook(EXECUTOR_COMMAND if selected else SHELL_COMMAND)
        if case in {
            "shell-login",
            "executor-sh",
            "executor-login",
            "executor-deny",
            "executor-stall",
        }:
            require(not os.path.lexists(marker), "refused_shell_command_marker_exists")
        else:
            bash = Path("/bin/bash").resolve(strict=True)
            posix_sh = Path("/bin/sh").resolve(strict=True)
            require(bash != posix_sh, "shell_executables_not_distinct")
            expected = (
                f"{posix_sh}\nposix-sh\n"
                if case == "shell-alternate"
                else f"{bash}\nbash-non-login\n"
            )
            if selected:
                expected += f"{EXECUTOR_ORIGIN}\n"
            expected_bytes = expected.encode()
            require(
                marker.is_file() and not marker.is_symlink(), "shell_marker_missing"
            )
            with marker.open("rb") as contents:
                require(
                    contents.read(len(expected_bytes) + 1) == expected_bytes,
                    "unexpected_shell_marker",
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
    elif sys.argv[1:] == ["--client-complete"]:
        await run_provider_case("client-complete")
    elif sys.argv[1:] == ["--client-exit"]:
        await run_provider_case("client-exit")
    elif sys.argv[1:] == ["--check-app-server-usage"]:
        await run_case("usage")
    elif sys.argv[1:] == ["--interrupt-model"]:
        await run_interrupt_case("interrupt-model")
    elif sys.argv[1:] == ["--interrupt-command"]:
        await run_interrupt_case("interrupt-command")
    elif sys.argv[1:] == ["--interrupt-kill"]:
        await run_interrupt_case("interrupt-kill")
    elif len(sys.argv) == 2 and sys.argv[1].removeprefix("--") in OUTPUT_CASES:
        await run_output_case(cast("OutputCase", sys.argv[1].removeprefix("--")))
    elif len(sys.argv) == 2 and sys.argv[1].removeprefix("--") in MESSAGE_CASES:
        await run_message_case(cast("MessageCase", sys.argv[1].removeprefix("--")))
    elif len(sys.argv) == 2 and sys.argv[1].removeprefix("--") in ERROR_CASES:
        await run_error_case(cast("ErrorCase", sys.argv[1].removeprefix("--")))
    elif sys.argv[1:] == ["--startup-version"]:
        await run_startup_refusal("startup-version")
    elif sys.argv[1:] == ["--startup-home"]:
        await run_startup_refusal("startup-home")
    elif sys.argv[1:] == ["--patch-allow"]:
        await run_hook_input_case("patch-allow")
    elif sys.argv[1:] == ["--patch-deny"]:
        await run_hook_input_case("patch-deny")
    elif sys.argv[1:] == ["--decoder-refuse"]:
        await run_hook_input_case("decoder-refuse")
    elif len(sys.argv) == 2 and sys.argv[1] in SHELL_FLAGS:
        await run_shell_case(SHELL_FLAGS[sys.argv[1]])
    else:
        require(not sys.argv[1:], "unexpected_arguments")
        await run_case("allow")
        await run_case("deny")


def report_connection_failure(
    error: RpcError
    | StartupError
    | TurnError
    | UsageError
    | NativeErrorRefused
    | MessageRefused,
) -> int:
    diagnostic("ERROR", "probe.case_failed", error.reason.value)
    return 1


def main() -> int:
    try:
        asyncio.run(run())
    except ProbeFailure as error:
        diagnostic("ERROR", "probe.case_failed", str(error))
        return 1
    except (
        RpcError,
        StartupError,
        TurnError,
        UsageError,
        NativeErrorRefused,
        MessageRefused,
    ) as error:
        return report_connection_failure(error)
    except TimeoutError:
        diagnostic("ERROR", "probe.case_failed", "cleanup_deadline_expired")
        return 1
    except (OSError, ValueError, IndexError):
        diagnostic("ERROR", "probe.case_failed", "invalid_fixture_io")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
