import asyncio
from dataclasses import replace
from pathlib import Path

from kora.conformance.capture import Capture
from kora.conformance.common import context_for
from kora.conformance.storage_support import (
    BytesSource,
    operation,
    single_manifest,
    single_part,
    stage_spec,
)
from kora.plugins.storage.local import LocalArtifactStorage, LocalStorageConfiguration
from kora.protocols.storage_models import StageWrite, StorageLimits
from tests.plugins.storage.activity import StorageBarrierActivity


def test_exact_seal_contender_returns_original_after_stage_closes(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async def scenario() -> None:
        provider = LocalArtifactStorage(
            LocalStorageConfiguration(
                root=str(tmp_path / "store"), limits=StorageLimits(version=1)
            )
        )
        context = context_for(kora_capture)
        spec = stage_spec(provider.identity(), "stage", b"contender")
        stage = await provider.begin_stage(
            operation(provider.identity(), "begin", "begin_stage", spec), spec, context
        )
        part_spec = single_part(b"contender")
        part = await provider.write_part(
            operation(
                provider.identity(),
                "part",
                "write_part",
                StageWrite(version=1, stage=stage, spec=part_spec),
            ),
            stage,
            part_spec,
            BytesSource(b"contender"),
            context,
        )
        manifest = single_manifest(stage, part_spec, part.part_identity)
        seal = operation(provider.identity(), "seal", "seal_stage", manifest)
        barrier = StorageBarrierActivity("storage.seal_stage.ownership_pending")
        contender = asyncio.create_task(
            provider.seal_stage(
                seal, stage, manifest, replace(context, activity=barrier)
            )
        )

        try:
            await asyncio.wait_for(barrier.entered.wait(), timeout=5)
            original = await provider.seal_stage(seal, stage, manifest, context)
            barrier.released.set()
            replayed = await contender
        finally:
            barrier.released.set()
            await provider.aclose()

        assert replayed == original

    asyncio.run(scenario())
