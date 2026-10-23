from __future__ import annotations

import asyncio
import errno
import gc
import json
import os
import platform
import shutil
import sqlite3
import stat
import sys
import threading
import time
import tracemalloc
from array import array
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from importlib.metadata import version
from pathlib import Path
from typing import Protocol, cast

import aiosqlite
import pytest
from sqlalchemy import (
    Connection,
    ExceptionContext,
    event,
    func,
    insert,
    literal,
    select,
    text,
)
from sqlalchemy.dialects.postgresql.base import PGDialect
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.connection import (
    Database,
    DatabaseClosedError,
    DatabaseSettings,
    Dialect,
    parse_database_url,
)
from kora.db.cost_reads import (
    CostReadRefused,
    HistoricalState,
    read_approval_cost,
    read_attempt_page,
    read_attempt_price,
    read_latest_price,
    read_price,
    read_usage_page,
    read_usage_source,
)
from kora.db.identifiers import IdentifierGenerator
from kora.db.migrations import current_revision, upgrade_database
from kora.db.reads import (
    MarkedRunSweep,
    events_of_run,
    has_unfinished_top_level_instance,
)
from kora.db.tables import (
    approval_request,
    attempt,
    metadata,
    namespace,
    price_table,
    usage_record,
)
from kora.db.types import JsonValue, canonical_json
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
from tests.db.seed import START, Seeder, seed_pricing_measurement_history
from tests.db.test_pricing_migration import (
    close_before_migration,
    price_check,
    wait_for_index,
)
from tests.measurements.harness import (
    Report,
    Sample,
    current_rss_bytes,
    new_report,
    open_descriptors,
    report_lifecycle,
    settled_counts,
    timed,
)
from tests.measurements.ir_config_lifetime import peak_rss_bytes, rss_bytes
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
from tests.support.observability import run_python

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
        self.pricing_template: DatabaseSettings | None = None

    def _next_name(self) -> str:
        self._counter += 1
        return f"kora_measure_{os.getpid()}_{self._counter}"

    async def empty(self) -> DatabaseSettings:
        if self.server is None:
            return sqlite_settings(self.directory / f"{self._next_name()}.db")
        name = self._next_name()
        await self.server.create_database_in_loop(name, EMPTY_TEMPLATE)
        self.created.append(name)
        return self.server.settings_for(name)

    async def migrated(self) -> DatabaseSettings:
        if self.server is None:
            assert self.sqlite_template is not None
            return copy_sqlite_template(
                self.sqlite_template, self.directory / f"{self._next_name()}.db"
            )
        name = self._next_name()
        await self.server.create_database_in_loop(name, TEMPLATE_DATABASE)
        self.created.append(name)
        return self.server.settings_for(name)

    async def predecessor(self) -> DatabaseSettings:
        if self.pricing_template is None:
            self.pricing_template = await self.empty()
            database = Database(self.pricing_template)
            try:
                await pricing_dataset(database)
            finally:
                await database.close()
        if self.server is None:
            destination = self.directory / f"{self._next_name()}.db"
            shutil.copyfile(self.pricing_template.sqlite_path, destination)
            return sqlite_settings(destination)
        name = self._next_name()
        await self.server.create_database_in_loop(
            name, str(self.pricing_template.url.database)
        )
        self.created.append(name)
        return self.server.settings_for(name)

    async def discard(self, settings: DatabaseSettings) -> None:
        if self.server is None:
            path = settings.sqlite_path
            for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
                if candidate.exists():
                    candidate.unlink()
            return
        name = str(settings.url.database)
        if name in self.created:
            await self.server.drop_database_in_loop(name)
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
    pricing_environment(report, "coordinator")
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
    await migration_cycles(report, kind, factory)
    await connect_cycles(report, kind, factory)
    await close_cycles(report, kind, factory)
    await transaction_cycles(report, kind, factory)
    await read_workloads(report, kind, factory)
    await pricing_process_runs(report, kind, factory)


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
        assert first.steps == 2
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


def slow_statement(kind: DatabaseKind) -> str:
    if kind is DatabaseKind.SQLITE:
        return SLOW_SQLITE_STATEMENT
    return SLOW_POSTGRESQL_STATEMENT


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


class StatementWatch:
    def __init__(self, database: Database, fragment: str) -> None:
        self.fragment = fragment
        self.started = asyncio.Event()
        self.observed = 0
        event.listen(
            database.writer_engine.sync_engine, "before_cursor_execute", self._observe
        )

    def _observe(
        self, connection: object, cursor: object, statement: str, *arguments: object
    ) -> None:
        if self.fragment in statement:
            self.observed += 1
            self.started.set()


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


PRICING_RUNS: int = 3
PRICING_WARMUPS: int = 3
PRICING_READ_SAMPLES: int = 30
PRICING_HISTORY_SIZES: tuple[int, ...] = (100, 1_000, 10_000)
PRICING_RELEASE_CYCLES: int = 30
PRICING_PAGE_SIZE: int = 32
PRICING_ATTEMPT_PAGE_SIZE: int = 4


def pricing_environment(report: Report, mode: str) -> None:
    report.environment.update(
        pid=os.getpid(),
        executable=sys.executable,
        cwd=str(Path.cwd()),
        sqlalchemy=version("sqlalchemy"),
        pydantic=version("pydantic"),
        sqlite_version=sqlite3.sqlite_version,
        pricing_mode=mode,
        pricing_runs=PRICING_RUNS,
        pricing_read_samples=PRICING_READ_SAMPLES,
        pricing_warmups=PRICING_WARMUPS,
        pricing_release_cycles=PRICING_RELEASE_CYCLES,
        pricing_cases="populated-upgrade,phase-writers,history-100-1000-10000,default,namespace,empty,before-first,endpoint,absent,ordinary-plan,generic-plan,all-ledger-pages,exact,zero,null,legacy,source-refusal,failure-recovery,contention,cancel,close,session-restore,process-exit",
    )


def migration_phase(statement: str) -> str | None:
    sql = " ".join(statement.upper().split())
    if "PG_ADVISORY_UNLOCK" in sql:
        return "lock_release"
    if "PG_ADVISORY_LOCK(" in sql or sql == "BEGIN IMMEDIATE":
        return "lock_acquire"
    if "VALIDATE CONSTRAINT" in sql:
        return "validate_" + sql.split()[2].lower()
    if sql.startswith("INSERT INTO _ALEMBIC_TMP_"):
        return "copy_" + sql.split()[2].removeprefix("_ALEMBIC_TMP_").lower()
    if sql == "PRAGMA FOREIGN_KEY_CHECK":
        return "foreign_keys"
    if sql.startswith("DROP INDEX CONCURRENTLY"):
        return "index_repair"
    if sql.startswith("CREATE UNIQUE INDEX"):
        return "index_build"
    if sql.startswith("UPDATE ALEMBIC_VERSION"):
        return "stamp"
    if sql.startswith(("ALTER TABLE", "CREATE TABLE", "DROP TABLE")):
        return "ddl"
    return None


