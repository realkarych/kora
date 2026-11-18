import sys
from dataclasses import replace
from pathlib import Path
from typing import cast

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    Message,
    RateLimitEvent,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    __version__,
)
from claude_agent_sdk._cli_version import __cli_version__

from kora.plugins.harness.claude.configuration import CLI_VERSION, SDK_VERSION
from kora.plugins.harness.claude.events import (
    ClaudeEvents,
    TextPiece,
    ToolPiece,
    refused,
)
from kora.protocols.errors import KoraProtocolError
from kora.protocols.harness import LaunchInputs


def require_native_versions(sdk_version: str, cli_version: str) -> None:
    if sdk_version != SDK_VERSION or cli_version != CLI_VERSION:
        raise KoraProtocolError(
            "config_error", "the installed Claude SDK version is unsupported", False, {}
        )


def launch_options(
    base: ClaudeAgentOptions, *, inputs: LaunchInputs, launcher: Path
) -> ClaudeAgentOptions:
    require_native_versions(__version__, __cli_version__)
    return replace(
        base, env=dict(inputs.environment), cwd=inputs.cwd, cli_path=launcher
    )


def bundled_binary() -> Path:
    require_native_versions(__version__, __cli_version__)
    module = sys.modules["claude_agent_sdk"].__file__
    if module is None:
        raise KoraProtocolError(
            "config_error", "the installed Claude SDK version is unsupported", False, {}
        )
    binary = Path(module).resolve().parent / "_bundled" / "claude"
    if not binary.is_file():
        raise KoraProtocolError(
            "config_error", "the installed Claude SDK version is unsupported", False, {}
        )
    return binary


def text_item(item: object) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        block = cast("dict[object, object]", item)
        kind = block.get("type")
        text = block.get("text")
        if kind == "text" and isinstance(text, str):
            return text
    refused()


def result_text(content: object) -> str | None:
    if content is None:
        return None
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        refused()
    items = list[object].copy(cast("list[object]", content))
    return "".join(text_item(item) for item in items)


SYNTHETIC_MODEL = "<synthetic>"


def feed_assistant(events: ClaudeEvents, message: AssistantMessage) -> None:
    if message.model != SYNTHETIC_MODEL:
        events.observe_model(message.message_id, message.model)
    events.observe_assistant_error(message.error)
    if message.model == SYNTHETIC_MODEL:
        events.drop("assistant")
        return
    message_id = message.message_id
    if not isinstance(message_id, str):
        events.drop("assistant")
        return
    pieces: list[TextPiece | ToolPiece] = []
    for block in message.content:
        if isinstance(block, TextBlock):
            pieces.append(TextPiece(block.text))
        elif isinstance(block, ToolUseBlock):
            pieces.append(ToolPiece(block.name, block.id))
    events.assistant(message_id, message.parent_tool_use_id, tuple(pieces))


def feed_user(events: ClaudeEvents, message: UserMessage) -> None:
    content = message.content
    if isinstance(content, str):
        events.drop("user")
        return
    for block in content:
        if isinstance(block, ToolResultBlock):
            events.tool_result(
                block.tool_use_id,
                block.is_error is True,
                result_text(block.content),
            )
            continue
        events.drop("user")


def feed(events: ClaudeEvents, message: Message) -> None:
    if isinstance(message, SystemMessage):
        if message.subtype == "init":
            events.observe_init(message.data)
            return
        if message.subtype == "api_retry":
            events.observe_retry(message.data)
            return
        events.drop("system")
        return
    if isinstance(message, AssistantMessage):
        feed_assistant(events, message)
        return
    if isinstance(message, UserMessage):
        feed_user(events, message)
        return
    if isinstance(message, ResultMessage):
        events.observe_result(
            message.subtype,
            message.is_error,
            message.terminal_reason,
            cast("object", message.model_usage),
            message.api_error_status,
            cast("object", message.permission_denials),
            cast("object", message.usage),
            structured_output=cast("object", message.structured_output),
            result=message.result,
            errors=message.errors,
        )
        return
    if isinstance(message, RateLimitEvent):
        events.observe_rate_limit(
            message.rate_limit_info.status,
            message.rate_limit_info.resets_at,
            message.rate_limit_info.rate_limit_type,
        )
        return
    if isinstance(message, StreamEvent):
        feed_stream(events, message)
        return
    events.drop("message")


def feed_stream(events: ClaudeEvents, message: StreamEvent) -> None:
    event = cast("object", message.event)
    if not isinstance(event, dict):
        refused()
    payload = cast("dict[object, object]", event)
    kind = payload.get("type")
    if kind == "message_start":
        body = payload.get("message")
        if not isinstance(body, dict):
            refused()
        fields = cast("dict[object, object]", body)
        message_id = fields.get("id")
        if not isinstance(message_id, str):
            refused()
        events.usage_start(
            message_id,
            fields.get("model"),
            message.parent_tool_use_id,
            fields.get("usage"),
        )
        return
    if kind == "message_delta":
        events.usage_delta(payload.get("usage"), message.parent_tool_use_id)
        return
    if kind == "message_stop":
        events.usage_stop(message.parent_tool_use_id)
        return
    events.drop("stream_event")
