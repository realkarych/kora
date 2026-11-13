from typing import Final

from pydantic import TypeAdapter, ValidationError

from kora.ir.graph_json import (
    GraphValueRejected,
    ObjectReader,
    TextBudget,
    render_object,
)
from kora.ir.json import JsonObject
from kora.ir.node import ToolPolicy
from kora.ir.tool_patterns import tool_policy_refusals
from kora.protocols.identity import Count
from kora.protocols.worker_claim_wire import (
    MAX_NAMESPACE_POLICY_BYTES,
    MAX_NODE_POLICY_BYTES,
    MAX_POLICY_COMPOSITION_BYTES,
    ClaimPartition,
    ClaimWireRejected,
    PreparedClaimPart,
    prepare_claim_part,
)

EXECUTION_REQUIREMENTS: Final[tuple[str, ...]] = (
    "require_protected_execution",
    "require_protected_resources",
    "require_network_enforcement",
    "allow_unenforced_agent_secrets",
    "allow_trust_repo_config",
)
REVISION: TypeAdapter[int] = TypeAdapter(Count)


def _bounded(value: object, limit: int) -> bytes:
    try:
        return render_object(value, TextBudget(limit, ascii_only=False)).encode("utf-8")
    except GraphValueRejected:
        raise ClaimWireRejected() from None


def _check_tool_policy(value: object) -> None:
    try:
        policy = ToolPolicy.model_validate(value)
    except ValidationError:
        raise ClaimWireRejected() from None
    if next(iter(tool_policy_refusals(policy)), None) is not None:
        raise ClaimWireRejected()


def prepare_node_policy(value: object) -> bytes:
    encoded = _bounded(value, MAX_NODE_POLICY_BYTES)
    _check_tool_policy(value)
    return encoded


def prepare_namespace_policy(value: JsonObject) -> bytes:
    projection = {
        name: value[name]
        for name in (*EXECUTION_REQUIREMENTS, "tool_policy")
        if name in value
    }
    encoded = _bounded(projection, MAX_NAMESPACE_POLICY_BYTES)
    if any(
        type(projection[name]) is not bool
        for name in EXECUTION_REQUIREMENTS
        if name in projection
    ):
        raise ClaimWireRejected()
    if "tool_policy" in projection:
        _check_tool_policy(projection["tool_policy"])
    return encoded


def prepare_claim_policy(
    namespace_policy: JsonObject, namespace_revision: int, node_policy: bytes
) -> PreparedClaimPart:
    namespace_bytes = prepare_namespace_policy(namespace_policy)
    if len(node_policy) > MAX_NODE_POLICY_BYTES:
        raise ClaimWireRejected()
    try:
        node = ObjectReader(node_policy.decode("utf-8")).object()
        revision = REVISION.validate_python(namespace_revision)
    except (UnicodeError, GraphValueRejected, ValidationError):
        raise ClaimWireRejected() from None
    if prepare_node_policy(node) != node_policy:
        raise ClaimWireRejected()
    result = prepare_claim_part(
        ClaimPartition.POLICY,
        {
            "execution_policy": {
                "values": {
                    "namespace": ObjectReader(namespace_bytes.decode("utf-8")).object(),
                    "node": {"tool_policy": node},
                },
                "namespace_revision": revision,
                "semantics": "kora.execution.v1",
            }
        },
    )
    if (
        len(result.payload) - len(namespace_bytes) - len(node_policy)
        > MAX_POLICY_COMPOSITION_BYTES
    ):
        raise ClaimWireRejected()
    return result
