import re
from dataclasses import dataclass, field
from typing import Final, Literal, Protocol, get_args

from kora.ir import ContractViolation, ToolPolicy
from kora.ir.json import JsonObject, JsonShapeError, parse_json_object
from kora.protocols.common import ProtocolContext, is_identifier
from kora.protocols.vcs import WorkspaceRef

PolicyReason = Literal[
    "allowed",
    "denied",
    "protected_path",
    "not_allowed",
    "request_invalid",
    "argument_invalid",
]
POLICY_REASONS: Final[frozenset[str]] = frozenset(get_args(PolicyReason))
RULE_ID: Final[re.Pattern[str]] = re.compile(
    r"(?:namespace|node)\.(?:allow|deny)\.(?:0|[1-9][0-9]{0,3})"
)
NODE_PATH: Final[re.Pattern[str]] = re.compile(r"[\x21-\x7e]{1,1024}")


class PolicyValueRefused(ValueError):
    def __init__(self, field_name: str) -> None:
        super().__init__(f"the policy value {field_name} is invalid")


def is_reason(value: object) -> bool:
    if type(value) is not str:
        return False
    return value in POLICY_REASONS or RULE_ID.fullmatch(value) is not None


def is_workspace(value: object) -> bool:
    return isinstance(value, WorkspaceRef)


def is_allowing_reason(reason: str) -> bool:
    return reason == "allowed" or ".allow." in reason


def is_exact_identifier(value: object) -> bool:
    return type(value) is str and is_identifier(value)


def is_node_path(value: object) -> bool:
    return type(value) is str and NODE_PATH.fullmatch(value) is not None


def require(condition: bool, field_name: str) -> None:
    if not condition:
        raise PolicyValueRefused(field_name)


@dataclass(frozen=True)
class ToolCallRequest:
    harness: str
    tool: str
    tool_use_id: str
    agent_id: str | None
    arguments: JsonObject = field(repr=False)
    workspace: WorkspaceRef
    node_path: str

    def __post_init__(self) -> None:
        for name in ("harness", "tool", "tool_use_id"):
            require(is_exact_identifier(getattr(self, name)), name)
        require(self.agent_id is None or is_exact_identifier(self.agent_id), "agent_id")
        require(is_workspace(self.workspace), "workspace")
        require(is_node_path(self.node_path), "node_path")
        try:
            arguments = parse_json_object(self.arguments)
        except JsonShapeError:
            arguments = None
        require(arguments is not None, "arguments")
        object.__setattr__(self, "arguments", arguments)


@dataclass(frozen=True)
class PolicyLayers:
    namespace: ToolPolicy
    node: ToolPolicy


@dataclass(frozen=True)
class PolicyDecision:
    allow: bool
    reason: str

    def __post_init__(self) -> None:
        require(type(self.allow) is bool, "allow")
        require(is_reason(self.reason), "reason")
        require(is_allowing_reason(self.reason) == self.allow, "reason")


@dataclass(frozen=True)
class PolicyCapabilities:
    pass


class Policy(Protocol):
    name: str
    config_schema: JsonObject

    def capabilities(self) -> PolicyCapabilities: ...

    def validate(self, policy: JsonObject) -> list[ContractViolation]: ...

    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
        ctx: ProtocolContext,
    ) -> PolicyDecision: ...


class PolicyClient(Protocol):
    async def decide(
        self,
        request: ToolCallRequest,
        layers: PolicyLayers,
        protected_paths: tuple[str, ...],
    ) -> PolicyDecision: ...
