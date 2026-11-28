import asyncio
import os
import signal
import sys
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import delete, insert, literal_column, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.connection import DATABASE_URL_VARIABLE, Database, Dialect
from kora.db.run_event_maintenance_admission import MaintenanceProfile, parse_evidence
from kora.db.run_event_position_reads import (
    PositionTransition,
    read_position_transition,
)
from kora.db.run_event_position_v1 import MAX_POSITION, RunEventPositionRejected
from kora.db.tables import (
    event,
    run_event_position,
    run_event_position_state,
    run_event_position_transition,
)
from kora.db.transactions import writer_transaction
from kora.engine.locks import TransitionLocks
from kora.engine.run_event_position_maintenance import prepare_run_event_positions
from tests.db.closing import ConnectionLedger
from tests.db.run_event_position_cancellation import (
    Verifier,
    cancel_position_preparation,
)
from tests.db.test_run_event_reads import SYSTEM, USER, seed_event, seed_run
from tests.engine.lock_support import DeliberateRollback, lock_inventory

OPERATION: str = "2" * 32
PROOF: str = "a" * 64


class PreparationInterrupted(RuntimeError):
    pass


def test_maintenance_evidence_refuses_large_json_integers_with_fixed_error() -> None:
    payload = b'{"control_inode":' + b"7" * 5000 + b"}"
    with pytest.raises(
        RunEventPositionRejected, match=r"^Run-event position state was not admitted\.$"
    ):
        parse_evidence(payload, MaintenanceProfile)


async def mark_frozen_fixture(connection: AsyncConnection) -> None:
    await connection.execute(delete(run_event_position_state))
    await connection.execute(
        update(run_event_position_transition)
        .where(run_event_position_transition.c.id == 1)
        .values(operation=OPERATION, proof=PROOF, ready=False)
    )


async def original_rows(database: Database) -> list[dict[str, object]]:
    async with database.read_snapshot() as reader:
        with await reader.execute(select(event).order_by(event.c.id)) as rows:
            return [dict(row) for row in rows.mappings()]


async def positioned_rows(database: Database, run_id: str) -> list[tuple[int, str]]:
    positions = run_event_position.c
    async with database.read_snapshot() as reader:
        with await reader.execute(
            select(positions.position, positions.event_id)
            .where(positions.run_id == run_id)
            .order_by(positions.position)
        ) as rows:
            return [cast(tuple[int, str], tuple(row)) for row in rows]


async def run_checkpoint(
    database: Database, run_id: str
) -> tuple[int, str | None, bool]:
    state = run_event_position_state.c
    async with database.read_snapshot() as reader:
        with await reader.execute(
            select(state.high_water, state.legacy_event_id, state.ready).where(
                state.run_id == run_id
            )
        ) as rows:
            return cast(tuple[int, str | None, bool], tuple(rows.one()))


@pytest.mark.asyncio
async def test_frozen_positions_resume_the_committed_prefix_without_relabeling(
    database: Database,
) -> None:
    ids = [f"{number:026d}" for number in range(1, 259)]
    async with database.transaction() as writer:
        first = await seed_run(writer, "frozen-first")
        sibling = await seed_run(writer, "frozen-sibling")
        empty = await seed_run(writer, "frozen-empty")
        sibling_event = await seed_event(writer, sibling, SYSTEM, 8)
        for identity in reversed(ids[1:]):
            await seed_event(writer, first, USER, 8, event_id=identity)
    async with database.transaction() as writer:
        await seed_event(writer, first, SYSTEM, 13, event_id=ids[0])

    async def discard_original() -> None:
        async with database.transaction() as writer:
            await seed_event(writer, first, SYSTEM, 0, event_id="0" * 26)
            raise DeliberateRollback()

    with pytest.raises(DeliberateRollback):
        await discard_original()
    before = await original_rows(database)
    assert len(before) == len(ids) + 1
    assert first.run_id < sibling.run_id < empty.run_id
    async with database.transaction() as writer:
        await mark_frozen_fixture(writer)
    ledger = ConnectionLedger(database)

    async def interrupt_after_first_commit() -> None:
        if await positioned_rows(database, first.run_id):
            raise PreparationInterrupted()

    async def observe_held_fixture() -> None:
        assert ledger.outstanding == database.units_in_flight() == 0
        assert database.writer_is_free()

    async def refuse_current_writer(writer: AsyncConnection) -> None:
        assert writer_transaction(writer).connection is writer
        assert (await read_position_transition(writer)).ready is False
        raise PreparationInterrupted()

    with pytest.raises(PreparationInterrupted):
        await prepare_run_event_positions(
            database,
            OPERATION,
            PROOF,
            observe_held_fixture,
            verify_writer=refuse_current_writer,
        )
    assert await positioned_rows(database, first.run_id) == []
    await observe_held_fixture()

    with pytest.raises(PreparationInterrupted):
        await prepare_run_event_positions(
            database, OPERATION, PROOF, interrupt_after_first_commit
        )
    prefix = await positioned_rows(database, first.run_id)
    checkpoint = await run_checkpoint(database, first.run_id)
    async with database.read_snapshot() as reader:
        interrupted = await read_position_transition(reader)
    await observe_held_fixture()
    assert await lock_inventory(database) == []
    settings = database.settings
    await database.close()

    resumed = Database(settings)
    resumed_ledger = ConnectionLedger(resumed)

    async def observe_resumed_fixture() -> None:
        assert resumed_ledger.outstanding == resumed.units_in_flight() == 0
        assert resumed.writer_is_free()

    try:
        with pytest.raises(RunEventPositionRejected):
            await prepare_run_event_positions(
                resumed, OPERATION, "c" * 64, observe_resumed_fixture
            )
        preserved = await positioned_rows(resumed, first.run_id)
        await prepare_run_event_positions(
            resumed, OPERATION, PROOF, observe_resumed_fixture
        )
        complete = await positioned_rows(resumed, first.run_id)
        await prepare_run_event_positions(
            resumed, OPERATION, PROOF, observe_resumed_fixture
        )
        repeated = await positioned_rows(resumed, first.run_id)
        sibling_positions = await positioned_rows(resumed, sibling.run_id)
        empty_positions = await positioned_rows(resumed, empty.run_id)
        final_original = await original_rows(resumed)
        async with resumed.read_snapshot() as reader:
            prepared = await read_position_transition(reader)

        assert prefix == preserved == list(enumerate(ids[:256], start=1))
        assert checkpoint == (256, ids[255], False)
        assert interrupted.ready is False
        assert prepared.ready is True
        assert (prepared.operation, prepared.proof) == (OPERATION, PROOF)
        assert complete == repeated == list(enumerate(ids, start=1))
        assert sibling_positions == [(1, sibling_event)]
        assert empty_positions == []
        assert await run_checkpoint(resumed, empty.run_id) == (0, None, True)
        assert await run_checkpoint(resumed, first.run_id) == (len(ids), ids[-1], True)
        assert final_original == before
        assert await lock_inventory(resumed) == []
        await observe_resumed_fixture()
    finally:
        await resumed.close()


