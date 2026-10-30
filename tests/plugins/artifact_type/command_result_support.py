import json
from typing import cast

from kora.conformance.artifact_type import (
    ArtifactTypeConformanceCase,
    ArtifactTypeConformanceInputs,
)
from kora.ir import CommandResultValidationError, parse_command_result
from kora.ir.data import detach_ir_data
from kora.ir.diagnostics import ContractViolation
from kora.ir.json import JsonObject, JsonValue
from kora.ir.schema_catalogue import builtin_schema
from kora.protocols.artifact_type import ArtifactType, BuiltinArtifactTypeBinding
from kora.protocols.common import ProtocolContext
from tests.ir.command_result_cases import stream_bytes, with_resources
from tests.ir.test_command_result_contract import ARTIFACTS

MARKER: str = "private-command-result-marker"


def binding() -> BuiltinArtifactTypeBinding:
    schema = builtin_schema("CommandResult", "1")
    assert schema is not None
    descriptor = cast(
        "JsonObject",
        json.loads((ARTIFACTS / "CommandResult.revision-1.json").read_bytes()),
    )
    return BuiltinArtifactTypeBinding(
        "CommandResult", cast("JsonObject", detach_ir_data(schema)), None, descriptor
    )


def payload() -> JsonObject:
    value = with_resources([MARKER])
    value["repo"] = MARKER
    value["commit"] = MARKER
    value["protected_paths"] = [MARKER]
    value["protection"] = "verified"
    value["stdout"] = {**stream_bytes(MARKER.encode()), "storage_ref": MARKER}
    return value


def equivalent() -> JsonObject:
    return dict(reversed(list(payload().items())))


def rejected(marker: str) -> JsonValue:
    return {**payload(), "duration_ms": marker}


def violations() -> tuple[ContractViolation, ...]:
    try:
        parse_command_result(rejected(MARKER))
    except CommandResultValidationError as error:
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
    assert provider.name == "CommandResult"
    assert provider.schema == binding().schema
    assert provider.validate(payload(), context) == []
    assert provider.summary(payload(), context) == payload()


def inputs() -> ArtifactTypeConformanceInputs:
    return ArtifactTypeConformanceInputs(
        accepted=(ArtifactTypeConformanceCase(payload, equivalent, ("",), payload),),
        rejected=rejected,
        expected_violations=violations(),
        failure=failure,
        configuration_effect=configuration_effect,
    )
