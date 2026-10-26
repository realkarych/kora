import pytest

from kora.conformance.vcs import (
    kora_capture,
    kora_exercise,
    kora_provider,
    test_capabilities_are_frozen_and_consistent,
    test_config_schema_is_an_object_schema,
    test_failure_is_a_bounded_protocol_error,
    test_provider_logs_through_its_context,
)
from kora.protocols.vcs import Vcs
from tests.conformance.runtimes import ReferenceVcs, vcs_exercise

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
def kora_vcs_provider() -> Vcs:
    provider: Vcs = ReferenceVcs()
    return provider


@pytest.fixture
def kora_vcs_exercise() -> object:
    return vcs_exercise