class PricingMigrationWatch:
    def __init__(
        self, report: Report, kind: DatabaseKind, settings: DatabaseSettings, label: str
    ) -> None:
        self.report = report
        self.kind = kind
        self.settings = settings
        self.label = label
        self.started = time.perf_counter()
        self.statement_started = self.started
        self.lock_acquired: float | None = None
        self.writer_tasks: list[asyncio.Task[None]] = []
        self.writer_phases: set[str] = set()
        self.database: Database | None = None
        self.ledger: ConnectionLedger | None = None
        self.storage_peak = 0
        self.wal_peak = 0
        self.shm_peak = 0
        self.temporary_peak = 0
        self.directory_baseline = pricing_directory_baseline(settings)
        self.finished = asyncio.Event()
        self.observer: asyncio.Task[None] | None = None
        self.writers: dict[str, Database] = {}
        self.writer_ledgers: list[ConnectionLedger] = []
        self.intervals: list[tuple[str, float, float]] = []
        self.active_phase: int | None = None
        self.pending_transaction: tuple[str, float] | None = None
        self.checkout_started = self.started

    async def prepare(self) -> None:
        for table in ("price_table", "usage_record", "attempt", "approval_request"):
            database = Database(narrow_settings(self.settings, 1))
            self.writers[table] = database
            self.writer_ledgers.append(ConnectionLedger(database))
            async with database.transaction() as connection:
                await connection.exec_driver_sql("SELECT 1")
        self.started = time.perf_counter()

    def checkout(self, *arguments: object) -> None:
        self.checkout_started = time.perf_counter()
        self.statement_started = self.checkout_started

    def begin(self, connection: Connection) -> None:
        self.completed_transaction()
        if self.kind is DatabaseKind.SQLITE:
            self.lock_acquired = time.perf_counter()
            record(
                self.report,
                f"pricing_{self.label}_sqlite_writer_lock_acquired",
                1,
                self.lock_acquired - self.checkout_started,
                self.kind,
                interval="checkout_to_completed_begin_immediate_upper_bound",
            )

    def completed_transaction(self, *arguments: object) -> None:
        pending = self.pending_transaction
        if pending is None:
            return
        self.pending_transaction = None
        outcome, started = pending
        finished = time.perf_counter()
        record(
            self.report,
            f"pricing_{self.label}_{outcome}_completed_bound",
            1,
            finished - started,
            self.kind,
            completion_bound="next_statement_begin_or_pool_checkin",
            start_offset_seconds=started - self.started,
            finish_offset_seconds=finished - self.started,
        )
        if self.kind is DatabaseKind.SQLITE and self.lock_acquired is not None:
            record(
                self.report,
                f"pricing_{self.label}_sqlite_writer_lock_released_bound",
                1,
                started - self.lock_acquired,
                self.kind,
                hold_upper_seconds=finished - self.lock_acquired,
                release_offset_upper_seconds=finished - self.started,
            )
            self.lock_acquired = None

    def error(self, context: ExceptionContext) -> None:
        finished = time.perf_counter()
        phase = migration_phase(context.statement or "")
        if self.active_phase is not None:
            phase, started, _ = self.intervals[self.active_phase]
            self.intervals[self.active_phase] = phase, started, finished
            self.active_phase = None
        record(
            self.report,
            f"pricing_{self.label}_failed_statement",
            1,
            finished - self.statement_started,
            self.kind,
            phase=phase or "statement_or_raw_begin",
            outcome="cancelled"
            if isinstance(context.original_exception, asyncio.CancelledError)
            else "failed",
            exception_type=type(context.original_exception).__name__,
        )

    def open(self, settings: DatabaseSettings) -> Database:
        database = Database(settings)
        self.database = database
        self.ledger = ConnectionLedger(database)
        engine = database.writer_engine.sync_engine
        event.listen(engine, "before_cursor_execute", self.before)
        event.listen(engine, "after_cursor_execute", self.after)
        event.listen(engine, "commit", self.commit)
        event.listen(engine, "rollback", self.rollback)
        event.listen(engine, "begin", self.begin)
        event.listen(engine, "checkout", self.checkout)
        event.listen(engine, "checkin", self.completed_transaction)
        event.listen(engine, "handle_error", self.error)
        self.observer = asyncio.create_task(self.observe())
        return database

    async def observe(self) -> None:
        database = Database(self.settings)
        locks: dict[tuple[int, str, str, bool], tuple[float, float, float]] = {}
        previous = time.perf_counter()
        observations = 0
        maximum_gap = 0.0
        regular_peak = 0
        unlinked_peak = 0
        try:
            while True:
                found: set[tuple[int, str, str, bool]] = set()
                async with database.read_transaction() as connection:
                    if self.kind is DatabaseKind.POSTGRESQL:
                        rows = await connection.execute(
                            text(
                                "SELECT l.pid, coalesce(c.relname, l.locktype), "
                                "l.mode, l.granted FROM pg_locks l LEFT JOIN "
                                "pg_class c ON c.oid = l.relation WHERE l.database "
                                "= (SELECT oid FROM pg_database WHERE datname = "
                                "current_database()) AND l.pid <> pg_backend_pid() "
                                "AND (c.relname IN ('price_table', 'usage_record', "
                                "'attempt', 'approval_request') OR "
                                "l.locktype = 'advisory')"
                            )
                        )
                        found = {
                            (int(pid), str(relation), str(mode), bool(granted))
                            for pid, relation, mode, granted in rows
                        }
                    else:
                        await connection.exec_driver_sql("SELECT 1")
                        regular_peak = max(regular_peak, regular_descriptor_bytes())
                        unlinked_peak = max(
                            unlinked_peak, regular_descriptor_bytes(True)
                        )
                        self.storage()
                        self.temporary_storage()
                now = time.perf_counter()
                observations += 1
                maximum_gap = max(maximum_gap, now - previous)
                for key in found:
                    prior = locks.get(key)
                    locks[key] = (
                        previous if prior is None else prior[0],
                        now if prior is None else prior[1],
                        now,
                    )
                for key in set(locks) - found:
                    earliest, first, last = locks.pop(key)
                    pid, relation, mode, granted = key
                    record(
                        self.report,
                        f"pricing_{self.label}_observed_lock",
                        1,
                        last - first,
                        self.kind,
                        pid=pid,
                        relation=relation,
                        mode=mode,
                        granted=granted,
                        duration_upper_seconds=now - earliest,
                        first_offset_seconds=first - self.started,
                        last_offset_seconds=last - self.started,
                    )
                previous = now
                if self.finished.is_set() and not locks:
                    break
            assert not locks
            record(
                self.report,
                f"pricing_{self.label}_observer",
                observations,
                time.perf_counter() - self.started,
                self.kind,
                maximum_poll_gap_seconds=maximum_gap,
                peak_open_regular_file_bytes=regular_peak,
                peak_open_unlinked_regular_file_bytes=unlinked_peak,
                file_counter_scope="process_regular_fstat_unique_inode_excluding_stdio",
                sampling="sql_completion_poll_observed_maximum_not_exhaustive_peak",
                storage_peak_observed_bytes=self.storage_peak,
            )
        finally:
            await database.close()

    def storage(self) -> int:
        if self.kind is DatabaseKind.POSTGRESQL:
            return 0
        main = sqlite_file_bytes(self.settings, "")
        wal = sqlite_file_bytes(self.settings, "-wal")
        shm = sqlite_file_bytes(self.settings, "-shm")
        total = main + wal + shm
        self.storage_peak = max(total, self.storage_peak)
        self.wal_peak = max(self.wal_peak, wal)
        self.shm_peak = max(self.shm_peak, shm)
        return total

    def temporary_storage(self) -> None:
        _, temporary = pricing_named_files(self.settings, self.directory_baseline)
        self.temporary_peak = max(temporary, self.temporary_peak)

    def before(
        self, connection: object, cursor: object, statement: str, *arguments: object
    ) -> None:
        self.completed_transaction()
        self.statement_started = time.perf_counter()
        self.storage()
        phase = migration_phase(statement)
        self.active_phase = None
        if phase is not None:
            index = len(self.intervals)
            self.active_phase = index
            self.intervals.append((phase, self.statement_started, 0.0))
            if self.writers and phase not in self.writer_phases:
                tables = {
                    "ddl": (
                        "price_table",
                        "usage_record",
                        "attempt",
                        "approval_request",
                    ),
                    "validate_price_table": ("price_table",),
                    "validate_usage_record": ("usage_record",),
                    "validate_approval_request": ("approval_request",),
                    "copy_price_table": ("price_table",),
                    "copy_usage_record": ("usage_record",),
                    "index_build": ("price_table",),
                    "stamp": (
                        "price_table",
                        "usage_record",
                        "attempt",
                        "approval_request",
                    ),
                }.get(phase, ())
                self.writer_phases.add(phase)
                for table in tables:
                    self.writer_tasks.append(
                        asyncio.create_task(self.writer(index, table))
                    )
        if phase == "lock_release" and self.lock_acquired is not None:
            record(
                self.report,
                f"pricing_{self.label}_advisory_lock_hold",
                1,
                self.statement_started - self.lock_acquired,
                self.kind,
            )

    def after(
        self, connection: object, cursor: object, statement: str, *arguments: object
    ) -> None:
        finished = time.perf_counter()
        phase = migration_phase(statement)
        if self.active_phase is not None:
            _, started, _ = self.intervals[self.active_phase]
            self.intervals[self.active_phase] = phase or "statement", started, finished
            self.active_phase = None
        if phase is not None:
            record(
                self.report,
                f"pricing_{self.label}_{phase}",
                1,
                finished - self.statement_started,
                self.kind,
                start_offset_seconds=self.statement_started - self.started,
                storage_bytes=self.storage(),
            )
        if phase == "lock_acquire":
            self.lock_acquired = finished

    def commit(self, connection: Connection) -> None:
        self.pending_transaction = "commit", time.perf_counter()

    def rollback(self, connection: Connection) -> None:
        self.pending_transaction = "rollback", time.perf_counter()

    async def writer(self, interval: int, table: str) -> None:
        database = self.writers[table]
        identifier = {
            "price_table": "old-other-scoped",
            "usage_record": "old-zero",
            "attempt": "old-attempt",
            "approval_request": "old-other-approval",
        }[table]
        started = time.perf_counter()
        async with database.transaction() as connection:
            admitted = time.perf_counter()
            relation = {
                "price_table": price_table,
                "usage_record": usage_record,
                "attempt": attempt,
                "approval_request": approval_request,
            }[table]
            result = await connection.execute(
                relation.update()
                .where(relation.c.id == identifier)
                .values(id=relation.c.id)
            )
            assert result.rowcount == 1
            written = time.perf_counter()
        finished = time.perf_counter()
        await until(lambda: self.intervals[interval][2] > 0)
        phase, phase_start, phase_finish = self.intervals[interval]
        record(
            self.report,
            f"pricing_{self.label}_writer",
            1,
            finished - started,
            self.kind,
            phase=phase,
            table=table,
            start_offset_seconds=started - self.started,
            admission_offset_seconds=admitted - self.started,
            admission_wait_seconds=admitted - started,
            statement_seconds=written - admitted,
            finish_offset_seconds=finished - self.started,
            phase_start_offset_seconds=phase_start - self.started,
            phase_finish_offset_seconds=phase_finish - self.started,
            request_overlap_seconds=max(
                0, min(finished, phase_finish) - max(started, phase_start)
            ),
            statement_overlap_seconds=max(
                0, min(written, phase_finish) - max(admitted, phase_start)
            ),
        )

    async def finish(self) -> None:
        try:
            await asyncio.gather(*self.writer_tasks)
        finally:
            for database in self.writers.values():
                await database.close()
            assert all(
                ledger.opened == ledger.closed and ledger.outstanding == 0
                for ledger in self.writer_ledgers
            )
            self.finished.set()
            assert self.observer is not None
            await asyncio.wait_for(self.observer, CLOSE_WAIT_SECONDS)
        assert self.database is not None
        assert self.ledger is not None
        assert self.database.units_in_flight() == 0
        assert self.ledger.outstanding == 0
        assert self.ledger.opened == self.ledger.closed
        named_count, named_bytes = pricing_named_files(
            self.settings, self.directory_baseline
        )
        assert named_count == 0
        record(
            self.report,
            f"pricing_{self.label}_released",
            1,
            time.perf_counter() - self.started,
            self.kind,
            storage_peak_observed_bytes=self.storage_peak,
            storage_after_bytes=self.storage(),
            wal_peak_observed_bytes=self.wal_peak,
            shm_peak_observed_bytes=self.shm_peak,
            new_directory_temporary_peak_observed_bytes=self.temporary_peak,
            new_named_temporary_files_after=named_count,
            new_named_temporary_bytes_after=named_bytes,
            temporary_counter_scope="new_files_in_fixture_directory_plus_process_open_unlinked_regular_files",
            wal_after_bytes=0
            if self.kind is DatabaseKind.POSTGRESQL
            else sqlite_file_bytes(self.settings, "-wal"),
            opened=self.ledger.opened,
            closed=self.ledger.closed,
        )
        engine = self.database.writer_engine.sync_engine
        event.remove(engine, "before_cursor_execute", self.before)
        event.remove(engine, "after_cursor_execute", self.after)
        event.remove(engine, "commit", self.commit)
        event.remove(engine, "rollback", self.rollback)
        event.remove(engine, "begin", self.begin)
        event.remove(engine, "checkout", self.checkout)
        event.remove(engine, "checkin", self.completed_transaction)
        event.remove(engine, "handle_error", self.error)


