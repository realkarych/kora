import asyncio
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Final, Literal

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ClaudeSDKError,
    HookMatcher,
    ResultMessage,
    __version__,
)
from claude_agent_sdk._cli_version import __cli_version__
from claude_agent_sdk.types import (
    HookContext,
    HookInput,
    SettingSource,
    SyncHookJSONOutput,
    SystemPromptPreset,
)

from kora.plugins.harness.claude.configuration import CLI_VERSION, SDK_VERSION
from kora.protocols.errors import KoraProtocolError
from kora.protocols.harness import LaunchInputs
from kora.protocols.time import Clock

LAUNCH_DIRECTORY: Final[str] = "KORA_CLAUDE_LAUNCH_DIR"
MODEL: Final[str] = "claude-sonnet-4-5"
TURN_LIMIT: Final[int] = 4
Decision = Literal["allow", "deny"]
Outcome = Literal["completed", "cancelled"]


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


def discard_stderr(_line: str) -> None:
    return None


async def drop(task: asyncio.Task[object]) -> None:
    if not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return
    if not task.cancelled():
        task.exception()


async def wait_task(task: asyncio.Task[object], clock: Clock, deadline_ns: int) -> None:
    if task.done():
        task.result()
        return
    timeout = asyncio.create_task(clock.sleep_until(deadline_ns))
    try:
        done, _pending = await asyncio.wait(
            {task, timeout}, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        await drop(timeout)
    if task not in done:
        raise KoraProtocolError("harness_error", "the Claude session failed", False, {})
    task.result()


async def read_result(client: ClaudeSDKClient) -> None:
    async for message in client.receive_response():
        if isinstance(message, ResultMessage):
            return
    raise KoraProtocolError("harness_error", "the Claude session failed", False, {})


async def finish_turn(
    client: ClaudeSDKClient,
    stop: asyncio.Event | None,
    clock: Clock,
    deadline_ns: int,
) -> Outcome:
    read = asyncio.create_task(read_result(client))
    timeout = asyncio.create_task(clock.sleep_until(deadline_ns))
    stopped = asyncio.create_task(stop.wait()) if stop is not None else None
    waiting: set[asyncio.Task[object]] = {read, timeout}
    if stopped is not None:
        waiting.add(stopped)
    try:
        await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
        if stop is not None and stop.is_set():
            if not read.done():
                grace = min(deadline_ns, clock.monotonic_ns() + 5_000_000_000)
                interrupt = asyncio.create_task(client.interrupt())
                try:
                    await wait_task(interrupt, clock, grace)
                finally:
                    await drop(interrupt)
                await wait_task(read, clock, grace)
            return "cancelled"
        if read.done() and not read.cancelled():
            read.result()
            return "completed"
        raise KoraProtocolError("harness_error", "the Claude session failed", False, {})
    finally:
        await drop(timeout)
        if stopped is not None:
            await drop(stopped)
        await drop(read)


async def run_scripted_client(
    *,
    cwd: Path,
    launcher: Path,
    launch_dir: Path,
    prompt: str,
    tools: tuple[str, ...],
    decide: Callable[[str], Decision],
    stop: asyncio.Event | None,
    clock: Clock,
    deadline_ns: int,
) -> Outcome:
    failures: list[str] = []

    async def hook(
        hook_input: HookInput, _tool_use_id: str | None, _context: HookContext
    ) -> SyncHookJSONOutput:
        decision: Decision = "deny"
        if hook_input["hook_event_name"] != "PreToolUse":
            failures.append("unnamed")
        else:
            name = hook_input["tool_name"]
            if name == "":
                failures.append("unnamed")
            else:
                try:
                    selected: object = decide(name)
                except Exception:
                    failures.append("decision")
                    selected = "deny"
                if selected == "allow":
                    decision = "allow"
                elif selected == "deny":
                    decision = "deny"
                else:
                    failures.append("decision")
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": decision,
                "permissionDecisionReason": "kora",
            }
        }

    sources: list[SettingSource] = []
    prompt_preset: SystemPromptPreset = {"type": "preset", "preset": "claude_code"}
    options = ClaudeAgentOptions(
        cwd=cwd,
        cli_path=launcher,
        env={LAUNCH_DIRECTORY: str(launch_dir)},
        setting_sources=sources,
        strict_mcp_config=True,
        permission_mode="bypassPermissions",
        tools=list(tools),
        max_turns=TURN_LIMIT,
        model=MODEL,
        extra_args={"no-session-persistence": None},
        system_prompt=prompt_preset,
        stderr=discard_stderr,
        hooks={"PreToolUse": [HookMatcher(hooks=[hook])]},
    )
    client = ClaudeSDKClient(options=options)
    try:
        connect = asyncio.create_task(client.connect(prompt))
        try:
            await wait_task(connect, clock, deadline_ns)
        finally:
            await drop(connect)
        outcome = await finish_turn(client, stop, clock, deadline_ns)
    except KoraProtocolError:
        raise
    except (ClaudeSDKError, OSError):
        raise KoraProtocolError(
            "harness_error", "the Claude session failed", False, {}
        ) from None
    finally:
        try:
            await client.disconnect()
        except (ClaudeSDKError, OSError):
            raise KoraProtocolError(
                "harness_error", "the Claude session failed", False, {}
            ) from None
    if failures:
        raise KoraProtocolError("harness_error", "the Claude session failed", False, {})
    return outcome
