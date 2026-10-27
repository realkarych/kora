import asyncio
import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import context_for
from kora.conformance.storage_support import (
    BytesSource,
    RepeatedSource,
    digest,
    operation,
    seal_bytes,
    seal_repeated,
)
from kora.plugins.storage.local import LocalArtifactStorage, LocalStorageConfiguration
from kora.protocols.errors import KoraProtocolError
from kora.protocols.storage_models import (
    ContentAddress,
    ContentPreparation,
    DeleteArguments,
    DeleteReleaseRequest,
    DeleteRequest,
    PreparationArguments,
    PreparedContentRef,
    PromotionArguments,
    PromotionRequest,
    StorageLimits,
)
from tests.plugins.storage.activity import StorageBarrierActivity


def test_interrupted_identical_preparation_stays_within_reserved_peak(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async def scenario() -> None:
        configuration = LocalStorageConfiguration(
            root=str(tmp_path / "store"), limits=StorageLimits(version=1)
        )
        provider = LocalArtifactStorage(configuration)
        context = context_for(kora_capture)
        size = 65536
        upload = await seal_repeated(provider, "interrupted-alias", size, context)
        raw = upload.content
        arguments = PreparationArguments(
            version=1,
            source=raw,
            maximum_output_size=size,
            expected_output_digest=raw.payload_digest,
        )
        request = ContentPreparation(
            version=1,
            operation=operation(
                provider.identity(), "prepare", "prepare_content", arguments
            ),
            arguments=arguments,
        )
        barrier = StorageBarrierActivity(
            "storage.prepare_content.effect_persisted", on_exit=True
        )
        source = RepeatedSource(size)
        prepared = asyncio.create_task(
            provider.prepare_content(
                request, source, replace(context, activity=barrier)
            )
        )

        await asyncio.wait_for(barrier.entered.wait(), 5)
        prepared.cancel()
        with pytest.raises(asyncio.CancelledError):
            await prepared
        await provider.aclose()
        recovered = LocalArtifactStorage(configuration)
        try:
            observation = await recovered.observe(request.operation, context)
            assert isinstance(observation.result, PreparedContentRef)
            candidate = await recovered.prepare_content(
                request, RepeatedSource(size), context
            )
            promotion_arguments = PromotionArguments(
                version=1,
                content=candidate,
                address=ContentAddress(
                    version=1,
                    storage_identity=recovered.identity(),
                    payload_digest=raw.payload_digest,
                ),
                content_generation=1,
            )
            promotion = PromotionRequest(
                version=1,
                operation=operation(
                    recovered.identity(), "promote", "promote", promotion_arguments
                ),
                arguments=promotion_arguments,
            )
            published = await recovered.promote(promotion, context)
            private_files = tuple((tmp_path / "store" / "stages" / "0").iterdir())
        finally:
            await recovered.aclose()

        assert source.closed
        assert published.created_object
        assert sum(path.stat().st_size for path in private_files) <= 2 * size
        assert "prepare" not in {path.name for path in private_files}

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [0, 1, 65536, 16777217])
def test_identical_preparation_aliases_raw_parts(
    tmp_path: Path, kora_capture: Capture, size: int
) -> None:
    async def scenario() -> None:
        provider = LocalArtifactStorage(
            LocalStorageConfiguration(
                root=str(tmp_path / "store"), limits=StorageLimits(version=1)
            )
        )
        context = context_for(kora_capture)
        upload = await seal_repeated(provider, "alias", size, context)
        raw = upload.content
        stage_directory = tmp_path / "store" / "stages" / "0"
        before = {path.name: path.stat().st_ino for path in stage_directory.iterdir()}
        arguments = PreparationArguments(
            version=1,
            source=raw,
            maximum_output_size=size,
            expected_output_digest=raw.payload_digest,
        )
        request = ContentPreparation(
            version=1,
            operation=operation(
                provider.identity(), "prepare", "prepare_content", arguments
            ),
            arguments=arguments,
        )

        try:
            candidate = await provider.prepare_content(
                request, RepeatedSource(size), context
            )
            after = {
                path.name: path.stat().st_ino for path in stage_directory.iterdir()
            }
            reader = await provider.read_stage(candidate, context)
            observed = hashlib.sha256()
            async with reader:
                async for chunk in reader:
                    observed.update(chunk)
            promotion_arguments = PromotionArguments(
                version=1,
                content=candidate,
                address=ContentAddress(
                    version=1,
                    storage_identity=provider.identity(),
                    payload_digest=raw.payload_digest,
                ),
                content_generation=1,
            )
            promotion = PromotionRequest(
                version=1,
                operation=operation(
                    provider.identity(), "promote", "promote", promotion_arguments
                ),
                arguments=promotion_arguments,
            )
            result = await provider.promote(promotion, context)
        finally:
            await provider.aclose()

        assert after == before
        assert "sha256:" + observed.hexdigest() == raw.payload_digest
        assert candidate.total_size == size
        assert result.created_object
        assert result.content.total_size == size

    asyncio.run(scenario())


