import asyncio
import os
import sys

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.pool import PoolResetState

from kora.db.connection import (
    DATABASE_URL_VARIABLE,
    Database,
    Dialect,
    parse_database_url,
)
from kora.engine.run_event_position_maintenance import prepare_run_event_positions
from tests.db.closing import ConnectionLedger


async def prepare_until_killed(operation: str, proof: str) -> None:
    database = Database(parse_database_url(os.environ[DATABASE_URL_VARIABLE]))
    ledger = ConnectionLedger(database)
    backend_pid = 0
    inserted_positions = False
    committing = False

    async def fixture_fence() -> None:
        assert ledger.outstanding == database.units_in_flight() == 0

    async def identify_writer(connection: AsyncConnection) -> None:
        nonlocal backend_pid
        if database.dialect is Dialect.POSTGRESQL:
            observed: object = await connection.scalar(
                text("SELECT pg_catalog.pg_backend_pid()")
            )
            assert type(observed) is int
            assert observed > 0
            backend_pid = observed

    def observe_write(
        connection: object, cursor: object, statement: str, *details: object
    ) -> None:
        nonlocal inserted_positions
        if statement.startswith("INSERT INTO run_event_position ("):
            inserted_positions = True

    def observe_commit(connection: object) -> None:
        nonlocal committing
        committing = inserted_positions

    def park_before_checkin(
        native_connection: object, record: object, state: PoolResetState
    ) -> None:
        if not committing or state.terminate_only:
            return
        assert ledger.outstanding == database.units_in_flight() == 1
        sys.stdout.write(f"commit {backend_pid}\n")
        sys.stdout.flush()
        sys.stdin.buffer.read(1)
        raise RuntimeError(
            "The preparation child must terminate before acknowledgment."
        )

    engine = database.writer_engine.sync_engine
    event.listen(engine, "after_cursor_execute", observe_write)
    event.listen(engine, "commit", observe_commit)
    event.listen(engine, "reset", park_before_checkin)
    try:
        await prepare_run_event_positions(
            database, operation, proof, fixture_fence, verify_writer=identify_writer
        )
        sys.stdout.write("prepared\n")
        sys.stdout.flush()
    finally:
        await database.close()


def main() -> None:
    operation, proof = sys.argv[1:]
    asyncio.run(prepare_until_killed(operation, proof))


if __name__ == "__main__":
    main()
