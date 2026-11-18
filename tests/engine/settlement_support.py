from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.connection import Database
from kora.db.tables import node_instance, run
from kora.engine.expiry_context import ExpiryContext
from kora.engine.locks import Scope, ScopeKind
from tests.engine.expiry_support import expiry_context
from tests.engine.promotion_support import PromotionCase, promotion_case


async def finished_run(
    database: Database,
    status: str = "succeeded",
    *,
    held: bool = False,
    timeout: str | None = "1h",
) -> PromotionCase:
    case = await promotion_case(database, timeout=timeout)
    await case.promote(database)
    async with database.transaction() as writer:
        await writer.execute(
            update(node_instance)
            .where(node_instance.c.run_id == case.run_id)
            .values(status=status)
        )
        await writer.execute(
            update(run)
            .where(run.c.id == case.run_id)
            .values(
                needs_scheduling=False,
                target_key="held" if held else None,
                target_held_since=case.clock.now() if held else None,
            )
        )
    return case


async def settlement_context(
    writer: AsyncConnection, case: PromotionCase, *, target: bool = True
) -> ExpiryContext:
    context = await expiry_context(writer, case, False)
    scopes = (
        Scope(ScopeKind.GLOBAL),
        Scope(ScopeKind.NAMESPACE, (case.scope.namespace_id,)),
        Scope(ScopeKind.GRAPH, (case.scope.graph_id,)),
    )
    if target:
        scopes += (Scope(ScopeKind.TARGET, (case.scope.namespace_id, "held")),)
    await context.locks.acquire_scopes(scopes)
    return context
