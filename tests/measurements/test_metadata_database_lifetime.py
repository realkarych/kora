import asyncio
import gc
import os
import platform
import sqlite3
import time
import tracemalloc
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

import pytest
from sqlalchemy import Connection, event, select, text

from kora.db.connection import Database, DatabaseSettings
from kora.db.identifiers import IdentifierGenerator
from kora.db.migrations import upgrade_database
from kora.db.reads import (
    MarkedRunSweep,
    events_of_run,
    has_unfinished_top_level_instance,
)
from kora.db.tables import namespace
from tests.db.seed import Seeder
from tests.measurements.harness import (
    Report,
    Sample,
    current_rss_bytes,
    open_descriptors,
    report_lifecycle,
    settled_counts,
    timed,
)
from tests.support.databases import (
    EMPTY_TEMPLATE,
    POSTGRES_DIGEST,
    POSTGRES_TAG,
    TEMPLATE_DATABASE,
    DatabaseKind,
    PostgresServer,
    copy_sqlite_template,
    prepare_sqlite_template,
    selected_databases,
    sqlite_settings,
)

pytestmark = pytest.mark.measurement

STEP_BUDGET_SECONDS: float = 120.0
MIGRATION_CYCLES: int = 20
CONNECT_CYCLES: int = 200
TRANSACTION_CYCLES: int = 2_000
CANCELLATION_CYCLES: int = 200
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
SLOW_SQLITE_STATEMENT: str = (
    "WITH RECURSIVE counter(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM counter "
    "WHERE n < 200000) SELECT count(*) FROM counter"
)
SLOW_POSTGRESQL_STATEMENT: str = "SELECT pg_sleep(0.005)"


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


class DatabaseFactory:
    def __init__(
        self, kind: DatabaseKind, directory: Path, server: PostgresServer | None
    ) -> None:
        self.kind = kind
        self.directory = directory
        self.server = server
        self.created: list[str] = []
        self.sqlite_template = (
            prepare_sqlite_template(directory) if server is None else None
        )
        self._counter = 0

    def _next_name(self) -> str:
        self._counter += 1
        return f"kora_measure_{os.getpid()}_{self._counter}"

    def empty(self) -> DatabaseSettings:
        if self.server is None:
            return sqlite_settings(self.directory / f"{self._next_name()}.db")
        name = self._next_name()
        self.server.create_database(name, EMPTY_TEMPLATE)
        self.created.append(name)
        return self.server.settings_for(name)

    def migrated(self) -> DatabaseSettings:
        if self.server is None or self.sqlite_template is None:
            assert self.sqlite_template is not None
            return copy_sqlite_template(
                self.sqlite_template, self.directory / f"{self._next_name()}.db"
            )
        name = self._next_name()
        self.server.create_database(name, TEMPLATE_DATABASE)
        self.created.append(name)
        return self.server.settings_for(name)

    def discard(self, settings: DatabaseSettings) -> None:
        if self.server is None:
            path = settings.sqlite_path
            for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
                if candidate.exists():
                    candidate.unlink()
            return
        name = str(settings.url.database)
        if name in self.created:
            self.server.drop_database(name)
            self.created.remove(name)

    def temporary_files(self, settings: DatabaseSettings) -> list[str]:
        if self.server is not None:
            return []
        path = settings.sqlite_path
        return [
            candidate.name
            for candidate in (Path(f"{path}-wal"), Path(f"{path}-shm"))
            if candidate.exists()
        ]

    def server_connections(self) -> int | None:
        return None if self.server is None else self.server.connection_count()

    def close(self) -> None:
        for name in list(self.created):
            if self.server is not None:
                self.server.drop_database(name)
        self.created.clear()


def utc_now() -> datetime:
    return datetime.now(UTC)


def heap_bytes() -> int:
    gc.collect()
    current, _ = tracemalloc.get_traced_memory()
    return current


def record(
    report: Report,
    workload: str,
    count: int,
    seconds: float,
    kind: DatabaseKind,
    **extra: float | int | bool | str | None,
) -> None:
    report.samples.append(
        Sample(f"{kind.value}_{workload}", 0, count, seconds, {**extra})
    )


