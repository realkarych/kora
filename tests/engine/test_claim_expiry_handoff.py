import asyncio
from collections.abc import AsyncGenerator, Callable, Generator
from contextlib import ExitStack, asynccontextmanager, closing, contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from time import monotonic
from typing import cast

import pytest
from sqlalchemy import Connection, Engine, event, insert, select, update
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.util.concurrency import await_
from starlette.types import ASGIApp, Message

from kora.api.application import create_application
from kora.api.worker_claims_route import WorkerClaimsRoute
from kora.cel_eval.thread import Evaluator
from kora.conformance.capture import captured
from kora.db.claim_source_values import ClaimSourcesRejected, prepare_claim_sources
from kora.db.claim_sources_v1 import SOURCE_FIELDS
from kora.db.connection import POSTGRESQL_WAL_SETTINGS_QUERY, Database, Dialect
from kora.db.tables import attempt, node_instance, run, wait
from kora.engine.attempt_inputs import prepare_attempt_inputs
from kora.engine.claim_context import ClaimQuery
from kora.engine.claim_sources import insert_claim_attempt
from kora.engine.expiry import record_run_expiry
from kora.engine.expiry_context import ExpiryFailed
from kora.engine.locks import COMPONENT, TransitionLocks
from kora.engine.worker_registration import register_worker
from kora.ir.json import JsonObject
from kora.observability import Level, LoggingSettings
from kora.plugins.authentication.static_token import StaticTokenAuthenticator
from kora.protocols.authentication import Principal
from tests.api.claim_support import admit_expiry_run, poller
from tests.api.deployment_cases import evaluator, skeleton_ir
from tests.api.support import RequestIds, lifespan
from tests.db.closing import (
    CONDITION_DEADLINE_SECONDS,
    ConnectionLedger,
    narrow_database,
    narrow_settings,
)
from tests.db.seed import START, Seeder
from tests.db.test_sqlite_writer_entry import writer_driver
from tests.engine.claim_support import SOURCE_LIMITS, ClaimCase, claim_case, state
from tests.engine.heartbeat_support import heartbeat, heartbeat_body
from tests.engine.test_claim_sources import ALLOWANCE, source_values
from tests.plugins.authentication.support import AuthorityClock

__all__ = ["evaluator"]


def expiry_ir(*, enclosing: bool) -> JsonObject:
    ir = skeleton_ir()
    graph = cast(JsonObject, ir["graph"])
    nodes = cast(JsonObject, graph["nodes"])
    if enclosing:
        cast(JsonObject, ir["runtime"])["limits"] = {"timeout": "1s"}
    else:
        cast(JsonObject, nodes["agent"])["limits"] = {"timeout": "1h"}
        cast(JsonObject, ir["runtime"])["limits"] = {"timeout": "2h"}
    return ir


async def add_sibling(database: Database, case: ClaimCase) -> str:
    run_id = case.binding.run.run_id
    async with database.transaction() as writer:
        locks = TransitionLocks(writer)
        await locks.acquire_run(run_id)
        seed = Seeder(writer)
        node_id = await seed.node(run_id, "gate", status="queued")
        attempt_id = seed.new_id()
        original = (
            (
                await writer.execute(
                    select(attempt).where(attempt.c.id == case.binding.attempt_id)
                )
            )
            .mappings()
            .one()
        )
        values = {key: original[key] for key in original if key not in SOURCE_FIELDS}
        values.update(id=attempt_id, node_instance_id=node_id)
        values.pop("acknowledged_segment_offset")
        binding = replace(
            case.binding,
            node_id=node_id,
            attempt_id=attempt_id,
            node_path="gate",
        )
        sources = source_values()
        sources["input_bindings"] = prepare_attempt_inputs({}, {}).storage
        await insert_claim_attempt(
            writer,
            locks,
            binding,
            values,
            prepare_claim_sources(sources, {name: ALLOWANCE for name in sources}),
            SOURCE_LIMITS,
            START,
        )
        await writer.execute(
            update(node_instance)
            .where(node_instance.c.id == node_id)
            .values(current_attempt_id=attempt_id)
        )
    return attempt_id


