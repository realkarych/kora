import pytest
from sqlalchemy import select

from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.conformance.capture import Capture
from kora.db.claim_sources import ClaimSourceBinding, acquire_claim_sources
from kora.db.connection import Database
from kora.db.run_input_reads import RunInputBinding
from kora.db.tables import attempt
from kora.engine.admission_processing import AdmissionProcessingFailed
from kora.engine.attempt_admission import AttemptAdmissionState
from kora.engine.claim_sources import decode_claim_sources
from tests.api.deployment_cases import preparer
from tests.api.support import api_capture, client_for
from tests.db.closing import ConnectionLedger
from tests.engine.admission_preparation_support import observe_detached_checks
from tests.engine.admission_processing_support import process, processor
from tests.engine.admission_snapshot_support import KEY, RUNS, snapshot_case

__all__ = ["api_capture", "preparer"]


@pytest.mark.asyncio
async def test_admission_processing_commits_public_sources_once(
    database: Database, preparer: DeploymentPreparer, api_capture: Capture
) -> None:
    case = await snapshot_case(database, preparer, api_capture)
    owner = processor(database, case)
    ledger = ConnectionLedger(database)

    with observe_detached_checks(database) as checks:
        outcome = await process(owner, case.node_id)
    checkouts = ledger.checkouts
    first = await case.promotion.snapshot(database)
    async with client_for(case.app) as client:
        replay = await client.post(
            RUNS,
            json={"params": {}},
            headers=case.headers | {"Idempotency-Key": KEY},
        )
    with pytest.raises(AdmissionProcessingFailed) as refused:
        await process(owner, case.node_id)
    second = await case.promotion.snapshot(database)
    scope = case.promotion.scope
    async with database.read_transaction() as reader:
        with await reader.execute(select(attempt)) as rows:
            saved = dict(rows.mappings().one())
        binding = ClaimSourceBinding(
            RunInputBinding(
                scope.namespace_id,
                scope.graph_id,
                scope.version_id,
                case.promotion.run_id,
            ),
            case.node_id,
            str(saved["id"]),
            case.price_id,
            "agent",
        )
        sources = await acquire_claim_sources(reader, binding, owner.limits.sources)

    assert outcome is AttemptAdmissionState.QUEUED
    assert replay.status_code == 200
    assert first == second
    assert checkouts == 2
    assert checks.count("decode_run_input_value") == 2
    assert saved["status"] == "created"
    assert saved["price_table_id"] == case.price_id
    assert saved["execution_state"] == "not_started"
    assert saved["lease_epoch"] == 0
    assert all(
        saved[field] is None
        for field in (
            "started_at",
            "deadline",
            "worker_id",
            "execution_domain_id",
            "lease_owner",
        )
    )
    assert sources is not None
    assert decode_claim_sources(sources).values["input_bindings"] == (
        '{"version":1,"inputs":[]}'
    )
    assert str(refused.value) == "admission candidate processing failed"
    assert refused.value.__cause__ is refused.value.__context__ is None
    assert ledger.outstanding == database.units_in_flight() == 0
