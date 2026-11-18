from pathlib import Path
from typing import Annotated, Literal

from claude_agent_sdk import ClaudeAgentOptions, HookMatcher
from claude_agent_sdk.types import HookCallback, PermissionMode, SystemPromptPreset
from pydantic import MISSING, Field, StrictInt, ValidationError

from kora.ir.config import NodeLimits
from kora.ir.json import JsonObject, JsonObjectField
from kora.protocols.common import BoundaryModel
from kora.protocols.errors import KoraProtocolError
from kora.protocols.harness import LaunchInputs
from kora.runner.calls import TRACKING_CEILING


class ClaudeSessionOptions(BoundaryModel):
    model: str | None = None
    system_prompt_append: str | None = None
    permission_mode: Literal["plan", "accept_edits", "dont_ask", "bypass"] = "dont_ask"
    max_turns: Annotated[StrictInt, Field(ge=1)]
    output_schema: JsonObjectField | None = None


def session_options(config: JsonObject, limits: NodeLimits) -> ClaudeSessionOptions:
    ignored = {
        "harness",
        "prompt",
        "workspace",
        "tool_policy",
        "env",
        "secret_env",
        "runtime",
        "vcs",
    }
    supported = {"model", "system_prompt_append", "permission_mode", "output_schema"}
    if (
        limits.budget_usd is not MISSING
        or config.get("trust_repo_config", False) is not False
        or config.keys() - ignored - supported - {"trust_repo_config"}
    ):
        raise configuration_refused()
    try:
        return ClaudeSessionOptions.model_validate(
            {key: value for key, value in config.items() if key in supported}
            | {
                "max_turns": TRACKING_CEILING
                if limits.max_events is MISSING
                else limits.max_events
            }
        )
    except ValidationError:
        raise configuration_refused() from None


def configuration_refused() -> KoraProtocolError:
    return KoraProtocolError(
        "config_error", "the Claude session configuration is unsupported", False, {}
    )


def discard_stderr(_line: str) -> None:
    return None


def agent_options(
    options: ClaudeSessionOptions,
    *,
    cli_path: Path,
    inputs: LaunchInputs,
    hook: HookCallback,
) -> ClaudeAgentOptions:
    modes: dict[str, PermissionMode] = {
        "plan": "plan",
        "accept_edits": "acceptEdits",
        "dont_ask": "dontAsk",
        "bypass": "bypassPermissions",
    }
    preset: SystemPromptPreset = {"type": "preset", "preset": "claude_code"}
    if options.system_prompt_append is not None:
        preset["append"] = options.system_prompt_append
    return ClaudeAgentOptions(
        setting_sources=[],
        strict_mcp_config=True,
        system_prompt=preset,
        permission_mode=modes[options.permission_mode],
        include_partial_messages=True,
        forward_subagent_text=True,
        stderr=discard_stderr,
        env=dict(inputs.environment),
        cwd=inputs.cwd,
        cli_path=cli_path,
        max_turns=options.max_turns,
        model=options.model,
        output_format=None
        if options.output_schema is None
        else {"type": "json_schema", "schema": options.output_schema},
        hooks={"PreToolUse": [HookMatcher(hooks=[hook])]},
    )
