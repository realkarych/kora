import asyncio
import hashlib
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta

from kora.conformance.storage_support import (
    BytesSource,
    RepeatedSource,
    SealedUpload,
    acknowledge_stage,
    operation,
    repeated_digest,
    seal_repeated,
)
from kora.plugins.storage.local import LocalStorageConfiguration
from kora.protocols.common import ProtocolContext
from kora.protocols.errors import KoraProtocolError
from kora.protocols.runtime import DEADLINE_GRACE, ExecutionDeadline
from kora.protocols.storage_models import (
    ContentAddress,
    ContentPreparation,
    ContentRef,
    DeleteArguments,
    DeleteReleaseRequest,
    DeleteRequest,
    ManifestPrefix,
    PartSpec,
    PrefixCapture,
    PreparationArguments,
    PreparedContentRef,
    PromotionArguments,
    PromotionRequest,
    PublishableContent,
    SealedStage,
    StageManifest,
    StageRef,
    StageSpec,
    StageWrite,
    StorageOperationRef,
)
from tests.measurements.storage_lifetime_manifest import Workload
from tests.measurements.storage_lifetime_resources import close_provider
from tests.plugins.storage.activity import StorageBarrierActivity
from tests.plugins.storage.clock import StorageClock
from tests.plugins.storage.inspection import InspectedStorage


class InterruptedSource(RepeatedSource):
    def __init__(self, size: int, cancel: bool, phase: Callable[[], None]) -> None:
        super().__init__(size)
        self.offset: int = 0
        self.cancel = cancel
        self.phase = phase
        self.entered = asyncio.Event()
        self.released = asyncio.Event()

    async def read(self, maximum: int) -> bytes:
        if self.offset < self.size:
            return await super().read(maximum)
        self.phase()
        self.entered.set()
        if self.cancel:
            await self.released.wait()
        raise OSError("an injected source ended before its completion receipt")


def promotion(
    provider: InspectedStorage, content: PublishableContent, label: str
) -> PromotionRequest:
    arguments = PromotionArguments(
        version=1,
        content=content,
        address=ContentAddress(
            version=1,
            storage_identity=provider.identity(),
            payload_digest=content.payload_digest,
        ),
        content_generation=1,
    )
    return PromotionRequest(
        version=1,
        operation=operation(provider.identity(), label, "promote", arguments),
        arguments=arguments,
    )


async def prepare_result(
    provider: InspectedStorage,
    source: SealedStage,
    size: int,
    label: str,
    context: ProtocolContext,
) -> tuple[PreparedContentRef, StorageOperationRef]:
    arguments = PreparationArguments(
        version=1,
        source=source,
        maximum_output_size=size,
        expected_output_digest=repeated_digest(size),
    )
    request = ContentPreparation(
        version=1,
        operation=operation(
            provider.identity(), label + "-prepare", "prepare_content", arguments
        ),
        arguments=arguments,
    )
    result = await provider.prepare_content(request, RepeatedSource(size), context)
    return result, request.operation


async def captured_upload(
    provider: InspectedStorage,
    label: str,
    count: int,
    context: ProtocolContext,
) -> SealedUpload:
    spec = StageSpec(
        version=1,
        stage=StageRef(version=1, storage_identity=provider.identity(), stage_id=label),
        payload_digest=repeated_digest(count + 1),
        total_size=count + 1,
        content_type="application/octet-stream",
    )
    begin = operation(provider.identity(), label + "-begin", "begin_stage", spec)
    await provider.begin_stage(begin, spec, context)
    part = PartSpec(
        version=1,
        index=0,
        byte_offset=0,
        size=count + 1,
        digest=spec.payload_digest,
        final=True,
    )
    arguments = PrefixCapture(
        version=1, stage=spec.stage, source_spec=part, count=count
    )
    capture = operation(
        provider.identity(), label + "-capture", "capture_prefix", arguments
    )
    source = RepeatedSource(count + 1)
    captured = await provider.capture_prefix(
        capture, spec.stage, part, count, source, context
    )
    replay_source = RepeatedSource(count + 1)
    replay = await provider.capture_prefix(
        capture, spec.stage, part, count, replay_source, context
    )
    if (
        not source.closed
        or not replay_source.closed
        or source.offset != count
        or replay_source.offset != count
        or replay != captured
    ):
        raise AssertionError(
            "prefix capture/replay consumed the unretained tail or changed its receipt"
        )
    if count:
        try:
            await provider.capture_prefix(
                capture,
                spec.stage,
                part,
                count,
                BytesSource(b"x" * min(count, 65536)),
                context,
            )
        except KoraProtocolError as error:
            if error.code != "storage_conflict":
                raise
        else:
            raise AssertionError("prefix replay accepted changed supplied bytes")
    manifest = StageManifest(
        version=1,
        stage=spec.stage,
        parts=(),
        prefix=ManifestPrefix(
            version=1,
            source_spec=part,
            count=captured.count,
            digest=captured.digest,
            private_identity=captured.private_identity,
        ),
    )
    seal = operation(provider.identity(), label + "-seal", "seal_stage", manifest)
    sealed = await provider.seal_stage(seal, spec.stage, manifest, context)
    if sealed.source_complete or sealed.total_size != count:
        raise AssertionError("bounded prefix seal claimed a complete source")
    return SealedUpload(sealed, (begin, capture, seal))


