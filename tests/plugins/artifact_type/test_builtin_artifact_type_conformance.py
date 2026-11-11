from collections.abc import Callable
from dataclasses import dataclass

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
from kora.plugins.artifact_type.agent_session import AgentSessionArtifactType
from kora.plugins.artifact_type.command_result import CommandResultArtifactType
from kora.plugins.artifact_type.structured_output import StructuredOutputArtifactType
from kora.plugins.artifact_type.vcs_ref import VcsRefArtifactType
from kora.protocols.artifact_type import ArtifactType
from tests.plugins.artifact_type import (
    agent_session_support,
    command_result_support,
    structured_output_support,
    support,
)

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


@dataclass(frozen=True)
class BuiltinCase:
    provider: Callable[[], ArtifactType]
    inputs: Callable[[], ArtifactTypeConformanceInputs]


def vcs_ref() -> ArtifactType:
    return VcsRefArtifactType({}, support.binding())


def command_result() -> ArtifactType:
    return CommandResultArtifactType({}, command_result_support.binding())


def agent_session() -> ArtifactType:
    return AgentSessionArtifactType({}, agent_session_support.binding())


def structured_output() -> ArtifactType:
    return StructuredOutputArtifactType({}, structured_output_support.binding())


CASES: dict[str, BuiltinCase] = {
    "vcs_ref": BuiltinCase(vcs_ref, support.inputs),
    "command_result": BuiltinCase(command_result, command_result_support.inputs),
    "agent_session": BuiltinCase(agent_session, agent_session_support.inputs),
    "structured_output": BuiltinCase(
        structured_output, structured_output_support.inputs
    ),
}


@pytest.fixture(params=tuple(CASES))
def builtin_case(request: pytest.FixtureRequest) -> BuiltinCase:
    name: object = request.param
    assert isinstance(name, str)
    return CASES[name]


@pytest.fixture
def kora_artifact_type_provider(builtin_case: BuiltinCase) -> ArtifactType:
    return builtin_case.provider()


@pytest.fixture
def kora_artifact_type_inputs(
    builtin_case: BuiltinCase,
) -> ArtifactTypeConformanceInputs:
    return builtin_case.inputs()


@pytest.fixture
def kora_artifact_type_configuration() -> JsonObject:
    return {}
