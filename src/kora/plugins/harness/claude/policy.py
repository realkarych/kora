import asyncio
from collections.abc import Callable
from typing import cast

from claude_agent_sdk.types import HookContext, HookInput, SyncHookJSONOutput

from kora.ir.json import JsonObject
from kora.plugins.harness.claude.denials import bounded_identifier
from kora.plugins.harness.claude.events import ClaudeEvents
from kora.protocols.errors import KoraProtocolError
from kora.protocols.harness import (
    EventSink,
    PermissionDeniedData,
    RunnerCause,
    ToolCallData,
)
from kora.protocols.policy import PolicyClient, PolicyValueRefused, ToolCallRequest
from kora.runner.calls import (
    CallScope,
    closed_exception,
    decide_call,
    denial_fields,
    refused_call,
)

MAX_PENDING_DECISIONS = 64


class PolicyBridge:
    def __init__(
        self,
        scope: CallScope,
        policy: PolicyClient,
        sink: EventSink,
        events: ClaudeEvents,
        stop: Callable[[RunnerCause], None],
        refusing: Callable[[], bool] = lambda: False,
        checked: Callable[[], bool] = lambda: True,
    ) -> None:
        self.scope = scope
        self.policy = policy
        self.sink = sink
        self.events = events
        self.stop = stop
        self.refusing = refusing
        self.checked = checked
        self.active: set[asyncio.Task[object]] = set()

    async def hook(
        self, hook_input: HookInput, tool_use_id: str | None, context: HookContext
    ) -> SyncHookJSONOutput:
        task = asyncio.current_task()
        admitted = len(self.active) < MAX_PENDING_DECISIONS
        if admitted and task is not None:
            self.active.add(task)
        try:
            allow = (
                await self.decide(hook_input, tool_use_id)
                if admitted
                else self.fail(hook_input, tool_use_id)
            )
        except Exception:
            allow = self.fail(hook_input, tool_use_id)
        finally:
            if admitted and task is not None:
                self.active.discard(task)
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow" if allow else "deny",
                "permissionDecisionReason": "kora",
            }
        }

    def fail(self, hook_input: HookInput, tool_use_id: str | None) -> bool:
        try:
            tool = hook_input.get("tool_name")
            if (
                isinstance(tool, str)
                and isinstance(tool_use_id, str)
                and bounded_identifier(tool)
                and bounded_identifier(tool_use_id)
            ):
                self.events.decided(tool_use_id, False)
                self.events.call(
                    ToolCallData(
                        tool=tool, tool_use_id=tool_use_id, output_channel=False
                    )
                )
                self.events.deny(
                    PermissionDeniedData(
                        tool=tool,
                        tool_use_id=tool_use_id,
                        source="harness",
                        code="policy_error",
                    )
                )
        except KoraProtocolError:
            self.stop("policy_error")
            return False
        self.stop("policy_error")
        return False

    async def decide(self, hook_input: HookInput, tool_use_id: str | None) -> bool:
        if hook_input["hook_event_name"] != "PreToolUse" or tool_use_id is None:
            self.stop("policy_error")
            return False
        if self.refusing():
            self.events.decided(tool_use_id, False)
            return False
        if not self.checked():
            return self.fail(hook_input, tool_use_id)
        scope = self.scope
        tool = hook_input["tool_name"]
        agent = cast("object", hook_input.get("agent_id"))
        if agent is not None and not isinstance(agent, str):
            self.stop("policy_error")
            return False
        self.events.flush()
        try:
            request = ToolCallRequest(
                harness=scope.harness,
                tool=tool,
                tool_use_id=tool_use_id,
                agent_id=agent,
                arguments=cast("JsonObject", cast("object", hook_input["tool_input"])),
                workspace=scope.workspace,
                node_path=scope.node_path,
            )
        except PolicyValueRefused:
            request = None
        if request is None:
            verdict = closed_exception(
                self.events.calls,
                self.sink.enforcement(),
                tool,
                tool_use_id,
                agent,
                scope.output_format,
            ) or refused_call(self.events.calls, tool_use_id)
        else:
            verdict = await decide_call(
                self.events.calls,
                self.sink.enforcement(),
                self.policy,
                request,
                scope.layers,
                scope.protected_paths,
                scope.output_format,
            )
        if self.refusing():
            self.events.decided(tool_use_id, False)
            return False
        if self.sink.enforcement().cause == "max_events":
            self.events.decided(tool_use_id, False)
            self.stop("max_events")
            return False
        self.events.decided(tool_use_id, verdict.allow)
        if not verdict.counted:
            return False
        self.events.call(
            ToolCallData(
                tool=tool,
                tool_use_id=tool_use_id,
                agent_id=agent,
                output_channel=verdict.output_channel,
            )
        )
        if verdict.cause is not None:
            self.events.deny(
                PermissionDeniedData(
                    tool=tool,
                    tool_use_id=tool_use_id,
                    source="harness",
                    code=verdict.cause,
                )
            )
            self.stop(verdict.cause)
        elif not verdict.allow:
            pattern, code = (
                (None, "request_invalid")
                if verdict.decision is None
                else denial_fields(verdict.decision)
            )
            self.events.deny(
                PermissionDeniedData.model_validate(
                    {
                        "tool": tool,
                        "tool_use_id": tool_use_id,
                        "source": "policy",
                        "pattern": pattern,
                        "code": code,
                    }
                )
            )
            if request is None:
                self.stop("policy_error")
        cause = self.sink.enforcement().cause
        if cause is not None:
            self.stop(cause)
        return verdict.allow
