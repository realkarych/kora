import pytest

from kora.db.bootstrap import DEFAULT_POLICY
from kora.ir.graph_json import ObjectReader, TextBudget, render_object
from kora.ir.json import JsonObject
from kora.protocols.worker_claim_policy import (
    prepare_claim_policy,
    prepare_namespace_policy,
    prepare_node_policy,
)
from kora.protocols.worker_claim_wire import (
    MAX_NAMESPACE_POLICY_BYTES,
    MAX_NODE_POLICY_BYTES,
    ClaimWireRejected,
)


@pytest.mark.parametrize("extra", [0, 1])
def test_node_policy_admits_exact_layer_boundary(extra: int) -> None:
    empty: JsonObject = {"x-data": ""}
    wrapper = len(render_object(empty, TextBudget(MAX_NODE_POLICY_BYTES)))
    policy: JsonObject = {"x-data": "a" * (MAX_NODE_POLICY_BYTES - wrapper + extra)}

    if extra:
        with pytest.raises(ClaimWireRejected):
            prepare_node_policy(policy)
    else:
        encoded = prepare_node_policy(policy)

        assert len(encoded) == MAX_NODE_POLICY_BYTES
        assert ObjectReader(encoded.decode("utf-8")).object() == policy


@pytest.mark.parametrize("extra", [0, 1])
def test_namespace_policy_admits_exact_layer_boundary(extra: int) -> None:
    empty: JsonObject = {"tool_policy": {"x-data": ""}}
    wrapper = len(render_object(empty, TextBudget(MAX_NAMESPACE_POLICY_BYTES)))
    policy: JsonObject = {
        "tool_policy": {"x-data": "a" * (MAX_NAMESPACE_POLICY_BYTES - wrapper + extra)}
    }

    if extra:
        with pytest.raises(ClaimWireRejected):
            prepare_namespace_policy(policy)
    else:
        encoded = prepare_namespace_policy(policy)

        assert len(encoded) == MAX_NAMESPACE_POLICY_BYTES
        assert ObjectReader(encoded.decode("utf-8")).object() == policy


def test_execution_policy_preserves_layers_and_excludes_live_authorization() -> None:
    namespace: JsonObject = {
        "tool_policy": {
            "allow": ["Bash(git *)"],
            "deny": ["Bash(git push *)"],
            "x-engine": {"mode": "strict"},
        },
        "require_protected_execution": True,
        "allow_unenforced_agent_secrets": False,
        "secret_prefixes": ["private-marker"],
        "concurrency": {"max_pending_runs": 1000},
        "daily_budget_usd": "123",
        "approver_allowlist": ["private-marker"],
        "http_allowlist": ["private-marker"],
    }
    node: JsonObject = {"allow": ["Bash(git status *)"], "x-engine": {"zero": -0.0}}

    prepared = prepare_claim_policy(namespace, 41, prepare_node_policy(node))
    encoded = prepared.payload
    namespace["require_protected_execution"] = False
    node["allow"] = ["Bash(*)"]
    value = ObjectReader(encoded.decode("utf-8")).object()

    assert value == {
        "execution_policy": {
            "namespace_revision": 41,
            "semantics": "kora.execution.v1",
            "values": {
                "namespace": {
                    "tool_policy": {
                        "allow": ["Bash(git *)"],
                        "deny": ["Bash(git push *)"],
                        "x-engine": {"mode": "strict"},
                    },
                    "require_protected_execution": True,
                    "allow_unenforced_agent_secrets": False,
                },
                "node": {
                    "tool_policy": {
                        "allow": ["Bash(git status *)"],
                        "x-engine": {"zero": -0.0},
                    }
                },
            },
        }
    }
    assert b"private-marker" not in encoded
    assert b"-0.0" in encoded


@pytest.mark.parametrize(
    "policy",
    [
        {"require_protected_execution": 1},
        {"allow_trust_repo_config": "true"},
        {"tool_policy": {"allow": ["StructuredOutput"]}},
        {"tool_policy": {"unknown": []}},
    ],
)
def test_namespace_execution_policy_refuses_invalid_requirements(
    policy: JsonObject,
) -> None:
    with pytest.raises(ClaimWireRejected) as error:
        prepare_namespace_policy(policy)

    assert str(error.value) == "the Claim representation is invalid"


def test_bootstrap_policy_has_no_execution_requirements() -> None:
    policy = DEFAULT_POLICY

    encoded = prepare_namespace_policy(policy)

    assert encoded == b"{}"
