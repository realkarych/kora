import asyncio
import json
import socket
from contextlib import AsyncExitStack
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest
from sqlalchemy import insert, select, text, update

from kora.conformance.authentication import kora_capture
from kora.conformance.capture import Capture
from kora.conformance.common import context_for
from kora.db.connection import SQLITE_READERS, Database, DatabaseSettings, Dialect
from kora.db.tables import api_token, idempotency_record, worker
from kora.plugins.authentication.static_token import REVOKED, StaticTokenAuthenticator
from kora.protocols.authentication import RequestInfo
from kora.protocols.errors import KoraProtocolError
from kora.protocols.loading import load_provider
from kora.protocols.metadata import ProtocolName
from tests.db.closing import ConnectionLedger
from tests.db.seed import START, Seeder
from tests.plugins.authentication.support import AuthorityClock, seed_case
from tests.support.event_loop import turn

__all__ = ["kora_capture"]


def assert_error(error: KoraProtocolError, code: str) -> None:
    assert error.code == code
    assert error.retryable is (code == "authentication_unavailable")
    assert error.details == {}
    assert error.__context__ is None
    assert error.__cause__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize("fresh", [False, True])
async def test_expiry_is_checked_at_the_exact_boundary_after_database_release(
    database: Database, kora_capture: Capture, fresh: bool
) -> None:
    ledger = ConnectionLedger(database)
    case = await seed_case(database, "expiry", expires_at=START)
    provider = StaticTokenAuthenticator({}, database)

    class ReleasedClock(AuthorityClock):
        def now(self) -> datetime:
            assert ledger.outstanding == 0
            return super().now()

    clock = ReleasedClock(START - timedelta(microseconds=1))
    context = replace(context_for(kora_capture), clock=clock)
    request = replace(case.request, requires_current_authority=fresh)

    before = await provider.authenticate(request, context)
    clock.moment = START
    with pytest.raises(KoraProtocolError) as at_boundary:
        await provider.authenticate(request, context)
    clock.moment += timedelta(microseconds=1)
    with pytest.raises(KoraProtocolError) as after_boundary:
        await provider.authenticate(request, context)

    assert before == case.principal
    assert_error(at_boundary.value, "authentication_rejected")
    assert_error(after_boundary.value, "authentication_rejected")
    assert ledger.outstanding == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "expiry",
    [None, datetime(1960, 1, 1, tzinfo=UTC), REVOKED, START + timedelta(days=1)],
)
async def test_revocation_is_permanent_under_concurrency_and_clock_rollback(
    database: Database, kora_capture: Capture, expiry: datetime | None
) -> None:
    case = await seed_case(database, "revocation", expires_at=expiry)
    provider = StaticTokenAuthenticator({}, database)
    context = context_for(kora_capture)
    committed = asyncio.Event()

    async def revoke() -> None:
        await provider.revoke(case.principal.token_id, context)
        committed.set()

    async def check_after_commit() -> None:
        await committed.wait()
        for moment in (
            datetime(1900, 1, 1, tzinfo=UTC),
            REVOKED,
            START,
            datetime(1950, 1, 1, tzinfo=UTC),
        ):
            clock_context = replace(context, clock=AuthorityClock(moment))
            for fresh in (False, True):
                with pytest.raises(KoraProtocolError) as raised:
                    await provider.authenticate(
                        replace(case.request, requires_current_authority=fresh),
                        clock_context,
                    )
                assert_error(raised.value, "authentication_rejected")

    await asyncio.gather(*(revoke() for _ in range(8)), check_after_commit())
    await provider.revoke("missing-token", context)

    async with database.read_transaction() as connection:
        saved = (
            (
                await connection.execute(
                    select(api_token).where(api_token.c.id == case.principal.token_id)
                )
            )
            .mappings()
            .one()
        )
    assert saved["expires_at"] == REVOKED
    assert saved["owner"] == case.principal.owner
    assert (
        saved["digest"]
        == sha256(
            case.request.authorization.removeprefix("Bearer ").encode()
        ).hexdigest()
        if case.request.authorization is not None
        else False
    )


