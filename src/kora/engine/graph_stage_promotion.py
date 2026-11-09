from dataclasses import replace
from typing import Never, Self, TypedDict

from kora.db.graph_deployment_stage_reads import (
    PAYLOAD_DIGEST_WIDTH,
    STORE_ID_WIDTH,
    GraphStage,
)
from kora.db.graph_stage_promotion_intent_reads import GraphStagePromotionIntentScalars
from kora.db.graph_stage_promotion_snapshot import GraphStagePromotionDocuments
from kora.db.identifiers import is_identifier
from kora.db.transactions import raise_private_metadata_error
from kora.ir.config import COMMAND_RESOURCE_MAX_BYTES
from kora.protocols.identity_records import MAX_COUNT
from kora.protocols.storage_models import (
    MAX_STORAGE_METADATA,
    ContentAddress,
    ContentRef,
    MutationObservation,
    NoEffectFailure,
    PreparedContentRef,
    PromotionRequest,
    PromotionResult,
    StorageOperationRef,
)

_CHECKED_PROMOTION = object()
_PREPARED_CUSTODY = object()


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


class PromotionIntentValues(TypedDict):
    stage_id: str
    store_id: str
    payload_digest: str
    fence_incarnation: int
    operation_id: str
    request_digest: str
    content_generation: int
    request_bytes: bytes


class PromotionSettlementValues(TypedDict):
    stage_id: str
    observation_bytes: bytes


class PreparedGraphStagePromotionIntent:
    __slots__ = (
        "_stage",
        "_generation",
        "_operation",
        "_address",
        "_request_bytes",
        "_issuer",
    )
    _stage: GraphStage
    _generation: int
    _operation: StorageOperationRef
    _address: ContentAddress
    _request_bytes: bytes
    _issuer: object

    def __new__(
        cls,
        issuer: object,
        stage: GraphStage,
        generation: int,
        request: PromotionRequest,
        request_bytes: bytes,
    ) -> Self:
        if issuer is not _PREPARED_CUSTODY:
            del issuer, stage, generation, request, request_bytes
            raise TypeError("graph promotion intent requires prepared evidence")
        intent = super().__new__(cls)
        object.__setattr__(intent, "_stage", stage)
        object.__setattr__(intent, "_generation", generation)
        object.__setattr__(intent, "_operation", request.operation)
        object.__setattr__(intent, "_address", request.arguments.address)
        object.__setattr__(intent, "_request_bytes", request_bytes)
        object.__setattr__(intent, "_issuer", issuer)
        return intent

    @property
    def stage_id(self) -> str:
        return self._stage.stage_id

    def values(self) -> PromotionIntentValues:
        return {
            "stage_id": self.stage_id,
            "store_id": self._stage.store_id,
            "payload_digest": self._stage.payload_digest,
            "fence_incarnation": self._stage.fence_incarnation,
            "operation_id": self._operation.operation_id,
            "request_digest": self._operation.request_digest,
            "content_generation": self._generation,
            "request_bytes": self._request_bytes,
        }

    def prepare_settlement(
        self, observation_bytes: bytes
    ) -> "PreparedGraphStagePromotionSettlement":
        try:
            if (
                type(self) is not PreparedGraphStagePromotionIntent
                or not hasattr(self, "_issuer")
                or self._issuer is not _PREPARED_CUSTODY
            ):
                raise GraphStagePromotionRejected()
            return self._prepare_settlement(observation_bytes)
        except (GraphStagePromotionRejected, ValueError) as refused:
            _discard_private_error(refused)
        del self, observation_bytes
        raise_private_metadata_error(GraphStagePromotionRejected())

    def _prepare_settlement(
        self, observation_bytes: bytes
    ) -> "PreparedGraphStagePromotionSettlement":
        _require_document_bytes(observation_bytes)
        observation = MutationObservation.model_validate_json(observation_bytes)
        if observation.canonical_bytes() != observation_bytes:
            raise GraphStagePromotionRejected()
        result = _closed_result(
            self._stage, self._operation, self._address, observation
        )
        promotion = (
            CheckedGraphStagePromotion(
                _CHECKED_PROMOTION, self._stage, self._generation, result
            )
            if isinstance(result, PromotionResult)
            else None
        )
        return PreparedGraphStagePromotionSettlement(
            _PREPARED_CUSTODY,
            self,
            observation_bytes,
            promotion,
            result.metadata_digest(),
        )

    def __setattr__(self, name: str, value: object) -> Never:
        raise AttributeError("prepared graph promotion intent is immutable")

    def __delattr__(self, name: str) -> Never:
        raise AttributeError("prepared graph promotion intent is immutable")

    def __getstate__(self) -> Never:
        raise TypeError("prepared graph promotion intent requires explicit access")


