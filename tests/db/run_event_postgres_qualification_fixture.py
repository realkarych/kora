import asyncio
import os
import secrets
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import pytest
from sqlalchemy import Connection, NullPool, event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.util import await_
from testcontainers.core.config import testcontainers_config

from kora.db.claim_source_values import ClaimSourcesRejected
from kora.db.connection import MIGRATION_LOCK_ID, Database, DatabaseSettings, Dialect
from kora.db.first_claim_basis import first_claim_basis_available
from kora.db.migrations import current_revision, upgrade_database
from kora.db.migrations.environment import MigrationReport, upgrade_to_head
from kora.db.run_event_maintenance_admission import (
    PREDECESSOR_REVISION,
    PositionMaintenanceRefused,
    PostgreSQLIdentity,
    require_postgresql_fence,
)
from kora.db.run_event_position_reads import (
    PositionTransition,
    read_position_transition,
)
from kora.db.run_event_position_v1 import POSITION_REVISION
from kora.engine.run_event_position_maintenance import prepare_run_event_positions
from tests.db.closing import ConnectionLedger, narrow_settings
from tests.db.predecessor import PredecessorDbUpgrade
from tests.db.test_postgres_writer_settings import postgres_driver
from tests.db.test_run_event_position_preparation import original_rows, positioned_rows
from tests.db.test_run_event_reads import SYSTEM, seed_event, seed_run
from tests.support.databases import (
    EMPTY_TEMPLATE,
    POSTGRES_IMAGE,
    POSTGRES_PORT,
    POSTGRES_USER,
    RYUK_VARIABLE,
    TEMPLATE_DATABASE,
    PostgresServer,
    SilentPostgresContainer,
)

TARGET = "position_managed_fixture"
FRESH_TARGET = "position_fresh_fixture"
NONEMPTY_TARGET = "position_nonempty_fixture"
KEEPER = "position_keeper"
OWNER = "position_owner"
OLD = "position_old"
NEXT = "position_next"
PREPARED = "position_unsettled_original"
OPERATION = "1" * 32
PROOF = "2" * 64
EARLIER = "0" * 25 + "2"
LATE = "0" * 25 + "1"
FROZEN_REVISION = text("SELECT version_num FROM public.alembic_version")
NATIVE_IDENTITY = text("""
    SELECT d.oid, (pg_catalog.pg_control_system()).system_identifier,
           pg_catalog.to_char(pg_catalog.pg_postmaster_start_time() AT TIME ZONE 'UTC',
                              'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
           (pg_catalog.pg_control_checkpoint()).timeline_id
    FROM pg_catalog.pg_database d
    WHERE d.datname = pg_catalog.current_database()
    """)
TARGET_LEASE_COUNT = text("""
    SELECT count(*) FROM pg_catalog.pg_locks
    WHERE locktype = 'advisory' AND objid = :key
      AND database = (
          SELECT oid FROM pg_catalog.pg_database WHERE datname = :catalog
      )
    """)
TARGET_SESSION_COUNT = text("""
    SELECT count(*) FROM pg_catalog.pg_stat_activity WHERE datname = :catalog
    """)


@pytest.fixture(scope="module")
def prepared_postgres_server() -> Generator[PostgresServer]:
    if os.environ.get(RYUK_VARIABLE) is None:
        testcontainers_config.ryuk_disabled = True
    password = secrets.token_urlsafe(24)
    container = SilentPostgresContainer(
        image=POSTGRES_IMAGE,
        port=POSTGRES_PORT,
        username=POSTGRES_USER,
        password=password,
        dbname=TEMPLATE_DATABASE,
        driver="asyncpg",
    ).with_command(["postgres", "-c", "max_prepared_transactions=4"])
    try:
        container.start()
        server = PostgresServer(
            host=container.get_container_host_ip(),
            port=container.get_exposed_port(POSTGRES_PORT),
            password=password,
            fresh_password=secrets.token_urlsafe(24),
        )
        server.wait_ready()
        yield server
    finally:
        container.stop()


