import asyncio
import pickle
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from traceback import format_exception, walk_tb
from typing import cast

import pytest
import pytest_asyncio
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select

from kora.conformance.capture import Capture
from kora.conformance.common import context_for, kora_capture
from kora.conformance.storage_support import (
    BytesSource,
    content_address,
    preparation_request,
    promotion_request,
    repeated_digest,
    seal_repeated,
)
from kora.db.connection import Database
from kora.db.graph_deployment_stage_reads import (
    GraphStage,
    GraphStageScope,
    read_graph_stage_inventory,
)
from kora.db.graph_preparation_reads import GraphPreparationBinding
from kora.db.graph_stage_promotion_intent_reads import GraphStagePromotionIntentScalars
from kora.db.graph_stage_promotion_snapshot import (
    GraphStagePromotionDocuments,
    read_graph_stage_promotion_snapshot,
)
from kora.db.tables import (
    graph_deployment_preparation,
    graph_deployment_stage,
    graph_deployment_stage_occurrence,
    storage_address_fence,
)
from kora.db.tables import (
    graph_deployment_stage_promotion_intent as intent_table,
)
from kora.db.tables import (
    graph_deployment_stage_promotion_settlement as settlement_table,
)
from kora.engine.graph_stage_promotion import (
    GraphStagePromotionRejected,
    PreparedGraphStagePromotionIntent,
    PreparedGraphStagePromotionSettlement,
    prepare_graph_stage_promotion_intent,
    read_detached_graph_stage_promotion,
    verify_graph_stage_promotion,
)
from kora.protocols.common import ProtocolContext
from kora.protocols.storage_models import (
    MAX_STORAGE_METADATA,
    MutationObservation,
    NoEffectFailure,
    PreparedContentRef,
    PromotionArguments,
    PromotionRequest,
    PromotionResult,
    StorageIdentity,
    StorageModel,
)
from tests.db.closing import CONDITION_DEADLINE_SECONDS, ConnectionLedger
from tests.db.seed import Seeder
from tests.db.test_graph_deployment_stage_migration import (
    occurrence_values,
    prepare_graph,
    stage_values,
)
from tests.engine.test_graph_output_inventory import observe_calls
from tests.plugins.storage.configuration import local_configuration
from tests.plugins.storage.inspection import InspectedStorage

__all__ = ["kora_capture"]

SIZE = 23
GENERATION = 7
PRIVATE_MARKER = "private-graph-promotion-evidence"


@dataclass(frozen=True)
class PromotionEvidence:
    original: PromotionRequest
    original_pending: MutationObservation
    original_settled: MutationObservation
    sibling: PromotionRequest
    sibling_settled: MutationObservation
    original_result: PromotionResult
    sibling_result: PromotionResult
    replayed: PromotionResult


async def seed_stages(
    database: Database, identity: StorageIdentity
) -> tuple[GraphStage, GraphStage]:
    digest = repeated_digest(SIZE)
    async with database.transaction() as writer:
        incarnation: object = await writer.scalar(
            storage_address_fence.insert()
            .values(
                store_id=identity.store_id,
                payload_digest=digest,
                last_deletion_generation=0,
            )
            .returning(storage_address_fence.c.fence_incarnation)
        )
        assert type(incarnation) is int
        first = await prepare_graph(writer, "promotion-original")
        second = await prepare_graph(writer, "promotion-sibling")
        seeder = Seeder(writer)
        for scope in (first, second):
            stage_id = seeder.new_id()
            values = stage_values(scope, stage_id, incarnation)
            values.update(
                store_id=identity.store_id, payload_digest=digest, total_size=SIZE
            )
            await writer.execute(graph_deployment_stage.insert().values(**values))
            await writer.execute(
                graph_deployment_stage_occurrence.insert().values(
                    **occurrence_values(scope, stage_id, 0)
                )
            )
    async with database.read_snapshot() as reader:
        original = await read_graph_stage_inventory(
            reader, GraphStageScope(first.preparation_id, first.namespace_id)
        )
        sibling = await read_graph_stage_inventory(
            reader, GraphStageScope(second.preparation_id, second.namespace_id)
        )
    (original_stage,) = original.stages
    (sibling_stage,) = sibling.stages
    return original_stage, sibling_stage


