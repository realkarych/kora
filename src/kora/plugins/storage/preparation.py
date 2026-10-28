import asyncio
import hashlib
from functools import partial

from kora.plugins.storage.errors import refuse
from kora.plugins.storage.payload import (
    FileRunner,
    PayloadFiles,
    PrivateFile,
    exclusive_lease,
    raw_content_identity,
)
from kora.plugins.storage.staging import StagedPayloads, verify_source
from kora.plugins.storage.state import LocalState
from kora.protocols.common import ProtocolContext
from kora.protocols.storage import StorageByteStream
from kora.protocols.storage_models import (
    MAX_CHUNK_BYTES,
    ContentPreparation,
    PreparedContentRef,
)


class ContentPreparer:
    def __init__(
        self, state: LocalState, staging: StagedPayloads, runner: FileRunner
    ) -> None:
        self.state = state
        self.root = state.root
        self.staging = staging
        self.runner = runner
        self.files = PayloadFiles(self.root)

    def admit(
        self, request: ContentPreparation, slot: int, ctx: ProtocolContext
    ) -> PrivateFile | PreparedContentRef:
        with self.state.transaction(write=True) as connection:
            saved = self.state.settled_receipt(
                connection, request.operation, PreparedContentRef
            )
            if saved is not None:
                return saved
            self.state.admit(connection, ctx)
            checked_slot, _ = self.staging.resolve(connection, request.arguments.source)
            if checked_slot != slot:
                refuse("storage_conflict")
            stage = request.arguments.source.stage
            observations = self.state.stage_observations(connection, stage)
            if any(semantic == "promote" for semantic, _, _ in observations):
                refuse("storage_conflict")
            retained = self.state.retained_stage_bytes(connection, stage)
            reserved = retained + max(retained, request.arguments.maximum_output_size)
            self.state.register(
                connection,
                request.operation,
                request.arguments,
                "stage",
                stage.stage_id,
                "prepare",
            )
            self.state.reserve_stage(connection, stage, reserved)
            return self.state.ensure_file_identity(connection, request.operation)

    async def replay(
        self, result: PreparedContentRef, source: StorageByteStream
    ) -> PreparedContentRef:
        await verify_source(source, result.total_size, result.payload_digest, False)
        return result

    async def prepare(
        self,
        request: ContentPreparation,
        source: StorageByteStream,
        ctx: ProtocolContext,
    ) -> PreparedContentRef:
        try:
            located = await self.runner(
                lambda: self.staging.lookup_content(
                    request.operation,
                    request.arguments.source,
                    PreparedContentRef,
                    ctx,
                )
            )
            if isinstance(located, PreparedContentRef):
                return await self.replay(located, source)
            slot = located
            ownership = await exclusive_lease(self.runner, self.root, f"stage-{slot}")
            with ownership:
                admitted = await self.runner(lambda: self.admit(request, slot, ctx))
                if isinstance(admitted, PreparedContentRef):
                    return await self.replay(admitted, source)
                async with ctx.activity.span("storage.prepare_content.registered"):
                    writer, identity = await self.runner(
                        lambda: self.state.open_writer(
                            slot, "prepare", request.operation, admitted
                        ),
                        on_cancel=lambda result: result[0].close(),
                    )
                    try:
                        digest = hashlib.sha256()
                        size = 0
                        maximum = request.arguments.maximum_output_size
                        raw = request.arguments.source
                        expected_alias = (
                            request.arguments.expected_output_digest
                            == raw.payload_digest
                            and maximum == raw.total_size
                        )
                        while True:
                            bound = min(MAX_CHUNK_BYTES, maximum - size + 1)
                            chunk = await source.read(bound)
                            if type(chunk) is not bytes or len(chunk) > bound:
                                refuse("storage_conflict")
                            if not chunk:
                                break
                            size += len(chunk)
                            if size > maximum:
                                refuse("storage_conflict")
                            digest.update(chunk)
                            if expected_alias:
                                await asyncio.sleep(0)
                            else:
                                await self.runner(
                                    partial(self.files.write, writer, chunk)
                                )
                        actual_digest = "sha256:" + digest.hexdigest()
                        if (
                            request.arguments.expected_output_digest is not None
                            and actual_digest
                            != request.arguments.expected_output_digest
                        ):
                            refuse("storage_conflict")
                        if expected_alias and size != raw.total_size:
                            refuse("storage_conflict")
                        await self.runner(lambda: self.files.finish(writer, slot))
                        identical = (
                            size == raw.total_size
                            and actual_digest == raw.payload_digest
                        )
                        result = PreparedContentRef(
                            version=1,
                            kind="prepared",
                            stage=request.arguments.source.stage,
                            operation=request.operation,
                            source=request.arguments.source,
                            total_size=size,
                            payload_digest=actual_digest,
                            content_identity=(
                                raw_content_identity(raw)
                                if identical
                                else identity.private_identity
                            ),
                        )
                        async with ctx.activity.span(
                            "storage.prepare_content.effect_persisted"
                        ):

                            def settle() -> None:
                                with self.state.transaction(write=True) as connection:
                                    self.state.settle(
                                        connection, request.operation, result
                                    )

                            await self.runner(settle)
                        if identical:
                            await self.runner(
                                lambda: self.files.discard(slot, "prepare", identity)
                            )
                        return result
                    finally:
                        writer.close()
        finally:
            await source.aclose()
