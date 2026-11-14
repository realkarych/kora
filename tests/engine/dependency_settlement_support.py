from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.connection import Database
from kora.db.tables import node_instance, run
from kora.engine.dependency_settlement import (
    DependencySettlementState,
    settle_dependencies,
)
from kora.engine.expiry_context import ExpiryContext
from kora.engine.locks import Scope, ScopeKind, TransitionLocks
from kora.engine.readiness import PreparedReadiness, prepare_readiness
from kora.engine.run_materialization import prepare_run_materialization
from kora.engine.run_promotion import PromotionOutcome, promotion_scopes
from tests.db.seed import FakeClock, Seeder
from tests.engine.promotion_support import READ_LIMITS, PromotionCase, insert_pending
from tests.engine.test_graph_execution_contract import LIMITS
from tests.engine.test_readiness import readiness_source


@dataclass(frozen=True)
class DependencyCase:
    promotion: PromotionCase
    readiness: PreparedReadiness
    node_ids: dict[str, str]

    async def context(self, writer: AsyncConnection) -> ExpiryContext:
        promotion = self.promotion
        locks = TransitionLocks(writer)
        await locks.acquire_run(promotion.run_id)
        await locks.acquire_scopes(
            (Scope(ScopeKind.GLOBAL), *promotion_scopes(promotion.scope))
        )
        return ExpiryContext(
            writer,
            locks,
            promotion.scope,
            promotion.run_id,
            promotion.clock.now,
            promotion.identifiers,
            READ_LIMITS,
            1_000_000,
        )

    async def apply(
        self, writer: AsyncConnection, path: str = "approval"
    ) -> DependencySettlementState:
        return await settle_dependencies(
            await self.context(writer),
            self.promotion.pin,
            self.readiness,
            self.node_ids[path],
            path,
        )

    async def settle(
        self, database: Database, path: str = "approval"
    ) -> DependencySettlementState:
        async with database.transaction() as writer:
            return await self.apply(writer, path)


async def dependency_cases(
    database: Database,
    rule: str,
    states: tuple[tuple[str, str], ...],
    *,
    parameter_input: bool = False,
) -> tuple[DependencyCase, ...]:
    checked = await readiness_source(
        database,
        rules={"agent": rule, "approval": rule},
        parameter_input=parameter_input,
    )
    readiness = prepare_readiness(
        checked, max_ir_bytes=LIMITS.resources.logical.ir_bytes
    )
    materialization = prepare_run_materialization(
        checked, max_ir_bytes=LIMITS.resources.logical.ir_bytes, max_top_level_nodes=3
    )
    cases: list[DependencyCase] = []
    for current_states in states:
        async with database.transaction() as writer:
            seed = Seeder(writer)
            source = PromotionCase(
                checked.scope,
                checked.pin,
                materialization,
                seed.new_id(),
                seed.new_id(),
                FakeClock(),
                seed.generator,
            )
            promotion = await insert_pending(writer, source)
        assert await promotion.promote(database) is PromotionOutcome.APPLIED
        async with database.transaction() as writer:
            with await writer.execute(
                select(node_instance.c.node_path, node_instance.c.id).where(
                    node_instance.c.run_id == promotion.run_id
                )
            ) as result:
                node_ids = {str(row[0]): str(row[1]) for row in result}
            for path, state in zip(("agent", "producer"), current_states, strict=True):
                await writer.execute(
                    update(node_instance)
                    .where(node_instance.c.id == node_ids[path])
                    .values(status=state)
                )
            await writer.execute(
                update(run)
                .where(run.c.id == promotion.run_id)
                .values(needs_scheduling=False)
            )
        cases.append(DependencyCase(promotion, readiness, node_ids))
    return tuple(cases)


async def dependency_case(
    database: Database,
    rule: str = "all_success",
    states: tuple[str, str] = ("succeeded", "failed"),
    *,
    parameter_input: bool = False,
) -> DependencyCase:
    return (
        await dependency_cases(
            database, rule, (states,), parameter_input=parameter_input
        )
    )[0]