def test_prepared_candidate_replays_exactly_and_refuses_changed_bytes(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async def scenario() -> None:
        provider = LocalArtifactStorage(
            LocalStorageConfiguration(
                root=str(tmp_path / "store"), limits=StorageLimits(version=1)
            )
        )
        context = context_for(kora_capture)
        output = b"validated-prepared-output" * 8192
        sealed, _, _ = await seal_bytes(provider, "stage", b"raw-source", context)
        preparation = PreparationArguments(
            version=1,
            source=sealed,
            maximum_output_size=len(output),
            expected_output_digest=digest(output),
        )
        prepare = ContentPreparation(
            version=1,
            operation=operation(
                provider.identity(), "prepare", "prepare_content", preparation
            ),
            arguments=preparation,
        )
        source = BytesSource(output)
        address = ContentAddress(
            version=1,
            storage_identity=provider.identity(),
            payload_digest=digest(output),
        )

        try:
            candidate = await provider.prepare_content(prepare, source, context)
            replayed = await provider.prepare_content(
                prepare, BytesSource(output), context
            )
            with pytest.raises(KoraProtocolError) as changed:
                await provider.prepare_content(
                    prepare, BytesSource(b"changed"), context
                )
            arguments = PromotionArguments(
                version=1, content=candidate, address=address, content_generation=1
            )
            result = await provider.promote(
                PromotionRequest(
                    version=1,
                    operation=operation(
                        provider.identity(), "promote", "promote", arguments
                    ),
                    arguments=arguments,
                ),
                context,
            )
            reader = await provider.get(result.content, context)
            async with reader:
                reread = b"".join([chunk async for chunk in reader])
        finally:
            await provider.aclose()

        assert source.closed
        assert candidate == replayed
        assert changed.value.code == "storage_conflict"
        assert candidate.total_size == len(output)
        assert result.created_object
        assert result.content.address == address
        assert reread == output

    asyncio.run(scenario())


def test_content_deletion_refuses_held_readers_prunes_shards_and_compacts_evidence(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async def scenario() -> None:
        provider = LocalArtifactStorage(
            LocalStorageConfiguration(
                root=str(tmp_path / "store"), limits=StorageLimits(version=1)
            )
        )
        context = context_for(kora_capture)
        sealed, _, _ = await seal_bytes(provider, "stage", b"deleted-content", context)
        address = ContentAddress(
            version=1,
            storage_identity=provider.identity(),
            payload_digest=sealed.payload_digest,
        )
        promotion = PromotionArguments(
            version=1, content=sealed, address=address, content_generation=1
        )
        published = await provider.promote(
            PromotionRequest(
                version=1,
                operation=operation(
                    provider.identity(), "promote", "promote", promotion
                ),
                arguments=promotion,
            ),
            context,
        )
        arguments = DeleteArguments(version=1, address=address, deletion_generation=1)
        deletion = DeleteRequest(
            version=1,
            operation=operation(
                provider.identity(), "delete", "delete_content", arguments
            ),
            arguments=arguments,
        )

        try:
            held = await provider.get_range(published.content, 0, 1, context)
            with pytest.raises(KoraProtocolError) as busy:
                await provider.delete_content(deletion, context)
            await held.aclose()
            await provider.delete_content(deletion, context)
            absent = await provider.exists(address, context)
            await provider.release_delete(
                DeleteReleaseRequest(
                    version=1,
                    operation=deletion.operation,
                    persisted_outcome_revision="delete-outcome",
                    issuance_closed="delete-issuers-closed",
                ),
                context,
            )
            unavailable = await provider.observe(deletion.operation, context)
        finally:
            await provider.aclose()

        assert busy.value.code == "storage_busy"
        assert absent is None
        assert not tuple((tmp_path / "store" / "artifacts" / "sha256").iterdir())
        assert unavailable.state == "evidence_unavailable"

    asyncio.run(scenario())
