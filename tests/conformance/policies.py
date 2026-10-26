import dataclasses
import fnmatch
import os
from collections.abc import Callable
from typing import Final, cast

from kora.conformance.common import CONFORMANCE_COMPONENT
from kora.conformance.policy import EditCall, PolicyCalls
from kora.ir import ContractViolation, ToolPolicy
from kora.ir.json import JsonObject
from kora.observability import CATALOG, EventSpec, FieldKind, Level
from kora.protocols.common import ProtocolContext
from kora.protocols.errors import KoraProtocolError
from kora.protocols.policy import (
    PolicyCapabilities,
    PolicyDecision,
    PolicyLayers,
    ToolCallRequest,
)
from kora.protocols.vcs import WorkspaceRef

ROOT: Final[str] = "/workspace/attempt"
WORKSPACE: Final[WorkspaceRef] = WorkspaceRef(
    attempt_id="attempt-1",
    root=ROOT,
    repo="example/repo",
    commit="c0ffee",
    base_commit="c0ffee",
    branch=None,
)
DECIDE_FAILED: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "conformance.test_policy.decide_failed",
        CONFORMANCE_COMPONENT,
        Level.WARNING,
        "a test policy could not decide a call",
        {"reason": FieldKind.STRING},
    )
)
EDIT_TOOLS: Final[dict[str, str]] = {
    "Edit": "file_path",
    "Write": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}


def shell(command: str) -> ToolCallRequest:
    return ToolCallRequest(
        harness="claude",
        tool="Bash",
        tool_use_id="call-1",
        agent_id=None,
        arguments={"command": command},
        workspace=WORKSPACE,
        node_path="node",
    )


def edit_builder(
    tool: str, harness: str = "claude"
) -> Callable[[str], ToolCallRequest]:
    def build(path: str) -> ToolCallRequest:
        return ToolCallRequest(
            harness=harness,
            tool=tool,
            tool_use_id="call-1",
            agent_id=None,
            arguments={EDIT_TOOLS[tool]: path},
            workspace=WORKSPACE,
            node_path="node",
        )

    return build


def path_naming(tool: str) -> Callable[[str], str]:
    def naming(path: str) -> str:
        return f"{tool}({path})"

    return naming


def reference_calls() -> PolicyCalls:
    return PolicyCalls(
        shell=shell("git status"),
        shell_other=shell("ls"),
        matching="Bash(git)",
        narrow="Bash(git status)",
        other="Bash(rm)",
        edits=tuple(
            EditCall(
                tool=tool,
                pattern=tool,
                build=edit_builder(tool),
                naming=path_naming(tool),
            )
            for tool in EDIT_TOOLS
        ),
        malformed={"allow": "Bash(git push)"},
    )


def argument(request: ToolCallRequest) -> str:
    key = EDIT_TOOLS.get(request.tool, "command")
    value = request.arguments.get(key)
    return value if isinstance(value, str) else ""


def matches(pattern: str, request: ToolCallRequest) -> bool:
    tool, _, rest = pattern.partition("(")
    if tool != request.tool:
        return False
    return not rest or argument(request).startswith(rest.removesuffix(")"))


def relative_path(request: ToolCallRequest) -> str:
    root = os.path.normpath(request.workspace.root)
    full = os.path.normpath(os.path.join(root, argument(request)))
    return full.removeprefix(root + "/")


def glob_matches(entry: str, path: str) -> bool:
    if entry.endswith("/**"):
        prefix = entry.removesuffix("/**")
        return path == prefix or path.startswith(prefix + "/")
    return fnmatch.fnmatchcase(path, entry)


def protected(request: ToolCallRequest, paths: tuple[str, ...]) -> bool:
    if request.tool not in EDIT_TOOLS:
        return False
    path = relative_path(request).casefold()
    return any(glob_matches(entry.casefold(), path) for entry in paths)


def layer_items(layers: PolicyLayers) -> tuple[tuple[str, ToolPolicy], ...]:
    return (("namespace", layers.namespace), ("node", layers.node))


def first_deny(layers: PolicyLayers, request: ToolCallRequest) -> str | None:
    for name, layer in layer_items(layers):
        for index, pattern in enumerate(layer.deny):
            if matches(pattern, request):
                return f"{name}.deny.{index}"
    return None


def allow_refusal(layers: PolicyLayers, request: ToolCallRequest) -> bool:
    return any(
        layer.allow and not any(matches(pattern, request) for pattern in layer.allow)
        for _, layer in layer_items(layers)
    )


