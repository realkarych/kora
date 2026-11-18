from dataclasses import dataclass

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.connection import Database
from kora.db.graph_version_reads import acquire_graph_version
from kora.db.run_input_parent import INPUT_SENTINEL
from kora.db.tables import run, run_inputs
from kora.engine.attempt_capacity import PreparedGraphCaps, prepare_graph_caps
from kora.engine.expiry_context import ExpiryContext
from kora.engine.graph_execution_contract import check_detached_execution_contract
from kora.engine.graph_resource_references import GraphResourceScope
from kora.engine.locks import Scope, ScopeKind, TransitionLocks
from kora.engine.namespace_policy import (
    NamespacePolicyConfiguration,
    namespace_policy_scopes,
    write_namespace_policy,
)
from kora.ir.json import JsonObject
from tests.db.seed import FakeClock, Seeder
from tests.engine.promotion_support import READ_LIMITS
from tests.engine.test_graph_execution_contract import (
    LIMITS,
    complete_inputs,
    insert_case,
    prepared_case,
)

PRIVATE: str = "private-attempt-capacity-material"


async def live_caps(
    database: Database,
    scope: GraphResourceScope,
    number: int,
    concurrency: JsonObject,
    *,
    max_ir_bytes: int = LIMITS.resources.logical.ir_bytes,
) -> PreparedGraphCaps:
    raw, saved, refs, owners = complete_inputs(
        scope, resources=False, runtime_settings={"concurrency": concurrency}
    )
    raw["x-private"] = PRIVATE
    prepared, pin = prepared_case(raw, saved, scope)
    async with database.transaction() as writer:
        await insert_case(Seeder(writer), scope, number, prepared, refs)
    async with database.read_transaction() as reader:
        acquired = await acquire_graph_version(
            reader,
            scope.namespace_id,
            scope.graph_id,
            scope.version_id,
            max_encoded_bytes=LIMITS.identity.max_encoded_bytes,
        )
    assert acquired is not None
    checked = check_detached_execution_contract(
        acquired, pin, scope, refs, owners, limits=LIMITS
    )
    return prepare_graph_caps(checked, max_ir_bytes=max_ir_bytes)


@dataclass(frozen=True)
class CapacityCase:
    scope: GraphResourceScope
    run_id: str
    caps: PreparedGraphCaps

    async def context(
        self, writer: AsyncConnection, *, capacity: bool = True
    ) -> ExpiryContext:
        locks = TransitionLocks(writer)
        await locks.acquire_run(self.run_id)
        if capacity:
            await locks.acquire_scopes(
                (
                    Scope(ScopeKind.GLOBAL),
                    Scope(ScopeKind.NAMESPACE, (self.scope.namespace_id,)),
                    Scope(ScopeKind.GRAPH, (self.scope.graph_id,)),
                    Scope(ScopeKind.RUNTIME, ("local",)),
                )
            )
        return ExpiryContext(
            writer,
            locks,
            self.scope,
            self.run_id,
            FakeClock().now,
            Seeder(writer).generator,
            READ_LIMITS,
            1_000_000,
        )


async def capacity_case(
    database: Database,
    concurrency: JsonObject | None = None,
    *,
    max_ir_bytes: int = LIMITS.resources.logical.ir_bytes,
) -> CapacityCase:
    async with database.transaction() as writer:
        seed = Seeder(writer)
        namespace_id = await seed.namespace("capacity-" + seed.new_id())
        graph_id = await seed.graph(namespace_id)
        scope = GraphResourceScope(namespace_id, graph_id, seed.new_id())
        run_id = seed.new_id()
    caps = await live_caps(
        database,
        scope,
        1,
        {} if concurrency is None else concurrency,
        max_ir_bytes=max_ir_bytes,
    )
    async with database.transaction() as writer:
        seed = Seeder(writer)
        values = seed.run_values(scope.namespace_id, scope.graph_id, scope.version_id)
        values.update(
            id=run_id,
            input_encoding_version=1,
            params=INPUT_SENTINEL,
            cel_params_projection=INPUT_SENTINEL,
        )
        await writer.execute(insert(run).values(**values))
        await writer.execute(
            insert(run_inputs).values(run_id=run_id, params=b"{}", cel_projection=b"{}")
        )
    return CapacityCase(scope, run_id, caps)


async def namespace_limit(
    database: Database, namespace_id: str, limit: int | None
) -> None:
    concurrency: JsonObject = {} if limit is None else {"max_running_attempts": limit}
    configured = NamespacePolicyConfiguration.prepare(
        namespace_id, 2, {"concurrency": concurrency, "private": PRIVATE}
    )
    async with database.transaction() as writer:
        locks = TransitionLocks(writer)
        await locks.acquire_scopes(namespace_policy_scopes(namespace_id))
        await write_namespace_policy(writer, locks, configured, FakeClock().now())
