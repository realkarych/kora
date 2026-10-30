from collections.abc import Callable
from functools import partial

import pytest

from kora.conformance.artifact_type import (
    ArtifactTypeConformanceCase,
    ArtifactTypeConformanceInputs,
)
from kora.conformance.bridge import ConstructorArguments, PreparedConformance
from kora.ir.diagnostics import ContractViolation
from kora.ir.json import JsonObject, JsonValue
from kora.ir.schema_catalogue import builtin_schema
from kora.plugins.artifact_type.structured_output import StructuredOutputArtifactType
from kora.protocols.artifact_type import ArtifactType, BuiltinArtifactTypeBinding
from kora.protocols.common import ProtocolContext
from kora.protocols.lifetime import ConformanceLifetime


def binding() -> BuiltinArtifactTypeBinding:
    schema = builtin_schema("StructuredOutput", "1")
    assert schema is not None
    descriptor: JsonObject = {
        "name": "StructuredOutput",
        "builtin_revision": "1",
        "schema": "urn:kora:artifact:StructuredOutput",
        "parent": None,
        "behavior": {
            "validation": "kora.structured-output-validation.v1",
            "digest_fields": "kora.json-payload.v1",
            "summary": "kora.json-payload.v1",
        },
        "validation": {
            "max_raw_json_bytes": 1048576,
            "max_canonical_json_bytes": 1048576,
            "max_json_depth": 16,
            "max_integer_bits": 14000,
        },
        "canonicalization": "kora.structured-json.v1",
        "compatibility": {
            "execution_semantics": "kora.execution.v1",
            "checker_view": "kora.structured-output-view.v1",
        },
        "digest_selection": [""],
        "schema_sha256": (
            "e924b9a44c0825f0b2a43b0291d1f98c4851425a50e291914dd85ba096ac6eae"
        ),
    }
    return BuiltinArtifactTypeBinding(
        "StructuredOutput", dict(schema), None, descriptor
    )


def payload() -> JsonValue:
    return {
        "private-structured-output-key-marker": [
            "private-structured-output-payload-marker",
            1,
            1.0,
            -0.0,
            None,
            "\0",
        ],
        "a": True,
    }


def array_payload() -> JsonValue:
    return [payload(), None, 1.0, -0.0]


def string_payload() -> JsonValue:
    return "private-structured-output-payload-marker\0"


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


def equivalent(build: Callable[[], JsonValue]) -> JsonValue:
    value = build()
    return dict(reversed(value.items())) if isinstance(value, dict) else value


def rejected(marker: str) -> JsonValue:
    return {marker: float("inf")}


async def failure(
    provider: ArtifactType, config: JsonObject, context: ProtocolContext, marker: str
) -> object:
    assert config == {}
    assert type(provider) is StructuredOutputArtifactType
    return provider.summary(rejected(marker), context)


async def effect(
    provider: ArtifactType, config: JsonObject, context: ProtocolContext
) -> None:
    assert config == {}
    assert type(provider) is StructuredOutputArtifactType
    assert provider.name == "StructuredOutput"
    assert provider.schema == binding().schema
    assert provider.validate(payload(), context) == []
    assert provider.summary(None, context) is None


class Adapter:
    exercises: ArtifactTypeConformanceInputs = ArtifactTypeConformanceInputs(
        tuple(
            ArtifactTypeConformanceCase(build, partial(equivalent, build), ("",), build)
            for build in (
                payload,
                array_payload,
                string_payload,
                integer_payload,
                float_payload,
                negative_zero_payload,
                boolean_payload,
                null_payload,
            )
        ),
        rejected,
        (
            ContractViolation(
                code="SO003",
                message="StructuredOutput value is outside its JSON domain.",
                path=(),
            ),
        ),
        failure,
        effect,
    )

    async def prepare(
        self,
        config: JsonObject,
        fixture_data: JsonObject,
        lifetime: ConformanceLifetime,
    ) -> PreparedConformance:
        return self

    def bind_configuration(self, config: JsonObject) -> ConstructorArguments:
        return ConstructorArguments(args=(config, binding()))

    async def cleanup(self, instance: object | None) -> None:
        assert instance is None or type(instance) is StructuredOutputArtifactType


@pytest.fixture
def kora_conformance_adapter() -> Adapter:
    return Adapter()
