import hashlib
import json
from typing import Annotated, Final, Literal, Self, cast

from pydantic import (
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)
from pydantic.config import ExtraValues
from pydantic_core import PydanticCustomError

from kora.ir.json import MAX_INTEGER_BITS, JsonObject, JsonShapeError, parse_json_object
from kora.ir.scalars import Digest
from kora.ir.vcs_ref_json import canonical_bytes as canonical_json
from kora.ir.vcs_ref_json import lexical_depth_valid, refuse_constant, unique_object
from kora.protocols.common import BoundaryModel
from kora.protocols.identity import Count, Identifier

MAX_STORAGE_METADATA: Final[int] = 16384
MAX_PAYLOAD_BYTES: Final[int] = 268435456
MAX_PARTS: Final[int] = 16
MAX_CHUNK_BYTES: Final[int] = 65536

StorageOperationKind = Literal[
    "begin_stage",
    "write_part",
    "capture_prefix",
    "seal_stage",
    "prepare_content",
    "promote",
    "retire_stage",
    "delete_content",
]
StorageScanKind = Literal["stages", "content"]
PayloadSize = Annotated[StrictInt, Field(ge=0, le=MAX_PAYLOAD_BYTES)]


def storage_json_integer(encoded: str) -> int:
    value = int(encoded)
    if value.bit_length() > MAX_INTEGER_BITS:
        raise ValueError("storage metadata integer exceeds its JSON boundary")
    return value


class StorageModel(BoundaryModel):
    version: Literal[1]

    @field_validator("version", mode="before")
    @classmethod
    def strict_version(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise PydanticCustomError("storage_version", "storage version must be 1")
        return value

    @model_validator(mode="after")
    def finite_json(self) -> Self:
        try:
            parse_json_object(
                self.model_dump(mode="json"), max_bytes=MAX_STORAGE_METADATA
            )
        except (JsonShapeError, UnicodeError):
            raise PydanticCustomError(
                "storage_metadata", "storage metadata exceeds its JSON boundary"
            ) from None
        return self

    @classmethod
    def model_validate_json(
        cls,
        json_data: str | bytes | bytearray,
        *,
        strict: bool | None = None,
        extra: ExtraValues | None = None,
        context: object | None = None,
        by_alias: bool | None = None,
        by_name: bool | None = None,
    ) -> Self:
        if len(json_data) > MAX_STORAGE_METADATA:
            raise ValueError("storage metadata exceeds its JSON boundary")
        try:
            encoded = (
                json_data.encode("utf-8")
                if isinstance(json_data, str)
                else bytes(json_data)
            )
            if len(encoded) > MAX_STORAGE_METADATA or not lexical_depth_valid(encoded):
                raise ValueError("storage metadata exceeds its JSON boundary")
            value: object = json.loads(
                encoded.decode("utf-8"),
                object_pairs_hook=unique_object,
                parse_constant=refuse_constant,
                parse_int=storage_json_integer,
            )
            if not isinstance(value, dict):
                raise ValueError("storage metadata must be a JSON object")
            canonical = canonical_json(cast("JsonObject", value))
            if len(canonical) > MAX_STORAGE_METADATA:
                raise ValueError("storage metadata exceeds its JSON boundary")
        except (ValueError, RecursionError):
            raise ValueError("invalid storage JSON metadata") from None
        return cls.model_validate(
            value,
            strict=strict,
            extra=extra,
            context=context,
            by_alias=by_alias,
            by_name=by_name,
        )

    def canonical_bytes(self) -> bytes:
        return canonical_json(self.model_dump(mode="json"))

    def metadata_digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.canonical_bytes()).hexdigest()


class StorageIdentity(StorageModel):
    store_id: Identifier


class ContentAddress(StorageModel):
    storage_identity: StorageIdentity
    payload_digest: Digest


class StageRef(StorageModel):
    storage_identity: StorageIdentity
    stage_id: Identifier


class StageSpec(StorageModel):
    stage: StageRef
    payload_digest: Digest
    total_size: Count
    content_type: Annotated[StrictStr, Field(min_length=1, max_length=4096)]


class PartSpec(StorageModel):
    index: Annotated[StrictInt, Field(ge=0, lt=MAX_PARTS)]
    byte_offset: PayloadSize
    size: PayloadSize
    digest: Digest
    final: StrictBool

    @model_validator(mode="after")
    def finite_extent(self) -> Self:
        if self.byte_offset + self.size > MAX_PAYLOAD_BYTES:
            raise PydanticCustomError(
                "storage_extent", "part extent exceeds payload bound"
            )
        if not self.final and self.size == 0:
            raise PydanticCustomError(
                "storage_extent", "nonfinal parts must have bytes"
            )
        return self


