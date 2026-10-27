import hashlib
from collections.abc import Awaitable, Callable
from contextlib import ExitStack
from functools import partial

from kora.plugins.storage.errors import refuse
from kora.plugins.storage.files import RootAnchor
from kora.plugins.storage.payload import (
    FileRunner,
    FileSegment,
    PayloadFiles,
    PrivateFile,
    exclusive_lease,
    raw_content_identity,
)
from kora.plugins.storage.readers import LocalReadStream, ReaderManager
from kora.plugins.storage.state import (
    LocalState,
    Settled,
    StorageConnection,
    operation_value,
    settled_result,
    text,
)
from kora.protocols.common import ProtocolContext
from kora.protocols.storage import StorageByteStream
from kora.protocols.storage_models import (
    MAX_CHUNK_BYTES,
    ManifestPart,
    ManifestPrefix,
    MutationObservation,
    PartRef,
    PartSpec,
    PrefixCapture,
    PrefixRef,
    PreparedContentRef,
    SealedStage,
    StageManifest,
    StageRef,
    StageWrite,
    StorageModel,
    StorageOperationRef,
)


async def consume_exact(
    source: StorageByteStream,
    size: int,
    prefix: bool,
    write: Callable[[bytes], Awaitable[None]] | None = None,
) -> str:
    digest = hashlib.sha256()
    consumed = 0
    while consumed < size:
        maximum = min(MAX_CHUNK_BYTES, size - consumed)
        chunk = await source.read(maximum)
        if type(chunk) is not bytes or len(chunk) > maximum or not chunk:
            refuse("storage_conflict")
        if write is not None:
            await write(chunk)
        consumed += len(chunk)
        digest.update(chunk)
    if not prefix and await source.read(1):
        refuse("storage_conflict")
    return "sha256:" + digest.hexdigest()


async def verify_source(
    source: StorageByteStream, size: int, expected: str, prefix: bool
) -> None:
    if await consume_exact(source, size, prefix) != expected:
        refuse("storage_conflict")


