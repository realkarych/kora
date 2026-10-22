import pytest

from kora.conformance.secrets import (
    kora_capture,
    kora_exercise,
    kora_provider,
    test_capabilities_are_frozen_and_consistent,
    test_config_schema_is_an_object_schema,
    test_failure_is_a_bounded_protocol_error,
    test_provider_logs_through_its_context,
    test_secrets_missing_reference_is_a_bounded_error,
    test_secrets_resolution_returns_a_masked_value,
    test_secrets_value_is_masked_in_a_captured_stream,
)
from tests.conformance.providers import (
    MISSING,
    REFERENCE,
    VALUE,
    ReferenceSecretsProvider,
    exercise,
)

__all__ = [
    "kora_capture",
    "kora_exercise",
    "kora_provider",
    "test_capabilities_are_frozen_and_consistent",
    "test_config_schema_is_an_object_schema",
    "test_failure_is_a_bounded_protocol_error",
    "test_provider_logs_through_its_context",
    "test_secrets_missing_reference_is_a_bounded_error",
    "test_secrets_resolution_returns_a_masked_value",
    "test_secrets_value_is_masked_in_a_captured_stream",
]


@pytest.fixture
def kora_secrets_provider() -> ReferenceSecretsProvider:
    return ReferenceSecretsProvider()


@pytest.fixture
def kora_secrets_exercise() -> object:
    return exercise


@pytest.fixture
def kora_secrets_resolvable() -> tuple[str, str]:
    return REFERENCE, VALUE


@pytest.fixture
def kora_secrets_missing_reference() -> str:
    return MISSING
