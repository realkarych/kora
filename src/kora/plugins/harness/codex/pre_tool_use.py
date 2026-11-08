from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, cast

from kora.plugins.harness.codex.app_server_rpc import decode_object

HookTool = Literal["Bash", "apply_patch"]
TEXT_FIELDS = frozenset(
    {
        "cwd",
        "hook_event_name",
        "model",
        "permission_mode",
        "session_id",
        "tool_name",
        "tool_use_id",
        "turn_id",
    }
)
HOOK_FIELDS = TEXT_FIELDS | {"tool_input", "transcript_path"}
PERMISSION_MODES = frozenset(
    {"default", "acceptEdits", "plan", "dontAsk", "bypassPermissions"}
)


class HookInputFailure(StrEnum):
    INVALID_INPUT = "hook_input_invalid"
    INPUT_TOO_LARGE = "hook_input_too_large"
    UNSUPPORTED_TOOL = "hook_tool_unsupported"


class HookInputRefused(ValueError):
    def __init__(self, reason: HookInputFailure) -> None:
        self.reason = reason
        super().__init__(reason.value)


@dataclass(frozen=True, repr=False)
class HookCall:
    tool: HookTool
    tool_use_id: str
    command: str
    session_id: str
    turn_id: str


def unicode_text(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def native_command(value: object) -> str:
    if not isinstance(value, dict):
        raise HookInputRefused(HookInputFailure.INVALID_INPUT)
    fields = cast("dict[str, object]", value)
    command = fields.get("command")
    if fields.keys() != {"command"} or not unicode_text(command):
        raise HookInputRefused(HookInputFailure.INVALID_INPUT)
    return cast("str", command)


def decode_pre_tool_use(raw: bytes, *, max_bytes: int) -> HookCall:
    if max_bytes < 1:
        raise ValueError("invalid Codex hook input budget")
    if len(raw) > max_bytes:
        raise HookInputRefused(HookInputFailure.INPUT_TOO_LARGE)
    fields = decode_object(raw)
    if fields is None or fields.keys() != HOOK_FIELDS:
        raise HookInputRefused(HookInputFailure.INVALID_INPUT)
    if any(not unicode_text(fields[name]) for name in TEXT_FIELDS):
        raise HookInputRefused(HookInputFailure.INVALID_INPUT)
    transcript = fields["transcript_path"]
    mode = cast("str", fields["permission_mode"])
    if (
        fields["hook_event_name"] != "PreToolUse"
        or mode not in PERMISSION_MODES
        or (transcript is not None and not unicode_text(transcript))
    ):
        raise HookInputRefused(HookInputFailure.INVALID_INPUT)
    tool = fields["tool_name"]
    if tool != "Bash" and tool != "apply_patch":
        raise HookInputRefused(HookInputFailure.UNSUPPORTED_TOOL)
    return HookCall(
        tool=cast("HookTool", tool),
        tool_use_id=cast("str", fields["tool_use_id"]),
        command=native_command(fields["tool_input"]),
        session_id=cast("str", fields["session_id"]),
        turn_id=cast("str", fields["turn_id"]),
    )
