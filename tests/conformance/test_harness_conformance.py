import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import context_for
from kora.conformance.harness import (
    HarnessCase,
    HarnessExercise,
    kora_capture,
    kora_exercise,
    kora_harness_host,
    kora_provider,
    test_capabilities_are_frozen_and_consistent,
    test_config_schema_is_an_object_schema,
    test_failure_is_a_bounded_protocol_error,
    test_harness_events_are_normalised,
    test_harness_max_events_denies_at_exact_count,
    test_harness_policy_precedes_every_tool_call,
    test_harness_structured_output_and_fallbacks,
    test_harness_usage_completeness,
    test_provider_logs_through_its_context,
)
from kora.plugins.harness.fake import FakeHarness
from tests.plugins.harness.fake.fake_support import (
    CASES,
    FakeExercise,
    harness,
)

__all__ = [
    "kora_capture",
    "kora_exercise",
    "kora_harness_host",
    "kora_provider",
    "test_capabilities_are_frozen_and_consistent",
    "test_config_schema_is_an_object_schema",
    "test_failure_is_a_bounded_protocol_error",
    "test_harness_events_are_normalised",
    "test_harness_max_events_denies_at_exact_count",
    "test_harness_policy_precedes_every_tool_call",
    "test_harness_structured_output_and_fallbacks",
    "test_harness_usage_completeness",
    "test_provider_logs_through_its_context",
]


@pytest.fixture
def kora_harness_provider(kora_capture: Capture) -> FakeHarness:
    return harness(CASES[HarnessCase.TOOL_CALLS], context_for(kora_capture))


@pytest.fixture
def kora_harness_exercise() -> HarnessExercise:
    return FakeExercise()
