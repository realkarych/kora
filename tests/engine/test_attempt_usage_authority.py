import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import update

from kora.db.connection import Database
from kora.db.tables import (
    attempt,
    attempt_dispatcher,
    worker_incarnation,
)
from kora.engine.attempt_usage import (
    UsageAccess,
    UsageAccessRejected,
)
from kora.engine.locks import TransitionLocks
from tests.db.closing import ConnectionLedger
from tests.engine.lock_support import DEADLINE, lock_inventory, wait_for_lock_waiter
from tests.engine.test_usage_acceptance import usage
from tests.engine.worker_usage_support import submit_usage, usage_case, usage_snapshot


@pytest.mark.asyncio
async def test_usage_rechecks_grant_after_waiting_for_run_lock(
    database: Database,
) -> None:
    case = await usage_case(database)
    ready, release = asyncio.Event(), asyncio.Event()
    ledger = ConnectionLedger(database)

    async def predecessor() -> None:
        async with database.transaction() as connection:
            locks = TransitionLocks(connection)
            await locks.acquire_run(case.context.run_id)
            await connection.execute(
                update(attempt)
                .where(attempt.c.id == case.attempt_id)
                .values(lease_epoch=2)
            )
            ready.set()
            await asyncio.wait_for(release.wait(), DEADLINE)

    owner = asyncio.create_task(predecessor())
    await asyncio.wait_for(ready.wait(), DEADLINE)
    contender = asyncio.create_task(submit_usage(database, case, (usage(),)))

    try:
        await wait_for_lock_waiter(database)
        release.set()
        await asyncio.wait_for(owner, DEADLINE)
        with pytest.raises(UsageAccessRejected) as refused:
            await asyncio.wait_for(contender, DEADLINE)
    finally:
        release.set()
        await asyncio.gather(owner, contender, return_exceptions=True)
    snapshot = await usage_snapshot(database, case)

    assert refused.value.reason is UsageAccess.STALE_GRANT
    assert snapshot[0] == ()
    assert snapshot[2] is False
    assert await lock_inventory(database) == []
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("binding", ["namespace", "graph", "node", "digest", "version"])
async def test_usage_refuses_changed_prepared_scope(
    database: Database, binding: str
) -> None:
    case = await usage_case(database)
    fields = {
        "namespace": "namespace_id",
        "graph": "graph_id",
        "node": "node_path",
        "digest": "execution_contract_digest",
        "version": "graph_version_id",
    }
    changed = replace(case, context=replace(case.context, **{fields[binding]: "other"}))
    before = await usage_snapshot(database, case)

    with pytest.raises(UsageAccessRejected) as refused:
        await submit_usage(database, changed, (usage(),))
    after = await usage_snapshot(database, case)

    assert refused.value.reason is UsageAccess.NOT_FOUND
    assert after == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed",
    [
        "epoch",
        "dispatcher",
        "incarnation",
        "retained_domain",
        "domain",
        "expiry",
        "worker",
        "token",
    ],
)
async def test_usage_refuses_current_grant_changes_without_writes(
    database: Database, changed: str
) -> None:
    case = await usage_case(database)
    async with database.transaction() as connection:
        if changed == "epoch":
            await connection.execute(update(attempt).values(lease_epoch=2))
        elif changed == "incarnation":
            await connection.execute(
                update(attempt_dispatcher).values(incarnation_id="unregistered")
            )
        elif changed == "retained_domain":
            await connection.execute(
                update(worker_incarnation).values(execution_domain_id="other")
            )
        elif changed == "domain":
            await connection.execute(
                update(attempt).values(execution_domain_id="new-domain")
            )
        elif changed == "expiry":
            case.clock.moment += timedelta(seconds=120)
        elif changed == "worker":
            case = replace(
                case, principal=case.principal.model_copy(update={"owner": "other"})
            )
        elif changed == "token":
            case = replace(
                case, principal=case.principal.model_copy(update={"token_id": "other"})
            )
        else:
            case = replace(case, dispatcher_id="other")
    before = await usage_snapshot(database, case)

    with pytest.raises(UsageAccessRejected) as refused:
        await submit_usage(database, case, (usage(),))
    after = await usage_snapshot(database, case)

    assert refused.value.reason is (
        UsageAccess.FORBIDDEN
        if changed in {"worker", "token"}
        else UsageAccess.STALE_GRANT
    )
    assert after == before
