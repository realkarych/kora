import pytest
from sqlalchemy import select

from kora.api.worker_history_route import WorkerHistoryRoute
from kora.api.worker_usage_route import WorkerUsageRoute
from kora.cel_eval.thread import Evaluator
from kora.client import KoraClient
from kora.conformance.capture import captured
from kora.db.connection import Database
from kora.db.tables import usage_record
from kora.models.worker_usage import UsageReport
from kora.observability import Level, LoggingSettings
from tests.api.claim_support import BEARER, application, request
from tests.api.deployment_cases import evaluator
from tests.api.history_support import history_query
from tests.client.support import running_app
from tests.db.closing import ConnectionLedger
from tests.engine.claim_support import HISTORY_LIMITS, WORKER_BYTES, claim_case, state
from tests.engine.test_graph_execution_contract import LIMITS
from tests.engine.test_usage_acceptance import coverage, usage
from tests.plugins.authentication.support import AuthorityClock

__all__ = ["evaluator"]


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["asgi", "http"])
async def test_claim_history_usage_and_receipt_replay_share_one_application(
    database: Database, evaluator: Evaluator, transport: str
) -> None:
    case = await claim_case(database, evaluator)
    clock = AuthorityClock()
    with captured(
        "api", LoggingSettings(level=Level.DEBUG), "usage-claim", clock.now
    ) as capture:
        history = WorkerHistoryRoute(
            database, HISTORY_LIMITS, clock, worker_bytes=WORKER_BYTES, read_timeout=10
        )
        app = await application(
            database,
            capture,
            clock,
            case,
            history,
            usage=WorkerUsageRoute(
                database, LIMITS, clock, case.identifiers, lock_timeout_ms=10_000
            ),
        )
        ledger = ConnectionLedger(database)

        async with running_app(app) as origin:
            async with KoraClient(
                origin if transport == "http" else "http://127.0.0.1",
                BEARER,
                asgi_app=app if transport == "asgi" else None,
            ) as client:
                issued = await client.claim_worker(case.query.worker_id, request(case))
                assert issued.claim is not None
                claim = issued.claim
                page = await client.grant_history(
                    claim.attempt_id, history_query(claim)
                )
                report = UsageReport(
                    lease_epoch=claim.lease_epoch,
                    dispatcher_id=claim.current_grant.dispatcher_id,
                    rows=(usage(input_tokens=1_000_000, observed_at=clock.now()),),
                    coverage=coverage(observed_at=clock.now()),
                )
                accepted = await client.report_usage(claim.attempt_id, report)
                before_replay = await state(database)
                usage_before = await usage_rows(database)
                replay = await client.claim_worker(case.query.worker_id, request(case))
                retained = await client.grant_history(
                    claim.attempt_id, history_query(claim)
                )
                repeated_usage = await client.report_usage(claim.attempt_id, report)
                after_replay = await state(database)
                usage_after = await usage_rows(database)

    assert page.complete
    assert page.grants == (claim.current_grant,)
    assert page == retained
    assert accepted.rows[0].usd_estimate == "1.25"
    assert accepted.coverage == report.coverage
    assert repeated_usage == accepted
    assert replay == issued
    assert before_replay == after_replay
    assert usage_before == usage_after
    assert ledger.outstanding == database.units_in_flight() == 0


async def usage_rows(database: Database) -> list[dict[str, object]]:
    async with database.read_transaction() as reader:
        result = await reader.execute(select(usage_record).order_by(usage_record.c.id))
        return [dict(row) for row in result.mappings()]
