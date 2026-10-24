import asyncio
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Annotated, Final, TypeVar, cast

from pydantic import Field, StrictStr

from kora.ir.json import JsonObject, parse_json_object
from kora.observability import CATALOG, EventSpec, FieldKind, Level
from kora.plugins.storage.access import ContentAccess
from kora.plugins.storage.deletion import ContentDeleter
from kora.plugins.storage.errors import refuse, require_deadline
from kora.plugins.storage.files import FileLease, RootAnchor, random_identity
from kora.plugins.storage.inventory import scan
from kora.plugins.storage.preparation import ContentPreparer
from kora.plugins.storage.publication import ContentPublisher
from kora.plugins.storage.readers import LocalReadStream, ReaderManager
from kora.plugins.storage.staging import StagedPayloads
from kora.plugins.storage.state import LocalState
from kora.protocols.common import BoundaryModel, ProtocolContext
from kora.protocols.errors import KoraProtocolError
from kora.protocols.identity import MAX_COUNT
from kora.protocols.metadata import ProtocolName, ProviderMetadata
from kora.protocols.storage import ArtifactStorageCapabilities, StorageByteStream
from kora.protocols.storage_models import (
    MAX_CHUNK_BYTES,
    ContentAddress,
    ContentPreparation,
    ContentRef,
    DeleteReleaseRequest,
    DeleteRequest,
    DeleteResult,
    MutationObservation,
    PartRef,
    PartSpec,
    PrefixCapture,
    PrefixRef,
    PreparedContentRef,
    PromotionRequest,
    PromotionResult,
    SealedStage,
    StageManifest,
    StageRef,
    StageReleaseRequest,
    StageRetirement,
    StageSpec,
    StageWrite,
    StorageIdentity,
    StorageLimits,
    StorageModel,
    StorageOperationKind,
    StorageOperationRef,
    StoragePage,
    StorageReleaseResult,
    StorageScanCursor,
    StorageScanKind,
)

Result = TypeVar("Result")
COMPONENT: Final[str] = CATALOG.register_component("storage.local")
COMPLETED: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "storage.local.completed",
        COMPONENT,
        Level.DEBUG,
        "a local storage operation completed",
        {"operation": FieldKind.STRING, "duration_ms": FieldKind.FLOAT},
    )
)
REFUSED: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "storage.local.refused",
        COMPONENT,
        Level.WARNING,
        "a local storage operation was refused",
        {"operation": FieldKind.STRING, "reason": FieldKind.STRING},
    )
)
FAILED: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "storage.local.failed",
        COMPONENT,
        Level.ERROR,
        "an unexpected local storage operation failed",
        {"operation": FieldKind.STRING, "exception": FieldKind.EXCEPTION},
    )
)

SCAN_WORK: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "storage.local.scan_work",
        COMPONENT,
        Level.DEBUG,
        "a bounded inventory query completed",
        {"queried_rows": FieldKind.INTEGER, "query_steps": FieldKind.INTEGER},
    )
)


class LocalStorageConfiguration(BoundaryModel):
    root: Annotated[StrictStr, Field(min_length=1, max_length=4096)]
    limits: StorageLimits


CONFIG_SCHEMA: Final[JsonObject] = parse_json_object(
    LocalStorageConfiguration.model_json_schema()
)


def close_lease(lease: FileLease | None) -> None:
    if lease is not None:
        lease.close()


