import base64
import json
from hashlib import sha256

import pytest

from kora.conformance.artifact_type import (
    ArtifactTypeConformanceCase,
    ArtifactTypeConformanceInputs,
)
from kora.conformance.bridge import ConstructorArguments, PreparedConformance
from kora.ir.diagnostics import ContractViolation
from kora.ir.json import JsonObject, JsonValue
from kora.ir.schema_catalogue import builtin_schema
from kora.plugins.artifact_type.command_result import CommandResultArtifactType
from kora.protocols.artifact_type import ArtifactType, BuiltinArtifactTypeBinding
from kora.protocols.common import ProtocolContext
from kora.protocols.lifetime import ConformanceLifetime


def binding() -> BuiltinArtifactTypeBinding:
    schema = builtin_schema("CommandResult", "1")
    assert schema is not None
    descriptor: JsonObject = {
        "name": "CommandResult",
        "builtin_revision": "1",
        "schema": "urn:kora:artifact:CommandResult",
        "parent": None,
        "behavior": {
            "validation": "kora.command-result-validation.v1",
            "digest_fields": "kora.json-payload.v1",
            "summary": "kora.json-payload.v1",
        },
        "validation": {
            "max_raw_json_bytes": 1048576,
            "max_canonical_json_bytes": 1048576,
            "max_json_depth": 16,
            "max_count": 9223372036854775807,
            "max_capture_bytes": 67108864,
            "max_tail_bytes": 4096,
            "max_resources": 64,
            "max_resource_bytes": 262144,
            "max_resource_name_utf8_bytes": 4096,
            "max_resource_component_utf8_bytes": 255,
            "max_protected_paths": 64,
            "max_protected_path_characters": 4096,
            "max_protected_path_segments": 32,
            "max_protected_path_pieces": 8192,
            "max_protected_path_utf8_bytes": 65536,
            "max_protected_path_non_ascii_characters": 16384,
            "max_repo_utf8_bytes": 4096,
            "max_revision_characters": 255,
        },
        "canonicalization": "kora.command-result-json.v1",
        "digest_selection": [""],
        "schema_sha256": (
            "8444eeb4d4688b6d059dd7221689dd4c824084c88ff189ee3fbb848153c1f8d4"
        ),
    }
    return BuiltinArtifactTypeBinding("CommandResult", dict(schema), None, descriptor)


def payload() -> JsonValue:
    marker = "private-command-result-marker"
    resources: list[JsonValue] = [
        {"name": marker, "sha256": "sha256:" + "0" * 64, "size": 0, "executable": False}
    ]
    empty: JsonObject = {
        "storage_ref": marker,
        "payload_digest": "sha256:" + sha256(b"").hexdigest(),
        "size": 0,
        "observed_bytes": 0,
        "eof": True,
        "truncated": False,
        "tail_base64": "",
    }
    retained = marker.encode()
    return {
        "exit_code": 0,
        "signal": None,
        "termination": "none",
        "stop_reason": "completed",
        "duration_ms": 1,
        "repo": marker,
        "commit": marker,
        "base_commit": marker,
        "protected_paths": [marker],
        "protection": "verified",
        "resources": resources,
        "resource_set_digest": "sha256:"
        + sha256(
            json.dumps(resources, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "resource_protection": "none",
        "stdout": {
            **empty,
            "payload_digest": "sha256:" + sha256(retained).hexdigest(),
            "size": len(retained),
            "observed_bytes": len(retained),
            "tail_base64": base64.b64encode(retained).decode(),
        },
        "stderr": empty,
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
    assert type(provider) is CommandResultArtifactType
    return provider.summary(rejected(marker), context)


async def effect(
    provider: ArtifactType, config: JsonObject, context: ProtocolContext
) -> None:
    assert config == {}
    assert type(provider) is CommandResultArtifactType
    assert provider.name == "CommandResult"
    assert provider.schema == binding().schema
    assert provider.validate(payload(), context) == []
    assert provider.summary(payload(), context) == payload()


class Adapter:
    exercises: ArtifactTypeConformanceInputs = ArtifactTypeConformanceInputs(
        (ArtifactTypeConformanceCase(payload, equivalent, ("",), payload),),
        rejected,
        (
            ContractViolation(
                code="CR003",
                message="CommandResult value is outside its revision domain.",
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
        assert instance is None or type(instance) is CommandResultArtifactType


@pytest.fixture
def kora_conformance_adapter() -> Adapter:
    return Adapter()