def require_strict_cut(count: int, source_spec: PartSpec) -> None:
    if count >= source_spec.size:
        raise PydanticCustomError(
            "storage_prefix", "prefix must be a strict source cut"
        )


class PartRef(StorageModel):
    stage: StageRef
    spec: PartSpec
    part_identity: Identifier


class PrefixRef(StorageModel):
    stage: StageRef
    source_spec: PartSpec
    count: PayloadSize
    digest: Digest
    private_identity: Identifier
    source_complete: Literal[False]

    @field_validator("source_complete", mode="before")
    @classmethod
    def incomplete_source(cls, value: object) -> object:
        if value is not False:
            raise PydanticCustomError("storage_prefix", "prefix source is incomplete")
        return value

    @model_validator(mode="after")
    def bounded_prefix(self) -> Self:
        require_strict_cut(self.count, self.source_spec)
        return self


class ManifestPart(StorageModel):
    spec: PartSpec
    part_identity: Identifier


class ManifestPrefix(StorageModel):
    source_spec: PartSpec
    count: PayloadSize
    digest: Digest
    private_identity: Identifier

    @model_validator(mode="after")
    def bounded_prefix(self) -> Self:
        require_strict_cut(self.count, self.source_spec)
        return self


class StageManifest(StorageModel):
    stage: StageRef
    parts: Annotated[tuple[ManifestPart, ...], Field(max_length=MAX_PARTS)]
    prefix: ManifestPrefix | None

    @model_validator(mode="after")
    def contiguous_sequence(self) -> Self:
        offset = 0
        for index, part in enumerate(self.parts):
            if part.spec.index != index or part.spec.byte_offset != offset:
                raise PydanticCustomError(
                    "storage_manifest", "manifest must be contiguous"
                )
            if part.spec.final and (
                index + 1 != len(self.parts) or self.prefix is not None
            ):
                raise PydanticCustomError(
                    "storage_manifest", "final part must terminate the source"
                )
            offset += part.spec.size
        if self.prefix is not None:
            if (
                len(self.parts) >= MAX_PARTS
                or self.prefix.source_spec.index != len(self.parts)
                or self.prefix.source_spec.byte_offset != offset
            ):
                raise PydanticCustomError(
                    "storage_manifest", "prefix must continue the source"
                )
        elif not self.parts or not self.parts[-1].spec.final:
            raise PydanticCustomError(
                "storage_manifest", "complete source requires a final part"
            )
        return self


class SealedStage(StorageModel):
    kind: Literal["sealed"]
    stage: StageRef
    manifest_digest: Digest
    total_size: PayloadSize
    payload_digest: Digest
    source_complete: StrictBool
    seal_identity: Identifier


class StorageOperationRef(StorageModel):
    storage_identity: StorageIdentity
    operation_id: Identifier
    kind: StorageOperationKind
    request_digest: Digest

    @classmethod
    def for_arguments(
        cls,
        identity: StorageIdentity,
        operation_id: str,
        kind: StorageOperationKind,
        arguments: StorageModel,
    ) -> Self:
        return cls(
            version=1,
            storage_identity=identity,
            operation_id=operation_id,
            kind=kind,
            request_digest=request_digest(identity, operation_id, kind, arguments),
        )

    def matches(self, kind: StorageOperationKind, arguments: StorageModel) -> bool:
        return self.kind == kind and self.request_digest == request_digest(
            self.storage_identity, self.operation_id, kind, arguments
        )


def request_digest(
    identity: StorageIdentity,
    operation_id: str,
    kind: StorageOperationKind,
    arguments: StorageModel,
) -> str:
    preimage: JsonObject = {
        "version": 1,
        "storage_identity": identity.model_dump(mode="json"),
        "operation_id": operation_id,
        "kind": kind,
        "arguments": arguments.model_dump(mode="json"),
    }
    return "sha256:" + hashlib.sha256(canonical_json(preimage)).hexdigest()


class StageWrite(StorageModel):
    stage: StageRef
    spec: PartSpec


class PrefixCapture(StorageModel):
    stage: StageRef
    source_spec: PartSpec
    count: PayloadSize

    @model_validator(mode="after")
    def strict_cut(self) -> Self:
        require_strict_cut(self.count, self.source_spec)
        return self


class PreparationArguments(StorageModel):
    source: SealedStage
    maximum_output_size: PayloadSize
    expected_output_digest: Digest | None


class ContentPreparation(StorageModel):
    operation: StorageOperationRef
    arguments: PreparationArguments


