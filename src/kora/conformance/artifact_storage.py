import asyncio
import hashlib
from collections.abc import Callable, Generator

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import (
    check,
    context_for,
    kora_capture,
    test_capabilities_are_frozen_and_consistent,
    test_config_schema_is_an_object_schema,
    test_failure_is_a_bounded_protocol_error,
    test_provider_logs_through_its_context,
)
from kora.conformance.storage_support import (
    BytesSource,
    acknowledge_stage,
    digest,
    operation,
    repeated_digest,
    seal_bytes,
    seal_repeated,
    single_manifest,
    single_part,
    stage_spec,
)
from kora.protocols.errors import KoraProtocolError
from kora.protocols.storage import ArtifactStorage
from kora.protocols.storage_models import (
    ContentAddress,
    ContentPreparation,
    DeleteArguments,
    DeleteReleaseRequest,
    DeleteRequest,
    ManifestPrefix,
    PrefixCapture,
    PreparationArguments,
    PreparedContentRef,
    PromotionArguments,
    PromotionRequest,
    PromotionResult,
    SealedStage,
    StageManifest,
    StageWrite,
)

StorageFactory = Callable[[], ArtifactStorage]

__all__ = [
    "kora_capture",
    "kora_exercise",
    "kora_provider",
    "test_capabilities_are_frozen_and_consistent",
    "test_config_schema_is_an_object_schema",
    "test_failure_is_a_bounded_protocol_error",
    "test_provider_logs_through_its_context",
    "test_artifact_storage_staging_round_trip",
    "test_artifact_storage_promotion_is_create_only",
    "test_artifact_storage_promotion_replay_preserves_outcome",
    "test_artifact_storage_staging_cleanup_is_confined",
    "test_artifact_storage_staging_lifetime",
    "test_artifact_storage_deletion_lifetime",
    "test_artifact_storage_inventory_is_bounded",
    "test_artifact_storage_range_reads_are_exact",
    "test_artifact_storage_range_lifetime",
]


@pytest.fixture
def kora_provider(
    kora_artifact_storage_factory: StorageFactory,
) -> Generator[ArtifactStorage]:
    provider = kora_artifact_storage_factory()
    try:
        yield provider
    finally:
        asyncio.run(provider.aclose())


@pytest.fixture
def kora_exercise(kora_artifact_storage_exercise: object) -> object:
    return kora_artifact_storage_exercise


async def publish(
    provider: ArtifactStorage,
    sealed: SealedStage | PreparedContentRef,
    key: str,
    capture: Capture,
) -> tuple[PromotionRequest, PromotionResult]:
    arguments = PromotionArguments(
        version=1,
        content=sealed,
        address=ContentAddress(
            version=1,
            storage_identity=provider.identity(),
            payload_digest=sealed.payload_digest,
        ),
        content_generation=1,
    )
    request = PromotionRequest(
        version=1,
        operation=operation(provider.identity(), key, "promote", arguments),
        arguments=arguments,
    )
    return request, await provider.promote(request, context_for(capture))


