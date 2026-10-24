import os
from dataclasses import dataclass

from kora.plugins.storage.content_files import ContentFiles, address_lock
from kora.plugins.storage.errors import refuse, require_deadline
from kora.plugins.storage.payload import (
    FileRunner,
    FileSegment,
    PayloadFiles,
    PrivateFile,
    raw_content_identity,
)
from kora.plugins.storage.staging import StagedPayloads, close_lease
from kora.plugins.storage.state import LocalState, StorageConnection, row_values, text
from kora.protocols.common import ProtocolContext
from kora.protocols.storage_models import (
    MAX_CHUNK_BYTES,
    ContentRef,
    NoEffectFailure,
    PreparedContentRef,
    PromotionArguments,
    PromotionRequest,
    PromotionResult,
    SealedStage,
    StageRef,
    StorageOperationRef,
)


@dataclass(frozen=True)
class PublicationWork:
    slot: int
    segments: tuple[FileSegment, ...]
    reference: ContentRef
    identity: PrivateFile
    prepared: bool


class ContentPublisher:
    def __init__(
        self, state: LocalState, staging: StagedPayloads, runner: FileRunner
    ) -> None:
        self.state = state
        self.root = state.root
        self.staging = staging
        self.runner = runner
        self.files = PayloadFiles(self.root)
        self.contents = ContentFiles(self.root)

    def lookup(
        self, request: PromotionRequest, ctx: ProtocolContext
    ) -> int | PromotionResult:
        with self.state.transaction() as connection:
            saved = self.state.receipt(connection, request.operation)
            if saved is not None and saved.state == "settled":
                if not isinstance(saved.result, PromotionResult):
                    refuse("storage_conflict")
                return saved.result
            require_deadline(ctx)
            self.state.admit_epoch(connection, self.staging.receiver_epoch)
            slot, _ = self.staging.resolve(connection, request.arguments.content)
            return slot

    def admit(
        self, request: PromotionRequest, slot: int, ctx: ProtocolContext
    ) -> PublicationWork | PromotionResult:
        with self.state.transaction(write=True) as connection:
            saved = self.state.receipt(connection, request.operation)
            if saved is not None and saved.state == "settled":
                if not isinstance(saved.result, PromotionResult):
                    refuse("storage_conflict")
                return saved.result
            require_deadline(ctx)
            self.state.admit_epoch(connection, self.staging.receiver_epoch)
            content = request.arguments.content
            checked_slot, segments = self.staging.resolve(connection, content)
            if checked_slot != slot:
                refuse("storage_conflict")
            if isinstance(content, SealedStage):
                if not content.source_complete or any(
                    semantic == "prepare"
                    for semantic, _, _ in self.state.stage_observations(
                        connection, content.stage
                    )
                ):
                    refuse("storage_conflict")
            self.state.register(
                connection,
                request.operation,
                request.arguments,
                "stage",
                content.stage.stage_id,
                "promote",
            )
            reference = self.state.publication(connection, request.operation)
            if reference is None:
                reference = ContentRef(
                    version=1,
                    address=request.arguments.address,
                    object_incarnation=self.root.new_identity(),
                    total_size=content.total_size,
                )
                self.state.save_publication(connection, request.operation, reference)
            identity = self.state.file_identity(connection, request.operation)
            prepared = isinstance(content, PreparedContentRef) and (
                content.content_identity != raw_content_identity(content.source)
            )
            if prepared:
                identity = segments[0].identity
            elif identity is None:
                identity = PrivateFile(
                    version=1,
                    private_identity=self.root.new_identity(),
                    device=self.root.manifest.device,
                    inode=None,
                )
                self.state.save_file_identity(connection, request.operation, identity)
            return PublicationWork(slot, segments, reference, identity, prepared)

    def assemble(self, work: PublicationWork, request: PromotionRequest) -> FileSegment:
        if work.prepared:
            witness = work.segments[0]
            self.contents.verify(witness, work.reference.address.payload_digest)
            return witness
        content = request.arguments.content
        if isinstance(content, PreparedContentRef):
            with self.state.transaction() as connection:
                temporary = self.state.file_identity(connection, content.operation)
            if temporary is None:
                refuse("storage_integrity")
            self.files.discard(work.slot, "prepare", temporary)
        writer, identity = self.state.open_writer(
            work.slot, "promote", request.operation, work.identity
        )
        with writer:
            for segment in work.segments:
                with self.files.open_segment(segment) as source:
                    while chunk := os.read(source.fd, MAX_CHUNK_BYTES):
                        self.files.write(writer, chunk)
            self.files.finish(writer, work.slot)
        witness = FileSegment(
            self.files.path(work.slot, "promote"), identity, work.reference.total_size
        )
        self.contents.verify(witness, work.reference.address.payload_digest)
        return witness

    def target_result(
        self, work: PublicationWork, request: PromotionRequest, verify: bool
    ) -> PromotionResult | None:
        target = self.contents.locate(work.reference.address)
        with self.state.transaction() as connection:
            indexed = self.state.content(connection, work.reference.address)
        if target is None:
            if indexed is not None:
                refuse("storage_integrity")
            return None
        created = work.identity.inode is not None and (
            target.identity.device,
            target.identity.inode,
        ) == (work.identity.device, work.identity.inode)
        if created:
            reference = work.reference
        else:
            if indexed is None or (indexed[1].device, indexed[1].inode) != (
                target.identity.device,
                target.identity.inode,
            ):
                refuse("storage_integrity")
            reference = indexed[0]
        if (
            target.size != reference.total_size
            or reference.address != request.arguments.address
        ):
            refuse("storage_integrity")
        if verify:
            self.contents.verify(target, reference.address.payload_digest)
        if indexed is not None and indexed[0] != reference:
            refuse("storage_integrity")
        return PromotionResult(
            version=1,
            operation=request.operation,
            content=reference,
            created_object=created,
        )

    def settle(self, result: PromotionResult) -> None:
        target = self.contents.locate(result.content.address)
        if target is None:
            refuse("storage_integrity")
        self.contents.synchronize(result.content.address, content=True)
        with self.state.transaction(write=True) as connection:
            indexed = self.state.content(connection, result.content.address)
            if indexed is None:
                if not result.created_object:
                    refuse("storage_integrity")
                connection.execute(
                    "INSERT INTO contents(digest, reference, device, inode) "
                    "VALUES(?, ?, ?, ?)",
                    (
                        result.content.address.payload_digest,
                        result.content.model_dump_json(),
                        target.identity.device,
                        target.identity.inode,
                    ),
                )
            elif indexed[0] != result.content or (
                indexed[1].device,
                indexed[1].inode,
            ) != (target.identity.device, target.identity.inode):
                refuse("storage_integrity")
            self.state.settle(connection, result.operation, result)

    async def promote(
        self, request: PromotionRequest, ctx: ProtocolContext
    ) -> PromotionResult:
        located = await self.runner(lambda: self.lookup(request, ctx))
        if isinstance(located, PromotionResult):
            return located
        ownership = await self.runner(
            lambda: self.root.try_lock(f"stage-{located}"), on_cancel=close_lease
        )
        if ownership is None:
            refuse("storage_busy")
        with ownership:
            stripe = await self.runner(
                lambda: self.root.try_lock(address_lock(request.arguments.address)),
                on_cancel=close_lease,
            )
            if stripe is None:
                refuse("storage_busy")
            with stripe:
                admitted = await self.runner(lambda: self.admit(request, located, ctx))
                if isinstance(admitted, PromotionResult):
                    return admitted
                async with ctx.activity.span("storage.promote.registered"):
                    result = await self.runner(
                        lambda: self.target_result(admitted, request, True)
                    )
                    if result is None:
                        witness = await self.runner(
                            lambda: self.assemble(admitted, request)
                        )

                        def ready() -> None:
                            with self.state.transaction(write=True) as connection:
                                connection.execute(
                                    "UPDATE receipts SET publication_ready=1 "
                                    "WHERE operation_id=?",
                                    (request.operation.operation_id,),
                                )

                        await self.runner(ready)
                        await self.runner(
                            lambda: self.contents.install(
                                witness, request.arguments.address
                            )
                        )
                        async with ctx.activity.span("storage.promote.linked"):
                            await self.runner(
                                lambda: self.contents.synchronize(
                                    request.arguments.address, content=True
                                )
                            )
                        result = PromotionResult(
                            version=1,
                            operation=request.operation,
                            content=admitted.reference,
                            created_object=True,
                        )
                    async with ctx.activity.span("storage.promote.effect_persisted"):
                        await self.runner(lambda: self.settle(result))
                    return result

    def recovery_work(
        self, connection: StorageConnection, request: PromotionRequest, slot: int
    ) -> PublicationWork:
        reference = self.state.publication(connection, request.operation)
        if reference is None:
            refuse("storage_integrity")
        content = request.arguments.content
        prepared = isinstance(content, PreparedContentRef) and (
            content.content_identity != raw_content_identity(content.source)
        )
        if isinstance(content, PreparedContentRef) and prepared:
            identity = self.state.file_identity(connection, content.operation)
        else:
            identity = self.state.file_identity(connection, request.operation)
        if identity is None:
            refuse("storage_integrity")
        return PublicationWork(slot, (), reference, identity, prepared)

    async def recover(self, stage: StageRef, slot: int, ctx: ProtocolContext) -> None:
        def pending() -> PromotionRequest | None:
            with self.state.transaction() as connection:
                row = row_values(
                    connection.execute(
                        "SELECT operation, arguments FROM receipts WHERE "
                        "owner_kind='stage' AND owner_key=? "
                        "AND semantic='promote' AND result IS NULL",
                        (stage.stage_id,),
                    ).fetchone()
                )
                if row is None:
                    return None
                return PromotionRequest(
                    version=1,
                    operation=StorageOperationRef.model_validate_json(text(row[0])),
                    arguments=PromotionArguments.model_validate_json(text(row[1])),
                )

        request = await self.runner(pending, control=True)
        if request is None:
            return
        stripe = await self.runner(
            lambda: self.root.try_lock(address_lock(request.arguments.address)),
            control=True,
            on_cancel=close_lease,
        )
        if stripe is None:
            refuse("storage_busy")
        with stripe:
            async with ctx.activity.span("storage.promote.reconcile"):

                def reconcile() -> None:
                    with self.state.transaction() as connection:
                        work = self.recovery_work(connection, request, slot)
                        row = row_values(
                            connection.execute(
                                "SELECT publication_ready FROM receipts "
                                "WHERE operation_id=?",
                                (request.operation.operation_id,),
                            ).fetchone()
                        )
                    target = self.contents.locate(request.arguments.address)
                    if (
                        target is not None
                        and (target.identity.device, target.identity.inode)
                        == (work.identity.device, work.identity.inode)
                        and row != (1,)
                    ):
                        refuse("storage_integrity")
                    result = self.target_result(work, request, False)
                    if result is not None:
                        self.settle(result)
                    else:
                        self.contents.synchronize(
                            request.arguments.address, content=False
                        )
                        with self.state.transaction(write=True) as connection:
                            self.state.settle(
                                connection,
                                request.operation,
                                NoEffectFailure(
                                    version=1,
                                    operation=request.operation,
                                    code="storage_issuer_closed",
                                ),
                            )

                await self.runner(reconcile, control=True)
