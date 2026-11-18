import json
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Literal

from kora.ir.json import JsonObject
from kora.plugins.harness.claude.trust import REVIEWED_PLUGINS

MODEL = "claude-sonnet-4-5"
SESSION = "sess-replay-1"


def init_frame() -> JsonObject:
    return {
        "type": "system",
        "subtype": "init",
        "session_id": SESSION,
        "apiKeySource": "ANTHROPIC_API_KEY",
        "claude_code_version": "2.1.301",
        "plugins": [
            {"name": name, "path": "builtin"} for name in sorted(REVIEWED_PLUGINS)
        ],
        "mcp_servers": [],
        "tools": ["Write"],
        "model": MODEL,
        "permissionMode": "bypassPermissions",
        "uuid": "init-1",
        "cwd": "/work",
        "slash_commands": [],
        "output_style": "default",
        "skills": [],
    }


def hook(tool: str, identity: str) -> JsonObject:
    return {
        "type": "control_request",
        "request_id": f"hook-{identity}",
        "request": {
            "subtype": "hook_callback",
            "callback_id": "hook_0",
            "tool_use_id": identity,
            "input": {
                "hook_event_name": "PreToolUse",
                "tool_name": tool,
                "tool_use_id": identity,
                "tool_input": {},
                "session_id": SESSION,
                "cwd": "/work",
                "transcript_path": "/unused",
            },
        },
    }


def text_block(text: str) -> JsonObject:
    return {"type": "text", "text": text}


def tool_block(tool: str, identity: str) -> JsonObject:
    return {"type": "tool_use", "id": identity, "name": tool, "input": {}}


def assistant(
    identity: str,
    *blocks: JsonObject,
    model: str = MODEL,
    parent_tool_use_id: str | None = None,
) -> JsonObject:
    return {
        "type": "assistant",
        "session_id": SESSION,
        "parent_tool_use_id": parent_tool_use_id,
        "message": {"id": identity, "model": model, "content": list(blocks)},
    }


def tool_result(*identities: str) -> JsonObject:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": identity,
                    "is_error": False,
                    "content": "done",
                }
                for identity in identities
            ],
        },
    }


def injected_text(*, blocks: bool) -> JsonObject:
    return {
        "type": "user",
        "message": {"content": [text_block("injected")] if blocks else "injected"},
    }


def stream(event: JsonObject, parent_tool_use_id: str | None = None) -> JsonObject:
    return {
        "type": "stream_event",
        "uuid": "stream",
        "session_id": SESSION,
        "parent_tool_use_id": parent_tool_use_id,
        "event": event,
    }


def message_start(identity: str, parent: str | None = None) -> JsonObject:
    return stream(
        {
            "type": "message_start",
            "message": {
                "id": identity,
                "model": MODEL,
                "usage": {
                    "input_tokens": 10,
                    "cache_read_input_tokens": 2,
                    "cache_creation_input_tokens": 1,
                },
            },
        },
        parent,
    )


def content_delta(
    kind: Literal["text", "thinking"], parent: str | None = None
) -> JsonObject:
    return stream(
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": f"{kind}_delta", kind: "discarded"},
        },
        parent,
    )


def message_delta(parent: str | None = None) -> JsonObject:
    return stream(
        {"type": "message_delta", "usage": {"output_tokens": 4, "reasoning_tokens": 1}},
        parent,
    )


def message_stop(parent: str | None = None) -> JsonObject:
    return stream({"type": "message_stop"}, parent)


def task_progress() -> JsonObject:
    return {
        "type": "system",
        "subtype": "task_progress",
        "task_id": "task",
        "description": "working",
        "usage": {},
        "uuid": "progress",
        "session_id": SESSION,
    }


def rate_limit() -> JsonObject:
    return {
        "type": "rate_limit_event",
        "rate_limit_info": {"status": "allowed"},
        "uuid": "rate",
        "session_id": SESSION,
    }


def api_retry(attempt: int = 1, retry_delay_ms: int | None = None) -> JsonObject:
    frame: JsonObject = {
        "type": "system",
        "subtype": "api_retry",
        "attempt": attempt,
        "max_retries": 10,
    }
    if retry_delay_ms is not None:
        frame["retry_delay_ms"] = retry_delay_ms
    return frame


NATIVE_TURN_COUNT = 99


def success(num_turns: int = NATIVE_TURN_COUNT, calls: int = 1) -> JsonObject:
    return {
        "type": "result",
        "subtype": "success",
        "duration_ms": 1,
        "duration_api_ms": 1,
        "is_error": False,
        "num_turns": num_turns,
        "session_id": SESSION,
        "terminal_reason": "completed",
        "permission_denials": [],
        "modelUsage": {
            MODEL: {
                "inputTokens": 10 * calls,
                "cacheReadInputTokens": 2 * calls,
                "cacheCreationInputTokens": calls,
                "outputTokens": 4 * calls,
                "costUSD": 0.01,
            }
        },
        "usage": {
            "input_tokens": 10 * calls,
            "cache_read_input_tokens": 2 * calls,
            "cache_creation_input_tokens": calls,
            "output_tokens": 4 * calls,
        },
    }


def recording(path: Path, frames: Iterable[JsonObject]) -> Path:
    with path.open("w") as handle:
        for frame in frames:
            handle.write(json.dumps(frame, separators=(",", ":")) + "\n")
    return path


def volume_frames(deltas: int, *, pause: bool = False) -> Iterator[JsonObject]:
    yield init_frame()
    yield hook("Write", "write")
    for identity, parent in (("main", None), ("child", "agent")):
        yield message_start(identity, parent)
        for index in range(deltas // 2):
            yield content_delta("text" if index % 2 else "thinking", parent)
        yield assistant(identity, text_block("retained"), parent_tool_use_id=parent)
        yield message_delta(parent)
        yield message_stop(parent)
    yield assistant("tool", tool_block("Write", "write"))
    yield tool_result("write")
    yield task_progress()
    yield rate_limit()
    for _ in range(50):
        yield api_retry()
    yield {"kora_replay": "stderr", "bytes": 1_048_576}
    if pause:
        yield {"kora_replay": "pause"}
    yield success(calls=2)
