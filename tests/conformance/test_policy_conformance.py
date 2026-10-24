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
from tests.conformance.policies import ReferencePolicy, policy_exercise, reference_calls

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
def kora_policy_provider() -> ReferencePolicy:
    return ReferencePolicy()


@pytest.fixture
def kora_policy_exercise() -> object:
    return policy_exercise


@pytest.fixture
def kora_policy_calls() -> PolicyCalls:
    return reference_calls()
