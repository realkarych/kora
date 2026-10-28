from kora.plugins.storage.content_files import ContentFiles, address_lock, content_path
from kora.plugins.storage.errors import refuse
from kora.plugins.storage.payload import (
    FileRunner,
    FileSegment,
    PrivateFile,
    exclusive_lease,
    try_lease,
)
from kora.plugins.storage.state import (
    LocalState,
    free_slot,
    integer,
    operation_value,
    row_values,
    settled_result,
    text,
)
from kora.protocols.common import ProtocolContext
from kora.protocols.storage_models import (
    ContentRef,
    DeleteReleaseRequest,
    DeleteRequest,
    DeleteResult,
    StorageReleaseResult,
)


class ContentDeleter:
    def __init__(self, state: LocalState, runner: FileRunner) -> None:
        self.state = state
        self.root = state.root
        self.runner = runner
        self.files = ContentFiles(self.root)

    def allocate(self, request: DeleteRequest) -> int | DeleteResult:
        with self.state.transaction(write=True) as connection:
            saved = self.state.settled_receipt(
                connection, request.operation, DeleteResult
            )
            if saved is not None:
                return saved
            row = row_values(
                connection.execute(
                    "SELECT slot, request FROM deletions WHERE operation_id=?",
                    (request.operation.operation_id,),
                ).fetchone()
            )
            if row is not None:
                if DeleteRequest.model_validate_json(text(row[1])) != request:
                    refuse("storage_conflict")
                return integer(row[0])
            self.state.admit_epoch(connection)
            slot = free_slot(
                connection,
                "SELECT slot FROM deletions",
                self.root.manifest.limits.deletion_slots,
            )
            if slot is None:
                refuse("storage_capacity")
            self.state.register(
                connection,
                request.operation,
                request.arguments,
                "delete",
                request.operation.operation_id,
                "delete",
            )
            connection.execute(
                "INSERT INTO deletions(slot, operation_id, request) VALUES(?, ?, ?)",
                (slot, request.operation.operation_id, request.model_dump_json()),
            )
            return slot

    def admit(
        self, request: DeleteRequest, slot: int
    ) -> FileSegment | DeleteResult | None:
        with self.state.transaction(write=True) as connection:
            saved = self.state.receipt(connection, request.operation)
            if saved is None:
                refuse("storage_issuer_closed")
            settled = settled_result(saved, DeleteResult)
            if settled is not None:
                return settled
            row = row_values(
                connection.execute(
                    "SELECT slot, request, target, device, inode, resolved "
                    "FROM deletions WHERE operation_id=?",
                    (request.operation.operation_id,),
                ).fetchone()
            )
            if (
                row is None
                or integer(row[0]) != slot
                or DeleteRequest.model_validate_json(text(row[1])) != request
            ):
                refuse("storage_conflict")
            pending = connection.execute(
                "SELECT 1 FROM receipts WHERE address_digest=? "
                "AND semantic='promote' AND result IS NULL LIMIT 1",
                (request.arguments.address.payload_digest,),
            ).fetchone()
            if pending is not None:
                refuse("storage_busy")
            if integer(row[5]) == 1:
                if row[2] is None:
                    return None
                reference = ContentRef.model_validate_json(text(row[2]))
                return FileSegment(
                    content_path(reference.address),
                    PrivateFile(
                        version=1,
                        private_identity=reference.object_incarnation,
                        device=integer(row[3]),
                        inode=integer(row[4]),
                    ),
                    reference.total_size,
                )
            indexed = self.state.content(connection, request.arguments.address)
            connection.execute(
                "UPDATE deletions SET resolved=1, target=?, device=?, inode=? "
                "WHERE operation_id=?",
                (
                    None if indexed is None else indexed[0].model_dump_json(),
                    None if indexed is None else indexed[1].device,
                    None if indexed is None else indexed[1].inode,
                    request.operation.operation_id,
                ),
            )
            if indexed is None:
                return None
            return FileSegment(
                content_path(indexed[0].address), indexed[1], indexed[0].total_size
            )

    async def delete(
        self, request: DeleteRequest, ctx: ProtocolContext
    ) -> DeleteResult:
        located = await self.runner(lambda: self.allocate(request), control=True)
        if isinstance(located, DeleteResult):
            return located
        ownership = await exclusive_lease(
            self.runner, self.root, f"delete-{located}", control=True
        )
        with ownership:
            stripe = await exclusive_lease(
                self.runner,
                self.root,
                address_lock(request.arguments.address),
                control=True,
            )
            with stripe:
                admitted = await self.runner(
                    lambda: self.admit(request, located), control=True
                )
                if isinstance(admitted, DeleteResult):
                    return admitted
                async with ctx.activity.span("storage.delete_content.registered"):
                    if admitted is None:
                        if (
                            await self.runner(
                                lambda: self.files.locate(request.arguments.address),
                                control=True,
                            )
                            is not None
                        ):
                            refuse("storage_integrity")
                    else:
                        await self.runner(
                            lambda: self.files.delete(
                                request.arguments.address, admitted
                            ),
                            control=True,
                        )
                        async with ctx.activity.span("storage.delete_content.unlinked"):
                            await self.runner(
                                lambda: self.files.synchronize(
                                    request.arguments.address, content=False
                                ),
                                control=True,
                            )
                    result = DeleteResult(
                        version=1,
                        operation=request.operation,
                        address=request.arguments.address,
                        deletion_generation=request.arguments.deletion_generation,
                        deleted_object=admitted is not None,
                    )
                    async with ctx.activity.span(
                        "storage.delete_content.effect_persisted"
                    ):

                        def settle() -> None:
                            with self.state.transaction(write=True) as connection:
                                connection.execute(
                                    "DELETE FROM contents WHERE digest=?",
                                    (request.arguments.address.payload_digest,),
                                )
                                self.state.settle(connection, request.operation, result)

                        await self.runner(settle, control=True)
                    return result

    async def release(
        self, request: DeleteReleaseRequest, ctx: ProtocolContext
    ) -> StorageReleaseResult:
        def locate() -> int | None:
            with self.state.transaction(synchronize=False) as connection:
                saved = self.state.receipt(connection, request.operation)
                if saved is None:
                    self.root.synchronize_directory(self.root.fd)
                    return None
                if (
                    not isinstance(saved.result, DeleteResult)
                    or saved.state != "settled"
                ):
                    refuse("storage_busy")
                return integer(
                    operation_value(
                        connection,
                        "SELECT slot FROM deletions WHERE operation_id=?",
                        request.operation,
                    )
                )

        slot = await self.runner(locate, control=True)
        if slot is None:
            return StorageReleaseResult(version=1, state="released")
        ownership = await try_lease(
            self.runner, self.root, f"delete-{slot}", control=True
        )
        if ownership is None:
            return StorageReleaseResult(version=1, state="pending")
        with ownership:
            async with ctx.activity.span("storage.release_delete.exclusive"):

                def compact() -> StorageReleaseResult:
                    with self.state.transaction(write=True) as connection:
                        saved = self.state.receipt(connection, request.operation)
                        if saved is None:
                            return StorageReleaseResult(version=1, state="released")
                        if saved.state != "settled" or not isinstance(
                            saved.result, DeleteResult
                        ):
                            refuse("storage_conflict")
                        row = row_values(
                            connection.execute(
                                "SELECT slot FROM deletions WHERE operation_id=?",
                                (request.operation.operation_id,),
                            ).fetchone()
                        )
                        if row != (slot,):
                            refuse("storage_conflict")
                        self.state.advance_epoch(connection)
                        connection.execute(
                            "DELETE FROM receipts WHERE operation_id=?",
                            (request.operation.operation_id,),
                        )
                        connection.execute(
                            "DELETE FROM deletions WHERE operation_id=?",
                            (request.operation.operation_id,),
                        )
                        return StorageReleaseResult(version=1, state="released")

                return await self.runner(compact, control=True)
