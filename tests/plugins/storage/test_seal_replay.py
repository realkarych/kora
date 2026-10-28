import asyncio
from dataclasses import replace
from pathlib import Path

from kora.conformance.capture import Capture
from kora.conformance.common import context_for
from kora.conformance.storage_support import operation, write_single_part
from kora.plugins.storage.local import LocalArtifactStorage
from tests.plugins.storage.activity import StorageBarrierActivity
from tests.plugins.storage.configuration import local_configuration


def test_exact_seal_contender_returns_original_after_stage_closes(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async def scenario() -> None:
        provider = LocalArtifactStorage(local_configuration(tmp_path / "store"))
        context = context_for(kora_capture)
        stage, _, manifest = await write_single_part(
            provider, "stage", b"contender", context, begin_id="begin", part_id="part"
        )
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
