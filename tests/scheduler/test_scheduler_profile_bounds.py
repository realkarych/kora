import asyncio
from dataclasses import replace

import httpx
import pytest
from sqlalchemy import event, literal, select, update
from sqlalchemy.engine import Connection

from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.conformance.capture import Capture
from kora.db.connection import Database, Dialect
from kora.db.maintenance import POSTGRESQL_TIMEOUT, SET_POSTGRESQL_TIMEOUT
from kora.db.run_input_parent import SQLITE_RECORD_HEADER_MAX, sqlite_storage_width
from kora.db.tables import attempt, attempt_dispatcher, node_instance, run, wait
from kora.db.transactions import WRITER_LEASE
from kora.engine.admission_snapshot import acquire_admission_snapshot
from kora.engine.attempt_admission import AttemptAdmissionState
from kora.engine.run_promotion import PromotionOutcome
from kora.models.runs import AcceptedRun
from tests.api.deployment_cases import preparer
from tests.api.scheduler_claim_support import node_id, scheduler_claim_setup
from tests.api.support import api_capture, client_for
from tests.db.closing import ConnectionLedger
from tests.engine.claim_support import state
from tests.engine.test_claim_snapshot import ClaimQueries
from tests.engine.test_http_trigger_admission import KEY, RUNS
from tests.engine.test_pending_promotion import deploy, pending_promotion
from tests.scheduler.test_first_claim_scheduler import BOUND

__all__ = ["api_capture", "preparer"]
SNAPSHOT_CONTROL: frozenset[str] = frozenset(
    {
        "BEGIN DEFERRED",
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY",
        POSTGRESQL_TIMEOUT,
        SET_POSTGRESQL_TIMEOUT,
    }
)


async def sqlite_parent_width(database: Database, run_id: str) -> int:
    width = sum(
        (sqlite_storage_width(column) for column in run.c),
        literal(SQLITE_RECORD_HEADER_MAX),
    )
    async with database.read_transaction() as reader:
        value = await reader.scalar(select(width).where(run.c.id == run_id))
    assert type(value) is int
    return value


async def accepted_run(
    client: httpx.AsyncClient, headers: dict[str, str], key: str
) -> AcceptedRun:
    response = await client.post(
        RUNS, json={"params": {}}, headers=headers | {"Idempotency-Key": key}
    )
    assert response.status_code == 201
    return AcceptedRun.model_validate(response.json())


@pytest.mark.asyncio
@pytest.mark.parametrize("overwidth", [False, True])
async def test_scheduler_preserves_accepted_run_at_worker_parent_bound(
    database: Database,
    preparer: DeploymentPreparer,
    api_capture: Capture,
    overwidth: bool,
) -> None:
    setup = await scheduler_claim_setup(database, preparer, api_capture)
    headers = {"Authorization": str(setup.user.request.authorization)}
    configuration = setup.processor.limits
    preparation = configuration.preparation
    source = preparation.snapshot
    bounded = replace(source, inputs=replace(source.inputs, parent=4096))
    admission = replace(
        setup.processor,
        limits=replace(
            configuration, preparation=replace(preparation, snapshot=bounded)
        ),
        initial_root_only=True,
    )
    promotion = pending_promotion(database)

    target_width = 4096 + int(overwidth)
    sqlite = database.dialect is Dialect.SQLITE
    async with client_for(setup.app) as client:
        padding = 4096 if overwidth else 2048
        expected_runs: set[str] = set()
        if sqlite:
            await deploy(client, headers, "01h")
            baseline = await accepted_run(client, headers, f"w{KEY[1:]}")
            baseline_id = baseline.run_id.removeprefix("run_")
            expected_runs.add(baseline_id)
            baseline_deadline = asyncio.get_running_loop().time() + 10.0
            baseline_outcome = await promotion.process(
                baseline_id, deadline=baseline_deadline, stopped=lambda: False
            )
            assert baseline_outcome is PromotionOutcome.APPLIED
            padding = (
                target_width - await sqlite_parent_width(database, baseline_id) + 1
            )
        await deploy(client, headers, "0" * padding + "1h")
        accepted = await accepted_run(client, headers, KEY)
        run_id = accepted.run_id.removeprefix("run_")
        expected_runs.add(run_id)
        deadline = asyncio.get_running_loop().time() + 10.0
        promoted = await promotion.process(
            run_id, deadline=deadline, stopped=lambda: False
        )
        actual_width = await sqlite_parent_width(database, run_id) if sqlite else None
        root = await node_id(database, run_id, "agent")
        admitted = await admission.process(
            root, periodic_recheck=True, deadline=deadline
        )
        replay = await client.post(
            RUNS, json={"params": {}}, headers=headers | {"Idempotency-Key": KEY}
        )
    async with database.read_transaction() as reader:
        with await reader.execute(select(run.c.id)) as result:
            runs = list(result.scalars())
        with await reader.execute(select(attempt.c.id)) as result:
            reservations = list(result.scalars())
        grant = await reader.scalar(select(attempt_dispatcher.c.id))

    refused = overwidth and sqlite
    assert promoted is PromotionOutcome.APPLIED
    assert admitted is (
        AttemptAdmissionState.REFUSED if refused else AttemptAdmissionState.QUEUED
    )
    assert len(reservations) == (0 if refused else 1)
    assert set(runs) == expected_runs
    assert actual_width == (target_width if sqlite else None)
    assert grant is None
    assert replay.status_code == 200
    assert AcceptedRun.model_validate(replay.json()) == accepted
    assert database.units_in_flight() == 0