def sqlite_file_bytes(settings: DatabaseSettings, suffix: str) -> int:
    path = Path(str(settings.sqlite_path) + suffix)
    return path.stat().st_size if path.exists() else 0


def pricing_directory_baseline(settings: DatabaseSettings) -> set[str]:
    if settings.dialect is Dialect.POSTGRESQL:
        return set()
    return {path.name for path in settings.sqlite_path.parent.iterdir()}


def pricing_named_files(
    settings: DatabaseSettings, baseline: set[str]
) -> tuple[int, int]:
    if settings.dialect is Dialect.POSTGRESQL:
        return 0, 0
    companions = {settings.sqlite_path.name + suffix for suffix in ("", "-wal", "-shm")}
    sizes = [
        path.stat().st_size
        for path in settings.sqlite_path.parent.iterdir()
        if path.is_file() and path.name not in baseline and path.name not in companions
    ]
    return len(sizes), sum(sizes)


def pricing_storage_released(
    settings: DatabaseSettings, report: Report, label: str, directory_baseline: set[str]
) -> None:
    if settings.dialect is Dialect.SQLITE:
        assert not Path(str(settings.sqlite_path) + "-wal").exists()
        assert not Path(str(settings.sqlite_path) + "-shm").exists()
        assert regular_descriptor_bytes(True) == 0
        named_count, named_bytes = pricing_named_files(settings, directory_baseline)
        assert named_count == 0
        record(
            report,
            f"pricing_storage_released_{label}",
            1,
            0,
            DatabaseKind.SQLITE,
            main_file_bytes=settings.sqlite_path.stat().st_size,
            wal_bytes=0,
            shm_bytes=0,
            open_unlinked_regular_file_bytes=0,
            new_named_temporary_files_after=named_count,
            new_named_temporary_bytes_after=named_bytes,
        )


async def pricing_storage(database: Database, report: Report, label: str) -> None:
    kind = DatabaseKind(database.dialect.value)
    async with database.read_transaction() as connection:
        if kind is DatabaseKind.POSTGRESQL:
            rows = await connection.execute(
                text(
                    "SELECT c.relname, pg_relation_size(c.oid), "
                    "pg_relation_size(c.oid, 'fsm'), pg_relation_size(c.oid, 'vm'), "
                    "pg_total_relation_size(c.oid) FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname "
                    "= current_schema() AND (c.relname IN ('price_table', "
                    "'usage_record') OR c.relname LIKE 'ix_price_table_%') ORDER "
                    "BY c.relname"
                )
            )
            for name, size, fsm, vm, total in rows:
                record(
                    report,
                    f"pricing_storage_{label}",
                    1,
                    0,
                    kind,
                    relation=str(name),
                    main_fork_bytes=int(size),
                    free_space_map_bytes=int(fsm),
                    visibility_map_bytes=int(vm),
                    relation_total_including_indexes_toast_bytes=int(total),
                )
        else:
            pages = int(
                (await connection.exec_driver_sql("PRAGMA page_count")).scalar_one()
            )
            page_size = int(
                (await connection.exec_driver_sql("PRAGMA page_size")).scalar_one()
            )
            free = int(
                (await connection.exec_driver_sql("PRAGMA freelist_count")).scalar_one()
            )
            record(
                report,
                f"pricing_storage_{label}",
                1,
                0,
                kind,
                bytes=pages * page_size,
                reusable_bytes=free * page_size,
                main_file_bytes=database.settings.sqlite_path.stat().st_size,
                wal_bytes=sqlite_file_bytes(database.settings, "-wal"),
                shm_bytes=sqlite_file_bytes(database.settings, "-shm"),
                open_regular_file_bytes=regular_descriptor_bytes(),
                open_unlinked_regular_file_bytes=regular_descriptor_bytes(True),
            )