async def promote_two_stages(
    provider: InspectedStorage,
    context: ProtocolContext,
    original: GraphStage,
    sibling: GraphStage,
) -> PromotionEvidence:
    identity = provider.identity()
    address = content_address(identity, original.payload_digest)
    first = await seal_repeated(
        provider, original.stage_id, original.total_size, context
    )
    second = await seal_repeated(
        provider, sibling.stage_id, sibling.total_size, context
    )
    prepare = preparation_request(
        identity,
        "prepare-original",
        first.content,
        original.total_size,
        original.payload_digest,
    )
    source = BytesSource(b"k" * original.total_size)
    candidate = await provider.prepare_content(prepare, source, context)
    assert source.closed
    original_request = promotion_request(
        identity, "promote-original", candidate, address, GENERATION
    )
    sibling_request = promotion_request(
        identity, "promote-sibling", second.content, address, GENERATION
    )
    sibling_result = await provider.promote(sibling_request, context)
    sibling_settled = await provider.observe(sibling_request.operation, context)
    original_pending = await provider.observe(original_request.operation, context)
    for candidate_request in (original_request, sibling_request):
        with pytest.raises(GraphStagePromotionRejected):
            verify_graph_stage_promotion(
                original,
                GENERATION,
                candidate_request.canonical_bytes(),
                sibling_settled.canonical_bytes(),
            )
    original_result = await provider.promote(original_request, context)
    original_settled = await provider.observe(original_request.operation, context)
    replayed = await provider.promote(original_request, context)
    return PromotionEvidence(
        original_request,
        original_pending,
        original_settled,
        sibling_request,
        sibling_settled,
        original_result,
        sibling_result,
        replayed,
    )


@pytest_asyncio.fixture
async def real_promotions(
    database: Database, kora_capture: Capture
) -> tuple[GraphStage, GraphStage, PromotionEvidence]:
    ledger = ConnectionLedger(database)
    with TemporaryDirectory(prefix="kora-graph-promotion-", dir="/tmp") as directory:
        provider = InspectedStorage(local_configuration(Path(directory) / "store"))
        try:
            original, sibling = await seed_stages(database, provider.identity())
            assert ledger.outstanding == database.units_in_flight() == 0
            evidence = await promote_two_stages(
                provider, context_for(kora_capture), original, sibling
            )
        finally:
            await provider.aclose()
        provider.assert_closed()
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()
    return original, sibling, evidence


@pytest.mark.asyncio
async def test_stage_promotion_preserves_detached_original_attribution(
    real_promotions: tuple[GraphStage, GraphStage, PromotionEvidence],
) -> None:
    original, sibling, evidence = real_promotions
    saved_stage = replace(original)
    original_ref = evidence.original_result.content

    checked = verify_graph_stage_promotion(
        original,
        GENERATION,
        evidence.original.canonical_bytes(),
        evidence.original_settled.canonical_bytes(),
    )
    creator = verify_graph_stage_promotion(
        sibling,
        GENERATION,
        evidence.sibling.canonical_bytes(),
        evidence.sibling_settled.canonical_bytes(),
    )
    exposed_ref = checked.reference
    exposed_ref.address.__init__(
        version=1,
        storage_identity=original_ref.address.storage_identity,
        payload_digest="sha256:" + "0" * 64,
    )
    original.__init__(
        sibling.stage_id,
        original.namespace_id,
        original.preparation_id,
        original.store_id,
        original.payload_digest,
        original.fence_incarnation,
        original.total_size,
    )

    assert checked.stage == saved_stage
    assert checked.content_generation == GENERATION
    assert checked.operation == evidence.original.operation
    assert checked.reference == original_ref == creator.reference
    assert creator.reference == evidence.sibling_result.content
    assert exposed_ref != original_ref
    assert checked.created_object is False
    assert creator.created_object is True
    assert evidence.replayed == evidence.original_result
    assert evidence.original_pending.state == "evidence_unavailable"
    assert saved_stage.stage_id not in repr(checked)
    with pytest.raises(TypeError, match="requires explicit access"):
        pickle.dumps(checked)
    with pytest.raises(
        ValueError, match="CheckedGraphStagePromotion.*not iterable"
    ) as refused:
        jsonable_encoder(checked)
    assert saved_stage.stage_id not in str(refused.value)


