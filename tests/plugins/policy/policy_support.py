import asyncio
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from kora.conformance.capture import Capture, captured
from kora.conformance.common import (
    CONFORMANCE_COMPONENT,
    context_for,
    raised_protocol_error,
    utc_now,
)
from kora.conformance.policy import EditCall, PolicyCalls
from kora.ir import ToolPolicy
from kora.ir.globs import Fuel
from kora.ir.json import JsonObject
from kora.observability import Level, LoggingSettings
from kora.plugins.policy.allow_deny import (
    AllowDenyPolicy,
    Prepared,
    evaluated,
    prepared_configuration,
)
from kora.protocols.policy import PolicyDecision, PolicyLayers, ToolCallRequest
from kora.protocols.vcs import WorkspaceRef

CLAUDE_EDIT_KEYS: Final[dict[str, str]] = {
    "Edit": "file_path",
    "Write": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}


def workspace(root: Path) -> WorkspaceRef:
    return WorkspaceRef(
        attempt_id="attempt-1",
        root=str(root),
        repo="example/repo",
        commit="c0ffee",
        base_commit="c0ffee",
        branch=None,
    )


def request(
    root: Path, harness: str, tool: str, arguments: JsonObject
) -> ToolCallRequest:
    return ToolCallRequest(
        harness=harness,
        tool=tool,
        tool_use_id="call-1",
        agent_id=None,
        arguments=arguments,
        workspace=workspace(root),
        node_path="graph.node",
    )


def shell(root: Path, command: str, harness: str = "claude") -> ToolCallRequest:
    return request(root, harness, "Bash", {"command": command})


def patch_text(*headers: str) -> str:
    return "\n".join(["*** Begin Patch", *headers, "*** End Patch"])


def update_patch(path: str) -> str:
    return patch_text(f"*** Update File: {path}", "@@", "-old", "+new")


def edit(root: Path, tool: str, path: str) -> ToolCallRequest:
    if tool == "apply_patch":
        return request(root, "codex", tool, {"command": update_patch(path)})
    return request(root, "claude", tool, {CLAUDE_EDIT_KEYS[tool]: path})


def edit_builder(root: Path, tool: str) -> Callable[[str], ToolCallRequest]:
    def build(path: str) -> ToolCallRequest:
        return edit(root, tool, path)

    return build


def policy(allow: tuple[str, ...] = (), deny: tuple[str, ...] = ()) -> ToolPolicy:
    return ToolPolicy.model_validate({"allow": list(allow), "deny": list(deny)})


def layers(
    namespace: ToolPolicy | None = None, node: ToolPolicy | None = None
) -> PolicyLayers:
    return PolicyLayers(namespace=namespace or policy(), node=node or policy())


def prepared_root(root: Path) -> Path:
    (root / "src").mkdir(parents=True)
    (root / "tests" / "unit").mkdir(parents=True)
    return root


def path_naming(tool: str) -> Callable[[str], str]:
    def naming(path: str) -> str:
        return f"{tool}({path})"

    return naming


def conformance_calls(root: Path) -> PolicyCalls:
    tools = (*CLAUDE_EDIT_KEYS, "apply_patch")
    return PolicyCalls(
        shell=shell(root, "git status"),
        shell_other=shell(root, "ls"),
        matching="Bash(git *)",
        narrow="Bash(git status)",
        other="Bash(rm *)",
        edits=tuple(
            EditCall(
                tool=tool,
                pattern=tool,
                build=edit_builder(root, tool),
                naming=path_naming(tool),
            )
            for tool in tools
        ),
        malformed={"allow": ["Bash(git *"]},
    )


def decision(
    capture: Capture,
    call: ToolCallRequest,
    chosen: PolicyLayers | None = None,
    protected: tuple[str, ...] = (),
    subject: AllowDenyPolicy | None = None,
) -> PolicyDecision:
    provider = subject or AllowDenyPolicy()
    return asyncio.run(
        provider.decide(call, chosen or layers(), protected, context_for(capture))
    )


def refusal(
    capture: Capture,
    call: ToolCallRequest,
    chosen: PolicyLayers | None = None,
    protected: tuple[str, ...] = (),
) -> str | None:
    error = raised_protocol_error(
        AllowDenyPolicy().decide(
            call, chosen or layers(), protected, context_for(capture)
        )
    )
    return None if error is None else error.code


def reason(
    capture: Capture,
    call: ToolCallRequest,
    chosen: PolicyLayers | None = None,
    protected: tuple[str, ...] = (),
) -> str:
    return decision(capture, call, chosen, protected).reason


@contextmanager
def debug_capture() -> Generator[Capture]:
    with captured(
        CONFORMANCE_COMPONENT, LoggingSettings(level=Level.DEBUG), "policy", utc_now
    ) as capture:
        yield capture


def prepared_or_raise(chosen: PolicyLayers, protected: tuple[str, ...]) -> Prepared:
    ready = prepared_configuration(chosen, protected)
    if isinstance(ready, str):
        raise LookupError(ready)
    return ready


def fuel_for(call: ToolCallRequest, chosen: PolicyLayers) -> int:
    fuel = Fuel(10**12)
    evaluated(call, prepared_or_raise(chosen, ()), fuel)
    return fuel.spent
