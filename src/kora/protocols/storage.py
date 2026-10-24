from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol, Self

from kora.ir.json import JsonObject
from kora.protocols.common import ProtocolContext
from kora.protocols.storage_models import (
    ContentAddress,
    ContentPreparation,
    ContentRef,
    DeleteReleaseRequest,
    DeleteRequest,
    DeleteResult,
    MutationObservation,
    PartRef,
    PartSpec,
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
    StorageIdentity,
    StorageOperationRef,
    StoragePage,
    StorageReleaseResult,
    StorageScanCursor,
    StorageScanKind,
)


@dataclass(frozen=True)
class ArtifactStorageCapabilities:
    pass


class StorageByteStream(Protocol):
    async def read(self, maximum: int) -> bytes: ...

    def __aiter__(self) -> AsyncIterator[bytes]: ...

    async def __anext__(self) -> bytes: ...

    async def aclose(self) -> None: ...

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: object,
    ) -> None: ...


class ArtifactStorage(Protocol):
    name: str
    config_schema: JsonObject

    def capabilities(self) -> ArtifactStorageCapabilities: ...

    def identity(self) -> StorageIdentity: ...

    async def begin_stage(
        self, operation: StorageOperationRef, spec: StageSpec, ctx: ProtocolContext
    ) -> StageRef: ...

    async def write_part(
        self,
        operation: StorageOperationRef,
        stage: StageRef,
        spec: PartSpec,
        stream: StorageByteStream,
        ctx: ProtocolContext,
    ) -> PartRef: ...

    async def capture_prefix(
        self,
        operation: StorageOperationRef,
        stage: StageRef,
        source_spec: PartSpec,
        count: int,
        stream: StorageByteStream,
        ctx: ProtocolContext,
    ) -> PrefixRef: ...

    async def seal_stage(
        self,
        operation: StorageOperationRef,
        stage: StageRef,
        manifest: StageManifest,
        ctx: ProtocolContext,
    ) -> SealedStage: ...

    async def read_stage(
        self, content: SealedStage | PreparedContentRef, ctx: ProtocolContext
    ) -> StorageByteStream: ...

    async def prepare_content(
        self,
        request: ContentPreparation,
        stream: StorageByteStream,
        ctx: ProtocolContext,
    ) -> PreparedContentRef: ...

    async def promote(
        self, request: PromotionRequest, ctx: ProtocolContext
    ) -> PromotionResult: ...

    async def observe(
        self, operation: StorageOperationRef, ctx: ProtocolContext
    ) -> MutationObservation: ...

    async def retire_stage(
        self, operation: StorageOperationRef, stage: StageRef, ctx: ProtocolContext
    ) -> StageRetirement: ...

    async def get_range(
        self, content: ContentRef, offset: int, length: int, ctx: ProtocolContext
    ) -> StorageByteStream: ...

    async def get(
        self, content: ContentRef, ctx: ProtocolContext
    ) -> StorageByteStream: ...

    async def exists(
        self, address: ContentAddress, ctx: ProtocolContext
    ) -> ContentRef | None: ...

    async def delete_content(
        self, request: DeleteRequest, ctx: ProtocolContext
    ) -> DeleteResult: ...

    async def release_stage(
        self, request: StageReleaseRequest, ctx: ProtocolContext
    ) -> StorageReleaseResult: ...

    async def release_delete(
        self, request: DeleteReleaseRequest, ctx: ProtocolContext
    ) -> StorageReleaseResult: ...

    async def scan(
        self,
        kind: StorageScanKind,
        cursor: StorageScanCursor | None,
        limit: int,
        ctx: ProtocolContext,
    ) -> StoragePage: ...

    async def aclose(self) -> None: ...