class ReferencePolicy:
    name: str = "reference"
    config_schema: JsonObject = {"type": "object"}

    def capabilities(self) -> PolicyCapabilities:
        return PolicyCapabilities()

    def validate(self, policy: JsonObject) -> list[ContractViolation]:
        return [
            ContractViolation(
                code="tool_policy_invalid",
                message="the tool policy is invalid",
                path=(key,),
            )
            for key, value in policy.items()
            if not isinstance(value, list)
        ]

    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
        ctx: ProtocolContext,
    ) -> PolicyDecision:
        if protected(request, protected_paths):
            return PolicyDecision(allow=False, reason="protected_path")
        denied = first_deny(layers, request)
        if denied is not None:
            return PolicyDecision(allow=False, reason=denied)
        if allow_refusal(layers, request):
            return PolicyDecision(allow=False, reason="not_allowed")
        return PolicyDecision(allow=True, reason="allowed")


class NodeAllowIgnoringPolicy(ReferencePolicy):
    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
        ctx: ProtocolContext,
    ) -> PolicyDecision:
        widened = PolicyLayers(
            namespace=layers.namespace,
            node=ToolPolicy.model_validate({"deny": list(layers.node.deny)}),
        )
        return await super().decide(request, widened, protected_paths, ctx)


class AnyDenyPolicy(ReferencePolicy):
    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
        ctx: ProtocolContext,
    ) -> PolicyDecision:
        if layers.namespace.deny or layers.node.deny:
            return PolicyDecision(allow=False, reason="not_allowed")
        return await super().decide(request, layers, protected_paths, ctx)


class FirstDenyOnlyPolicy(ReferencePolicy):
    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
        ctx: ProtocolContext,
    ) -> PolicyDecision:
        return await super().decide(
            request, kept_denies(layers, 1), protected_paths, ctx
        )


class SubagentSkippingPolicy(ReferencePolicy):
    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
        ctx: ProtocolContext,
    ) -> PolicyDecision:
        if request.agent_id is not None:
            return PolicyDecision(allow=True, reason="allowed")
        return await super().decide(request, layers, protected_paths, ctx)


class AllowOverridesDenyPolicy(ReferencePolicy):
    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
        ctx: ProtocolContext,
    ) -> PolicyDecision:
        for _, layer in layer_items(layers):
            if any(matches(pattern, request) for pattern in layer.allow):
                return PolicyDecision(allow=True, reason="allowed")
        return await super().decide(request, layers, protected_paths, ctx)


class ToolCachingPolicy(ReferencePolicy):
    def __init__(self) -> None:
        self.cache: dict[str, PolicyDecision] = {}

    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
        ctx: ProtocolContext,
    ) -> PolicyDecision:
        if request.tool != "Bash":
            return await super().decide(request, layers, protected_paths, ctx)
        cached = self.cache.get(request.tool)
        if cached is None:
            cached = await super().decide(request, layers, protected_paths, ctx)
            self.cache[request.tool] = cached
        return cached


class ExactProtectedPathPolicy(ReferencePolicy):
    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
        ctx: ProtocolContext,
    ) -> PolicyDecision:
        literal = argument(request) in protected_paths
        if request.tool in EDIT_TOOLS and literal:
            return PolicyDecision(allow=False, reason="protected_path")
        return await super().decide(request, layers, (), ctx)


class SubagentProtectionSkippingPolicy(ReferencePolicy):
    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
        ctx: ProtocolContext,
    ) -> PolicyDecision:
        chosen = () if request.agent_id is not None else protected_paths
        return await super().decide(request, layers, chosen, ctx)


class ShellOnlyRulesPolicy(ReferencePolicy):
    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
        ctx: ProtocolContext,
    ) -> PolicyDecision:
        if request.tool != "Bash":
            empty = ToolPolicy.model_validate({})
            layers = PolicyLayers(namespace=empty, node=empty)
        return await super().decide(request, layers, protected_paths, ctx)


class HardcodedTestsPolicy(ReferencePolicy):
    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
        ctx: ProtocolContext,
    ) -> PolicyDecision:
        return await super().decide(request, layers, ("tests/**",), ctx)


class NonBoolAllowPolicy(ReferencePolicy):
    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
        ctx: ProtocolContext,
    ) -> PolicyDecision:
        decision = await super().decide(request, layers, protected_paths, ctx)
        object.__setattr__(decision, "allow", int(decision.allow))
        return decision


class ContradictingReasonPolicy(ReferencePolicy):
    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
        ctx: ProtocolContext,
    ) -> PolicyDecision:
        decision = await super().decide(request, layers, protected_paths, ctx)
        if not decision.allow:
            object.__setattr__(decision, "reason", "allowed")
        return decision


