import json
from typing import cast

from kora.conformance.artifact_type import (
    ArtifactTypeConformanceCase,
    ArtifactTypeConformanceInputs,
)
from kora.ir import AgentSessionValidationError, parse_agent_session
from kora.ir.data import detach_ir_data
from kora.ir.diagnostics import ContractViolation
from kora.ir.json import JsonObject
from kora.ir.schema_catalogue import builtin_schema
from kora.protocols.artifact_type import ArtifactType, BuiltinArtifactTypeBinding
from kora.protocols.common import ProtocolContext
from tests.ir.agent_session_cases import dense, first_usage, fixture, metadata, rows
from tests.ir.test_agent_session_contract import ARTIFACTS

MARKER: str = "private-agent-session-price-marker"
USAGE_MARKER: str = "private-agent-session-usage-marker"
COVERAGE_MARKER: str = "private-agent-session-coverage-marker"
DENIAL_MARKER: str = "private-agent-session-denial-marker"
INVALID_MARKER: str = "private-agent-session-invalid-marker"
MARKERS: tuple[str, ...] = (
    MARKER,
    USAGE_MARKER,
    COVERAGE_MARKER,
    DENIAL_MARKER,
    INVALID_MARKER,
)


def binding() -> BuiltinArtifactTypeBinding:
    schema = builtin_schema("AgentSession", "1")
    assert schema is not None
    descriptor = cast(
        "JsonObject",
        json.loads((ARTIFACTS / "AgentSession.revision-1.json").read_bytes()),
    )
    return BuiltinArtifactTypeBinding(
        "AgentSession", cast("JsonObject", detach_ir_data(schema)), None, descriptor
    )


def payload() -> JsonObject:
    value = fixture("denial-sample-incomplete")
    metadata(value)["price_table_id"] = MARKER
    first_usage(value)["observation_id"] = USAGE_MARKER
    cast("JsonObject", metadata(value)["usage_coverage"])["observation_id"] = (
        COVERAGE_MARKER
    )
    denials = cast("JsonObject", value["denials"])
    cast("list[JsonObject]", denials["items"])[0]["tool"] = DENIAL_MARKER
    return value


def expected_summary() -> JsonObject:
    return cast(
        "JsonObject",
        next(
            case["permanent_view"]
            for case in rows("canonical_cases")
            if case["name"] == "denial-sample-incomplete"
        ),
    )


def equivalent() -> JsonObject:
    return dict(reversed(list(payload().items())))


def rejected(marker: str) -> JsonObject:
    value = payload()
    metadata(value)["duration_ms"] = marker
    return value


def violations() -> tuple[ContractViolation, ...]:
    try:
        parse_agent_session(rejected(INVALID_MARKER))
    except AgentSessionValidationError as error:
        return (error.violation,)
    raise AssertionError("the reference refusal was accepted")


def transcript_descriptor(*, truncated: bool = False) -> JsonObject:
    return {
        "capture": "captured",
        "source_size": 268435457 if truncated else 268435456,
        "source_sha256": "1" * 64,
        "source_verified": not truncated,
        "retained_size": 268435456,
        "retained_sha256": "2" * 64 if truncated else "1" * 64,
        "truncated": truncated,
    }


def late_refusal(value: JsonObject) -> JsonObject:
    result = cast("JsonObject", detach_ir_data(value))
    result["transcript"] = {**transcript_descriptor(), "source_verified": False}
    return result


def lifetime_shapes() -> tuple[tuple[JsonObject, JsonObject], ...]:
    minimal = fixture("unavailable-no-usage")
    crowded = dense()
    crowded["transcript"] = transcript_descriptor()
    return ((minimal, late_refusal(minimal)), (crowded, late_refusal(crowded)))


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
    assert provider.name == "AgentSession"
    assert provider.schema == binding().schema
    assert provider.validate(payload(), context) == []
    assert provider.summary(payload(), context) == expected_summary()


def inputs() -> ArtifactTypeConformanceInputs:
    return ArtifactTypeConformanceInputs(
        accepted=(
            ArtifactTypeConformanceCase(payload, equivalent, ("",), expected_summary),
        ),
        rejected=rejected,
        expected_violations=violations(),
        failure=failure,
        configuration_effect=configuration_effect,
    )
