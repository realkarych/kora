import os
from dataclasses import dataclass

from kora.plugins.storage.content_files import ContentFiles, address_lock
from kora.plugins.storage.errors import refuse
from kora.plugins.storage.payload import (
    FileRunner,
    FileSegment,
    PayloadFiles,
    PrivateFile,
    exclusive_lease,
    raw_content_identity,
    same_file,
)
from kora.plugins.storage.staging import StagedPayloads
from kora.plugins.storage.state import LocalState, StorageConnection, row_values, text
from kora.protocols.common import ProtocolContext
from kora.protocols.storage_models import (
    MAX_CHUNK_BYTES,
    ContentRef,
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

    def admit(
        self, request: PromotionRequest, slot: int, ctx: ProtocolContext
    ) -> PublicationWork | PromotionResult:
        with self.state.transaction(write=True) as connection:
            saved = self.state.settled_receipt(
                connection, request.operation, PromotionResult
            )
            if saved is not None:
                return saved
            self.state.admit(connection, ctx)
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
            prepared = isinstance(content, PreparedContentRef) and (
                content.content_identity != raw_content_identity(content.source)
            )
            if prepared:
                self.state.file_identity(connection, request.operation)
                identity = segments[0].identity
            else:
                identity = self.state.ensure_file_identity(
                    connection, request.operation
                )
            return PublicationWork(slot, segments, reference, identity, prepared)

    def assemble(self, work: PublicationWork, request: PromotionRequest) -> FileSegment:
        if work.prepared:
            witness = work.segments[0]
            self.contents.verify(witness, work.reference.address.payload_digest)
            return witness
        content = request.arguments.content
        if isinstance(content, PreparedContentRef):
            with self.state.transaction(synchronize=False) as connection:
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
        with self.state.transaction(synchronize=False) as connection:
            indexed = self.state.content(connection, work.reference.address)
        if target is None:
            if indexed is not None:
                refuse("storage_integrity")
            return None
        created = work.identity.inode is not None and same_file(
            target.identity, work.identity
        )
        if created:
            reference = work.reference
        else:
            if indexed is None or not same_file(indexed[1], target.identity):
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
            elif indexed[0] != result.content or not same_file(
                indexed[1], target.identity
            ):
                refuse("storage_integrity")
            self.state.settle(connection, result.operation, result)

    async def promote(
        self, request: PromotionRequest, ctx: ProtocolContext
    ) -> PromotionResult:
        located = await self.runner(
            lambda: self.staging.lookup_content(
                request.operation, request.arguments.content, PromotionResult, ctx
            )
        )
        if isinstance(located, PromotionResult):
            return located
        ownership = await exclusive_lease(self.runner, self.root, f"stage-{located}")
        with ownership:
            stripe = await exclusive_lease(
                self.runner, self.root, address_lock(request.arguments.address)
            )
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
            with self.state.transaction(synchronize=False) as connection:
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
        stripe = await exclusive_lease(
            self.runner,
            self.root,
            address_lock(request.arguments.address),
            control=True,
        )
        with stripe:
            async with ctx.activity.span("storage.promote.reconcile"):

                def reconcile() -> None:
                    with self.state.transaction(synchronize=False) as connection:
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
                        and same_file(target.identity, work.identity)
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
                            self.state.abandon(connection, request.operation)

                await self.runner(reconcile, control=True)
