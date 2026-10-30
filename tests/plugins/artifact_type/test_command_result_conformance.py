import pytest

from kora.conformance.artifact_type import (
    ArtifactTypeConformanceInputs,
    kora_capture,
    kora_exercise,
    kora_provider,
    test_artifact_type_accepts_declared_values,
    test_artifact_type_configuration_is_effective,
    test_artifact_type_digest_fields_are_stable,
    test_artifact_type_invalid_extraction_is_refused,
    test_artifact_type_rejects_declared_values,
    test_artifact_type_summaries_are_detached,
    test_capabilities_are_frozen_and_consistent,
    test_config_schema_is_an_object_schema,
    test_failure_is_a_bounded_protocol_error,
    test_provider_logs_through_its_context,
)
from kora.ir.json import JsonObject
from kora.plugins.artifact_type.command_result import CommandResultArtifactType
from tests.plugins.artifact_type.command_result_support import binding, inputs

__all__ = [
    "kora_capture",
    "kora_exercise",
    "kora_provider",
    "test_artifact_type_accepts_declared_values",
    "test_artifact_type_configuration_is_effective",
    "test_artifact_type_digest_fields_are_stable",
    "test_artifact_type_invalid_extraction_is_refused",
    "test_artifact_type_rejects_declared_values",
    "test_artifact_type_summaries_are_detached",
    "test_capabilities_are_frozen_and_consistent",
    "test_config_schema_is_an_object_schema",
    "test_failure_is_a_bounded_protocol_error",
    "test_provider_logs_through_its_context",
]


@pytest.fixture
def kora_artifact_type_provider() -> CommandResultArtifactType:
    return CommandResultArtifactType({}, binding())


@pytest.fixture
def kora_artifact_type_inputs() -> ArtifactTypeConformanceInputs:
    return inputs()


@pytest.fixture
def kora_artifact_type_configuration() -> JsonObject:
    return {}
