import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from kora.db.connection import Database
from kora.db.graph_resource_owner_reads import acquire_graph_resource_owners
from kora.db.graph_resource_transport import graph_resource_binary_projection
from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected
from kora.db.tables import (
    attempt,
    attempt_dispatcher,
    graph_version,
    graph_version_resource_owner,
    worker_incarnation,
)
from kora.engine.attempt_usage import UsageAccess, UsageAccessRejected
from kora.engine.locks import TransitionLocks
from kora.engine.usage_preparation import read_usage_context
from tests.db.closing import ConnectionLedger
from tests.engine.lock_support import DEADLINE, lock_inventory, wait_for_lock_waiter
from tests.engine.test_graph_execution_contract import LIMITS
from tests.engine.test_usage_acceptance import usage
from tests.engine.worker_usage_support import submit_usage, usage_case, usage_snapshot


async def recheck_grant_after_waiting_for_run_lock(database: Database) -> None:
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


async def refuse_changed_prepared_scope(database: Database, binding: str) -> None:
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


async def refuse_current_grant_change_without_writes(
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


async def read_exact_saved_contract(database: Database, resources: bool) -> None:
    case = await usage_case(database, resources=resources)
    ledger = ConnectionLedger(database)

    context = await read_usage_context(
        database,
        case.attempt_id,
        case.dispatcher_id,
        1,
        case.principal,
        case.clock,
        LIMITS,
    )

    assert context == case.context
    assert ledger.outstanding == 0
    assert database.units_in_flight() == 0


async def keep_historical_resource_metadata(database: Database, state: str) -> None:
    case = await usage_case(database, resources=True)
    async with database.transaction() as connection:
        await connection.execute(
            update(graph_version_resource_owner).values(payload_state=state)
        )

    context = await read_usage_context(
        database,
        case.attempt_id,
        case.dispatcher_id,
        1,
        case.principal,
        case.clock,
        LIMITS,
    )

    assert context == case.context


async def check_stale_grant_before_saved_material(
    database: Database, field: str
) -> None:
    case = await usage_case(database)
    async with database.transaction() as connection:
        await connection.execute(update(graph_version).values({field: "x" * 1_000_000}))
    ledger = ConnectionLedger(database)

    with pytest.raises(UsageAccessRejected) as stale:
        await read_usage_context(
            database,
            case.attempt_id,
            case.dispatcher_id,
            0,
            case.principal,
            case.clock,
            LIMITS,
        )
    with pytest.raises(GraphValueRejected):
        await read_usage_context(
            database,
            case.attempt_id,
            case.dispatcher_id,
            1,
            case.principal,
            case.clock,
            LIMITS,
        )

    assert stale.value.reason is UsageAccess.STALE_GRANT
    assert ledger.outstanding == 0
    assert database.units_in_flight() == 0


async def charge_resources_and_owners_together(
    database: Database, resources: bool
) -> None:
    case = await usage_case(database, resources=resources)
    async with database.read_transaction() as connection:
        payload = await connection.scalar(
            select(
                graph_resource_binary_projection(
                    database.dialect,
                    graph_version.c.resources,
                )
            )
        )
        assert payload is not None
        owners = await acquire_graph_resource_owners(
            connection,
            case.context.graph_version_id,
            max_text_bytes=100_000,
        )
        width = len(payload) + sum(
            len(value) for row in owners.rows for value in row if type(value) is bytes
        )
    exact = replace(LIMITS, physical_input_bytes=width)

    context = await read_usage_context(
        database,
        case.attempt_id,
        case.dispatcher_id,
        1,
        case.principal,
        case.clock,
        exact,
    )
    with pytest.raises(GraphValueRejected) as overflow:
        await read_usage_context(
            database,
            case.attempt_id,
            case.dispatcher_id,
            1,
            case.principal,
            case.clock,
            replace(exact, physical_input_bytes=width - 1),
        )

    assert context == case.context
    assert overflow.value.reason is GraphValueFailure.BUDGET


async def refuse_stale_write_authority(database: Database) -> None:
    case = await usage_case(database)
    context = await read_usage_context(
        database,
        case.attempt_id,
        case.dispatcher_id,
        1,
        case.principal,
        case.clock,
        LIMITS,
    )
    async with database.transaction() as connection:
        await connection.execute(update(attempt).values(lease_epoch=2))
    before = await usage_snapshot(database, case)

    with pytest.raises(UsageAccessRejected) as refused:
        await submit_usage(database, replace(case, context=context), (usage(),))
    after = await usage_snapshot(database, case)

    assert refused.value.reason is UsageAccess.STALE_GRANT
    assert after == before
