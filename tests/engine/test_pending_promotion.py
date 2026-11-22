import asyncio
from collections.abc import Generator
from contextlib import contextmanager

import httpx
import pytest
from sqlalchemy import delete, select, update
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy.engine import Connection

from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.api.installed_worker import installed_worker_profile
from kora.conformance.capture import Capture
from kora.db.connection import Database
from kora.db.identifiers import IdentifierGenerator
from kora.db.tables import (
    event,
    inbound_event,
    node_instance,
    run,
    run_promotion_admission,
)
from kora.db.transactions import WRITER_LEASE
from kora.engine.graph_snapshot import GraphSnapshotLimits
from kora.engine.pending_promotion import PendingPromotion
from kora.engine.run_promotion import PromotionOutcome
from kora.engine.start_capacity import Overlap
from kora.models.graphs import DeployResponse
from kora.models.runs import AcceptedRun
from tests.api.deployment_cases import GRAPH, HARNESS, NAMESPACE, preparer, skeleton_ir
from tests.api.support import api_capture, application, client_for, deployment_services
from tests.db.closing import ConnectionLedger
from tests.db.seed import FakeClock, fixed_randomness
from tests.engine.graph_output_cases import child
from tests.engine.test_http_trigger_admission import KEY
from tests.plugins.authentication.support import seed_case

__all__ = ["api_capture", "preparer"]


def pending_promotion(database: Database) -> PendingPromotion:
    profile = installed_worker_profile("2000")
    assert profile is not None
    snapshot = profile.limits.snapshot
    clock = FakeClock()
    return PendingPromotion(
        database,
        GraphSnapshotLimits(
            snapshot.graph, snapshot.graph_native_bytes, snapshot.owner_text_bytes
        ),
        profile.limits.expiry,
        clock.now,
        IdentifierGenerator(clock.now, fixed_randomness(41)),
    )


def graph_path(graph: str) -> str:
    return f"/api/v1/namespaces/{NAMESPACE}/graphs/{graph}"


async def deploy(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    timeout: str,
    *,
    graph: str = GRAPH,
    overlap: Overlap | None = None,
) -> DeployResponse:
    ir = skeleton_ir(graph=graph)
    child(ir, "runtime")["limits"] = {"timeout": timeout}
    if overlap is not None:
        child(ir, "graph")["overlap"] = overlap
    response = await client.post(
        f"{graph_path(graph)}/versions",
        json={"graph": graph, "ir": ir, "resources": []},
        headers=headers,
    )
    assert response.status_code == 201
    return DeployResponse.model_validate(response.json())


async def trigger(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    *,
    graph: str = GRAPH,
    key: str = KEY,
) -> AcceptedRun:
    response = await client.post(
        f"{graph_path(graph)}/runs",
        json={"params": {}},
        headers=headers | {"Idempotency-Key": key},
    )
    assert response.status_code == 201
    return AcceptedRun.model_validate(response.json())


@pytest.mark.asyncio
async def test_scheduler_promotes_original_pin_after_later_deploy(
    database: Database, preparer: DeploymentPreparer, api_capture: Capture
) -> None:
    user = await seed_case(database, NAMESPACE)
    headers = {"Authorization": str(user.request.authorization)}
    app = application(
        database, api_capture, deployment_services(database, preparer, HARNESS)
    )
    promotion = pending_promotion(database)
    ledger = ConnectionLedger(database)
    async with client_for(app) as client:
        original = await deploy(client, headers, "01h")
        accepted = await trigger(client, headers)
        newer = await deploy(client, headers, "02h")
    run_id = accepted.run_id.removeprefix("run_")
    deadline = asyncio.get_running_loop().time() + 10.0

    outcome = await promotion.process(run_id, deadline=deadline, stopped=lambda: False)
    async with database.read_transaction() as reader:
        with await reader.execute(select(run).where(run.c.id == run_id)) as result:
            saved = result.mappings().one()
        with await reader.execute(
            select(event.c.payload).where(
                event.c.run_id == run_id, event.c.kind == "run.started"
            )
        ) as result:
            started = result.scalar_one()
        with await reader.execute(
            select(node_instance.c.node_path).where(node_instance.c.run_id == run_id)
        ) as result:
            nodes = set(result.scalars())

    assert outcome is PromotionOutcome.APPLIED
    assert (
        accepted.graph_version_id == original.graph_version_id != newer.graph_version_id
    )
    assert saved["graph_version_id"] == original.graph_version_id.removeprefix("gv_")
    assert saved["status"] == "running"
    assert saved["limits"] == {"timeout": "01h"}
    assert started["graph_version"] == saved["graph_version_id"]
    assert nodes == {"agent", "gate", "approve"}
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["missing", "overwidth", "wrong_receipt"])
async def test_scheduler_refuses_unadmitted_or_overwidth_source(
    database: Database,
    preparer: DeploymentPreparer,
    api_capture: Capture,
    source: str,
) -> None:
    user = await seed_case(database, NAMESPACE)
    headers = {"Authorization": str(user.request.authorization)}
    app = application(
        database, api_capture, deployment_services(database, preparer, HARNESS)
    )
    promotion = pending_promotion(database)
    ledger = ConnectionLedger(database)
    async with client_for(app) as client:
        await deploy(client, headers, "01h")
        accepted = await trigger(client, headers)
    run_id = accepted.run_id.removeprefix("run_")
    async with database.transaction() as writer:
        match = run_promotion_admission.c.run_id == run_id
        if source == "missing":
            await writer.execute(delete(run_promotion_admission).where(match))
        elif source == "overwidth":
            await writer.execute(
                update(run_promotion_admission)
                .where(match)
                .values(run_bytes=promotion.limits.parent_bytes + 1)
            )
        else:
            receipt_id = (
                select(run_promotion_admission.c.receipt_id)
                .where(match)
                .scalar_subquery()
            )
            await writer.execute(
                update(inbound_event)
                .where(inbound_event.c.id == receipt_id)
                .values(start_outcome={"version": 1})
            )
    deadline = asyncio.get_running_loop().time() + 10.0

    outcome = await promotion.process(run_id, deadline=deadline, stopped=lambda: False)
    async with database.read_transaction() as reader:
        status = await reader.scalar(select(run.c.status).where(run.c.id == run_id))
        node = await reader.scalar(
            select(node_instance.c.id).where(node_instance.c.run_id == run_id)
        )
        started = await reader.scalar(
            select(event.c.id).where(
                event.c.run_id == run_id, event.c.kind == "run.started"
            )
        )

    assert outcome is PromotionOutcome.REFUSED
    assert status == "pending"
    assert node is None
    assert started is None
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()


