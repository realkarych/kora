import pytest
from sqlalchemy import func, select

from kora.api.graph_deployment_limits import READ
from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.cel_eval.thread import Evaluator
from kora.conformance.capture import Capture
from kora.db.connection import Database
from kora.db.graph_resource_owner_reads import acquire_graph_resource_owners
from kora.db.graph_resource_transport import graph_resource_binary_projection
from kora.db.graph_version_reads import acquire_graph_version
from kora.db.identifiers import IdentifierGenerator
from kora.db.tables import attempt, graph_version, inbound_event, run
from kora.engine.expiry import expire_run, observe_run_expiry
from kora.engine.expiry_context import ExpiryState
from kora.engine.graph_execution_contract import check_detached_execution_contract
from kora.engine.graph_resource_references import (
    GraphResourceScope,
    NativeGraphResourceTransport,
)
from kora.engine.graph_version_identity import read_acquired_graph_version_texts
from kora.engine.run_materialization import prepare_run_materialization
from kora.engine.run_promotion import PromotionOutcome
from kora.models.runs import AcceptedRun
from tests.api.deployment_cases import HARNESS, evaluator, preparer, skeleton_ir
from tests.api.support import api_capture, application, client_for, deployment_services
from tests.api.test_trigger_route import RUNS, deployed, keyed, user
from tests.db.seed import FakeClock
from tests.engine.expiry_support import expiry_context
from tests.engine.graph_output_cases import child
from tests.engine.promotion_support import PromotionCase

__all__ = ["api_capture", "evaluator", "preparer"]


async def read_triggered_case(
    database: Database, accepted: AcceptedRun
) -> PromotionCase:
    run_id = accepted.run_id.removeprefix("run_")
    async with database.read_transaction() as reader:
        owner = (
            await reader.execute(
                select(
                    run.c.namespace_id, run.c.graph_id, run.c.graph_version_id
                ).where(run.c.id == run_id)
            )
        ).one()
        scope = GraphResourceScope(*owner)
        acquired = await acquire_graph_version(
            reader,
            scope.namespace_id,
            scope.graph_id,
            scope.version_id,
            max_encoded_bytes=READ.identity.max_encoded_bytes,
        )
        physical = (
            await reader.execute(
                select(
                    graph_resource_binary_projection(
                        database.dialect, graph_version.c.resources
                    )
                ).where(graph_version.c.id == scope.version_id)
            )
        ).scalar_one()
        owners = await acquire_graph_resource_owners(
            reader, scope.version_id, max_text_bytes=READ.physical_input_bytes
        )
        receipt_id = (await reader.execute(select(inbound_event.c.id))).scalar_one()
    assert acquired is not None
    assert acquired.encoding in ("ascii", "utf-8")
    pin, _ = read_acquired_graph_version_texts(acquired, limits=READ.identity)
    checked = check_detached_execution_contract(
        acquired,
        pin,
        scope,
        NativeGraphResourceTransport(physical, "utf-8"),
        owners,
        limits=READ,
    )
    materialization = prepare_run_materialization(
        checked, max_ir_bytes=len(checked.canonical_ir), max_top_level_nodes=3
    )
    clock = FakeClock()
    return PromotionCase(
        scope,
        pin,
        materialization,
        run_id,
        receipt_id,
        clock,
        IdentifierGenerator(clock.now),
    )


@pytest.mark.asyncio
async def test_http_trigger_promotes_and_expires_at_saved_deadline(
    database: Database,
    evaluator: Evaluator,
    preparer: DeploymentPreparer,
    api_capture: Capture,
) -> None:
    headers, namespace_id = await user(database)
    ir = skeleton_ir()
    child(ir, "runtime")["limits"] = {"timeout": "1s"}
    version = await deployed(database, evaluator, namespace_id, ir)
    app = application(
        database, api_capture, deployment_services(database, preparer, HARNESS)
    )
    request_headers = keyed(headers, "trigger-expiry")

    async with client_for(app) as client:
        response = await client.post(RUNS, json={"params": {}}, headers=request_headers)
        accepted = AcceptedRun.model_validate(response.json())
        case = await read_triggered_case(database, accepted)
        async with database.read_transaction() as reader:
            receipt_before = dict(
                (await reader.execute(select(inbound_event))).mappings().one()
            )
        promoted = await case.promote(database)
        repeated = await case.promote(database)
        before, pending, before_events = await case.snapshot(database)
        case.clock.advance(1)
        async with database.transaction() as writer:
            context = await expiry_context(writer, case)
            observed = await observe_run_expiry(context)
            expired = await expire_run(context)
        replay = await client.post(RUNS, json={"params": {}}, headers=request_headers)
    after, cancelled, events = await case.snapshot(database)
    async with database.read_transaction() as reader:
        attempt_count = await reader.scalar(select(func.count()).select_from(attempt))
        receipt_after = dict(
            (await reader.execute(select(inbound_event))).mappings().one()
        )

    assert response.status_code == 201
    assert replay.status_code == 200
    assert replay.json() == response.json()
    assert accepted.graph_version_id == version
    assert promoted is PromotionOutcome.APPLIED
    assert repeated is PromotionOutcome.UNCHANGED
    assert before["status"] == "running"
    assert before["limits"] == {"timeout": "1s"}
    assert before["trigger"] == after["trigger"]
    assert [row["kind"] for row in before_events].count("run.started") == 1
    assert len(pending) == len(cancelled) == 3
    assert all(row["status"] == "pending" for row in pending)
    assert all(row["status"] == "cancelled" for row in cancelled)
    assert observed is expired is ExpiryState.EXPIRED
    assert after["status"] == "failed"
    assert after["started_at"] == before["started_at"]
    assert after["deadline_expired_at"] == case.clock.now()
    assert [row["kind"] for row in events].count("run.failed") == 1
    assert attempt_count == 0
    assert receipt_after == receipt_before
    assert database.units_in_flight() == 0
    assert database.writer_is_free()
