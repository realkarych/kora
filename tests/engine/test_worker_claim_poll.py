import asyncio
from dataclasses import replace
from datetime import timedelta
from typing import Literal, cast

import pytest
from sqlalchemy import Connection, delete, event, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.util.concurrency import await_

from kora.api.installed_worker import installed_worker_profile
from kora.cel_eval.thread import Evaluator
from kora.db.claim_source_values import ClaimSourcesRejected, prepare_claim_sources
from kora.db.connection import POSTGRESQL_WAL_SETTINGS_QUERY, Database, Dialect
from kora.db.grant_history_v1 import ACK_FIELDS, TIP_FIELDS
from kora.db.tables import (
    attempt,
    attempt_claim_read_admission,
    attempt_dispatcher,
    run,
    worker,
)
from kora.db.worker_claim_poll import advance_claim_cursor
from kora.engine.claim_context import claim_worker
from kora.engine.claim_sources import update_claim_sources
from kora.engine.locks import TransitionLocks
from kora.engine.namespace_policy import (
    NamespacePolicyConfiguration,
    namespace_policy_scopes,
    write_namespace_policy,
)
from kora.engine.worker_claim_poll import ClaimPollLimits, WorkerClaimPoll
from kora.ir.graph_json import ObjectReader
from kora.ir.json import JsonObject
from tests.api.claim_support import LIMITS as POLL_LIMITS
from tests.api.claim_support import admit_expiry_run, poller
from tests.api.deployment_cases import evaluator, skeleton_ir
from tests.api.worker_support import CAPABILITIES
from tests.db.closing import ConnectionLedger
from tests.db.test_postgres_writer_settings import postgres_driver
from tests.engine.claim_support import (
    UNCERTAINTY,
    WORKER_BYTES,
    ClaimCase,
    another_run,
    claim_case,
    state,
)
from tests.engine.test_claim_sources import ALLOWANCE
from tests.plugins.authentication.support import AuthorityClock

__all__ = ["evaluator"]

WINNER_DEFAULT: timedelta = timedelta(seconds=5)
CONTENDER_DEFAULT: timedelta = timedelta(seconds=41)
ClaimTransactionMode = Literal["read", "snapshot", "write"]


