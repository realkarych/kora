import json
from typing import cast

from kora.conformance.artifact_type import (
    ArtifactTypeConformanceCase,
    ArtifactTypeConformanceInputs,
)
from kora.ir.diagnostics import ContractViolation
from kora.ir.json import JsonObject, JsonValue
from kora.ir.schema_catalogue import builtin_schema
from kora.ir.vcs_ref import VcsRefValidationError, parse_vcs_ref
from kora.protocols.artifact_type import ArtifactType, BuiltinArtifactTypeBinding
from kora.protocols.common import ProtocolContext
from tests.ir.vcs_ref_cases import ARTIFACTS, example


def binding() -> BuiltinArtifactTypeBinding:
    schema = builtin_schema("VcsRef", "1")
    assert schema is not None
    descriptor = cast(
        "JsonObject", json.loads((ARTIFACTS / "VcsRef.revision-1.json").read_bytes())
    )
    return BuiltinArtifactTypeBinding("VcsRef", dict(schema), None, descriptor)


def payload() -> JsonObject:
    return example()


def equivalent() -> JsonObject:
    return dict(reversed(list(payload().items())))


def rejected(marker: str) -> JsonValue:
    return {**payload(), "commit": marker, "changed_paths": ["../" + marker]}


def violations() -> tuple[ContractViolation, ...]:
    try:
        parse_vcs_ref(rejected("marker"))
    except VcsRefValidationError as error:
        return (error.violation,)
    raise AssertionError("the reference refusal was accepted")


async def failure(
    provider: ArtifactType,
    configuration: JsonObject,
    context: ProtocolContext,
    marker: str,
) -> object:
    assert configuration == {}
    return provider.summary(rejected(marker), context)


async def configuration_effect(
    provider: ArtifactType, configuration: JsonObject, context: ProtocolContext
) -> None:
    assert configuration == {}
    assert provider.name == "VcsRef"
    assert provider.schema == binding().schema
    assert provider.validate(payload(), context) == []


def inputs() -> ArtifactTypeConformanceInputs:
    return ArtifactTypeConformanceInputs(
        accepted=(ArtifactTypeConformanceCase(payload, equivalent, ("",), payload),),
        rejected=rejected,
        expected_violations=violations(),
        failure=failure,
        configuration_effect=configuration_effect,
    )