@pytest.mark.asyncio
async def test_backfill_refuses_event_with_original_run_and_foreign_namespace(
    database: Database,
) -> None:
    async with database.transaction() as writer:
        first = await seed_run(writer, "frozen-first")
        sibling = await seed_run(writer, "foreign-owner")
        mismatched = await seed_event(writer, first, SYSTEM, 8)
        await writer.execute(
            update(event)
            .where(event.c.id == mismatched)
            .values(namespace_id=sibling.namespace_id)
        )
        await mark_frozen_fixture(writer)
    before = await original_rows(database)
    ledger = ConnectionLedger(database)

    async def observe_fixture() -> None:
        assert ledger.outstanding == database.units_in_flight() == 0

    with pytest.raises(RunEventPositionRejected):
        await prepare_run_event_positions(database, OPERATION, PROOF, observe_fixture)
    after = await original_rows(database)
    positioned = await positioned_rows(database, first.run_id)
    async with database.read_snapshot() as reader:
        checkpoint = await read_position_transition(reader)
        saved_state = await reader.scalar(select(run_event_position_state.c.run_id))

    assert before == after
    assert positioned == []
    assert checkpoint == PositionTransition(OPERATION, PROOF, None, False)
    assert saved_state is None
    assert await lock_inventory(database) == []
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
async def test_native_positions_require_positive_signed_integer_storage(
    database: Database,
) -> None:
    async with database.transaction() as writer:
        scope = await seed_run(writer)
        identity = await seed_event(writer, scope, SYSTEM, 8)

    async def insert_position(value: str) -> None:
        async with database.transaction() as writer:
            await writer.execute(
                insert(run_event_position).values(
                    run_id=scope.run_id,
                    position=literal_column(value),
                    event_id=identity,
                )
            )

    for invalid in ("0", "-1", "'1.5'", str(MAX_POSITION + 1)):
        with pytest.raises(DBAPIError):
            await insert_position(invalid)
    await insert_position(str(MAX_POSITION))
    positions = await positioned_rows(database, scope.run_id)

    assert positions == [(MAX_POSITION, identity)]
    assert type(positions[0][0]) is int
    assert database.units_in_flight() == 0


async def preparation_backend_active(database: Database, backend_pid: int) -> bool:
    async with database.read_transaction() as reader:
        return (
            await reader.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_stat_activity "
                    "WHERE pid = :pid AND datname = pg_catalog.current_database())"
                ),
                {"pid": backend_pid},
            )
        ) is True