class PreparedGraphStagePromotionSettlement:
    __slots__ = ("_intent", "_observation_bytes", "_promotion", "_outcome_digest")
    _intent: PreparedGraphStagePromotionIntent
    _observation_bytes: bytes
    _promotion: CheckedGraphStagePromotion | None
    _outcome_digest: str

    def __new__(
        cls,
        issuer: object,
        intent: PreparedGraphStagePromotionIntent,
        observation_bytes: bytes,
        promotion: CheckedGraphStagePromotion | None,
        outcome_digest: str,
    ) -> Self:
        if issuer is not _PREPARED_CUSTODY:
            del issuer, intent, observation_bytes, promotion, outcome_digest
            raise TypeError("graph promotion settlement requires prepared evidence")
        settlement = super().__new__(cls)
        object.__setattr__(settlement, "_intent", intent)
        object.__setattr__(settlement, "_observation_bytes", observation_bytes)
        object.__setattr__(settlement, "_promotion", promotion)
        object.__setattr__(settlement, "_outcome_digest", outcome_digest)
        return settlement

    @property
    def promotion(self) -> CheckedGraphStagePromotion | None:
        return self._promotion

    @property
    def outcome_digest(self) -> str:
        return self._outcome_digest

    def belongs_to(self, intent: PreparedGraphStagePromotionIntent) -> bool:
        return (
            type(self) is PreparedGraphStagePromotionSettlement
            and hasattr(self, "_intent")
            and self._intent is intent
        )

    def values(self) -> PromotionSettlementValues:
        return {
            "stage_id": self._intent.stage_id,
            "observation_bytes": self._observation_bytes,
        }

    def __setattr__(self, name: str, value: object) -> Never:
        raise AttributeError("prepared graph promotion settlement is immutable")

    def __delattr__(self, name: str) -> Never:
        raise AttributeError("prepared graph promotion settlement is immutable")

    def __getstate__(self) -> Never:
        raise TypeError("prepared graph promotion settlement requires explicit access")


def _require_document_bytes(document: bytes) -> None:
    if type(document) is not bytes or not 0 < len(document) <= MAX_STORAGE_METADATA:
        raise GraphStagePromotionRejected()


def _discard_private_error(error: BaseException) -> None:
    error.__traceback__ = None
    error.__cause__ = None
    error.__context__ = None


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


def _closed_result(
    stage: GraphStage,
    operation: StorageOperationRef,
    address: ContentAddress,
    observation: MutationObservation,
) -> PromotionResult | NoEffectFailure:
    result = observation.result
    if (
        observation.state != "settled"
        or not observation.effects_closed
        or observation.operation != operation
        or not isinstance(result, (PromotionResult, NoEffectFailure))
        or result.operation != operation
    ):
        raise GraphStagePromotionRejected()
    if isinstance(result, PromotionResult) and (
        result.content.address != address
        or result.content.total_size != stage.total_size
    ):
        raise GraphStagePromotionRejected()
    return result


def _prepare_intent(
    stage: GraphStage, content_generation: int, request_bytes: bytes
) -> PreparedGraphStagePromotionIntent:
    _require_stage_shape(stage, content_generation)
    _require_document_bytes(request_bytes)
    snapshot = replace(stage)
    request = PromotionRequest.model_validate_json(request_bytes)
    _require_request_binding(snapshot, content_generation, request)
    if request.canonical_bytes() != request_bytes:
        raise GraphStagePromotionRejected()
    return PreparedGraphStagePromotionIntent(
        _PREPARED_CUSTODY, snapshot, content_generation, request, request_bytes
    )


def prepare_graph_stage_promotion_intent(
    stage: GraphStage, content_generation: int, request_bytes: bytes
) -> PreparedGraphStagePromotionIntent:
    try:
        return _prepare_intent(stage, content_generation, request_bytes)
    except (GraphStagePromotionRejected, ValueError) as refused:
        _discard_private_error(refused)
    del stage, content_generation, request_bytes
    raise_private_metadata_error(GraphStagePromotionRejected())


def _require_saved_intent_fields(
    native: GraphStagePromotionIntentScalars, prepared: PromotionIntentValues
) -> None:
    if (
        type(native.store_id) is not str
        or native.store_id != prepared["store_id"]
        or type(native.payload_digest) is not str
        or native.payload_digest != prepared["payload_digest"]
        or type(native.fence_incarnation) is not int
        or native.fence_incarnation != prepared["fence_incarnation"]
        or type(native.operation_id) is not str
        or native.operation_id != prepared["operation_id"]
        or type(native.request_digest) is not str
        or native.request_digest != prepared["request_digest"]
    ):
        raise GraphStagePromotionRejected()


def _read_detached_graph_stage_promotion(
    stage: GraphStage, documents: GraphStagePromotionDocuments
) -> (
    tuple[
        PreparedGraphStagePromotionIntent, PreparedGraphStagePromotionSettlement | None
    ]
    | None
):
    if (
        type(stage) is not GraphStage
        or type(stage.stage_id) is not str
        or type(documents) is not GraphStagePromotionDocuments
        or type(documents.stage_id) is not str
        or documents.stage_id != stage.stage_id
    ):
        raise GraphStagePromotionRejected()
    native = documents.intent_scalars
    request = documents.request_bytes
    observation = documents.observation_bytes
    if native is None and request is None and observation is None:
        _require_stage_shape(stage, 0)
        return None
    if type(native) is not GraphStagePromotionIntentScalars or request is None:
        raise GraphStagePromotionRejected()
    _require_document_bytes(request)
    if observation is not None:
        _require_document_bytes(observation)
    intent = _prepare_intent(stage, native.content_generation, request)
    _require_saved_intent_fields(native, intent.values())
    settlement = (
        intent.prepare_settlement(observation) if observation is not None else None
    )
    return intent, settlement


def read_detached_graph_stage_promotion(
    stage: GraphStage, documents: GraphStagePromotionDocuments
) -> (
    tuple[
        PreparedGraphStagePromotionIntent, PreparedGraphStagePromotionSettlement | None
    ]
    | None
):
    try:
        return _read_detached_graph_stage_promotion(stage, documents)
    except (GraphStagePromotionRejected, ValueError) as refused:
        _discard_private_error(refused)
    del stage, documents
    raise_private_metadata_error(GraphStagePromotionRejected())


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
    result = _closed_result(
        snapshot, request.operation, request.arguments.address, observation
    )
    if type(result) is not PromotionResult:
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
