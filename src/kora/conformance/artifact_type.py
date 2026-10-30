import asyncio
import json
from collections.abc import Awaitable, Callable, Generator
from copy import deepcopy
from dataclasses import dataclass
from functools import partial

import pytest

from kora.conformance.capture import Capture, captured
from kora.conformance.common import (
    CONFORMANCE_COMPONENT,
    INSTANCE_ID,
    MARKER,
    Exercise,
    awaited,
    check,
    check_protocol_error,
    context_for,
    fail,
    test_capabilities_are_frozen_and_consistent,
    test_config_schema_is_an_object_schema,
    test_failure_is_a_bounded_protocol_error,
    test_provider_logs_through_its_context,
    utc_now,
)
from kora.ir import ContractViolation
from kora.ir.json import JsonObject, JsonValue
from kora.observability import Level, LoggingSettings
from kora.protocols.artifact_type import ArtifactType
from kora.protocols.common import ProtocolContext
from kora.protocols.errors import KoraProtocolError

__all__ = [
    "ArtifactTypeConformanceCase",
    "ArtifactTypeConformanceInputs",
    "artifact_type_capture_settings",
    "check_artifact_type_accepts_declared_values",
    "check_artifact_type_digest_fields_are_stable",
    "check_artifact_type_invalid_extraction_is_refused",
    "check_artifact_type_rejects_declared_values",
    "check_artifact_type_summaries_are_detached",
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
class ArtifactTypeConformanceCase:
    payload: Callable[[], JsonValue]
    equivalent_payload: Callable[[], JsonValue]
    expected_digest_fields: tuple[str, ...]
    expected_summary: Callable[[], JsonValue]


@dataclass(frozen=True)
class ArtifactTypeConformanceInputs:
    accepted: tuple[ArtifactTypeConformanceCase, ...]
    rejected: Callable[[str], JsonValue]
    expected_violations: tuple[ContractViolation, ...]
    failure: Callable[
        [ArtifactType, JsonObject, ProtocolContext, str], Awaitable[object]
    ]
    configuration_effect: Callable[
        [ArtifactType, JsonObject, ProtocolContext], Awaitable[None]
    ]


def artifact_type_capture_settings() -> LoggingSettings:
    return LoggingSettings(component_levels={CONFORMANCE_COMPONENT: Level.DEBUG})


@pytest.fixture
def kora_capture() -> Generator[Capture]:
    with captured(
        CONFORMANCE_COMPONENT, artifact_type_capture_settings(), INSTANCE_ID, utc_now
    ) as capture:
        yield capture


@pytest.fixture
def kora_provider(kora_artifact_type_provider: ArtifactType) -> ArtifactType:
    return kora_artifact_type_provider


@pytest.fixture
def kora_exercise(
    kora_artifact_type_provider: ArtifactType,
    kora_artifact_type_inputs: ArtifactTypeConformanceInputs,
    kora_artifact_type_configuration: JsonObject,
) -> Exercise:
    async def exercise(context: ProtocolContext, marker: str) -> object:
        return await kora_artifact_type_inputs.failure(
            kora_artifact_type_provider,
            kora_artifact_type_configuration,
            context,
            marker,
        )

    return exercise


def accepted_cases(
    inputs: ArtifactTypeConformanceInputs,
) -> tuple[ArtifactTypeConformanceCase, ...]:
    check(bool(inputs.accepted), "accepted artifact cases are nonempty")
    return inputs.accepted


def same_json(first: JsonValue, second: JsonValue) -> bool:
    return json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def check_artifact_type_accepts_declared_values(
    provider: ArtifactType, inputs: ArtifactTypeConformanceInputs, ctx: ProtocolContext
) -> None:
    for case in accepted_cases(inputs):
        check(
            same_json(case.payload(), case.equivalent_payload()),
            "equivalent payloads preserve the JSON value",
        )
        for build in (case.payload, case.equivalent_payload):
            payload = build()
            before = deepcopy(payload)

            violations = provider.validate(payload, ctx)

            check(
                type(violations) is list and not violations, "valid payload is accepted"
            )
            check(same_json(payload, before), "validation preserves its input")


def check_artifact_type_rejects_declared_values(
    provider: ArtifactType, inputs: ArtifactTypeConformanceInputs, ctx: ProtocolContext
) -> None:
    check(bool(inputs.expected_violations), "expected artifact violations are nonempty")
    payload = inputs.rejected(MARKER)

    result = provider.validate(payload, ctx)

    check(type(result) is list, "validation returns a list")
    check(
        all(type(item) is ContractViolation for item in result),
        "validation returns contract violations",
    )
    check(
        tuple(result) == inputs.expected_violations,
        "validation preserves declared errors",
    )
    for violation in result:
        check(violation.path is not None, "validation errors have segmented JSON paths")
        check(
            MARKER not in violation.model_dump_json(),
            "validation errors carry no submitted content",
        )


def check_artifact_type_digest_fields_are_stable(
    provider: ArtifactType, inputs: ArtifactTypeConformanceInputs, ctx: ProtocolContext
) -> None:
    for case in accepted_cases(inputs):
        for build in (case.payload, case.equivalent_payload):
            payload = build()
            before = deepcopy(payload)

            fields = provider.digest_fields(payload, ctx)

            check(type(fields) is list, "digest selection is a list")
            check(
                tuple(fields) == case.expected_digest_fields, "digest selection matches"
            )
            check(same_json(payload, before), "digest selection preserves its input")
            fields.clear()
            check(
                tuple(provider.digest_fields(build(), ctx))
                == case.expected_digest_fields,
                "digest selections are detached",
            )


def alter_containers(value: JsonValue) -> None:
    if isinstance(value, dict):
        for item in tuple(value.values()):
            alter_containers(item)
        value["kora_conformance_mutation"] = True
    elif isinstance(value, list):
        for item in tuple(value):
            alter_containers(item)
        value.append("kora_conformance_mutation")


def check_artifact_type_summaries_are_detached(
    provider: ArtifactType, inputs: ArtifactTypeConformanceInputs, ctx: ProtocolContext
) -> None:
    for case in accepted_cases(inputs):
        for build in (case.payload, case.equivalent_payload):
            payload = build()
            before = deepcopy(payload)

            summary = provider.summary(payload, ctx)

            check(same_json(summary, case.expected_summary()), "the summary matches")
            check(same_json(payload, before), "summary computation preserves its input")
            alter_containers(summary)
            check(
                same_json(payload, before), "summary containers are detached from input"
            )
            check(
                same_json(provider.summary(build(), ctx), case.expected_summary()),
                "summary containers are detached from provider state",
            )


def extraction_error(action: Callable[[], object]) -> KoraProtocolError | None:
    try:
        action()
    except KoraProtocolError as error:
        return error
    return None


def check_artifact_type_invalid_extraction_is_refused(
    provider: ArtifactType, inputs: ArtifactTypeConformanceInputs, ctx: ProtocolContext
) -> None:
    for method in (provider.digest_fields, provider.summary):
        payload = inputs.rejected(MARKER)

        error = extraction_error(partial(method, payload, ctx))

        check_protocol_error(error, MARKER)
        if error is None:
            fail("invalid extraction raises a protocol error")
        check(not error.retryable, "invalid extraction is not retryable")
        check(
            error.__context__ is None and error.__cause__ is None,
            "invalid extraction has no native exception chain",
        )


@pytest.mark.kora_conformance
def test_artifact_type_accepts_declared_values(
    kora_artifact_type_provider: ArtifactType,
    kora_artifact_type_inputs: ArtifactTypeConformanceInputs,
    kora_capture: Capture,
) -> None:
    check_artifact_type_accepts_declared_values(
        kora_artifact_type_provider,
        kora_artifact_type_inputs,
        context_for(kora_capture),
    )


@pytest.mark.kora_conformance
def test_artifact_type_rejects_declared_values(
    kora_artifact_type_provider: ArtifactType,
    kora_artifact_type_inputs: ArtifactTypeConformanceInputs,
    kora_capture: Capture,
) -> None:
    check_artifact_type_rejects_declared_values(
        kora_artifact_type_provider,
        kora_artifact_type_inputs,
        context_for(kora_capture),
    )


@pytest.mark.kora_conformance
def test_artifact_type_digest_fields_are_stable(
    kora_artifact_type_provider: ArtifactType,
    kora_artifact_type_inputs: ArtifactTypeConformanceInputs,
    kora_capture: Capture,
) -> None:
    check_artifact_type_digest_fields_are_stable(
        kora_artifact_type_provider,
        kora_artifact_type_inputs,
        context_for(kora_capture),
    )


@pytest.mark.kora_conformance
def test_artifact_type_summaries_are_detached(
    kora_artifact_type_provider: ArtifactType,
    kora_artifact_type_inputs: ArtifactTypeConformanceInputs,
    kora_capture: Capture,
) -> None:
    check_artifact_type_summaries_are_detached(
        kora_artifact_type_provider,
        kora_artifact_type_inputs,
        context_for(kora_capture),
    )


@pytest.mark.kora_conformance
def test_artifact_type_invalid_extraction_is_refused(
    kora_artifact_type_provider: ArtifactType,
    kora_artifact_type_inputs: ArtifactTypeConformanceInputs,
    kora_capture: Capture,
) -> None:
    check_artifact_type_invalid_extraction_is_refused(
        kora_artifact_type_provider,
        kora_artifact_type_inputs,
        context_for(kora_capture),
    )


@pytest.mark.kora_conformance
def test_artifact_type_configuration_is_effective(
    kora_artifact_type_provider: ArtifactType,
    kora_artifact_type_inputs: ArtifactTypeConformanceInputs,
    kora_artifact_type_configuration: JsonObject,
    kora_capture: Capture,
) -> None:
    asyncio.run(
        awaited(
            kora_artifact_type_inputs.configuration_effect(
                kora_artifact_type_provider,
                kora_artifact_type_configuration,
                context_for(kora_capture),
            )
        )
    )
