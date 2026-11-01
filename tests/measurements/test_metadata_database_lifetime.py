from __future__ import annotations

import asyncio
import gc
import os
import platform
import sqlite3
import threading
import time
import tracemalloc
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

import pytest
from sqlalchemy import (
    Connection,
    event,
    select,
    text,
)

from kora.db.connection import (
    Database,
    DatabaseSettings,
)
from kora.db.identifiers import IdentifierGenerator
from kora.db.migrations import upgrade_database
from kora.db.reads import (
    MarkedRunSweep,
    events_of_run,
    has_unfinished_top_level_instance,
)
from kora.db.tables import (
    namespace,
)
from tests.db.closing import (
    CLOSED_BEFORE_FINISHING,
    ConnectionLedger,
    failure_messages,
    hold_reader,
    hold_writer,
    narrow_settings,
    reached,
    sqlite_companions,
    until,
    write,
)
from tests.db.seed import Seeder
from tests.measurements.harness import (
    Report,
    Sample,
    current_rss_bytes,
    report_lifecycle,
    settled_counts,
    timed,
)
from tests.measurements.metadata_lifetime_support import (
    CheckoutBalance,
    DatabaseFactory,
    StatementWatch,
    gate,
    record,
    slow_statement,
)
from tests.support.databases import (
    POSTGRES_DIGEST,
    POSTGRES_TAG,
    DatabaseKind,
    PostgresServer,
    selected_databases,
)

pytestmark = pytest.mark.measurement

STEP_BUDGET_SECONDS: float = 120.0
MIGRATION_CYCLES: int = 20
CONNECT_CYCLES: int = 200
TRANSACTION_CYCLES: int = 2_000
CANCELLATION_CYCLES: int = 200
POOL_WAIT_CYCLES: int = 50
POOL_WAIT_TIMEOUT_SECONDS: float = 0.05
CLOSE_CYCLES: int = 50
CLOSE_DEADLINE_SECONDS: float = 0.01
CLOSE_WAIT_SECONDS: float = 10.0
CLOSE_SCENARIO_SECONDS: float = 10.0
HEAP_SAMPLES_PER_CLOSE_SCENARIO: int = 3
WARM_UP_CYCLES: int = 1
MARKED_RUNS: int = 1_000
PAGE_SIZE: int = 100
TOP_LEVEL_NODES: int = 10
BODY_NODES: int = 1_000
EVENT_ROWS: int = 5_000
READ_CALLS: int = 200
IDENTIFIERS: int = 100_000
HEAP_SLOPE_ALLOWANCE_BYTES: int = 65_536
HEAP_GROWTH_ALLOWANCE_BYTES: int = 1_048_576
RSS_GROWTH_ALLOWANCE_BYTES: int = 16_777_216


class Fault(RuntimeError):
    pass


class StatementCounter:
    def __init__(self, database: Database) -> None:
        self.statements = 0
        for engine in {database.writer_engine, database.reader_engine}:
            event.listen(engine.sync_engine, "before_cursor_execute", self._count)

    def _count(self, *arguments: object) -> None:
        self.statements += 1


@pytest.fixture
def report() -> Iterator[Report]:
    yield from report_lifecycle()


def postgres_server_for(request: pytest.FixtureRequest) -> PostgresServer:
    server: PostgresServer = request.getfixturevalue("postgres_server")
    return server


def utc_now() -> datetime:
    return datetime.now(UTC)


def heap_bytes() -> int:
    gc.collect()
    current, _ = tracemalloc.get_traced_memory()
    return current


