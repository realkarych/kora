import pytest
from sqlalchemy import update

from kora.api.exchange import ApiExchange
from kora.client import KoraClient
from kora.client.errors import ApiError
from kora.db.connection import Database
from kora.db.tables import attempt_dispatcher
from kora.models.worker_history import WorkerHistoryQuery
from tests.api.claim_support import BEARER
from tests.api.deployment_cases import evaluator
from tests.api.history_support import history_api
from tests.db.closing import ConnectionLedger
from tests.engine.claim_support import ClaimCase, state
from tests.engine.test_worker_history import retained_history
from tests.plugins.authentication.support import AuthorityClock

__all__ = ["evaluator", "history_api"]


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["process", "count", "digest", "hole", "wide"])
async def test_history_corrupt_saved_page_fails_without_content_or_mutation(
    database: Database,
    history_api: tuple[ClaimCase, AuthorityClock, ApiExchange],
    corruption: str,
) -> None:
    case, _, app = history_api
    grants, snapshot = await retained_history(database, case, 3, False)
    query = WorkerHistoryQuery(
        lease_epoch=grants[-1].lease_epoch,
        dispatcher_id=grants[-1].dispatcher_id,
        snapshot_version=1,
        snapshot_upper_epoch=snapshot.upper_epoch,
        snapshot_count=snapshot.count,
        snapshot_digest=snapshot.digest,
        received_count=0,
        prefix_digest=snapshot.seed(),
    )
    private = "private-saved-history-content"
    async with database.transaction() as writer:
        damaged = update(attempt_dispatcher).where(
            attempt_dispatcher.c.id == grants[1].dispatcher_id
        )
        if corruption == "process":
            damaged = damaged.values(process={"private": private})
        elif corruption == "wide":
            damaged = damaged.values(process={"private": private * 20_000})
        elif corruption == "count":
            damaged = damaged.values(prefix_count=99)
        elif corruption == "digest":
            damaged = damaged.values(prefix_digest="a" * 64)
        else:
            damaged = damaged.values(prefix_count=None, prefix_digest=None)
        await writer.execute(damaged)
    before = await state(database)
    ledger = ConnectionLedger(database)

    async with KoraClient("http://127.0.0.1", BEARER, asgi_app=app) as client:
        with pytest.raises(ApiError) as caught:
            await client.grant_history(case.binding.attempt_id, query)
    after = await state(database)

    assert caught.value.code == "API010"
    assert private not in str(caught.value)
    assert private.encode() not in caught.value.content
    assert before == after
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()