async def pricing_dataset(database: Database) -> None:
    async with database.transaction() as connection:
        revision = await connection.run_sync(current_revision)
        if revision is None:
            await connection.run_sync(seed_pricing_measurement_history)
        else:
            assert revision == "0001"
    async with database.read_transaction() as connection:
        for table_name, count in (
            ("price_table", 1000),
            ("attempt", 100),
            ("usage_record", 1001),
            ("approval_request", 25),
        ):
            actual = await connection.scalar(
                select(func.count()).select_from(metadata.tables[table_name])
            )
            assert actual == count
        scopes = await connection.scalar(
            text(
                "SELECT count(*) FROM (SELECT DISTINCT namespace_id FROM "
                "price_table) AS scopes"
            )
        )
        assert scopes == 10


def pricing_map() -> dict[str, JsonValue]:
    rates: dict[str, JsonValue] = {
        "input": "1",
        "cached_input": None,
        "cache_creation_5m": None,
        "cache_creation_1h": None,
        "output": "2",
        "reasoning": None,
    }
    prices: dict[str, JsonValue] = {f"model-{i}": rates for i in range(7)}
    extra = 1024 - len(canonical_json(prices).encode())
    prices["model-6" + "x" * extra] = prices.pop("model-6")
    assert len(canonical_json(prices).encode()) == 1024
    return prices


async def seed_price_revisions(database: Database, previous: int, count: int) -> None:
    prices = pricing_map()
    async with database.transaction() as connection:
        await connection.execute(
            insert(price_table),
            [
                dict(
                    id=f"revision-{i:05d}",
                    namespace_id=None
                    if i % 3 == 0
                    else "old-namespace"
                    if i % 3 == 1
                    else "other-namespace",
                    model=None,
                    version=1,
                    normalization_version=1,
                    effective_from=START + timedelta(seconds=i),
                    prices={} if i % 3 == 2 else prices,
                    created_at=START,
                    updated_at=START,
                )
                for i in range(previous, count)
            ],
        )
        await connection.execute(text("ANALYZE"))


class PricingSqlCapture:
    def __init__(self, connection: AsyncConnection) -> None:
        self.connection = connection
        self.statements: list[tuple[str, tuple[object, ...]]] = []
        event.listen(connection.sync_connection, "before_cursor_execute", self.capture)

    def capture(
        self,
        connection: object,
        cursor: object,
        statement: str,
        parameters: tuple[object, ...],
        *arguments: object,
    ) -> None:
        self.statements.append((statement, parameters))

    def finish(self) -> list[tuple[str, tuple[object, ...]]]:
        event.remove(
            self.connection.sync_connection, "before_cursor_execute", self.capture
        )
        return self.statements


async def pricing_plan(
    connection: AsyncConnection,
    report: Report,
    label: str,
    statement: str,
    parameters: tuple[object, ...],
) -> None:
    kind = DatabaseKind(connection.dialect.name)
    started = time.perf_counter()
    if kind is DatabaseKind.SQLITE:
        plan = [
            str(tuple(row))
            for row in await connection.exec_driver_sql(
                f"EXPLAIN QUERY PLAN {statement}", parameters
            )
        ]
        raw = await connection.get_raw_connection()
        driver = cast(aiosqlite.Connection, raw.driver_connection)
        instructions = 0

        def count() -> int:
            nonlocal instructions
            instructions += 1
            return 0

        await driver.set_progress_handler(count, 1)
        try:
            result = await connection.exec_driver_sql(statement, parameters)
            rows = result.fetchall()
            result.close()
        finally:
            await driver.set_progress_handler(count, 0)
        record(
            report,
            f"pricing_plan_{label}",
            1,
            time.perf_counter() - started,
            kind,
            mode="ordinary",
            plan=json.dumps(plan),
            vm_instructions=instructions,
            rows=len(rows),
            selected_projection_bytes=sum(
                len(str(value).encode()) for row in rows for value in row
            ),
        )
    else:
        for mode in ("ordinary", "generic"):
            started = time.perf_counter()
            if mode == "ordinary":
                result = await connection.exec_driver_sql(
                    f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {statement}", parameters
                )
            else:
                await connection.exec_driver_sql(
                    "SET LOCAL plan_cache_mode = force_generic_plan"
                )
                await connection.exec_driver_sql(
                    f"PREPARE kora_pricing_work AS {statement}"
                )
                arguments = ", ".join(
                    str(
                        literal(value).compile(
                            dialect=connection.dialect,
                            compile_kwargs={"literal_binds": True},
                        )
                    )
                    for value in parameters
                )
                result = await connection.exec_driver_sql(
                    f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) EXECUTE "
                    f"kora_pricing_work({arguments})"
                )
            plan = result.scalar_one()
            result.close()
            record(
                report,
                f"pricing_plan_{label}",
                1,
                time.perf_counter() - started,
                kind,
                mode=mode,
                plan=json.dumps(plan),
            )
            if mode == "generic":
                await connection.exec_driver_sql("DEALLOCATE kora_pricing_work")
                await connection.exec_driver_sql("SET LOCAL plan_cache_mode = auto")
        selected = await connection.exec_driver_sql(statement, parameters)
        rows = selected.fetchall()
        selected.close()
        record(
            report,
            f"pricing_projection_{label}",
            len(rows),
            0,
            kind,
            observer_statements=1,
            selected_projection_bytes=sum(
                len(str(value).encode()) for row in rows for value in row
            ),
        )


async def pricing_history_reads(
    database: Database, report: Report, history: int
) -> None:
    kind = DatabaseKind(database.dialect.value)
    cases = (
        ("default", None, START + timedelta(seconds=history)),
        ("namespace", "old-namespace", START + timedelta(seconds=history)),
        ("empty", "other-namespace", START + timedelta(seconds=history)),
        ("before_first", None, START - timedelta(seconds=1)),
        ("endpoint", "old-namespace", START + timedelta(seconds=1)),
        ("absent", "missing-scope", START + timedelta(seconds=history)),
    )
    async with database.read_transaction() as connection:
        for label, scope, at in cases:
            for index in range(PRICING_WARMUPS + PRICING_READ_SAMPLES):
                capture = PricingSqlCapture(connection)
                started = time.perf_counter()
                revision = await read_latest_price(connection, scope, at)
                seconds = time.perf_counter() - started
                statements = capture.finish()
                assert len(statements) == 1
                if label in {"before_first", "absent"}:
                    assert revision is None
                else:
                    assert revision is not None
                    assert revision.namespace_id == scope
                    assert bool(revision.prices) == (label != "empty")
                    if label == "endpoint":
                        assert revision.id == "revision-00001"
                if index >= PRICING_WARMUPS:
                    record(
                        report,
                        f"pricing_latest_{label}_{history}",
                        1,
                        seconds,
                        kind,
                        sample=index - PRICING_WARMUPS,
                        statements=len(statements),
                        selected_map_bytes=0
                        if revision is None
                        else len(
                            canonical_json(
                                cast(
                                    JsonValue,
                                    revision.model_dump(mode="json")["prices"],
                                )
                            ).encode()
                        ),
                        decoded_revision_bytes=0
                        if revision is None
                        else len(revision.model_dump_json().encode()),
                    )
                if index == PRICING_WARMUPS:
                    await pricing_plan(
                        connection, report, f"latest_{label}_{history}", *statements[0]
                    )


