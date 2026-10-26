import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Self

from kora.protocols.common import ProtocolContext
from kora.protocols.storage import ArtifactStorage
from kora.protocols.storage_models import (
    ManifestPart,
    MutationReceipt,
    PartSpec,
    SealedStage,
    StageManifest,
    StageRef,
    StageReleaseRequest,
    StageSpec,
    StageWrite,
    StorageIdentity,
    StorageModel,
    StorageMutationSet,
    StorageOperationKind,
    StorageOperationRef,
)


class BytesSource:
    def __init__(self, payload: bytes, chunk_size: int = 65536) -> None:
        self.payload = payload
        self.chunk_size = chunk_size
        self.offset = 0
        self.closed = False
        self.calls = 0

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self

    async def __anext__(self) -> bytes:
        chunk = await self.read(self.chunk_size)
        if not chunk:
            raise StopAsyncIteration
        return chunk

    async def read(self, maximum: int) -> bytes:
        self.calls += 1
        if self.closed or self.offset == len(self.payload):
            return b""
        chunk = self.payload[self.offset : self.offset + min(maximum, self.chunk_size)]
        self.offset += len(chunk)
        return chunk

    async def aclose(self) -> None:
        self.closed = True

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: object,
    ) -> None:
        await self.aclose()


def digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def stage_spec(identity: StorageIdentity, stage_id: str, payload: bytes) -> StageSpec:
    return StageSpec(
        version=1,
        stage=StageRef(version=1, storage_identity=identity, stage_id=stage_id),
        payload_digest=digest(payload),
        total_size=len(payload),
        content_type="application/octet-stream",
    )


def operation(
    identity: StorageIdentity,
    operation_id: str,
    kind: StorageOperationKind,
    arguments: StorageModel,
) -> StorageOperationRef:
    return StorageOperationRef.for_arguments(identity, operation_id, kind, arguments)


def single_part(payload: bytes) -> PartSpec:
    return PartSpec(
        version=1,
        index=0,
        byte_offset=0,
        size=len(payload),
        digest=digest(payload),
        final=True,
    )


def single_manifest(
    stage: StageRef, part: PartSpec, part_identity: str
) -> StageManifest:
    return StageManifest(
        version=1,
        stage=stage,
        parts=(ManifestPart(version=1, spec=part, part_identity=part_identity),),
        prefix=None,
    )


async def seal_bytes(
    provider: ArtifactStorage,
    stage_id: str,
    payload: bytes,
    context: ProtocolContext,
) -> tuple[SealedStage, StageManifest, StorageOperationRef]:
    spec = stage_spec(provider.identity(), stage_id, payload)
    stage = await provider.begin_stage(
        operation(provider.identity(), stage_id + "-begin", "begin_stage", spec),
        spec,
        context,
    )
    part_spec = single_part(payload)
    part = await provider.write_part(
        operation(
            provider.identity(),
            stage_id + "-part",
            "write_part",
            StageWrite(version=1, stage=stage, spec=part_spec),
        ),
        stage,
        part_spec,
        BytesSource(payload),
        context,
    )
    manifest = single_manifest(stage, part_spec, part.part_identity)
    seal = operation(provider.identity(), stage_id + "-seal", "seal_stage", manifest)
    return await provider.seal_stage(seal, stage, manifest, context), manifest, seal


class RepeatedSource(BytesSource):
    def __init__(self, size: int) -> None:
        super().__init__(b"k" * min(size, 65536))
        self.size = size

    async def read(self, maximum: int) -> bytes:
        self.calls += 1
        if self.closed or self.offset == self.size:
            return b""
        count = min(maximum, self.chunk_size, self.size - self.offset)
        self.offset += count
        return self.payload[:count]


def repeated_digest(size: int) -> str:
    state = hashlib.sha256()
    chunk = b"k" * 65536
    for offset in range(0, size, 65536):
        state.update(chunk[: min(65536, size - offset)])
    return "sha256:" + state.hexdigest()


@dataclass(frozen=True)
class SealedUpload:
    content: SealedStage
    operations: tuple[StorageOperationRef, ...]


async def seal_repeated(
    provider: ArtifactStorage, stage_id: str, size: int, context: ProtocolContext
) -> SealedUpload:
    spec = StageSpec(
        version=1,
        stage=StageRef(
            version=1, storage_identity=provider.identity(), stage_id=stage_id
        ),
        payload_digest=repeated_digest(size),
        total_size=size,
        content_type="application/octet-stream",
    )
    begin = operation(provider.identity(), stage_id + "-begin", "begin_stage", spec)
    stage = await provider.begin_stage(begin, spec, context)
    operations = [begin]
    parts: list[ManifestPart] = []
    for index, offset in enumerate(range(0, max(1, size), 16777216)):
        count = min(16777216, size - offset)
        part = PartSpec(
            version=1,
            index=index,
            byte_offset=offset,
            size=count,
            digest=repeated_digest(count),
            final=offset + count == size,
        )
        write = operation(
            provider.identity(),
            f"{stage_id}-part-{index}",
            "write_part",
            StageWrite(version=1, stage=stage, spec=part),
        )
        saved = await provider.write_part(
            write, stage, part, RepeatedSource(count), context
        )
        operations.append(write)
        parts.append(
            ManifestPart(version=1, spec=part, part_identity=saved.part_identity)
        )
    manifest = StageManifest(version=1, stage=stage, parts=tuple(parts), prefix=None)
    seal = operation(provider.identity(), stage_id + "-seal", "seal_stage", manifest)
    content = await provider.seal_stage(seal, stage, manifest, context)
    return SealedUpload(content, (*operations, seal))


async def acknowledge_stage(
    provider: ArtifactStorage,
    stage: StageRef,
    operations: tuple[StorageOperationRef, ...],
    context: ProtocolContext,
) -> StageReleaseRequest:
    retire = operation(
        provider.identity(), stage.stage_id + "-retire", "retire_stage", stage
    )
    retired = await provider.retire_stage(retire, stage, context)
    if not retired.completed:
        raise AssertionError("actual stage cleanup must complete before acknowledgment")
    receipts: list[MutationReceipt] = []
    for issued in (*operations, retire):
        observed = await provider.observe(issued, context)
        if (
            observed.state != "settled"
            or observed.result is None
            or not observed.effects_closed
        ):
            raise AssertionError(
                "every issued outcome must be persisted before acknowledgment"
            )
        receipts.append(
            MutationReceipt(
                version=1,
                operation_id=issued.operation_id,
                kind=issued.kind,
                request_digest=issued.request_digest,
                outcome_digest=observed.result.metadata_digest(),
            )
        )
    acknowledged = StorageMutationSet(
        version=1,
        stage=stage,
        receipts=tuple(sorted(receipts, key=lambda item: item.operation_id)),
    )
    return StageReleaseRequest(
        version=1,
        stage=stage,
        persisted_outcome_revision="persisted-storage-outcome",
        mutation_set_digest=acknowledged.metadata_digest(),
        issuance_closed="all-storage-issuers-closed",
    )