class StagedPayloads:
    def __init__(
        self,
        root: RootAnchor,
        state: LocalState,
        runner: FileRunner,
        readers: ReaderManager,
    ) -> None:
        self.root = root
        self.state = state
        self.runner = runner
        self.readers = readers
        self.files = PayloadFiles(root)

    def lookup(
        self, operation: StorageOperationRef, stage: StageRef, ctx: ProtocolContext
    ) -> tuple[int, MutationObservation | None]:
        with self.state.transaction(synchronize=False) as connection:
            saved = self.state.receipt(connection, operation)
            record = self.state.stage(connection, stage)
            if saved is not None and saved.state == "settled":
                self.root.synchronize_directory(self.root.fd)
                return -1, saved
            self.state.admit(connection, ctx)
            if record is None or record[1] != "open" or record[3] is None:
                refuse("storage_unavailable")
            return record[0], saved

    def lookup_content(
        self,
        operation: StorageOperationRef,
        content: SealedStage | PreparedContentRef,
        kind: type[Settled],
        ctx: ProtocolContext,
    ) -> int | Settled:
        with self.state.transaction(synchronize=False) as connection:
            saved = self.state.settled_receipt(connection, operation, kind)
            if saved is not None:
                self.root.synchronize_directory(self.root.fd)
                return saved
            self.state.admit(connection, ctx)
            slot, _ = self.resolve(connection, content)
            return slot

    def admit_piece(
        self,
        operation: StorageOperationRef,
        arguments: StorageModel,
        stage: StageRef,
        spec: PartSpec,
        semantic: str,
        slot: int,
        prefix: bool,
        ctx: ProtocolContext,
    ) -> PrivateFile | MutationObservation:
        with self.state.transaction(write=True) as connection:
            saved = self.state.receipt(connection, operation)
            if saved is not None and saved.state == "settled":
                return saved
            self.state.admit(connection, ctx)
            record = self.state.stage(connection, stage)
            if (
                record is None
                or record[0] != slot
                or record[1] != "open"
                or record[3] is None
            ):
                refuse("storage_conflict")
            observed = self.state.stage_observations(connection, stage)
            parts = tuple(
                sorted(
                    (
                        item.result
                        for _, item, _ in observed
                        if isinstance(item.result, PartRef)
                    ),
                    key=lambda item: item.spec.index,
                )
            )
            if any(isinstance(item.result, PrefixRef) for _, item, _ in observed):
                refuse("storage_conflict")
            offset = sum(part.spec.size for part in parts)
            if (
                spec.index != len(parts)
                or spec.byte_offset != offset
                or (parts and parts[-1].spec.final)
            ):
                refuse("storage_conflict")
            if spec.byte_offset + spec.size > record[3].total_size:
                refuse("storage_conflict")
            if (
                not prefix
                and spec.final
                and spec.byte_offset + spec.size != record[3].total_size
            ):
                refuse("storage_conflict")
            self.state.register(
                connection, operation, arguments, "stage", stage.stage_id, semantic
            )
            self.state.reserve_stage(
                connection,
                stage,
                max(record[2], 2 * self.state.retained_stage_bytes(connection, stage)),
            )
            return self.state.ensure_file_identity(connection, operation)

    async def write(
        self,
        operation: StorageOperationRef,
        stage: StageRef,
        spec: PartSpec,
        source: StorageByteStream,
        ctx: ProtocolContext,
        count: int | None = None,
    ) -> PartRef | PrefixRef:
        prefix = count is not None
        arguments = (
            StageWrite(version=1, stage=stage, spec=spec)
            if count is None
            else PrefixCapture(version=1, stage=stage, source_spec=spec, count=count)
        )
        semantic = f"part-{spec.index}" if count is None else "prefix"
        maximum_size = spec.size if count is None else count
        try:
            slot, saved = await self.runner(lambda: self.lookup(operation, stage, ctx))
            if saved is not None and saved.state == "settled":
                return await self.replay(saved, source, prefix)
            ownership = await exclusive_lease(self.runner, self.root, f"stage-{slot}")
            with ownership:
                admitted = await self.runner(
                    lambda: self.admit_piece(
                        operation, arguments, stage, spec, semantic, slot, prefix, ctx
                    )
                )
                if isinstance(admitted, MutationObservation):
                    return await self.replay(admitted, source, prefix)
                async with ctx.activity.span(
                    "storage.write_part.registered"
                    if not prefix
                    else "storage.capture_prefix.registered"
                ):
                    writer, identity = await self.runner(
                        lambda: self.state.open_writer(
                            slot, semantic, operation, admitted
                        ),
                        on_cancel=lambda result: result[0].close(),
                    )
                    try:
                        actual = await consume_exact(
                            source,
                            maximum_size,
                            prefix,
                            lambda chunk: self.runner(
                                partial(self.files.write, writer, chunk)
                            ),
                        )
                        if not prefix and actual != spec.digest:
                            refuse("storage_conflict")
                        await self.runner(lambda: self.files.finish(writer, slot))
                        result = (
                            PartRef(
                                version=1,
                                stage=stage,
                                spec=spec,
                                part_identity=identity.private_identity,
                            )
                            if count is None
                            else PrefixRef(
                                version=1,
                                stage=stage,
                                source_spec=spec,
                                count=count,
                                digest=actual,
                                private_identity=identity.private_identity,
                                source_complete=False,
                            )
                        )
                        async with ctx.activity.span(
                            "storage.write_part.effect_persisted"
                            if not prefix
                            else "storage.capture_prefix.effect_persisted"
                        ):

                            def settle() -> None:
                                with self.state.transaction(write=True) as connection:
                                    self.state.settle(connection, operation, result)

                            await self.runner(settle)
                        return result
                    finally:
                        writer.close()
        finally:
            await source.aclose()

    async def replay(
        self, saved: MutationObservation, source: StorageByteStream, prefix: bool
    ) -> PartRef | PrefixRef:
        result = saved.result
        if isinstance(result, PartRef) and not prefix:
            await verify_source(source, result.spec.size, result.spec.digest, False)
            return result
        if isinstance(result, PrefixRef) and prefix:
            await verify_source(source, result.count, result.digest, True)
            return result
        refuse("storage_conflict")

    def segments(
        self,
        connection: StorageConnection,
        stage: StageRef,
        manifest: StageManifest,
        slot: int,
    ) -> tuple[FileSegment, ...]:
        observations = self.state.stage_observations(connection, stage)
        parts: list[tuple[PartRef, PrivateFile]] = []
        prefix: tuple[PrefixRef, PrivateFile] | None = None
        for _, observation, identity in observations:
            if isinstance(observation.result, PartRef):
                if identity is None or identity.inode is None:
                    refuse("storage_integrity")
                parts.append((observation.result, identity))
            if isinstance(observation.result, PrefixRef):
                if identity is None or identity.inode is None:
                    refuse("storage_integrity")
                prefix = observation.result, identity
        ordered = tuple(sorted(parts, key=lambda pair: pair[0].spec.index))
        compact = tuple(
            ManifestPart(version=1, spec=part.spec, part_identity=part.part_identity)
            for part, _ in ordered
        )
        prefix_manifest = (
            None
            if prefix is None
            else ManifestPrefix(
                version=1,
                source_spec=prefix[0].source_spec,
                count=prefix[0].count,
                digest=prefix[0].digest,
                private_identity=prefix[0].private_identity,
            )
        )
        if (
            manifest.stage != stage
            or manifest.parts != compact
            or manifest.prefix != prefix_manifest
        ):
            refuse("storage_conflict")
        segments = tuple(
            FileSegment(
                self.files.path(slot, f"part-{part.spec.index}"),
                identity,
                part.spec.size,
            )
            for part, identity in ordered
        )
        if prefix is not None:
            segments += (
                FileSegment(
                    self.files.path(slot, "prefix"), prefix[1], prefix[0].count
                ),
            )
        return segments

    async def seal(
        self,
        operation: StorageOperationRef,
        stage: StageRef,
        manifest: StageManifest,
        ctx: ProtocolContext,
    ) -> SealedStage:
        slot, saved = await self.runner(lambda: self.lookup(operation, stage, ctx))
        replayed = settled_result(saved, SealedStage)
        if replayed is not None:
            return replayed
        with ExitStack() as ownership:
            async with ctx.activity.span("storage.seal_stage.ownership_pending"):
                ownership.enter_context(
                    await exclusive_lease(self.runner, self.root, f"stage-{slot}")
                )

            def admit() -> tuple[tuple[FileSegment, ...], str, int] | SealedStage:
                with self.state.transaction(write=True) as connection:
                    saved = self.state.settled_receipt(
                        connection, operation, SealedStage
                    )
                    if saved is not None:
                        return saved
                    self.state.admit(connection, ctx)
                    record = self.state.stage(connection, stage)
                    if (
                        record is None
                        or record[0] != slot
                        or record[1] != "open"
                        or record[3] is None
                    ):
                        refuse("storage_conflict")
                    segments = self.segments(connection, stage, manifest, slot)
                    self.state.register(
                        connection, operation, manifest, "stage", stage.stage_id, "seal"
                    )
                    return segments, record[3].payload_digest, record[3].total_size

            admitted = await self.runner(admit)
            if isinstance(admitted, SealedStage):
                return admitted
            segments, expected_digest, expected_size = admitted
            async with ctx.activity.span("storage.seal_stage.registered"):
                size, digest = await self.runner(
                    lambda: self.files.hash_segments(segments)
                )
                if manifest.prefix is None and (
                    size != expected_size or digest != expected_digest
                ):
                    refuse("storage_integrity")
                sealed = SealedStage(
                    version=1,
                    kind="sealed",
                    stage=stage,
                    manifest_digest=manifest.metadata_digest(),
                    total_size=size,
                    payload_digest=digest,
                    source_complete=manifest.prefix is None,
                    seal_identity=operation.operation_id,
                )

                def settle() -> SealedStage:
                    with self.state.transaction(write=True) as connection:
                        self.state.settle(connection, operation, sealed)
                        connection.execute(
                            "UPDATE stages SET state=CASE WHEN state='open' THEN "
                            "'sealed' ELSE state END WHERE stage_id=?",
                            (stage.stage_id,),
                        )
                    return sealed

                return await self.runner(settle)

    def resolve(
        self,
        connection: StorageConnection,
        content: SealedStage | PreparedContentRef,
    ) -> tuple[int, tuple[FileSegment, ...]]:
        record = self.state.stage(connection, content.stage)
        if record is None or record[1] != "sealed":
            refuse("storage_unavailable")
        saved = tuple(
            (item, identity)
            for _, item, identity in self.state.stage_observations(
                connection, content.stage
            )
            if item.result == content
        )
        if len(saved) != 1:
            refuse("storage_conflict")
        observation, identity = saved[0]
        if isinstance(content, PreparedContentRef):
            source_slot, source_segments = self.resolve(connection, content.source)
            if content.content_identity == raw_content_identity(content.source):
                if (
                    content.total_size != content.source.total_size
                    or content.payload_digest != content.source.payload_digest
                ):
                    refuse("storage_integrity")
                return source_slot, source_segments
            if identity is None or identity.inode is None:
                refuse("storage_integrity")
            return record[0], (
                FileSegment(
                    self.files.path(record[0], "prepare"), identity, content.total_size
                ),
            )
        arguments = operation_value(
            connection,
            "SELECT arguments FROM receipts WHERE operation_id=?",
            observation.operation,
        )
        manifest = StageManifest.model_validate_json(text(arguments))
        return record[0], self.segments(connection, content.stage, manifest, record[0])

    async def read(
        self, content: SealedStage | PreparedContentRef, ctx: ProtocolContext
    ) -> LocalReadStream:
        def locate() -> tuple[int, tuple[FileSegment, ...]]:
            with self.state.transaction(synchronize=False) as connection:
                self.state.admit(connection, ctx)
                return self.resolve(connection, content)

        slot, segments = await self.runner(locate)

        def validate() -> None:
            checked_slot, checked_segments = locate()
            if slot != checked_slot or segments != checked_segments:
                refuse("storage_conflict")

        return await self.readers.open(
            segments, f"stage-{slot}", validate, 0, content.total_size, ctx
        )

    async def cleanup(self, stage: StageRef, slot: int) -> None:
        def inspect() -> tuple[
            tuple[str, MutationObservation, PrivateFile | None], ...
        ]:
            with self.state.transaction(synchronize=False) as connection:
                return self.state.stage_observations(connection, stage)

        observations = await self.runner(inspect, control=True)
        if any(
            semantic == "promote" and observation.state != "settled"
            for semantic, observation, _ in observations
        ):
            refuse("storage_busy")
        files = tuple(
            (semantic, identity)
            for semantic, _, identity in observations
            if identity is not None
        )
        await self.runner(partial(self.files.discard_stage, slot, files), control=True)

        def abort_pending() -> None:
            with self.state.transaction(write=True) as connection:
                for semantic, observation, _ in observations:
                    if observation.state == "pending" and semantic != "retire":
                        self.state.abandon(connection, observation.operation)

        if any(
            observation.state == "pending" and semantic != "retire"
            for semantic, observation, _ in observations
        ):
            await self.runner(abort_pending, control=True)