async def contender(
    database: Database, case: ClaimCase
) -> tuple[ClaimQuery, Principal]:
    identity = "contending-worker"
    domain = "contending-domain"
    async with database.transaction() as writer:
        seed = Seeder(writer)
        token_id = await seed.token("e" * 64, kind="worker", owner=identity)
        principal = case.principal.model_copy(
            update={"owner": identity, "token_id": token_id}
        )
        await register_worker(
            writer,
            replace(
                case.registration,
                name=identity,
                execution_domain_id=domain,
                process=case.registration.process.model_copy(update={"pid": 321}),
            ),
            principal,
            portable=False,
            now=START,
            event_id=seed.new_id(),
        )
    return (
        case.query.model_copy(
            update={
                "worker_id": identity,
                "execution_domain_id": domain,
                "claim_id": "contender",
            }
        ),
        principal,
    )


async def hold_target(database: Database, case: ClaimCase) -> None:
    async with database.transaction() as writer:
        await writer.execute(
            update(run)
            .where(run.c.id == case.binding.run.run_id)
            .values(
                needs_scheduling=False,
                target_key="held",
                target_held_since=START,
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("admitted_timeout", [None, "2h"])
async def test_claim_run_expiry_requires_admitted_due_parent(
    database: Database, evaluator: Evaluator, admitted_timeout: str | None
) -> None:
    case = await claim_case(database, evaluator, expiry_ir(enclosing=True))
    if admitted_timeout is not None:
        await admit_expiry_run(database, case, admitted_timeout)
    await hold_target(database, case)
    before = await state(database)
    poll = poller(database, AuthorityClock(START + timedelta(seconds=1)))

    with pytest.raises(ClaimSourcesRejected, match="claim sources are unavailable"):
        await poll.poll(case.query, case.principal)
    after = await state(database)

    assert after == before
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_claim_attempt_expiry_survives_rollback_before_worker_commit(
    database: Database, evaluator: Evaluator
) -> None:
    case = await claim_case(database, evaluator, expiry_ir(enclosing=False))
    await admit_expiry_run(database, case, "2h")
    first = await case.claim(database)
    assert first.delivery is not None
    await hold_target(database, case)
    cutoff = START + timedelta(hours=1)
    clock = AuthorityClock(cutoff)
    observed = False

    def roll_back(
        connection: object, cursor: object, statement: str, *arguments: object
    ) -> None:
        nonlocal observed
        if statement.startswith("UPDATE worker SET") and "last_seen_at=" in statement:
            clock.moment = cutoff - timedelta(seconds=1)
            observed = True

    engine = database.writer_engine.sync_engine
    event.listen(engine, "after_cursor_execute", roll_back)
    poll = poller(database, clock)
    fresh = case.query.model_copy(update={"claim_id": "expiry"})

    try:
        expired = await poll.poll(fresh, case.principal)
        after = await state(database)
    finally:
        event.remove(engine, "after_cursor_execute", roll_back)
    recovered = await poll.poll(fresh, case.principal)

    assert observed
    assert expired is None
    assert after["attempt"][0]["deadline_expired_at"] == cutoff
    assert after["attempt"][0]["desired_state"] == "cancel"
    assert after["attempt"][0]["cancel_cause"] == "timeout"
    assert after["attempt"][0]["recovery_required"] is True
    assert after["attempt"][0]["lease_epoch"] == first.delivery.system.lease_epoch
    assert after["run"][0]["deadline_expired_at"] is None
    assert after["run"][0]["needs_scheduling"] is True
    assert after["run"][0]["target_held_since"] == START
    assert recovered is not None
    assert recovered.system.mode == "recover"
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("retained_row", ["unadmitted-parent", "terminal-attempt"])
async def test_claim_does_not_latch_expiry_on_ineligible_retained_attempt(
    database: Database, evaluator: Evaluator, retained_row: str
) -> None:
    case = await claim_case(database, evaluator, expiry_ir(enclosing=False))
    if retained_row == "terminal-attempt":
        await admit_expiry_run(database, case, "2h")
    first = await case.claim(database)
    assert first.delivery is not None
    if retained_row == "terminal-attempt":
        async with database.transaction() as writer:
            await writer.execute(
                update(attempt)
                .where(attempt.c.id == case.binding.attempt_id)
                .values(status="failed", recovery_required=True)
            )
    await hold_target(database, case)
    before = await state(database)
    poll = poller(database, AuthorityClock(START + timedelta(hours=1)))
    fresh = case.query.model_copy(update={"claim_id": "retained-attempt-expiry"})

    result = await poll.poll(fresh, case.principal)
    after = await state(database)

    assert result is None
    assert after["attempt"] == before["attempt"]
    assert after["attempt_dispatcher"] == before["attempt_dispatcher"]
    assert after["run"] == before["run"]
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_claim_attempt_expiry_respects_saved_parent_timeout_precedence(
    database: Database, evaluator: Evaluator
) -> None:
    case = await claim_case(database, evaluator, expiry_ir(enclosing=False))
    await admit_expiry_run(database, case, "30m")
    first = await case.claim(database)
    assert first.delivery is not None
    cutoff = START + timedelta(hours=1)
    poll = poller(database, AuthorityClock(cutoff))
    fresh = case.query.model_copy(update={"claim_id": "enclosing-expiry"})

    result = await poll.poll(fresh, case.principal)
    after = await state(database)

    assert result is None
    assert after["run"][0]["deadline_expired_at"] == cutoff
    assert after["attempt"][0]["deadline_expired_at"] is None
    assert after["attempt"][0]["desired_state"] == "cancel"
    assert after["attempt"][0]["updated_at"] == cutoff
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("commit_succeeds", [False, True], ids=["rollback", "commit"])
async def test_claim_cancellation_during_first_commit_settles_run_expiry(
    database: Database, evaluator: Evaluator, commit_succeeds: bool
) -> None:
    case = await claim_case(database, evaluator, expiry_ir(enclosing=True))
    await admit_expiry_run(database, case, "1s")
    await hold_target(database, case)
    before = await state(database)
    cutoff = START + timedelta(seconds=1)
    clock = AuthorityClock(cutoff)
    poll = poller(database, clock)
    engine = database.writer_engine.sync_engine
    entered = asyncio.Event()
    release = asyncio.Event()
    cancellation_turn = asyncio.Event()
    intent_connection: object | None = None

    def record_intent(
        connection: object, cursor: object, statement: str, *arguments: object
    ) -> None:
        nonlocal intent_connection
        if (
            statement.startswith("UPDATE run SET")
            and "deadline_expired_at=" in statement
        ):
            intent_connection = connection

    def hold_commit(connection: Connection) -> None:
        if connection is intent_connection and not entered.is_set():
            clock.moment = START
            entered.set()
            await_(release.wait())
            if not commit_succeeds:
                connection.exec_driver_sql("SELECT missing_claim_expiry_probe")

    event.listen(engine, "after_cursor_execute", record_intent)
    event.listen(engine, "commit", hold_commit)

    try:
        with captured(
            COMPONENT, LoggingSettings(level=Level.DEBUG), "claim-run-expiry", clock.now
        ) as capture:
            async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:
                original = tasks.create_task(poll.poll(case.query, case.principal))
                try:
                    await entered.wait()
                    original.cancel("cancel at Claim commit")
                    asyncio.get_running_loop().call_soon(cancellation_turn.set)
                    await cancellation_turn.wait()
                    still_settling = not original.done()
                    during_commit = await state(database)
                finally:
                    release.set()
                with pytest.raises(
                    asyncio.CancelledError, match="cancel at Claim commit"
                ):
                    await original
            records = capture.finish()
        after = await state(database)
    finally:
        event.remove(engine, "after_cursor_execute", record_intent)
        event.remove(engine, "commit", hold_commit)

    assert still_settling
    assert during_commit == before
    assert [
        row["action"] for row in records if row["event"] == "engine.expiry.prepared"
    ] == ["run_expiry_intent", "run_expiry_cancellation"]
    assert case.binding.run.run_id not in repr(records)
    assert case.binding.attempt_id not in repr(records)
    assert "missing_claim_expiry_probe" not in repr(records)
    assert database.units_in_flight() == 0
    if not commit_succeeds:
        assert after == before
        return
    assert after["run"][0]["deadline_expired_at"] == cutoff
    assert after["run"][0]["needs_scheduling"] is True
    assert after["run"][0]["target_held_since"] == START
    assert after["run"][0]["status"] == "running"
    assert after["attempt"][0]["desired_state"] == "cancel"
    assert after["attempt"][0]["cancel_cause"] == "timeout"
    assert after["attempt"][0]["updated_at"] == cutoff
    assert after["attempt_dispatcher"] == before["attempt_dispatcher"]


@dataclass
class HeldExpiryContinuation:
    database: Database
    clock: AuthorityClock
    rolled_back: datetime
    refuse: bool = False
    sqlite_transactions: int = 0
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    def wait_before_lock(self) -> None:
        self.clock.moment = self.rolled_back
        self.entered.set()
        await_(self.release.wait())

    def before_checkout(
        self, connection: DBAPIConnection, record: object, proxy: object
    ) -> None:
        self.sqlite_transactions += 1
        if self.sqlite_transactions == 2:
            self.wait_before_lock()
            if self.refuse:
                with closing(connection.cursor()) as cursor:
                    cursor.execute("SELECT missing_claim_expiry_probe")

    def before_statement(
        self, connection: Connection, cursor: object, statement: str, *arguments: object
    ) -> None:
        if statement.startswith("SELECT pg_advisory_xact_lock("):
            self.wait_before_lock()
            if self.refuse:
                connection.exec_driver_sql("SELECT missing_claim_expiry_probe")

    @contextmanager
    def installed(self) -> Generator[None]:
        engine = self.database.writer_engine.sync_engine
        if self.database.dialect is Dialect.SQLITE:
            name, listener = "checkout", self.before_checkout
        else:
            name, listener = "before_cursor_execute", self.before_statement
        event.listen(engine, name, listener)
        try:
            yield
        finally:
            event.remove(engine, name, listener)


async def run_lifespan(application: ASGIApp, messages: list[Message]) -> None:
    incoming: asyncio.Queue[Message] = asyncio.Queue()
    await incoming.put({"type": "lifespan.startup"})
    await incoming.put({"type": "lifespan.shutdown"})

    async def send(message: Message) -> None:
        messages.append(message)

    await application(
        {"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}},
        incoming.get,
        send,
    )


async def assert_startup_refuses(
    application: ASGIApp, database: Database, private_reason: str
) -> None:
    messages: list[Message] = []

    async with asyncio.timeout(CONDITION_DEADLINE_SECONDS):
        with pytest.raises(ExpiryFailed) as refusal:
            await run_lifespan(application, messages)

    assert [message["type"] for message in messages] == ["lifespan.startup.failed"]
    assert private_reason not in repr(messages)
    assert str(refusal.value) == "execution authority expiry failed"
    assert refusal.value.__cause__ is None
    assert refusal.value.__context__ is None
    assert database.units_in_flight() == 0


async def refuse_native_startup_read(application: ASGIApp, database: Database) -> None:
    engine = database.reader_engine.sync_engine

    def fail_discovery(
        connection: Connection, cursor: object, statement: str, *arguments: object
    ) -> None:
        if statement.startswith("SELECT max(run.id)"):
            connection.exec_driver_sql("SELECT missing_startup_expiry_probe")

    event.listen(engine, "before_cursor_execute", fail_discovery)
    try:
        await assert_startup_refuses(
            application, database, "missing_startup_expiry_probe"
        )
    finally:
        event.remove(engine, "before_cursor_execute", fail_discovery)


async def refuse_native_startup_writer(
    application: ASGIApp, database: Database
) -> None:
    if database.dialect is Dialect.SQLITE:
        async with database.writer_engine.connect() as connection:
            driver = await writer_driver(connection)
            async with driver.execute("PRAGMA main.synchronous=NORMAL"):
                pass
        try:
            await assert_startup_refuses(
                application, database, "requires FULL synchronization"
            )
        finally:
            async with database.writer_engine.connect() as connection:
                assert await writer_driver(connection) is driver
                async with driver.execute("PRAGMA main.synchronous=FULL"):
                    pass
        assert database.writer_is_free()
        return

    engine = database.writer_engine.sync_engine
    checks = 0

    def weaken_durability_at_commit(
        connection: Connection, cursor: object, statement: str, *arguments: object
    ) -> None:
        nonlocal checks
        if statement == POSTGRESQL_WAL_SETTINGS_QUERY.text:
            checks += 1
            if checks == 2:
                connection.exec_driver_sql("SET LOCAL synchronous_commit TO off")

    event.listen(engine, "before_cursor_execute", weaken_durability_at_commit)
    try:
        await assert_startup_refuses(
            application, database, "requires durable WAL settings"
        )
    finally:
        event.remove(engine, "before_cursor_execute", weaken_durability_at_commit)

    assert checks == 2


async def expiry_wait_state(database: Database, wait_id: str) -> tuple[object, ...]:
    async with database.read_transaction() as reader:
        with await reader.execute(
            select(wait.c.status, wait.c.updated_at).where(wait.c.id == wait_id)
        ) as result:
            return tuple(result.one())


async def open_expiry_wait(database: Database, run_id: str) -> str:
    async with database.transaction() as writer:
        seed = Seeder(writer)
        wait_node = await seed.node(run_id, "approve", status="waiting")
        wait_id = seed.new_id()
        await writer.execute(
            insert(wait).values(
                id=wait_id,
                node_instance_id=wait_node,
                kind="approval",
                status="open",
                **seed.stamps(),
            )
        )
    return wait_id


async def latch_legacy_wait(
    database: Database, case: ClaimCase, cutoff: datetime
) -> tuple[str, str]:
    binding = case.binding.run
    inline = {"retained": "private-legacy-input-" + "x" * 8192}
    async with database.transaction() as writer:
        seed = Seeder.at(writer, START - timedelta(hours=1))
        values = seed.run_values(
            binding.namespace_id, binding.graph_id, binding.version_id
        )
        run_id = str(values["id"])
        await writer.execute(
            insert(run).values(
                values
                | {
                    "input_encoding_version": None,
                    "params": inline,
                    "cel_params_projection": inline,
                    "started_at": START,
                }
            )
        )
    legacy = replace(
        case, binding=replace(case.binding, run=replace(binding, run_id=run_id))
    )
    await admit_expiry_run(database, legacy, "1s")
    wait_id = await open_expiry_wait(database, run_id)
    async with database.transaction() as writer:
        await record_run_expiry(await legacy.context(writer), cutoff)
    return run_id, wait_id


@dataclass
class HandoffWriterTimes:
    now: Callable[[], float]
    driver: DBAPIConnection | None = None
    events: list[tuple[str, float]] = field(default_factory=list[tuple[str, float]])
    overflow: bool = False

    def record(self, label: str) -> None:
        if len(self.events) < 12:
            self.events.append((label, self.now()))
        else:
            self.overflow = True

    def checkout(self, driver: DBAPIConnection, *arguments: object) -> None:
        self.driver = driver
        self.record("checkout")

    def begin_return(self, connection: Connection) -> None:
        paired = (
            self.driver is not None
            and connection.connection.dbapi_connection is self.driver
        )
        self.record("raw_begin_return" if paired else "unpaired_begin_return")

    def checkin(self, driver: DBAPIConnection | None, *arguments: object) -> None:
        self.driver = None

    def install(self, stack: ExitStack, engine: Engine) -> None:
        stack.callback(self.checkin, None)
        for event_name, observer in (
            ("checkout", self.checkout),
            ("begin", self.begin_return),
            ("checkin", self.checkin),
        ):
            event.listen(engine, event_name, observer)
            stack.callback(event.remove, engine, event_name, observer)

    def note(self, deadline: float) -> str:
        items = " ".join(
            f"{name}={round((deadline - at) * 1000)}" for name, at in self.events
        )
        return f"{items} overflow={self.overflow}"


class HandoffTimes:
    def __init__(self) -> None:
        self.loop_offset: float = asyncio.get_running_loop().time() - monotonic()
        self.phases: dict[str, float] = {}
        self.original: HandoffWriterTimes = HandoffWriterTimes(self.now)
        self.secondary: HandoffWriterTimes = HandoffWriterTimes(self.now)
        self.sqlite: bool = False

    def now(self) -> float:
        return monotonic() + self.loop_offset

    def mark(self, phase: str) -> None:
        self.phases[phase] = self.now()

    @contextmanager
    def writers(self, original: Database, secondary: Database) -> Generator[None]:
        with ExitStack() as stack:
            if original.dialect is Dialect.SQLITE:
                self.sqlite = True
                self.original.install(stack, original.writer_engine.sync_engine)
                self.secondary.install(stack, secondary.writer_engine.sync_engine)
            yield

    @asynccontextmanager
    async def watchdog(self) -> AsyncGenerator[None]:
        timer = asyncio.timeout(15)
        nested_timeout = False
        try:
            async with timer:
                self.mark("entered")
                try:
                    yield
                except TimeoutError:
                    nested_timeout = True
                    raise
        except TimeoutError as error:
            deadline = timer.when()
            if not nested_timeout and timer.expired() and deadline is not None:
                error.add_note(self.note(deadline))
            raise

    def note(self, deadline: float) -> str:
        phases = " ".join(
            f"{name}={round((deadline - at) * 1000)}"
            for name, at in self.phases.items()
        )
        rows = [f"Claim handoff remaining milliseconds: {phases}"]
        if self.sqlite:
            rows += [
                "SQLite native begin start unobserved.",
                f"Original writer: {self.original.note(deadline)}",
                f"Secondary writer: {self.secondary.note(deadline)}",
            ]
        return "\n".join(rows)[:1024]


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupted", [False, True])
async def test_run_latch_denies_competing_claim_and_live_heartbeat_before_cascade(
    database: Database, evaluator: Evaluator, interrupted: bool
) -> None:
    case = await claim_case(database, evaluator, expiry_ir(enclosing=True))
    await admit_expiry_run(database, case, "1s")
    sibling = await add_sibling(database, case)
    clock = AuthorityClock()
    first_poll = poller(database, clock)
    delivery = await first_poll.poll(case.query, case.principal)
    assert delivery is not None
    query, principal = await contender(database, case)
    other = narrow_database(database)
    cutoff = START + timedelta(seconds=1)
    clock.moment = cutoff
    hold = HeldExpiryContinuation(database, clock, START)
    concurrent_poll = poller(other, clock)
    fresh = case.query.model_copy(update={"claim_id": "expiry"})
    times = HandoffTimes()

    try:
        await hold_target(other, case)
        with times.writers(database, other), hold.installed():
            async with times.watchdog(), asyncio.TaskGroup() as tasks:
                original = tasks.create_task(first_poll.poll(fresh, case.principal))
                try:
                    await hold.entered.wait()
                    times.mark("held")
                    pending = await state(other)
                    times.mark("pending_snapshot_return")
                    replayed = await concurrent_poll.poll(case.query, case.principal)
                    times.mark("replay_return")
                    after_replay = await state(other)
                    times.mark("replay_snapshot_return")
                    beat = await heartbeat(
                        other, case, clock, heartbeat_body(delivery.system)
                    )
                    times.mark("heartbeat_return")
                    times.mark("rival_entry")
                    rival = await concurrent_poll.poll(query, principal)
                    times.mark("rival_return")
                    if interrupted:
                        original.cancel()
                finally:
                    hold.release.set()
                    times.mark("release")
                if interrupted:
                    with pytest.raises(asyncio.CancelledError):
                        await original
                    result = None
                else:
                    result = await original
                times.mark("continuation_settled")
        final = await state(database)
    finally:
        await other.close()

    assert result is None
    assert rival is None
    assert replayed is not None
    assert replayed.encode() == delivery.encode()
    assert after_replay == pending
    assert pending["run"][0]["deadline_expired_at"] == cutoff
    assert pending["run"][0]["needs_scheduling"] is True
    assert {row["desired_state"] for row in pending["attempt"]} == {"run"}
    assert beat.mode == "recover"
    assert beat.desired_state == "cancel"
    assert beat.permitted_actions == ("observe", "retire")
    assert {row["id"] for row in final["attempt"]} == {case.binding.attempt_id, sibling}
    assert {row["cancel_cause"] for row in final["attempt"]} == {"timeout"}
    assert {row["updated_at"] for row in final["attempt"]} == {cutoff}
    assert [row["kind"] for row in final["event"]].count(
        "attempt.recovery_required"
    ) == 1
    assert len(final["attempt_dispatcher"]) == 1
    assert final["run"][0]["target_held_since"] == START
    assert final["run"][0]["status"] == "running"
    assert database.units_in_flight() == other.units_in_flight() == 0


@pytest.mark.asyncio
async def test_claim_startup_resumes_interrupted_expiry_without_worker_traffic(
    database: Database, evaluator: Evaluator
) -> None:
    case = await claim_case(database, evaluator, expiry_ir(enclosing=True))
    await admit_expiry_run(database, case, "1s")
    sibling = await add_sibling(database, case)
    await case.claim(database)
    await hold_target(database, case)
    wait_id = await open_expiry_wait(database, case.binding.run.run_id)
    cutoff = START + timedelta(seconds=1)
    clock = AuthorityClock(cutoff)
    hold = HeldExpiryContinuation(database, clock, START, refuse=True)
    hold.release.set()
    poll = poller(database, clock)
    fresh = case.query.model_copy(update={"claim_id": "interrupted"})

    with hold.installed(), pytest.raises(ClaimSourcesRejected):
        await poll.poll(fresh, case.principal)
    async with database.transaction() as writer:
        await writer.execute(
            update(attempt)
            .where(attempt.c.id == sibling)
            .values(status="cancelled", desired_state="cancel", cancel_cause="cancel")
        )
        scope = case.binding.run
        seed = Seeder.at(writer, START - timedelta(days=1))
        earlier_runs = [
            {
                **seed.run_values(scope.namespace_id, scope.graph_id, scope.version_id),
                "started_at": START - timedelta(seconds=2),
                "limits": {"timeout": "1s"},
            }
            for _ in range(100)
        ]
        await writer.execute(insert(run), earlier_runs)
    legacy_run, legacy_wait = await latch_legacy_wait(database, case, cutoff)
    pending = await state(database)
    recovered_database = Database(
        narrow_settings(database.settings),
        require_sqlite_full_sync=database.dialect is Dialect.SQLITE,
        require_postgresql_wal_settings=database.dialect is Dialect.POSTGRESQL,
    )
    try:
        with captured(
            "api", LoggingSettings(level=Level.DEBUG), "expiry-startup", clock.now
        ) as cap:
            authenticator = StaticTokenAuthenticator({}, recovered_database)
            bare_application = create_application(
                authenticator, cap.diagnostics, clock, RequestIds()
            )
            application = create_application(
                authenticator,
                cap.diagnostics,
                clock,
                RequestIds(),
                worker_claims=WorkerClaimsRoute(poller(recovered_database, clock)),
            )
            async with lifespan(bare_application):
                before_claim_startup = await state(recovered_database)
            await refuse_native_startup_read(application, recovered_database)
            after_read_refusal = await state(recovered_database)
            await refuse_native_startup_writer(application, recovered_database)
            after_writer_refusal = await state(recovered_database)
            wait_after_writer_refusal = await expiry_wait_state(
                recovered_database, wait_id
            )
            async with lifespan(application):
                final = await state(recovered_database)
                stopped_wait = await expiry_wait_state(recovered_database, wait_id)
                stopped_legacy_wait = await expiry_wait_state(
                    recovered_database, legacy_wait
                )
            async with lifespan(application):
                repeated = await state(recovered_database)
                repeated_legacy_wait = await expiry_wait_state(
                    recovered_database, legacy_wait
                )
            records = cap.finish()
    finally:
        await recovered_database.close()

    assert hold.entered.is_set()
    assert len(pending["run"]) == 102
    assert pending["run"][-2]["id"] == legacy_run
    assert pending["run"][-2]["input_encoding_version"] is None
    assert pending["run"][-2]["deadline_expired_at"] == cutoff
    assert pending["run"][-1]["id"] == case.binding.run.run_id
    assert pending["run"][-1]["deadline_expired_at"] == cutoff
    assert pending["run"][-1]["target_held_since"] == START
    lease_until = pending["attempt"][0]["lease_until"]
    assert isinstance(lease_until, datetime)
    assert lease_until > clock.now()
    assert pending["attempt"][0]["desired_state"] == "run"
    assert before_claim_startup == after_read_refusal == after_writer_refusal == pending
    assert wait_after_writer_refusal == ("open", START)
    assert final["run"] == pending["run"]
    assert final["attempt"][0]["desired_state"] == "cancel"
    assert final["attempt"][0]["cancel_cause"] == "timeout"
    assert final["attempt"][0]["updated_at"] == cutoff
    assert final["attempt"][1] == pending["attempt"][1]
    assert stopped_wait == ("cancelled", cutoff)
    assert stopped_legacy_wait == repeated_legacy_wait == ("cancelled", cutoff)
    assert final["attempt_dispatcher"] == pending["attempt_dispatcher"]
    assert repeated == final
    assert [
        row["event"]
        for row in records
        if str(row["event"]).startswith("engine.claim_expiry_recovery.")
    ] == [
        "engine.claim_expiry_recovery.refused",
        "engine.claim_expiry_recovery.refused",
        "engine.claim_expiry_recovery.finished",
        "engine.claim_expiry_recovery.finished",
    ]
    assert case.binding.run.run_id not in repr(records)
    assert case.binding.attempt_id not in repr(records)
    assert "private-legacy-input-" not in repr(records)
    assert "missing_startup_expiry_probe" not in repr(records)
    assert "requires FULL synchronization" not in repr(records)
    assert "requires durable WAL settings" not in repr(records)
    assert recovered_database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_cancelled_claim_startup_releases_writer_and_replays_latch(
    database: Database, evaluator: Evaluator
) -> None:
    case = await claim_case(database, evaluator, expiry_ir(enclosing=True))
    run_id = case.binding.run.run_id
    await admit_expiry_run(database, case, "1s")
    await case.claim(database)
    await hold_target(database, case)
    wait_id = await open_expiry_wait(database, run_id)
    cutoff = START + timedelta(seconds=1)
    async with database.transaction() as writer:
        await record_run_expiry(await case.context(writer), cutoff)
    pending = await state(database)
    recovered_database = narrow_database(database)
    ledger = ConnectionLedger(recovered_database)
    engine = recovered_database.writer_engine.sync_engine
    clock = AuthorityClock(START)
    entered, release = asyncio.Event(), asyncio.Event()
    messages: list[Message] = []

    def hold_recovery_write(
        connection: Connection, cursor: object, statement: str, *arguments: object
    ) -> None:
        if statement.startswith("UPDATE attempt SET") and not entered.is_set():
            entered.set()
            await_(release.wait())

    try:
        with captured(
            "api",
            LoggingSettings(level=Level.DEBUG),
            "cancel-expiry-startup",
            clock.now,
        ) as cap:
            application = create_application(
                StaticTokenAuthenticator({}, recovered_database),
                cap.diagnostics,
                clock,
                RequestIds(),
                worker_claims=WorkerClaimsRoute(poller(recovered_database, clock)),
            )
            event.listen(engine, "after_cursor_execute", hold_recovery_write)
            try:
                async with (
                    asyncio.timeout(CONDITION_DEADLINE_SECONDS),
                    asyncio.TaskGroup() as tasks,
                ):
                    startup = tasks.create_task(run_lifespan(application, messages))
                    try:
                        await entered.wait()
                        live_writer = (
                            ledger.outstanding,
                            recovered_database.units_in_flight(),
                        )
                        startup.cancel("cancel Claim startup")
                    finally:
                        release.set()
                    with pytest.raises(
                        asyncio.CancelledError, match="cancel Claim startup"
                    ):
                        await startup
            finally:
                event.remove(engine, "after_cursor_execute", hold_recovery_write)
            settled_writer = (ledger.outstanding, recovered_database.units_in_flight())
            rolled_back = await state(database)
            open_wait = await expiry_wait_state(database, wait_id)
            async with asyncio.timeout(CONDITION_DEADLINE_SECONDS):
                async with database.transaction() as writer:
                    reacquired = await TransitionLocks(writer).acquire_run(
                        run_id, wait=False
                    )
            async with lifespan(application):
                recovered = await state(recovered_database)
                stopped_wait = await expiry_wait_state(recovered_database, wait_id)
            records = cap.finish()
    finally:
        await recovered_database.close()

    assert [message["type"] for message in messages] == ["lifespan.startup.failed"]
    assert live_writer == (1, 1)
    assert settled_writer == (0, 0)
    assert reacquired
    assert rolled_back == pending
    assert open_wait == ("open", START)
    assert recovered["run"] == pending["run"]
    assert recovered["run"][0]["deadline_expired_at"] == cutoff
    assert recovered["run"][0]["target_held_since"] == START
    assert recovered["attempt"][0]["desired_state"] == "cancel"
    assert recovered["attempt"][0]["updated_at"] == cutoff
    assert stopped_wait == ("cancelled", cutoff)
    assert [
        row["event"]
        for row in records
        if str(row["event"]).startswith("engine.claim_expiry_recovery.")
    ] == ["engine.claim_expiry_recovery.finished"]
    assert ledger.outstanding == recovered_database.units_in_flight() == 0
