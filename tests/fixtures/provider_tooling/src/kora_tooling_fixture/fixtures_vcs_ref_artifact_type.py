import pytest

from kora.conformance.artifact_type import (
    ArtifactTypeConformanceCase,
    ArtifactTypeConformanceInputs,
)
from kora.conformance.bridge import ConstructorArguments, PreparedConformance
from kora.ir.diagnostics import ContractViolation
from kora.ir.json import JsonObject, JsonValue
from kora.ir.schema_catalogue import builtin_schema
from kora.plugins.artifact_type.vcs_ref import VcsRefArtifactType
from kora.protocols.artifact_type import ArtifactType, BuiltinArtifactTypeBinding
from kora.protocols.common import ProtocolContext
from kora.protocols.lifetime import ConformanceLifetime


def binding() -> BuiltinArtifactTypeBinding:
    schema = builtin_schema("VcsRef", "1")
    assert schema is not None
    descriptor: JsonObject = {
        "name": "VcsRef",
        "builtin_revision": "1",
        "schema": "urn:kora:artifact:VcsRef",
        "parent": None,
        "behavior": {
            "validation": "kora.vcs-ref-validation.v1",
            "digest_fields": "kora.json-payload.v1",
            "summary": "kora.json-payload.v1",
        },
        "validation": {
            "max_raw_json_bytes": 1048576,
            "max_canonical_json_bytes": 1048576,
            "max_changed_paths": 10000,
            "max_changed_path_utf8_bytes": 524288,
            "max_revision_characters": 255,
            "max_principal_characters": 255,
            "max_json_depth": 16,
        },
        "digest_selection": [""],
        "schema_sha256": (
            "6cb29467d126b9a5f324408568d6925795946dde67ba927244a844f3169a86a8"
        ),
    }
    return BuiltinArtifactTypeBinding("VcsRef", dict(schema), None, descriptor)


def payload() -> JsonValue:
    return {
        "repo": "private-repository-marker",
        "branch": "private-branch-marker",
        "commit": "revision:2",
        "base_commit": "revision:1",
        "changed_paths": ["src/private-path-marker"],
        "diff_stat": "private-diff-marker",
        "pushed_by": {
            "provider_instance": "fixture",
            "stable_id": "publisher",
            "kind": "service_user",
            "display_name": "private-principal-marker",
        },
    }


def equivalent() -> JsonValue:
    value = payload()
    assert isinstance(value, dict)
    return dict(reversed(list(value.items())))


def rejected(marker: str) -> JsonValue:
    return {"repo": marker}


async def failure(
    provider: ArtifactType, config: JsonObject, context: ProtocolContext, marker: str
) -> object:
    assert config == {}
    return provider.summary(rejected(marker), context)


async def effect(
    provider: ArtifactType, config: JsonObject, context: ProtocolContext
) -> None:
    assert config == {}
    assert provider.name == "VcsRef"
    assert provider.schema == binding().schema
    assert provider.validate(payload(), context) == []
    assert provider.summary(payload(), context) == payload()


class Adapter:
    exercises: ArtifactTypeConformanceInputs = ArtifactTypeConformanceInputs(
        (ArtifactTypeConformanceCase(payload, equivalent, ("",), payload),),
        rejected,
        (
            ContractViolation(
                code="VCS003",
                message="VcsRef payload shape is invalid.",
                path=("branch",),
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
        assert instance is None or isinstance(instance, VcsRefArtifactType)


@pytest.fixture
def kora_conformance_adapter() -> Adapter:
    return Adapter()