def test_metadata_database_lifetime(
    report: Report, request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    step_started = time.perf_counter()
    report.environment["sqlite_version"] = sqlite3.sqlite_version
    report.environment["sqlalchemy"] = version("sqlalchemy")
    report.environment["alembic"] = version("alembic")
    report.environment["aiosqlite"] = version("aiosqlite")
    report.environment["cpu_count"] = os.cpu_count()
    report.environment["machine"] = platform.machine()
    tracemalloc.start()
    try:
        for kind in selected_databases(os.environ):
            fixture_started = time.perf_counter()
            server = (
                postgres_server_for(request)
                if kind is DatabaseKind.POSTGRESQL
                else None
            )
            report.durations[f"{kind.value}_fixture"] = (
                time.perf_counter() - fixture_started
            )
            step_started += report.durations[f"{kind.value}_fixture"]
            if server is not None:
                report.environment["asyncpg"] = version("asyncpg")
                report.environment["postgres_image"] = (
                    f"{POSTGRES_TAG}@{POSTGRES_DIGEST}"
                )
            (tmp_path / kind.value).mkdir(exist_ok=True)
            factory = DatabaseFactory(kind, tmp_path / kind.value, server)
            try:
                baseline_connections = factory.server_connections()
                baseline_threads, baseline_descriptors = settled_counts()
                asyncio.run(measure(report, kind, factory))
                threads, descriptors = settled_counts()
                connections = factory.server_connections()
                gate(
                    report,
                    "threads_return_to_baseline",
                    kind,
                    threads <= baseline_threads,
                )
                gate(
                    report,
                    "descriptors_return_to_baseline",
                    kind,
                    descriptors <= baseline_descriptors,
                )
                record(
                    report,
                    "resource_counts",
                    1,
                    0.0,
                    kind,
                    baseline_threads=baseline_threads,
                    threads=threads,
                    baseline_descriptors=baseline_descriptors,
                    descriptors=descriptors,
                )
                gate(
                    report,
                    "server_connections_return_to_baseline",
                    kind,
                    connections is None
                    or baseline_connections is None
                    or connections <= baseline_connections,
                )
                record(
                    report,
                    "server_connections",
                    1,
                    0.0,
                    kind,
                    baseline=baseline_connections,
                    after=connections,
                )
            finally:
                factory.close()
    finally:
        tracemalloc.stop()
    report.durations["step_total"] = time.perf_counter() - step_started
    for name in ("identifier_generation",):
        measure_identifiers(report, name)

    serialised = report.to_json()
    assert all(report.gates.values()), report.gates
    report.diagnostic(
        "step_total_seconds",
        report.durations["step_total"],
        STEP_BUDGET_SECONDS,
        allow_equal=False,
    )
    assert "password" not in serialised.lower()
    assert "kora_measure_" not in serialised


def measure_identifiers(report: Report, name: str) -> None:
    generator = IdentifierGenerator(utc_now)

    def generate() -> None:
        previous = ""
        for _ in range(IDENTIFIERS):
            current = generator.new()
            assert current > previous
            previous = current

    seconds = timed(generate)
    report.samples.append(Sample(name, 0, IDENTIFIERS, seconds))


async def measure(report: Report, kind: DatabaseKind, factory: DatabaseFactory) -> None:
    await migration_cycles(report, kind, factory)
    await connect_cycles(report, kind, factory)
    await close_cycles(report, kind, factory)
    await transaction_cycles(report, kind, factory)
    await read_workloads(report, kind, factory)


async def migration_cycles(
    report: Report, kind: DatabaseKind, factory: DatabaseFactory
) -> None:
    durations: list[float] = []
    leftovers: list[str] = []
    no_op_steps: list[int] = []
    for _ in range(MIGRATION_CYCLES):
        settings = await factory.empty()
        started = time.perf_counter()
        first = await upgrade_database(settings)
        durations.append(time.perf_counter() - started)
        second = await upgrade_database(settings)
        no_op_steps.append(second.steps)
        leftovers.extend(factory.temporary_files(settings))
        await factory.discard(settings)
        assert first.steps == 5
    gate(report, "migration_rerun_is_a_no_op", kind, all(s == 0 for s in no_op_steps))
    gate(report, "migration_leaves_no_temporary_files", kind, leftovers == [])
    record(
        report,
        "migrate_from_empty",
        MIGRATION_CYCLES,
        sum(durations),
        kind,
        min_seconds=min(durations),
        max_seconds=max(durations),
    )


async def connect_cycles(
    report: Report, kind: DatabaseKind, factory: DatabaseFactory
) -> None:
    settings = await factory.migrated()
    heap_samples: list[int] = []
    rss_start = current_rss_bytes()

    async def cycle() -> None:
        database = Database(settings)
        try:
            async with database.read_transaction() as connection:
                await connection.execute(select(namespace.c.id).limit(1))
        finally:
            await database.close()

    for index in range(CONNECT_CYCLES):
        if index == 0:
            await cycle()
            heap_samples.append(heap_bytes())
        started = time.perf_counter()
        await cycle()
        heap_samples.append(heap_bytes())
        report.durations.setdefault(f"{kind.value}_connect_cycle_total", 0.0)
        report.durations[f"{kind.value}_connect_cycle_total"] += (
            time.perf_counter() - started
        )
    half = len(heap_samples) // 2
    first_half = heap_samples[half] - heap_samples[0]
    second_half = heap_samples[-1] - heap_samples[half]
    rss_growth = (
        None
        if rss_start is None or current_rss_bytes() is None
        else (current_rss_bytes() or 0) - rss_start
    )
    gate(
        report,
        "connect_heap_growth_bounded",
        kind,
        second_half <= HEAP_SLOPE_ALLOWANCE_BYTES
        and heap_samples[-1] - heap_samples[0] < HEAP_GROWTH_ALLOWANCE_BYTES,
    )
    gate(
        report,
        "connect_rss_growth_bounded",
        kind,
        rss_growth is None or rss_growth < RSS_GROWTH_ALLOWANCE_BYTES,
    )
    record(
        report,
        "connect_and_dispose",
        CONNECT_CYCLES,
        report.durations[f"{kind.value}_connect_cycle_total"],
        kind,
        heap_growth_first_half_bytes=first_half,
        heap_growth_second_half_bytes=second_half,
        rss_growth_bytes=rss_growth,
    )
    await factory.discard(settings)


CloseScenarioRun = Callable[[Database], Awaitable[list[str]]]
HANDED_OFF_PREFIX: str = "handed-off-"
CUT_SHORT_NAMES: frozenset[str] = frozenset({"outlived", "queued", "behind"})


@dataclass(frozen=True)
class CloseScenario:
    name: str
    run: CloseScenarioRun
    pool_size: int
    failing_units: int
    committed_per_cycle: int


async def close_past_deadline(database: Database) -> list[str]:
    writer_entered = asyncio.Event()
    reader_entered = asyncio.Event()
    release_writer = asyncio.Event()
    release_reader = asyncio.Event()
    holding = asyncio.create_task(
        hold_writer(database, "outlived", writer_entered, release_writer)
    )
    await asyncio.wait_for(writer_entered.wait(), CLOSE_WAIT_SECONDS)
    reading = asyncio.create_task(hold_reader(database, reader_entered, release_reader))
    await asyncio.wait_for(reader_entered.wait(), CLOSE_WAIT_SECONDS)
    queued = asyncio.create_task(write(database, "queued"))
    await until(lambda: database.units_in_flight() == 3)
    await database.close(deadline_seconds=CLOSE_DEADLINE_SECONDS)
    release_writer.set()
    writer_results = await asyncio.gather(holding, queued, return_exceptions=True)
    release_reader.set()
    reader_results = await asyncio.gather(reading, return_exceptions=True)
    return failure_messages([*writer_results, *reader_results])


async def close_during_handoff(database: Database) -> list[str]:
    first_entered = asyncio.Event()
    second_entered = asyncio.Event()
    release_first = asyncio.Event()
    release_second = asyncio.Event()
    closes: list[asyncio.Future[None]] = []

    def close_during_checkin(*arguments: object) -> None:
        if not closes:
            closes.append(asyncio.ensure_future(database.close(deadline_seconds=0)))

    event.listen(database.writer_engine.sync_engine, "reset", close_during_checkin)
    first = asyncio.create_task(
        hold_writer(
            database,
            f"{HANDED_OFF_PREFIX}{time.perf_counter_ns()}",
            first_entered,
            release_first,
        )
    )
    await asyncio.wait_for(first_entered.wait(), CLOSE_WAIT_SECONDS)
    second = asyncio.create_task(
        hold_writer(database, "behind", second_entered, release_second)
    )
    await until(lambda: database.units_in_flight() == 2)
    release_first.set()
    await first
    await closes[0]
    release_second.set()
    results = await asyncio.gather(second, return_exceptions=True)
    return failure_messages(results)


CLOSE_SCENARIOS: tuple[CloseScenario, ...] = (
    CloseScenario("close_past_deadline", close_past_deadline, 2, 3, 0),
    CloseScenario("close_during_handoff", close_during_handoff, 1, 1, 1),
)


async def close_cycles(
    report: Report, kind: DatabaseKind, factory: DatabaseFactory
) -> None:
    for scenario in CLOSE_SCENARIOS:
        settings = await factory.migrated()
        await close_scenario_cycles(report, kind, settings, scenario)
        await factory.discard(settings)


async def close_scenario_cycles(
    report: Report,
    kind: DatabaseKind,
    settings: DatabaseSettings,
    scenario: CloseScenario,
) -> None:
    name = scenario.name
    heap_samples: list[int] = []
    durations: list[float] = []
    cycles = CloseCycleOutcome()
    for index in range(WARM_UP_CYCLES + CLOSE_CYCLES):
        threads_before = threading.active_count()
        database = Database(narrow_settings(settings, scenario.pool_size))
        ledger = ConnectionLedger(database)
        started = time.perf_counter()
        try:
            messages = await asyncio.wait_for(
                scenario.run(database), CLOSE_SCENARIO_SECONDS
            )
        except TimeoutError:
            cycles.completed = False
            break
        elapsed = time.perf_counter() - started
        released = (
            ledger.closed == ledger.opened
            and ledger.outstanding == 0
            and ledger.closed_while_checked_out == 0
        )
        companions = sqlite_companions(database)
        threads_back = await threads_returned_to(threads_before)
        cycles.outcomes_as_expected = cycles.outcomes_as_expected and (
            messages == [CLOSED_BEFORE_FINISHING] * scenario.failing_units
        )
        cycles.connections_released = cycles.connections_released and released
        cycles.threads_released = cycles.threads_released and threads_back
        cycles.leftovers.extend(companions)
        if not cycles.passing():
            break
        if index < WARM_UP_CYCLES:
            heap_samples.append(heap_bytes())
            continue
        durations.append(elapsed)
        if index == WARM_UP_CYCLES + CLOSE_CYCLES // 2:
            heap_samples.append(heap_bytes())
    heap_samples.append(heap_bytes())
    written = await namespace_names(settings) if cycles.passing() else []
    gate(report, f"{name}_completed", kind, cycles.completed)
    gate(report, f"{name}_units_failed", kind, cycles.outcomes_as_expected)
    gate(
        report,
        f"{name}_writes_as_expected",
        kind,
        cycles.passing()
        and not CUT_SHORT_NAMES.intersection(written)
        and sum(1 for each in written if each.startswith(HANDED_OFF_PREFIX))
        == scenario.committed_per_cycle * (WARM_UP_CYCLES + CLOSE_CYCLES),
    )
    gate(
        report,
        f"{name}_connections_closed_each_cycle",
        kind,
        cycles.connections_released,
    )
    gate(report, f"{name}_threads_returned_each_cycle", kind, cycles.threads_released)
    gate(report, f"{name}_no_temporary_files", kind, not cycles.leftovers)
    gate(
        report,
        f"{name}_heap_growth_bounded",
        kind,
        len(heap_samples) == HEAP_SAMPLES_PER_CLOSE_SCENARIO
        and heap_samples[-1] - heap_samples[1] <= HEAP_SLOPE_ALLOWANCE_BYTES
        and heap_samples[-1] - heap_samples[0] < HEAP_GROWTH_ALLOWANCE_BYTES,
    )
    record(
        report,
        name,
        len(durations),
        sum(durations),
        kind,
        p50_ms=percentile(durations, 0.5) * 1_000 if durations else None,
        p95_ms=percentile(durations, 0.95) * 1_000 if durations else None,
        max_ms=max(durations) * 1_000 if durations else None,
        heap_samples=len(heap_samples),
    )


@dataclass
class CloseCycleOutcome:
    completed: bool = True
    outcomes_as_expected: bool = True
    connections_released: bool = True
    threads_released: bool = True
    leftovers: list[str] = field(default_factory=list[str])

    def passing(self) -> bool:
        return (
            self.completed
            and self.outcomes_as_expected
            and self.connections_released
            and self.threads_released
            and not self.leftovers
        )


async def threads_returned_to(count: int) -> bool:
    return await reached(lambda: threading.active_count() <= count)


async def namespace_names(settings: DatabaseSettings) -> list[str]:
    database = Database(settings)
    try:
        async with database.read_transaction() as connection:
            rows = (await connection.execute(select(namespace.c.name))).all()
    finally:
        await database.close()
    return [str(row[0]) for row in rows]


async def transaction_cycles(
    report: Report, kind: DatabaseKind, factory: DatabaseFactory
) -> None:
    settings = await factory.migrated()
    database = Database(settings)
    counter = StatementCounter(database)
    try:
        await run_path(report, kind, database, "commit", TRANSACTION_CYCLES, commit)
        await run_path(report, kind, database, "rollback", TRANSACTION_CYCLES, rollback)
        watch = StatementWatch(
            database, "pg_sleep" if kind is DatabaseKind.POSTGRESQL else "counter"
        )
        await run_path(
            report,
            kind,
            database,
            "cancel_in_flight",
            CANCELLATION_CYCLES,
            cancel_in_flight(watch, slow_statement(kind)),
            expected_invalidations=0
            if kind is DatabaseKind.SQLITE
            else CANCELLATION_CYCLES + WARM_UP_CYCLES,
        )
        gate(
            report,
            "cancel_in_flight_statements_observed",
            kind,
            watch.observed == CANCELLATION_CYCLES + WARM_UP_CYCLES,
        )
        if kind is DatabaseKind.SQLITE:
            await run_path(
                report,
                kind,
                database,
                "cancel_while_waiting",
                CANCELLATION_CYCLES,
                cancel_waiting_transaction,
            )
        else:
            narrow = Database(
                replace(
                    settings,
                    pool_size=1,
                    max_overflow=0,
                    pool_timeout_seconds=POOL_WAIT_TIMEOUT_SECONDS,
                )
            )
            try:
                await run_path(
                    report,
                    kind,
                    narrow,
                    "cancel_waiting_for_the_pool",
                    POOL_WAIT_CYCLES,
                    cancel_waiting_transaction,
                )
            finally:
                await narrow.close()
        async with database.read_transaction() as connection:
            rows = (await connection.execute(select(namespace.c.id))).all()
        gate(
            report,
            "committed_rows_match",
            kind,
            len(rows) == TRANSACTION_CYCLES + WARM_UP_CYCLES,
        )
        record(
            report,
            "statements_per_transaction_cycle",
            counter.statements,
            0.0,
            kind,
            statements=counter.statements,
        )
    finally:
        await database.close()
    gate(
        report,
        "transaction_temporary_files_released",
        kind,
        factory.temporary_files(settings) == [],
    )
    await factory.discard(settings)


Path_ = Callable[[Database], Awaitable[None]]


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


async def run_path(
    report: Report,
    kind: DatabaseKind,
    database: Database,
    name: str,
    cycles: int,
    path: Path_,
    expected_invalidations: int = 0,
) -> None:
    checkouts = CheckoutBalance(database)
    heap_samples: list[int] = []
    durations: list[float] = [0.0] * cycles
    await path(database)
    heap_samples.append(heap_bytes())
    tracemalloc.reset_peak()
    started = time.perf_counter()
    for index in range(cycles):
        cycle_started = time.perf_counter()
        await path(database)
        durations[index] = time.perf_counter() - cycle_started
        if index == cycles // 2:
            heap_samples.append(heap_bytes())
    seconds = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    heap_samples.append(heap_bytes())
    in_transaction = await open_transaction(database)
    gate(report, f"{name}_checkouts_balanced", kind, checkouts.outstanding == 0)
    gate(report, f"{name}_writer_free", kind, database.writer_is_free())
    gate(report, f"{name}_no_open_transaction", kind, not in_transaction)
    gate(
        report,
        f"{name}_invalidations_as_expected",
        kind,
        checkouts.invalidations == expected_invalidations,
    )
    gate(
        report,
        f"{name}_heap_growth_bounded",
        kind,
        heap_samples[-1] - heap_samples[1] <= HEAP_SLOPE_ALLOWANCE_BYTES
        and heap_samples[-1] - heap_samples[0] < HEAP_GROWTH_ALLOWANCE_BYTES,
    )
    record(
        report,
        f"transaction_{name}",
        cycles,
        seconds,
        kind,
        checkouts=checkouts.checkouts,
        invalidations=checkouts.invalidations,
        min_us=min(durations) * 1_000_000,
        p50_us=percentile(durations, 0.5) * 1_000_000,
        p95_us=percentile(durations, 0.95) * 1_000_000,
        max_us=max(durations) * 1_000_000,
        peak_traced_bytes=peak,
        heap_growth_first_half_bytes=heap_samples[1] - heap_samples[0],
        heap_growth_second_half_bytes=heap_samples[-1] - heap_samples[1],
        timed_under_tracemalloc=True,
    )


async def open_transaction(database: Database) -> bool:
    async with database.transaction() as connection:
        before = await connection.run_sync(in_transaction)
    return not before


def in_transaction(connection: Connection) -> bool:
    return connection.in_transaction()


async def commit(database: Database) -> None:
    async with database.transaction() as connection:
        await Seeder(connection).namespace(f"ns-{time.perf_counter_ns()}")


async def rollback(database: Database) -> None:
    try:
        async with database.transaction() as connection:
            await Seeder(connection).namespace("rolled-back")
            raise Fault()
    except Fault:
        return


def cancel_in_flight(watch: StatementWatch, statement: str) -> Path_:
    async def path(database: Database) -> None:
        watch.started.clear()

        async def work() -> None:
            async with database.transaction() as connection:
                await connection.execute(text(statement))

        task = asyncio.create_task(work())
        await asyncio.wait_for(watch.started.wait(), 10)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return

    return path


async def cancel_waiting_transaction(database: Database) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def holder() -> None:
        async with database.transaction():
            entered.set()
            await release.wait()

    async def waiter() -> None:
        async with database.transaction():
            return None

    holding = asyncio.create_task(holder())
    await entered.wait()
    waiting = asyncio.create_task(waiter())
    await asyncio.sleep(0)
    waiting.cancel()
    try:
        await waiting
    except asyncio.CancelledError:
        pass
    release.set()
    await holding


async def read_workloads(
    report: Report, kind: DatabaseKind, factory: DatabaseFactory
) -> None:
    settings = await factory.migrated()
    database = Database(settings)
    try:
        async with database.transaction() as connection:
            seeder = Seeder(connection)
            namespace_id = await seeder.namespace("reads")
            graph_id = await seeder.graph(namespace_id)
            version_id = await seeder.graph_version(graph_id)
            marked = await seeder.runs(namespace_id, graph_id, version_id, MARKED_RUNS)
            await seeder.runs(namespace_id, graph_id, version_id, MARKED_RUNS, False)
            finish_run = marked[0]
            await seeder.nodes(
                finish_run,
                [f"top{i}" for i in range(TOP_LEVEL_NODES)],
                "succeeded",
                True,
            )
            await seeder.nodes(
                finish_run,
                [f"fan[{i}]@1.body" for i in range(BODY_NODES)],
                "running",
                False,
            )
            await seeder.events(namespace_id, finish_run, EVENT_ROWS)
        if kind is DatabaseKind.POSTGRESQL:
            async with database.transaction() as connection:
                await connection.execute(text("ANALYZE"))
        counter = StatementCounter(database)

        sweep = MarkedRunSweep(PAGE_SIZE)
        visited = 0
        pages = 0
        started = time.perf_counter()
        async with database.read_transaction() as connection:
            while True:
                page = await sweep.next_page(connection)
                visited += len(page.run_ids)
                pages += 1
                if sweep.ceiling is None:
                    break
        sweep_seconds = time.perf_counter() - started
        sweep_statements = counter.statements
        gate(report, "sweep_visits_every_marked_run_once", kind, visited == MARKED_RUNS)
        record(
            report,
            "marked_run_sweep_page",
            pages,
            sweep_seconds,
            kind,
            statements=sweep_statements,
            pages=pages,
            visited=visited,
        )

        counter.statements = 0
        started = time.perf_counter()
        async with database.read_transaction() as connection:
            for _ in range(READ_CALLS):
                unfinished = await has_unfinished_top_level_instance(
                    connection, finish_run
                )
                assert not unfinished
        record(
            report,
            "run_finish_check",
            READ_CALLS,
            time.perf_counter() - started,
            kind,
            statements=counter.statements,
        )
        gate(
            report,
            "finish_check_one_statement_per_call",
            kind,
            counter.statements == READ_CALLS,
        )

        counter.statements = 0
        started = time.perf_counter()
        read_rows = 0
        async with database.read_transaction() as connection:
            after: str | None = None
            while True:
                page_rows = await events_of_run(
                    connection, namespace_id, finish_run, after, PAGE_SIZE
                )
                read_rows += len(page_rows)
                if len(page_rows) < PAGE_SIZE:
                    break
                after = page_rows[-1].id
        record(
            report,
            "event_timeline_page",
            counter.statements,
            time.perf_counter() - started,
            kind,
            statements=counter.statements,
            rows=read_rows,
        )
        gate(report, "event_pages_read_every_row", kind, read_rows == EVENT_ROWS)
    finally:
        await database.close()
    await factory.discard(settings)
