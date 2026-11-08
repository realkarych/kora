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
from datetime import UTC, datetime
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
    CompletedMessage,
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
from kora.plugins.harness.codex.app_server_session import AppServerTurnSession
from kora.plugins.harness.codex.app_server_startup import (
    StartupError,
    StartupFailure,
    StartupIdentity,
    initialize_app_server,
)
from kora.plugins.harness.codex.app_server_tools import (
    ToolCounts,
    ToolObservation,
    ToolRefused,
    reject_premature_tool,
)
from kora.plugins.harness.codex.app_server_turn import (
    TurnCompletion,
    TurnError,
    observe_app_server_turn,
    reject_premature_completion,
)
from kora.plugins.harness.codex.app_server_usage import (
    ConfirmedUsage,
    TokenCounts,
    TurnUsage,
    UsageChange,
    UsageError,
    UsageObserver,
    decode_usage,
    reject_premature_usage,
)
from kora.plugins.harness.codex.launch_configuration import CODEX_VERSION
from kora.plugins.harness.codex.selected_startup import (
    SelectedEnvironment,
    SelectedStartupError,
    SelectedStartupFailure,
    prepare_selected_thread,
)
from tests.plugins.harness.codex.selected_launch import (
    HOOK_TIMEOUT_SOCKET,
    METADATA_ENV_CONTROL,
    METADATA_ENVIRONMENT,
    METADATA_MARKER,
    PROJECT_DOC_CANARY,
    SELECTED_AUTH_CASES,
    SELECTED_BASH_STARTUP_CASES,
    SELECTED_BASH_TRACE_CASES,
    SELECTED_COMMAND,
    SELECTED_DENIAL,
    SELECTED_HOOK_CASES,
    SELECTED_HOOK_INVENTORY_REFUSALS,
    SELECTED_HOOK_TIMEOUT_CASES,
    SELECTED_LAUNCH_CASES,
    SELECTED_MODEL,
    SELECTED_NOISE_REGISTRY_CASES,
    SELECTED_PATCH_CASES,
    SELECTED_REFUSALS,
    SELECTED_SHELL_PATCH_CASES,
    SELECTED_SQLITE_HOME_CASES,
    SELECTED_TLS_CASES,
    SELECTED_TLS_OVERRIDES,
    SELECTED_TOOL_INVENTORY_CASES,
    SELECTED_TURN_CASES,
    SHELL_PATCH_DENIAL,
    SHELL_PATCH_POLICY_OUTCOMES,
    SHELL_PATCH_POLICY_RECEIPT,
    SHELL_PATCH_WORKSPACE,
    SQLITE_HOME_CANARY,
    SelectedHookCase,
    SelectedLaunchCase,
    SelectedNoiseRegistryCase,
    materialize_native_inputs,
    plant_checkout_canaries,
    prepare_native_inputs,
    role_homes,
    selected_command,
    selected_managed_hook,
    workspace_remains_untrusted,
)
from tests.plugins.harness.codex.selected_tls import (
    SelectedTls,
    SelectedTlsError,
    UnknownCaAlert,
    create_selected_tls,
    start_selected_tls_server,
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
    | SelectedLaunchCase
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

PATCH_DENIAL = "kora managed hook probe denied the direct patch"
SELECTED_HOOK_OUTCOMES: dict[SelectedHookCase, tuple[str, tuple[str, str] | None]] = {
    "selected-command-exit-seven": ("completed", None),
    "selected-hook-deny": ("blocked", ("feedback", SELECTED_DENIAL)),
    "selected-hook-no-reason": (
        "failed",
        (
            "error",
            "PreToolUse hook exited with code 2 "
            "but did not write a blocking reason to stderr",
        ),
    ),
    "selected-hook-exit-one": ("failed", ("error", "hook exited with code 1")),
    "selected-hook-timeout": ("failed", ("error", "hook timed out after 5s")),
    "selected-patch-allow": ("completed", None),
    "selected-patch-deny": ("blocked", ("feedback", PATCH_DENIAL)),
    "selected-patch-exit-one": ("failed", ("error", "hook exited with code 1")),
    "selected-patch-timeout": ("failed", ("error", "hook timed out after 5s")),
    "selected-shell-patch-allow": ("completed", None),
    "selected-shell-patch-protected": ("blocked", ("feedback", SHELL_PATCH_DENIAL)),
    "selected-shell-patch-exit-one": ("failed", ("error", "hook exited with code 1")),
    "selected-shell-patch-timeout": ("failed", ("error", "hook timed out after 5s")),
}

CODEX = "/usr/local/bin/codex"
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
EVENT_CANARY = "kora-event-mask-canary"
OUTPUT_CANARY = "kora-candidate-content-canary"
SELECTED_OUTPUT = {"ok": True, "note": f"{EVENT_CANARY} {OUTPUT_CANARY}"}
SELECTED_OUTPUT_TEXT = json.dumps(SELECTED_OUTPUT, separators=(",", ":"))
SELECTED_OUTPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}, "note": {"type": "string"}},
    "required": ["ok", "note"],
    "additionalProperties": False,
}
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
DIRECT_PATCH_CASES = PATCH_CASES | SELECTED_PATCH_CASES
SELECTED_FILE_CHANGE_CASES = SELECTED_PATCH_CASES | SELECTED_SHELL_PATCH_CASES
PATCH_WRITE_CASES = frozenset(
    {
        "patch-allow",
        "selected-patch-allow",
        "selected-patch-exit-one",
        "selected-patch-timeout",
        "selected-shell-patch-allow",
        "selected-shell-patch-exit-one",
        "selected-shell-patch-timeout",
    }
)
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
BASH_STARTUP_SCRIPT = "fixture-startup.sh"
BASH_EXTRA_EFFECT_MARKER = "kora-probe.startup"
BASH_EXTRA_EFFECT_TEXT = "shell startup marker"
BASH_TRACE_PREFIX = "kora-fixture-trace:"
COMMAND = f"touch {MARKER}"
CALL_ID = "kora-hook-probe-call"
PATCH_MARKER = b"direct patch marker\n"
PATCH = (
    f"*** Begin Patch\n*** Add File: {MARKER}\n+direct patch marker\n*** End Patch\n"
)
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


def output_response(
    case: ProbeCase, *, complete: bool, usage: dict[str, object] | None = None
) -> bytes:
    text = OUTPUT_TEXT
    if case in SELECTED_TURN_CASES:
        text = SELECTED_OUTPUT_TEXT
    elif case == "output-invalid":
        text = INVALID_OUTPUT_TEXT
    item: dict[str, object] = {
        "id": OUTPUT_MESSAGE,
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }
    if complete:
        return sse("kora-output-response", item, usage=usage)
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


EXPECTED_SELECTED_TOOL_INVENTORY = (
    ("", "custom", "apply_patch"),
    ("", "function", "create_goal"),
    ("", "function", "exec_command"),
    ("", "function", "get_goal"),
    ("", "function", "request_user_input"),
    ("", "function", "update_goal"),
    ("", "function", "view_image"),
    ("", "tool_search", "tool_search"),
)


def tool_inventory_name(value: object) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", value) is None
    ):
        raise ProbeFailure("invalid_tool_inventory_name")
    return value


def tool_inventory(
    tools: list[dict[str, object]],
) -> tuple[tuple[str, str, str], ...]:
    require(len(tools) <= 64, "invalid_tool_inventory_size")
    entries: list[tuple[str, str, str]] = []
    for tool in tools:
        kind = tool.get("type")
        if not isinstance(kind, str):
            raise ProbeFailure("invalid_tool_inventory_type")
        if kind in ("tool_search", "web_search"):
            require("name" not in tool, "unexpected_tool_inventory_name")
            entries.append(("", kind, kind))
            continue
        if kind not in ("function", "custom", "namespace"):
            raise ProbeFailure("invalid_tool_inventory_type")
        name = tool_inventory_name(tool.get("name"))
        entries.append(("", kind, name))
        if kind == "namespace":
            members = as_list(tool.get("tools"), "missing_tool_inventory_namespace")
            require(len(entries) + len(members) <= 64, "invalid_tool_inventory_size")
            for item in members:
                member = as_object(item)
                member_kind = member.get("type")
                if not isinstance(member_kind, str) or member_kind not in (
                    "function",
                    "custom",
                ):
                    raise ProbeFailure("invalid_tool_inventory_member_type")
                entries.append(
                    (name, member_kind, tool_inventory_name(member.get("name")))
                )
    require(len(entries) <= 64, "invalid_tool_inventory_size")
    require(len(set(entries)) == len(entries), "duplicate_tool_inventory_entry")
    return tuple(sorted(entries))


