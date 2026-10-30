import json
from collections.abc import Callable
from functools import partial
from typing import cast

from kora.conformance.artifact_type import (
    ArtifactTypeConformanceCase,
    ArtifactTypeConformanceInputs,
)
from kora.ir import StructuredOutputValidationError, parse_structured_output
from kora.ir.data import detach_ir_data
from kora.ir.diagnostics import ContractViolation
from kora.ir.json import JsonObject, JsonValue
from kora.ir.schema_catalogue import builtin_schema
from kora.protocols.artifact_type import ArtifactType, BuiltinArtifactTypeBinding
from kora.protocols.common import ProtocolContext
from tests.ir.structured_output_cases import ARTIFACTS

MARKER: str = "private-structured-output-payload-marker"
KEY_MARKER: str = "private-structured-output-key-marker"
INVALID_MARKER: str = "private-structured-output-invalid-marker"
MARKERS: tuple[str, ...] = (MARKER, KEY_MARKER, INVALID_MARKER)


def binding() -> BuiltinArtifactTypeBinding:
    schema = builtin_schema("StructuredOutput", "1")
    assert schema is not None
    descriptor = cast(
        "JsonObject",
        json.loads((ARTIFACTS / "StructuredOutput.revision-1.json").read_bytes()),
    )
    return BuiltinArtifactTypeBinding(
        "StructuredOutput", cast("JsonObject", detach_ir_data(schema)), None, descriptor
    )


def payload() -> JsonObject:
    return {KEY_MARKER: [MARKER, 1, 1.0, -0.0, 0.0, None, "\0"], "a": {"x": True}}


def array_payload() -> JsonValue:
    return [payload(), None, 1, 1.0, -0.0, "\0"]


def string_payload() -> JsonValue:
    return MARKER + "\0"


def integer_payload() -> JsonValue:
    return 9_007_199_254_740_993


def float_payload() -> JsonValue:
    return 1.0


def negative_zero_payload() -> JsonValue:
    return -0.0


def boolean_payload() -> JsonValue:
    return True


def null_payload() -> JsonValue:
    return None


ROOT_FACTORIES: tuple[Callable[[], JsonValue], ...] = (
    payload,
    array_payload,
    string_payload,
    integer_payload,
    float_payload,
    negative_zero_payload,
    boolean_payload,
    null_payload,
)


def reverse_members(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        return {name: reverse_members(item) for name, item in reversed(value.items())}
    if isinstance(value, list):
        return [reverse_members(item) for item in value]
    return value


def equivalent(build: Callable[[], JsonValue]) -> JsonValue:
    return reverse_members(build())


def rejected(marker: str) -> JsonValue:
    return {marker: float("inf")}


def violations() -> tuple[ContractViolation, ...]:
    try:
        parse_structured_output(rejected(INVALID_MARKER))
    except StructuredOutputValidationError as error:
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
    assert provider.name == "StructuredOutput"
    assert provider.schema == binding().schema
    assert provider.validate(payload(), context) == []
    assert provider.summary(None, context) is None


def inputs() -> ArtifactTypeConformanceInputs:
    return ArtifactTypeConformanceInputs(
        accepted=tuple(
            ArtifactTypeConformanceCase(build, partial(equivalent, build), ("",), build)
            for build in ROOT_FACTORIES
        ),
        rejected=rejected,
        expected_violations=violations(),
        failure=failure,
        configuration_effect=configuration_effect,
    )
