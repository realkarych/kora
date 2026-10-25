from dataclasses import replace
from typing import Final

import pytest

from kora.ir.json import DEFAULT_MAX_DEPTH, MAX_CANONICAL_BYTES, JsonObject
from kora.protocols.harness import (
    Enforcement,
    HarnessValueRefused,
    PermissionDeniedData,
)
from kora.protocols.policy import (
    POLICY_REASONS,
    PolicyDecision,
    PolicyValueRefused,
    ToolCallRequest,
)
from kora.protocols.vcs import WorkspaceRef
from tests.protocols.harness_documents import (
    REFUSED_MARKER,
    WORKSPACE_REF,
    nested_list,
)

WORKSPACE: Final[WorkspaceRef] = WorkspaceRef.model_validate(WORKSPACE_REF)
REQUEST: Final[ToolCallRequest] = ToolCallRequest(
    harness="claude",
    tool="Bash",
    tool_use_id="call-1",
    agent_id=None,
    arguments={"command": REFUSED_MARKER},
    workspace=WORKSPACE,
    node_path="graph.node",
)


class ReasonText(str):
    def __str__(self) -> str:
        return REFUSED_MARKER


DECISION: Final[PolicyDecision] = PolicyDecision(allow=True, reason="allowed")
TERMINATION: Final[Enforcement] = Enforcement(action="terminate", cause="stall")
REFUSED: Final[
    list[tuple[ToolCallRequest | PolicyDecision | Enforcement, str, object]]
] = [
    (REQUEST, "harness", f"{REFUSED_MARKER} harness"),
    (REQUEST, "tool", ReasonText("Bash")),
    (REQUEST, "harness", ReasonText("claude")),
    (REQUEST, "tool_use_id", ReasonText("call-1")),
    (REQUEST, "node_path", "n" * 1025),
    (REQUEST, "agent_id", ReasonText("agent")),
    (REQUEST, "node_path", ReasonText("graph.node")),
    (REQUEST, "tool", ""),
    (REQUEST, "tool_use_id", 7),
    (REQUEST, "agent_id", "a" * 257),
    (REQUEST, "workspace", WORKSPACE_REF),
    (REQUEST, "node_path", f"{REFUSED_MARKER} path"),
    (REQUEST, "arguments", {"command": float("nan")}),
    (REQUEST, "arguments", [REFUSED_MARKER]),
    (REQUEST, "arguments", {"command": nested_list(DEFAULT_MAX_DEPTH)}),
    (REQUEST, "arguments", {"command": "x" * MAX_CANONICAL_BYTES}),
    (DECISION, "allow", 1),
    (DECISION, "reason", f"{REFUSED_MARKER} because"),
    (DECISION, "reason", "namespace.deny.12345"),
    (DECISION, "reason", "graph.deny.0"),
    (DECISION, "reason", "node.allow.007"),
    (DECISION, "reason", "namespace.deny.0"),
    (DECISION, "reason", "protected_path"),
    (replace(DECISION, allow=False, reason="denied"), "reason", "allowed"),
    (replace(DECISION, allow=False, reason="denied"), "reason", "node.allow.0"),
    (DECISION, "reason", ReasonText("allowed")),
    (TERMINATION, "cause", None),
    (Enforcement(action="continue", cause=None), "cause", "stall"),
]


def test_request_repr_shows_no_argument() -> None:
    built = REQUEST

    rendered = repr(built)

    assert REFUSED_MARKER not in rendered
    assert "call-1" in rendered


def test_request_owns_a_parsed_copy_of_its_arguments() -> None:
    arguments: JsonObject = {"command": "ls"}

    built = replace(REQUEST, arguments=arguments)
    arguments["command"] = REFUSED_MARKER

    assert built.arguments == {"command": "ls"}


@pytest.mark.parametrize(
    ("allow", "reason"),
    [
        (True, "allowed"),
        (True, "node.allow.0"),
        (True, "namespace.allow.9999"),
        (False, "denied"),
        (False, "protected_path"),
        (False, "namespace.deny.0"),
        (False, "node.deny.10"),
    ],
)
def test_policy_decision_accepts_reasons_and_rule_ids(allow: bool, reason: str) -> None:
    built = PolicyDecision(allow=allow, reason=reason)

    assert (built.allow, built.reason) == (allow, reason)


@pytest.mark.parametrize(("valid", "field", "value"), REFUSED)
def test_policy_dataclasses_check_their_fields(
    valid: ToolCallRequest | PolicyDecision | Enforcement, field: str, value: object
) -> None:
    changes = {field: value}

    with pytest.raises((PolicyValueRefused, HarnessValueRefused)) as raised:
        replace(valid, **changes)

    assert str(raised.value).endswith(f" {field} is invalid")
    assert REFUSED_MARKER not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_node_paths_reach_their_bound() -> None:
    node_path = "n" * 1024

    built = replace(REQUEST, node_path=node_path)

    assert built.node_path == node_path


DENIAL_MAPPING: Final[list[tuple[str, str | None, str]]] = [
    *((reason, None, reason) for reason in sorted(POLICY_REASONS - {"allowed"})),
    ("namespace.deny.0", "namespace.deny.0", "denied"),
    ("node.deny.9999", "node.deny.9999", "denied"),
]


@pytest.mark.parametrize(("reason", "pattern", "code"), DENIAL_MAPPING)
def test_every_denial_maps_to_a_permission_denied_event(
    reason: str, pattern: str | None, code: str
) -> None:
    decision = PolicyDecision(allow=False, reason=reason)
    document = {
        "tool": "Bash",
        "tool_use_id": "call-1",
        "source": "policy",
        "pattern": pattern,
        "code": code,
    }

    event = PermissionDeniedData.model_validate(document)

    assert (decision.reason, event.pattern, event.code) == (reason, pattern, code)