class LayersSkippedUnderProtectionPolicy(ReferencePolicy):
    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
        ctx: ProtocolContext,
    ) -> PolicyDecision:
        if protected_paths:
            empty = ToolPolicy.model_validate({})
            layers = PolicyLayers(namespace=empty, node=empty)
        return await super().decide(request, layers, protected_paths, ctx)


class EditCachingPolicy(ReferencePolicy):
    def __init__(self) -> None:
        self.cache: dict[str, PolicyDecision] = {}

    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
        ctx: ProtocolContext,
    ) -> PolicyDecision:
        if request.tool == "Bash":
            return await super().decide(request, layers, protected_paths, ctx)
        cached = self.cache.get(request.tool)
        if cached is None:
            cached = await super().decide(request, layers, protected_paths, ctx)
            self.cache[request.tool] = cached
        return cached


class FirstProtectedOnlyPolicy(ReferencePolicy):
    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
        ctx: ProtocolContext,
    ) -> PolicyDecision:
        return await super().decide(request, layers, protected_paths[:1], ctx)


class WrongRuleIdPolicy(ReferencePolicy):
    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
        ctx: ProtocolContext,
    ) -> PolicyDecision:
        decision = await super().decide(request, layers, protected_paths, ctx)
        if ".deny." in decision.reason:
            return PolicyDecision(allow=False, reason="namespace.deny.0")
        return decision


class FormlessReasonPolicy(ReferencePolicy):
    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
        ctx: ProtocolContext,
    ) -> PolicyDecision:
        decision = await super().decide(request, layers, protected_paths, ctx)
        object.__setattr__(decision, "reason", "because")
        return decision


class DriftingReasonPolicy(ReferencePolicy):
    def __init__(self) -> None:
        self.calls = 0

    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
        ctx: ProtocolContext,
    ) -> PolicyDecision:
        decision = await super().decide(request, layers, protected_paths, ctx)
        drifted = self.calls > 0
        self.calls += 1
        if drifted and not decision.allow:
            return PolicyDecision(allow=False, reason="denied")
        return decision


class IdentityCachedDriftPolicy(DriftingReasonPolicy):
    def __init__(self) -> None:
        super().__init__()
        self.cache: dict[tuple[int, str], PolicyDecision] = {}

    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
        ctx: ProtocolContext,
    ) -> PolicyDecision:
        key = (id(request), repr((layers, protected_paths)))
        cached = self.cache.get(key)
        if cached is None:
            cached = await super().decide(request, layers, protected_paths, ctx)
            self.cache[key] = cached
        return cached


class TupleValidatePolicy(ReferencePolicy):
    def validate(self, policy: JsonObject) -> list[ContractViolation]:
        return cast("list[ContractViolation]", tuple(super().validate(policy)))


class MappingValidatePolicy(ReferencePolicy):
    def validate(self, policy: JsonObject) -> list[ContractViolation]:
        return cast(
            "list[ContractViolation]",
            [item.model_dump(mode="json") for item in super().validate(policy)],
        )


class PathlessValidatePolicy(ReferencePolicy):
    def validate(self, policy: JsonObject) -> list[ContractViolation]:
        return [
            ContractViolation(
                code="tool_policy_invalid", message="the tool policy is invalid"
            )
            for _ in super().validate(policy)
        ]


class EchoingValidatePolicy(ReferencePolicy):
    def validate(self, policy: JsonObject) -> list[ContractViolation]:
        return [
            ContractViolation(
                code="tool_policy_invalid", message=str(value), path=(key,)
            )
            for key, value in policy.items()
        ]


def calls_with_edits(edits: tuple[EditCall, ...]) -> PolicyCalls:
    return dataclasses.replace(reference_calls(), edits=edits)


def edit_calls(*tools: tuple[str, str, str]) -> tuple[EditCall, ...]:
    return tuple(
        EditCall(
            tool=label,
            pattern=tool,
            build=edit_builder(tool, harness),
            naming=path_naming(tool),
        )
        for label, tool, harness in tools
    )


def kept_denies(layers: PolicyLayers, count: int) -> PolicyLayers:
    return PolicyLayers(
        namespace=layers.namespace.model_copy(
            update={"deny": layers.namespace.deny[:count]}
        ),
        node=layers.node.model_copy(update={"deny": layers.node.deny[:count]}),
    )


class FirstTwoDeniesPolicy(ReferencePolicy):
    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
        ctx: ProtocolContext,
    ) -> PolicyDecision:
        return await super().decide(
            request, kept_denies(layers, 2), protected_paths, ctx
        )


class FirstTwoProtectedPolicy(ReferencePolicy):
    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
        ctx: ProtocolContext,
    ) -> PolicyDecision:
        return await super().decide(request, layers, protected_paths[:2], ctx)


