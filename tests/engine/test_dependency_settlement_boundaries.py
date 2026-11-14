from dataclasses import replace

import pytest
from sqlalchemy import delete, update

from kora.db.connection import Database
from kora.db.tables import node_instance
from kora.db.transactions import WriterTransactionError
from kora.engine.dependency_settlement import (
    DependencySettlementState,
    settle_dependencies,
)
from kora.engine.locks import LockError, TransitionLocks
from tests.db.seed import Seeder
from tests.engine.dependency_settlement_support import dependency_case


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["missing", "foreign", "kind", "rule", "iteration", "top_level", "unknown"]
)
async def test_missing_or_ineligible_predecessor_is_never_a_root(
    database: Database, fault: str
) -> None:
    case = await dependency_case(database, "all_done")
    async with database.transaction() as writer:
        predecessor = node_instance.c.id == case.node_ids["producer"]
        if fault == "missing":
            await writer.execute(delete(node_instance).where(predecessor))
        else:
            values: dict[str, object] = {}
            if fault == "foreign":
                seed = Seeder(writer)
                values["run_id"] = await seed.run(
                    case.promotion.scope.namespace_id,
                    case.promotion.scope.graph_id,
                    case.promotion.scope.version_id,
                )
            else:
                field, value = {
                    "kind": ("kind", "agent"),
                    "rule": ("trigger_rule", "one_failed"),
                    "iteration": ("subgraph_attempt", 2),
                    "top_level": ("top_level", False),
                    "unknown": ("status", "private-unknown-state"),
                }[fault]
                values[field] = value
            await writer.execute(
                update(node_instance).where(predecessor).values(**values)
            )
    before = await case.promotion.snapshot(database)

    result = await case.settle(database)

    assert result is DependencySettlementState.REFUSED
    assert await case.promotion.snapshot(database) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "namespace",
        "graph",
        "version",
        "digest",
        "target",
        "path",
        "pointer",
        "expiry",
        "state",
    ],
)
async def test_dependency_settlement_requires_the_current_pinned_owner(
    database: Database, fault: str
) -> None:
    case = await dependency_case(database)
    pin, readiness = case.promotion.pin, case.readiness
    node_id, path = case.node_ids["approval"], "approval"
    async with database.transaction() as writer:
        seed = Seeder(writer)
        if fault == "pointer":
            attempt_id = await seed.attempt(node_id)
            await writer.execute(
                update(node_instance)
                .where(node_instance.c.id == node_id)
                .values(current_attempt_id=attempt_id)
            )
        elif fault in ("expiry", "state"):
            values: dict[str, object] = (
                {"deadline_expired_at": case.promotion.clock.now()}
                if fault == "expiry"
                else {"status": "queued"}
            )
            await writer.execute(
                update(node_instance)
                .where(node_instance.c.id == node_id)
                .values(**values)
            )
    before = await case.promotion.snapshot(database)

    async with database.transaction() as writer:
        context = await case.context(writer)
        if fault in ("namespace", "graph", "version"):
            context = replace(
                context,
                scope=replace(context.scope, **{fault + "_id": case.promotion.run_id}),
            )
            if fault != "version":
                with pytest.raises(LockError):
                    await settle_dependencies(context, pin, readiness, node_id, path)
                result = DependencySettlementState.REFUSED
            else:
                result = await settle_dependencies(
                    context, pin, readiness, node_id, path
                )
        else:
            if fault == "digest":
                pin = replace(pin, execution_contract_digest="0" * 64)
            if fault == "target":
                node_id = case.node_ids["agent"]
            if fault == "path":
                path = "unknown"
            result = await settle_dependencies(context, pin, readiness, node_id, path)

    assert result is DependencySettlementState.REFUSED
    assert await case.promotion.snapshot(database) == before


@pytest.mark.asyncio
async def test_dependency_settlement_requires_original_transaction_locks(
    database: Database,
) -> None:
    case = await dependency_case(database)
    async with database.transaction() as writer:
        context = await case.context(writer)
    before = await case.promotion.snapshot(database)

    with pytest.raises(WriterTransactionError):
        await settle_dependencies(
            context,
            case.promotion.pin,
            case.readiness,
            case.node_ids["approval"],
            "approval",
        )
    async with database.transaction() as writer:
        context = replace(context, connection=writer, locks=TransitionLocks(writer))
        with pytest.raises(LockError):
            await settle_dependencies(
                context,
                case.promotion.pin,
                case.readiness,
                case.node_ids["approval"],
                "approval",
            )

    assert await case.promotion.snapshot(database) == before
