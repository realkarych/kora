from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.connection import Database
from kora.db.tables import attempt, attempt_dispatcher, node_instance, run
from kora.engine.deadline_records import deadline_document, prepare_deadline_record
from kora.engine.expiry_context import ExpiryContext
from kora.engine.locks import Scope, ScopeKind, TransitionLocks
from kora.protocols.deadlines import ExecutionDeadline
from tests.db.seed import Seeder
from tests.engine.promotion_support import READ_LIMITS, PromotionCase, promotion_case


@dataclass(frozen=True)
class ExecutionCase:
    promotion: PromotionCase
    node_id: str
    attempt_id: str
    domain_id: str
    grant_id: str | None

    async def context(
        self, writer: AsyncConnection, capacity: bool = True
    ) -> ExpiryContext:
        return await expiry_context(writer, self.promotion, capacity)

    async def snapshot(self, database: Database) -> dict[str, object]:
        async with database.read_transaction() as reader:
            with await reader.execute(
                select(attempt).where(attempt.c.id == self.attempt_id)
            ) as result:
                return dict(result.mappings().one())


async def expiry_context(
    writer: AsyncConnection, case: PromotionCase, capacity: bool = True
) -> ExpiryContext:
    locks = TransitionLocks(writer)
    await locks.acquire_run(case.run_id)
    if capacity:
        await locks.acquire_scopes(
            (
                Scope(ScopeKind.GLOBAL),
                Scope(ScopeKind.NAMESPACE, (case.scope.namespace_id,)),
                Scope(ScopeKind.GRAPH, (case.scope.graph_id,)),
                Scope(ScopeKind.RUNTIME, ("local",)),
            )
        )
    return ExpiryContext(
        writer,
        locks,
        case.scope,
        case.run_id,
        case.clock.now,
        case.identifiers,
        READ_LIMITS,
        1_000_000,
    )


async def execution_case(
    database: Database,
    *,
    timeout: str | None = "1h",
    granted: bool = True,
    status: str = "running",
) -> ExecutionCase:
    case = await promotion_case(database, timeout=timeout)
    await case.promote(database)
    async with database.transaction() as writer:
        seed = Seeder(writer)
        node_id = str(
            await writer.scalar(
                select(node_instance.c.id)
                .where(node_instance.c.run_id == case.run_id)
                .order_by(node_instance.c.id)
                .limit(1)
            )
        )
        attempt_id = await seed.attempt(node_id, status if granted else "created")
        domain = seed.new_id()
        grant_id = seed.new_id() if granted else None
        worker_id = await seed.worker(await seed.token("a" * 64, kind="worker"))
        deadline = ExecutionDeadline(
            at=case.clock.now() + timedelta(seconds=60),
            scope_kind="attempt",
            scope_id=attempt_id,
            clock_uncertainty=timedelta(0),
            grace=timedelta(seconds=30),
        )
        await writer.execute(
            update(node_instance)
            .where(node_instance.c.id == node_id)
            .values(current_attempt_id=attempt_id, status="running", attempt_count=1)
        )
        await writer.execute(
            update(run)
            .where(run.c.id == case.run_id)
            .values(
                needs_scheduling=False,
                target_key="held",
                target_held_since=case.clock.now(),
            )
        )
        await writer.execute(
            update(attempt)
            .where(attempt.c.id == attempt_id)
            .values(
                execution_state="live" if granted else "not_started",
                execution_domain_id=domain if granted else None,
                worker_id=worker_id if granted else None,
                lease_owner=worker_id if granted else None,
                lease_epoch=1 if granted else 0,
                lease_until=case.clock.now() + timedelta(seconds=30)
                if granted
                else None,
                started_at=case.clock.now() if granted else None,
                deadline=deadline.at if granted else None,
                deadline_binding=deadline_document(prepare_deadline_record(deadline))
                if granted
                else None,
                recovery={
                    "last_observation": {"state": "live", "at": "original"},
                    "pending_action": "stop",
                    "publication_operations": [
                        {"id": "publication", "state": "unknown"}
                    ],
                },
            )
        )
        if grant_id is not None:
            await writer.execute(
                insert(attempt_dispatcher).values(
                    id=grant_id,
                    attempt_id=attempt_id,
                    execution_domain_id=domain,
                    worker_id=worker_id,
                    incarnation_id=seed.new_id(),
                    lease_epoch=1,
                    mode="execute",
                    granted_at=case.clock.now(),
                    closed_at=None,
                    evidence=None,
                    **seed.stamps(),
                )
            )
    return ExecutionCase(case, node_id, attempt_id, domain, grant_id)