def verify_selected_tool_inventory(tools: list[dict[str, object]]) -> None:
    require(bool(tools), "invalid_tool_inventory_size")
    inventory = tool_inventory(tools)
    require(
        ("", "function", "exec_command") in inventory
        and ("", "custom", "apply_patch") in inventory
        and not any(
            name in {"web_search", "write_stdin", "update_plan"}
            for _, _, name in inventory
        ),
        "unexpected_tool_inventory_exposure",
    )
    require(
        inventory == EXPECTED_SELECTED_TOOL_INVENTORY, "selected_tool_inventory_drift"
    )
    diagnostic("INFO", "probe.selected_tool_inventory", json.dumps(inventory))


SEARCH_CALL_ID = "kora-selected-tool-search-call"
SEARCH_RESPONSE = sse(
    "kora-selected-tool-search-response",
    {
        "type": "tool_search_call",
        "call_id": SEARCH_CALL_ID,
        "execution": "client",
        "arguments": {"query": "spawn agent", "limit": 8},
    },
)
EXPECTED_SELECTED_SEARCH_INVENTORY = (
    ("", "namespace", "multi_agent_v1"),
    ("multi_agent_v1", "function", "close_agent"),
    ("multi_agent_v1", "function", "resume_agent"),
    ("multi_agent_v1", "function", "send_input"),
    ("multi_agent_v1", "function", "spawn_agent"),
    ("multi_agent_v1", "function", "wait_agent"),
)


def verify_selected_tool_search(request: dict[str, object]) -> None:
    items = [
        as_object(item)
        for item in as_list(request.get("input"), "responses_input_missing")
    ]
    outputs = [item for item in items if item.get("type") == "tool_search_output"]
    require(
        len(outputs) == 1
        and outputs[0].get("call_id") == SEARCH_CALL_ID
        and outputs[0].get("execution") == "client"
        and outputs[0].get("status") == "completed",
        "unexpected_selected_tool_search_result",
    )
    tools = [
        as_object(tool)
        for tool in as_list(outputs[0].get("tools"), "missing_selected_search_tools")
    ]
    inventory = tool_inventory(tools)
    require(
        inventory == EXPECTED_SELECTED_SEARCH_INVENTORY,
        "selected_tool_search_inventory_drift",
    )
    diagnostic("INFO", "probe.selected_tool_search", json.dumps(inventory))