async def verify_reads(
    provider: InspectedStorage, content: ContentRef, context: ProtocolContext
) -> None:
    digest = hashlib.sha256()
    reader = await provider.get(content, context)
    async with reader:
        async for chunk in reader:
            digest.update(chunk)
    if "sha256:" + digest.hexdigest() != content.address.payload_digest:
        raise AssertionError("a complete storage read changed bytes")
    length = min(17, content.total_size)
    offset = content.total_size - length
    if length:
        selected = await provider.get_range(content, offset, length, context)
        consumed = 0
        async with selected:
            async for chunk in selected:
                consumed += len(chunk)
        if consumed != length:
            raise AssertionError("the bounded range changed length")
    early = await provider.get(content, context)
    if content.total_size:
        await early.read(1)
    await early.aclose()


async def interrupted_upload(
    provider: InspectedStorage,
    label: str,
    size: int,
    context: ProtocolContext,
    cancel: bool,
    phase: Callable[[], None],
) -> tuple[StageRef, tuple[StorageOperationRef, ...]]:
    spec = StageSpec(
        version=1,
        stage=StageRef(version=1, storage_identity=provider.identity(), stage_id=label),
        payload_digest=repeated_digest(size),
        total_size=size,
        content_type="application/octet-stream",
    )
    begin = operation(provider.identity(), label + "-begin", "begin_stage", spec)
    await provider.begin_stage(begin, spec, context)
    part = PartSpec(
        version=1,
        index=0,
        byte_offset=0,
        size=size,
        digest=spec.payload_digest,
        final=True,
    )
    write = operation(
        provider.identity(),
        label + "-part",
        "write_part",
        StageWrite(version=1, stage=spec.stage, spec=part),
    )
    source = InterruptedSource(size, cancel, phase)
    if cancel:
        task = asyncio.create_task(
            provider.write_part(write, spec.stage, part, source, context)
        )
        await asyncio.wait_for(source.entered.wait(), 5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("the cancelled write returned a result")
    else:
        try:
            await provider.write_part(write, spec.stage, part, source, context)
        except KoraProtocolError as error:
            if error.code != "storage_io":
                raise
        else:
            raise AssertionError("the failing source returned a write result")
    return spec.stage, (begin, write)


async def cycle(
    configuration: LocalStorageConfiguration,
    workload: Workload,
    context: ProtocolContext,
    label: str,
    phase: Callable[[], None],
) -> tuple[dict[str, int], float]:
    provider = InspectedStorage(configuration)
    stages: list[tuple[StageRef, tuple[StorageOperationRef, ...]]] = []
    published: ContentRef | None = None
    try:
        if workload.ending in {"source_error", "cancelled"}:
            stage, issued = await interrupted_upload(
                provider,
                label,
                workload.size,
                context,
                workload.ending == "cancelled",
                phase,
            )
            await close_provider(provider)
            provider = InspectedStorage(configuration)
            unknown = await provider.observe(issued[-1], context)
            if unknown.state != "pending":
                raise AssertionError("an interrupted write lost its recovery intent")
            stages.append((stage, issued))
        else:
            upload = (
                await captured_upload(provider, label, workload.size, context)
                if workload.ending == "prefix"
                else await seal_repeated(provider, label, workload.size, context)
            )
            phase()
            issued = upload.operations
            selected = upload.content
            if workload.ending in {"prepared", "alias", "prefix"}:
                size = (
                    workload.size
                    if workload.ending in {"alias", "prefix"}
                    else (
                        workload.size + 1
                        if workload.size < 268435456
                        else workload.size - 1
                    )
                )
                selected_prepared, prepare_operation = await prepare_result(
                    provider, selected, size, label, context
                )
                issued += (prepare_operation,)
                phase()
                reader = await provider.read_stage(selected_prepared, context)
                async with reader:
                    async for _ in reader:
                        pass
                request = promotion(provider, selected_prepared, label + "-publish")
            else:
                request = promotion(provider, upload.content, label + "-publish")
            if workload.ending == "replay_conflict":
                part = PartSpec(
                    version=1,
                    index=0,
                    byte_offset=0,
                    size=workload.size,
                    digest=selected.payload_digest,
                    final=True,
                )
                replay = await provider.write_part(
                    issued[1],
                    selected.stage,
                    part,
                    RepeatedSource(workload.size),
                    context,
                )
                try:
                    await provider.write_part(
                        issued[1],
                        selected.stage,
                        part,
                        BytesSource(b"changed"),
                        context,
                    )
                except KoraProtocolError as error:
                    if error.code != "storage_conflict":
                        raise
                else:
                    raise AssertionError("changed replay bytes were accepted")
                if replay.stage != selected.stage:
                    raise AssertionError("byte replay changed owner")
            if workload.ending == "expired":
                clock = StorageClock()
                expired = replace(
                    context,
                    clock=clock,
                    deadline=ExecutionDeadline(
                        at=clock.now() - timedelta(seconds=1),
                        scope_kind="run",
                        scope_id="expired-storage",
                        clock_uncertainty=timedelta(0),
                        grace=DEADLINE_GRACE,
                    ),
                )
                try:
                    await provider.read_stage(selected, expired)
                except KoraProtocolError as error:
                    if error.code != "storage_deadline":
                        raise
                else:
                    raise AssertionError("expired useful storage admission succeeded")
            if workload.ending == "lost_response":
                barrier = StorageBarrierActivity(
                    "storage.promote.admitted", on_exit=True
                )
                task = asyncio.create_task(
                    provider.promote(request, replace(context, activity=barrier))
                )
                await asyncio.wait_for(barrier.entered.wait(), 5)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                else:
                    raise AssertionError("the response-loss trace returned a response")
                await close_provider(provider)
                provider = InspectedStorage(configuration)
            result = await provider.promote(request, context)
            published = result.content
            issued += (request.operation,)
            phase()
            await verify_reads(provider, published, context)
            if workload.ending == "raw" and workload.size <= 65536:
                duplicate = await captured_upload(
                    provider, label + "-existing", workload.size, context
                )
                candidate, prepared_operation = await prepare_result(
                    provider,
                    duplicate.content,
                    workload.size,
                    label + "-existing",
                    context,
                )
                phase()
                existing_request = promotion(
                    provider, candidate, label + "-existing-publish"
                )
                existing = await provider.promote(existing_request, context)
                if existing.created_object or existing.content != published:
                    raise AssertionError("create-only publication changed attribution")
                stages.append(
                    (
                        duplicate.content.stage,
                        (
                            *duplicate.operations,
                            prepared_operation,
                            existing_request.operation,
                        ),
                    )
                )
            stages.append((selected.stage, issued))
        for stage, issued in stages:
            acknowledgment = await acknowledge_stage(provider, stage, issued, context)
            released = await provider.release_stage(acknowledgment, context)
            if released.state != "released":
                raise AssertionError("settled stage ownership did not compact")
        phase()
        if published is not None:
            await close_provider(provider)
            provider = InspectedStorage(configuration)
            arguments = DeleteArguments(
                version=1, address=published.address, deletion_generation=1
            )
            request = DeleteRequest(
                version=1,
                operation=operation(
                    provider.identity(), label + "-delete", "delete_content", arguments
                ),
                arguments=arguments,
            )
            deleted = await provider.delete_content(request, context)
            replayed = await provider.delete_content(request, context)
            if not deleted.deleted_object or replayed != deleted:
                raise AssertionError("deletion replay changed its saved outcome")
            await provider.release_delete(
                DeleteReleaseRequest(
                    version=1,
                    operation=request.operation,
                    persisted_outcome_revision="saved-delete",
                    issuance_closed="closed-delete",
                ),
                context,
            )
        observation_started = time.perf_counter()
        counts = provider.resource_counts()
        observation_seconds = time.perf_counter() - observation_started
        if any(
            counts[name]
            for name in (
                "stage_rows",
                "delete_rows",
                "receipts",
                "content_rows",
                "reserved_bytes",
                "owned_jobs",
                "native_jobs",
                "readers",
                "connections",
            )
        ):
            raise AssertionError(
                "disposable provider resources remain after acknowledgment"
            )
        return counts, observation_seconds
    finally:
        await close_provider(provider)
