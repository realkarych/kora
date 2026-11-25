import asyncio
from collections import Counter
from contextlib import ExitStack
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from json import JSONDecodeError
from typing import Literal, cast

import httpx
import pytest
from sqlalchemy import event as sql_events
from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import Connection, ExecutionContext
from sqlalchemy.exc import SQLAlchemyError

from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.conformance.capture import Capture, CaptureOverflow, CaptureTimeout
from kora.db.connection import (
    Database,
    DatabaseClosedError,
    DatabaseSnapshotRejected,
    Dialect,
)
from kora.db.identifiers import is_identifier
from kora.db.tables import attempt, event, node_instance, run, wait
from kora.db.transactions import WRITER_LEASE
from kora.engine.graph_snapshot import GraphSnapshotLimits
from kora.engine.pending_promotion import PendingPromotion
from kora.engine.run_promotion import PromotionOutcome
from kora.engine.staging_maintenance import StagingMaintenance
from kora.models.runs import AcceptedRun
from kora.scheduler.discovery import Discovery
from kora.scheduler.tick import Tick
from tests.api.deployment_cases import GRAPH, preparer
from tests.api.scheduler_claim_support import (
    PARAMS,
    node_id,
    scheduler_claim_ir,
    scheduler_claim_setup,
)
from tests.api.support import api_capture
from tests.client.support import running_app
from tests.db.closing import ConnectionLedger
from tests.db.seed import Seeder
from tests.engine.admission_processing_support import processor
from tests.engine.admission_snapshot_support import snapshot_case
from tests.engine.expiry_support import execution_case
from tests.engine.promotion_support import append_pending
from tests.engine.test_http_trigger_admission import RUNS, VERSIONS
from tests.scheduler.test_first_claim_scheduler import BOUND, scheduler

__all__ = ["api_capture", "preparer"]