class PreparedContentRef(StorageModel):
    kind: Literal["prepared"]
    stage: StageRef
    operation: StorageOperationRef
    source: SealedStage
    total_size: PayloadSize
    payload_digest: Digest
    content_identity: Identifier


PublishableContent = Annotated[
    SealedStage | PreparedContentRef, Field(discriminator="kind")
]


class PromotionArguments(StorageModel):
    content: PublishableContent
    address: ContentAddress
    content_generation: Count


class PromotionRequest(StorageModel):
    operation: StorageOperationRef
    arguments: PromotionArguments


class ContentRef(StorageModel):
    address: ContentAddress
    object_incarnation: Identifier
    total_size: PayloadSize


class PromotionResult(StorageModel):
    operation: StorageOperationRef
    content: ContentRef
    created_object: StrictBool


class DeleteArguments(StorageModel):
    address: ContentAddress
    deletion_generation: Count


class DeleteRequest(StorageModel):
    operation: StorageOperationRef
    arguments: DeleteArguments


class DeleteResult(StorageModel):
    operation: StorageOperationRef
    address: ContentAddress
    deletion_generation: Count
    deleted_object: StrictBool


class StageRetirement(StorageModel):
    stage: StageRef
    operation: StorageOperationRef
    closed_to_mutations: StrictBool
    completed: StrictBool
    remaining_bytes: Count
    remaining_effects: Count


class NoEffectFailure(StorageModel):
    operation: StorageOperationRef
    code: Identifier


MutationResult = (
    StageRef
    | PartRef
    | PrefixRef
    | SealedStage
    | PreparedContentRef
    | PromotionResult
    | DeleteResult
    | StageRetirement
    | NoEffectFailure
)


class MutationObservation(StorageModel):
    operation: StorageOperationRef
    state: Literal["pending", "settled", "evidence_unavailable"]
    result: MutationResult | None
    effects_closed: StrictBool

    @model_validator(mode="after")
    def truthful_observation(self) -> Self:
        if self.state == "settled":
            if self.result is None or not self.effects_closed:
                raise PydanticCustomError(
                    "storage_observation", "settled result requires closure"
                )
        elif self.state == "evidence_unavailable" and (
            self.result is not None or self.effects_closed
        ):
            raise PydanticCustomError(
                "storage_observation", "unavailable evidence grants no closure"
            )
        elif self.state == "pending" and self.effects_closed:
            raise PydanticCustomError(
                "storage_observation", "pending effects are not closed"
            )
        return self


class StorageLimits(StorageModel):
    private_bytes: Annotated[StrictInt, Field(ge=1048576, le=17179869184)] = 2147483648
    stage_slots: Annotated[StrictInt, Field(ge=1, le=32)] = 8
    deletion_slots: Annotated[StrictInt, Field(ge=1, le=32)] = 8
    data_work_slots: Annotated[StrictInt, Field(ge=1, le=8)] = 4
    reader_slots: Annotated[StrictInt, Field(ge=1, le=16)] = 8
    control_work_slots: Annotated[StrictInt, Field(ge=2, le=2)] = 2


class MutationReceipt(StorageModel):
    operation_id: Identifier
    kind: StorageOperationKind
    request_digest: Digest
    outcome_digest: Digest


class StorageMutationSet(StorageModel):
    stage: StageRef
    receipts: Annotated[tuple[MutationReceipt, ...], Field(max_length=22)]

    @model_validator(mode="after")
    def ordered_unique_operations(self) -> Self:
        keys = tuple(receipt.operation_id for receipt in self.receipts)
        if keys != tuple(sorted(set(keys))):
            raise PydanticCustomError(
                "storage_mutations", "mutation receipts must be ordered and unique"
            )
        return self


class StageReleaseRequest(StorageModel):
    stage: StageRef
    persisted_outcome_revision: Identifier
    mutation_set_digest: Digest
    issuance_closed: Identifier


class DeleteReleaseRequest(StorageModel):
    operation: StorageOperationRef
    persisted_outcome_revision: Identifier
    issuance_closed: Identifier


class StorageReleaseResult(StorageModel):
    state: Literal["pending", "released"]


class StorageScanCursor(StorageModel):
    storage_identity: StorageIdentity
    kind: StorageScanKind
    last_key: Identifier


class StorageScanEntry(StorageModel):
    key: Identifier
    state: Literal["open", "sealed", "retiring", "retired", "content"]
    size: Count


class StoragePage(StorageModel):
    storage_identity: StorageIdentity
    kind: StorageScanKind
    entries: Annotated[tuple[StorageScanEntry, ...], Field(max_length=128)]
    next_cursor: StorageScanCursor | None
