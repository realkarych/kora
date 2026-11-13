import hashlib

import pytest
from sqlalchemy import update

from kora.api.application import create_application
from kora.api.exchange import ApiExchange
from kora.api.worker_claims_route import WorkerClaimsRoute
from kora.cel_eval.thread import Evaluator
from kora.client import KoraClient
from kora.client.errors import ApiError
from kora.db.connection import Database
from kora.db.tables import api_token
from kora.models.errors import ApiErrorCode
from kora.observability import Level, LoggingSettings, ProcessDiagnostics
from kora.plugins.authentication.static_token import StaticTokenAuthenticator
from tests.api.claim_support import BEARER, poller, request
from tests.api.deployment_cases import evaluator
from tests.api.support import RequestIds
from tests.db.closing import ConnectionLedger
from tests.engine.claim_support import ClaimCase, claim_case
from tests.observability.conftest import (
    NOTE,
    TEST_COMPONENT,
    Configure,
    Pipe,
    configured_with_reader,
    finished_output,
    wait_until,
)
from tests.plugins.authentication.support import AuthorityClock

__all__ = ["evaluator"]


async def application(
    database: Database, case: ClaimCase, diagnostics: ProcessDiagnostics
) -> ApiExchange:
    async with database.transaction() as writer:
        await writer.execute(
            update(api_token)
            .where(api_token.c.id == case.principal.token_id)
            .values(digest=hashlib.sha256(BEARER.encode()).hexdigest(), kind="service")
        )
    clock = AuthorityClock()
    return create_application(
        StaticTokenAuthenticator({}, database),
        diagnostics,
        clock,
        RequestIds(),
        worker_claims=WorkerClaimsRoute(poller(database, clock)),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("level", [Level.DEBUG, Level.INFO])
async def test_claim_http_diagnostics_exclude_payload_and_native_failure(
    database: Database,
    evaluator: Evaluator,
    configure: Configure,
    pipe: Pipe,
    level: Level,
) -> None:
    case = await claim_case(database, evaluator)
    diagnostics = configured_with_reader(configure, pipe, LoggingSettings(level=level))
    app = await application(database, case, diagnostics)
    ledger = ConnectionLedger(database)

    async with KoraClient("http://127.0.0.1", BEARER, asgi_app=app) as client:
        granted = await client.claim_worker(case.query.worker_id, request(case))
        replayed = await client.claim_worker(case.query.worker_id, request(case))
        async with database.transaction() as writer:
            await writer.exec_driver_sql("DROP TABLE attempt_dispatcher")
        with pytest.raises(ApiError) as refusal:
            await client.claim_worker(case.query.worker_id, request(case, "refused"))
    output = finished_output(diagnostics, pipe)
    records = pipe.reader.records()

    assert granted.claim is not None
    assert replayed == granted
    assert refusal.value.code == ApiErrorCode.INTERNAL
    for private in (
        BEARER,
        case.query.worker_id,
        "Summarize the repository",
        "attempt_dispatcher",
    ):
        assert private not in output
    assert (
        len([record for record in records if record["event"] == "api.request.failed"])
        == 1
    )
    assert len(
        [record for record in records if record["event"] == "api.request.completed"]
    ) == (3 if level is Level.DEBUG else 0)
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", [False, True])
async def test_claim_http_survives_unavailable_diagnostic_sink(
    database: Database,
    evaluator: Evaluator,
    configure: Configure,
    pipe: Pipe,
    broken: bool,
) -> None:
    case = await claim_case(database, evaluator)
    ledger = ConnectionLedger(database)
    settings = LoggingSettings(level=Level.DEBUG, queue_records=64, queue_bytes=65536)
    diagnostics = configure(settings, pipe.write_end)
    app = await application(database, case, diagnostics)
    if broken:
        pipe.close_read_end()
    logger = diagnostics.logger(TEST_COMPONENT)
    for _ in range(256):
        logger.emit(NOTE, text="bounded fixture record " * 100)

    async with KoraClient("http://127.0.0.1", BEARER, asgi_app=app) as client:
        granted = await client.claim_worker(case.query.worker_id, request(case))
        replayed = await client.claim_worker(case.query.worker_id, request(case))
    if broken:
        wait_until(lambda: diagnostics.snapshot().sink_failed)
    snapshot = diagnostics.snapshot()
    if not broken:
        pipe.reader.start()
    drained = diagnostics.shutdown()

    assert granted.claim is not None
    assert replayed == granted
    assert snapshot.dropped_total() > 0
    assert snapshot.queue_records <= settings.queue_records
    assert snapshot.queue_bytes <= settings.queue_bytes
    assert drained
    assert not diagnostics.writer_is_alive()
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()
