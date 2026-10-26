import pytest

from kora.conformance.runtime import (
    kora_capture,
    kora_exercise,
    kora_provider,
    test_capabilities_are_frozen_and_consistent,
    test_config_schema_is_an_object_schema,
    test_failure_is_a_bounded_protocol_error,
    test_provider_logs_through_its_context,
)
from kora.protocols.runtime import Runtime
from tests.conformance.runtimes import ReferenceRuntime, runtime_exercise

__all__ = [
    "kora_capture",
    "kora_exercise",
    "kora_provider",
    "test_capabilities_are_frozen_and_consistent",
    "test_config_schema_is_an_object_schema",
    "test_failure_is_a_bounded_protocol_error",
    "test_provider_logs_through_its_context",
]


@pytest.fixture
def kora_runtime_provider() -> Runtime:
    provider: Runtime = ReferenceRuntime()
    return provider


@pytest.fixture
def kora_runtime_exercise() -> object:
    return runtime_exercise
