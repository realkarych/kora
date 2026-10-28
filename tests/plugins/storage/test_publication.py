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
    content_address,
    delete_release,
    delete_request,
    digest,
    preparation_request,
    promotion_request,
    seal_bytes,
    seal_repeated,
)
from kora.plugins.storage.local import LocalArtifactStorage
from kora.protocols.errors import KoraProtocolError
from kora.protocols.storage_models import PreparedContentRef
from tests.plugins.storage.activity import StorageBarrierActivity
from tests.plugins.storage.configuration import local_configuration


def test_interrupted_identical_preparation_stays_within_reserved_peak(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async def scenario() -> None:
        configuration = local_configuration(tmp_path / "store")
        provider = LocalArtifactStorage(configuration)
        context = context_for(kora_capture)
        size = 65536
        upload = await seal_repeated(provider, "interrupted-alias", size, context)
        raw = upload.content
        request = preparation_request(
            provider.identity(), "prepare", raw, size, raw.payload_digest
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
            promotion = promotion_request(
                recovered.identity(),
                "promote",
                candidate,
                content_address(recovered.identity(), raw.payload_digest),
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
        provider = LocalArtifactStorage(local_configuration(tmp_path / "store"))
        context = context_for(kora_capture)
        upload = await seal_repeated(provider, "alias", size, context)
        raw = upload.content
        stage_directory = tmp_path / "store" / "stages" / "0"
        before = {path.name: path.stat().st_ino for path in stage_directory.iterdir()}
        request = preparation_request(
            provider.identity(), "prepare", raw, size, raw.payload_digest
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
            promotion = promotion_request(
                provider.identity(),
                "promote",
                candidate,
                content_address(provider.identity(), raw.payload_digest),
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
        provider = LocalArtifactStorage(local_configuration(tmp_path / "store"))
        context = context_for(kora_capture)
        output = b"validated-prepared-output" * 8192
        sealed, _, _ = await seal_bytes(provider, "stage", b"raw-source", context)
        prepare = preparation_request(
            provider.identity(), "prepare", sealed, len(output), digest(output)
        )
        source = BytesSource(output)
        address = content_address(provider.identity(), digest(output))

        try:
            candidate = await provider.prepare_content(prepare, source, context)
            replayed = await provider.prepare_content(
                prepare, BytesSource(output), context
            )
            with pytest.raises(KoraProtocolError) as changed:
                await provider.prepare_content(
                    prepare, BytesSource(b"changed"), context
                )
            result = await provider.promote(
                promotion_request(provider.identity(), "promote", candidate, address),
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
        provider = LocalArtifactStorage(local_configuration(tmp_path / "store"))
        context = context_for(kora_capture)
        sealed, _, _ = await seal_bytes(provider, "stage", b"deleted-content", context)
        address = content_address(provider.identity(), sealed.payload_digest)
        published = await provider.promote(
            promotion_request(provider.identity(), "promote", sealed, address), context
        )
        deletion = delete_request(provider.identity(), "delete", address)

        try:
            held = await provider.get_range(published.content, 0, 1, context)
            with pytest.raises(KoraProtocolError) as busy:
                await provider.delete_content(deletion, context)
            await held.aclose()
            await provider.delete_content(deletion, context)
            absent = await provider.exists(address, context)
            await provider.release_delete(
                delete_release(
                    deletion.operation,
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
