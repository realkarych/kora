import pickle
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from traceback import format_exception, walk_tb

import pytest
import pytest_asyncio
from fastapi.encoders import jsonable_encoder

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
from kora.db.tables import (
    graph_deployment_stage,
    graph_deployment_stage_occurrence,
    storage_address_fence,
)
from kora.engine.graph_stage_promotion import (
    GraphStagePromotionRejected,
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
)
from tests.db.closing import ConnectionLedger
from tests.db.seed import Seeder
from tests.db.test_graph_deployment_stage_migration import (
    occurrence_values,
    prepare_graph,
    stage_values,
)
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
        (GENERATION, request, settled_no_effect.canonical_bytes()),
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