def replace_source(
    evidence: PromotionEvidence, arguments: PromotionArguments
) -> tuple[bytes, bytes]:
    request = promotion_request(
        evidence.original.operation.storage_identity,
        evidence.original.operation.operation_id,
        arguments.content,
        arguments.address,
        arguments.content_generation,
    )
    result = evidence.original_result.model_copy(
        update={"operation": request.operation}
    )
    observation = evidence.original_settled.model_copy(
        update={"operation": request.operation, "result": result}
    )
    return request.canonical_bytes(), observation.canonical_bytes()


@pytest.mark.asyncio
async def test_stage_promotion_refuses_substitution_and_invalid_evidence(
    real_promotions: tuple[GraphStage, GraphStage, PromotionEvidence],
) -> None:
    original, _, evidence = real_promotions
    request = evidence.original.canonical_bytes()
    settled = evidence.original_settled.canonical_bytes()
    content = evidence.original.arguments.content
    assert isinstance(content, PreparedContentRef)
    foreign_source = content.model_copy(
        update={"source": evidence.sibling.arguments.content}
    )
    incomplete = content.model_copy(
        update={"source": content.source.model_copy(update={"source_complete": False})}
    )
    wrong_source_size = content.model_copy(
        update={
            "source": content.source.model_copy(
                update={"total_size": original.total_size + 1}
            )
        }
    )
    wrong_source_digest = content.model_copy(
        update={
            "source": content.source.model_copy(
                update={"payload_digest": "sha256:" + "0" * 64}
            )
        }
    )
    wrong_preparation_kind = content.model_copy(
        update={
            "operation": content.operation.model_copy(update={"kind": "seal_stage"})
        }
    )
    bad_result_ref = evidence.original_result.model_copy(
        update={"operation": evidence.sibling.operation}
    )
    bad_content = evidence.original_result.content.model_copy(
        update={"total_size": original.total_size + 1}
    )
    bad_result_content = evidence.original_result.model_copy(
        update={"content": bad_content}
    )
    bad_address = content_address(
        evidence.original.operation.storage_identity, "sha256:" + "0" * 64
    )
    bad_result_address = evidence.original_result.model_copy(
        update={
            "content": evidence.original_result.content.model_copy(
                update={"address": bad_address}
            )
        }
    )
    bad_request_op = evidence.original.operation.model_copy(
        update={"request_digest": "sha256:" + "0" * 64}
    )
    bad_request = evidence.original.model_copy(update={"operation": bad_request_op})
    matching_wrong_hash = evidence.original_settled.model_copy(
        update={
            "operation": bad_request_op,
            "result": evidence.original_result.model_copy(
                update={"operation": bad_request_op}
            ),
        }
    )
    settled_no_effect = evidence.original_settled.model_copy(
        update={
            "result": NoEffectFailure(
                version=1,
                operation=evidence.original.operation,
                code="storage_integrity",
            )
        }
    )
    no_effect_bytes = settled_no_effect.canonical_bytes()
    refusals = (
        (GENERATION, request, evidence.sibling_settled.canonical_bytes()),
        (
            GENERATION,
            evidence.sibling.canonical_bytes(),
            evidence.sibling_settled.canonical_bytes(),
        ),
        (GENERATION, request, evidence.original_pending.canonical_bytes()),
        (
            GENERATION,
            request,
            evidence.original_settled.model_copy(
                update={"state": "pending", "effects_closed": False}
            ).canonical_bytes(),
        ),
        (
            GENERATION,
            request,
            evidence.original_settled.model_copy(
                update={"effects_closed": False}
            ).canonical_bytes(),
        ),
        (GENERATION, request, no_effect_bytes),
        (
            GENERATION,
            request,
            evidence.original_settled.model_copy(
                update={"operation": evidence.sibling.operation}
            ).canonical_bytes(),
        ),
        (
            GENERATION,
            request,
            evidence.original_settled.model_copy(
                update={"result": bad_result_ref}
            ).canonical_bytes(),
        ),
        (
            GENERATION,
            request,
            evidence.original_settled.model_copy(
                update={"result": bad_result_content}
            ).canonical_bytes(),
        ),
        (
            GENERATION,
            request,
            evidence.original_settled.model_copy(
                update={"result": bad_result_address}
            ).canonical_bytes(),
        ),
        (
            GENERATION,
            bad_request.canonical_bytes(),
            matching_wrong_hash.canonical_bytes(),
        ),
        (GENERATION + 1, request, settled),
        (
            True,
            *replace_source(
                evidence,
                evidence.original.arguments.model_copy(
                    update={"content_generation": 1}
                ),
            ),
        ),
        (
            GENERATION,
            *replace_source(
                evidence,
                evidence.original.arguments.model_copy(
                    update={"content": foreign_source}
                ),
            ),
        ),
        (
            GENERATION,
            *replace_source(
                evidence,
                evidence.original.arguments.model_copy(update={"content": incomplete}),
            ),
        ),
        *(
            (
                GENERATION,
                *replace_source(
                    evidence,
                    evidence.original.arguments.model_copy(update={"content": changed}),
                ),
            )
            for changed in (
                wrong_source_size,
                wrong_source_digest,
                wrong_preparation_kind,
            )
        ),
        (GENERATION, request.ljust(MAX_STORAGE_METADATA + 1), settled),
        (GENERATION, request, settled.ljust(MAX_STORAGE_METADATA + 1)),
        (GENERATION, b'{"version":1,' + request[1:], settled),
        (GENERATION, request, b'{"private":"' + PRIVATE_MARKER.encode("ascii")),
        (GENERATION, request, b'{"deep":' + b"[" * 16 + b"0" + b"]" * 16 + b"}"),
    )

    checked = verify_graph_stage_promotion(
        original,
        GENERATION,
        request.ljust(MAX_STORAGE_METADATA),
        settled.ljust(MAX_STORAGE_METADATA),
    )
    failures: list[GraphStagePromotionRejected] = []
    for generation, intent, receipt in refusals:
        with pytest.raises(GraphStagePromotionRejected) as refused:
            verify_graph_stage_promotion(original, generation, intent, receipt)
        failures.append(refused.value)
        if receipt != no_effect_bytes:
            with pytest.raises(GraphStagePromotionRejected):
                prepare_graph_stage_promotion_intent(
                    original, generation, intent
                ).prepare_settlement(receipt)

    assert checked.reference == evidence.original_result.content
    assert len(failures) == len(refusals)
    for error in failures:
        rendered = "".join(format_exception(error))
        assert str(error) == "graph stage promotion evidence rejected"
        assert error.__cause__ is error.__context__ is None
        for marker in (
            PRIVATE_MARKER,
            original.stage_id,
            original.store_id,
            original.payload_digest,
        ):
            assert marker not in rendered
        frames = {
            frame.f_code.co_name: frame.f_locals
            for frame, _ in walk_tb(error.__traceback__)
        }
        assert frames["verify_graph_stage_promotion"] == {}
        assert "_verify" not in frames