def gate(report: Report, name: str, kind: DatabaseKind, passed: bool) -> None:
    report.gates[f"{kind.value}_{name}"] = passed


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
            server = (
                postgres_server_for(request)
                if kind is DatabaseKind.POSTGRESQL
                else None
            )
            if server is not None:
                report.environment["asyncpg"] = version("asyncpg")
                report.environment["postgres_image"] = (
                    f"{POSTGRES_TAG}@{POSTGRES_DIGEST}"
                )
            (tmp_path / kind.value).mkdir(exist_ok=True)
            factory = DatabaseFactory(kind, tmp_path / kind.value, server)
            try:
                baseline_connections = factory.server_connections()
                asyncio.run(measure(report, kind, factory))
                connections = factory.server_connections()
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
    assert report.durations["step_total"] < STEP_BUDGET_SECONDS
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
    baseline_threads, baseline_descriptors = settled_counts()
    await migration_cycles(report, kind, factory)
    await connect_cycles(report, kind, factory)
    await transaction_cycles(report, kind, factory)
    await read_workloads(report, kind, factory)
    threads, descriptors = settled_counts()
    gate(report, "threads_return_to_baseline", kind, threads <= baseline_threads)
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


async def migration_cycles(
    report: Report, kind: DatabaseKind, factory: DatabaseFactory
) -> None:
    durations: list[float] = []
    leftovers: list[str] = []
    no_op_steps: list[int] = []
    for _ in range(MIGRATION_CYCLES):
        settings = factory.empty()
        started = time.perf_counter()
        first = await upgrade_database(settings)
        durations.append(time.perf_counter() - started)
        second = await upgrade_database(settings)
        no_op_steps.append(second.steps)
        leftovers.extend(factory.temporary_files(settings))
        factory.discard(settings)
        assert first.steps == 1
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
    settings = factory.migrated()
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
    factory.discard(settings)


def slow_statement(kind: DatabaseKind) -> str:
    if kind is DatabaseKind.SQLITE:
        return SLOW_SQLITE_STATEMENT
    return SLOW_POSTGRESQL_STATEMENT


async def transaction_cycles(
    report: Report, kind: DatabaseKind, factory: DatabaseFactory
) -> None:
    settings = factory.migrated()
    database = Database(settings)
    counter = StatementCounter(database)
    try:
        await run_path(report, kind, database, "commit", TRANSACTION_CYCLES, commit)
        await run_path(report, kind, database, "rollback", TRANSACTION_CYCLES, rollback)
        await run_path(
            report,
            kind,
            database,
            "cancel_in_flight",
            CANCELLATION_CYCLES,
            lambda db: cancel_in_flight(db, slow_statement(kind)),
        )
        if kind is DatabaseKind.SQLITE:
            await run_path(
                report,
                kind,
                database,
                "cancel_while_waiting",
                CANCELLATION_CYCLES,
                cancel_while_waiting,
            )
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
    factory.discard(settings)


Path_ = Callable[[Database], Awaitable[None]]


async def run_path(
    report: Report,
    kind: DatabaseKind,
    database: Database,
    name: str,
    cycles: int,
    path: Path_,
) -> None:
    checkouts = CheckoutBalance(database)
    heap_samples: list[int] = []
    await path(database)
    heap_samples.append(heap_bytes())
    started = time.perf_counter()
    for index in range(cycles):
        await path(database)
        if index == cycles // 2:
            heap_samples.append(heap_bytes())
    seconds = time.perf_counter() - started
    heap_samples.append(heap_bytes())
    in_transaction = await open_transaction(database)
    gate(report, f"{name}_checkouts_balanced", kind, checkouts.outstanding == 0)
    gate(report, f"{name}_writer_free", kind, database.writer_is_free())
    gate(report, f"{name}_no_open_transaction", kind, not in_transaction)
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
        heap_growth_first_half_bytes=heap_samples[1] - heap_samples[0],
        heap_growth_second_half_bytes=heap_samples[-1] - heap_samples[1],
    )


class CheckoutBalance:
    def __init__(self, database: Database) -> None:
        self.outstanding = 0
        self.checkouts = 0
        self.invalidations = 0
        for engine in {database.writer_engine, database.reader_engine}:
            event.listen(engine.sync_engine, "checkout", self._checkout)
            event.listen(engine.sync_engine, "checkin", self._checkin)
            event.listen(engine.sync_engine, "invalidate", self._invalidate)

    def _checkout(self, *arguments: object) -> None:
        self.outstanding += 1
        self.checkouts += 1

    def _checkin(self, *arguments: object) -> None:
        self.outstanding -= 1

    def _invalidate(self, *arguments: object) -> None:
        self.invalidations += 1


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


async def cancel_in_flight(database: Database, statement: str) -> None:
    async def work() -> None:
        async with database.transaction() as connection:
            await connection.execute(text(statement))

    task = asyncio.create_task(work())
    await asyncio.sleep(0)
    while not task.done() and database.units_in_flight() == 0:
        await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        return


async def cancel_while_waiting(database: Database) -> None:
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
    settings = factory.migrated()
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
    factory.discard(settings)
    assert open_descriptors() >= 0