class LocalArtifactStorage:
    name: str = "local"
    config_schema: JsonObject = CONFIG_SCHEMA

    def __init__(
        self,
        configuration: LocalStorageConfiguration,
        *,
        new_identity: Callable[[], str] = random_identity,
    ) -> None:
        self._root = RootAnchor.open(
            Path(configuration.root), configuration.limits, new_identity
        )
        try:
            self._state = LocalState(self._root)
            self._epoch = self._state.current_epoch()
        except BaseException:
            self._root.close()
            raise
        self._payload = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="kora-storage-data"
        )
        self._control = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="kora-storage-control"
        )
        self._jobs: set[asyncio.Task[object]] = set()
        self._read_jobs: set[asyncio.Task[object]] = set()
        self._native: set[asyncio.Future[object]] = set()
        self._data_count = 0
        self._control_count = 0
        self._closing = False
        self._close_task: asyncio.Task[None] | None = None
        self._readers = ReaderManager(self._root, self.io, self.start_read)
        self._staging = StagedPayloads(
            self._root, self._state, self._epoch, self.io, self._readers
        )
        self._preparer = ContentPreparer(self._state, self._staging, self.io)
        self._publisher = ContentPublisher(self._state, self._staging, self.io)
        self._access = ContentAccess(self._state, self._epoch, self.io, self._readers)
        self._deleter = ContentDeleter(self._state, self._epoch, self.io)

    def identity(self) -> StorageIdentity:
        return self._root.identity

    def capabilities(self) -> ArtifactStorageCapabilities:
        return ArtifactStorageCapabilities()

    def validate_operation(
        self,
        operation: StorageOperationRef,
        kind: StorageOperationKind,
        arguments: StorageModel,
    ) -> None:
        if operation.storage_identity != self.identity() or not operation.matches(
            kind, arguments
        ):
            refuse("storage_conflict")

    async def io(
        self,
        function: Callable[[], Result],
        control: bool = False,
        on_cancel: Callable[[Result], None] | None = None,
    ) -> Result:
        future = asyncio.get_running_loop().run_in_executor(
            self._control if control else self._payload, function
        )
        owned = cast("asyncio.Future[object]", future)
        self._native.add(owned)
        cancelled = False
        try:
            while True:
                try:
                    result = await asyncio.shield(future)
                    break
                except asyncio.CancelledError:
                    if future.cancelled():
                        raise
                    cancelled = True
            if cancelled:
                if on_cancel is not None:
                    on_cancel(result)
                raise asyncio.CancelledError
            return result
        finally:
            self._native.discard(owned)

    def ticket(self, control: bool) -> FileLease:
        kind = "control" if control else "data"
        count = 2 if control else self._root.manifest.limits.data_work_slots
        for index in range(count):
            acquired = self._root.try_lock(f"{kind}-{index}")
            if acquired is not None:
                return acquired
        refuse("storage_busy")

    def start_job(
        self,
        operation: str,
        function: Callable[[], Awaitable[Result]],
        ctx: ProtocolContext,
        control: bool = False,
    ) -> asyncio.Task[Result]:
        if self._closing:
            refuse("storage_closed")
        if (self._control_count >= 2) if control else (self._data_count >= 8):
            refuse("storage_busy")
        if control:
            self._control_count += 1
        else:
            self._data_count += 1

        async def execute() -> object:
            started = ctx.clock.monotonic_ns()
            try:
                lease = await self.io(
                    lambda: self.ticket(control), control, on_cancel=FileLease.close
                )
                with lease:
                    async with ctx.activity.span(f"storage.{operation}.admitted"):
                        result = await function()
                ctx.logger.emit(
                    COMPLETED,
                    operation=operation,
                    duration_ms=max(0, ctx.clock.monotonic_ns() - started) / 1000000,
                )
                return result
            except KoraProtocolError as error:
                ctx.logger.emit_at(
                    Level.DEBUG if error.code == "storage_busy" else Level.WARNING,
                    REFUSED,
                    operation=operation,
                    reason=error.code,
                )
                raise
            except OSError:
                ctx.logger.emit(REFUSED, operation=operation, reason="storage_io")
                refuse("storage_io")
            except Exception as error:
                ctx.logger.emit(FAILED, operation=operation, exception=error)
                raise
            finally:
                if control:
                    self._control_count -= 1
                else:
                    self._data_count -= 1

        task = asyncio.create_task(execute())
        self._jobs.add(task)

        def completed(job: asyncio.Task[object]) -> None:
            self._jobs.discard(job)
            if not job.cancelled():
                job.exception()

        task.add_done_callback(completed)
        return cast("asyncio.Task[Result]", task)

    async def dispatch(
        self,
        operation: str,
        function: Callable[[], Awaitable[Result]],
        ctx: ProtocolContext,
        control: bool = False,
    ) -> Result:
        task = self.start_job(operation, function, ctx, control)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if not task.cancelling():
                task.cancel()
            raise

    def start_read(
        self, function: Callable[[], bytes], ctx: ProtocolContext
    ) -> asyncio.Task[bytes]:
        async def read() -> bytes:
            async with ctx.activity.span("storage.reader.read"):
                return await self.io(function)

        task = self.start_job("reader", read, ctx)
        owned = cast("asyncio.Task[object]", task)
        self._read_jobs.add(owned)
        owned.add_done_callback(self._read_jobs.discard)
        return task

    async def begin_stage(
        self, operation: StorageOperationRef, spec: StageSpec, ctx: ProtocolContext
    ) -> StageRef:
        async def begin() -> StageRef:
            self.validate_operation(operation, "begin_stage", spec)
            if spec.stage.storage_identity != self.identity():
                refuse("storage_conflict")

            def mutate() -> StageRef:
                with self._state.transaction(write=True) as connection:
                    saved = self._state.receipt(connection, operation)
                    if saved is not None and saved.state == "settled":
                        if not isinstance(saved.result, StageRef):
                            refuse("storage_conflict")
                        return saved.result
                    require_deadline(ctx)
                    self._state.admit_epoch(connection, self._epoch)
                    record = self._state.stage(connection, spec.stage)
                    if record is not None:
                        if record[1] != "open" or record[3] != spec:
                            refuse("storage_conflict")
                    else:
                        self._state.allocate_stage(connection, spec.stage, spec)
                    self._state.register(
                        connection,
                        operation,
                        spec,
                        "stage",
                        spec.stage.stage_id,
                        "begin",
                    )
                    self._state.settle(connection, operation, spec.stage)
                    return spec.stage

            return await self.io(mutate)

        return await self.dispatch("begin_stage", begin, ctx)

    async def write_part(
        self,
        operation: StorageOperationRef,
        stage: StageRef,
        spec: PartSpec,
        stream: StorageByteStream,
        ctx: ProtocolContext,
    ) -> PartRef:
        transferred = False

        async def write() -> PartRef:
            nonlocal transferred
            transferred = True
            try:
                self.validate_operation(
                    operation,
                    "write_part",
                    StageWrite(version=1, stage=stage, spec=spec),
                )
            except KoraProtocolError:
                await stream.aclose()
                raise
            result = await self._staging.write(operation, stage, spec, stream, ctx)
            if not isinstance(result, PartRef):
                refuse("storage_integrity")
            return result

        try:
            return await self.dispatch("write_part", write, ctx)
        finally:
            if not transferred:
                await stream.aclose()

    async def capture_prefix(
        self,
        operation: StorageOperationRef,
        stage: StageRef,
        source_spec: PartSpec,
        count: int,
        stream: StorageByteStream,
        ctx: ProtocolContext,
    ) -> PrefixRef:
        transferred = False

        async def capture() -> PrefixRef:
            nonlocal transferred
            transferred = True
            try:
                if type(count) is not int or not 0 <= count < source_spec.size:
                    refuse("storage_conflict")
                self.validate_operation(
                    operation,
                    "capture_prefix",
                    PrefixCapture(
                        version=1, stage=stage, source_spec=source_spec, count=count
                    ),
                )
            except KoraProtocolError:
                await stream.aclose()
                raise
            result = await self._staging.write(
                operation, stage, source_spec, stream, ctx, count=count
            )
            if not isinstance(result, PrefixRef):
                refuse("storage_integrity")
            return result

        try:
            return await self.dispatch("capture_prefix", capture, ctx)
        finally:
            if not transferred:
                await stream.aclose()

    async def seal_stage(
        self,
        operation: StorageOperationRef,
        stage: StageRef,
        manifest: StageManifest,
        ctx: ProtocolContext,
    ) -> SealedStage:
        async def seal() -> SealedStage:
            if manifest.stage != stage:
                refuse("storage_conflict")
            self.validate_operation(operation, "seal_stage", manifest)
            return await self._staging.seal(operation, stage, manifest, ctx)

        return await self.dispatch("seal_stage", seal, ctx)

    async def dispatch_stream(
        self,
        operation: str,
        open_stream: Callable[[], Awaitable[LocalReadStream]],
        ctx: ProtocolContext,
    ) -> StorageByteStream:
        stream: LocalReadStream | None = None

        async def read() -> StorageByteStream:
            nonlocal stream
            stream = await open_stream()
            return stream

        try:
            return await self.dispatch(operation, read, ctx)
        except BaseException:
            if stream is not None:
                stream.start_close()
            raise

    async def read_stage(
        self, content: SealedStage | PreparedContentRef, ctx: ProtocolContext
    ) -> StorageByteStream:
        return await self.dispatch_stream(
            "read_stage", lambda: self._staging.read(content, ctx), ctx
        )

    async def get(self, content: ContentRef, ctx: ProtocolContext) -> StorageByteStream:
        return await self.dispatch_stream(
            "get", lambda: self._access.open(content, 0, content.total_size, ctx), ctx
        )

    async def get_range(
        self, content: ContentRef, offset: int, length: int, ctx: ProtocolContext
    ) -> StorageByteStream:
        if (
            type(offset) is not int
            or type(length) is not int
            or offset < 0
            or offset > content.total_size
            or not 1 <= length <= MAX_CHUNK_BYTES
            or length > content.total_size - offset
        ):
            refuse("storage_range")
        return await self.dispatch_stream(
            "get_range", lambda: self._access.open(content, offset, length, ctx), ctx
        )

    async def exists(
        self, address: ContentAddress, ctx: ProtocolContext
    ) -> ContentRef | None:
        return await self.dispatch(
            "exists", lambda: self._access.exists(address, ctx), ctx
        )

    async def prepare_content(
        self,
        request: ContentPreparation,
        stream: StorageByteStream,
        ctx: ProtocolContext,
    ) -> PreparedContentRef:
        transferred = False

        async def prepare() -> PreparedContentRef:
            nonlocal transferred
            try:
                self.validate_operation(
                    request.operation, "prepare_content", request.arguments
                )
            except KoraProtocolError:
                await stream.aclose()
                raise
            transferred = True
            return await self._preparer.prepare(request, stream, ctx)

        try:
            return await self.dispatch("prepare_content", prepare, ctx)
        finally:
            if not transferred:
                await stream.aclose()

    async def promote(
        self, request: PromotionRequest, ctx: ProtocolContext
    ) -> PromotionResult:
        async def promote() -> PromotionResult:
            self.validate_operation(request.operation, "promote", request.arguments)
            selected = request.arguments.content
            if (
                selected.stage.storage_identity != self.identity()
                or request.arguments.address.storage_identity != self.identity()
                or request.arguments.address.payload_digest != selected.payload_digest
            ):
                refuse("storage_conflict")
            return await self._publisher.promote(request, ctx)

        return await self.dispatch("promote", promote, ctx)

    async def delete_content(
        self, request: DeleteRequest, ctx: ProtocolContext
    ) -> DeleteResult:
        async def delete() -> DeleteResult:
            self.validate_operation(
                request.operation, "delete_content", request.arguments
            )
            if request.arguments.address.storage_identity != self.identity():
                refuse("storage_conflict")
            return await self._deleter.delete(request, ctx)

        return await self.dispatch("delete_content", delete, ctx, control=True)

    async def release_delete(
        self, request: DeleteReleaseRequest, ctx: ProtocolContext
    ) -> StorageReleaseResult:
        async def release() -> StorageReleaseResult:
            if (
                request.operation.storage_identity != self.identity()
                or request.operation.kind != "delete_content"
            ):
                refuse("storage_conflict")
            return await self._deleter.release(request, ctx)

        return await self.dispatch("release_delete", release, ctx, control=True)

    async def scan(
        self,
        kind: StorageScanKind,
        cursor: StorageScanCursor | None,
        limit: int,
        ctx: ProtocolContext,
    ) -> StoragePage:
        async def read() -> StoragePage:
            measured = ctx.logger.is_enabled(Level.DEBUG)
            result = await self.io(
                lambda: scan(self._state, kind, cursor, limit, measure=measured),
                control=True,
            )
            if result.query_steps is not None:
                ctx.logger.emit(
                    SCAN_WORK,
                    queried_rows=result.queried_rows,
                    query_steps=result.query_steps,
                )
            return result.page

        return await self.dispatch("scan", read, ctx, control=True)

    async def observe(
        self, operation: StorageOperationRef, ctx: ProtocolContext
    ) -> MutationObservation:
        async def observe() -> MutationObservation:
            if operation.storage_identity != self.identity():
                refuse("storage_conflict")

            def read() -> MutationObservation:
                with self._state.transaction() as connection:
                    saved = self._state.receipt(connection, operation)
                    if saved is not None:
                        return saved
                    return MutationObservation(
                        version=1,
                        operation=operation,
                        state="evidence_unavailable",
                        result=None,
                        effects_closed=False,
                    )

            return await self.io(read, control=True)

        return await self.dispatch("observe", observe, ctx, control=True)

    async def retire_stage(
        self, operation: StorageOperationRef, stage: StageRef, ctx: ProtocolContext
    ) -> StageRetirement:
        async def retire() -> StageRetirement:
            self.validate_operation(operation, "retire_stage", stage)

            def close_admission() -> tuple[int, int] | StageRetirement:
                with self._state.transaction(write=True) as connection:
                    saved = self._state.receipt(connection, operation)
                    if saved is not None and saved.state == "settled":
                        if not isinstance(saved.result, StageRetirement):
                            refuse("storage_conflict")
                        return saved.result
                    record = self._state.stage(connection, stage)
                    if record is None:
                        if self._state.epoch(connection) == MAX_COUNT:
                            return StageRetirement(
                                version=1,
                                stage=stage,
                                operation=operation,
                                closed_to_mutations=True,
                                completed=True,
                                remaining_bytes=0,
                                remaining_effects=0,
                            )
                        self._state.admit_epoch(connection, self._epoch)
                        self._state.allocate_stage(connection, stage, None)
                        record = self._state.stage(connection, stage)
                        if record is None:
                            refuse("storage_integrity")
                    self._state.register(
                        connection, operation, stage, "stage", stage.stage_id, "retire"
                    )
                    connection.execute(
                        "UPDATE stages SET state='retiring' WHERE stage_id=?",
                        (stage.stage_id,),
                    )
                    return record[0], record[2]

            admitted = await self.io(close_admission, control=True)
            if isinstance(admitted, StageRetirement):
                return admitted
            slot, remaining = admitted
            ownership = await self.io(
                lambda: self._root.try_lock(f"stage-{slot}"),
                control=True,
                on_cancel=close_lease,
            )
            if ownership is None:
                return StageRetirement(
                    version=1,
                    stage=stage,
                    operation=operation,
                    closed_to_mutations=True,
                    completed=False,
                    remaining_bytes=remaining,
                    remaining_effects=1,
                )
            with ownership:
                async with ctx.activity.span("storage.retire_stage.exclusive"):
                    await self._publisher.recover(stage, slot, ctx)
                    await self._staging.cleanup(stage, slot)

                    def finish() -> StageRetirement:
                        with self._state.transaction(write=True) as connection:
                            result = StageRetirement(
                                version=1,
                                stage=stage,
                                operation=operation,
                                closed_to_mutations=True,
                                completed=True,
                                remaining_bytes=0,
                                remaining_effects=0,
                            )
                            connection.execute(
                                "UPDATE stages SET state='retired', reserved=0 WHERE "
                                "stage_id=?",
                                (stage.stage_id,),
                            )
                            self._state.settle(connection, operation, result)
                            self._state.mutation_set(connection, stage)
                            return result

                    return await self.io(finish, control=True)

        return await self.dispatch("retire_stage", retire, ctx, control=True)

    async def release_stage(
        self, request: StageReleaseRequest, ctx: ProtocolContext
    ) -> StorageReleaseResult:
        async def release() -> StorageReleaseResult:
            def locate() -> int | None:
                with self._state.transaction() as connection:
                    record = self._state.stage(connection, request.stage)
                    if record is None:
                        return None
                    return record[0]

            slot = await self.io(locate, control=True)
            if slot is None:
                return StorageReleaseResult(version=1, state="released")
            ownership = await self.io(
                lambda: self._root.try_lock(f"stage-{slot}"),
                control=True,
                on_cancel=close_lease,
            )
            if ownership is None:
                return StorageReleaseResult(version=1, state="pending")
            with ownership:
                async with ctx.activity.span("storage.release_stage.exclusive"):

                    def compact() -> StorageReleaseResult:
                        with self._state.transaction(write=True) as connection:
                            record = self._state.stage(connection, request.stage)
                            if record is None:
                                return StorageReleaseResult(version=1, state="released")
                            if (
                                record[0] != slot
                                or record[1] != "retired"
                                or record[2] != 0
                            ):
                                refuse("storage_conflict")
                            mutation_set = self._state.mutation_set(
                                connection, request.stage
                            )
                            if (
                                mutation_set.metadata_digest()
                                != request.mutation_set_digest
                            ):
                                refuse("storage_conflict")
                            current = self._state.epoch(connection)
                            if current < MAX_COUNT:
                                connection.execute(
                                    "UPDATE root SET epoch=? WHERE singleton=1",
                                    (current + 1,),
                                )
                            connection.execute(
                                "DELETE FROM receipts WHERE owner_kind='stage' AND "
                                "owner_key=?",
                                (request.stage.stage_id,),
                            )
                            connection.execute(
                                "DELETE FROM stages WHERE stage_id=?",
                                (request.stage.stage_id,),
                            )
                            return StorageReleaseResult(version=1, state="released")

                    return await self.io(compact, control=True)

        return await self.dispatch("release_stage", release, ctx, control=True)

    async def aclose(self) -> None:
        if self._close_task is None:
            self._closing = True

            async def close() -> None:
                jobs = tuple(self._jobs)
                for job in jobs:
                    if job not in self._read_jobs and not job.cancelling():
                        job.cancel()
                try:
                    results = await asyncio.gather(
                        *jobs, self._readers.aclose(), return_exceptions=True
                    )
                    readers_closed = results[-1]
                    if isinstance(readers_closed, BaseException):
                        raise readers_closed
                finally:
                    try:
                        self._payload.shutdown(wait=True, cancel_futures=False)
                    finally:
                        try:
                            self._control.shutdown(wait=True, cancel_futures=False)
                        finally:
                            if (
                                self._readers.streams
                                or self._native
                                or any(not job.done() for job in self._jobs)
                            ):
                                refuse("storage_io")
                            self._root.close()

            self._close_task = asyncio.create_task(close())
        await asyncio.shield(self._close_task)


def create_provider(configuration: JsonObject) -> LocalArtifactStorage:
    parsed = LocalStorageConfiguration.model_validate(configuration)
    return LocalArtifactStorage(parsed)


def provider_metadata() -> ProviderMetadata:
    return ProviderMetadata(
        ProtocolName.ARTIFACT_STORAGE, "local", create_provider, CONFIG_SCHEMA, "1"
    )
