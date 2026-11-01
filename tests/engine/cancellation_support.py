from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.connection import Database
from kora.db.identifiers import IdentifierGenerator
from kora.db.tables import event, inbound_event, run
from kora.engine.cancellation import CancellationResult, cancel_pending_run
from kora.engine.locks import Scope, ScopeKind, TransitionLocks
from kora.protocols.authentication import NamespaceGrant, Principal
from tests.db.seed import FakeClock, Seeder, fixed_randomness

PRIVATE_TRIGGER: str = "private-trigger-never-in-diagnostics"
PRIVATE_PARAMS: str = "private-params-never-in-diagnostics"


class CountedClock(FakeClock):
    reads: int = 0

    def now(self) -> datetime:
        self.reads += 1
        return self.moment


@dataclass
class PendingCancellation:
    database: Database
    namespace_id: str
    graph_id: str
    version_id: str
    run_id: str
    original_id: str
    principal: Principal
    clock: CountedClock = field(default_factory=CountedClock)

    def scopes(self) -> tuple[Scope, ...]:
        return (
            Scope(ScopeKind.GLOBAL),
            Scope(ScopeKind.NAMESPACE, (self.namespace_id,)),
            Scope(ScopeKind.GRAPH, (self.graph_id,)),
        )

    async def acquire(self, connection: AsyncConnection) -> TransitionLocks:
        locks = TransitionLocks(connection)
        await locks.acquire_run(self.run_id)
        await locks.acquire_scopes(self.scopes())
        return locks

    def identifiers(self) -> IdentifierGenerator:
        return IdentifierGenerator(self.clock.now, fixed_randomness(18))

    async def transition(
        self,
        connection: AsyncConnection,
        locks: TransitionLocks,
        principal: Principal | None = None,
    ) -> CancellationResult:
        return await cancel_pending_run(
            connection,
            locks,
            self.principal if principal is None else principal,
            self.namespace_id,
            self.graph_id,
            self.run_id,
            self.clock.now,
            self.identifiers(),
        )

    async def apply(self, principal: Principal | None = None) -> CancellationResult:
        async def transaction(connection: AsyncConnection) -> CancellationResult:
            return await self.transition(
                connection, await self.acquire(connection), principal
            )

        return await self.database.run(transaction)

    async def snapshot(
        self,
    ) -> tuple[dict[str, object], list[dict[str, object]], list[dict[str, object]]]:
        async with self.database.read_transaction() as connection:
            run_result = await connection.execute(
                select(run).where(run.c.id == self.run_id)
            )
            event_result = await connection.execute(select(event).order_by(event.c.id))
            receipt_result = await connection.execute(
                select(inbound_event).order_by(inbound_event.c.id)
            )
            return (
                dict(run_result.mappings().one()),
                [dict(row) for row in event_result.mappings()],
                [dict(row) for row in receipt_result.mappings()],
            )


async def pending_cancellation(
    database: Database, marked: bool = False
) -> PendingCancellation:
    async with database.transaction() as connection:
        seed = Seeder(connection)
        namespace_id = await seed.namespace()
        graph_id = await seed.graph(namespace_id)
        version_id = await seed.graph_version(graph_id)
        original_id = await seed.run(
            namespace_id, graph_id, version_id, status="succeeded"
        )
        values = seed.run_values(namespace_id, graph_id, version_id, "pending", marked)
        values.update(
            rerun_of_run_id=original_id,
            params={"private": PRIVATE_PARAMS},
            cel_params_projection={"private": PRIVATE_PARAMS},
            trigger={"kind": "manual", "occurrence": PRIVATE_TRIGGER},
            retention={"transcripts": 30, "artifact_payloads": 30},
            retention_version=1,
            target_key="bound-target-without-hold",
        )
        await connection.execute(insert(run).values(**values))
        run_id = str(values["id"])
        principal = Principal(
            token_id=seed.new_id(),
            kind="user",
            owner=seed.new_id(),
            namespaces=(NamespaceGrant(namespace_id=namespace_id, transcripts=False),),
            approve=False,
            platform=False,
            runtime=None,
        )
        await connection.execute(
            insert(inbound_event).values(
                id=seed.new_id(),
                namespace_id=namespace_id,
                name="accepted-start",
                idempotency_key=run_id,
                payload={},
                auth_kind="token",
                nonce=None,
                received_at=seed.clock.now(),
                consumed_by=[],
                start_outcome={
                    "outcome": "accepted",
                    "run_id": run_id,
                    "graph_version_id": version_id,
                    "status": "pending",
                },
                **seed.stamps(),
            )
        )
    scenario = PendingCancellation(
        database, namespace_id, graph_id, version_id, run_id, original_id, principal
    )
    scenario.clock.advance(10)
    return scenario
