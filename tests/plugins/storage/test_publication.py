import asyncio
import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import context_for
from kora.conformance.storage_support import RepeatedSource, seal_repeated
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
from tests.plugins.storage.support import BytesSource, digest, operation, seal_bytes


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


def test_prepared_publication_ranges_and_delete_replay(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async def scenario() -> None:
        configuration = LocalStorageConfiguration(
            root=str(tmp_path / "store"), limits=StorageLimits(version=1)
        )
        provider = LocalArtifactStorage(configuration)
        context = context_for(kora_capture)
        raw = b"raw-source"
        output = b"validated-prepared-output" * 8192
        sealed, _, _ = await seal_bytes(provider, "stage", raw, context)
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
            with pytest.raises(KoraProtocolError, match="storage_conflict"):
                await provider.prepare_content(
                    prepare, BytesSource(b"changed"), context
                )
            arguments = PromotionArguments(
                version=1, content=candidate, address=address, content_generation=1
            )
            promotion = PromotionRequest(
                version=1,
                operation=operation(
                    provider.identity(), "promote", "promote", arguments
                ),
                arguments=arguments,
            )
            result = await provider.promote(promotion, context)
            reader = await provider.get_range(result.content, 65530, 20, context)
            selected = b"".join([chunk async for chunk in reader])
            await reader.aclose()
            full = await provider.get(result.content, context)
            reread = b"".join([chunk async for chunk in full])
            await full.aclose()
            deletion_arguments = DeleteArguments(
                version=1, address=address, deletion_generation=1
            )
            deletion = DeleteRequest(
                version=1,
                operation=operation(
                    provider.identity(), "delete", "delete_content", deletion_arguments
                ),
                arguments=deletion_arguments,
            )
            held = await provider.get_range(result.content, 0, 1, context)
            with pytest.raises(KoraProtocolError, match="storage_busy"):
                await provider.delete_content(deletion, context)
            await held.aclose()
            deleted = await provider.delete_content(deletion, context)
            original = await provider.promote(promotion, context)
            absent = await provider.exists(address, context)
            delete_replay = await provider.delete_content(deletion, context)
            release = await provider.release_delete(
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

        assert source.closed
        assert candidate == replayed
        assert candidate.total_size == len(output)
        assert result.created_object
        assert result.content.address == address
        assert selected == output[65530:65550]
        assert reread == output
        assert deleted.deleted_object
        assert delete_replay == deleted
        assert original == result
        assert absent is None
        assert not tuple((tmp_path / "store" / "artifacts" / "sha256").iterdir())
        assert release.state == "released"
        assert unavailable.state == "evidence_unavailable"

    asyncio.run(scenario())


def test_create_only_publication_preserves_incarnation_and_shared_bytes(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async def scenario() -> None:
        provider = LocalArtifactStorage(
            LocalStorageConfiguration(
                root=str(tmp_path / "store"), limits=StorageLimits(version=1)
            )
        )
        context = context_for(kora_capture)
        payload = b"shared-content"
        first, _, _ = await seal_bytes(provider, "first", payload, context)
        second, _, _ = await seal_bytes(provider, "second", payload, context)
        address = ContentAddress(
            version=1,
            storage_identity=provider.identity(),
            payload_digest=digest(payload),
        )

        try:
            first_arguments = PromotionArguments(
                version=1, content=first, address=address, content_generation=1
            )
            first_request = PromotionRequest(
                version=1,
                operation=operation(
                    provider.identity(), "first-promote", "promote", first_arguments
                ),
                arguments=first_arguments,
            )
            created = await provider.promote(first_request, context)
            second_arguments = PromotionArguments(
                version=1, content=second, address=address, content_generation=1
            )
            second_request = PromotionRequest(
                version=1,
                operation=operation(
                    provider.identity(), "second-promote", "promote", second_arguments
                ),
                arguments=second_arguments,
            )
            existing = await provider.promote(second_request, context)
            retired = await provider.retire_stage(
                operation(provider.identity(), "retire", "retire_stage", first.stage),
                first.stage,
                context,
            )
            reader = await provider.get(created.content, context)
            content = b"".join([chunk async for chunk in reader])
            await reader.aclose()
        finally:
            await provider.aclose()

        assert created.created_object
        assert not existing.created_object
        assert existing.content == created.content
        assert retired.completed
        assert content == payload

    asyncio.run(scenario())
