import asyncio
from dataclasses import replace
from datetime import datetime

import pytest
from sqlalchemy import update

from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database
from kora.db.grant_history import HistoryScope
from kora.db.tables import attempt, attempt_dispatcher
from kora.engine.grant_history import append_grant
from kora.engine.locks import TransitionLocks
from kora.engine.worker_history import (
    HistoryAccessRejected,
    HistoryRead,
    read_worker_history,
)
from kora.engine.worker_registration import register_worker
from kora.protocols.history import (
    MAX_ENVELOPE_BYTES,
    GrantHistoryPage,
    GrantHistoryPosition,
    GrantHistorySnapshot,
)
from kora.protocols.identity import GrantBinding, ProcessIdentity
from kora.protocols.time import SystemClock
from tests.api.deployment_cases import evaluator
from tests.db.closing import ConnectionLedger
from tests.engine.claim_support import (
    HISTORY_LIMITS,
    WORKER_BYTES,
    ClaimCase,
    claim_case,
    state,
)

__all__ = ["evaluator"]


class ReadClock(SystemClock):
    def __init__(self, case: ClaimCase) -> None:
        self.case = case

    def now(self) -> datetime:
        return self.case.clock.now()


async def retained_history(
    database: Database, case: ClaimCase, count: int, wide: bool
) -> tuple[tuple[GrantBinding, ...], GrantHistorySnapshot]:
    first = await case.claim(database)
    assert first.delivery is not None
    system = first.delivery.system
    grants = [system.current_grant]
    snapshot = system.grant_history
    scope = HistoryScope(
        case.binding.run.namespace_id,
        case.binding.run.run_id,
        case.binding.node_id,
        case.binding.attempt_id,
        case.registration.execution_domain_id,
    )
    registration = (
        replace(
            case.registration,
            incarnation_id='"' * 256,
            process=ProcessIdentity(
                host_id='"' * 256,
                boot_id='"' * 256,
                pid_namespace='"' * 256,
                pid=234,
                start_time=567,
                leads_group=True,
            ),
        )
        if wide
        else case.registration
    )
    async with database.transaction() as writer:
        if wide:
            await register_worker(
                writer,
                registration,
                case.principal,
                portable=False,
                now=case.clock.now(),
                event_id=case.identifiers.new(),
            )
        locks = TransitionLocks(writer)
        await locks.acquire_run(scope.run_id)
        for number in range(2, count + 1):
            grant = GrantBinding(
                dispatcher_id=case.identifiers.new(),
                worker_id=case.principal.owner,
                incarnation_id=registration.incarnation_id,
                lease_epoch=number * 3,
                execution_domain_id=scope.execution_domain_id,
                process=registration.process,
            )
            appended = await append_grant(
                writer, locks, scope, grant, "recover", case.clock.now(), HISTORY_LIMITS
            )
            assert appended is not None
            snapshot = appended
            grants.append(grant)
        await writer.execute(
            update(attempt)
            .where(attempt.c.id == scope.attempt_id)
            .values(lease_epoch=grants[-1].lease_epoch)
        )
        await writer.execute(
            update(attempt_dispatcher)
            .where(
                attempt_dispatcher.c.attempt_id == scope.attempt_id,
                attempt_dispatcher.c.id != grants[-1].dispatcher_id,
            )
            .values(closed_at=case.clock.now(), evidence={"retained": "x" * 40_000})
        )
    return tuple(grants), snapshot


async def page(database: Database, case: ClaimCase, request: HistoryRead) -> bytes:
    return await read_worker_history(
        database,
        request,
        case.principal,
        ReadClock(case),
        HISTORY_LIMITS,
        WORKER_BYTES,
        deadline=asyncio.get_running_loop().time() + 10,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("count", "wide"),
    [(1, False), (128, False), (129, False), (1000, False), (129, True)],
)
async def test_history_pages_preserve_prefixes_with_gaps_and_closed_rows(
    database: Database, evaluator: Evaluator, count: int, wide: bool
) -> None:
    case = await claim_case(database, evaluator)
    grants, snapshot = await retained_history(database, case, count, wide)
    start = GrantHistoryPosition(
        after_epoch=None, received_count=0, prefix_digest=snapshot.seed()
    )
    request = HistoryRead(
        case.binding.attempt_id,
        grants[-1].dispatcher_id,
        grants[-1].lease_epoch,
        snapshot.tip(),
        start,
    )
    ledger = ConnectionLedger(database)
    before = await state(database)

    pages: list[GrantHistoryPage] = []
    sizes: list[int] = []
    while True:
        encoded = await page(database, case, request)
        received = GrantHistoryPage.model_validate_json(encoded)
        pages.append(received)
        sizes.append(len(encoded))
        assert ledger.outstanding == database.units_in_flight() == 0
        if received.complete:
            break
        request = replace(request, start=received.end)
    replay = await page(database, case, request)
    after = await state(database)

    assert tuple(grant for item in pages for grant in item.grants) == grants
    assert all(item.snapshot == snapshot for item in pages)
    assert all(0 < len(item.grants) <= 128 for item in pages)
    assert all(size <= MAX_ENVELOPE_BYTES for size in sizes)
    assert pages[-1].end == snapshot.tip()
    assert GrantHistoryPage.model_validate_json(replay) == pages[-1]
    assert before == after
    assert database.writer_is_free()
    if wide:
        assert len(pages[0].grants) < 128


@pytest.mark.asyncio
async def test_history_old_prefix_requires_the_new_current_grant(
    database: Database, evaluator: Evaluator
) -> None:
    case = await claim_case(database, evaluator)
    first = await case.claim(database)
    assert first.delivery is not None
    old = first.delivery.system
    grants, _ = await retained_history(database, case, 2, False)
    start = GrantHistoryPosition(
        after_epoch=None, received_count=0, prefix_digest=old.grant_history.seed()
    )
    request = HistoryRead(
        case.binding.attempt_id,
        grants[-1].dispatcher_id,
        grants[-1].lease_epoch,
        old.grant_history.tip(),
        start,
    )
    before = await state(database)

    received = GrantHistoryPage.model_validate_json(await page(database, case, request))
    with pytest.raises(HistoryAccessRejected) as stale:
        await page(
            database,
            case,
            replace(
                request,
                dispatcher_id=old.current_grant.dispatcher_id,
                lease_epoch=old.lease_epoch,
            ),
        )
    after = await state(database)

    assert received.complete
    assert received.grants == (old.current_grant,)
    assert stale.value.reason == "stale"
    assert before == after