@pytest.mark.kora_conformance
@pytest.mark.parametrize("size", [0, 1, 65536, 16777216, 268435456])
def test_artifact_storage_staging_round_trip(
    kora_provider: ArtifactStorage,
    kora_capture: Capture,
    kora_artifact_storage_factory: StorageFactory,
    size: int,
) -> None:
    context = context_for(kora_capture)

    async def scenario() -> None:
        upload = await seal_repeated(kora_provider, "round-trip", size, context)
        reader = await kora_provider.read_stage(upload.content, context)
        actual = hashlib.sha256()
        count = 0
        async with reader:
            async for chunk in reader:
                check(0 < len(chunk) <= 65536, "read chunks obey the streaming bound")
                count += len(chunk)
                actual.update(chunk)

        check(count == size, "immutable reread has the exact size")
        check(
            "sha256:" + actual.hexdigest() == repeated_digest(size),
            "immutable reread has the exact full digest",
        )
        output = f"prepared-{size}".encode()
        preparation = PreparationArguments(
            version=1,
            source=upload.content,
            maximum_output_size=len(output),
            expected_output_digest=digest(output),
        )
        prepared = await kora_provider.prepare_content(
            ContentPreparation(
                version=1,
                operation=operation(
                    kora_provider.identity(), "prepare", "prepare_content", preparation
                ),
                arguments=preparation,
            ),
            BytesSource(output),
            context,
        )
        prefix_payload = b"k" * (min(size, 65536) + 1)
        spec = stage_spec(kora_provider.identity(), "prefix", prefix_payload)
        stage = await kora_provider.begin_stage(
            operation(kora_provider.identity(), "prefix-begin", "begin_stage", spec),
            spec,
            context,
        )
        part = single_part(prefix_payload)
        capture = PrefixCapture(
            version=1, stage=stage, source_spec=part, count=len(prefix_payload) - 1
        )
        source = BytesSource(prefix_payload)
        prefix = await kora_provider.capture_prefix(
            operation(kora_provider.identity(), "capture", "capture_prefix", capture),
            stage,
            part,
            capture.count,
            source,
            context,
        )
        manifest = StageManifest(
            version=1,
            stage=stage,
            parts=(),
            prefix=ManifestPrefix(
                version=1,
                source_spec=part,
                count=prefix.count,
                digest=prefix.digest,
                private_identity=prefix.private_identity,
            ),
        )
        incomplete = await kora_provider.seal_stage(
            operation(kora_provider.identity(), "prefix-seal", "seal_stage", manifest),
            stage,
            manifest,
            context,
        )
        restarted = kora_artifact_storage_factory()
        try:
            prepared_reader = await restarted.read_stage(prepared, context)
            async with prepared_reader:
                prepared_bytes = b"".join([chunk async for chunk in prepared_reader])
            prefix_reader = await restarted.read_stage(incomplete, context)
            async with prefix_reader:
                prefix_bytes = b"".join([chunk async for chunk in prefix_reader])
        finally:
            await restarted.aclose()

        check(
            prepared_bytes == output,
            "a separate prepared candidate survives reconstruction",
        )
        check(
            prefix_bytes == prefix_payload[:-1],
            "prefix capture preserves the exact bounded bytes",
        )
        check(
            not incomplete.source_complete,
            "prefix sealing never claims a complete source",
        )
        check(
            source.closed and source.offset == capture.count,
            "prefix capture closes without consuming the tail",
        )

    asyncio.run(scenario())