@asynccontextmanager
async def administration(
    server: PostgresServer, target: str | None = None
) -> AsyncGenerator[AsyncConnection]:
    url = server.admin_url() if target is None else server.url_for(target)
    engine = create_async_engine(url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            yield connection
    finally:
        await engine.dispose()


@dataclass(frozen=True)
class ManagedInstallation:
    identity: PostgreSQLIdentity
    keeper: DatabaseSettings
    original: DatabaseSettings


@asynccontextmanager
async def managed_installation(
    server: PostgresServer, predecessor: PredecessorDbUpgrade
) -> AsyncGenerator[ManagedInstallation]:
    keeper_password = secrets.token_urlsafe(24)
    original_password = secrets.token_urlsafe(24)
    await server.create_database_in_loop(TARGET, EMPTY_TEMPLATE)
    try:
        async with administration(server) as admin:
            for statement in (
                f"CREATE ROLE {OWNER} NOLOGIN",
                f"CREATE ROLE {KEEPER} LOGIN PASSWORD '{keeper_password}' "
                f"IN ROLE {OWNER}",
                f"CREATE ROLE {OLD} LOGIN PASSWORD '{original_password}'",
                f"CREATE ROLE {NEXT} NOLOGIN",
                f"ALTER DATABASE {TARGET} OWNER TO {OWNER}",
                f"REVOKE CONNECT ON DATABASE {TARGET} FROM PUBLIC",
                f"GRANT CONNECT ON DATABASE {TARGET} TO {KEEPER}, {OLD}",
                f"ALTER ROLE {KEEPER} IN DATABASE {TARGET} SET ROLE TO {OWNER}",
            ):
                (await admin.exec_driver_sql(statement)).close()
        keeper = DatabaseSettings(
            server.url_for(TARGET).set(username=KEEPER, password=keeper_password),
            Dialect.POSTGRESQL,
        )
        original = DatabaseSettings(
            server.url_for(TARGET).set(username=OLD, password=original_password),
            Dialect.POSTGRESQL,
        )
        result = await asyncio.to_thread(
            predecessor,
            {"KORA_DATABASE_URL": keeper.url.render_as_string(hide_password=False)},
        )
        assert result.code == 0, result.stderr.decode()
        assert result.stdout == f"{PREDECESSOR_REVISION}\n".encode()
        async with administration(server, TARGET) as admin:
            for statement in (
                f"GRANT EXECUTE ON FUNCTION pg_catalog.pg_control_system() TO {OWNER}",
                "GRANT EXECUTE ON FUNCTION pg_catalog.pg_control_checkpoint() "
                f"TO {OWNER}",
                f"GRANT SELECT, INSERT ON public.event TO {OLD}",
            ):
                (await admin.exec_driver_sql(statement)).close()
            with await admin.execute(NATIVE_IDENTITY) as native:
                oid, system, generation, timeline = native.one()
        identity = PostgreSQLIdentity(
            backend="postgresql",
            catalog=TARGET,
            oid=oid,
            system_identifier=system,
            primary_generation=generation,
            timeline=timeline,
            maintenance_login=KEEPER,
            original_login=OLD,
            successor_login=NEXT,
            catalog_owner=OWNER,
        )
        yield ManagedInstallation(identity, keeper, original)
    finally:
        await server.drop_database_in_loop(TARGET)
        async with administration(server) as admin:
            for role in (NEXT, OLD, KEEPER, OWNER):
                (await admin.exec_driver_sql(f"DROP ROLE IF EXISTS {role}")).close()


async def target_state(server: PostgresServer) -> tuple[object, object]:
    async with administration(server, TARGET) as admin:
        return (
            await admin.scalar(FROZEN_REVISION),
            await admin.scalar(
                text("SELECT to_regclass('public.run_event_position') IS NOT NULL")
            ),
        )


async def assert_backend_released(server: PostgresServer, catalog: str) -> None:
    async with administration(server) as admin:
        assert await admin.scalar(TARGET_SESSION_COUNT, {"catalog": catalog}) == 0
        assert (
            await admin.scalar(
                TARGET_LEASE_COUNT, {"catalog": catalog, "key": MIGRATION_LOCK_ID}
            )
            == 0
        )


async def prepare_schema(
    database: Database, identity: PostgreSQLIdentity
) -> MigrationReport:
    def verify(connection: Connection) -> None:
        require_postgresql_fence(connection, identity)

    async with database.migration_connection() as connection:
        native = postgres_driver(
            (await connection.get_raw_connection()).driver_connection
        )
        await connection.run_sync(require_postgresql_fence, identity, inventory=True)
        assert (
            await connection.scalar(
                TARGET_LEASE_COUNT,
                {"catalog": identity.catalog, "key": MIGRATION_LOCK_ID},
            )
            == 1
        )
        report = await connection.run_sync(
            upgrade_to_head, "head", ((OPERATION, PROOF), verify)
        )
        assert (
            postgres_driver((await connection.get_raw_connection()).driver_connection)
            is native
        )
        return report


@pytest.mark.asyncio
async def test_managed_postgres_waits_for_original_work_and_resumes_native_positions(
    prepared_postgres_server: PostgresServer,
    predecessor_db_upgrade: PredecessorDbUpgrade,
) -> None:
    server = prepared_postgres_server
    async with managed_installation(server, predecessor_db_upgrade) as installation:
        database = Database(narrow_settings(installation.keeper, pool_size=1))
        ledger = ConnectionLedger(database)
        original = create_async_engine(installation.original.url, poolclass=NullPool)
        try:
            async with database.transaction() as writer:
                scope = await seed_run(writer, "managed-position")
                await seed_event(writer, scope, SYSTEM, 0, event_id=EARLIER)
            async with original.connect() as old_writer:
                await seed_event(old_writer, scope, SYSTEM, 0, event_id=LATE)
                async with administration(server) as admin:
                    (await admin.exec_driver_sql(f"ALTER ROLE {OLD} NOLOGIN")).close()
                with pytest.raises(PositionMaintenanceRefused):
                    await prepare_schema(database, installation.identity)
                assert await target_state(server) == (PREDECESSOR_REVISION, False)
                (
                    await old_writer.exec_driver_sql(
                        f"PREPARE TRANSACTION '{PREPARED}'"
                    )
                ).close()
                await old_writer.rollback()

            with pytest.raises(DBAPIError, match="not permitted to log in"):
                async with original.connect():
                    raise AssertionError("The original role reconnected.")
            try:
                with pytest.raises(PositionMaintenanceRefused):
                    await prepare_schema(database, installation.identity)
                assert await target_state(server) == (PREDECESSOR_REVISION, False)
            finally:
                async with administration(server, TARGET) as admin:
                    (
                        await admin.exec_driver_sql(f"COMMIT PREPARED '{PREPARED}'")
                    ).close()

            foreign = installation.identity.model_copy(
                update={"timeline": installation.identity.timeline + 1}
            )
            with pytest.raises(PositionMaintenanceRefused):
                await prepare_schema(database, foreign)
            assert await target_state(server) == (PREDECESSOR_REVISION, False)

            report = await prepare_schema(database, installation.identity)
            assert (report.from_revision, report.to_revision, report.steps) == (
                PREDECESSOR_REVISION,
                POSITION_REVISION,
                1,
            )

            async def check_boundary() -> None:
                assert ledger.outstanding == database.units_in_flight() == 0

            async def verify_writer(connection: AsyncConnection) -> None:
                await connection.run_sync(
                    require_postgresql_fence, installation.identity
                )

            before = await original_rows(database)
            async with administration(server) as admin:
                (await admin.exec_driver_sql(f"ALTER ROLE {OLD} LOGIN")).close()
            try:
                with pytest.raises(PositionMaintenanceRefused):
                    await prepare_run_event_positions(
                        database,
                        OPERATION,
                        PROOF,
                        check_boundary,
                        verify_writer=verify_writer,
                    )
                assert await positioned_rows(database, scope.run_id) == []
                async with database.read_transaction() as reader:
                    staged = await read_position_transition(reader)
                    with pytest.raises(ClaimSourcesRejected):
                        await first_claim_basis_available(reader)
                assert staged == PositionTransition(OPERATION, PROOF, None, False)
            finally:
                async with administration(server) as admin:
                    (await admin.exec_driver_sql(f"ALTER ROLE {OLD} NOLOGIN")).close()

            ready_to_resume = await prepare_schema(database, installation.identity)
            assert ready_to_resume.steps == 0
            await prepare_run_event_positions(
                database, OPERATION, PROOF, check_boundary, verify_writer=verify_writer
            )
            assert await positioned_rows(database, scope.run_id) == [
                (1, LATE),
                (2, EARLIER),
            ]
            assert await original_rows(database) == before
            repeated = await prepare_schema(database, installation.identity)
            assert repeated.steps == 0
            async with database.read_transaction() as reader:
                assert await first_claim_basis_available(reader)
                assert await read_position_transition(reader) == PositionTransition(
                    OPERATION, PROOF, scope.run_id, True
                )
        finally:
            await original.dispose()
            await database.close()

        assert ledger.outstanding == database.units_in_flight() == 0
        assert ledger.opened == ledger.closed
        await assert_backend_released(server, TARGET)


@pytest.mark.asyncio
async def test_fresh_postgres_refuses_a_catalog_with_a_large_object(
    postgres_server: PostgresServer,
) -> None:
    server = postgres_server
    await server.create_database_in_loop(NONEMPTY_TARGET, EMPTY_TEMPLATE)
    try:
        settings = await server.prepare_fresh_database(NONEMPTY_TARGET)
        payload = b"owned native large object"
        async with administration(server, NONEMPTY_TARGET) as admin:
            object_id = await admin.scalar(
                text("SELECT pg_catalog.lo_from_bytea(0, :payload)"),
                {"payload": payload},
            )
            assert type(object_id) is int
        with pytest.raises(PositionMaintenanceRefused):
            await upgrade_database(settings)
        await assert_backend_released(server, NONEMPTY_TARGET)
        async with administration(server, NONEMPTY_TARGET) as admin:
            assert await admin.run_sync(current_revision) is None
            assert (
                await admin.scalar(
                    text("SELECT pg_catalog.lo_get(:identity)"), {"identity": object_id}
                )
                == payload
            )
    finally:
        await server.drop_database_in_loop(NONEMPTY_TARGET)


@pytest.mark.asyncio
async def test_fresh_postgres_loses_native_authority_after_first_revision_commit(
    postgres_server: PostgresServer,
) -> None:
    server = postgres_server
    await server.create_database_in_loop(FRESH_TARGET, EMPTY_TEMPLATE)
    try:
        settings = await server.prepare_fresh_database(FRESH_TARGET)
        database = Database(narrow_settings(settings, pool_size=1))
        ledger = ConnectionLedger(database)
        stamp_written = False
        power_withdrawn = False

        def observe_stamp(
            connection: Connection, cursor: object, statement: str, *args: object
        ) -> None:
            nonlocal stamp_written
            if statement.startswith("INSERT INTO alembic_version "):
                stamp_written = True

        async def withdraw_authority() -> None:
            competitor = create_async_engine(settings.url, poolclass=NullPool)
            try:
                with pytest.raises(DBAPIError, match="too many connections"):
                    async with competitor.connect():
                        raise AssertionError("A second login opened the fresh catalog.")
            finally:
                await competitor.dispose()
            async with administration(server, FRESH_TARGET) as observer:
                assert await observer.run_sync(current_revision) == "0001"
                assert (
                    await observer.scalar(
                        TARGET_LEASE_COUNT,
                        {"catalog": FRESH_TARGET, "key": MIGRATION_LOCK_ID},
                    )
                    == 1
                )
            async with administration(server) as admin:
                (
                    await admin.exec_driver_sql(
                        f"ALTER DATABASE {FRESH_TARGET} CONNECTION LIMIT 2"
                    )
                ).close()

        def change_before_next_dispatch(
            connection: Connection, cursor: object, statement: str, *args: object
        ) -> None:
            nonlocal power_withdrawn
            driver = postgres_driver(connection.connection.driver_connection)
            if stamp_written and not power_withdrawn and not driver.is_in_transaction():
                power_withdrawn = True
                await_(withdraw_authority())

        engine = database.writer_engine.sync_engine
        event.listen(engine, "after_cursor_execute", observe_stamp)
        event.listen(engine, "before_cursor_execute", change_before_next_dispatch)
        try:
            with pytest.raises(PositionMaintenanceRefused):
                async with database.migration_connection() as connection:
                    await connection.run_sync(upgrade_to_head)
        finally:
            event.remove(engine, "after_cursor_execute", observe_stamp)
            event.remove(engine, "before_cursor_execute", change_before_next_dispatch)
            await database.close()

        assert stamp_written
        assert power_withdrawn
        assert ledger.opened == ledger.closed == 1
        assert ledger.outstanding == database.units_in_flight() == 0
        await assert_backend_released(server, FRESH_TARGET)
        async with administration(server) as admin:
            (
                await admin.exec_driver_sql(
                    f"ALTER DATABASE {FRESH_TARGET} CONNECTION LIMIT 1"
                )
            ).close()
        with pytest.raises(PositionMaintenanceRefused):
            await upgrade_database(settings)
        async with administration(server, FRESH_TARGET) as observer:
            assert await observer.run_sync(current_revision) == "0001"
            assert not await observer.scalar(
                text("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = 'price_table'
                      AND column_name = 'version'
                )
                """)
            )
            assert not await observer.scalar(
                text("SELECT to_regclass('public.run_event_position') IS NOT NULL")
            )
        await assert_backend_released(server, FRESH_TARGET)
    finally:
        await server.drop_database_in_loop(FRESH_TARGET)
