import asyncio
from dataclasses import replace

import pytest
from sqlalchemy import update

from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database, Lifecycle
from kora.db.tables import attempt_dispatcher, node_instance, run
from kora.engine.worker_history import HistoryAccessRejected, HistoryRead
from kora.protocols.history import GrantHistoryPage, GrantHistoryPosition
from tests.api.deployment_cases import evaluator
from tests.db.closing import ConnectionLedger, until
from tests.db.test_grant_history_reads import PausedHistory
from tests.engine.claim_support import claim_case
from tests.engine.test_worker_history import page, retained_history

__all__ = ["evaluator"]


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["cancel", "expire", "shutdown", "append"])
async def test_history_snapshot_settles_after_interruption_and_concurrent_append(
    database: Database, evaluator: Evaluator, ending: str
) -> None:
    case = await claim_case(database, evaluator)
    grants, snapshot = await retained_history(database, case, 1, False)
    request = HistoryRead(
        case.binding.attempt_id,
        grants[-1].dispatcher_id,
        grants[-1].lease_epoch,
        snapshot.tip(),
        GrantHistoryPosition(
            after_epoch=None, received_count=0, prefix_digest=snapshot.seed()
        ),
    )
    ledger = ConnectionLedger(database)
    closing: asyncio.Task[None] | None = None
    with PausedHistory(database) as paused:
        pending = asyncio.create_task(page(database, case, request))
        await asyncio.wait_for(paused.entered.wait(), 10)

        try:
            if ending == "cancel":
                pending.cancel()
            elif ending == "expire":
                case.clock.advance(120)
            elif ending == "append":
                await retained_history(database, case, 2, False)
            else:
                closing = asyncio.create_task(database.close(deadline_seconds=10))
                await until(lambda: database.lifecycle is Lifecycle.CLOSING)
            paused.release.set()
            if ending == "cancel":
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(pending, 10)
            elif ending == "expire":
                with pytest.raises(HistoryAccessRejected) as caught:
                    await asyncio.wait_for(pending, 10)
                assert caught.value.reason == "stale"
            else:
                received = GrantHistoryPage.model_validate_json(
                    await asyncio.wait_for(pending, 10)
                )
                assert received.grants == grants
                assert received.snapshot == snapshot
        finally:
            paused.release.set()
            await asyncio.gather(pending, return_exceptions=True)
            if closing is not None:
                await asyncio.wait_for(closing, 10)

    assert ledger.outstanding == database.units_in_flight() == 0
    if closing is not None:
        assert database.lifecycle is Lifecycle.CLOSED


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", ["start", "final"])
async def test_history_empty_prefix_and_final_replay_preserve_recovery_authority(
    database: Database, evaluator: Evaluator, empty: str
) -> None:
    case = await claim_case(database, evaluator)
    grants, snapshot = await retained_history(database, case, 1, False)
    start = GrantHistoryPosition(
        after_epoch=None, received_count=0, prefix_digest=snapshot.seed()
    )
    tip = snapshot.tip() if empty == "final" else start
    request = HistoryRead(
        case.binding.attempt_id,
        grants[0].dispatcher_id,
        grants[0].lease_epoch,
        tip,
        tip,
    )
    async with database.transaction() as writer:
        await writer.execute(update(attempt_dispatcher).values(mode="recover"))
        await writer.execute(update(run).values(status="cancelled"))
        await writer.execute(update(node_instance).values(current_attempt_id=None))

    received = GrantHistoryPage.model_validate_json(await page(database, case, request))
    replay = GrantHistoryPage.model_validate_json(await page(database, case, request))
    forged = replace(
        request,
        start=tip.model_copy(update={"prefix_digest": "f" * 64}),
    )
    with pytest.raises(HistoryAccessRejected) as caught:
        await page(database, case, forged)

    assert received == replay
    assert received.grants == ()
    assert received.start == received.end == tip
    assert received.complete
    assert caught.value.reason == "conflict"
    assert database.units_in_flight() == 0