@pytest.mark.kora_conformance
def test_artifact_storage_promotion_is_create_only(
    kora_provider: ArtifactStorage, kora_capture: Capture
) -> None:
    context = context_for(kora_capture)

    async def scenario() -> None:
        first, _, _ = await seal_bytes(kora_provider, "first", b"same-bytes", context)
        second, _, _ = await seal_bytes(kora_provider, "second", b"same-bytes", context)
        raw, _, _ = await seal_bytes(kora_provider, "candidate", b"raw-bytes", context)
        preparation = PreparationArguments(
            version=1,
            source=raw,
            maximum_output_size=8,
            expected_output_digest=digest(b"prepared"),
        )
        prepared = await kora_provider.prepare_content(
            ContentPreparation(
                version=1,
                operation=operation(
                    kora_provider.identity(), "prepare", "prepare_content", preparation
                ),
                arguments=preparation,
            ),
            BytesSource(b"prepared"),
            context,
        )
        forged = (
            first.model_copy(
                update={
                    "stage": first.stage.model_copy(
                        update={"stage_id": "foreign-stage"}
                    )
                }
            ),
            first.model_copy(update={"seal_identity": "foreign-seal"}),
            prepared.model_copy(update={"content_identity": "foreign-candidate"}),
            prepared.model_copy(update={"source": first}),
        )
        for index, content in enumerate(forged):
            with pytest.raises(KoraProtocolError):
                await publish(kora_provider, content, f"forged-{index}", kora_capture)
            check(
                not await kora_provider.exists(
                    ContentAddress(
                        version=1,
                        storage_identity=kora_provider.identity(),
                        payload_digest=content.payload_digest,
                    ),
                    context,
                ),
                "a first-call foreign binding cannot publish bytes",
            )
        wrong_destination = ContentAddress(
            version=1,
            storage_identity=kora_provider.identity(),
            payload_digest=digest(b"wrong-destination"),
        )
        wrong_arguments = PromotionArguments(
            version=1, content=prepared, address=wrong_destination, content_generation=1
        )
        with pytest.raises(KoraProtocolError):
            await kora_provider.promote(
                PromotionRequest(
                    version=1,
                    operation=operation(
                        kora_provider.identity(),
                        "wrong-destination",
                        "promote",
                        wrong_arguments,
                    ),
                    arguments=wrong_arguments,
                ),
                context,
            )
        check(
            not await kora_provider.exists(wrong_destination, context),
            "destination mismatch is refused before publication",
        )
        binding_spec = stage_spec(kora_provider.identity(), "binding", b"bound")
        binding = await kora_provider.begin_stage(
            operation(
                kora_provider.identity(), "binding-begin", "begin_stage", binding_spec
            ),
            binding_spec,
            context,
        )
        part_spec = single_part(b"bound")
        part = await kora_provider.write_part(
            operation(
                kora_provider.identity(),
                "binding-part",
                "write_part",
                StageWrite(version=1, stage=binding, spec=part_spec),
            ),
            binding,
            part_spec,
            BytesSource(b"bound"),
            context,
        )
        manifest = single_manifest(binding, part_spec, part.part_identity)
        foreign_part = single_manifest(binding, part_spec, "foreign-part")
        foreign_stage = manifest.model_copy(update={"stage": first.stage})
        for index, invalid in enumerate((foreign_part, foreign_stage)):
            with pytest.raises(KoraProtocolError):
                await kora_provider.seal_stage(
                    operation(
                        kora_provider.identity(),
                        f"invalid-seal-{index}",
                        "seal_stage",
                        invalid,
                    ),
                    binding,
                    invalid,
                    context,
                )
        valid = await kora_provider.seal_stage(
            operation(kora_provider.identity(), "binding-seal", "seal_stage", manifest),
            binding,
            manifest,
            context,
        )
        check(
            valid.payload_digest == binding_spec.payload_digest,
            "refused bindings preserve the valid stage",
        )
        prefix_spec = stage_spec(kora_provider.identity(), "incomplete", b"x")
        prefix_stage = await kora_provider.begin_stage(
            operation(
                kora_provider.identity(), "incomplete-begin", "begin_stage", prefix_spec
            ),
            prefix_spec,
            context,
        )
        source_spec = single_part(b"x")
        capture = PrefixCapture(
            version=1, stage=prefix_stage, source_spec=source_spec, count=0
        )
        prefix = await kora_provider.capture_prefix(
            operation(
                kora_provider.identity(), "incomplete-prefix", "capture_prefix", capture
            ),
            prefix_stage,
            source_spec,
            0,
            BytesSource(b"x"),
            context,
        )
        incomplete_manifest = StageManifest(
            version=1,
            stage=prefix_stage,
            parts=(),
            prefix=ManifestPrefix(
                version=1,
                source_spec=source_spec,
                count=0,
                digest=prefix.digest,
                private_identity=prefix.private_identity,
            ),
        )
        incomplete = await kora_provider.seal_stage(
            operation(
                kora_provider.identity(),
                "incomplete-seal",
                "seal_stage",
                incomplete_manifest,
            ),
            prefix_stage,
            incomplete_manifest,
            context,
        )
        with pytest.raises(KoraProtocolError):
            await publish(kora_provider, incomplete, "incomplete-publish", kora_capture)
        check(
            not await kora_provider.exists(
                ContentAddress(
                    version=1,
                    storage_identity=kora_provider.identity(),
                    payload_digest=incomplete.payload_digest,
                ),
                context,
            ),
            "an incomplete raw source cannot be published",
        )

        _, created = await publish(kora_provider, first, "first-publish", kora_capture)
        _, existing = await publish(
            kora_provider, second, "second-publish", kora_capture
        )
        _, selected = await publish(
            kora_provider, prepared, "prepared-publish", kora_capture
        )
        selected_reader = await kora_provider.get(selected.content, context)
        async with selected_reader:
            selected_bytes = b"".join([chunk async for chunk in selected_reader])
        arguments = PreparationArguments(
            version=1,
            source=first,
            maximum_output_size=8,
            expected_output_digest=digest(b"prepared"),
        )
        request = ContentPreparation(
            version=1,
            operation=operation(
                kora_provider.identity(), "late-prepare", "prepare_content", arguments
            ),
            arguments=arguments,
        )
        with pytest.raises(KoraProtocolError):
            await kora_provider.prepare_content(
                request, BytesSource(b"prepared"), context
            )

        check(created.created_object, "first publication reports creation")
        check(not existing.created_object, "deduplication reports the existing object")
        check(
            existing.content == created.content,
            "deduplication preserves the exact incarnation",
        )
        check(
            selected_bytes == b"prepared", "publication selects the prepared candidate"
        )
        check(
            not await kora_provider.exists(
                ContentAddress(
                    version=1,
                    storage_identity=kora_provider.identity(),
                    payload_digest=raw.payload_digest,
                ),
                context,
            ),
            "prepared publication does not publish raw bytes",
        )

    asyncio.run(scenario())