@pytest.mark.asyncio
async def test_prepared_custody_round_trips_real_provider_results(
    database: Database,
    real_promotions: tuple[GraphStage, GraphStage, PromotionEvidence],
) -> None:
    original, sibling, evidence = real_promotions
    cases = (
        (
            original,
            evidence.original,
            evidence.original_settled,
            evidence.original_result,
        ),
        (sibling, evidence.sibling, evidence.sibling_settled, evidence.sibling_result),
    )
    ledger = ConnectionLedger(database)
    before = ledger.checkouts

    prepared = [
        prepare_graph_stage_promotion_intent(
            stage, GENERATION, request.canonical_bytes()
        )
        for stage, request, _, _ in cases
    ]
    settled = [
        intent.prepare_settlement(observation.canonical_bytes())
        for intent, (_, _, observation, _) in zip(prepared, cases, strict=True)
    ]
    distinct_intent = prepare_graph_stage_promotion_intent(
        original, GENERATION, evidence.original.canonical_bytes()
    )
    changed_request = prepared[0].values()
    changed_request["stage_id"] = sibling.stage_id
    changed_request["request_bytes"] = b"{}"
    changed_result = settled[0].values()
    changed_result["observation_bytes"] = b"{}"
    exposed = settled[0].promotion
    assert exposed is not None
    exposed_stage = exposed.stage
    exposed_stage.__init__(
        sibling.stage_id,
        exposed_stage.namespace_id,
        exposed_stage.preparation_id,
        exposed_stage.store_id,
        exposed_stage.payload_digest,
        exposed_stage.fence_incarnation,
        exposed_stage.total_size,
    )
    after_preparation = ledger.checkouts
    async with database.transaction() as writer:
        for intent, settlement in zip(prepared, settled, strict=True):
            await writer.execute(intent_table.insert().values(**intent.values()))
            await writer.execute(
                settlement_table.insert().values(**settlement.values())
            )
    async with database.read_snapshot() as reader:
        preparations = graph_deployment_preparation.c
        bindings = await reader.execute(
            select(preparations.id, preparations.initial_graph_id).where(
                preparations.id.in_((original.preparation_id, sibling.preparation_id))
            )
        )
        graph_ids = {preparation_id: graph_id for preparation_id, graph_id in bindings}
    restored: list[
        tuple[PreparedGraphStagePromotionIntent, PreparedGraphStagePromotionSettlement]
    ] = []
    for stage, _, _, _ in cases:
        initial_graph_id: object = graph_ids[stage.preparation_id]
        assert type(initial_graph_id) is str
        snapshot = await read_graph_stage_promotion_snapshot(
            database,
            GraphPreparationBinding(
                stage.preparation_id,
                stage.namespace_id,
                "pipeline",
                initial_graph_id,
                "a" * 64,
            ),
            max_stages=1,
            max_document_bytes=2 * MAX_STORAGE_METADATA,
            deadline=asyncio.get_running_loop().time() + CONDITION_DEADLINE_SECONDS,
        )
        (saved_stage,) = snapshot.inventory.stages
        (saved_documents,) = snapshot.documents
        assert saved_stage == stage
        assert ledger.outstanding == database.units_in_flight() == 0
        reads = ledger.checkouts
        with observe_calls(
            (StorageModel.model_validate_json.__code__,)
        ) as decode_calls:
            checked = read_detached_graph_stage_promotion(saved_stage, saved_documents)
        assert checked is not None
        intent, settlement = checked
        assert settlement is not None
        assert decode_calls == [2]
        assert ledger.checkouts == reads
        restored.append((intent, settlement))

    assert after_preparation == before
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()
    assert not settled[0].belongs_to(distinct_intent)
    assert not restored[0][1].belongs_to(prepared[0])
    for (stage, request, observation, result), (intent, settlement) in zip(
        cases, restored, strict=True
    ):
        assert intent.values() == {
            "stage_id": stage.stage_id,
            "store_id": stage.store_id,
            "payload_digest": stage.payload_digest,
            "fence_incarnation": stage.fence_incarnation,
            "operation_id": request.operation.operation_id,
            "request_digest": request.operation.request_digest,
            "content_generation": GENERATION,
            "request_bytes": request.canonical_bytes(),
        }
        assert settlement.values() == {
            "stage_id": stage.stage_id,
            "observation_bytes": observation.canonical_bytes(),
        }
        assert settlement.belongs_to(intent)
        assert settlement.outcome_digest == result.metadata_digest()
        promotion = settlement.promotion
        assert promotion is not None
        assert promotion.stage == stage
        assert promotion.operation == request.operation
        assert promotion.reference == result.content
        assert promotion.created_object is result.created_object
        assert stage.stage_id not in repr(intent) + repr(settlement)
    assert evidence.original_result.created_object is False
    assert evidence.sibling_result.created_object is True


