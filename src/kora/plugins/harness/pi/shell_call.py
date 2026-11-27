from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from kora.protocols.common import ProtocolContext
from kora.protocols.harness import Enforcement
from kora.protocols.lifetime import OperationId
from kora.protocols.policy import Policy, PolicyLayers, ToolCallRequest
from kora.protocols.vcs import WorkspaceRef
from kora.runner.agent import BoundPolicy
from kora.runner.calls import ToolCallLedger, decide_call

ShellDispatch = Callable[[str, OperationId, int, Callable[[], bool]], Awaitable[str]]


class ShellCallRefused(ValueError):
    def __init__(self) -> None:
        super().__init__("pi_shell_refused")


@dataclass(frozen=True, repr=False, slots=True)
class BashRequest:
    id: str
    challenge: OperationId
    command: str


class PiShellCall:
    def __init__(
        self,
        policy: Policy,
        context: ProtocolContext,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
        workspace: WorkspaceRef,
        node_path: str,
        dispatch: ShellDispatch,
    ) -> None:
        self._policy = BoundPolicy(policy, context)
        self._layers = layers
        self._paths = protected_paths
        self._workspace = workspace
        self._node = node_path
        self._dispatch = dispatch
        self._ledger = ToolCallLedger()
        self._claimed = False

    async def run(
        self, call: BashRequest, cutoff_ns: int, current: Callable[[], bool]
    ) -> str:
        if self._claimed or not current():
            raise ShellCallRefused()
        self._claimed = True
        request = ToolCallRequest(
            harness="pi",
            tool="Bash",
            tool_use_id=call.id,
            agent_id=None,
            arguments={"command": call.command},
            workspace=self._workspace,
            node_path=self._node,
        )
        verdict = await decide_call(
            self._ledger,
            Enforcement("continue", None),
            self._policy,
            request,
            self._layers,
            self._paths,
            False,
        )
        if not verdict.allow or not current():
            raise ShellCallRefused()
        return await self._dispatch(call.command, call.challenge, cutoff_ns, current)
