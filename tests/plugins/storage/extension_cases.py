from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from kora.conformance.artifact_storage import (
    StorageFactory,
    kora_capture,
    kora_exercise,
    kora_provider,
    test_artifact_storage_deletion_lifetime,
    test_artifact_storage_inventory_is_bounded,
    test_artifact_storage_promotion_is_create_only,
    test_artifact_storage_promotion_replay_preserves_outcome,
    test_artifact_storage_range_lifetime,
    test_artifact_storage_range_reads_are_exact,
    test_artifact_storage_staging_cleanup_is_confined,
    test_artifact_storage_staging_lifetime,
    test_artifact_storage_staging_round_trip,
    test_capabilities_are_frozen_and_consistent,
    test_config_schema_is_an_object_schema,
    test_failure_is_a_bounded_protocol_error,
    test_provider_logs_through_its_context,
)
from kora.conformance.storage_support import operation, stage_spec
from kora.ir.json import JsonObject
from kora.protocols.common import ProtocolContext
from kora.protocols.storage import ArtifactStorage

__all__ = [
    "kora_capture",
    "kora_exercise",
    "kora_provider",
    "test_artifact_storage_deletion_lifetime",
    "test_artifact_storage_inventory_is_bounded",
    "test_artifact_storage_promotion_is_create_only",
    "test_artifact_storage_promotion_replay_preserves_outcome",
    "test_artifact_storage_range_lifetime",
    "test_artifact_storage_range_reads_are_exact",
    "test_artifact_storage_staging_cleanup_is_confined",
    "test_artifact_storage_staging_lifetime",
    "test_artifact_storage_staging_round_trip",
    "test_capabilities_are_frozen_and_consistent",
    "test_config_schema_is_an_object_schema",
    "test_failure_is_a_bounded_protocol_error",
    "test_provider_logs_through_its_context",
]


@pytest.fixture
def kora_artifact_storage_factory(
    tmp_path: Path, storage_extension_factory: Callable[[JsonObject], ArtifactStorage]
) -> StorageFactory:
    configuration: JsonObject = {
        "root": str(tmp_path / "store"),
        "limits": {"version": 1},
    }
    return lambda: storage_extension_factory(configuration)


@pytest.fixture
def kora_artifact_storage_exercise(
    kora_provider: ArtifactStorage,
) -> Callable[[ProtocolContext, str], Awaitable[object]]:
    async def exercise(context: ProtocolContext, marker: str) -> object:
        spec = stage_spec(kora_provider.identity(), marker, b"")
        request = operation(
            kora_provider.identity(), "wrong-binding", "begin_stage", spec
        )
        changed = spec.model_copy(update={"content_type": "changed"})
        return await kora_provider.begin_stage(request, changed, context)

    return exercise