def original_promotion_documents(
    stage: GraphStage, evidence: PromotionEvidence
) -> GraphStagePromotionDocuments:
    operation = evidence.original.operation
    return GraphStagePromotionDocuments(
        stage.stage_id,
        evidence.original.canonical_bytes(),
        evidence.original_settled.canonical_bytes(),
        GraphStagePromotionIntentScalars(
            stage.store_id,
            stage.payload_digest,
            stage.fence_incarnation,
            operation.operation_id,
            operation.request_digest,
            GENERATION,
        ),
    )


@pytest.mark.asyncio
async def test_detached_custody_distinguishes_absence_unsettled_and_closed_no_effect(
    real_promotions: tuple[GraphStage, GraphStage, PromotionEvidence],
) -> None:
    stage, _, evidence = real_promotions
    documents = original_promotion_documents(stage, evidence)
    absent = GraphStagePromotionDocuments(stage.stage_id, None, None, None)
    unsettled = replace(documents, observation_bytes=None)
    failure = NoEffectFailure(
        version=1, operation=evidence.original.operation, code="storage_issuer_closed"
    )
    observation = MutationObservation(
        version=1,
        operation=evidence.original.operation,
        state="settled",
        result=failure,
        effects_closed=True,
    )
    no_effect = replace(documents, observation_bytes=observation.canonical_bytes())

    missing = read_detached_graph_stage_promotion(stage, absent)
    waiting = read_detached_graph_stage_promotion(stage, unsettled)
    closed = read_detached_graph_stage_promotion(stage, no_effect)

    assert missing is None
    assert waiting is not None
    assert waiting[0].values()["request_bytes"] == unsettled.request_bytes
    assert waiting[1] is None
    assert closed is not None
    intent, settlement = closed
    assert settlement is not None
    assert settlement.belongs_to(intent)
    assert settlement.promotion is None
    assert settlement.outcome_digest == failure.metadata_digest()