@contextmanager
def promotion_statements(
    database: Database,
) -> Generator[list[tuple[str, bool]], None, None]:
    statements: list[tuple[str, bool]] = []
    engines = {database.reader_engine.sync_engine, database.writer_engine.sync_engine}

    def capture(
        connection: Connection, cursor: object, statement: str, *args: object
    ) -> None:
        statements.append((statement.lower(), WRITER_LEASE in connection.info))

    for engine in engines:
        sqlalchemy_event.listen(engine, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        for engine in engines:
            sqlalchemy_event.remove(engine, "before_cursor_execute", capture)


async def admitted_runs(
    database: Database, run_ids: tuple[str, ...]
) -> dict[str, tuple[object, ...]]:
    statement = (
        select(
            run_promotion_admission.c.run_id,
            run_promotion_admission.c.receipt_id,
            run_promotion_admission.c.run_bytes,
            run_promotion_admission.c.receipt_bytes,
            run_promotion_admission.c.graph_version_id,
            run.c.status,
        )
        .join(run, run.c.id == run_promotion_admission.c.run_id)
        .where(run.c.id.in_(run_ids))
    )
    async with database.read_transaction() as reader:
        with await reader.execute(statement) as result:
            return {row[0]: tuple(row[1:]) for row in result}


@pytest.mark.asyncio
async def test_pending_promotion_skips_blocked_queue_before_payload_or_writer(
    database: Database, preparer: DeploymentPreparer, api_capture: Capture
) -> None:
    user = await seed_case(database, NAMESPACE)
    headers = {"Authorization": str(user.request.authorization)}
    app = application(
        database, api_capture, deployment_services(database, preparer, HARNESS)
    )
    promotion = pending_promotion(database)
    independent_graph = "independent"

    async def process(accepted: AcceptedRun) -> PromotionOutcome:
        deadline = asyncio.get_running_loop().time() + 10.0
        return await promotion.process(
            accepted.run_id.removeprefix("run_"),
            deadline=deadline,
            stopped=lambda: False,
        )

    async with client_for(app) as client:
        await deploy(client, headers, "01h", overlap="queue")
        await deploy(client, headers, "01h", graph=independent_graph)
        running = await trigger(client, headers, key="queue-running")
        assert await process(running) is PromotionOutcome.APPLIED
        older = await trigger(client, headers, key="queue-older")
        younger = await trigger(client, headers, key="queue-younger")
        independent = await trigger(
            client, headers, graph=independent_graph, key="queue-independent"
        )
    blocked_ids = tuple(
        candidate.run_id.removeprefix("run_") for candidate in (older, younger)
    )
    running_id = running.run_id.removeprefix("run_")
    independent_id = independent.run_id.removeprefix("run_")
    all_ids = (running_id, *blocked_ids, independent_id)
    before = await admitted_runs(database, all_ids)
    ledger = ConnectionLedger(database)

    with promotion_statements(database) as blocked:
        for candidate in (younger, older):
            await process(candidate)
    blocked_checkouts = ledger.checkouts
    with promotion_statements(database) as available:
        outcome = await process(independent)
    after = await admitted_runs(database, all_ids)
    async with database.read_transaction() as reader:
        with await reader.execute(
            select(event.c.run_id).where(
                event.c.run_id.in_(all_ids), event.c.kind == "run.started"
            )
        ) as result:
            started = set(result.scalars())

    assert set(before) == set(after) == set(all_ids)
    assert blocked_checkouts == len(blocked_ids)
    assert any("run_promotion_admission" in sql for sql, _ in blocked)
    assert all(
        "graph_version_read_admission" not in sql and "v.ir" not in sql and not writer
        for sql, writer in blocked
    )
    assert all(
        after[run_id] == before[run_id] and after[run_id][-1] == "pending"
        for run_id in blocked_ids
    )
    assert after[running_id] == before[running_id]
    assert after[running_id][-1] == "running"
    assert outcome is PromotionOutcome.APPLIED
    assert any("graph_version_read_admission" in sql for sql, _ in available)
    assert any(writer for _, writer in available)
    assert after[independent_id][:-1] == before[independent_id][:-1]
    assert after[independent_id][-1] == "running"
    assert started == {running_id, independent_id}
    assert ledger.outstanding == database.units_in_flight() == 0