class SingleEntryAllowOnlyPolicy(ReferencePolicy):
    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
        ctx: ProtocolContext,
    ) -> PolicyDecision:
        def kept(layer: ToolPolicy) -> ToolPolicy:
            allow = layer.allow if len(layer.allow) == 1 else ()
            return layer.model_copy(update={"allow": allow})

        trimmed = PolicyLayers(namespace=kept(layers.namespace), node=kept(layers.node))
        return await super().decide(request, trimmed, protected_paths, ctx)


def longest_match(patterns: tuple[str, ...], request: ToolCallRequest) -> int:
    return max((len(item) for item in patterns if matches(item, request)), default=-1)


class SpecificAllowWinsPolicy(ReferencePolicy):
    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
        ctx: ProtocolContext,
    ) -> PolicyDecision:
        allows = layers.namespace.allow + layers.node.allow
        denies = layers.namespace.deny + layers.node.deny
        if longest_match(allows, request) > longest_match(denies, request):
            return PolicyDecision(allow=True, reason="allowed")
        return await super().decide(request, layers, protected_paths, ctx)


class ArgumentAllowSkipsProtectionPolicy(ReferencePolicy):
    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
        ctx: ProtocolContext,
    ) -> PolicyDecision:
        allows = layers.namespace.allow + layers.node.allow
        named = any("(" in item and matches(item, request) for item in allows)
        chosen = () if named else protected_paths
        return await super().decide(request, layers, chosen, ctx)


class NodeDenyReplacesNamespacePolicy(ReferencePolicy):
    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
        ctx: ProtocolContext,
    ) -> PolicyDecision:
        if layers.node.deny:
            namespace = layers.namespace.model_copy(update={"deny": ()})
            layers = PolicyLayers(namespace=namespace, node=layers.node)
        return await super().decide(request, layers, protected_paths, ctx)


class NestedNodeSkippingPolicy(ReferencePolicy):
    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
        ctx: ProtocolContext,
    ) -> PolicyDecision:
        chosen = () if "/" in request.node_path else protected_paths
        return await super().decide(request, layers, chosen, ctx)


class LastDenyOnlyPolicy(ReferencePolicy):
    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
        ctx: ProtocolContext,
    ) -> PolicyDecision:
        trimmed = PolicyLayers(
            namespace=layers.namespace.model_copy(
                update={"deny": layers.namespace.deny[-1:]}
            ),
            node=layers.node.model_copy(update={"deny": layers.node.deny[-1:]}),
        )
        decision = await super().decide(request, trimmed, protected_paths, ctx)
        if ".deny." in decision.reason:
            return PolicyDecision(allow=False, reason="denied")
        return decision


class GlobOnlyProtectionPolicy(ReferencePolicy):
    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
        ctx: ProtocolContext,
    ) -> PolicyDecision:
        globs = tuple(entry for entry in protected_paths if "*" in entry)
        return await super().decide(request, layers, globs, ctx)


class ProtectionBlindCachePolicy(ReferencePolicy):
    def __init__(self) -> None:
        self.cache: dict[str, PolicyDecision] = {}

    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
        ctx: ProtocolContext,
    ) -> PolicyDecision:
        key = repr((request.tool, request.arguments, layers))
        cached = self.cache.get(key)
        if cached is None:
            cached = await super().decide(request, layers, protected_paths, ctx)
            self.cache[key] = cached
        return cached


class MisattributedProtectionPolicy(ReferencePolicy):
    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
        ctx: ProtocolContext,
    ) -> PolicyDecision:
        decision = await super().decide(request, layers, protected_paths, ctx)
        if decision.reason == "protected_path" and layers.namespace.deny:
            return PolicyDecision(allow=False, reason="namespace.deny.0")
        return decision


class SilentValidatePolicy(ReferencePolicy):
    def validate(self, policy: JsonObject) -> list[ContractViolation]:
        return []


class LooseDecisionPolicy(ReferencePolicy):
    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
        ctx: ProtocolContext,
    ) -> PolicyDecision:
        return LooseDecision(allow=True, reason="allowed")


class LooseDecision(PolicyDecision):
    pass


async def policy_exercise(ctx: ProtocolContext, marker: str) -> object:
    ctx.logger.emit(DECIDE_FAILED, reason="unreachable")
    raise KoraProtocolError("policy_failed", "the policy failed", False, {})


async def echoing_policy_exercise(ctx: ProtocolContext, marker: str) -> object:
    raise KoraProtocolError(
        "policy_failed", f"the policy failed on {marker}", False, {}
    )