DISABLED_PLAN_RESPONSE = sse(
    "kora-probe-response-1",
    {
        "type": "function_call",
        "call_id": CALL_ID,
        "name": "update_plan",
        "arguments": json.dumps(
            {"plan": [{"step": "Fixture step", "status": "pending"}]}
        ),
    },
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
FIRST_RESPONSE_USAGE: dict[str, object] = {
    "input_tokens": 100,
    "input_tokens_details": {"cached_tokens": 20, "cache_write_tokens": 30},
    "output_tokens": 10,
    "output_tokens_details": {"reasoning_tokens": 4},
    "total_tokens": 110,
}
SECOND_RESPONSE_USAGE: dict[str, object] = {
    "input_tokens": 60,
    "input_tokens_details": {"cached_tokens": 5, "cache_write_tokens": 10},
    "output_tokens": 20,
    "output_tokens_details": {"reasoning_tokens": 6},
    "total_tokens": 80,
}
USAGE_TOOL_RESPONSE = command_response(
    {"cmd": COMMAND, "login": False}, usage=FIRST_RESPONSE_USAGE
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
    usage=SECOND_RESPONSE_USAGE,
)
USAGE_SOURCE_TIME = datetime(2026, 1, 1, tzinfo=UTC)
EXPECTED_USAGE = (TokenCounts(100, 20, 30, 10, 4), TokenCounts(160, 25, 40, 30, 10))


REGISTRY_ID = "kora-probe-registry"
REGISTRY_BEARER = "synthetic-noise-registry-token"
NOISE_ACCOUNT = "synthetic-noise-registry-account"
NOISE_REGISTRY_ROUTE = (
    f"POST /cloud/environment/{REGISTRY_ID}/connect HTTP/1.1".encode()
)


class ResponsesFixture:
    def __init__(self, case: ProbeCase, one_shot: bool) -> None:
        self.case: ProbeCase = case
        self.one_shot = one_shot
        self.request_limit: int
        if case == "selected-noise-registry-override":
            self.request_limit = 1
        elif case in SELECTED_REFUSALS:
            self.request_limit = 0
        elif case in PROVIDER_CASES:
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
        self.first_usage_emitted = asyncio.Event()
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
                registry = self.case == "selected-noise-registry-override"
                expected_route = (
                    NOISE_REGISTRY_ROUTE if registry else b"POST /v1/responses HTTP/1.1"
                )
                require(
                    request_line == expected_route,
                    "unexpected_registry_route"
                    if registry
                    else "unexpected_responses_route",
                )
                fields = self.verify_headers(headers)
                size = int(fields.get(b"content-length", b"0"))
                require(0 < size <= MAX_FRAME_BYTES, "invalid_responses_size")
                body = await reader.readexactly(size)
                if self.case == "selected-startup-ready":
                    require(
                        PROJECT_DOC_CANARY not in body, "project_instructions_applied"
                    )
                request = parse_object(body)
                if registry:
                    require(
                        request.keys() == {"harness_public_key"}
                        and isinstance(request["harness_public_key"], dict),
                        "unexpected_registry_connect_body",
                    )
                    self.requests += 1
                    writer.write(
                        b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n"
                        b"Content-Length: 0\r\n\r\n"
                    )
                    await writer.drain()
                    diagnostic(
                        "INFO", "probe.selected_noise_registry_request_refused", "403"
                    )
                    return
                if self.case == "selected-startup-ready":
                    require(
                        request.get("model") == SELECTED_MODEL,
                        "project_model_applied",
                    )
                response = self.respond(request)
                self.request_admitted.set()
                second_selected = (
                    self.case in SELECTED_TURN_CASES and self.requests == 2
                )
                if second_selected:
                    await self.first_usage_emitted.wait()
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
                if self.case in {"interrupt-model", "messages-kill", "output-kill"} or (
                    second_selected and self.case == "selected-turn-cut"
                ):
                    prelude = (
                        response if second_selected else self.open_stream_prelude()
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
        account_count = 0
        for header in headers:
            key, separator, value = header.partition(b":")
            require(
                separator == b":" and HTTP_FIELD_NAME.fullmatch(key) is not None,
                "invalid_responses_header",
            )
            name = key.lower()
            if name == b"authorization":
                authorization_count += 1
            if name == b"chatgpt-account-id":
                account_count += 1
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
        if self.case == "selected-noise-registry-override":
            require(
                authorization_count == 1
                and fields[b"authorization"] == f"Bearer {REGISTRY_BEARER}".encode()
                and account_count == 1
                and fields[b"chatgpt-account-id"] == NOISE_ACCOUNT.encode(),
                "unexpected_registry_authorization",
            )
        elif self.case == "provider_key_present" or self.case in SELECTED_AUTH_CASES:
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
        if self.case in OUTPUT_CASES or self.case in SELECTED_TURN_CASES:
            formatting = as_object(as_object(request.get("text")).get("format"))
            schema = (
                SELECTED_OUTPUT_SCHEMA
                if self.case in SELECTED_TURN_CASES
                else OUTPUT_SCHEMA
            )
            require(
                formatting.get("type") == "json_schema"
                and formatting.get("schema") == schema,
                "native_output_schema_mismatch",
            )
        if self.case in OUTPUT_CASES:
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
            if self.case in SELECTED_TOOL_INVENTORY_CASES:
                require(
                    request.get("model") == SELECTED_MODEL, "inventory_model_mismatch"
                )
                verify_selected_tool_inventory(tools)
            if self.case in DIRECT_PATCH_CASES:
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
            if self.case == "selected-tool-inventory-disabled":
                return DISABLED_PLAN_RESPONSE
            if self.case == "selected-tool-search":
                return SEARCH_RESPONSE
            if self.case in SELECTED_LAUNCH_CASES:
                usage = (
                    FIRST_RESPONSE_USAGE if self.case in SELECTED_TURN_CASES else None
                )
                return command_response(
                    {"cmd": selected_command(self.case)}, usage=usage
                )
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

        if self.case in DIRECT_PATCH_CASES:
            output = tool_output(request, CALL_ID, "custom_tool_call_output")
            if self.case in PATCH_WRITE_CASES:
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

        if self.case == "selected-tool-search":
            verify_selected_tool_search(request)
            return FINAL_RESPONSE

        output = tool_output(request, CALL_ID)
        if self.case in SELECTED_SHELL_PATCH_CASES:
            verify_shell_patch_output(self.case, output)
        elif self.case == "selected-tool-inventory-disabled":
            require(
                output == "unsupported call: update_plan", "disabled_plan_not_refused"
            )
        elif self.case == "decoder-refuse":
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
        elif self.case in {"selected-deny", "selected-hook-deny"}:
            require(
                output
                == (
                    f"Command blocked by PreToolUse hook: {SELECTED_DENIAL}. "
                    f"Command: {SELECTED_COMMAND}"
                ),
                "selected_hook_denial_missing",
            )
        elif self.case in SHELL_ARGUMENTS or self.case in SELECTED_AUTH_CASES:
            header, separator, body = output.partition("\nOutput:\n")
            exit_code = 7 if self.case == "selected-command-exit-seven" else 0
            status = [
                line
                for line in header.splitlines()
                if line.startswith(
                    ("Process exited with code ", "Process running with session ID ")
                )
            ]
            require(
                bool(separator) and status == [f"Process exited with code {exit_code}"],
                "shell_command_failed",
            )
            if (
                self.case in SELECTED_HOOK_CASES
                or self.case in SELECTED_SQLITE_HOME_CASES
                or self.case in SELECTED_BASH_STARTUP_CASES
                or self.case == "selected-command-metadata"
            ):
                require(not body.strip(), "selected_fault_unexpected_command_output")
            if self.case in SELECTED_BASH_TRACE_CASES:
                verify_bash_trace_output(self.case, body)
        else:
            require(
                (DENIAL in output) == (self.case == "deny"),
                "unexpected_hook_denial_result",
            )
        if self.case in SELECTED_TURN_CASES:
            return output_response(
                self.case,
                complete=self.case == "selected-turn-complete",
                usage=SECOND_RESPONSE_USAGE,
            )
        return USAGE_FINAL_RESPONSE if self.case == "usage" else FINAL_RESPONSE

    def open_stream_prelude(self) -> bytes:
        if self.case == "output-kill":
            return output_response(self.case, complete=False)
        if self.case == "messages-kill":
            return message_response(complete=False)
        return (
            b'data: {"type":"response.created","response":'
            b'{"id":"kora-probe-open-response"}}\n\n'
        )

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


class ToolProbe:
    def __init__(self, case: ProbeCase, thread_id: str) -> None:
        self.case = case
        self.thread_id = thread_id
        self.turn_id: str | None = None
        self.events: list[tuple[str, str, str, str | None, int | None]] = []
        self.confirmed: list[ToolObservation] = []

    def confirm_completion(self, tool: ToolObservation) -> None:
        require(not self.confirmed, "tool_completion_handoff_overflow")
        self.confirmed.append(tool)

    def observe(self, notification: Notification) -> None:
        if notification.method not in ("item/started", "item/completed"):
            return
        params = as_object(notification.params)
        item = as_object(params.get("item"))
        kind = item.get("type")
        if not isinstance(kind, str) or kind not in ("commandExecution", "fileChange"):
            return
        turn_id = params.get("turnId")
        require(
            params.get("threadId") == self.thread_id
            and isinstance(turn_id, str)
            and 0 < len(turn_id) <= 256
            and self.turn_id in (None, turn_id),
            "tool_native_turn_identity",
        )
        self.turn_id = cast("str", turn_id)
        require(item.get("id") == CALL_ID, "tool_item_hook_identity_mismatch")
        status = item.get("status")
        require(
            status in ("inProgress", "completed", "failed", "declined"),
            "tool_native_status_unknown",
        )
        source = item.get("source") if kind == "commandExecution" else None
        require(
            source
            in (
                None,
                "agent",
                "userShell",
                "unifiedExecStartup",
                "unifiedExecInteraction",
            ),
            "tool_native_source_unknown",
        )
        code = item.get("exitCode") if kind == "commandExecution" else None
        require(code is None or type(code) is int, "tool_native_exit_invalid")
        phase = "started" if notification.method == "item/started" else "completed"
        event = (
            kind,
            phase,
            cast("str", status),
            cast("str | None", source),
            cast("int | None", code),
        )
        require(len(self.events) < 2, "tool_native_lifecycle_overflow")
        self.events.append(event)
        diagnostic(
            "INFO",
            "probe.tool_lifecycle_observed",
            ":".join(str(value) if value is not None else "unknown" for value in event),
        )

    def verify(
        self, terminal: TurnCompletion, session: AppServerTurnSession | None
    ) -> None:
        expected: tuple[tuple[str, str, str, str | None, int | None], ...]
        if self.case in (
            "allow",
            "selected-command-metadata",
            "selected-command-exit-seven",
        ):
            exit_code = 7 if self.case == "selected-command-exit-seven" else 0
            status = "failed" if exit_code else "completed"
            expected = (
                (
                    "commandExecution",
                    "started",
                    "inProgress",
                    "unifiedExecStartup",
                    None,
                ),
                (
                    "commandExecution",
                    "completed",
                    status,
                    "unifiedExecStartup",
                    exit_code,
                ),
            )
        elif self.case in PATCH_WRITE_CASES:
            expected = (
                ("fileChange", "started", "inProgress", None, None),
                ("fileChange", "completed", "completed", None, None),
            )
        elif self.case in (
            "deny",
            "patch-deny",
            "selected-patch-deny",
            "selected-shell-patch-protected",
            "decoder-refuse",
        ):
            expected = ()
        else:
            raise ProbeFailure("tool_fixture_case_unknown")
        require(tuple(self.events) == expected, "tool_native_lifecycle_mismatch")
        require(self.turn_id in (None, terminal.turn_id), "tool_native_turn_mismatch")
        if session is None:
            raise ProbeFailure("tool_completion_session_missing")
        self.verify_session(session)
        diagnostic(
            "INFO", "probe.tool_lifecycle_verified", f"{self.case}:{len(self.events)}"
        )

    def verify_session(self, session: AppServerTurnSession) -> None:
        state = session.snapshot()
        starts = sum(event[1] == "started" for event in self.events)
        completions = sum(event[1] == "completed" for event in self.events)
        require(
            state.tool_counts.items == bool(self.events)
            and state.tool_counts.started == starts
            and state.tool_counts.completed == completions
            and state.tool_counts.open == max(starts - completions, 0)
            and state.tool_counts.completion_without_start
            == max(completions - starts, 0),
            "tool_session_counts_mismatch",
        )
        latest = state.latest_tool
        if self.events:
            if latest is None:
                raise ProbeFailure("tool_session_observation_missing")
            require(
                latest.item_id == CALL_ID
                and (
                    latest.kind,
                    latest.phase,
                    latest.status,
                    latest.source,
                    latest.exit_code,
                )
                == self.events[-1],
                "tool_session_observation_mismatch",
            )
        else:
            require(latest is None, "tool_session_observation_invented")
        require(
            [
                (
                    tool.thread_id,
                    tool.turn_id,
                    tool.item_id,
                    tool.kind,
                    tool.phase,
                    tool.status,
                    tool.source,
                    tool.exit_code,
                )
                for tool in self.confirmed
            ]
            == [
                (self.thread_id, self.turn_id, CALL_ID, *event)
                for event in self.events
                if event[1] == "completed"
            ],
            "tool_completion_handoff_mismatch",
        )
        diagnostic(
            "INFO",
            "probe.tool_completion_handoff_verified",
            f"{self.case}:{completions}",
        )


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
        self.confirmed_count = 0
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
        if observation is not None and self.acknowledged_turn is not None:
            self.messages.for_turn(self.acknowledged_turn)

    def confirmed(self, ordinal: int, observation: CompletedMessage) -> None:
        require(
            self.case != "messages-kill"
            and ordinal == self.confirmed_count + 1
            and ordinal <= len(self.expected),
            "message_count_unexpected",
        )
        phase, text = self.expected[ordinal - 1]
        require(
            observation.thread_id == self.thread_id
            and observation.turn_id == self.acknowledged_turn
            and observation.phase == phase
            and observation.text == text,
            "message_content_mismatch",
        )
        self.confirmed_count = ordinal

    def verify(self, terminal: TurnCompletion) -> None:
        require(terminal.status == "completed", "message_turn_not_completed")
        require(self.messages.count == len(self.expected), "message_count_missing")
        require(
            self.messages.for_turn(terminal.turn_id) is not None,
            "message_completion_missing",
        )
        require(self.confirmed_count == len(self.expected), "message_handoff_missing")
        diagnostic(
            "INFO", "probe.confirmed_messages_verified", str(self.confirmed_count)
        )

    def verify_partial(self) -> None:
        identity = self.acknowledged_turn
        require(
            identity is not None and self.delta_turn == identity,
            "message_delta_turn_mismatch",
        )
        require(
            self.messages.count == 0
            and self.confirmed_count == 0
            and self.messages.for_turn(cast("str", identity)) is None,
            "partial_message_adopted",
        )


class HookCompletionProbe:
    def __init__(self, thread_id: str, case: SelectedHookCase) -> None:
        self.thread_id: str = thread_id
        self.case: SelectedHookCase = case
        self.turn_id: str | None = None

    def observe(self, notification: Notification) -> None:
        if notification.method != "hook/completed":
            return
        params = as_object(notification.params)
        turn_id = params.get("turnId")
        require(
            self.turn_id is None
            and params.get("threadId") == self.thread_id
            and isinstance(turn_id, str)
            and 0 < len(turn_id) <= 256,
            "selected_hook_completion_identity",
        )
        run = as_object(params.get("run"))
        status, entry = SELECTED_HOOK_OUTCOMES[self.case]
        require(
            run.get("id") == f"pre-tool-use:0:/opt/kora-probe:{CALL_ID}"
            and run.get("eventName") == "preToolUse"
            and run.get("handlerType") == "command"
            and run.get("executionMode") == "sync"
            and run.get("scope") == "turn"
            and run.get("sourcePath") == "/opt/kora-probe"
            and run.get("source") == "system"
            and run.get("displayOrder") == 0,
            "selected_hook_handler_identity",
        )
        require(run.get("status") == status, "selected_hook_completion_status")
        entries = [] if entry is None else [{"kind": entry[0], "text": entry[1]}]
        require(run.get("entries") == entries, "selected_hook_completion_entry")
        self.turn_id = cast("str", turn_id)

    def verify(self, turn_id: str) -> None:
        require(self.turn_id == turn_id, "selected_hook_completion_turn")
        receipt = read_hook_receipt()
        require(
            receipt.get("session_id") == self.thread_id
            and receipt.get("turn_id") == turn_id,
            "selected_hook_input_identity",
        )
        status, _ = SELECTED_HOOK_OUTCOMES[self.case]
        diagnostic("INFO", "probe.selected_hook_completion", f"{self.case}:{status}")


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
        self.confirmed_counts: list[TokenCounts] = []

    def confirmed(self, observation: ConfirmedUsage) -> None:
        require(
            len(self.confirmed_counts) < len(EXPECTED_USAGE)
            and observation.observed_at == USAGE_SOURCE_TIME
            and not observation.observation_gap,
            "unexpected_confirmed_usage",
        )
        self.confirmed_counts.append(observation.counts)

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
        require(
            tuple(self.confirmed_counts) in (EXPECTED_USAGE, EXPECTED_USAGE[1:]),
            "unexpected_confirmed_usage_counts",
        )
        diagnostic(
            "INFO", "probe.confirmed_usage_verified", str(len(self.confirmed_counts))
        )
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
        self.session: AppServerTurnSession | None = None

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
        self,
        workspace: Path,
        case: ProbeCase,
        executor_url: str | None = None,
        executor_bearer: str = SYNTHETIC_EXECUTOR_BEARER,
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
            await self.register_executor(executor_url, executor_bearer)
        await self.run_turn(workspace, case, selected_executor=executor_url is not None)

    async def observe_noise_registry(self, case: SelectedNoiseRegistryCase) -> None:
        await initialize_app_server(
            self.rpc, self.identity, deadline=self.deadline, experimental_api=True
        )
        local = await self.call("environment/status", {"environmentId": "local"})
        require(local.get("status") == "unknown", "local_executor_available")
        alternate: dict[str, object] = {"environmentId": "remote"}
        try:
            await self.call("environment/info", alternate)
        except RpcError as error:
            require(error.reason is RpcFailure.REMOTE_ERROR, "registry_lookup_failed")
        else:
            raise ProbeFailure("registry_lookup_unexpectedly_accepted")
        status = await self.call("environment/status", alternate)
        expected = (
            "disconnected" if case == "selected-noise-registry-override" else "unknown"
        )
        require(
            status.get("status") == expected, "unexpected_registry_environment_status"
        )
        diagnostic("INFO", "probe.selected_noise_registry_lookup_refused", case)

    async def observe_selected(
        self, workspace: Path, case: ProbeCase, endpoint: str, bearer: str
    ) -> None:
        expected_failure = (
            SelectedStartupFailure.EXECUTOR_INFO_REFUSED
            if case == "selected-startup-bearer"
            else SelectedStartupFailure.EXECUTOR_SHELL
            if case == "selected-startup-bash"
            else None
        )
        if case in SELECTED_HOOK_INVENTORY_REFUSALS:
            expected_failure = SelectedStartupFailure.HOOK_INVENTORY
        if case == "selected-startup-bearer":
            bearer = ("b" if bearer.startswith("a") else "a") + bearer[1:]
        expected = SelectedEnvironment(
            EXECUTOR_ID,
            endpoint,
            Path(
                EXECUTOR_BASH_OVERRIDE
                if case == "selected-startup-bash"
                else "/bin/bash"
            ),
            workspace,
            SELECTED_MODEL,
            selected_managed_hook(case),
        )
        try:
            thread_id = await prepare_selected_thread(
                self.rpc, self.identity, expected, bearer, deadline=self.deadline
            )
        except SelectedStartupError as error:
            if error.reason is not expected_failure:
                raise
            diagnostic("INFO", "probe.selected_startup_refused", error.reason.value)
            return
        require(expected_failure is None, "selected_startup_unexpectedly_accepted")
        diagnostic("INFO", "probe.selected_startup_prepared", case)
        await self.run_turn(
            workspace, case, selected_executor=True, thread_id=thread_id
        )
        if case == "selected-command-metadata":
            verify_command_metadata(workspace, thread_id)

    async def register_executor(self, executor_url: str, bearer: str) -> None:
        await self.call(
            "environment/add",
            {
                "environmentId": EXECUTOR_ID,
                "execServerUrl": executor_url,
                "authBearerToken": bearer,
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

    async def start_legacy_thread(
        self, workspace: Path, case: ProbeCase, *, selected_executor: bool
    ) -> str:
        thread_params: dict[str, object] = {"cwd": str(workspace)}
        if selected_executor:
            thread_params["environments"] = [
                {"environmentId": EXECUTOR_ID, "cwd": str(workspace)}
            ]
        ephemeral = (
            case in PROVIDER_CASES
            or case in SELECTED_LAUNCH_CASES
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
        start = await self.call("thread/start", thread_params)
        thread = as_object(start.get("thread"))
        if ephemeral:
            require(thread.get("ephemeral") is True, "thread_not_ephemeral")
        thread_id = thread.get("id")
        require(isinstance(thread_id, str) and bool(thread_id), "missing_thread_id")
        return cast("str", thread_id)

    async def run_turn(
        self,
        workspace: Path,
        case: ProbeCase,
        *,
        selected_executor: bool,
        thread_id: str | None = None,
    ) -> None:
        if thread_id is None:
            thread_id = await self.start_legacy_thread(
                workspace, case, selected_executor=selected_executor
            )
        prompt = "Run the probe marker command."
        if case in DIRECT_PATCH_CASES:
            prompt = "Apply the probe marker patch."
        elif case == "selected-tool-search":
            prompt = "Search for a native agent tool."
        elif (
            case in PROVIDER_CASES
            or case in CLIENT_CASES
            or case in ERROR_CASES
            or case in MESSAGE_CASES
            or case in OUTPUT_CASES
        ):
            prompt = "Complete the fixture turn."
        usage = UsageProbe(thread_id) if case == "usage" else None
        errors = ErrorProbe(thread_id, case) if case in ERROR_CASES else None
        messages = MessageProbe(thread_id, case) if case in MESSAGE_CASES else None
        self.messages = messages
        outputs = OutputProbe(thread_id, case) if case in OUTPUT_CASES else None
        self.outputs = outputs
        tools = (
            ToolProbe(case, thread_id)
            if case in DIRECT_PATCH_CASES
            or case in SELECTED_SHELL_PATCH_CASES
            or case
            in (
                "allow",
                "deny",
                "decoder-refuse",
                "selected-command-metadata",
                "selected-command-exit-seven",
            )
            else None
        )
        hook = (
            HookCompletionProbe(thread_id, case)
            if case in SELECTED_HOOK_CASES
            else None
        )

        def observe(notification: Notification) -> None:
            if case == "selected-tool-inventory-disabled":
                require(
                    notification.method != "turn/plan/updated", "disabled_plan_executed"
                )
            if case == "selected-tool-search":
                require(
                    notification.method != "hook/completed",
                    "selected_tool_search_reported_hook",
                )
            if usage is not None:
                usage.observe(notification)
            if errors is not None:
                errors.observe(notification)
            if messages is not None:
                messages.observe(notification)
            if outputs is not None:
                outputs.observe(notification)
            if tools is not None:
                tools.observe(notification)
            if hook is not None:
                hook.observe(notification)

        def observe_before_send(notification: Notification) -> None:
            if tools is not None:
                reject_premature_tool(notification)
            if usage is not None:
                reject_premature_usage(notification)
            if errors is not None:
                reject_premature_error(notification)
            if messages is not None:
                reject_premature_message(notification)
            if outputs is not None:
                reject_premature_message(notification)
            if hook is not None:
                require(
                    notification.method != "hook/completed",
                    "selected_hook_completion_before_turn",
                )

        def observe_started(turn_id: str) -> None:
            if messages is not None:
                messages.started(turn_id)
            if outputs is not None:
                outputs.started(turn_id)

        if any(
            value is not None for value in (usage, errors, messages, outputs, tools)
        ):
            self.session = AppServerTurnSession(
                thread_id, max_messages=16, max_tools=16
            )
            self.terminal = await self.session.run(
                self.rpc,
                prompt=prompt,
                deadline=self.deadline,
                observe=observe,
                on_waiting=self.waiting_terminal.set,
                on_started=observe_started,
                on_message=messages.confirmed if messages is not None else None,
                on_tool_completed=tools.confirm_completion
                if tools is not None
                else None,
                output_schema=OUTPUT_SCHEMA if outputs is not None else None,
                usage_observer=(
                    UsageObserver(lambda: USAGE_SOURCE_TIME, usage.confirmed)
                    if usage is not None
                    else None
                ),
            )
            state = self.session.snapshot()
            self.verify_session_terminal(self.terminal)
            if usage is not None:
                require(
                    state.usage == usage.usage.for_turn(self.terminal.turn_id)
                    and state.usage_gap == usage.usage.observation_gap,
                    "session_usage_mismatch",
                )
            if errors is not None:
                require(
                    state.latest_error == errors.errors.for_turn(self.terminal.turn_id),
                    "session_error_mismatch",
                )
            selected_messages = (
                messages.messages
                if messages is not None
                else outputs.messages
                if outputs is not None
                else None
            )
            if selected_messages is not None:
                require(
                    state.latest_message
                    == selected_messages.for_turn(self.terminal.turn_id)
                    and state.message_count == selected_messages.count,
                    "session_messages_mismatch",
                )
            diagnostic("INFO", "probe.session_terminal_verified", case)
        else:
            self.terminal = await observe_app_server_turn(
                self.rpc,
                thread_id=thread_id,
                prompt=prompt,
                deadline=self.deadline,
                observe=observe,
                observe_before_send=observe_before_send,
                on_waiting=self.waiting_terminal.set,
                on_started=observe_started,
            )
        if tools is not None:
            tools.verify(self.terminal, self.session)
        if hook is not None:
            hook.verify(self.terminal.turn_id)
        if outputs is not None:
            outputs.verify(self.terminal)
        if messages is not None:
            messages.verify(self.terminal)
        if errors is not None:
            errors.verify(self.terminal)
        elif case in {"provider_key_absent", "selected-key-absent"}:
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

    def verify_session_terminal(self, terminal: TurnCompletion) -> None:
        if self.session is None:
            raise ProbeFailure("session_missing")
        state = self.session.snapshot()
        require(
            state.turn_id == terminal.turn_id
            and state.terminal_status == terminal.status
            and state.final_message == terminal.final_message,
            "session_terminal_mismatch",
        )
        detail = (
            decode_error_info(terminal.error) if terminal.error is not None else None
        )
        require(state.terminal_error == detail, "session_terminal_error_mismatch")

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
        tools_ready = asyncio.Event()
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
            if self.session is not None:
                latest = self.session.snapshot().latest_tool
                if (
                    latest is not None
                    and latest.item_id == CALL_ID
                    and latest.phase == "started"
                ):
                    tools_ready.set()

        def started(identity: str) -> None:
            nonlocal turn_id
            turn_id = identity

        with ExitStack() as resources:
            listener = resources.enter_context(socket.socket(socket.AF_UNIX))
            listener.setblocking(False)
            listener.bind(str(CONTROL / "interrupt.sock"))
            listener.listen(1)
            self.session = AppServerTurnSession(
                thread_id, max_messages=16, max_tools=16
            )
            observation = asyncio.create_task(
                self.session.run(
                    self.rpc,
                    prompt="Run the fixture action.",
                    deadline=self.deadline,
                    observe=observe,
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
                    await tools_ready.wait()
                    require(
                        self.session.snapshot().tool_counts
                        == ToolCounts(2, 2, 1, 1, 0),
                        "interrupt_open_tool_barrier_missing",
                    )
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
                state = self.session.snapshot()
                counts = state.tool_counts
                if fixture.case == "interrupt-model":
                    require(counts == ToolCounts(), "model_interrupt_tool_invented")
                elif fixture.case == "interrupt-kill":
                    require(
                        counts == ToolCounts(2, 2, 1, 1, 0),
                        "kill_tool_completion_invented",
                    )
                else:
                    require(
                        counts.items == counts.started == 2
                        and counts.completed in (1, 2)
                        and counts.open == 2 - counts.completed
                        and counts.completion_without_start == 0,
                        "interrupt_tool_prefix_invalid",
                    )
                diagnostic(
                    "INFO",
                    "probe.interrupt_tool_prefix_verified",
                    f"{fixture.case}:{counts.started}:{counts.completed}:{counts.open}",
                )
                require(
                    state.turn_id == acknowledged_turn
                    and state.usage == snapshot
                    and not state.usage_gap,
                    "session_interrupt_usage_mismatch",
                )
                if self.terminal is None:
                    require(
                        state.terminal_status is None
                        and state.final_message is None
                        and state.terminal_error is None,
                        "session_kill_terminal_invented",
                    )
                else:
                    self.verify_session_terminal(self.terminal)
                diagnostic("INFO", "probe.session_interrupt_verified", fixture.case)
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
    argv = (
        CODEX,
        "exec-server",
        "--strict-config",
        "--listen",
        "ws://127.0.0.1:0",
        "--ws-auth",
        "capability-token",
        "--ws-token-sha256",
        hashlib.sha256(SYNTHETIC_EXECUTOR_BEARER.encode()).hexdigest(),
    )
    return await launch_executor(argv, workspace, child_env)


async def launch_executor(
    argv: tuple[str, ...], workspace: Path, child_env: dict[str, str]
) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        *argv,
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


def read_hook_receipt() -> dict[str, object]:
    require(RECEIPTS.is_file(), "hook_receipt_missing")
    with RECEIPTS.open("rb") as file:
        raw = file.read(4097)
    require(len(raw) <= 4096, "hook_receipt_size_limit")
    lines = raw.splitlines()
    require(len(lines) == 1, "hook_receipt_count")
    return parse_object(lines[0])


def hook_pid(
    command: str = COMMAND,
    *,
    nested_input: bool = False,
    tool: str = "Bash",
    selected_binding: bool = False,
) -> int:
    receipt = read_hook_receipt()
    if selected_binding:
        require(
            isinstance(receipt.pop("session_id", None), str)
            and isinstance(receipt.pop("turn_id", None), str),
            "selected_hook_input_identity",
        )
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


async def observe_selected_event_turn(
    fixture: ResponsesFixture,
    rpc: AppServerRpc,
    identity: StartupIdentity,
    expected: SelectedEnvironment,
    bearer: str,
    app: asyncio.subprocess.Process,
    deadline: float,
) -> None:
    from itertools import count

    from kora.conformance.harness import ConformanceSink, Trace
    from kora.ir.json import JsonObject
    from kora.observability.redaction import MASK, SecretRegistry
    from kora.plugins.harness.codex.selected_turn import (
        SelectedTurnInputs,
        run_selected_turn,
    )
    from kora.plugins.harness.codex.usage_events import UsageStamp
    from kora.protocols.harness import ResultEvent, TurnEvent, UsageEvent
    from kora.runner.usage import UsageLedger

    message_emitted = asyncio.Event()

    class SelectedSink(ConformanceSink):
        def emit(self, kind: str, data: JsonObject) -> None:
            super().emit(kind, data)
            if kind == "usage":
                fixture.first_usage_emitted.set()
            elif kind == "turn":
                message_emitted.set()

    async def cut_after_message() -> None:
        await fixture.stream_ready.wait()
        await message_emitted.wait()
        require(
            not fixture.stream_closed.is_set(), "selected_event_stream_closed_early"
        )
        signal_native_process(app, signal.SIGKILL)

    cut = fixture.case == "selected-turn-cut"
    sink = SelectedSink(None, Trace())
    registry = SecretRegistry()
    registration = registry.register(EVENT_CANARY)
    sequence = count(1)

    def next_stamp() -> UsageStamp:
        ordinal = next(sequence)
        return UsageStamp(f"selected-usage-{ordinal}", ordinal)

    killer = asyncio.create_task(cut_after_message()) if cut else None
    result = None
    try:
        try:
            result = await run_selected_turn(
                rpc,
                identity,
                expected,
                bearer,
                SelectedTurnInputs(
                    "Run the probe marker command and return JSON.",
                    SELECTED_OUTPUT_SCHEMA,
                    True,
                    16,
                    16,
                ),
                deadline=deadline,
                sink=sink,
                registry=registry,
                usage_message_id="selected-usage",
                source_time=lambda: USAGE_SOURCE_TIME,
                next_usage_stamp=next_stamp,
            )
        except RpcError as error:
            if killer is None or error.reason is not RpcFailure.DISCONNECTED:
                raise
            require(killer.done(), "selected_event_app_not_signalled")
            killer.result()
            require(
                await app.wait() == -signal.SIGKILL, "selected_event_app_not_killed"
            )
            await fixture.stream_closed.wait()
            diagnostic("INFO", "probe.selected_event_peer_eof", fixture.case)
    finally:
        registration.release()
        if killer is not None:
            killer.cancel()
            await asyncio.gather(killer, return_exceptions=True)

    messages = [event.data for event in sink.events if isinstance(event, TurnEvent)]
    usage = [event.data for event in sink.events if isinstance(event, UsageEvent)]
    results = [event.data for event in sink.events if isinstance(event, ResultEvent)]
    require(
        len(sink.events) == (2 if cut else 4)
        and len(messages) == sink.count.events == 1
        and messages[0].text == SELECTED_OUTPUT_TEXT.replace(EVENT_CANARY, MASK)
        and not messages[0].truncated,
        "selected_event_message_mismatch",
    )
    expected_counts = ((50, 20, 30, 10, 4), (95, 25, 40, 30, 10))
    require(
        tuple(
            (
                data.input_tokens,
                data.cached_input_tokens,
                data.cache_creation_tokens,
                data.output_tokens,
                data.reasoning_tokens,
            )
            for data in usage
        )
        == (expected_counts[:1] if cut else expected_counts),
        "selected_event_usage_counts_mismatch",
    )
    ledger = UsageLedger()
    for ordinal, data in enumerate(usage, 1):
        require(
            data.observation_id == f"selected-usage-{ordinal}"
            and data.observation_sequence == ordinal
            and data.observed_at == USAGE_SOURCE_TIME
            and data.coverage.completeness == "partial"
            and data.coverage.attribution_gap
            and not data.coverage.observation_gap,
            "selected_event_usage_identity_mismatch",
        )
        ledger.observe(data)
    coverage = ledger.coverage(result_seen=not cut)
    require(
        ledger.latest == usage[-1]
        and ledger.rows() == ()
        and coverage.completeness == "unavailable"
        and coverage.attribution_gap,
        "selected_event_ledger_mismatch",
    )
    if cut:
        require(result is None and not results, "selected_event_cut_result_emitted")
    else:
        require(
            result is not None
            and results == [result.data]
            and sink.events[-1].kind == "result"
            and result.data.subtype == "codex_completed"
            and not result.data.is_error
            and result.failure_class is None
            and result.output_diagnostic is None
            and result.output is not None
            and result.output.value == SELECTED_OUTPUT
            and EVENT_CANARY not in result.data.model_dump_json()
            and OUTPUT_CANARY not in result.data.model_dump_json(),
            "selected_event_result_mismatch",
        )
    diagnostic(
        "INFO",
        "probe.selected_events_verified",
        f"{fixture.case}:messages={len(messages)},usage={len(usage)},results={len(results)}",
    )


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
            diagnostic("INFO", "probe.confirmed_messages_verified", "0")
        if fixture.case == "output-kill":
            require(stdio.outputs is not None, "output_observer_missing")
            cast("OutputProbe", stdio.outputs).verify_pending()
            await fixture.stream_closed.wait()
        if stdio.session is not None:
            state = stdio.session.snapshot()
            require(
                state.turn_id is not None
                and state.terminal_status is None
                and state.final_message is None
                and state.terminal_error is None,
                "session_exit_terminal_invented",
            )
            if fixture.case == "messages-kill":
                require(
                    state.latest_message is None and state.message_count == 0,
                    "session_delta_promoted",
                )
            elif fixture.case == "output-kill":
                require(
                    state.latest_message is not None
                    and state.latest_message.text == OUTPUT_TEXT
                    and state.message_count == 1,
                    "session_completed_item_lost",
                )
            diagnostic("INFO", "probe.session_partial_verified", fixture.case)
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


async def observe_hook_timeout(
    stdio: Stdio,
    case: SelectedLaunchCase,
    workspace: Path,
    endpoint: str,
    bearer: str,
    native_roles: tuple[asyncio.subprocess.Process, asyncio.subprocess.Process],
) -> None:
    if sys.platform != "linux":
        raise ProbeFailure("selected_hook_timeout_requires_linux")
    loop = asyncio.get_running_loop()
    with ExitStack() as resources:
        native_descriptors: list[int] = []
        for role in native_roles:
            descriptor = open_child_pidfd(role.pid)
            resources.callback(os.close, descriptor)
            native_descriptors.append(descriptor)
        listener = resources.enter_context(socket.socket(socket.AF_UNIX))
        listener.setblocking(False)
        listener.bind(str(CONTROL / HOOK_TIMEOUT_SOCKET))
        listener.listen(1)
        observation = asyncio.create_task(
            stdio.observe_selected(workspace, case, endpoint, bearer)
        )
        try:
            peer, _ = await loop.sock_accept(listener)
            resources.enter_context(peer)
            peer.setblocking(False)
            require(
                await loop.sock_recv(peer, 1) == b"1", "selected_hook_timeout_ready"
            )
            pid = (
                verify_patch_hook(selected_binding=True)
                if case in SELECTED_PATCH_CASES
                else verify_shell_hook(selected_command(case), selected_binding=True)
            )
            credentials = peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            require(
                len(credentials) == 12
                and int.from_bytes(credentials[:4], sys.byteorder, signed=True) == pid,
                "selected_hook_timeout_peer",
            )
            descriptor = open_child_pidfd(pid)
            resources.callback(os.close, descriptor)
            require(
                not select.select([descriptor], [], [], 0)[0],
                "selected_hook_timeout_not_live",
            )
            await observation
            await wait_pidfd_exit(descriptor)
            require(
                not await loop.sock_recv(peer, 1), "selected_hook_timeout_barrier_open"
            )
            require(
                not select.select(native_descriptors, [], [], 0)[0],
                "selected_native_role_exited_before_hook_settlement",
            )
        finally:
            observation.cancel()
            await asyncio.gather(observation, return_exceptions=True)


async def drive(
    fixture: ResponsesFixture,
    root: Path,
    workspace: Path,
    *,
    tls: SelectedTls | None = None,
    unknown_ca: UnknownCaAlert | None = None,
) -> None:
    deadline = asyncio.get_running_loop().time() + TURN_SECONDS

    def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        fixture.accept(reader, writer, deadline)

    async with asyncio.timeout_at(deadline):
        if tls is None:
            server = await asyncio.start_server(accept, "127.0.0.1", 0, limit=16_384)
        else:
            server = await start_selected_tls_server(
                tls.context, accept, handshake_timeout=CLEANUP_SECONDS
            )
    process: asyncio.subprocess.Process | None = None
    executor: asyncio.subprocess.Process | None = None
    executor_url: str | None = None
    bearer = SYNTHETIC_EXECUTOR_BEARER
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
            if fixture.case in SELECTED_LAUNCH_CASES:
                bearer = secrets.token_urlsafe(32)
                inputs = prepare_native_inputs(
                    root,
                    workspace,
                    binary=Path(CODEX),
                    port=port,
                    bearer=bearer,
                    https=tls is not None,
                )
                child_env = materialize_native_inputs(inputs.app_server)
                exec_env = materialize_native_inputs(inputs.exec_server)
                if tls is not None:
                    names = SELECTED_TLS_OVERRIDES.values()
                    require(
                        not any(
                            name in child_env or name in exec_env for name in names
                        ),
                        "selected_tls_inherited_override",
                    )
                    tls_name = SELECTED_TLS_OVERRIDES.get(fixture.case)
                    if tls_name is not None:
                        child_env[tls_name] = str(
                            tls.roots if tls_name == "SSL_CERT_DIR" else tls.bundle
                        )
                if fixture.case == "selected-command-metadata":
                    exec_env.update(METADATA_ENVIRONMENT)
                if fixture.case == "selected-sqlite-home-override":
                    child_env["CODEX_SQLITE_HOME"] = str(root / SQLITE_HOME_CANARY)
                if fixture.case == "selected-bash-startup-override":
                    exec_env["BASH_ENV"] = str(workspace / BASH_STARTUP_SCRIPT)
                if fixture.case == "selected-bash-trace-shellopts":
                    exec_env["SHELLOPTS"] = "xtrace"
                if fixture.case == "selected-bash-trace-ps4":
                    exec_env["PS4"] = (
                        f"$(if [ ! -e {MARKER} ] && [ ! -L {MARKER} ]; then "
                        f"printf '%s\\n' '{BASH_EXTRA_EFFECT_TEXT}'"
                        f" > {BASH_EXTRA_EFFECT_MARKER}; fi){BASH_TRACE_PREFIX} "
                    )
                if fixture.case in SELECTED_NOISE_REGISTRY_CASES:
                    require(
                        child_env.get("CODEX_EXEC_SERVER_URL") == "none",
                        "selected_default_executor_not_disabled",
                    )
                if fixture.case == "selected-noise-registry-override":
                    child_env.update(
                        {
                            "CODEX_EXEC_SERVER_NOISE_REGISTRY_URL": f"http://127.0.0.1:{port}",
                            "CODEX_EXEC_SERVER_NOISE_ENVIRONMENT_ID": REGISTRY_ID,
                            "CODEX_EXEC_SERVER_NOISE_AUTH_TOKEN": REGISTRY_BEARER,
                            "CODEX_EXEC_SERVER_NOISE_CHATGPT_ACCOUNT_ID": NOISE_ACCOUNT,
                        }
                    )
                if fixture.case != "selected-key-absent":
                    child_env["CODEX_API_KEY"] = SYNTHETIC_BEARER
                executor = await launch_executor(
                    inputs.exec_server.argv, inputs.exec_server.cwd, exec_env
                )
                executor_url = await read_executor_url(executor)
                app_argv = inputs.app_server.argv
                app_cwd = inputs.app_server.cwd
            else:
                if fixture.case in EXECUTOR_CASES:
                    executor = await start_executor(root / "executor", workspace)
                    executor_url = await read_executor_url(executor)
                child_env = environment(root, port, fixture.case)
                app_argv = (CODEX, *args)
                app_cwd = workspace
            identity = startup_identity(fixture.case, Path(child_env["CODEX_HOME"]))
            process = await asyncio.create_subprocess_exec(
                *app_argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                limit=MAX_FRAME_BYTES,
                start_new_session=True,
                cwd=app_cwd,
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
            elif fixture.case in SELECTED_NOISE_REGISTRY_CASES:
                await stdio.observe_noise_registry(fixture.case)
            elif fixture.case in SELECTED_TURN_CASES:
                assert executor_url is not None
                await observe_selected_event_turn(
                    fixture,
                    rpc,
                    identity,
                    SelectedEnvironment(
                        EXECUTOR_ID,
                        executor_url,
                        Path("/bin/bash"),
                        workspace,
                        SELECTED_MODEL,
                        selected_managed_hook(fixture.case),
                    ),
                    bearer,
                    process,
                    deadline,
                )
            elif fixture.case in SELECTED_LAUNCH_CASES:
                assert executor_url is not None
                if unknown_ca is not None:
                    await unknown_ca.wait_for_refusal(
                        stdio.observe_selected(
                            workspace, fixture.case, executor_url, bearer
                        )
                    )
                elif fixture.case in SELECTED_HOOK_TIMEOUT_CASES:
                    assert executor is not None
                    await observe_hook_timeout(
                        stdio,
                        fixture.case,
                        workspace,
                        executor_url,
                        bearer,
                        (process, executor),
                    )
                    diagnostic(
                        "INFO",
                        "probe.selected_hook_timeout_exit_verified",
                        fixture.case,
                    )
                else:
                    await stdio.observe_selected(
                        workspace, fixture.case, executor_url, bearer
                    )
            else:
                await stdio.observe(workspace, fixture.case, executor_url, bearer)
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
        if fixture.case in SELECTED_LAUNCH_CASES:
            await close_selected_launch(
                fixture, server, process, executor, cleanup_deadline
            )
        elif process is not None and (
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


async def close_selected_launch(
    fixture: ResponsesFixture,
    server: asyncio.Server,
    app: asyncio.subprocess.Process | None,
    executor: asyncio.subprocess.Process | None,
    deadline: float,
) -> None:
    children = [child for child in (app, executor) if child is not None]
    try:
        async with asyncio.timeout_at(deadline):
            outcomes = await asyncio.gather(
                *(stop_native_process(child) for child in children),
                return_exceptions=True,
            )
            for outcome in outcomes:
                if isinstance(outcome, BaseException):
                    raise outcome
            require(
                all(child.returncode is not None for child in children),
                "selected_native_children_not_reaped",
            )
    finally:
        async with asyncio.timeout_at(deadline):
            await fixture.close(server)
    if len(children) == 2:
        diagnostic("INFO", "probe.selected_children_reaped", fixture.case)


def verify_command_metadata(workspace: Path, thread_id: str) -> None:
    marker = workspace / METADATA_MARKER
    expected = f"{thread_id}\n{CALL_ID}\n{METADATA_ENV_CONTROL}\n".encode()
    require(marker.is_file() and not marker.is_symlink(), "command_metadata_missing")
    with marker.open("rb") as contents:
        require(
            contents.read(len(expected) + 1) == expected,
            "command_metadata_mismatch",
        )
    diagnostic(
        "INFO", "probe.selected_command_metadata_verified", "selected-command-metadata"
    )


def verify_sqlite_home(root: Path, *, override: bool) -> None:
    default_home = role_homes(root / "app").codex_home
    canary_home = root / SQLITE_HOME_CANARY
    selected, competing = (
        (canary_home, default_home) if override else (default_home, canary_home)
    )
    filename = "state_5.sqlite"
    database = selected / filename
    require(
        database.is_file() and not database.is_symlink(),
        "selected_sqlite_state_missing",
    )
    require(not os.path.lexists(competing / filename), "competing_sqlite_state_exists")
    signature = b"SQLite format 3\x00"
    with database.open("rb", buffering=0) as contents:
        require(
            contents.read(len(signature)) == signature, "invalid_sqlite_state_header"
        )


def verify_bash_trace_output(case: ProbeCase, body: str) -> None:
    custom = case == "selected-bash-trace-ps4"
    require(custom == (BASH_TRACE_PREFIX in body), "unexpected_bash_trace_prefix")
    if case == "selected-bash-trace-default":
        require(not body.strip(), "unexpected_bash_trace")
    else:
        prefix = BASH_TRACE_PREFIX if custom else "+"
        require(
            body.splitlines().count(f"{prefix} shopt -q login_shell") == 1,
            "bash_trace_witness_missing",
        )
    diagnostic("INFO", "probe.selected_bash_trace_output_verified", case)


def verify_bash_extra_effect(workspace: Path, *, expected_effect: bool) -> None:
    marker = workspace / BASH_EXTRA_EFFECT_MARKER
    if not expected_effect:
        require(not os.path.lexists(marker), "unexpected_bash_extra_effect")
        return
    require(marker.is_file() and not marker.is_symlink(), "bash_extra_effect_missing")
    expected = f"{BASH_EXTRA_EFFECT_TEXT}\n".encode()
    with marker.open("rb") as contents:
        require(
            contents.read(len(expected) + 1) == expected,
            "bash_extra_effect_mismatch",
        )


def verify_shell_patch_output(case: ProbeCase, output: str) -> None:
    if case == "selected-shell-patch-protected":
        require(
            output
            == (
                f"Command blocked by PreToolUse hook: {SHELL_PATCH_DENIAL}. "
                f"Command: {selected_command(case)}"
            ),
            "shell_patch_policy_denial_missing",
        )
    else:
        lines = output.splitlines()
        require(
            len(lines) == 7
            and lines[:3] == ["Wall time: 0.0000 seconds", "Output:", "Exit code: 0"]
            and re.fullmatch(r"Wall time: [0-9]+(?:\.[0-9])? seconds", lines[3])
            is not None
            and lines[4:]
            == ["Output:", "Success. Updated the following files:", f"A {MARKER}"],
            "shell_patch_function_result_missing",
        )


def verify_shell_patch_policy(case: SelectedLaunchCase) -> None:
    receipt = CONTROL / SHELL_PATCH_POLICY_RECEIPT
    expected = SHELL_PATCH_POLICY_OUTCOMES.get(case)
    if expected is None:
        require(not os.path.lexists(receipt), "unexpected_shell_patch_policy_receipt")
        return
    with receipt.open("rb") as contents:
        require(
            contents.read(len(expected) + 1) == expected, "shell_patch_policy_receipt"
        )
    diagnostic("INFO", "probe.shell_patch_policy_verified", expected.decode().strip())


async def run_selected_launch(case: SelectedLaunchCase) -> None:
    if case in SELECTED_AUTH_CASES or case in SELECTED_HOOK_INVENTORY_REFUSALS:
        decision = (
            "selected-allow"
            if case == "selected-startup-ready"
            or case in SELECTED_TURN_CASES
            or case in SELECTED_SQLITE_HOME_CASES
            or case in SELECTED_BASH_STARTUP_CASES
            or case in SELECTED_TLS_CASES
            or case in SELECTED_TOOL_INVENTORY_CASES
            or case in SELECTED_HOOK_INVENTORY_REFUSALS
            else case
        )
        (CONTROL / "decision").write_text(decision, encoding="utf-8")
    with TemporaryDirectory(prefix="kora-codex-", dir="/tmp") as scratch:
        root = Path(scratch)
        workspace = root / (
            "workspace :@" if case == "selected-startup-ready" else "workspace"
        )
        workspace.mkdir()
        if case in SHELL_PATCH_POLICY_OUTCOMES:
            with (CONTROL / SHELL_PATCH_WORKSPACE).open("xb") as binding:
                binding.write(os.fsencode(workspace))
        if case in SELECTED_SQLITE_HOME_CASES:
            (root / SQLITE_HOME_CANARY).mkdir(mode=0o700)
        if case in SELECTED_BASH_STARTUP_CASES:
            (workspace / BASH_STARTUP_SCRIPT).write_text(
                f"if [ -e {MARKER} ] || [ -L {MARKER} ]; then exit 1; fi\n"
                f"printf '%s\\n' '{BASH_EXTRA_EFFECT_TEXT}'"
                f" > {BASH_EXTRA_EFFECT_MARKER} || exit 1\n",
                encoding="utf-8",
            )
        if case == "selected-startup-ready":
            plant_checkout_canaries(workspace)
        marker = workspace / MARKER
        fixture = ResponsesFixture(case, one_shot=True)
        tls = (
            create_selected_tls(root / "model-tls")
            if case in SELECTED_TLS_CASES
            else None
        )
        if case == "selected-tls-default":
            assert tls is not None
            unknown_ca = UnknownCaAlert(tls.context)
            async with unknown_ca.capture():
                await drive(fixture, root, workspace, tls=tls, unknown_ca=unknown_ca)
            unknown_ca.verify()
            diagnostic("INFO", "probe.selected_tls_unknown_ca_verified", case)
        else:
            await drive(fixture, root, workspace, tls=tls)
        require(fixture.failure is None, fixture.failure or "unexpected_fixture_state")
        require(
            fixture.connections == fixture.requests == fixture.request_limit,
            "unexpected_selected_launch_request_count",
        )
        if case == "selected-tool-search":
            require(
                not os.path.lexists(CONTROL / "invocations")
                and not os.path.lexists(RECEIPTS),
                "selected_tool_search_reached_hook",
            )
            diagnostic("INFO", "probe.selected_tool_search_hook", "absent")
        elif case in SELECTED_REFUSALS or case == "selected-tool-inventory-disabled":
            require(
                not os.path.lexists(RECEIPTS)
                and not os.path.lexists(CONTROL / "invocations"),
                "refused_selected_startup_reached_hook",
            )
        elif case in SELECTED_PATCH_CASES:
            verify_patch_hook(selected_binding=True)
        else:
            verify_shell_hook(
                selected_command(case), selected_binding=case in SELECTED_HOOK_CASES
            )
        executes = case in SELECTED_AUTH_CASES and case not in SELECTED_REFUSALS | {
            "selected-tool-inventory-disabled",
            "selected-tool-search",
            "selected-deny",
            "selected-hook-deny",
            "selected-patch-deny",
            "selected-shell-patch-protected",
        }
        if case in SELECTED_FILE_CHANGE_CASES:
            verify_patch_marker(marker, writes=executes)
        elif executes:
            bash = Path("/bin/bash").resolve(strict=True)
            expected = f"{bash}\n{role_homes(root / 'exec').home}\n".encode()
            require(
                marker.is_file() and not marker.is_symlink(),
                "selected_marker_missing",
            )
            with marker.open("rb") as contents:
                require(
                    contents.read(len(expected) + 1) == expected,
                    "unexpected_selected_marker",
                )
        else:
            require(not os.path.lexists(marker), "refused_selected_marker_exists")
        if case in SELECTED_SHELL_PATCH_CASES:
            verify_shell_patch_policy(case)
        if case in SELECTED_HOOK_CASES:
            effect = "executed" if executes else "blocked"
            diagnostic(
                "INFO", "probe.selected_hook_effect_verified", f"{case}:{effect}"
            )
        if case in SELECTED_SQLITE_HOME_CASES:
            verify_sqlite_home(root, override=case == "selected-sqlite-home-override")
            diagnostic("INFO", "probe.selected_sqlite_home_verified", case)
        if case in SELECTED_BASH_STARTUP_CASES:
            verify_bash_extra_effect(
                workspace, expected_effect=case == "selected-bash-startup-override"
            )
            diagnostic("INFO", "probe.selected_bash_startup_verified", case)
        if case in SELECTED_BASH_TRACE_CASES:
            verify_bash_extra_effect(
                workspace, expected_effect=case == "selected-bash-trace-ps4"
            )
            diagnostic("INFO", "probe.selected_bash_trace_effect_verified", case)
        if case == "selected-startup-ready":
            require(
                workspace_remains_untrusted(
                    role_homes(root / "app").codex_home / "config.toml", workspace
                ),
                "checkout_trust_changed",
            )
        for role in ("app", "exec"):
            codex_home = role_homes(root / role).codex_home
            require(
                not any(
                    os.path.lexists(codex_home / name)
                    for name in ("auth.json", "environments.toml")
                ),
                "selected_native_auth_state_created",
            )
    diagnostic("INFO", "probe.case_verified", case)


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
        if case == "decoder-refuse":
            verify_hook_invocation()
            require(not os.path.lexists(RECEIPTS), "decoder_refusal_receipt_exists")
        else:
            verify_patch_hook()
        verify_patch_marker(marker, writes=case in PATCH_WRITE_CASES)
    diagnostic("INFO", "probe.case_verified", case)


def verify_hook_invocation() -> None:
    with (CONTROL / "invocations").open("rb") as invocations:
        require(invocations.read(2) == b"1", "unexpected_hook_input_invocation_count")


def verify_patch_hook(*, selected_binding: bool = False) -> int:
    verify_hook_invocation()
    return hook_pid(PATCH, tool="apply_patch", selected_binding=selected_binding)


def verify_patch_marker(marker: Path, *, writes: bool) -> None:
    if not writes:
        require(not os.path.lexists(marker), "blocked_hook_input_marker_exists")
        return
    require(marker.is_file() and not marker.is_symlink(), "direct_patch_marker_missing")
    with marker.open("rb") as contents:
        require(
            contents.read(len(PATCH_MARKER) + 1) == PATCH_MARKER,
            "direct_patch_marker_content",
        )


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


def verify_shell_hook(command: str, *, selected_binding: bool = False) -> int:
    with (CONTROL / "invocations").open("rb") as invocations:
        require(invocations.read(2) == b"1", "shell_hook_invocation_count")
    return hook_pid(command, nested_input=True, selected_binding=selected_binding)


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
    elif len(sys.argv) == 2 and sys.argv[1].removeprefix("--") in SELECTED_LAUNCH_CASES:
        await run_selected_launch(
            cast("SelectedLaunchCase", sys.argv[1].removeprefix("--"))
        )
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
    | MessageRefused
    | ToolRefused,
) -> int:
    diagnostic("ERROR", "probe.case_failed", error.reason.value)
    return 1


def main() -> int:
    try:
        asyncio.run(run())
    except (ProbeFailure, SelectedTlsError) as error:
        diagnostic("ERROR", "probe.case_failed", str(error))
        return 1
    except (
        RpcError,
        StartupError,
        TurnError,
        UsageError,
        NativeErrorRefused,
        MessageRefused,
        ToolRefused,
    ) as error:
        return report_connection_failure(error)
    except SelectedStartupError as error:
        diagnostic("ERROR", "probe.case_failed", error.reason.value)
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