@pytest.mark.asyncio
async def test_preparation_resumes_after_kill_before_acknowledgment(
    database: Database,
) -> None:
    keys = [f"{number:026d}" for number in range(1, 259)]
    async with database.transaction() as writer:
        scope = await seed_run(writer, "crash-frozen")
        for key in reversed(keys):
            await seed_event(writer, scope, SYSTEM, 8, event_id=key)
        await mark_frozen_fixture(writer)
    original = await original_rows(database)
    settings = database.settings
    await database.close()

    observer = Database(settings)
    observation = ConnectionLedger(observer)
    prefix = list(enumerate(keys[:256], start=1))
    try:
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tests.db.run_event_position_crash_probe",
            OPERATION,
            PROOF,
            cwd=Path(__file__).resolve().parents[2],
            env={
                "PATH": os.environ["PATH"],
                DATABASE_URL_VARIABLE: settings.url.render_as_string(
                    hide_password=False
                ),
            },
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            assert child.stdout is not None
            async with asyncio.timeout(20):
                notice, pid = (await child.stdout.readline()).split()
                assert notice == b"commit"
                backend_pid = int(pid)
                if observer.dialect is Dialect.POSTGRESQL:
                    assert backend_pid > 0
                    assert await preparation_backend_active(observer, backend_pid)
                else:
                    assert backend_pid == 0
                assert await positioned_rows(observer, scope.run_id) == prefix
                assert await run_checkpoint(observer, scope.run_id) == (
                    256,
                    keys[255],
                    False,
                )
                async with observer.read_snapshot() as reader:
                    assert await read_position_transition(reader) == PositionTransition(
                        OPERATION, PROOF, None, False
                    )
                assert child.returncode is None
        finally:
            if child.returncode is None:
                child.kill()
            async with asyncio.timeout(10):
                acknowledgment, diagnostics = await child.communicate()

        assert child.returncode == -signal.SIGKILL
        assert acknowledgment == diagnostics == b""
        if observer.dialect is Dialect.POSTGRESQL:
            async with asyncio.timeout(10):
                while await preparation_backend_active(observer, backend_pid):
                    await asyncio.sleep(0.01)
        assert observation.outstanding == observer.units_in_flight() == 0
    finally:
        await observer.close()
    assert observation.opened == observation.closed

    resumed = Database(settings)
    ledger = ConnectionLedger(resumed)
    try:
        loop = asyncio.get_running_loop()
        async with resumed.transaction(admission_deadline=loop.time() + 5) as writer:
            assert await TransitionLocks(writer).acquire_run(scope.run_id, wait=False)
        assert await positioned_rows(resumed, scope.run_id) == prefix

        async def observe_released() -> None:
            assert ledger.outstanding == resumed.units_in_flight() == 0
            assert resumed.writer_is_free()
            assert await lock_inventory(resumed) == []

        expected = list(enumerate(keys, start=1))
        await prepare_run_event_positions(resumed, OPERATION, PROOF, observe_released)
        assert await positioned_rows(resumed, scope.run_id) == expected
        assert await run_checkpoint(resumed, scope.run_id) == (
            len(keys),
            keys[-1],
            True,
        )
        async with resumed.read_snapshot() as reader:
            ready = await read_position_transition(reader)
        assert ready == PositionTransition(OPERATION, PROOF, scope.run_id, True)
        await prepare_run_event_positions(resumed, OPERATION, PROOF, observe_released)
        assert await positioned_rows(resumed, scope.run_id) == expected
        assert await original_rows(resumed) == original
        await observe_released()
    finally:
        await resumed.close()
    assert ledger.opened == ledger.closed


@pytest.mark.asyncio
async def test_cancelled_position_preparation_settles_native_statement(
    database: Database,
) -> None:
    async with database.transaction() as writer:
        scope = await seed_run(writer, "cancel-frozen")
        keys = [f"{number:026d}" for number in range(1, 4)]
        for key in reversed(keys):
            await seed_event(writer, scope, SYSTEM, 8, event_id=key)
        await mark_frozen_fixture(writer)
    before = await original_rows(database)
    ledger = ConnectionLedger(database)

    async def observe_settled() -> None:
        assert ledger.outstanding == database.units_in_flight() == 0
        assert database.writer_is_free()
        assert await lock_inventory(database) == []

    async def verify_uncommitted_batch() -> None:
        assert ledger.outstanding == database.units_in_flight() == 1
        assert await positioned_rows(database, scope.run_id) == []
        async with database.read_snapshot() as reader:
            assert await read_position_transition(reader) == PositionTransition(
                OPERATION, PROOF, None, False
            )

    async def prepare(verify_writer: Verifier | None = None) -> None:
        await prepare_run_event_positions(
            database, OPERATION, PROOF, observe_settled, verify_writer=verify_writer
        )

    await cancel_position_preparation(
        database, scope.run_id, prepare, verify_uncommitted_batch
    )
    await observe_settled()
    assert await positioned_rows(database, scope.run_id) == []
    async with database.read_snapshot() as reader:
        assert await read_position_transition(reader) == PositionTransition(
            OPERATION, PROOF, None, False
        )
        assert await reader.scalar(select(run_event_position_state.c.run_id)) is None

    await prepare()
    await observe_settled()
    assert await positioned_rows(database, scope.run_id) == list(
        enumerate(keys, start=1)
    )
    assert await run_checkpoint(database, scope.run_id) == (len(keys), keys[-1], True)
    async with database.read_snapshot() as reader:
        assert await read_position_transition(reader) == PositionTransition(
            OPERATION, PROOF, scope.run_id, True
        )
    assert await original_rows(database) == before
    await observe_settled()