class NativeClaimTransactions:
    def __init__(self, database: Database) -> None:
        self.database: Database = database
        self.ledger: ConnectionLedger = ConnectionLedger(database)
        self.connections: dict[Connection, ClaimTransactionMode] = {}
        self.begins: dict[Connection, int] = {}
        self.entries: list[ClaimTransactionMode] = []
        self.checkout_load: list[int] = []
        self.writer_outcomes: list[str] = []
        self.wal_reads: list[tuple[bool, tuple[str, ...]]] = []
        self.mutations: list[str] = []
        self.writer_entered: asyncio.Event = asyncio.Event()
        self.release_writer: asyncio.Event = asyncio.Event()
        self.final_snapshot_entered: asyncio.Event = asyncio.Event()
        self.release_final_snapshot: asyncio.Event = asyncio.Event()
        self.pause_writer: bool = False
        self.pause_final_snapshot: bool = False
        self.pause_final_read: bool = False
        self.snapshots: int = 0
        for engine in {database.reader_engine, database.writer_engine}:
            event.listen(engine.sync_engine, "checkout", self._checkout)
            event.listen(engine.sync_engine, "begin", self._begin)
            event.listen(engine.sync_engine, "before_cursor_execute", self._before)
            event.listen(engine.sync_engine, "after_cursor_execute", self._after)
            event.listen(engine.sync_engine, "commit", self._commit)
            event.listen(engine.sync_engine, "rollback", self._rollback)
        if database.dialect is Dialect.SQLITE:
            event.listen(
                database.writer_engine.sync_engine, "checkout", self._sqlite_writer
            )

    def _checkout(self, *arguments: object) -> None:
        self.checkout_load.append(self.ledger.outstanding)

    def _begin(self, connection: Connection) -> None:
        self.begins[connection] = self.begins.get(connection, 0) + 1

    def _sqlite_writer(self, *arguments: object) -> None:
        self._pause_writer()

    def _pause_writer(self) -> None:
        self.writer_entered.set()
        if self.pause_writer and not self.release_writer.is_set():
            await_(self.release_writer.wait())

    def _before(
        self, connection: Connection, cursor: object, statement: str, *rest: object
    ) -> None:
        normalized = statement.lstrip().upper()
        if normalized.startswith(("INSERT ", "UPDATE ", "DELETE ", "REPLACE ")):
            self.mutations.append(normalized)
        if connection in self.connections:
            return
        mode = self._mode(connection, normalized)
        self.connections[connection] = mode
        self.entries.append(mode)
        if mode == "write" and self.database.dialect is Dialect.POSTGRESQL:
            self._pause_writer()
        if mode == "snapshot":
            self.snapshots += 1
            if self.pause_final_snapshot and self.snapshots == 2:
                self.final_snapshot_entered.set()
                await_(self.release_final_snapshot.wait())

    def _after(
        self, connection: Connection, cursor: object, statement: str, *rest: object
    ) -> None:
        if statement == POSTGRESQL_WAL_SETTINGS_QUERY.text:
            native = postgres_driver(connection.connection.driver_connection)
            self.wal_reads.append(
                (native.is_in_transaction(), tuple(self.writer_outcomes))
            )
        if (
            self.pause_final_read
            and not self.final_snapshot_entered.is_set()
            and self.snapshots == 2
            and self.connections.get(connection) == "snapshot"
            and statement.lstrip().upper().startswith("SELECT")
        ):
            self.final_snapshot_entered.set()
            await_(self.release_final_snapshot.wait())

    def _mode(self, connection: Connection, statement: str) -> ClaimTransactionMode:
        if self.database.dialect is Dialect.SQLITE:
            if connection.engine is not self.database.reader_engine.sync_engine:
                return "write"
            return "snapshot" if statement == "BEGIN DEFERRED" else "read"
        if statement == "SET TRANSACTION READ ONLY":
            return "read"
        if statement == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY":
            return "snapshot"
        return "write"

    def _commit(self, connection: Connection) -> None:
        if self.connections.get(connection) == "write":
            self.writer_outcomes.append("commit")

    def _rollback(self, connection: Connection) -> None:
        if self.connections.get(connection) == "write":
            self.writer_outcomes.append("rollback")

    def assert_closed_and_read_only(self, *, writers: int) -> None:
        assert self.ledger.checkouts == len(self.entries)
        assert self.checkout_load == [1] * len(self.entries)
        assert self.ledger.outstanding == self.database.units_in_flight() == 0
        assert self.ledger.opened == self.ledger.closed
        assert self.database.writer_is_free()
        assert self.writer_outcomes == ["rollback"] * writers
        assert not self.mutations