@pytest.mark.asyncio
async def test_revocation_preserves_worker_and_idempotency_history_references(
    database: Database, kora_capture: Capture
) -> None:
    case = await seed_case(database, "worker", kind="service")
    async with database.transaction() as connection:
        seed = Seeder(connection)
        worker_id = await seed.worker(case.principal.token_id)
        await connection.execute(
            insert(idempotency_record).values(
                token_id=case.principal.token_id,
                key="retained",
                request_digest="0" * 64,
                response={"accepted": True},
                expires_at=START + timedelta(days=1),
                **seed.stamps(),
            )
        )
    provider = StaticTokenAuthenticator({}, database)

    await provider.revoke(case.principal.token_id, context_for(kora_capture))

    async with database.read_transaction() as connection:
        assert (
            await connection.scalar(
                select(worker.c.id).where(worker.c.token_id == case.principal.token_id)
            )
            == worker_id
        )
        assert await connection.scalar(
            select(idempotency_record.c.response).where(
                idempotency_record.c.token_id == case.principal.token_id
            )
        ) == {"accepted": True}
        assert (
            await connection.scalar(
                select(api_token.c.expires_at).where(
                    api_token.c.id == case.principal.token_id
                )
            )
            == REVOKED
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "Basic value",
        "Bearer ",
        "Bearer a b",
        "Bearer token,token",
        "Bearer token\n",
        "Bearer ключ",
        " Bearer value",
    ],
)
async def test_malformed_headers_use_the_same_safe_refusal_without_database_access(
    database: Database, kora_capture: Capture, header: str | None
) -> None:
    provider = StaticTokenAuthenticator({}, database)
    ledger = ConnectionLedger(database)

    with pytest.raises(KoraProtocolError) as raised:
        await provider.authenticate(RequestInfo(header), context_for(kora_capture))

    assert_error(raised.value, "authentication_rejected")
    assert raised.value.message == "Authentication is required."
    assert ledger.checkouts == ledger.outstanding == 0


@pytest.mark.asyncio
async def test_digest_lookup_preserves_exact_bearer_bytes_and_actual_configured_rights(
    database: Database, kora_capture: Capture
) -> None:
    case = await seed_case(database, "exact")
    async with database.transaction() as connection:
        await connection.execute(
            update(api_token)
            .where(api_token.c.id == case.principal.token_id)
            .values(approve=False, platform=False)
        )
    assert case.request.authorization is not None
    provider = StaticTokenAuthenticator({}, database)
    context = context_for(kora_capture)

    principal = await provider.authenticate(
        RequestInfo(case.request.authorization.replace("Bearer ", "bEaReR   ")), context
    )
    with pytest.raises(KoraProtocolError) as changed_case:
        await provider.authenticate(
            RequestInfo(case.request.authorization.upper()), context
        )

    assert principal.owner == case.principal.owner
    assert principal.namespaces == case.principal.namespaces
    assert principal.approve is principal.platform is False
    assert_error(changed_case.value, "authentication_rejected")
    async with database.read_transaction() as connection:
        stored = (await connection.execute(select(api_token))).mappings().all()
    encoded = json.dumps([dict(row) for row in stored], default=str)
    assert case.request.authorization.removeprefix("Bearer ") not in encoded


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", [True, False])
async def test_invalid_authority_and_schema_failure_are_safe_internal_errors(
    database: Database, kora_capture: Capture, malformed: bool
) -> None:
    case = await seed_case(database, "invalid")
    provider = StaticTokenAuthenticator({}, database)
    ledger = ConnectionLedger(database)
    async with database.transaction() as connection:
        if malformed:
            await connection.execute(
                update(api_token)
                .where(api_token.c.id == case.principal.token_id)
                .values(kind="private-invalid-authority-marker")
            )
        else:
            await connection.execute(
                text("ALTER TABLE api_token RENAME TO retired_api_token")
            )

    with pytest.raises(KoraProtocolError) as raised:
        await provider.authenticate(case.request, context_for(kora_capture))

    assert_error(raised.value, "authentication_failed")
    assert ledger.outstanding == 0
    assert "private-invalid-authority-marker" not in str(raised.value)
    assert "private-invalid-authority-marker" not in json.dumps(kora_capture.finish())