async def pricing_ledger_pages(database: Database, report: Report) -> None:
    kind = DatabaseKind(database.dialect.value)
    scopes = ["old-namespace", "other-namespace", *(f"scope-{i}" for i in range(8))]
    totals = {"usage": 0, "attempt": 0}
    async with database.read_transaction() as connection:
        for scope_index, scope in enumerate(scopes):
            run_id = "old-run" if scope_index == 0 else f"ledger-{scope_index}"
            for label in ("usage", "attempt"):
                after: str | None = None
                page_index = 0
                page_size = (
                    PRICING_PAGE_SIZE if label == "usage" else PRICING_ATTEMPT_PAGE_SIZE
                )
                while True:
                    capture = PricingSqlCapture(connection)
                    started = time.perf_counter()
                    if label == "usage":
                        usage = await read_usage_page(
                            connection, scope, run_id, after, page_size
                        )
                        ids = [row.id for row in usage]
                        decoded = sum(
                            len(repr(dict(row.fields)).encode()) for row in usage
                        )
                        assert all("source" not in row.fields for row in usage)
                    else:
                        attempts = await read_attempt_page(
                            connection, scope, run_id, after, page_size
                        )
                        ids = [row.id for row in attempts]
                        decoded = sum(len(repr(row).encode()) for row in attempts)
                    elapsed = time.perf_counter() - started
                    statements = capture.finish()
                    assert len(statements) == 1
                    assert len(ids) <= page_size
                    assert len(set(ids)) == len(ids)
                    assert after is None or all(each > after for each in ids)
                    totals[label] += len(ids)
                    record(
                        report,
                        f"pricing_{label}_page",
                        len(ids),
                        elapsed,
                        kind,
                        scope=scope_index,
                        page=page_index,
                        statements=1,
                        decoded_representation_bytes=decoded,
                    )
                    await pricing_plan(
                        connection,
                        report,
                        f"{label}_{scope_index}_{page_index}",
                        *statements[0],
                    )
                    if len(ids) < page_size:
                        break
                    after = ids[-1]
                    page_index += 1
                assert page_index > 0
    assert totals == {"usage": 1001, "attempt": 100}


async def pricing_session_restored(database: Database) -> None:
    if database.dialect is Dialect.SQLITE:
        async with database.migration_connection() as connection:
            assert (
                await connection.exec_driver_sql("PRAGMA foreign_keys")
            ).scalar_one() == 1
        return
    async with database.transaction() as connection:
        await connection.exec_driver_sql("SET lock_timeout = '17s'")
        backend = await connection.scalar(text("SELECT pg_backend_pid()"))
    async with database.migration_connection() as connection:
        await connection.rollback()
        await connection.execution_options(isolation_level="AUTOCOMMIT")
        await connection.exec_driver_sql("SELECT 1")
    async with database.transaction() as connection:
        assert await connection.scalar(text("SELECT pg_backend_pid()")) == backend
        assert (
            await connection.exec_driver_sql("SHOW lock_timeout")
        ).scalar_one() == "17s"
        assert (
            await connection.exec_driver_sql("SHOW transaction_isolation")
        ).scalar_one() == "read committed"
        assert (
            await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND "
                    "pid = pg_backend_pid()"
                )
            )
            == 0
        )
        assert (
            await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_cursors WHERE statement NOT LIKE "
                    "'%pg_cursors%'"
                )
            )
            == 0
        )
        await connection.exec_driver_sql("RESET lock_timeout")


async def pricing_read_cycle(database: Database) -> None:
    async with database.read_transaction() as connection:
        exact = await read_price(connection, None, "revision-00000")
        legacy = await read_attempt_price(connection, "old-namespace", "old-attempt")
        missing = await read_price(connection, None, "missing-revision")
        absent = await read_price(connection, None, None)
        approval = await read_approval_cost(connection, "old-namespace", "old-approval")
        page = await read_usage_page(
            connection, "old-namespace", "old-run", "old-terminal", 128
        )
        amounts = {row.id: row.usd_estimate for row in page}
        with pytest.raises(CostReadRefused):
            await read_usage_source(connection, "old-namespace", "oversized-source")
        source = await read_usage_source(connection, "old-namespace", "old-zero")
    assert exact.state is HistoricalState.VERSION_1
    assert legacy.state is HistoricalState.PREDECESSOR
    assert missing.state is HistoricalState.UNMATCHED
    assert absent.state is HistoricalState.ABSENT
    assert approval.state is HistoricalState.PREDECESSOR
    assert amounts["old-zero"] == Decimal(0)
    assert amounts["oversized-source"] is None
    assert source is not None


async def pricing_quiescence(
    database: Database, balance: CheckoutBalance, tasks: int
) -> None:
    await until(lambda: database.units_in_flight() == 0 and balance.outstanding == 0)
    assert database.writer_is_free()
    assert len(asyncio.all_tasks()) == tasks
    async with database.read_transaction() as connection:
        if database.dialect is Dialect.POSTGRESQL:
            assert (
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_cursors WHERE statement NOT LIKE "
                        "'%pg_cursors%'"
                    )
                )
                == 0
            )
            assert (
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = "
                        "current_database() AND pid <> pg_backend_pid() AND state "
                        "<> 'idle'"
                    )
                )
                == 0
            )


