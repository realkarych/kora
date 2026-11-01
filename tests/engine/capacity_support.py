from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.connection import Database
from kora.db.tables import (
    attempt,
    event,
    graph_version,
    idempotency_record,
    inbound_event,
    namespace,
    node_instance,
    run,
)
from kora.engine.locks import Scope, ScopeKind, TransitionLocks
from kora.engine.start_capacity import (
    Overlap,
    StartCapacity,
    check_manual_start_capacity,
)
from kora.ir.json import JsonValue
from kora.protocols.authentication import NamespaceGrant, Principal
from tests.db.seed import Seeder

PRIVATE: str = "private-start-capacity-fixture"


@dataclass(frozen=True)
class CapacityScenario:
    database: Database
    namespace_id: str
    graph_id: str
    version_id: str
    run_id: str
    principal: Principal

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

    async def check(
        self,
        connection: AsyncConnection,
        locks: TransitionLocks,
        overlap: Overlap = "allow",
    ) -> StartCapacity:
        return await check_manual_start_capacity(
            connection,
            locks,
            self.principal,
            self.namespace_id,
            self.graph_id,
            self.run_id,
            overlap,
        )

    async def read(self, overlap: Overlap = "allow") -> StartCapacity:
        async with self.database.transaction() as connection:
            return await self.check(connection, await self.acquire(connection), overlap)

    async def policy(self, value: JsonValue) -> None:
        async with self.database.transaction() as connection:
            await connection.execute(
                update(namespace)
                .where(namespace.c.id == self.namespace_id)
                .values(policy=value)
            )

    async def snapshot(self) -> dict[str, list[dict[str, object]]]:
        snapshot: dict[str, list[dict[str, object]]] = {}
        async with self.database.read_transaction() as connection:
            for table in (
                namespace,
                graph_version,
                run,
                node_instance,
                attempt,
                inbound_event,
                event,
                idempotency_record,
            ):
                result = await connection.execute(select(table))
                try:
                    snapshot[table.name] = [dict(row) for row in result.mappings()]
                finally:
                    result.close()
        return snapshot


async def capacity_scenario(database: Database) -> CapacityScenario:
    async with database.transaction() as connection:
        seed = Seeder(connection)
        namespace_id = await seed.namespace()
        graph_id = await seed.graph(namespace_id)
        version_id = await seed.graph_version(graph_id)
        run_id = seed.new_id()
        principal = Principal(
            token_id=seed.new_id(),
            kind="user",
            owner=PRIVATE,
            namespaces=(NamespaceGrant(namespace_id=namespace_id, transcripts=False),),
            approve=False,
            platform=False,
            runtime=None,
        )
    scenario = CapacityScenario(
        database, namespace_id, graph_id, version_id, run_id, principal
    )
    await scenario.policy({"concurrency": {"max_pending_runs": 2}, "private": PRIVATE})
    return scenario