@pytest.mark.asyncio
async def test_real_database_connection_unavailability_is_not_bad_credentials(
    database_settings: DatabaseSettings, tmp_path: Path, kora_capture: Capture
) -> None:
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        if database_settings.dialect is Dialect.POSTGRESQL:
            settings = replace(
                database_settings,
                url=database_settings.url.set(
                    host="127.0.0.1", port=reserved.getsockname()[1]
                ),
            )
        else:
            settings = replace(
                database_settings,
                url=database_settings.url.set(database=str(tmp_path / "unavailable")),
            )
        database = Database(settings)
        if settings.dialect is Dialect.SQLITE:
            settings.sqlite_path.unlink()
            settings.sqlite_path.mkdir()
        provider = StaticTokenAuthenticator({}, database)
        ledger = ConnectionLedger(database)

        try:
            for operation in (
                provider.authenticate(
                    RequestInfo("Bearer private-outage-marker"),
                    context_for(kora_capture),
                ),
                provider.revoke("token-example", context_for(kora_capture)),
            ):
                with pytest.raises(KoraProtocolError) as raised:
                    await operation
                assert_error(raised.value, "authentication_unavailable")
        finally:
            await database.close()

    assert ledger.outstanding == 0
    assert "private-outage-marker" not in json.dumps(kora_capture.finish())


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["authenticate", "revoke"])
async def test_cancellation_releases_a_pending_database_checkout(
    database: Database, kora_capture: Capture, operation: str
) -> None:
    case = await seed_case(database, "cancelled")
    provider = StaticTokenAuthenticator({}, database)
    ledger = ConnectionLedger(database)
    capacity = (
        SQLITE_READERS
        if database.dialect is Dialect.SQLITE
        else database.settings.pool_size + database.settings.max_overflow
    )
    writer = database.dialect is Dialect.SQLITE and operation == "revoke"
    if writer:
        capacity = 1
    held = AsyncExitStack()
    for _ in range(capacity):
        await held.enter_async_context(
            database.transaction() if writer else database.read_transaction()
        )
    started = asyncio.Event()

    async def authenticate() -> object:
        started.set()
        if operation == "revoke":
            return await provider.revoke(
                case.principal.token_id, context_for(kora_capture)
            )
        return await provider.authenticate(case.request, context_for(kora_capture))

    task = asyncio.create_task(authenticate())
    try:
        await started.wait()
        await turn()
        assert not task.done()
        assert ledger.outstanding == capacity

        task.cancel()
        await held.aclose()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await held.aclose()
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    assert ledger.outstanding == 0
    assert (
        await provider.authenticate(case.request, context_for(kora_capture))
        == case.principal
    )
    assert ledger.outstanding == 0


@pytest.mark.asyncio
async def test_borrowed_database_shutdown_and_provider_state_are_bounded(
    database: Database, kora_capture: Capture
) -> None:
    case = await seed_case(database, "shutdown")
    provider = StaticTokenAuthenticator({}, database)
    context = context_for(kora_capture)
    ledger = ConnectionLedger(database)
    original = {key: id(value) for key, value in vars(provider).items()}

    for _ in range(25):
        assert await provider.authenticate(case.request, context) == case.principal
    await database.close()
    for operation in (
        provider.authenticate(case.request, context),
        provider.revoke(case.principal.token_id, context),
    ):
        with pytest.raises(KoraProtocolError) as raised:
            await operation
        assert_error(raised.value, "authentication_unavailable")

    assert ledger.outstanding == 0
    assert {key: id(value) for key, value in vars(provider).items()} == original
    assert set(vars(provider)) == {"config_schema", "_database", "_errors"}


def test_static_token_entry_point_and_configuration(
    database: Database, kora_capture: Capture
) -> None:
    metadata = load_provider(
        ProtocolName.AUTHENTICATION, "static_token", kora_capture.diagnostics
    )

    instance = metadata.implementation({}, database)

    assert type(instance) is StaticTokenAuthenticator
    assert metadata.config_schema == {"type": "object", "additionalProperties": False}
    with pytest.raises(KoraProtocolError, match="configuration_invalid"):
        metadata.implementation({"private": "private-configuration-marker"}, database)