def configured_poller(
    database: Database, default: timedelta, *, limits: ClaimPollLimits = POLL_LIMITS
) -> WorkerClaimPoll:
    return WorkerClaimPoll(
        database,
        {"local": replace(CAPABILITIES, default_stall_after=default)},
        AuthorityClock(),
        limits,
        UNCERTAINTY,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("late", [False, True], ids=["early", "late-same-attempt"])
async def test_same_attempt_replay_uses_winner_and_releases_native_checkout(
    database: Database, evaluator: Evaluator, late: bool
) -> None:
    case = await claim_case(database, evaluator)
    winner_poll = configured_poller(database, WINNER_DEFAULT)
    guard_wal = late and database.dialect is Dialect.POSTGRESQL
    replay_database = Database(
        database.settings, require_postgresql_wal_settings=guard_wal
    )
    observer = NativeClaimTransactions(replay_database)
    observer.pause_writer = late
    contender = configured_poller(replay_database, CONTENDER_DEFAULT)

    try:
        async with asyncio.timeout(15), asyncio.TaskGroup() as tasks:
            pending = None
            if late:
                pending = tasks.create_task(contender.poll(case.query, case.principal))
            try:
                if late:
                    await observer.writer_entered.wait()
                winner = await winner_poll.poll(case.query, case.principal)
                committed = await state(database)
            finally:
                observer.release_writer.set()
            replayed = (
                await pending
                if pending is not None
                else await contender.poll(case.query, case.principal)
            )
        after = await state(database)
    finally:
        await replay_database.close()

    assert winner is not None
    assert replayed is not None
    assert winner.system.attempt_id == case.binding.attempt_id
    immutable = ObjectReader(winner.immutable.payload.decode()).object()
    assert cast(JsonObject, immutable["limits"])["stall_after_us"] == "5000000"
    assert replayed.encode() == winner.encode()
    assert committed == after
    assert (
        len(after["attempt_dispatcher"]) == len(after["attempt_first_claim_basis"]) == 1
    )
    assert observer.entries.count("write") == int(late)
    if late:
        writer = observer.entries.index("write")
        assert observer.entries[writer + 1 :].count("snapshot") == 2
        [writer_connection] = [
            connection
            for connection, mode in observer.connections.items()
            if mode == "write"
        ]
        assert (observer.begins[writer_connection], observer.wal_reads) == (
            1,
            [(True, ())] if guard_wal else [],
        )
    else:
        assert observer.snapshots == 2
        assert observer.wal_reads == []
    observer.assert_closed_and_read_only(writers=int(late))


@pytest.mark.asyncio
@pytest.mark.parametrize("installed", [False, True], ids=["generic", "installed"])
@pytest.mark.parametrize(
    ("native_shortfall", "raw_shortfall"),
    [(0, 0), (1, 0), (0, 1)],
    ids=["exact", "native-short", "raw-short"],
)
async def test_live_replay_admits_exact_aggregate_before_snapshot_checkout(
    database: Database,
    evaluator: Evaluator,
    native_shortfall: int,
    raw_shortfall: int,
    installed: bool,
) -> None:
    profile = installed_worker_profile("2000")
    assert profile is not None
    limits = profile.limits if installed else POLL_LIMITS
    native, raw = (129_790_652, 89_603_964) if installed else (122_612_732, 87_612_156)
    case = await claim_case(database, evaluator)
    winner = await configured_poller(database, WINNER_DEFAULT, limits=limits).poll(
        case.query, case.principal
    )
    before = await state(database)
    bounded = replace(
        limits,
        snapshot=replace(
            limits.snapshot,
            native_bytes=native - native_shortfall,
            raw_bytes=raw - raw_shortfall,
        ),
    )
    replay_database = Database(database.settings)
    observer = NativeClaimTransactions(replay_database)
    contender = configured_poller(replay_database, CONTENDER_DEFAULT, limits=bounded)
    short = bool(native_shortfall or raw_shortfall)

    replayed = None
    try:
        async with asyncio.timeout(15):
            if short:
                with pytest.raises(
                    ClaimSourcesRejected, match="claim sources are unavailable"
                ):
                    await contender.poll(case.query, case.principal)
            else:
                replayed = await contender.poll(case.query, case.principal)
        after = await state(database)
    finally:
        await replay_database.close()

    assert winner is not None
    assert before == after
    if short:
        assert observer.snapshots == 0
        assert observer.entries in ([], ["read"])
    else:
        assert replayed is not None
        assert replayed.encode() == winner.encode()
        assert observer.snapshots == 2
    observer.assert_closed_and_read_only(writers=0)


async def revise_replay_sources(
    database: Database, case: ClaimCase, change: Literal["source", "policy"]
) -> None:
    binding = case.binding
    namespace_id = binding.run.namespace_id
    async with database.transaction() as writer:
        locks = TransitionLocks(writer)
        if change == "source":
            await locks.acquire_run(binding.run.run_id)
            assert (
                await update_claim_sources(
                    writer,
                    locks,
                    binding,
                    prepare_claim_sources(
                        {
                            "handle": {
                                "runtime": case.query.runtime,
                                "attempt_id": binding.attempt_id,
                                "execution_domain_id": case.query.execution_domain_id,
                                "host_hint": None,
                                "data": {"version": "new"},
                            },
                        },
                        {"handle": ALLOWANCE},
                    ),
                    POLL_LIMITS.snapshot.sources,
                    2,
                    case.clock.now(),
                )
                == 3
            )
        else:
            await locks.acquire_scopes(namespace_policy_scopes(namespace_id))
            await write_namespace_policy(
                writer,
                locks,
                NamespacePolicyConfiguration.prepare(
                    namespace_id, 3, {"require_protected_execution": False}
                ),
                case.clock.now(),
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["source", "policy", "scalars", "cancel"])
async def test_replay_final_native_snapshot_rechecks_current_state_and_closes(
    database: Database,
    evaluator: Evaluator,
    change: Literal["source", "policy", "scalars", "cancel"],
) -> None:
    case = await claim_case(database, evaluator)
    original = await configured_poller(database, WINNER_DEFAULT).poll(
        case.query, case.principal
    )
    assert original is not None
    replay_database = Database(database.settings)
    observer = NativeClaimTransactions(replay_database)
    observer.pause_final_snapshot = change != "cancel"
    observer.pause_final_read = change == "cancel"
    contender = configured_poller(replay_database, CONTENDER_DEFAULT)
    current_lease = case.clock.now() + timedelta(minutes=3)

    replayed = None
    pending = asyncio.create_task(contender.poll(case.query, case.principal))
    try:
        async with asyncio.timeout(15):
            await observer.final_snapshot_entered.wait()
            if change == "cancel":
                before = await state(database)
                assert (
                    observer.ledger.outstanding
                    == replay_database.units_in_flight()
                    == 1
                )
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
            elif change == "scalars":
                async with database.transaction() as writer:
                    locks = TransitionLocks(writer)
                    await locks.acquire_run(case.binding.run.run_id)
                    await writer.execute(
                        update(attempt)
                        .where(attempt.c.id == case.binding.attempt_id)
                        .values(
                            lease_until=current_lease, acknowledged_segment_offset=7
                        )
                    )
                    await writer.execute(
                        update(attempt_dispatcher)
                        .where(attempt_dispatcher.c.id == original.system.dispatcher_id)
                        .values(closed_at=case.clock.now())
                    )
                before = await state(database)
                observer.release_final_snapshot.set()
                replayed = await pending
            else:
                await revise_replay_sources(database, case, change)
                before = await state(database)
                observer.release_final_snapshot.set()
                with pytest.raises(
                    ClaimSourcesRejected, match="claim sources are unavailable"
                ):
                    await pending
        after = await state(database)
    finally:
        if not pending.done():
            pending.cancel()
        observer.release_final_snapshot.set()
        await asyncio.gather(pending, return_exceptions=True)
        await replay_database.close()

    assert before == after
    if change == "scalars":
        assert replayed is not None
        assert replayed.immutable.payload == original.immutable.payload
        assert replayed.policy.payload == original.policy.payload
        assert replayed.system.lease_until == current_lease
        assert replayed.system.acknowledged_segment_offset == 7
        assert replayed.system.current_grant == original.system.current_grant
    assert observer.snapshots == 2
    assert "write" not in observer.entries
    observer.assert_closed_and_read_only(writers=0)


@pytest.mark.asyncio
async def test_poll_commits_cursor_with_grant(
    database: Database, evaluator: Evaluator
) -> None:
    case = await claim_case(database, evaluator)
    poll = poller(database, AuthorityClock())

    delivery = await poll.poll(case.query, case.principal)
    empty = await poll.poll(
        case.query.model_copy(update={"claim_id": "full"}), case.principal
    )
    saved = await state(database)

    assert delivery is not None
    assert empty is None
    assert saved["worker"][0]["claim_cursor"] is None


@pytest.mark.asyncio
async def test_cursor_cas_loss_rolls_back_the_complete_tentative_grant(
    database: Database, evaluator: Evaluator
) -> None:
    case = await claim_case(database, evaluator)
    poll = poller(database, AuthorityClock())
    observed, release = asyncio.Event(), asyncio.Event()
    basis_writes: list[str] = []

    def pause_run_read(
        connection: object, cursor: object, statement: str, *arguments: object
    ) -> None:
        if "FROM run \n" in statement and not observed.is_set():
            observed.set()
            await_(release.wait())

    def observe_basis_write(
        connection: object, cursor: object, statement: str, *arguments: object
    ) -> None:
        if statement.startswith("INSERT INTO attempt_first_claim_basis"):
            basis_writes.append(statement)

    event.listen(
        database.reader_engine.sync_engine, "after_cursor_execute", pause_run_read
    )
    event.listen(
        database.writer_engine.sync_engine, "after_cursor_execute", observe_basis_write
    )

    try:
        async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:
            pending = tasks.create_task(poll.poll(case.query, case.principal))
            await observed.wait()
            async with database.transaction() as writer:
                await claim_worker(
                    writer, case.query, case.principal, WORKER_BYTES, lock=True
                )
                assert await advance_claim_cursor(
                    writer, case.query.worker_id, None, 0, None, case.clock.now()
                )
            before = await state(database)
            release.set()
            result = await pending
        after = await state(database)
    finally:
        event.remove(
            database.reader_engine.sync_engine, "after_cursor_execute", pause_run_read
        )
        event.remove(
            database.writer_engine.sync_engine,
            "after_cursor_execute",
            observe_basis_write,
        )

    assert result is None
    assert after == before
    assert len(basis_writes) == 1
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_poll_traverses_only_32_candidates_then_wraps(
    database: Database, evaluator: Evaluator
) -> None:
    case = await claim_case(database, evaluator)
    for _ in range(31):
        await another_run(database, case)
    async with database.transaction() as writer:
        await writer.execute(
            update(attempt).values(
                lease_until=case.clock.now() + timedelta(seconds=120)
            )
        )
    tail = await another_run(database, case)
    async with database.transaction() as writer:
        await writer.execute(
            update(attempt)
            .where(attempt.c.id == tail.binding.attempt_id)
            .values(lease_until=None)
        )
    poll = poller(database, AuthorityClock())

    first = await poll.poll(case.query, case.principal)
    traversed = await state(database)
    second = await poll.poll(case.query, case.principal)
    third = await poll.poll(
        case.query.model_copy(update={"claim_id": "wrap"}), case.principal
    )
    wrapped = await state(database)

    assert first is None
    assert traversed["worker"][0]["claim_cursor"] == traversed["attempt"][-2]["id"]
    assert len(traversed["attempt_dispatcher"]) == 0
    assert second is not None
    assert second.system.attempt_id == tail.binding.attempt_id
    assert third is None
    assert wrapped["worker"][0]["claim_cursor"] is None
    assert len(wrapped["attempt_dispatcher"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("maintenance", ["legacy", "history"])
async def test_poll_stops_after_terminal_maintenance_before_valid_tail(
    database: Database, evaluator: Evaluator, maintenance: str
) -> None:
    case = await claim_case(database, evaluator)
    tail = await another_run(database, case)
    await case.claim(database)
    clock = AuthorityClock()
    async with database.transaction() as writer:
        await writer.execute(update(worker).values(capacity=2))
        if maintenance == "legacy":
            await writer.execute(
                update(attempt)
                .where(attempt.c.id == case.binding.attempt_id)
                .values(current_dispatcher_open=None)
            )
        else:
            await writer.execute(
                update(attempt_dispatcher).values(
                    prefix_count=None,
                    prefix_digest=None,
                    claim_id=None,
                    claim_digest=None,
                )
            )
            await writer.execute(
                update(attempt)
                .where(attempt.c.id == case.binding.attempt_id)
                .values(**dict.fromkeys((*TIP_FIELDS, *ACK_FIELDS)))
            )
            clock.moment += timedelta(seconds=121)
    before = await state(database)
    poll = poller(database, clock)
    query = case.query.model_copy(update={"claim_id": "maintenance"})

    maintained = await poll.poll(query, case.principal)
    intermediate = await state(database)
    next_grant = await poll.poll(query, case.principal)

    assert maintained is None
    assert len(intermediate["attempt_dispatcher"]) == 1
    assert intermediate["event"] == before["event"]
    assert intermediate["attempt"][-1] == before["attempt"][-1]
    assert intermediate["worker"][0]["claim_cursor"] is None
    assert next_grant is not None
    assert next_grant.system.attempt_id == (
        tail.binding.attempt_id if maintenance == "legacy" else case.binding.attempt_id
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_source", [False, True])
async def test_poll_replays_concurrent_receipt_on_another_run_without_writes(
    database: Database, evaluator: Evaluator, bad_source: bool
) -> None:
    case = await claim_case(database, evaluator)
    other = await another_run(database, case)
    if bad_source:
        async with database.transaction() as writer:
            await writer.execute(
                delete(attempt_claim_read_admission).where(
                    attempt_claim_read_admission.c.attempt_id == case.binding.attempt_id
                )
            )
    poll = poller(database, AuthorityClock())
    observed, release = asyncio.Event(), asyncio.Event()

    def pause_run_read(
        connection: object, cursor: object, statement: str, *arguments: object
    ) -> None:
        if "FROM run \n" in statement and not observed.is_set():
            observed.set()
            await_(release.wait())

    event.listen(
        database.reader_engine.sync_engine, "after_cursor_execute", pause_run_read
    )

    try:
        async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:
            pending = tasks.create_task(poll.poll(case.query, case.principal))
            await observed.wait()
            async with database.transaction() as writer:
                await claim_worker(
                    writer, case.query, case.principal, WORKER_BYTES, lock=True
                )
                assert await advance_claim_cursor(
                    writer,
                    case.query.worker_id,
                    None,
                    0,
                    case.binding.attempt_id,
                    case.clock.now(),
                )
            winner = await poll.poll(case.query, case.principal)
            committed = await state(database)
            release.set()
            replayed = await pending
        after = await state(database)
    finally:
        event.remove(
            database.reader_engine.sync_engine, "after_cursor_execute", pause_run_read
        )

    assert winner is not None
    assert replayed is not None
    assert winner.system.attempt_id == other.binding.attempt_id
    assert replayed.encode() == winner.encode()
    assert committed == after
    assert database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
async def test_poll_finishes_expiry_observation_before_returning_without_a_grant(
    database: Database, evaluator: Evaluator
) -> None:
    ir = skeleton_ir()
    cast(JsonObject, ir["runtime"])["limits"] = {"timeout": "1s"}
    case = await claim_case(database, evaluator, ir)
    await admit_expiry_run(database, case, "1s")
    tail = await another_run(database, case)
    clock = AuthorityClock()
    clock.moment += timedelta(seconds=1)
    async with database.transaction() as writer:
        await writer.execute(
            update(run)
            .where(run.c.id == tail.binding.run.run_id)
            .values(started_at=clock.now())
        )
    poll = poller(database, clock)

    expired = await poll.poll(case.query, case.principal)
    after = await state(database)

    assert expired is None
    assert after["run"][0]["deadline_expired_at"] == clock.now()
    assert after["run"][-1]["deadline_expired_at"] is None
    assert after["attempt_dispatcher"] == []
    assert after["worker"][0]["claim_cursor"] is None
    assert database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["snapshot", "cursor"])
async def test_poll_cancellation_releases_sources_and_rolls_back_tentative_writes(
    database: Database, evaluator: Evaluator, stage: str
) -> None:
    case = await claim_case(database, evaluator)
    before = await state(database)
    poll = poller(database, AuthorityClock())
    observed, release = asyncio.Event(), asyncio.Event()
    engine = database.reader_engine if stage == "snapshot" else database.writer_engine
    basis_writes: list[str] = []

    def pause_native_operation(
        connection: object, cursor: object, statement: str, *arguments: object
    ) -> None:
        if statement.startswith("INSERT INTO attempt_first_claim_basis"):
            basis_writes.append(statement)
        matches = (
            "FROM run \n" in statement
            if stage == "snapshot"
            else statement.startswith("UPDATE worker SET")
            and "claim_cursor=" in statement
        )
        if matches and not observed.is_set():
            observed.set()
            await_(release.wait())

    event.listen(engine.sync_engine, "after_cursor_execute", pause_native_operation)

    pending = asyncio.create_task(poll.poll(case.query, case.principal))
    try:
        async with asyncio.timeout(10):
            await observed.wait()
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        after = await state(database)
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        event.remove(engine.sync_engine, "after_cursor_execute", pause_native_operation)

    assert before == after
    assert len(basis_writes) == int(stage == "cursor")
    assert database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "statement_marker",
    [
        "EXECUTION_STATE",
        "SOURCE_REVISION",
        "ATTEMPT_CLAIM_READ_ADMISSION",
        "TRANSACTION_READ_ONLY",
    ],
)
async def test_poll_keeps_cursor_when_grant_writer_fails(
    database: Database, evaluator: Evaluator, statement_marker: str
) -> None:
    case = await claim_case(database, evaluator)
    before = await state(database)
    poll = poller(database, AuthorityClock())
    if (
        statement_marker == "TRANSACTION_READ_ONLY"
        and database.dialect is not Dialect.POSTGRESQL
    ):
        delivery = await poll.poll(case.query, case.principal)
        saved = await state(database)

        assert delivery is not None
        assert saved["worker"][0]["claim_cursor"] == case.binding.attempt_id
        assert database.units_in_flight() == 0
        assert database.writer_is_free()
        return
    failed = False
    admission_reads = 0

    def fail_grant_write(
        connection: object, cursor: object, statement: str, *arguments: object
    ) -> None:
        nonlocal failed, admission_reads
        normalized = statement.lstrip().upper()
        admission = statement_marker == "ATTEMPT_CLAIM_READ_ADMISSION"
        matched = statement_marker in normalized and (
            admission
            or statement_marker == "TRANSACTION_READ_ONLY"
            or normalized.startswith("UPDATE")
        )
        if not matched or failed:
            return
        if admission:
            admission_reads += 1
            if database.dialect is Dialect.POSTGRESQL and admission_reads == 1:
                return
        failed = True
        raise OperationalError(statement, {}, Exception())

    event.listen(
        database.writer_engine.sync_engine,
        "before_cursor_execute",
        fail_grant_write,
    )
    try:
        with pytest.raises(ClaimSourcesRejected) as refusal:
            await poll.poll(case.query, case.principal)
        after = await state(database)
    finally:
        event.remove(
            database.writer_engine.sync_engine,
            "before_cursor_execute",
            fail_grant_write,
        )

    assert failed
    assert str(refusal.value) == "claim sources are unavailable"
    assert refusal.value.__cause__ is refusal.value.__context__ is None
    assert case.binding.attempt_id not in str(refusal.value)
    assert after == before
    assert database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
async def test_poll_advances_past_an_unrepresentable_saved_timeout(
    database: Database, evaluator: Evaluator
) -> None:
    ir = skeleton_ir()
    agent = cast(
        JsonObject, cast(JsonObject, cast(JsonObject, ir["graph"])["nodes"])["agent"]
    )
    agent["limits"] = {"timeout": "999999999999d"}
    case = await claim_case(database, evaluator, ir)
    before = await state(database)
    poll = poller(database, AuthorityClock())

    with pytest.raises(ClaimSourcesRejected) as refusal:
        await poll.poll(case.query, case.principal)
    advanced = await state(database)
    wrapped = await poll.poll(
        case.query.model_copy(update={"claim_id": "after-timeout"}), case.principal
    )
    after = await state(database)

    assert str(refusal.value) == "claim sources are unavailable"
    assert case.binding.attempt_id not in str(refusal.value)
    assert advanced["worker"][0]["claim_cursor"] == case.binding.attempt_id
    assert advanced["worker"][0]["claim_cursor_generation"] == 1
    assert advanced["attempt_dispatcher"] == before["attempt_dispatcher"]
    assert wrapped is None
    assert after["worker"][0]["claim_cursor"] is None
    assert database.units_in_flight() == 0
    assert database.writer_is_free()