@pytest.mark.kora_conformance
def test_artifact_storage_promotion_replay_preserves_outcome(
    kora_provider: ArtifactStorage,
    kora_capture: Capture,
    kora_artifact_storage_factory: StorageFactory,
) -> None:
    context = context_for(kora_capture)

    async def scenario() -> None:
        sealed, manifest, _ = await seal_bytes(
            kora_provider, "replay", b"original", context
        )
        request, original = await publish(
            kora_provider, sealed, "publish", kora_capture
        )
        arguments = DeleteArguments(
            version=1, address=original.content.address, deletion_generation=1
        )
        deletion = DeleteRequest(
            version=1,
            operation=operation(
                kora_provider.identity(), "delete", "delete_content", arguments
            ),
            arguments=arguments,
        )

        await kora_provider.delete_content(deletion, context)
        await kora_provider.release_delete(
            DeleteReleaseRequest(
                version=1,
                operation=deletion.operation,
                persisted_outcome_revision="saved-delete",
                issuance_closed="closed-delete",
            ),
            context,
        )
        replayed = await kora_provider.promote(request, context)
        observed = await kora_provider.observe(request.operation, context)
        part = manifest.parts[0]
        write = operation(
            kora_provider.identity(),
            "replay-part",
            "write_part",
            StageWrite(version=1, stage=sealed.stage, spec=part.spec),
        )
        supplied = BytesSource(b"original")
        replayed_part = await kora_provider.write_part(
            write, sealed.stage, part.spec, supplied, context
        )
        with pytest.raises(KoraProtocolError):
            await kora_provider.write_part(
                write, sealed.stage, part.spec, BytesSource(b"changed!"), context
            )
        await kora_provider.aclose()
        reconstructed = kora_artifact_storage_factory()
        try:
            recovered = await reconstructed.promote(request, context)
        finally:
            await reconstructed.aclose()

        check(
            replayed == original,
            "later deletion cannot change the saved promotion outcome",
        )
        check(
            observed.result == original and observed.effects_closed,
            "observation preserves original attribution and closure",
        )
        check(
            replayed_part.part_identity == part.part_identity and supplied.closed,
            "stale byte replay verifies and preserves the saved immutable part",
        )
        check(
            recovered == original,
            "a reconstructed receiver preserves the exact saved outcome",
        )

    asyncio.run(scenario())


@pytest.mark.kora_conformance
def test_artifact_storage_staging_cleanup_is_confined(
    kora_provider: ArtifactStorage,
    kora_capture: Capture,
    kora_artifact_storage_factory: StorageFactory,
) -> None:
    context = context_for(kora_capture)

    async def scenario() -> None:
        upload = await seal_repeated(kora_provider, "cleanup", 65536, context)
        promotion, result = await publish(
            kora_provider, upload.content, "publish", kora_capture
        )

        release = await acknowledge_stage(
            kora_provider,
            upload.content.stage,
            (*upload.operations, promotion.operation),
            context,
        )
        released = await kora_provider.release_stage(release, context)
        with pytest.raises(KoraProtocolError):
            await kora_provider.promote(promotion, context)
        saved = await kora_provider.observe(promotion.operation, context)
        await kora_provider.aclose()
        reconstructed = kora_artifact_storage_factory()
        try:
            reader = await reconstructed.get(result.content, context)
            actual = hashlib.sha256()
            async with reader:
                async for chunk in reader:
                    actual.update(chunk)
        finally:
            await reconstructed.aclose()

        check(released.state == "released", "acknowledgment compacts the retired stage")
        check(
            saved.state == "evidence_unavailable",
            "compaction never manufactures an old result",
        )
        check(result.created_object, "private cleanup follows a real publication")
        check(
            "sha256:" + actual.hexdigest() == upload.content.payload_digest,
            "stage cleanup preserves shared content across reconstruction",
        )

    asyncio.run(scenario())


@pytest.mark.kora_conformance
def test_artifact_storage_staging_lifetime(
    kora_provider: ArtifactStorage, kora_capture: Capture
) -> None:
    context = context_for(kora_capture)

    async def scenario() -> None:
        upload = await seal_repeated(kora_provider, "reader", 65536, context)
        reader = await kora_provider.read_stage(upload.content, context)
        retire = operation(
            kora_provider.identity(), "retire", "retire_stage", upload.content.stage
        )

        pending = await kora_provider.retire_stage(
            retire, upload.content.stage, context
        )
        with pytest.raises(KoraProtocolError):
            await kora_provider.read_stage(upload.content, context)
        await reader.aclose()
        completed = await kora_provider.retire_stage(
            retire, upload.content.stage, context
        )

        check(
            not pending.completed and pending.remaining_effects > 0,
            "an actual reader keeps cleanup unresolved",
        )
        check(completed.completed, "cleanup completes after actual reader closure")

    asyncio.run(scenario())