async def pricing_release_cycles(
    database: Database, report: Report, traced: bool
) -> None:
    kind = DatabaseKind(database.dialect.value)
    balance = CheckoutBalance(database)
    tasks = len(asyncio.all_tasks())
    for _ in range(PRICING_WARMUPS):
        await pricing_read_cycle(database)
    await pricing_session_restored(database)
    await pricing_quiescence(database, balance, tasks)
    gc.collect()
    descriptors = open_descriptors()
    threads = threading.active_count()
    checkpoints = array("d", [0.0] * (4 * len(PRICING_CHECKPOINT_FIELDS)))
    if traced:
        tracemalloc.start()
    baseline_snapshot = tracemalloc.take_snapshot() if traced else None
    started = time.perf_counter()
    for index in range(PRICING_RELEASE_CYCLES + 1):
        if index:
            await pricing_read_cycle(database)
        if index % (PRICING_RELEASE_CYCLES // 3) == 0:
            await pricing_quiescence(database, balance, tasks)
            gc.collect()
            capture_pricing_checkpoint(
                checkpoints,
                index // (PRICING_RELEASE_CYCLES // 3),
                index,
                started,
                traced,
            )
            assert open_descriptors() == descriptors
            assert threading.active_count() == threads
    retained = (
        []
        if baseline_snapshot is None
        else tracemalloc.take_snapshot().compare_to(baseline_snapshot, "lineno")
    )
    if traced:
        tracemalloc.stop()
    publish_pricing_checkpoints(
        report, checkpoints, kind, "release_heap" if traced else "release_native"
    )
    for item in retained[:10]:
        record(
            report,
            "pricing_heap_retainer",
            item.count_diff,
            0,
            kind,
            size_diff=item.size_diff,
            location=str(item.traceback),
        )


async def pricing_failure_recovery(settings: DatabaseSettings, report: Report) -> None:
    directory_baseline = pricing_directory_baseline(settings)
    kind = DatabaseKind(settings.dialect.value)
    database = Database(settings)
    try:
        await pricing_dataset(database)
        async with database.transaction() as connection:
            if kind is DatabaseKind.SQLITE:
                await connection.exec_driver_sql(
                    "CREATE TABLE pricing_blocker (price_id VARCHAR(26) REFERENCES "
                    "price_table(id))"
                )
                await connection.exec_driver_sql(
                    "INSERT INTO pricing_blocker VALUES ('old-price')"
                )
            else:
                await connection.exec_driver_sql(
                    "ALTER TABLE price_table ADD COLUMN version INTEGER"
                )
                await connection.exec_driver_sql(
                    "ALTER TABLE price_table ADD COLUMN normalization_version INTEGER"
                )
                await connection.exec_driver_sql(
                    "UPDATE price_table SET version = 1 WHERE id = 'old-price'"
                )
                await connection.exec_driver_sql(
                    "ALTER TABLE price_table ADD CONSTRAINT "
                    "ck_price_table_revision CHECK (" + price_check() + ") NOT VALID"
                )
        watch = PricingMigrationWatch(report, kind, settings, "refused")
        await watch.prepare()
        with pytest.raises(DBAPIError):
            await upgrade_database(settings, watch.open)
        await watch.finish()
        await pricing_storage(database, report, "failed")
        async with database.transaction() as connection:
            assert await connection.run_sync(current_revision) == "0001"
            if kind is DatabaseKind.SQLITE:
                assert (
                    await connection.scalar(
                        text(
                            "SELECT count(*) FROM sqlite_master WHERE name LIKE "
                            "'_alembic_tmp_%'"
                        )
                    )
                    == 0
                )
                await connection.exec_driver_sql("DROP TABLE pricing_blocker")
            else:
                assert (
                    await connection.scalar(
                        text(
                            "SELECT convalidated FROM pg_constraint WHERE conname "
                            "= 'ck_price_table_revision'"
                        )
                    )
                    is False
                )
                await connection.exec_driver_sql(
                    "UPDATE price_table SET version = NULL WHERE id = 'old-price'"
                )
        repaired = PricingMigrationWatch(report, kind, settings, "recovered")
        await repaired.prepare()
        assert (await upgrade_database(settings, repaired.open)).steps == 1
        await repaired.finish()
        await pricing_storage(database, report, "recovered")
        assert (await upgrade_database(settings)).steps == 0
    finally:
        await database.close()
    pricing_storage_released(
        settings, report, "pricing_failure_recovery", directory_baseline
    )


async def pricing_interruption(settings: DatabaseSettings, report: Report) -> None:
    directory_baseline = pricing_directory_baseline(settings)
    kind = DatabaseKind(settings.dialect.value)
    database = Database(settings)
    contenders: list[Database] = []
    ledgers: list[ConnectionLedger] = []
    watches: list[PricingMigrationWatch] = []

    def open_contender(value: DatabaseSettings) -> Database:
        watch = PricingMigrationWatch(report, kind, value, "contended_cancel")
        watches.append(watch)
        contender = watch.open(value)
        contenders.append(contender)
        ledgers.append(ConnectionLedger(contender))
        return contender

    try:
        await pricing_dataset(database)
        if kind is DatabaseKind.POSTGRESQL:
            async with database.transaction() as connection:
                await connection.exec_driver_sql(
                    "CREATE INDEX ix_price_table_default_effective ON price_table "
                    "(model)"
                )
            with pytest.raises(RuntimeError, match="incompatible schema"):
                await upgrade_database(settings)
            async with database.transaction() as connection:
                await connection.exec_driver_sql(
                    "DROP INDEX ix_price_table_default_effective"
                )
        async with database.transaction() as holder:
            await holder.exec_driver_sql(
                "UPDATE price_table SET model = model WHERE id = 'old-price'"
            )
            first = asyncio.create_task(upgrade_database(settings, open_contender))
            if kind is DatabaseKind.POSTGRESQL:
                invalid = await wait_for_index(database)
            else:
                await until(
                    lambda: bool(contenders) and contenders[0].units_in_flight() == 1
                )
                invalid = 0
            second = asyncio.create_task(upgrade_database(settings, open_contender))
            await until(
                lambda: len(contenders) == 2 and contenders[1].units_in_flight() == 1
            )
            if kind is DatabaseKind.POSTGRESQL:
                async with asyncio.timeout(4):
                    waiting = 0
                    while not waiting:
                        async with database.read_transaction() as connection:
                            waiting = await connection.scalar(
                                text(
                                    "SELECT count(*) FROM pg_locks WHERE "
                                    "locktype = 'advisory' AND NOT granted"
                                )
                            )
            started = time.perf_counter()
            second.cancel()
            first.cancel()
            if kind is DatabaseKind.POSTGRESQL:
                await asyncio.gather(first, second, return_exceptions=True)
        with pytest.raises(asyncio.CancelledError):
            await second
        with pytest.raises(asyncio.CancelledError):
            await first
        for watch in watches:
            await watch.finish()
        record(
            report,
            "pricing_contended_cancel",
            2,
            time.perf_counter() - started,
            kind,
        )
        assert all(db.units_in_flight() == 0 for db in contenders)
        assert all(
            ledger.outstanding == 0 and ledger.opened == ledger.closed
            for ledger in ledgers
        )
        if kind is DatabaseKind.POSTGRESQL:
            async with database.read_transaction() as connection:
                assert (
                    await connection.scalar(
                        text("SELECT indisvalid FROM pg_index WHERE indexrelid = :oid"),
                        {"oid": invalid},
                    )
                    is False
                )
        await pricing_storage(database, report, "interrupted")
        watch = PricingMigrationWatch(report, kind, settings, "interruption_repair")
        await watch.prepare()
        assert (await upgrade_database(settings, watch.open)).steps == 1
        await watch.finish()
        await pricing_storage(database, report, "interruption_repaired")
        await pricing_session_restored(database)
        assert (await upgrade_database(settings)).steps == 0
    finally:
        await database.close()
        for contender in contenders:
            await contender.close()
    pricing_storage_released(
        settings, report, "pricing_interruption", directory_baseline
    )


async def pricing_close_recovery(settings: DatabaseSettings, report: Report) -> None:
    directory_baseline = pricing_directory_baseline(settings)
    kind = DatabaseKind(settings.dialect.value)
    database = Database(settings)
    ledger = ConnectionLedger(database)
    try:
        await pricing_dataset(database)
        started = time.perf_counter()
        with pytest.raises(DatabaseClosedError):
            await close_before_migration(database)
        assert database.units_in_flight() == 0
        assert ledger.outstanding == 0
        assert ledger.opened == ledger.closed
        observer = Database(settings)
        try:
            async with observer.read_transaction() as connection:
                assert await connection.run_sync(current_revision) == "0001"
            assert (await upgrade_database(settings)).steps == 1
            await pricing_session_restored(observer)
        finally:
            await observer.close()
        record(report, "pricing_close_recovery", 1, time.perf_counter() - started, kind)
    finally:
        await database.close()
    pricing_storage_released(
        settings, report, "pricing_close_recovery", directory_baseline
    )


async def pricing_native_run(settings: list[DatabaseSettings], report: Report) -> None:
    directory_baseline = pricing_directory_baseline(settings[0])
    kind = DatabaseKind(settings[0].dialect.value)
    database = Database(settings[0])
    try:
        await pricing_dataset(database)
    finally:
        await database.close()
    watch = PricingMigrationWatch(report, kind, settings[0], "populated")
    await watch.prepare()
    assert (await upgrade_database(settings[0], watch.open)).steps == 1
    await watch.finish()
    del watch
    database = Database(settings[0])
    ledger = ConnectionLedger(database)
    try:
        await pricing_storage(database, report, "populated")
        previous = 0
        for size in PRICING_HISTORY_SIZES:
            await seed_price_revisions(database, previous, size)
            await pricing_history_reads(database, report, size)
            previous = size
        await pricing_ledger_pages(database, report)
        async with database.transaction() as connection:
            await connection.execute(
                usage_record.update()
                .where(usage_record.c.id == "oversized-source")
                .values(usd_estimate=None)
            )
        await pricing_release_cycles(database, report, False)
        await pricing_session_restored(database)
    finally:
        await database.close()
    assert ledger.outstanding == 0
    assert ledger.opened == ledger.closed
    pricing_storage_released(settings[0], report, "native_final", directory_baseline)
    del database
    await pricing_lifecycle_cycles(settings[1:13], report, False)


def pricing_child() -> None:
    mode = os.environ["KORA_PRICING_MODE"]
    settings = [
        parse_database_url(value)
        for value in cast(list[str], json.loads(os.environ["KORA_PRICING_DATABASES"]))
    ]
    report = new_report()
    pricing_environment(report, mode)
    initial_threads, initial_descriptors = settled_counts()

    async def warm_connection() -> None:
        database = Database(settings[0])
        try:
            async with database.read_transaction() as connection:
                assert await connection.scalar(text("SELECT 1")) == 1
        finally:
            await database.close()

    asyncio.run(warm_connection())
    gc.collect()
    threads, descriptors = settled_counts()
    report.environment.update(
        process_initial_threads=initial_threads,
        process_initial_descriptors=initial_descriptors,
        process_warmed_threads=threads,
        process_warmed_descriptors=descriptors,
    )
    started = time.perf_counter()

    async def run() -> None:
        if mode == "native":
            await pricing_native_run(settings, report)
        else:
            database = Database(settings[0])
            ledger = ConnectionLedger(database)
            try:
                await pricing_release_cycles(database, report, True)
            finally:
                await database.close()
            assert ledger.opened == ledger.closed
            assert ledger.outstanding == 0
            del database
            await pricing_lifecycle_cycles(settings[13:25], report, True)
        assert len(asyncio.all_tasks()) == 1

    try:
        asyncio.run(run())
        gc.collect()
        after_threads, after_descriptors = settled_counts()
        report.environment.update(
            process_threads_before=threads,
            process_threads_after=after_threads,
            process_descriptors_before=descriptors,
            process_descriptors_after=after_descriptors,
        )
        assert after_threads == threads
        assert after_descriptors == descriptors
        report.gates["pricing_child_resources_released"] = True
    finally:
        report.durations["pricing_child_total"] = time.perf_counter() - started
        Path(os.environ["KORA_PRICING_OUTPUT"]).write_text(report.to_json())


def merge_pricing_report(
    report: Report,
    path: Path,
    kind: DatabaseKind,
    mode: str,
    repeat: int,
    cycle: int | None = None,
) -> None:
    payload = cast(dict[str, object], json.loads(path.read_text()))
    environment = cast(
        dict[str, float | int | bool | str | None], payload["environment"]
    )
    prefix = f"{kind.value}_pricing_{mode}_{repeat}"
    if cycle is not None:
        prefix += f"_cycle_{cycle}"
    for key, value in environment.items():
        report.environment[f"{prefix}_{key}"] = value
    report.environment[f"{prefix}_revision"] = cast(str, payload["revision"])
    for raw in cast(list[dict[str, object]], payload["samples"]):
        report.samples.append(
            Sample(
                cast(str, raw["workload"]),
                repeat,
                cast(int, raw["count"]),
                cast(float, raw["seconds"]),
                {
                    key: cast(float | int | bool | str | None, value)
                    for key, value in raw.items()
                    if key
                    not in {"workload", "repeat", "count", "seconds", "us_per_record"}
                }
                | {"pricing_run": repeat, "pricing_mode": mode}
                | ({} if cycle is None else {"pricing_cycle": cycle}),
            )
        )
    for name, passed in cast(dict[str, bool], payload["gates"]).items():
        gate(report, f"{name}_{mode}_{repeat}", kind, passed)
    for name, seconds in cast(dict[str, float], payload["durations_seconds"]).items():
        report.durations[f"{kind.value}_{name}_{mode}_{repeat}"] = seconds


async def pricing_process_runs(
    report: Report, kind: DatabaseKind, factory: DatabaseFactory
) -> None:
    for repeat in range(PRICING_RUNS):
        settings = [await factory.predecessor() for _ in range(25)]
        try:
            for mode in ("native", "heap"):
                output = factory.directory / f"pricing-{mode}-{repeat}.json"
                environment = {
                    "KORA_PRICING_MODE": mode,
                    "KORA_PRICING_OUTPUT": str(output),
                    "KORA_PRICING_DATABASES": json.dumps(
                        [
                            value.url.render_as_string(hide_password=False)
                            for value in settings
                        ]
                    ),
                    "KORA_MEASUREMENT_REVISION": report.revision,
                    "PYTHONPATH": os.pathsep.join(
                        (str(Path.cwd()), str(Path.cwd() / "src"))
                    ),
                }
                started = time.perf_counter()
                result = await asyncio.to_thread(
                    run_python,
                    [
                        "-c",
                        "from tests.measurements.test_metadata_database_lifetime "
                        "import pricing_child; pricing_child()",
                    ],
                    environment,
                )
                assert result.code == 0, result.stderr.decode()
                assert result.stdout == b""
                merge_pricing_report(report, output, kind, mode, repeat)
                record(
                    report,
                    f"pricing_process_{mode}",
                    1,
                    time.perf_counter() - started,
                    kind,
                    repeat=repeat,
                    exit_code=result.code,
                )
        finally:
            for value in settings:
                await factory.discard(value)


async def pricing_lifecycle_cycles(
    settings: list[DatabaseSettings], report: Report, traced: bool
) -> None:
    assert len(settings) == 12
    kind = DatabaseKind(settings[0].dialect.value)
    warmup_start = len(report.samples)
    await pricing_failure_recovery(settings[0], report)
    await pricing_interruption(settings[1], report)
    await pricing_close_recovery(settings[2], report)
    await pricing_lease_outcomes(settings[2], report)
    for sample in report.samples[warmup_start:]:
        sample.extra["pricing_cycle"] = 0
    baseline_threads = {
        thread.ident
        for thread in threading.enumerate()
        if not thread.name.startswith("asyncio_")
    }
    baseline_descriptors = open_descriptors()
    baseline_tasks = len(asyncio.all_tasks())
    mode = "heap" if traced else "native"
    phase_environment = dict(report.environment)
    report.environment[f"{mode}_reflection_cache_before"] = json.dumps(
        pricing_reflection_cache_info()
    )
    output = Path(os.environ["KORA_PRICING_OUTPUT"])
    fragments = [
        output.with_suffix(f".{mode}-cycle-{cycle}.json") for cycle in range(3)
    ]
    checkpoints = array("d", [0.0] * (4 * len(PRICING_CHECKPOINT_FIELDS)))
    if traced:
        tracemalloc.start()
    baseline_snapshot = tracemalloc.take_snapshot() if traced else None
    started = time.perf_counter()
    for cycle in range(4):
        if cycle:
            phase_report = new_report()
            phase_report.environment.update(
                phase_environment,
                pricing_mode=mode + "_migration_cycle",
                pricing_cycle=cycle,
            )
            first = cycle * 3
            await pricing_failure_recovery(settings[first], phase_report)
            await pricing_interruption(settings[first + 1], phase_report)
            await pricing_close_recovery(settings[first + 2], phase_report)
            await pricing_lease_outcomes(settings[first + 2], phase_report)
            await until(
                lambda: (
                    {
                        thread.ident
                        for thread in threading.enumerate()
                        if not thread.name.startswith("asyncio_")
                    }
                    == baseline_threads
                )
            )
            assert open_descriptors() == baseline_descriptors
            assert len(asyncio.all_tasks()) == baseline_tasks
            gc.collect()
            raw_heap, raw_peak = tracemalloc.get_traced_memory() if traced else (0, 0)
            raw_rss = 0 if traced else rss_bytes()
            sample_count = len(phase_report.samples)
            environment_count = len(phase_report.environment)
            if traced:
                tracemalloc.reset_peak()
            serialization_started = time.perf_counter()
            encoded = phase_report.to_json()
            encoded_bytes = len(encoded.encode())
            fragments[cycle - 1].write_text(encoded)
            serialization_seconds = time.perf_counter() - serialization_started
            serialization_peak = tracemalloc.get_traced_memory()[1] if traced else 0
            del encoded, phase_report
            gc.collect()
            capture_pricing_checkpoint(checkpoints, cycle, cycle, started, traced)
            offset = cycle * len(PRICING_CHECKPOINT_FIELDS)
            for name, value in (
                ("unfiltered_heap_bytes", raw_heap),
                ("unfiltered_peak_heap_bytes", raw_peak),
                ("unfiltered_rss_bytes", raw_rss),
                ("collector_samples", sample_count),
                ("collector_environment_entries", environment_count),
                ("collector_serialized_bytes", encoded_bytes),
                ("collector_serialization_seconds", serialization_seconds),
                ("collector_serialization_peak_heap_bytes", serialization_peak),
                (
                    "collector_serialization_peak_increment_bytes",
                    max(0, serialization_peak - raw_heap),
                ),
            ):
                checkpoints[offset + PRICING_CHECKPOINT_FIELDS.index(name)] = value
            if traced:
                tracemalloc.reset_peak()
        else:
            gc.collect()
            capture_pricing_checkpoint(checkpoints, cycle, cycle, started, traced)
        assert open_descriptors() == baseline_descriptors
        assert len(asyncio.all_tasks()) == baseline_tasks
    retained = (
        []
        if baseline_snapshot is None
        else tracemalloc.take_snapshot().compare_to(baseline_snapshot, "lineno")
    )
    if traced:
        tracemalloc.stop()
    report.environment[f"{mode}_reflection_cache_after"] = json.dumps(
        pricing_reflection_cache_info()
    )
    publish_pricing_checkpoints(report, checkpoints, kind, f"migration_release_{mode}")
    for item in retained[:20]:
        record(
            report,
            "pricing_migration_heap_retainer",
            item.count_diff,
            0,
            kind,
            size_diff=item.size_diff,
            location=str(item.traceback),
        )
    for cycle, path in enumerate(fragments, 1):
        merge_pricing_report(report, path, kind, mode, 0, cycle)


def regular_descriptor_bytes(unlinked_only: bool = False) -> int:
    files: dict[tuple[int, int], int] = {}
    for name in os.listdir("/dev/fd"):
        descriptor = int(name)
        if descriptor < 3:
            continue
        try:
            value = os.fstat(descriptor)
        except OSError as error:
            if error.errno != errno.EBADF:
                raise
            continue
        if stat.S_ISREG(value.st_mode) and (not unlinked_only or value.st_nlink == 0):
            files[value.st_dev, value.st_ino] = value.st_size
    return sum(files.values())


async def pricing_lease_outcomes(settings: DatabaseSettings, report: Report) -> None:
    database = Database(narrow_settings(settings, 1))
    ledger = ConnectionLedger(database)
    balance = CheckoutBalance(database)
    kind = DatabaseKind(settings.dialect.value)
    try:
        for outcome in ("success", "failure", "cancel"):
            async with database.transaction() as connection:
                if kind is DatabaseKind.POSTGRESQL:
                    await connection.exec_driver_sql("SET lock_timeout = '17s'")
                    backend = int(
                        await connection.scalar(text("SELECT pg_backend_pid()"))
                    )
                else:
                    backend = id(
                        (await connection.get_raw_connection()).driver_connection
                    )
            watch = StatementWatch(
                database, "pg_sleep" if kind is DatabaseKind.POSTGRESQL else "counter"
            )

            async def operation(outcome: str = outcome) -> None:
                async with database.migration_connection() as connection:
                    if kind is DatabaseKind.POSTGRESQL:
                        await connection.rollback()
                        await connection.execution_options(isolation_level="AUTOCOMMIT")
                    if outcome == "failure":
                        await connection.exec_driver_sql(
                            "SELECT * FROM absent_pricing_measurement_relation"
                        )
                    elif outcome == "cancel":
                        await connection.exec_driver_sql(slow_statement(kind))
                    else:
                        await connection.exec_driver_sql("SELECT 1")

            started = time.perf_counter()
            if outcome == "failure":
                with pytest.raises(DBAPIError):
                    await operation()
            elif outcome == "cancel":
                task = asyncio.create_task(operation())
                await asyncio.wait_for(watch.started.wait(), CLOSE_WAIT_SECONDS)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                await operation()
            assert database.units_in_flight() == 0
            assert balance.outstanding == 0
            async with database.transaction() as connection:
                if kind is DatabaseKind.POSTGRESQL:
                    reused = int(
                        await connection.scalar(text("SELECT pg_backend_pid()"))
                    )
                    if reused == backend:
                        assert (
                            await connection.exec_driver_sql("SHOW lock_timeout")
                        ).scalar_one() == "17s"
                    else:
                        assert balance.invalidations > 0
                        assert (
                            await connection.scalar(
                                text(
                                    "SELECT count(*) FROM pg_stat_activity "
                                    "WHERE pid = :pid"
                                ),
                                {"pid": backend},
                            )
                            == 0
                        )
                    assert (
                        await connection.exec_driver_sql("SHOW transaction_isolation")
                    ).scalar_one() == "read committed"
                    assert (
                        await connection.scalar(
                            text(
                                "SELECT count(*) FROM pg_locks WHERE pid = "
                                "pg_backend_pid() AND locktype = 'advisory'"
                            )
                        )
                        == 0
                    )
                    await connection.exec_driver_sql("RESET lock_timeout")
                else:
                    reused = id(
                        (await connection.get_raw_connection()).driver_connection
                    )
                    assert (
                        await connection.exec_driver_sql("PRAGMA foreign_keys")
                    ).scalar_one() == 1
                    assert reused == backend or balance.invalidations > 0
            record(
                report,
                f"pricing_lease_{outcome}",
                1,
                time.perf_counter() - started,
                kind,
                original_backend=backend,
                reused_backend=reused,
                invalidations=balance.invalidations,
                outstanding=balance.outstanding,
            )
    finally:
        await database.close()
    assert ledger.outstanding == 0
    assert ledger.opened == ledger.closed


PRICING_CHECKPOINT_FIELDS: tuple[str, ...] = (
    "checkpoint",
    "seconds",
    "current_heap_bytes",
    "peak_heap_bytes",
    "current_rss_bytes",
    "process_peak_rss_bytes",
    "descriptors",
    "threads",
    "executor_threads",
    "tasks",
    "sqlite_live_cursors",
    "live_database_objects",
    "live_pricing_observers",
    "live_report_objects",
    "postgresql_reflection_cache_entries",
    "postgresql_reflection_cache_capacity",
    "live_postgresql_dialects",
    "unfiltered_heap_bytes",
    "unfiltered_peak_heap_bytes",
    "unfiltered_rss_bytes",
    "collector_samples",
    "collector_environment_entries",
    "collector_serialized_bytes",
    "collector_serialization_seconds",
    "collector_serialization_peak_heap_bytes",
    "collector_serialization_peak_increment_bytes",
)


def capture_pricing_checkpoint(
    checkpoints: array[float], position: int, cycle: int, started: float, traced: bool
) -> None:
    current, peak = tracemalloc.get_traced_memory() if traced else (0, 0)
    values = (
        cycle,
        time.perf_counter() - started,
        current,
        peak,
        0 if traced else rss_bytes(),
        0 if traced else peak_rss_bytes(),
        open_descriptors(),
        threading.active_count(),
        sum(thread.name.startswith("asyncio_") for thread in threading.enumerate()),
        len(asyncio.all_tasks()),
        sum(isinstance(item, sqlite3.Cursor) for item in gc.get_objects()),
        sum(isinstance(item, Database) for item in gc.get_objects()),
        sum(isinstance(item, PricingMigrationWatch) for item in gc.get_objects()),
        sum(isinstance(item, Report) for item in gc.get_objects()),
        sum(info[3] for info in pricing_reflection_cache_info().values()),
        sum(info[2] for info in pricing_reflection_cache_info().values()),
        sum(isinstance(item, PGDialect) for item in gc.get_objects()),
    )
    offset = position * len(PRICING_CHECKPOINT_FIELDS)
    for index, value in enumerate(values):
        checkpoints[offset + index] = value


def publish_pricing_checkpoints(
    report: Report, checkpoints: array[float], kind: DatabaseKind, name: str
) -> None:
    for position in range(4):
        offset = position * len(PRICING_CHECKPOINT_FIELDS)
        values = {
            key: checkpoints[offset + index]
            for index, key in enumerate(PRICING_CHECKPOINT_FIELDS)
        }
        report.samples.append(
            Sample(
                f"{kind.value}_pricing_{name}",
                0,
                int(values["checkpoint"]),
                values.pop("seconds"),
                {**values, "collector_buffer_bytes": sys.getsizeof(checkpoints)},
            )
        )


class PricingCacheInfo(Protocol):
    def cache_info(self) -> tuple[int, int, int, int]: ...


def pricing_reflection_cache_info() -> dict[str, tuple[int, int, int, int]]:
    names = (
        "_has_multi_table_query",
        "_table_options_query",
        "_columns_query",
        "_table_oids_query",
        "_foreing_key_query",
        "_comment_query",
        "_check_constraint_query",
        "_enum_query",
        "_domain_query",
    )
    return {
        name: cast(PricingCacheInfo, getattr(PGDialect, name)).cache_info()
        for name in names
    }