@pytest.mark.asyncio
async def test_detached_custody_refuses_conflicting_native_or_document_evidence(
    real_promotions: tuple[GraphStage, GraphStage, PromotionEvidence],
) -> None:
    stage, sibling, evidence = real_promotions
    stage = replace(stage, fence_incarnation=1)
    documents = original_promotion_documents(stage, evidence)
    native, request, observation = (
        documents.intent_scalars,
        documents.request_bytes,
        documents.observation_bytes,
    )
    assert native is not None
    assert request is not None
    assert observation is not None
    absent = GraphStagePromotionDocuments(sibling.stage_id, None, None, None)
    changed_scalars = (
        replace(native, store_id="another-store"),
        replace(native, payload_digest=repeated_digest(SIZE + 1)),
        replace(native, fence_incarnation=2),
        replace(native, fence_incarnation=True),
        replace(native, operation_id=evidence.sibling.operation.operation_id),
        replace(native, request_digest=evidence.sibling.operation.request_digest),
        replace(native, content_generation=GENERATION + 1),
        replace(native, content_generation=cast("int", float(GENERATION))),
    )
    refusals = (
        absent,
        replace(documents, stage_id=sibling.stage_id),
        replace(documents, intent_scalars=None, request_bytes=None),
        replace(documents, intent_scalars=None),
        replace(documents, request_bytes=None, observation_bytes=None),
        replace(documents, request_bytes=request + b" "),
        replace(documents, observation_bytes=observation + b" "),
        replace(
            documents, observation_bytes=evidence.original_pending.canonical_bytes()
        ),
        replace(
            documents, observation_bytes=evidence.sibling_settled.canonical_bytes()
        ),
        *(replace(documents, intent_scalars=changed) for changed in changed_scalars),
    )
    overwidth = (
        replace(documents, request_bytes=request.ljust(MAX_STORAGE_METADATA + 1)),
        replace(
            documents, observation_bytes=observation.ljust(MAX_STORAGE_METADATA + 1)
        ),
    )
    private_bytes = b'{"private":"' + PRIVATE_MARKER.encode()
    private_documents = (
        replace(documents, request_bytes=private_bytes),
        replace(documents, observation_bytes=private_bytes),
    )

    for bad in refusals:
        with pytest.raises(GraphStagePromotionRejected):
            read_detached_graph_stage_promotion(stage, bad)
    with observe_calls((StorageModel.model_validate_json.__code__,)) as decode_calls:
        for wide in overwidth:
            with pytest.raises(GraphStagePromotionRejected):
                read_detached_graph_stage_promotion(stage, wide)
    errors: list[GraphStagePromotionRejected] = []
    for private in private_documents:
        try:
            raise RuntimeError(PRIVATE_MARKER)
        except RuntimeError:
            with pytest.raises(GraphStagePromotionRejected) as refused:
                read_detached_graph_stage_promotion(stage, private)
        errors.append(refused.value)

    assert decode_calls == [0]
    for error in errors:
        assert str(error) == "graph stage promotion evidence rejected"
        assert error.__cause__ is error.__context__ is None
        assert PRIVATE_MARKER not in "".join(format_exception(error))
        frames = {
            frame.f_code.co_name: frame.f_locals
            for frame, _ in walk_tb(error.__traceback__)
        }
        assert frames["read_detached_graph_stage_promotion"] == {}
        assert "_read_detached_graph_stage_promotion" not in frames
        assert "prepare_settlement" not in frames