@pytest.mark.kora_conformance
def test_artifact_storage_deletion_lifetime(
    kora_provider: ArtifactStorage, kora_capture: Capture
) -> None:
    context = context_for(kora_capture)

    async def scenario() -> None:
        sealed, _, _ = await seal_bytes(
            kora_provider, "deletion", b"delete-me", context
        )
        _, published = await publish(kora_provider, sealed, "publish", kora_capture)
        arguments = DeleteArguments(
            version=1, address=published.content.address, deletion_generation=1
        )
        request = DeleteRequest(
            version=1,
            operation=operation(
                kora_provider.identity(), "delete", "delete_content", arguments
            ),
            arguments=arguments,
        )

        deleted = await kora_provider.delete_content(request, context)
        replayed = await kora_provider.delete_content(request, context)
        released = await kora_provider.release_delete(
            DeleteReleaseRequest(
                version=1,
                operation=request.operation,
                persisted_outcome_revision="saved-delete",
                issuance_closed="closed-delete",
            ),
            context,
        )
        with pytest.raises(KoraProtocolError):
            await kora_provider.delete_content(request, context)

        check(
            deleted.deleted_object and replayed == deleted,
            "exact deletion outcome survives replay",
        )
        check(released.state == "released", "closed acknowledged deletion compacts")

    asyncio.run(scenario())


@pytest.mark.kora_conformance
def test_artifact_storage_inventory_is_bounded(
    kora_provider: ArtifactStorage, kora_capture: Capture
) -> None:
    context = context_for(kora_capture)

    async def scenario() -> None:
        for index in range(3):
            await seal_repeated(kora_provider, f"inventory-{index}", index, context)

        first = await kora_provider.scan("stages", None, 2, context)
        second = await kora_provider.scan("stages", first.next_cursor, 2, context)

        check(
            tuple(item.key for item in first.entries) == ("inventory-0", "inventory-1"),
            "first keyset page is ordered",
        )
        check(
            tuple(item.key for item in second.entries) == ("inventory-2",),
            "continuation starts after the last emitted key",
        )
        check(
            len(first.canonical_bytes()) <= 16384
            and len(second.canonical_bytes()) <= 16384,
            "complete pages fit the metadata bound",
        )

    asyncio.run(scenario())


@pytest.mark.kora_conformance
def test_artifact_storage_range_reads_are_exact(
    kora_provider: ArtifactStorage, kora_capture: Capture
) -> None:
    context = context_for(kora_capture)

    async def scenario() -> None:
        payload = bytes(range(256)) * 1024
        sealed, _, _ = await seal_bytes(kora_provider, "range", payload, context)
        _, published = await publish(kora_provider, sealed, "publish", kora_capture)

        for offset, length in ((0, 1), (65530, 65536), (len(payload) - 1, 1)):
            reader = await kora_provider.get_range(
                published.content, offset, length, context
            )
            async with reader:
                actual = b"".join([chunk async for chunk in reader])
            check(
                actual == payload[offset : offset + length],
                "range returns the exact span without a prefix",
            )
        for offset, length in (
            (-1, 1),
            (0, 0),
            (0, 65537),
            (len(payload), 1),
            (True, 1),
        ):
            with pytest.raises(KoraProtocolError):
                await kora_provider.get_range(
                    published.content, offset, length, context
                )

    asyncio.run(scenario())


@pytest.mark.kora_conformance
def test_artifact_storage_range_lifetime(
    kora_provider: ArtifactStorage, kora_capture: Capture
) -> None:
    context = context_for(kora_capture)

    async def scenario() -> None:
        sealed, _, _ = await seal_bytes(
            kora_provider, "range-reader", b"reader", context
        )
        _, published = await publish(kora_provider, sealed, "publish", kora_capture)
        reader = await kora_provider.get_range(published.content, 1, 2, context)
        arguments = DeleteArguments(
            version=1, address=published.content.address, deletion_generation=1
        )
        request = DeleteRequest(
            version=1,
            operation=operation(
                kora_provider.identity(), "delete", "delete_content", arguments
            ),
            arguments=arguments,
        )

        with pytest.raises(KoraProtocolError):
            await kora_provider.delete_content(request, context)
        await reader.aclose()
        deleted = await kora_provider.delete_content(request, context)

        check(
            deleted.deleted_object,
            "deletion proceeds only after the actual range reader closes",
        )

    asyncio.run(scenario())