async def state_with_waits(database: Database) -> dict[str, list[dict[str, object]]]:
    saved = await state(database)
    async with database.read_transaction() as reader:
        with await reader.execute(select(wait).order_by(wait.c.id)) as result:
            saved["wait"] = [dict(row) for row in result.mappings()]
    return saved


@pytest.mark.asyncio
@pytest.mark.parametrize("saved_kind", ["command", "agent"])
async def test_scheduler_refuses_ready_dependent_before_reservation(
    database: Database,
    preparer: DeploymentPreparer,
    api_capture: Capture,
    saved_kind: str,
) -> None:
    setup = await scheduler_claim_setup(database, preparer, api_capture)
    headers = {"Authorization": str(setup.user.request.authorization)}
    admission = replace(setup.processor, initial_root_only=True)
    promotion = pending_promotion(database)
    async with client_for(setup.app) as client:
        await deploy(client, headers, "1h")
        accepted = await accepted_run(client, headers, KEY)
    run_id = accepted.run_id.removeprefix("run_")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + BOUND
    assert (
        await promotion.process(run_id, deadline=deadline, stopped=lambda: False)
        is PromotionOutcome.APPLIED
    )
    dependent = await node_id(database, run_id, "gate")
    async with database.transaction() as writer:
        original_kind = await writer.scalar(
            select(node_instance.c.kind).where(node_instance.c.id == dependent)
        )
        await writer.execute(
            update(node_instance)
            .where(node_instance.c.id == dependent)
            .values(status="ready")
        )
        if saved_kind == "agent":
            await writer.execute(
                update(node_instance)
                .where(node_instance.c.id == dependent)
                .values(kind="agent")
            )
    before = await state_with_waits(database)
    ledger = ConnectionLedger(database)
    writer_statements: list[str] = []

    def observe(
        connection: Connection, cursor: object, statement: str, *arguments: object
    ) -> None:
        if WRITER_LEASE in connection.info:
            writer_statements.append(statement)

    target = database.writer_engine.sync_engine

    event.listen(target, "before_cursor_execute", observe)
    try:
        with ClaimQueries(database) as staging:
            outcome = await admission.process(
                dependent, periodic_recheck=True, deadline=loop.time() + BOUND
            )
    finally:
        event.remove(target, "before_cursor_execute", observe)
    staging_checkouts, staging_outstanding = ledger.checkouts, ledger.outstanding
    staging_units = database.units_in_flight()
    with ClaimQueries(database) as generic:
        acquired = await acquire_admission_snapshot(
            database,
            dependent,
            admission.limits.preparation.snapshot,
            observed_at=setup.clock.now(),
            periodic_recheck=True,
            deadline=loop.time() + BOUND,
        )
    after = await state_with_waits(database)

    saved = next(row for row in before["node_instance"] if row["id"] == dependent)
    assert original_kind == "command"
    assert saved["kind"] == saved_kind
    assert saved["run_id"] == run_id
    assert saved["node_path"] == "gate"
    assert outcome is AttemptAdmissionState.REFUSED
    assert not writer_statements
    assert staging_checkouts == 1
    assert staging_outstanding == staging_units == 0
    staging_reads = [
        statement.lower()
        for statement in staging.statements
        if statement not in SNAPSHOT_CONTROL
    ]
    if saved_kind == "command":
        (candidate_read,) = staging_reads
        assert candidate_read.startswith("select ")
        assert "node_instance" in candidate_read
    else:
        assert any("graph_version_read_admission" in sql for sql in staging_reads)
    assert acquired.candidate.node_id == dependent
    assert acquired.candidate.run.run_id == run_id
    assert acquired.graph.graph.id == acquired.candidate.run.version_id
    assert acquired.cap_graph.graph.id == acquired.cap_source.version_id
    assert acquired.params == b"{}"
    assert all(
        any(table in statement.lower() for statement in generic.statements)
        for table in (
            "graph_version_read_admission",
            "graph_version.number",
            "price_table",
            "run_inputs",
            "namespace.policy",
        )
    )
    assert before == after
    assert not after["attempt"]
    assert ledger.outstanding == database.units_in_flight() == 0
