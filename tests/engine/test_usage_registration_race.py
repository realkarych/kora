import asyncio
from datetime import timedelta
from typing import cast

import pytest
from sqlalchemy import select

from kora.db.connection import Database
from kora.db.tables import worker
from kora.engine.attempt_usage import UsageAccess, UsageAccessRejected, report_usage
from kora.engine.locks import TransitionLocks
from kora.engine.worker_registration import Registration, register_worker
from kora.protocols.identity_records import ProcessIdentity
from tests.db.closing import ConnectionLedger
from tests.db.test_graph_credential_guard import postgres_pid, wait_for_blocked_writer
from tests.engine.lock_support import DEADLINE, lock_inventory
from tests.engine.test_usage_acceptance import coverage, usage
from tests.engine.worker_usage_support import usage_case, usage_snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["incarnation", "lease_expiry", "cancellation"])
async def test_usage_rechecks_authority_after_waiting_for_registration(
    database: Database, change: str
) -> None:
    case = await usage_case(database)
    release = asyncio.Event()
    loop = asyncio.get_running_loop()
    blocker: asyncio.Future[int | None] = loop.create_future()
    waiter: asyncio.Future[int | None] = loop.create_future()
    ledger = ConnectionLedger(database)

    async def registrar() -> None:
        async with database.transaction() as connection:
            await connection.execute(
                select(worker.c.id)
                .where(worker.c.id == case.principal.owner)
                .with_for_update()
            )
            if change == "incarnation":
                await register_worker(
                    connection,
                    Registration(
                        case.principal.owner,
                        "replacement-incarnation",
                        "local",
                        "replacement-domain",
                        "0.1.0",
                        1,
                        ProcessIdentity(
                            host_id="host",
                            boot_id="boot",
                            pid_namespace="namespace",
                            pid=2345,
                            start_time=600,
                            leads_group=True,
                        ),
                    ),
                    case.principal,
                    portable=False,
                    now=case.clock.now(),
                    event_id=case.identifiers.new(),
                )
            blocker.set_result(await postgres_pid(connection, database.dialect))
            await asyncio.wait_for(release.wait(), DEADLINE)

    async def reporter() -> None:
        async with database.transaction() as connection:
            waiter.set_result(await postgres_pid(connection, database.dialect))
            locks = TransitionLocks(connection)
            await locks.acquire_run(case.context.run_id)
            await report_usage(
                connection,
                locks,
                case.context,
                case.attempt_id,
                case.dispatcher_id,
                1,
                case.principal,
                (usage(),),
                coverage(),
                case.clock,
                case.identifiers,
            )

    owner = asyncio.create_task(registrar())
    owner_pid = await asyncio.wait_for(blocker, DEADLINE)
    contender = asyncio.create_task(reporter())

    try:
        await wait_for_blocked_writer(database, owner_pid, waiter)
        if change == "lease_expiry":
            case.clock.moment += timedelta(seconds=120)
        if change == "cancellation":
            contender.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(contender, DEADLINE)
        else:
            release.set()
            await asyncio.wait_for(owner, DEADLINE)
            if change == "incarnation":
                await asyncio.wait_for(contender, DEADLINE)
            else:
                with pytest.raises(UsageAccessRejected) as refused:
                    await asyncio.wait_for(contender, DEADLINE)
                assert refused.value.reason is UsageAccess.STALE_GRANT
    finally:
        release.set()
        await asyncio.gather(owner, contender, return_exceptions=True)
    after = await usage_snapshot(database, case)

    assert isinstance(after[0], tuple)
    assert len(cast(tuple[object, ...], after[0])) == (
        1 if change == "incarnation" else 0
    )
    assert after[2] is (change == "incarnation")
    assert ledger.outstanding == database.units_in_flight() == 0
    assert await lock_inventory(database) == []
