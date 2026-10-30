import pytest

from kora.conformance.artifact_type import (
    ArtifactTypeConformanceCase,
    ArtifactTypeConformanceInputs,
)
from kora.conformance.bridge import ConstructorArguments, PreparedConformance
from kora.ir.diagnostics import ContractViolation
from kora.ir.json import JsonObject, JsonValue
from kora.ir.schema_catalogue import builtin_schema
from kora.plugins.artifact_type.agent_session import AgentSessionArtifactType
from kora.protocols.artifact_type import ArtifactType, BuiltinArtifactTypeBinding
from kora.protocols.common import ProtocolContext
from kora.protocols.lifetime import ConformanceLifetime


def binding() -> BuiltinArtifactTypeBinding:
    schema = builtin_schema("AgentSession", "1")
    assert schema is not None
    descriptor: JsonObject = {
        "name": "AgentSession",
        "builtin_revision": "1",
        "schema": "urn:kora:artifact:AgentSession",
        "parent": None,
        "representation": "kora.agent-session-transcript-primary.v1",
        "behavior": {
            "validation": "kora.agent-session-validation.v1",
            "digest_fields": "kora.agent-session-logical.v1",
            "summary": "kora.agent-session-summary.v1",
        },
        "canonicalization": "kora.structured-json.v1",
        "validation": {
            "max_raw_logical_json_bytes": 4194304,
            "max_canonical_logical_json_bytes": 4194304,
            "max_json_depth": 16,
        },
        "digest_selection": [""],
        "logical_identity": "kora.agent-session-logical.v1",
        "compatibility": {
            "execution_semantics": "kora.execution.v1",
            "checker_view": "kora.agent-session-view.v1",
        },
        "schema_sha256": (
            "d7a63554c3c50b9237a118f788ffd4a0f0af262bc192c2301db568f7c902e55a"
        ),
        "cel_policy_sha256": (
            "8522225273b2696446b698c3caaab6b9a382a3f8a842c3277da18a1199d7ff30"
        ),
    }
    return BuiltinArtifactTypeBinding("AgentSession", dict(schema), None, descriptor)


def expected_summary() -> JsonObject:
    return {
        "harness": "claude",
        "installed_version": "installed-example",
        "session_id": "session-example",
        "billing_class": "api_key",
        "turns": 1,
        "duration_ms": 1500,
        "stop_reason": "success",
        "provenance": {"worker_id": "worker-example", "image_digest": None},
        "usage": [
            {
                "model": "example-model",
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "cache_creation_tokens": 0,
                "output_tokens": 0,
                "reasoning_tokens": 0,
                "known_token_fields": [
                    "cache_creation_tokens",
                    "cached_input_tokens",
                    "input_tokens",
                    "output_tokens",
                    "reasoning_tokens",
                ],
                "cache_creation_by_ttl": {"5m": 0, "1h": 0},
                "completeness": "complete",
                "basis": "price_table",
                "billing_class": "api_key",
                "delta_of_session_totals": False,
                "usd_estimate": "0",
            }
        ],
        "usage_coverage": {
            "completeness": "complete",
            "attribution_gap": False,
            "observation_gap": False,
        },
    }


def payload() -> JsonObject:
    metadata = expected_summary()
    usage = metadata["usage"]
    assert isinstance(usage, list)
    row = usage[0]
    assert isinstance(row, dict)
    row.update(
        {
            "reported_usd": None,
            "observation_id": "private-agent-session-usage-marker",
            "observation_sequence": 2,
            "observed_at": "2026-10-29T12:00:00Z",
        }
    )
    coverage = metadata["usage_coverage"]
    assert isinstance(coverage, dict)
    coverage.update(
        {
            "version": 1,
            "observation_id": "private-agent-session-coverage-marker",
            "observation_sequence": 2,
            "observed_at": "2026-10-29T12:00:00Z",
        }
    )
    metadata["price_table_id"] = "private-agent-session-price-marker"
    return {
        "metadata": metadata,
        "denials": {
            "items": [
                {
                    "tool": "private-agent-session-denial-marker",
                    "tool_use_id": "call-example",
                    "source": "policy",
                    "pattern": "namespace.deny.0",
                    "code": "denied",
                }
            ],
            "total": 2,
            "complete": False,
        },
        "transcript": {
            "capture": "captured",
            "source_size": 1,
            "source_sha256": "0" * 64,
            "source_verified": True,
            "retained_size": 1,
            "retained_sha256": "0" * 64,
            "truncated": False,
        },
    }


def equivalent() -> JsonObject:
    return dict(reversed(list(payload().items())))


def rejected(marker: str) -> JsonValue:
    return {"metadata": marker}


async def failure(
    provider: ArtifactType, config: JsonObject, context: ProtocolContext, marker: str
) -> object:
    assert config == {}
    assert type(provider) is AgentSessionArtifactType
    return provider.summary(rejected(marker), context)


async def effect(
    provider: ArtifactType, config: JsonObject, context: ProtocolContext
) -> None:
    assert config == {}
    assert type(provider) is AgentSessionArtifactType
    assert provider.name == "AgentSession"
    assert provider.schema == binding().schema
    assert provider.validate(payload(), context) == []
    assert provider.summary(payload(), context) == expected_summary()
    assert expected_summary() != payload()


class Adapter:
    exercises: ArtifactTypeConformanceInputs = ArtifactTypeConformanceInputs(
        (ArtifactTypeConformanceCase(payload, equivalent, ("",), expected_summary),),
        rejected,
        (
            ContractViolation(
                code="AS003",
                message="AgentSession value is outside its revision domain.",
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
        assert instance is None or type(instance) is AgentSessionArtifactType


@pytest.fixture
def kora_conformance_adapter() -> Adapter:
    return Adapter()
