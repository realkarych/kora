from pathlib import Path

import pytest

from kora.conformance.policy import (
    PolicyCalls,
    kora_capture,
    kora_exercise,
    kora_provider,
    test_capabilities_are_frozen_and_consistent,
    test_config_schema_is_an_object_schema,
    test_failure_is_a_bounded_protocol_error,
    test_policy_allow_list_restricts_in_either_layer,
    test_policy_allows_what_no_rule_refuses,
    test_policy_decisions_are_deterministic,
    test_policy_deny_in_either_layer_denies,
    test_policy_protected_path_is_denied,
    test_policy_validate_refuses_a_malformed_policy,
    test_provider_logs_through_its_context,
)
from kora.plugins.policy.allow_deny import AllowDenyPolicy
from kora.protocols.common import ProtocolContext
from kora.protocols.policy import PolicyDecision
from tests.plugins.policy.policy_support import (
    conformance_calls,
    layers,
    prepared_root,
    request,
)

__all__ = [
    "kora_capture",
    "kora_exercise",
    "kora_provider",
    "test_capabilities_are_frozen_and_consistent",
    "test_config_schema_is_an_object_schema",
    "test_failure_is_a_bounded_protocol_error",
    "test_policy_allow_list_restricts_in_either_layer",
    "test_policy_allows_what_no_rule_refuses",
    "test_policy_decisions_are_deterministic",
    "test_policy_deny_in_either_layer_denies",
    "test_policy_protected_path_is_denied",
    "test_policy_validate_refuses_a_malformed_policy",
    "test_provider_logs_through_its_context",
]


@pytest.fixture
def kora_policy_provider() -> AllowDenyPolicy:
    return AllowDenyPolicy()


@pytest.fixture
def kora_policy_exercise(tmp_path: Path) -> object:
    async def exercise(ctx: ProtocolContext, marker: str) -> PolicyDecision:
        unsupported = request(tmp_path, "unknown-harness", "Bash", {"command": marker})
        return await AllowDenyPolicy().decide(unsupported, layers(), (), ctx)

    return exercise


@pytest.fixture
def kora_policy_calls(tmp_path: Path) -> PolicyCalls:
    return conformance_calls(prepared_root(tmp_path))
