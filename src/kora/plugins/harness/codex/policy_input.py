from kora.plugins.harness.codex.pre_tool_use import HookCall
from kora.protocols.policy import ToolCallRequest
from kora.protocols.vcs import WorkspaceRef


def policy_request(
    call: HookCall, *, workspace: WorkspaceRef, node_path: str
) -> ToolCallRequest:
    return ToolCallRequest(
        harness="codex",
        tool=call.tool,
        tool_use_id=call.tool_use_id,
        agent_id=None,
        arguments={"command": call.command},
        workspace=workspace,
        node_path=node_path,
    )