@pytest.mark.asyncio
async def test_prepared_custody_accepts_closed_no_effect_without_content(
    real_promotions: tuple[GraphStage, GraphStage, PromotionEvidence],
) -> None:
    original, _, evidence = real_promotions
    intent = prepare_graph_stage_promotion_intent(
        original, GENERATION, evidence.original.canonical_bytes()
    )
    failure = NoEffectFailure(
        version=1, operation=evidence.original.operation, code="storage_issuer_closed"
    )
    observation = MutationObservation(
        version=1,
        operation=evidence.original.operation,
        state="settled",
        result=failure,
        effects_closed=True,
    )
    encoded = observation.canonical_bytes()

    settlement = intent.prepare_settlement(encoded)

    assert settlement.belongs_to(intent)
    assert settlement.promotion is None
    assert settlement.values() == {
        "stage_id": original.stage_id,
        "observation_bytes": encoded,
    }
    assert settlement.outcome_digest == failure.metadata_digest()
    assert settlement.outcome_digest != observation.metadata_digest()


@pytest.mark.asyncio
async def test_prepared_custody_refuses_unissued_or_noncanonical_inputs_privately(
    real_promotions: tuple[GraphStage, GraphStage, PromotionEvidence],
) -> None:
    original, _, evidence = real_promotions
    request_bytes = evidence.original.canonical_bytes()
    observation_bytes = evidence.original_settled.canonical_bytes()
    intent = prepare_graph_stage_promotion_intent(original, GENERATION, request_bytes)
    unissued_intent = object.__new__(PreparedGraphStagePromotionIntent)
    unissued_settlement = object.__new__(PreparedGraphStagePromotionSettlement)
    prefix = b'{"version":1,'
    invalid_requests = (
        b"",
        cast("bytes", bytearray(request_bytes)),
        request_bytes + b" ",
        request_bytes.ljust(MAX_STORAGE_METADATA + 1),
        prefix + request_bytes[1:],
    )
    invalid_observations = (
        b"",
        cast("bytes", bytearray(observation_bytes)),
        observation_bytes + b" ",
        observation_bytes.ljust(MAX_STORAGE_METADATA + 1),
        prefix + observation_bytes[1:],
    )

    for bad in invalid_requests:
        with pytest.raises(GraphStagePromotionRejected):
            prepare_graph_stage_promotion_intent(original, GENERATION, bad)
    for bad in invalid_observations:
        with pytest.raises(GraphStagePromotionRejected):
            intent.prepare_settlement(bad)
    with pytest.raises(TypeError, match="requires prepared evidence"):
        PreparedGraphStagePromotionIntent(
            object(), original, GENERATION, evidence.original, request_bytes
        )
    with pytest.raises(TypeError, match="requires prepared evidence"):
        PreparedGraphStagePromotionSettlement(
            object(), intent, observation_bytes, None, PRIVATE_MARKER
        )
    with pytest.raises(TypeError, match="requires explicit access"):
        pickle.dumps(intent)
    with pytest.raises(TypeError, match="requires explicit access"):
        pickle.dumps(intent.prepare_settlement(observation_bytes))
    with pytest.raises(GraphStagePromotionRejected) as rejected_unissued:
        unissued_intent.prepare_settlement(observation_bytes)
    try:
        raise RuntimeError(PRIVATE_MARKER)
    except RuntimeError:
        with pytest.raises(GraphStagePromotionRejected) as rejected_intent:
            prepare_graph_stage_promotion_intent(
                original, GENERATION, b'{"invalid":"' + PRIVATE_MARKER.encode() + b'"}'
            )
        with pytest.raises(GraphStagePromotionRejected) as rejected_settlement:
            intent.prepare_settlement(b'{"invalid":"' + PRIVATE_MARKER.encode() + b'"}')

    assert not unissued_settlement.belongs_to(intent)
    for error, function in (
        (rejected_intent.value, "prepare_graph_stage_promotion_intent"),
        (rejected_settlement.value, "prepare_settlement"),
        (rejected_unissued.value, "prepare_settlement"),
    ):
        assert error.__cause__ is error.__context__ is None
        assert PRIVATE_MARKER not in "".join(format_exception(error))
        private = next(
            frame.f_locals
            for frame, _ in walk_tb(error.__traceback__)
            if frame.f_code.co_name == function
        )
        assert private == {}