RecoveryPhase = Literal["candidate", "managed_parent", "wait_insert"]
MAX_RECOVERY_MARKS = 64
CAPTURE_FIELDS = {
    "engine.admission_discovery.selected": "count",
    "engine.admission_snapshot.acquired": "acquired",
    "engine.admission_preparation.completed": "prepared",
    "engine.admission_processing.completed": "outcome",
}
ADMISSION_OUTCOMES = frozenset(
    {"queued", "waiting", "expired", "refused", "cancelled", "failed"}
)
RUN_STATES = frozenset({"pending", "running", "failed", "cancelled", "succeeded"})
ROOT_STATES = frozenset({"pending", "ready", "waiting", "failed", "cancelled"})
SCHEDULER_READBACK_UNKNOWN = (
    "Scheduler timeout durable_readback=unknown "
    "root_lookup=unknown root_outcomes=unknown"
)
FIXTURE_SQL_KEY = ' \nWHERE (id COLLATE "BINARY") = ? AND '
CANDIDATE_SOURCE = "\nFROM main.node_instance INDEXED BY ix_claim_node_locator"
PARENT_SOURCE = "\nFROM main.run INDEXED BY ix_run_input_locator"
WAIT_INSERT = (
    "INSERT INTO wait (id, node_instance_id, attempt_id, kind, wake_at, reason, "
    "status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def recovery_statement(statement: str) -> tuple[RecoveryPhase, int] | None:
    if statement == WAIT_INSERT:
        return "wait_insert", 1
    if statement.startswith("SELECT CASE WHEN "):
        if CANDIDATE_SOURCE + FIXTURE_SQL_KEY in statement:
            return "candidate", -1
        if PARENT_SOURCE + FIXTURE_SQL_KEY in statement:
            return "managed_parent", -1
    return None


SQLITE_BIND_COUNTS = {
    "candidate": (232, 283),
    "managed_parent": (118, 141),
    "wait_insert": (9,),
}


@dataclass(frozen=True, slots=True)
class RecoveryMark:
    phase: RecoveryPhase
    identity: str
    returned: bool


class SQLiteRecoveryEvidence:
    def __init__(self, database: Database, observers: ExitStack) -> None:
        self.marks: list[RecoveryMark] = []
        self.pending: tuple[int, RecoveryMark] | None = None
        self.incomplete = False
        for engine in (database.reader_engine, database.writer_engine):
            target = engine.sync_engine
            sql_events.listen(target, "before_cursor_execute", self.before_dispatch)
            observers.callback(
                sql_events.remove, target, "before_cursor_execute", self.before_dispatch
            )
            sql_events.listen(target, "after_cursor_execute", self.driver_return)
            observers.callback(
                sql_events.remove, target, "after_cursor_execute", self.driver_return
            )

    def append(self, mark: RecoveryMark) -> None:
        if len(self.marks) < MAX_RECOVERY_MARKS:
            self.marks.append(mark)
        else:
            self.incomplete = True

    def before_dispatch(
        self,
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: ExecutionContext,
        executemany: bool,
    ) -> None:
        self.pending = None
        shape = recovery_statement(statement)
        if shape is None:
            return
        phase, position = shape
        if phase == "managed_parent" and WRITER_LEASE not in connection.info:
            return
        if phase == "wait_insert" and WRITER_LEASE not in connection.info:
            self.incomplete = True
            return
        if executemany or not isinstance(parameters, tuple):
            self.incomplete = True
            return
        values = cast("tuple[object, ...]", parameters)
        length = len(values)
        if length not in SQLITE_BIND_COUNTS[phase] or statement.count("?") != length:
            self.incomplete = True
            return
        identity = values[position]
        if not isinstance(identity, str) or not is_identifier(identity):
            self.incomplete = True
            return
        mark = RecoveryMark(phase, identity, False)
        self.append(mark)
        self.pending = (id(context), mark)

    def driver_return(
        self,
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: ExecutionContext,
        executemany: bool,
    ) -> None:
        pending = self.pending
        self.pending = None
        if pending is not None and pending[0] == id(context):
            self.append(replace(pending[1], returned=True))
        else:
            shape = recovery_statement(statement)
            if shape is not None and (
                shape[0] == "candidate" or WRITER_LEASE in connection.info
            ):
                self.incomplete = True

    def summary(self, run_id: str, root: str | None, cutoff: int) -> str:
        if self.incomplete:
            return "native=unknown"
        targets = {"candidate": root, "managed_parent": run_id, "wait_insert": root}
        before, joined = Counter[tuple[str, str]](), Counter[tuple[str, str]]()
        for index, mark in enumerate(self.marks):
            if targets[mark.phase] == mark.identity:
                label = "driver_return" if mark.returned else "before_dispatch"
                (before if index < cutoff else joined)[mark.phase, label] += 1
        return (
            f"native_through_watchdog={dict(before)!r} "
            f"native_during_join={dict(joined)!r}"
        )


def recovery_capture_note(
    capture: Capture, evidence: SQLiteRecoveryEvidence, root: str | None
) -> str:
    if evidence.incomplete:
        return "root_lookup=unknown root_outcomes=unknown"
    native = [
        mark.identity
        for mark in evidence.marks
        if mark.phase == "candidate" and not mark.returned
    ]
    lookup = "observed_through_join" if root in native else "unobserved"
    unknown = f"root_lookup={lookup} capture=unknown root_outcomes=unknown"
    try:
        records = capture.finish()
    except (
        CaptureOverflow,
        CaptureTimeout,
        JSONDecodeError,
        UnicodeDecodeError,
        OSError,
    ):
        return unknown
    snapshot = capture.diagnostics.snapshot()
    if (
        snapshot.dropped_total()
        or snapshot.rejected_total()
        or snapshot.suppressed_total()
        or snapshot.sink_failures
        or capture.reader.discarded
    ):
        return unknown
    observed: list[tuple[str, bool | int | str]] = []
    wraps = 0
    for record in records:
        event_name = record.get("event")
        if not isinstance(event_name, str) or event_name not in CAPTURE_FIELDS:
            continue
        field = CAPTURE_FIELDS[event_name]
        value = record.get(field)
        if field == "count":
            wrapped = record.get("wrapped")
            if (
                type(value) is not int
                or value not in (0, 1)
                or type(wrapped) is not bool
            ):
                return unknown
            wraps += wrapped
            if value == 0:
                continue
        elif field == "outcome":
            if not isinstance(value, str) or value not in ADMISSION_OUTCOMES:
                return unknown
        elif type(value) is not bool:
            return unknown
        observed.append((field, value))
        if len(observed) > MAX_RECOVERY_MARKS:
            return unknown
    labels = [name for name, _ in observed]
    prefix = (
        f"root_lookup={lookup} "
        f"selected_candidates_through_join={labels.count('count')} "
        f"wraps_through_join={wraps}"
    )
    if lookup == "unobserved" or labels != [
        "count",
        "acquired",
        "prepared",
        "outcome",
    ] * len(native):
        return f"{prefix} root_outcomes=unknown"
    outcomes = [
        tuple(value for _, value in observed[index * 4 + 1 : index * 4 + 4])
        for index, identity in enumerate(native)
        if identity == root
    ]
    return f"{prefix} root_acquired_prepared_outcome_through_join={outcomes!r}"


async def recovery_timeout_note(
    database: Database,
    accepted: list[str],
    evidence: SQLiteRecoveryEvidence,
    capture: Capture,
    cutoff: int,
) -> str:
    if len(accepted) != 2:
        return SCHEDULER_READBACK_UNKNOWN
    wait_count = (
        select(func.count())
        .select_from(wait)
        .where(
            wait.c.node_instance_id == node_instance.c.id,
            wait.c.kind == "admission",
            wait.c.status == "open",
        )
        .correlate(node_instance)
        .scalar_subquery()
    )
    query = (
        select(
            run.c.id,
            run.c.status,
            node_instance.c.id,
            node_instance.c.status,
            wait_count,
        )
        .select_from(
            run.outerjoin(
                node_instance,
                (node_instance.c.run_id == run.c.id)
                & (node_instance.c.node_path == "agent"),
            )
        )
        .where(run.c.id.in_(accepted))
        .limit(3)
    )
    try:
        deadline = asyncio.get_running_loop().time() + capture.deadline_seconds
        async with database.read_snapshot(deadline=deadline) as reader:
            rows = (await reader.execute(query)).all()
    except (DatabaseClosedError, DatabaseSnapshotRejected, SQLAlchemyError, OSError):
        return SCHEDULER_READBACK_UNKNOWN
    if len(rows) != 2 or {row[0] for row in rows} != set(accepted):
        return SCHEDULER_READBACK_UNKNOWN
    _, run_status, root, root_status, waits = next(
        row for row in rows if row[0] == accepted[1]
    )
    if (
        type(run_status) is not str
        or run_status not in RUN_STATES
        or type(waits) is not int
        or waits < 0
        or (
            root is not None
            and (
                not isinstance(root, str)
                or not is_identifier(root)
                or type(root_status) is not str
                or root_status not in ROOT_STATES
            )
        )
    ):
        return SCHEDULER_READBACK_UNKNOWN
    state = root_status if root else "unmaterialized"
    return (
        f"Scheduler second root run={run_status} state={state} durable_waits={waits} "
        f"{evidence.summary(accepted[1], root, cutoff)} "
        f"{recovery_capture_note(capture, evidence, root)}"
    )


async def pending_runs(origin: str, authorization: str) -> list[str]:
    headers = {"Authorization": authorization}
    async with httpx.AsyncClient(
        base_url=origin, trust_env=False, timeout=BOUND
    ) as client:
        deployed = await client.post(
            VERSIONS,
            json={"graph": GRAPH, "ir": scheduler_claim_ir(), "resources": []},
            headers=headers,
        )
        assert deployed.status_code == 201
        accepted: list[str] = []
        for index in range(2):
            result = await client.post(
                RUNS,
                json={"params": PARAMS},
                headers=headers | {"Idempotency-Key": f"scheduler-late-{index}"},
            )
            assert result.status_code == 201
            accepted.append(
                AcceptedRun.model_validate(result.json()).run_id.removeprefix("run_")
            )
    return accepted


async def admitted_waits(
    database: Database, run_ids: list[str]
) -> list[tuple[str, datetime]]:
    query = (
        select(node_instance.c.id, wait.c.wake_at)
        .join(wait, wait.c.node_instance_id == node_instance.c.id)
        .join(event, event.c.node_instance_id == node_instance.c.id)
        .where(
            node_instance.c.run_id.in_(run_ids),
            wait.c.kind == "admission",
            wait.c.status == "open",
            event.c.kind == "node.waiting",
        )
        .order_by(event.c.id)
    )
    async with database.read_transaction() as reader:
        with await reader.execute(query) as result:
            return [(row.id, row.wake_at) for row in result]


@pytest.mark.asyncio
async def test_scheduler_rotates_past_future_wait_and_recovers_terminal_parent(
    database: Database, preparer: DeploymentPreparer, api_capture: Capture
) -> None:
    setup = await scheduler_claim_setup(database, preparer, api_capture)
    orphan = await execution_case(database)
    setup.clock.advance(60)
    cutoff = setup.clock.now()
    async with database.transaction() as writer:
        await writer.execute(
            update(run)
            .where(run.c.id == orphan.promotion.run_id)
            .values(status="cancelled", needs_scheduling=False)
        )
        await writer.execute(
            update(attempt)
            .where(attempt.c.id == orphan.attempt_id)
            .values(lease_until=cutoff)
        )
    owner = scheduler(database, setup.clock, 43, page_size=1)
    async with running_app(setup.app) as origin:
        accepted = await pending_runs(origin, str(setup.user.request.authorization))

    await owner.recover()
    recovered = await orphan.snapshot(database)
    settled_waits = asyncio.Event()
    pending_inserts = len(accepted)

    def observe(
        connection: object, cursor: object, statement: str, *arguments: object
    ) -> None:
        nonlocal pending_inserts
        if statement.lower().startswith("insert into wait "):
            pending_inserts -= 1
            if pending_inserts == 0:
                settled_waits.set()

    writer_engine = database.writer_engine.sync_engine
    with ExitStack() as observers:
        sql_events.listen(writer_engine, "after_cursor_execute", observe)
        observers.callback(
            sql_events.remove, writer_engine, "after_cursor_execute", observe
        )
        evidence = (
            SQLiteRecoveryEvidence(database, observers)
            if database.dialect is Dialect.SQLITE
            else None
        )
        task = asyncio.create_task(owner.run())
        try:
            await asyncio.wait_for(settled_waits.wait(), BOUND)
        except TimeoutError as error:
            cutoff = len(evidence.marks) if evidence is not None else 0
            owner.stop()
            await task
            failure = AssertionError(
                (pending_inserts, await admitted_waits(database, accepted))
            )
            observers.close()
            if evidence is not None:
                failure.add_note(
                    await recovery_timeout_note(
                        database, accepted, evidence, api_capture, cutoff
                    )
                )
            raise failure from error
        finally:
            owner.stop()
            await task
    observed = await admitted_waits(database, accepted)
    async with database.read_transaction() as reader:
        unexpected = await reader.scalar(
            select(attempt.c.id).where(attempt.c.id != orphan.attempt_id)
        )

    assert len(observed) == len(accepted)
    assert recovered["recovery_required"] is True
    assert recovered["execution_domain_id"] == orphan.domain_id
    assert all(wake == cutoff + timedelta(seconds=1) for _, wake in observed)
    assert [node for node, _ in observed] == sorted(node for node, _ in observed)
    assert unexpected is None
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_scheduler_tick_continues_open_admission_sweep_without_hot_wrap(
    database: Database, preparer: DeploymentPreparer, api_capture: Capture
) -> None:
    case = await snapshot_case(database, preparer, api_capture)
    roots = [case.node_id]
    run_ids = [case.promotion.run_id]
    for _ in range(2):
        added = await append_pending(database, case.promotion)
        assert await added.promote(database) is PromotionOutcome.APPLIED
        roots.append(await node_id(database, added.run_id, "agent"))
        run_ids.append(added.run_id)
    admission = replace(
        processor(database, case), runtime_limit=None, initial_root_only=True
    )
    clock, identifiers = admission.clock, admission.identifiers
    wake = clock() + timedelta(seconds=1)
    async with database.transaction() as writer:
        seed = Seeder(writer)
        await writer.execute(
            update(run).where(run.c.id.in_(run_ids)).values(needs_scheduling=False)
        )
        await writer.execute(
            update(node_instance)
            .where(node_instance.c.id.in_(roots))
            .values(status="waiting")
        )
        await writer.execute(
            update(node_instance)
            .where(
                node_instance.c.run_id.in_(run_ids),
                node_instance.c.id.not_in(roots),
            )
            .values(status="cancelled")
        )
        for root in roots:
            await writer.execute(
                insert(wait).values(
                    id=seed.new_id(),
                    node_instance_id=root,
                    kind="admission",
                    status="open",
                    wake_at=wake,
                    **seed.stamps(),
                )
            )
    limits = admission.limits
    source = limits.preparation.snapshot
    graph_limits = GraphSnapshotLimits(
        source.graph, source.graph_native_bytes, source.owner_text_bytes
    )
    tick = Tick(
        Discovery(database, 2),
        clock,
        BOUND,
        PendingPromotion(database, graph_limits, limits.promotion, clock, identifiers),
        admission,
        StagingMaintenance(
            database,
            limits.promotion,
            limits.sources.attempt_row,
            clock,
            identifiers,
            admission.selection.runtime,
        ),
    )
    ledger = ConnectionLedger(database)
    event_count = select(func.count()).select_from(event)
    async with database.read_transaction() as reader:
        initial_events = await reader.scalar(event_count)
    stopped = asyncio.Event()
    processed: list[str] = []

    def observe(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        *arguments: object,
    ) -> None:
        if statement.startswith("SELECT id, kind, attempt_id "):
            assert isinstance(parameters, tuple)
            node = cast("tuple[object, ...]", parameters)[0]
            assert isinstance(node, str)
            processed.append(node)

    reader_engine = database.reader_engine.sync_engine
    sql_events.listen(reader_engine, "after_cursor_execute", observe)
    try:
        continuation = [await tick(stopped.is_set), await tick(stopped.is_set)]
    finally:
        sql_events.remove(reader_engine, "after_cursor_execute", observe)
    selected = "engine.admission_discovery.selected"
    completed = "engine.admission_processing.completed"
    observed = [
        (
            record["event"],
            record.get("count"),
            record.get("wrapped"),
            record.get("outcome"),
        )
        for record in api_capture.finish()
        if record["event"] in (selected, completed, "scheduler.transition")
    ]
    saved = (
        select(wait.c.node_instance_id, wait.c.wake_at, node_instance.c.status)
        .join(node_instance, node_instance.c.id == wait.c.node_instance_id)
        .where(wait.c.kind == "admission", wait.c.status == "open")
        .order_by(wait.c.node_instance_id)
    )
    async with database.read_transaction() as reader:
        with await reader.execute(saved) as result:
            waits = result.all()
        reservation = await reader.scalar(select(attempt.c.id).limit(1))
        final_events = await reader.scalar(event_count)

    assert continuation == [True, False]
    assert observed == [
        (selected, 2, True, None),
        (completed, None, None, "refused"),
        (completed, None, None, "refused"),
        (selected, 1, False, None),
        (completed, None, None, "refused"),
    ]
    assert processed == sorted(roots)
    assert waits == [(root, wake, "waiting") for root in sorted(roots)]
    assert reservation is None
    assert initial_events == final_events
    assert ledger.outstanding == database.units_in_flight() == 0
