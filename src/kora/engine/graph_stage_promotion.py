from dataclasses import replace
from typing import Never, Self

from kora.db.graph_deployment_stage_reads import (
    PAYLOAD_DIGEST_WIDTH,
    STORE_ID_WIDTH,
    GraphStage,
)
from kora.db.identifiers import is_identifier
from kora.db.transactions import raise_private_metadata_error
from kora.ir.config import COMMAND_RESOURCE_MAX_BYTES
from kora.protocols.identity_records import MAX_COUNT
from kora.protocols.storage_models import (
    MAX_STORAGE_METADATA,
    ContentRef,
    MutationObservation,
    PreparedContentRef,
    PromotionRequest,
    PromotionResult,
    StorageOperationRef,
)

_CHECKED_PROMOTION = object()


class GraphStagePromotionRejected(RuntimeError):
    def __init__(self) -> None:
        super().__init__("graph stage promotion evidence rejected")


class CheckedGraphStagePromotion:
    __slots__ = ("_stage", "_generation", "_result")
    _stage: GraphStage
    _generation: int
    _result: PromotionResult

    def __new__(
        cls, issuer: object, stage: GraphStage, generation: int, result: PromotionResult
    ) -> Self:
        if issuer is not _CHECKED_PROMOTION:
            raise TypeError("graph stage promotion requires checked evidence")
        checked = super().__new__(cls)
        object.__setattr__(checked, "_stage", stage)
        object.__setattr__(checked, "_generation", generation)
        object.__setattr__(checked, "_result", result)
        return checked

    @property
    def stage(self) -> GraphStage:
        return replace(self._stage)

    @property
    def content_generation(self) -> int:
        return self._generation

    @property
    def operation(self) -> StorageOperationRef:
        return self._result.operation.model_copy(deep=True)

    @property
    def reference(self) -> ContentRef:
        return self._result.content.model_copy(deep=True)

    @property
    def created_object(self) -> bool:
        return self._result.created_object

    def __setattr__(self, name: str, value: object) -> Never:
        raise AttributeError("checked graph stage promotion is immutable")

    def __delattr__(self, name: str) -> Never:
        raise AttributeError("checked graph stage promotion is immutable")

    def __getstate__(self) -> Never:
        raise TypeError("checked graph stage promotion requires explicit access")


def _require_stage_shape(stage: GraphStage, content_generation: int) -> None:
    if (
        type(stage) is not GraphStage
        or any(
            type(identifier) is not str or not is_identifier(identifier)
            for identifier in (stage.stage_id, stage.namespace_id, stage.preparation_id)
        )
        or type(stage.store_id) is not str
        or not 1 <= len(stage.store_id) <= STORE_ID_WIDTH
        or type(stage.payload_digest) is not str
        or len(stage.payload_digest) != PAYLOAD_DIGEST_WIDTH
        or type(stage.fence_incarnation) is not int
        or not 0 < stage.fence_incarnation <= MAX_COUNT
        or type(stage.total_size) is not int
        or not 0 <= stage.total_size <= COMMAND_RESOURCE_MAX_BYTES
        or type(content_generation) is not int
        or not 0 <= content_generation <= MAX_COUNT
    ):
        raise GraphStagePromotionRejected()


def _require_request_binding(
    stage: GraphStage, content_generation: int, request: PromotionRequest
) -> None:
    operation, arguments = request.operation, request.arguments
    address, content = arguments.address, arguments.content
    source = content.source if isinstance(content, PreparedContentRef) else content
    if (
        arguments.content_generation != content_generation
        or operation.storage_identity != address.storage_identity
        or address.storage_identity.store_id != stage.store_id
        or address.payload_digest != stage.payload_digest
        or content.stage.storage_identity != address.storage_identity
        or content.stage.stage_id != stage.stage_id
        or content.total_size != stage.total_size
        or content.payload_digest != stage.payload_digest
        or source.stage != content.stage
        or source.total_size != stage.total_size
        or source.payload_digest != stage.payload_digest
        or not source.source_complete
        or (
            isinstance(content, PreparedContentRef)
            and (
                content.operation.kind != "prepare_content"
                or content.operation.storage_identity != content.stage.storage_identity
            )
        )
        or not operation.matches("promote", arguments)
    ):
        raise GraphStagePromotionRejected()


def _verify(
    stage: GraphStage,
    content_generation: int,
    request_bytes: bytes,
    observation_bytes: bytes,
) -> CheckedGraphStagePromotion:
    _require_stage_shape(stage, content_generation)
    if any(
        type(encoded) is not bytes or len(encoded) > MAX_STORAGE_METADATA
        for encoded in (request_bytes, observation_bytes)
    ):
        raise GraphStagePromotionRejected()
    snapshot = replace(stage)
    request = PromotionRequest.model_validate_json(request_bytes)
    _require_request_binding(snapshot, content_generation, request)
    observation = MutationObservation.model_validate_json(observation_bytes)
    result = observation.result
    if (
        observation.state != "settled"
        or not observation.effects_closed
        or observation.operation != request.operation
        or type(result) is not PromotionResult
        or result.operation != request.operation
        or result.content.address != request.arguments.address
        or result.content.total_size != snapshot.total_size
    ):
        raise GraphStagePromotionRejected()
    return CheckedGraphStagePromotion(
        _CHECKED_PROMOTION, snapshot, content_generation, result
    )


def verify_graph_stage_promotion(
    stage: GraphStage,
    content_generation: int,
    request_bytes: bytes,
    observation_bytes: bytes,
) -> CheckedGraphStagePromotion:
    try:
        return _verify(stage, content_generation, request_bytes, observation_bytes)
    except (GraphStagePromotionRejected, ValueError):
        del stage, content_generation, request_bytes, observation_bytes
    raise_private_metadata_error(GraphStagePromotionRejected())
