import asyncio
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import context_for
from kora.conformance.storage_support import operation, seal_repeated, single_part
from kora.plugins.storage.local import LocalStorageConfiguration
from kora.protocols.storage_models import (
    ContentPreparation,
    PartRef,
    PreparationArguments,
    PreparedContentRef,
    StorageLimits,
)
from tests.measurements.storage_lifetime_workload import InterruptedSource
from tests.plugins.storage.inspection import InspectedStorage


@pytest.mark.parametrize("kind", ["write_part", "prepare_content"])
def test_artifact_storage_replay_validates_supplied_bytes_on_cancel(
    tmp_path: Path, kora_capture: Capture, kind: str
) -> None:
    async def scenario() -> None:
        configuration = LocalStorageConfiguration(
            root=str(tmp_path / "store"), limits=StorageLimits(version=1)
        )
        provider = InspectedStorage(configuration)
        context = context_for(kora_capture)
        try:
            upload = await seal_repeated(provider, "replay", 65536, context)
            preparation = PreparationArguments(
                version=1,
                source=upload.content,
                maximum_output_size=65536,
                expected_output_digest=upload.content.payload_digest,
            )
            request = ContentPreparation(
                version=1,
                operation=operation(
                    provider.identity(), "prepare", "prepare_content", preparation
                ),
                arguments=preparation,
            )
            from kora.conformance.storage_support import RepeatedSource

            if kind == "prepare_content":
                await provider.prepare_content(request, RepeatedSource(65536), context)
                selected = request.operation
            else:
                selected = upload.operations[1]
            before = await provider.observe(selected, context)
            directory = tmp_path / "store" / "stages" / "0"
            inodes = {
                path.name: (path.stat().st_ino, path.stat().st_size)
                for path in directory.iterdir()
            }
            inventory = provider.resource_counts()
            source = InterruptedSource(65536, True, lambda: None)

            async def replay() -> PartRef | PreparedContentRef:
                if kind == "prepare_content":
                    return await provider.prepare_content(request, source, context)
                return await provider.write_part(
                    selected,
                    upload.content.stage,
                    single_part(b"k" * 65536),
                    source,
                    context,
                )

            pending = asyncio.create_task(replay())
            try:
                await asyncio.wait_for(source.entered.wait(), 5)
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
            finally:
                source.released.set()
                await provider.aclose()
            recovered = InspectedStorage(configuration)
            try:
                after = await recovered.observe(selected, context)
                settled = recovered.resource_counts()
                actual = {
                    path.name: (path.stat().st_ino, path.stat().st_size)
                    for path in directory.iterdir()
                }
            finally:
                await recovered.aclose()
        finally:
            await provider.aclose()

        assert source.closed
        assert source.offset == 65536
        assert before == after
        assert inodes == actual
        assert inventory == settled
        provider.assert_closed()
        recovered.assert_closed()

    asyncio.run(scenario())
