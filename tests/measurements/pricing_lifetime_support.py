from __future__ import annotations

import asyncio
import errno
import gc
import hashlib
import inspect
import json
import math
import os
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
from array import array
from collections import Counter
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal
from importlib.metadata import version
from pathlib import Path
from types import FrameType
from typing import Literal, Protocol, cast

import aiosqlite
import pytest
from pydantic import BaseModel, ConfigDict
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
from testcontainers.core.labels import LABEL_SESSION_ID

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
from kora.db.migrations import current_revision, upgrade_database
from kora.db.tables import (
    approval_request,
    attempt,
    metadata,
    price_table,
    usage_record,
)
from kora.db.types import JsonValue, canonical_json
from kora.ir.json import parse_json_object
from tests.cel_eval.children import full_path
from tests.db.closing import (
    ConnectionLedger,
    narrow_settings,
    until,
)
from tests.db.seed import START, seed_pricing_measurement_history
from tests.db.test_pricing_migration import (
    close_before_migration,
    price_check,
    wait_for_index,
)
from tests.measurements.cel_measurement_process import run_process
from tests.measurements.harness import (
    ExtraValue,
    Report,
    Sample,
    new_report,
    open_descriptors,
    settled_counts,
)
from tests.measurements.ir_config_lifetime import peak_rss_bytes, rss_bytes
from tests.measurements.metadata_lifetime_support import (
    CheckoutBalance,
    DatabaseFactory,
    StatementWatch,
    gate,
    record,
    slow_statement,
)
from tests.support.databases import (
    POSTGRES_IMAGE,
    DatabaseKind,
    PostgresServer,
    sqlite_settings,
)

CLOSE_WAIT_SECONDS: float = 10.0


class PricingDatabaseFactory(DatabaseFactory):
    def __init__(
        self, kind: DatabaseKind, directory: Path, server: PostgresServer | None
    ) -> None:
        super().__init__(kind, directory, server)
        self.pricing_template: DatabaseSettings | None = None

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


PRICING_RUNS: int = 3
PRICING_WARMUPS: int = 3
PRICING_READ_SAMPLES: int = 30
PRICING_HISTORY_SIZES: tuple[int, ...] = (100, 1_000, 10_000)
PRICING_RELEASE_CYCLES: int = 30
PRICING_PAGE_SIZE: int = 32
PRICING_ATTEMPT_PAGE_SIZE: int = 4


def pricing_environment(report: Report, mode: str) -> None:
    report.environment.update(pricing_origins())
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
        finalization = asyncio.create_task(self.finalize())
        failures: list[BaseException] = []
        while not finalization.done():
            try:
                await asyncio.shield(finalization)
            except asyncio.CancelledError as error:
                failures.append(error)
                for task in self.writer_tasks:
                    if not task.done():
                        task.cancel()
        failures.extend(finalization.result())
        if len(failures) == 1:
            raise failures[0]
        if failures:
            raise BaseExceptionGroup("pricing watcher finalization failed", failures)

    async def settle_writers(self) -> None:
        deadline_error: TimeoutError | None = None
        try:
            outcomes = await asyncio.wait_for(
                asyncio.gather(*self.writer_tasks, return_exceptions=True),
                CLOSE_WAIT_SECONDS,
            )
        except TimeoutError as error:
            deadline_error = error
            outcomes = await asyncio.gather(*self.writer_tasks, return_exceptions=True)
        failures = [value for value in outcomes if isinstance(value, BaseException)]
        if deadline_error is not None:
            failures.append(deadline_error)
        if len(failures) == 1:
            raise failures[0]
        if failures:
            raise BaseExceptionGroup("pricing writer operations failed", failures)

    async def finalize(self) -> list[BaseException]:
        outcomes: list[object] = list(
            await asyncio.gather(self.settle_writers(), return_exceptions=True)
        )
        for database in self.writers.values():
            outcomes.extend(
                await asyncio.gather(database.close(), return_exceptions=True)
            )
        self.finished.set()
        outcomes.extend(
            await asyncio.gather(self.join_observer(), return_exceptions=True)
        )
        outcomes.extend(
            await asyncio.gather(self.verify_release(), return_exceptions=True)
        )
        outcomes.extend(
            await asyncio.gather(self.detach_listeners(), return_exceptions=True)
        )
        return [value for value in outcomes if isinstance(value, BaseException)]

    async def join_observer(self) -> None:
        assert self.observer is not None
        await asyncio.wait_for(self.observer, CLOSE_WAIT_SECONDS)

    async def verify_release(self) -> None:
        assert all(task.done() for task in self.writer_tasks)
        assert all(
            database.units_in_flight() == 0 for database in self.writers.values()
        )
        assert all(
            ledger.opened == ledger.closed and ledger.outstanding == 0
            for ledger in self.writer_ledgers
        )
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

    async def detach_listeners(self) -> None:
        assert self.database is not None
        engine = self.database.writer_engine.sync_engine

        async def remove(name: str, callback: Callable[..., None]) -> None:
            event.remove(engine, name, callback)

        outcomes = await asyncio.gather(
            remove("before_cursor_execute", self.before),
            remove("after_cursor_execute", self.after),
            remove("commit", self.commit),
            remove("rollback", self.rollback),
            remove("begin", self.begin),
            remove("checkout", self.checkout),
            remove("checkin", self.completed_transaction),
            remove("handle_error", self.error),
            return_exceptions=True,
        )
        failures = [value for value in outcomes if isinstance(value, BaseException)]
        if len(failures) == 1:
            raise failures[0]
        if failures:
            raise BaseExceptionGroup("pricing listener detachment failed", failures)


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
    async with asyncio.timeout(CLOSE_WAIT_SECONDS):
        await until(
            lambda: database.units_in_flight() == 0 and balance.outstanding == 0
        )
        assert database.writer_is_free()
        assert len(asyncio.all_tasks()) == tasks
        while True:
            async with database.read_transaction() as connection:
                if database.dialect is Dialect.SQLITE:
                    return
                assert (
                    await connection.scalar(
                        text(
                            "SELECT count(*) FROM pg_cursors WHERE statement NOT LIKE "
                            "'%pg_cursors%'"
                        )
                    )
                    == 0
                )
                active = await connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = "
                        "current_database() AND pid <> pg_backend_pid() AND state "
                        "<> 'idle'"
                    )
                )
            if active == 0:
                return


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
        assert (await upgrade_database(settings, repaired.open)).steps == 5
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
        assert (await upgrade_database(settings, watch.open)).steps == 5
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
            assert (await upgrade_database(settings)).steps == 5
            await pricing_session_restored(observer)
        finally:
            await observer.close()
        record(report, "pricing_close_recovery", 1, time.perf_counter() - started, kind)
    finally:
        await database.close()
    pricing_storage_released(
        settings, report, "pricing_close_recovery", directory_baseline
    )


async def pricing_native_run(
    settings: list[DatabaseSettings], report: Report, output: Path
) -> None:
    directory_baseline = pricing_directory_baseline(settings[0])
    kind = DatabaseKind(settings[0].dialect.value)
    database = Database(settings[0])
    try:
        await pricing_dataset(database)
    finally:
        await database.close()
    watch = PricingMigrationWatch(report, kind, settings[0], "populated")
    await watch.prepare()
    assert (await upgrade_database(settings[0], watch.open)).steps == 5
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
    await pricing_lifecycle_cycles(settings[1:13], report, False, output)


def pricing_child(request: PricingRequest) -> None:
    mode = request.mode
    settings = [parse_database_url(value) for value in request.databases]
    report = new_report()
    report.revision = request.revision
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
            await pricing_native_run(settings, report, request.output)
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
            await pricing_lifecycle_cycles(
                settings[13:25], report, True, request.output
            )
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
        write_pricing_report(request.output, report, CHILD_LIMIT)


def merge_pricing_report(
    report: Report,
    path: Path,
    kind: DatabaseKind,
    mode: str,
    repeat: int,
    cycle: int | None = None,
) -> None:
    raw = read_pricing_bytes(path, CHILD_LIMIT if cycle is None else FRAGMENT_LIMIT)
    payload = cast(
        dict[str, object], PricingPayload.model_validate_json(raw).model_dump()
    )
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


async def pricing_lifecycle_cycles(
    settings: list[DatabaseSettings], report: Report, traced: bool, output: Path
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
            phase_report.revision = report.revision
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
            write_pricing_bytes(fragments[cycle - 1], encoded.encode(), FRAGMENT_LIMIT)
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


def pricing_live_objects(objects: list[object]) -> dict[type[object], int]:
    types = Counter(type(item) for item in objects)
    return {
        category: sum(
            count
            for value_type, count in types.items()
            if issubclass(value_type, category)
        )
        for category in (
            sqlite3.Cursor,
            Database,
            PricingMigrationWatch,
            Report,
            PGDialect,
        )
    }


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
    )
    live_objects = pricing_live_objects(gc.get_objects())
    values += (
        live_objects[sqlite3.Cursor],
        live_objects[Database],
        live_objects[PricingMigrationWatch],
        live_objects[Report],
        sum(info[3] for info in pricing_reflection_cache_info().values()),
        sum(info[2] for info in pricing_reflection_cache_info().values()),
        live_objects[PGDialect],
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


PARENT_LIMIT: int = 16 * 1024 * 1024
CHILD_LIMIT: int = 4 * 1024 * 1024
FRAGMENT_LIMIT: int = 512 * 1024
STREAM_LIMIT: int = 256 * 1024
MANIFEST_LIMIT: int = 1024 * 1024
COMPLETION_LIMIT: int = 64 * 1024
AGGREGATE_LIMIT: int = 96 * 1024 * 1024
REQUEST_LIMIT: int = 64 * 1024
ROOT: Path = Path(__file__).resolve().parents[2]


class PricingEvidenceError(ValueError):
    pass


class PricingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mode: Literal["native", "heap"]
    databases: list[str]
    output: Path
    revision: str


class PricingPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    started_at: str
    platform: str
    python: str
    revision: str
    environment: dict[str, ExtraValue]
    summary: dict[str, dict[str, float]]
    gates: dict[str, bool]
    durations_seconds: dict[str, float]
    samples: list[dict[str, ExtraValue]]


def read_pricing_bytes(path: Path, limit: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise PricingEvidenceError("evidence is not a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return stream.read(limit + 1)
    finally:
        os.close(descriptor)


def write_pricing_bytes(path: Path, content: bytes, limit: int) -> None:
    if len(content) > limit:
        raise PricingEvidenceError("evidence exceeds its write limit")
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_NONBLOCK,
        0o600,
    )
    with os.fdopen(descriptor, "wb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise PricingEvidenceError("evidence target is not a regular file")
        stream.write(content)


def write_pricing_report(path: Path, report: Report, limit: int) -> None:
    write_pricing_bytes(path, report.to_json().encode(), limit)


def redact_pricing_bytes(content: bytes, secrets: tuple[bytes, ...]) -> bytes:
    redacted = content
    for secret in sorted(set(secrets), key=len, reverse=True):
        if not secret:
            continue
        redacted = redacted.replace(secret, b"*" * len(secret))
        for length in range(min(len(secret) - 1, len(redacted)), 0, -1):
            if redacted.endswith(secret[:length]):
                redacted = redacted[:-length] + b"*" * length
                break
        for length in range(min(len(secret) - 1, len(redacted)), 0, -1):
            if redacted.startswith(secret[-length:]):
                redacted = b"*" * length + redacted[length:]
                break
    return redacted


def pricing_files(kinds: tuple[str, ...]) -> dict[str, int]:
    files = {"parent.json": PARENT_LIMIT, "manifest.json": MANIFEST_LIMIT}
    for kind in kinds:
        for repeat in range(PRICING_RUNS):
            for mode in ("native", "heap"):
                identity = f"{kind}-{mode}-{repeat}"
                files[f"{identity}.json"] = CHILD_LIMIT
                for cycle in range(3):
                    files[f"{identity}.{mode}-cycle-{cycle}.json"] = FRAGMENT_LIMIT
                for stream in ("stdout", "stderr"):
                    files[f"{identity}.{stream}"] = STREAM_LIMIT
    return files


class PricingCollectorMemory:
    def __init__(self) -> None:
        if tracemalloc.is_tracing():
            raise PricingEvidenceError("pricing parent tracing already has an owner")
        tracemalloc.start(10)
        self.observations: list[JsonValue] = []
        self.failures: list[str] = []

    def observe(self, phase: str, report: Report, entries: int) -> None:
        gc.collect()
        current, peak = tracemalloc.get_traced_memory()
        observation: dict[str, JsonValue] = {
            "phase": phase,
            "python_current_bytes": current,
            "python_peak_since_trace_start_bytes": peak,
            "retained_report_samples": len(report.samples),
            "retained_evidence_entries": entries,
        }
        try:
            observation["current_rss_bytes"] = rss_bytes()
            observation["process_high_water_rss_bytes"] = peak_rss_bytes()
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            observation["native_counter_unavailable"] = type(error).__name__
            self.failures.append(type(error).__name__)
        snapshot = tracemalloc.take_snapshot()
        observation["absolute_top_allocations"] = [
            {"location": str(item.traceback), "bytes": item.size, "count": item.count}
            for item in snapshot.statistics("lineno")[:20]
        ]
        del snapshot
        self.observations.append(observation)

    def close(self) -> None:
        tracemalloc.stop()


class PricingEvidence:
    def __init__(
        self,
        directory: Path,
        kinds: tuple[str, ...],
        *,
        aggregate_limit: int = AGGREGATE_LIMIT,
    ) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=False)
        self.expected = pricing_files(kinds)
        self.aggregate_limit = aggregate_limit
        self.entries: dict[str, dict[str, ExtraValue]] = {}
        self.processes: dict[str, dict[str, ExtraValue]] = {}
        self.failures: list[str] = []
        self.complete = False
        self.parent_write_seconds = 0.0
        self.workload_seconds: float | None = None
        self.manifest_publication_seconds: float | None = None
        self.memory: PricingCollectorMemory | None = None
        self.request_removed: bool | None = None
        self.secrets: tuple[bytes, ...] = ()
        self.reserve()

    def reserve(self) -> None:
        content = json.dumps(
            {
                "complete": self.complete,
                "request_removed": self.request_removed,
                "pricing_workload_total": self.workload_seconds,
                "pricing_workload_reference_seconds": 256.0,
                "pricing_workload_reference_exceeded": None
                if self.workload_seconds is None
                else self.workload_seconds >= 256.0,
                "manifest_publication_seconds": self.manifest_publication_seconds,
                "failures": self.failures,
                "processes": self.processes,
                "entries": self.entries,
                "missing": sorted(set(self.expected) - self.entries.keys()),
            }
        ).encode()
        write_pricing_bytes(
            self.directory / "completion.json", content, COMPLETION_LIMIT
        )

    def retain(self, name: str, raw: bytes, *, lower_bound: bool = False) -> bytes:
        if name not in self.expected:
            raise PricingEvidenceError("unexpected evidence identity")
        limit = self.expected[name]
        used = sum(
            int(entry["retained_bytes"] or 0)
            for key, entry in self.entries.items()
            if key != name
        )
        available = max(0, self.aggregate_limit - COMPLETION_LIMIT - used)
        retained = raw[: min(limit, available)]
        redacted = redact_pricing_bytes(retained, self.secrets)
        overflow = len(raw) > limit or len(raw) > available or lower_bound
        self.entries[name] = {
            "observed_bytes": len(raw),
            "observed_is_lower_bound": lower_bound or len(raw) > limit,
            "retained_bytes": len(redacted),
            "modified_by_redaction": redacted != retained,
            "overflow": overflow,
            "truncated": len(raw) > len(retained) or lower_bound,
        }
        write_started = time.perf_counter()
        write_pricing_bytes(self.directory / name, redacted, limit)
        if name == "parent.json":
            self.parent_write_seconds += time.perf_counter() - write_started
        if overflow:
            self.failures.append(f"{name}:overflow")
        self.reserve()
        return redacted

    def harvest(self, name: str, source: Path) -> list[Exception]:
        try:
            raw = read_pricing_bytes(source, self.expected[name])
            retained = self.retain(name, raw)
            if self.entries[name]["overflow"]:
                raise PricingEvidenceError("evidence overflow")
            PricingPayload.model_validate_json(retained)
        except (OSError, ValueError) as error:
            self.failures.append(f"{name}:{type(error).__name__}")
            self.reserve()
            return [error]
        return []

    def run_child(
        self,
        identity: str,
        argv: list[str],
        source: Path,
        secrets: tuple[bytes, ...],
        *,
        deadline_seconds: float = 60.0,
        output_limit: int = STREAM_LIMIT,
    ) -> None:
        self.secrets = tuple(set(self.secrets + secrets))
        self.processes[identity] = {
            "pid": None,
            "code": None,
            "timed_out": None,
            "overflow": None,
            "outcome": "incomplete",
            "stdout_available": False,
            "stderr_available": False,
        }
        self.reserve()
        try:
            result = run_process(
                argv, deadline_seconds=deadline_seconds, output_limit=output_limit
            )
            self.processes[identity].update(
                pid=result.pid,
                code=result.code,
                timed_out=result.timed_out,
                overflow=result.overflow,
                outcome="returned",
                stdout_available=True,
                stderr_available=True,
            )
            self.reserve()
            self.retain(
                f"{identity}.stdout",
                result.stdout,
                lower_bound=result.overflow and len(result.stdout) == output_limit,
            )
            self.retain(
                f"{identity}.stderr",
                result.stderr,
                lower_bound=result.overflow and len(result.stderr) == output_limit,
            )
            if result.code != 0 or result.timed_out or result.overflow or result.stdout:
                raise PricingEvidenceError(
                    "pricing child process did not complete successfully"
                )
        finally:
            original = sys.exception()
            if original is not None:
                self.processes[identity]["outcome"] = type(original).__name__
                self.failures.append(f"{identity}:{type(original).__name__}")
            failures: list[BaseException] = [] if original is None else [original]
            mode = identity.split("-")[1]
            expected = [
                f"{identity}.json",
                *(f"{identity}.{mode}-cycle-{cycle}.json" for cycle in range(3)),
            ]
            try:
                unexpected = set(path.name for path in source.iterdir()) - set(expected)
                if unexpected:
                    raise PricingEvidenceError("unexpected child evidence entries")
            except (OSError, ValueError) as error:
                self.failures.append(
                    f"{identity}:source_inventory:{type(error).__name__}"
                )
                failures.append(error)
            for name in expected:
                failures.extend(self.harvest(name, source / name))
            self.reserve()
            if failures:
                raise BaseExceptionGroup(
                    "pricing process and evidence failures", failures
                )

    def validate_inventory(self, pending: set[str]) -> None:
        actual = {path.name for path in self.directory.iterdir()}
        if (
            actual != (set(self.expected) - pending) | {"completion.json"}
            or set(self.entries) != set(self.expected) - pending
            or self.failures
        ):
            raise PricingEvidenceError("pricing evidence inventory is incomplete")
        if any(
            path.is_symlink() or not path.is_file() for path in self.directory.iterdir()
        ):
            raise PricingEvidenceError("pricing evidence contains nonregular entries")

    def finish(self, report: Report, provenance: dict[str, JsonValue]) -> None:
        started_value = provenance.pop("workload_started", time.perf_counter())
        fixture_value = provenance.get("excluded_common_postgresql_fixture", 0.0)
        assert isinstance(started_value, (float, int))
        assert isinstance(fixture_value, (float, int))
        original = sys.exception()
        failures: list[BaseException] = [] if original is None else [original]
        if original is not None:
            self.failures.append(type(original).__name__)
        memory = self.memory or PricingCollectorMemory()
        serialization_seconds = 0.0
        try:
            if not memory.observations:
                memory.observe("finalization_baseline", report, len(self.entries))
            memory.observe("post_factory_release", report, len(self.entries))
            report.environment["parent_snapshot_timing"] = (
                "earlier nonauthoritative snapshot; final workload elapsed "
                "is in manifest/completion"
            )
            serialization_started = time.perf_counter()
            encoded_parent = report.to_json().encode()
            serialization_seconds = time.perf_counter() - serialization_started
            self.retain("parent.json", encoded_parent)
            serialization_seconds += self.parent_write_seconds
            del encoded_parent
            memory.observe("parent_serialized", report, len(self.entries))
            report.samples.clear()
            report.gates.clear()
            report.environment.clear()
            report.durations.clear()
            try:
                self.validate_inventory({"manifest.json"})
            except (OSError, ValueError) as error:
                failures.append(error)
            memory.observe("report_payload_released", report, len(self.entries))
        finally:
            collection = sys.exception()
            if collection is not None and collection not in failures:
                failures.append(collection)
            memory.close()
            workload_seconds = (
                time.perf_counter()
                - started_value
                - fixture_value
                - serialization_seconds
            )
            self.workload_seconds = workload_seconds
            provenance.update(
                pricing_workload_total=workload_seconds,
                excluded_parent_serialization_seconds=serialization_seconds,
                parent_collector_memory=memory.observations,
                parent_memory_counter_failures=list[JsonValue](memory.failures),
                parent_memory_retention=(
                    "Report payload released; bounded observations, "
                    "entry/process metadata, redaction literals and failure tracebacks "
                    "remain until waited pytest exit"
                ),
                parent_native_counter_scope=(
                    "RSS after GC is not a live-object census; "
                    "process high-water includes earlier pytest/imports"
                ),
                final_manifest_publication=(
                    "outside workload interval; no subtraction of external finalization"
                ),
            )
            self.failures.extend(type(error).__name__ for error in failures)
            self.failures.extend(memory.failures)
            publication_started = time.perf_counter()
            self.retain("manifest.json", json.dumps(provenance).encode())
            self.manifest_publication_seconds = (
                time.perf_counter() - publication_started
            )
            self.complete = not failures and not self.failures
            self.reserve()
        if len(failures) == 1:
            raise failures[0]
        if failures:
            raise BaseExceptionGroup(
                "pricing workload and final evidence failures", failures
            )


def pricing_bootstrap() -> None:
    request_path = Path(sys.argv[2])
    raw = read_pricing_bytes(request_path, REQUEST_LIMIT)
    if len(raw) > REQUEST_LIMIT or request_path.stat().st_mode & 0o077:
        raise PricingEvidenceError("private pricing request is invalid")
    request = PricingRequest.model_validate_json(raw)
    if len(request.databases) != 25:
        raise PricingEvidenceError("pricing request requires 25 databases")
    pricing_child(request)


def pricing_argv(request: Path) -> list[str]:
    return [
        sys.executable,
        "-I",
        "-c",
        "import json,sys; sys.path[:]=json.loads(sys.argv[1]); "
        "from tests.measurements.pricing_lifetime_support import pricing_bootstrap; "
        "pricing_bootstrap()",
        json.dumps(full_path(ROOT)),
        str(request),
    ]


def pricing_process_runs(
    report: Report,
    kind: DatabaseKind,
    factory: PricingDatabaseFactory,
    evidence: PricingEvidence,
) -> None:
    async def prepare() -> list[DatabaseSettings]:
        return [await factory.predecessor() for _ in range(25)]

    async def discard(settings: list[DatabaseSettings]) -> list[BaseException]:
        failures: list[BaseException] = []
        for value in settings:
            outcomes = await asyncio.gather(
                factory.discard(value), return_exceptions=True
            )
            failures.extend(
                outcome for outcome in outcomes if isinstance(outcome, BaseException)
            )
        return failures

    for repeat in range(PRICING_RUNS):
        settings = asyncio.run(prepare())
        try:
            for mode in ("native", "heap"):
                identity = f"{kind.value}-{mode}-{repeat}"
                source = factory.directory / identity
                source.mkdir()
                output = source / f"{identity}.json"
                request_path = factory.directory / "pricing-request.json"
                request = PricingRequest(
                    mode=mode,
                    databases=[
                        value.url.render_as_string(hide_password=False)
                        for value in settings
                    ],
                    output=output,
                    revision=report.revision,
                )
                secrets = tuple(value.encode() for value in request.databases) + tuple(
                    str(value.url.password).encode()
                    for value in settings
                    if value.url.password
                )
                register_pricing_ownership(set(), secrets)
                started = time.perf_counter()
                try:
                    write_pricing_bytes(
                        request_path, request.model_dump_json().encode(), REQUEST_LIMIT
                    )
                    evidence.run_child(
                        identity, pricing_argv(request_path), source, secrets
                    )
                    validate_pricing_child(
                        evidence.directory, identity, report.revision
                    )
                    merge_pricing_report(
                        report,
                        evidence.directory / f"{identity}.json",
                        kind,
                        mode,
                        repeat,
                    )
                    if evidence.memory is not None:
                        evidence.memory.observe(
                            f"{identity}_collected", report, len(evidence.entries)
                        )
                    record(
                        report,
                        f"pricing_process_{mode}",
                        1,
                        time.perf_counter() - started,
                        kind,
                        repeat=repeat,
                        exit_code=evidence.processes[identity]["code"],
                    )
                finally:
                    remove_pricing_request(request_path, evidence)
        finally:
            original = sys.exception()
            failures = asyncio.run(discard(settings))
            if original is not None:
                failures.insert(0, original)
            if failures:
                raise BaseExceptionGroup(
                    "pricing workload and discard failures", failures
                )


def remove_pricing_request(path: Path, evidence: PricingEvidence) -> None:
    original = sys.exception()
    try:
        path.unlink(missing_ok=True)
        evidence.request_removed = not path.exists()
    except OSError as error:
        evidence.request_removed = False
        evidence.failures.append(f"private_request_cleanup:{type(error).__name__}")
        failures: list[BaseException] = (
            [error] if original is None else [original, error]
        )
        evidence.reserve()
        raise BaseExceptionGroup(
            "pricing request and cleanup failures", failures
        ) from None
    evidence.reserve()


def pricing_origins() -> dict[str, str]:
    if Path(inspect.getfile(Database)).resolve() != ROOT / "src/kora/db/connection.py":
        raise PricingEvidenceError("parent database import does not match checkout")
    return {
        "pricing_source": str(Path(__file__).resolve()),
        "database_source": str(Path(inspect.getfile(Database)).resolve()),
        "executable": sys.executable,
        "python": sys.version.split()[0],
    }


def validate_pricing_origin(payload: PricingPayload, revision: str, mode: str) -> None:
    if payload.python != "3.11.17" or payload.revision != revision:
        raise PricingEvidenceError("pricing interpreter or revision does not match")
    for key, value in pricing_origins().items():
        if payload.environment.get(key) != value:
            raise PricingEvidenceError(
                "pricing imported source or executable does not match"
            )
    if payload.environment.get("pricing_mode") != mode:
        raise PricingEvidenceError("pricing mode does not match")


def validate_pricing_inventory(
    payload: PricingPayload, kind: str, mode: str, *, cycles: int, native: bool
) -> None:
    samples = payload.samples
    counts = Counter(str(sample["workload"]) for sample in samples)
    prefix = f"{kind}_pricing_"
    for scenario in (
        "refused_released",
        "recovered_released",
        "contended_cancel",
        "close_recovery",
        "lease_success",
        "lease_failure",
        "lease_cancel",
        "interruption_repair_released",
    ):
        if counts[prefix + scenario] != cycles:
            raise PricingEvidenceError("pricing lifecycle inventory is incomplete")
    tables = ("price_table", "usage_record", "attempt", "approval_request")
    ddl = [("ddl", table) for table in tables]
    stamp = [("stamp", table) for table in tables]
    if kind == "sqlite":
        validation = [
            ("copy_price_table", "price_table"),
            ("copy_usage_record", "usage_record"),
        ]
        refused = ddl + validation[:1]
        repaired = ddl + validation + [("index_build", "price_table")] + stamp
    else:
        validation = [
            (f"validate_{table}", table)
            for table in ("price_table", "usage_record", "approval_request")
        ]
        refused = ddl + validation[:1]
        repaired = ddl + [("index_build", "price_table")] + stamp
    complete = ddl + validation + [("index_build", "price_table")] + stamp
    expected: Counter[tuple[str, str, str]] = Counter()
    for label, requests, repeats in (
        ("populated", complete, int(native)),
        ("refused", refused, cycles),
        ("recovered", complete, cycles),
        ("interruption_repair", repaired, cycles),
    ):
        expected.update(
            {
                (prefix + label + "_writer", phase, table): repeats
                for phase, table in requests
                if repeats
            }
        )
    actual = Counter(
        (str(sample["workload"]), str(sample["phase"]), str(sample["table"]))
        for sample in samples
        if str(sample["workload"]).endswith("_writer")
    )
    if actual != expected:
        raise PricingEvidenceError("pricing writer request inventory does not match")
    if native:
        for branch in (
            "default",
            "namespace",
            "empty",
            "before_first",
            "endpoint",
            "absent",
        ):
            for history in PRICING_HISTORY_SIZES:
                selected = [
                    sample
                    for sample in samples
                    if sample["workload"] == f"{prefix}latest_{branch}_{history}"
                ]
                if len(selected) != 30 or {
                    sample["sample"] for sample in selected
                } != set(range(30)):
                    raise PricingEvidenceError(
                        "pricing latest-read inventory is incomplete"
                    )
        for label, pages, total in (("usage", 37, 1001), ("attempt", 31, 100)):
            selected = [
                sample
                for sample in samples
                if sample["workload"] == prefix + label + "_page"
            ]
            if (
                len(selected) != pages
                or sum(cast(int, sample["count"]) for sample in selected) != total
            ):
                raise PricingEvidenceError(
                    "pricing ledger page inventory is incomplete"
                )
    elif any(
        str(sample["workload"]).startswith(prefix + "latest_") for sample in samples
    ):
        raise PricingEvidenceError("unexpected native reads in heap or fragment")
    if cycles == 4:
        for name, positions in (
            (f"release_{mode}", {0, 10, 20, 30}),
            (f"migration_release_{mode}", {0, 1, 2, 3}),
        ):
            selected = [
                sample for sample in samples if sample["workload"] == prefix + name
            ]
            if (
                len(selected) != 4
                or {sample["checkpoint"] for sample in selected} != positions
            ):
                raise PricingEvidenceError("pricing checkpoint inventory is incomplete")


def validate_pricing_child(directory: Path, identity: str, revision: str) -> None:
    kind, mode, _ = identity.split("-")
    payload = PricingPayload.model_validate_json(
        read_pricing_bytes(directory / f"{identity}.json", CHILD_LIMIT)
    )
    validate_pricing_origin(payload, revision, mode)
    if payload.gates != {"pricing_child_resources_released": True}:
        raise PricingEvidenceError("pricing child gate inventory does not match")
    validate_pricing_inventory(payload, kind, mode, cycles=4, native=mode == "native")
    for cycle in range(3):
        fragment = PricingPayload.model_validate_json(
            read_pricing_bytes(
                directory / f"{identity}.{mode}-cycle-{cycle}.json", FRAGMENT_LIMIT
            )
        )
        validate_pricing_origin(fragment, revision, mode + "_migration_cycle")
        if fragment.gates or fragment.environment.get("pricing_cycle") != cycle + 1:
            raise PricingEvidenceError("pricing fragment identity does not match")
        validate_pricing_inventory(fragment, kind, mode, cycles=1, native=False)


def pricing_provenance() -> dict[str, JsonValue]:
    hashes: dict[str, JsonValue] = {}
    for relative in (
        "pyproject.toml",
        "uv.lock",
        ".github/workflows/ci.yml",
        "tests/measurements/pricing_lifetime_support.py",
        "tests/measurements/metadata_lifetime_support.py",
        "tests/measurements/test_metadata_database_lifetime.py",
        "tests/measurements/test_pricing_database_lifetime.py",
        "tests/measurements/cel_measurement_process.py",
        "tests/measurements/harness.py",
        "tests/db/seed.py",
        "tests/support/databases.py",
        "src/kora/db/connection.py",
        "src/kora/db/cost_reads.py",
    ):
        hashes[relative] = hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
    git = run_process(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD", "HEAD^{tree}"],
        deadline_seconds=60,
    )
    if git.code or git.timed_out or git.overflow:
        raise PricingEvidenceError("cannot establish checkout provenance")
    checkout, tree = git.stdout.decode().splitlines()
    return {
        "checkout": checkout,
        "tree": tree,
        "pr_head": os.environ.get("KORA_MEASUREMENT_REVISION", ""),
        "origins": dict(pricing_origins()),
        "hashes": hashes,
        "raw_evidence": "bounded post-redaction bytes; known literal credentials only",
        "command_complete": False,
    }


def pricing_containers() -> set[str]:
    docker = shutil.which("docker")
    if docker is None:
        raise PricingEvidenceError("docker executable unavailable")
    result = run_process(
        [docker, "ps", "--all", "--quiet", "--no-trunc"], deadline_seconds=60
    )
    if result.code or result.timed_out or result.overflow:
        raise PricingEvidenceError("container inventory unavailable")
    return set(result.stdout.decode().splitlines())


def pricing_command() -> int:
    directory = Path(os.environ["KORA_PRICING_EVIDENCE"])
    return pricing_command_run(
        directory,
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-m",
            "measurement",
            "tests/measurements/test_pricing_database_lifetime.py",
        ],
    )


class PricingCommandOwner(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    containers: list[str] = []
    sessions: list[str] = []
    secrets: list[str] = []


def read_pricing_owner(path: Path) -> PricingCommandOwner:
    try:
        raw = read_pricing_bytes(path, REQUEST_LIMIT)
        if len(raw) > REQUEST_LIMIT:
            raise PricingEvidenceError("private pricing owner exceeds limit")
        return PricingCommandOwner.model_validate_json(raw)
    except (OSError, ValueError):
        raise PricingEvidenceError("private pricing owner validation failed") from None


def register_pricing_ownership(
    containers: set[str],
    secrets: tuple[bytes, ...] = (),
    *,
    session_id: str | None = None,
) -> None:
    raw = os.environ.get("KORA_PRICING_OWNER")
    if raw is None:
        return
    path = Path(raw)
    owner = read_pricing_owner(path)
    owner.containers = sorted(set(owner.containers) | containers)
    if session_id is not None:
        owner.sessions = sorted(set(owner.sessions) | {session_id})
    owner.secrets = sorted(set(owner.secrets) | {value.decode() for value in secrets})
    write_pricing_bytes(path, owner.model_dump_json().encode(), REQUEST_LIMIT)


def pricing_command_run(
    directory: Path, argv: list[str], *, cleanup_seconds: float = 60.0
) -> int:
    if not math.isfinite(cleanup_seconds) or cleanup_seconds <= 0:
        raise ValueError("cleanup deadline must be positive and finite")
    before = pricing_containers()
    owner_path = directory.with_name(directory.name + "-owner.json")
    owner_path.parent.mkdir(parents=True, exist_ok=True)
    write_pricing_bytes(
        owner_path, PricingCommandOwner().model_dump_json().encode(), REQUEST_LIMIT
    )
    environment = dict(os.environ)
    environment["KORA_PRICING_OWNER"] = str(owner_path)
    started = time.perf_counter()
    code: int | None = None
    child: subprocess.Popen[bytes] | None = None
    descendants: dict[int, dict[str, JsonValue]] = {}
    interrupted: int | None = None
    interrupt_deadline: float | None = None
    failures: list[BaseException] = []

    def interrupt(signum: int, frame: FrameType | None) -> None:
        nonlocal interrupted, interrupt_deadline
        if interrupted is None:
            interrupted = signum
            interrupt_deadline = time.monotonic() + cleanup_seconds
            if child is not None and child.poll() is None:
                child.send_signal(signal.SIGINT)

    previous_int = signal.signal(signal.SIGINT, interrupt)
    previous_term = signal.signal(signal.SIGTERM, interrupt)
    try:
        child = subprocess.Popen(argv, env=environment, start_new_session=True)
        if interrupted is not None:
            child.send_signal(signal.SIGINT)
        while code is None:
            try:
                code = child.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                if (
                    interrupt_deadline is not None
                    and time.monotonic() >= interrupt_deadline
                ):
                    failures.append(
                        PricingEvidenceError(
                            "pricing owner exceeded cooperative cleanup deadline"
                        )
                    )
                    try:
                        descendants.update(release_pricing_descendants(child.pid))
                    except (OSError, ValueError) as error:
                        failures.append(error)
                    finally:
                        child.kill()
                        code = child.wait()
        if interrupted is not None:
            failures.append(KeyboardInterrupt())
    finally:
        original = sys.exception()
        if original is not None:
            failures.append(original)
        if child is not None and child.poll() is None:
            try:
                descendants.update(release_pricing_descendants(child.pid))
            except (OSError, ValueError) as error:
                failures.append(error)
            finally:
                child.kill()
                code = child.wait()
        signal.signal(signal.SIGINT, previous_int)
        signal.signal(signal.SIGTERM, previous_term)
        elapsed = time.perf_counter() - started
        descendant_release: dict[str, JsonValue] = {}
        for pid in sorted(descendants):
            deadline = time.monotonic() + 10.0
            while True:
                observed = run_process(
                    ["/bin/ps", "-o", "pid=", "-p", str(pid)], deadline_seconds=10
                )
                if observed.code == 1 and not observed.stdout:
                    descendant_release[str(pid)] = {
                        **descendants[pid],
                        "absent_after_owner_exit": True,
                        "nonchild_reaped": None,
                    }
                    break
                if (
                    observed.timed_out
                    or observed.overflow
                    or time.monotonic() >= deadline
                ):
                    failures.append(
                        PricingEvidenceError("owned descendant release unverified")
                    )
                    descendant_release[str(pid)] = {
                        **descendants[pid],
                        "absent_after_owner_exit": False,
                        "nonchild_reaped": None,
                    }
                    break
        owner = PricingCommandOwner()
        owner_valid = False
        try:
            owner = read_pricing_owner(owner_path)
            owner_valid = True
        except PricingEvidenceError as error:
            failures.append(error)
        known: dict[str, JsonValue] = {
            "complete": False,
            "command_exit_code": code,
            "command_pid": None if child is None else child.pid,
            "command_reaped": child is not None and code is not None,
            "command_interruption": interrupted,
            "pytest_process_seconds": elapsed,
            "escalated_descendant_release": descendant_release,
            "private_owner_valid": owner_valid,
        }
        manifest, completion, collection = recover_pricing_envelope(
            directory,
            known,
            tuple(value.encode() for value in owner.secrets),
            collect_working=owner_valid,
        )
        failures.extend(collection)
        try:
            after = pricing_containers()
            owned = set(owner.containers) - before
            docker = shutil.which("docker")
            if docker is None:
                raise PricingEvidenceError("docker executable unavailable")
            for session_id in owner.sessions:
                identified = run_process(
                    [
                        docker,
                        "ps",
                        "--all",
                        "--quiet",
                        "--no-trunc",
                        "--filter",
                        f"label={LABEL_SESSION_ID}={session_id}",
                        "--filter",
                        f"ancestor={POSTGRES_IMAGE}",
                    ],
                    deadline_seconds=60,
                )
                if identified.code or identified.timed_out or identified.overflow:
                    raise PricingEvidenceError("fixture session ownership unavailable")
                owned.update(set(identified.stdout.decode().splitlines()) - before)
            retained = owned & after
            manifest.update(
                containers_before=list[JsonValue](sorted(before)),
                containers_owned=list[JsonValue](sorted(owned)),
                fixture_session_ownership=list[JsonValue](owner.sessions),
                uncontended_inventory_verified=not (after - before - owned),
                unowned_new_containers=list[JsonValue](sorted(after - before - owned)),
                containers_retained_before_cleanup=list[JsonValue](sorted(retained)),
            )
            if after != before:
                failures.append(
                    PricingEvidenceError(
                        "pricing command did not release its containers"
                    )
                )
            for identity in retained:
                remove_pricing_container(identity)
            after_release = pricing_containers()
            manifest.update(
                containers_after=list[JsonValue](sorted(after_release)),
                containers_released=after_release == before,
            )
        except (OSError, ValueError) as error:
            failures.append(error)
            manifest["container_verification_failure"] = type(error).__name__
        try:
            owner_path.unlink(missing_ok=True)
            known["private_owner_removed"] = not owner_path.exists()
        except OSError as error:
            failures.append(error)
            known["private_owner_removed"] = False
        manifest.update(known)
        manifest["command_complete"] = (
            code == 0 and completion.get("complete") is True and not failures
        )
        completion.update(known, complete=manifest["command_complete"])
        manifest["collection_failures"] = [type(error).__name__ for error in failures]
        try:
            write_pricing_envelope(
                directory,
                manifest,
                completion,
                tuple(value.encode() for value in owner.secrets),
            )
        except (OSError, ValueError) as error:
            failures.append(error)
            completion["complete"] = False
            manifest["command_complete"] = False
            manifest["envelope_write_failure"] = type(error).__name__
        try:
            failures.extend(
                seal_pricing_evidence(
                    directory,
                    tuple(value.encode() for value in owner.secrets),
                    manifest,
                    completion,
                    collect_working=owner_valid,
                )
            )
        except (OSError, ValueError) as error:
            failures.append(error)
        if failures:
            raise BaseExceptionGroup(
                "pricing command and finalization failures", failures
            )
    assert code is not None
    if code == 0 and manifest.get("command_complete") is not True:
        raise PricingEvidenceError("pricing command evidence is incomplete")
    return code


def release_pricing_descendants(parent: int) -> dict[int, dict[str, JsonValue]]:
    result = run_process(["/bin/ps", "-axo", "pid=,ppid=,lstart="], deadline_seconds=10)
    if result.code or result.timed_out or result.overflow:
        raise PricingEvidenceError("owned process inventory unavailable")
    ancestry = {
        int(parts[0]): (int(parts[1]), " ".join(parts[2:]))
        for row in result.stdout.decode().splitlines()
        if len(parts := row.split()) >= 7
    }
    owned = {parent}
    while (
        descendants := {pid for pid, (ppid, _) in ancestry.items() if ppid in owned}
        - owned
    ):
        owned.update(descendants)
    released: dict[int, dict[str, JsonValue]] = {}
    for pid in reversed(list(ancestry)):
        if pid in owned and pid != parent:
            current = run_process(
                ["/bin/ps", "-o", "ppid=,lstart=", "-p", str(pid)], deadline_seconds=10
            )
            identity = " ".join(current.stdout.decode().split())
            ppid, birth = ancestry[pid]
            if current.code == 0 and identity == f"{ppid} {birth}":
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    continue
                released[pid] = {
                    "parent_pid": ppid,
                    "started": birth,
                    "signal": int(signal.SIGKILL),
                    "signal_sent": True,
                }
    return released


def remove_pricing_container(identity: str) -> None:
    if len(identity) != 64 or any(char not in "0123456789abcdef" for char in identity):
        raise PricingEvidenceError("owned container identity is invalid")
    docker = shutil.which("docker")
    if docker is None:
        raise PricingEvidenceError("docker executable unavailable")
    result = run_process([docker, "rm", "--force", identity], deadline_seconds=60)
    if result.code or result.timed_out or result.overflow:
        raise PricingEvidenceError("owned container removal failed")


def recover_pricing_envelope(
    directory: Path,
    known: dict[str, JsonValue],
    secrets: tuple[bytes, ...],
    *,
    reserve_directory: Path | None = None,
    collect_working: bool = True,
) -> tuple[dict[str, JsonValue], dict[str, JsonValue], list[BaseException]]:
    raw_inputs: dict[str, bytes] = {}
    failures: list[BaseException] = []
    if directory.is_symlink():
        failures.append(PricingEvidenceError("evidence directory ownership mismatch"))
        collect_working = False
    for name, limit in (
        ("completion.json", COMPLETION_LIMIT),
        ("manifest.json", MANIFEST_LIMIT),
    ):
        if not collect_working:
            continue
        try:
            raw_inputs[name] = read_pricing_bytes(directory / name, limit)
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as error:
            failures.append(error)
    reserved = reserve_directory or directory.with_name(directory.name + "-sealed")
    try:
        if directory.is_symlink():
            raise PricingEvidenceError("evidence directory ownership mismatch")
        directory.mkdir(parents=True, exist_ok=True)
    except (OSError, ValueError) as error:
        failures.append(error)
    try:
        if reserved.is_symlink():
            raise PricingEvidenceError("evidence directory ownership mismatch")
        reserved.mkdir(mode=0o700, exist_ok=True)
        write_pricing_bytes(
            reserved / "completion.json",
            json.dumps({**known, "complete": False}).encode(),
            COMPLETION_LIMIT,
        )
    except (OSError, ValueError) as error:
        failures.append(error)
    documents: dict[str, dict[str, JsonValue]] = {}
    malformed: dict[str, JsonValue] = {}
    for name, raw in raw_inputs.items():
        limit = COMPLETION_LIMIT if name == "completion.json" else MANIFEST_LIMIT
        try:
            if len(raw) > limit:
                raise PricingEvidenceError("envelope exceeds its limit")
            documents[name] = parse_json_object(json.loads(raw))
        except (ValueError, UnicodeError) as error:
            failures.append(error)
            retained = redact_pricing_bytes(raw[:65536], secrets)
            malformed[name] = {
                "raw_hex": retained.hex(),
                "observed_bytes": len(raw),
                "observed_is_lower_bound": len(raw) > limit,
                "retained_bytes": len(retained),
                "truncated": len(raw) > len(retained),
                "modified_by_redaction": retained != raw[:65536],
            }
    manifest = documents.get("manifest.json", {})
    if malformed:
        manifest["malformed_inputs"] = malformed
    previous = documents.get("completion.json", {})
    previous.update({key: value for key, value in known.items() if key != "complete"})
    if failures:
        previous["complete"] = False
    return manifest, previous, failures


def seal_pricing_evidence(
    directory: Path,
    secrets: tuple[bytes, ...],
    manifest: dict[str, JsonValue],
    completion: dict[str, JsonValue],
    *,
    collect_working: bool = True,
) -> list[BaseException]:
    destination = directory.with_name(directory.name + "-sealed")
    if destination.is_symlink():
        raise PricingEvidenceError("sealed evidence ownership mismatch")
    destination.mkdir(mode=0o700, exist_ok=True)
    for existing in destination.iterdir():
        if existing.name != "completion.json":
            if existing.is_symlink() or not existing.is_file():
                raise PricingEvidenceError(
                    "sealed evidence contains nonregular entries"
                )
            existing.unlink()
    selected = pricing_files(("sqlite", "postgresql"))
    failures: list[BaseException] = []
    entries: dict[str, JsonValue] = {}
    used = 0
    try:
        unexpected = (
            {path.name for path in directory.iterdir()}
            - selected.keys()
            - {"completion.json"}
        )
        if unexpected:
            failures.append(
                PricingEvidenceError("unexpected evidence excluded from upload")
            )
    except OSError as error:
        failures.append(error)
    for name, limit in selected.items():
        if name == "manifest.json" or not collect_working:
            continue
        try:
            raw = read_pricing_bytes(directory / name, limit)
            available = max(
                0, AGGREGATE_LIMIT - COMPLETION_LIMIT - MANIFEST_LIMIT - used
            )
            retained = redact_pricing_bytes(raw[: min(limit, available)], secrets)
            write_pricing_bytes(destination / name, retained, limit)
            used += len(retained)
            entries[name] = {
                "observed_bytes": len(raw),
                "observed_is_lower_bound": len(raw) > limit,
                "retained_bytes": len(retained),
                "truncated": len(raw) > len(retained),
                "modified_by_redaction": retained != raw[: len(retained)],
            }
            if len(raw) > len(retained):
                failures.append(PricingEvidenceError("upload evidence overflow"))
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as error:
            failures.append(error)
    missing = sorted(set(selected) - entries.keys() - {"manifest.json"})
    if completion.get("complete") is True and missing:
        failures.append(
            PricingEvidenceError("complete upload inventory is missing selected files")
        )
    completion["upload_missing"] = list[JsonValue](missing)
    completion["working_evidence_available"] = collect_working
    if failures or completion.get("complete") is not True:
        completion["complete"] = False
        manifest["command_complete"] = False
    completion["upload_failures"] = [type(error).__name__ for error in failures]
    completion["upload_entries"] = entries
    raw_manifest = redact_pricing_bytes(json.dumps(manifest).encode(), secrets)
    if len(raw_manifest) > MANIFEST_LIMIT:
        failures.append(PricingEvidenceError("sealed manifest exceeds limit"))
        completion["complete"] = False
        raw_manifest = json.dumps(
            {"command_complete": False, "manifest_overflow": True}
        ).encode()
    write_pricing_bytes(destination / "manifest.json", raw_manifest, MANIFEST_LIMIT)
    completion["upload_retained_bytes_without_completion"] = used + len(raw_manifest)
    raw_completion = redact_pricing_bytes(json.dumps(completion).encode(), secrets)
    if len(raw_completion) > COMPLETION_LIMIT:
        failures.append(PricingEvidenceError("sealed completion exceeds limit"))
        raw_completion = json.dumps(
            {
                "complete": False,
                "completion_overflow": True,
                "command_exit_code": completion.get("command_exit_code"),
                "command_interruption": completion.get("command_interruption"),
            }
        ).encode()
    write_pricing_bytes(
        destination / "completion.json", raw_completion, COMPLETION_LIMIT
    )
    return failures


def pricing_environment_release() -> None:
    raw = os.environ.get("KORA_PRICING_ROOT")
    if raw is None:
        raise PricingEvidenceError("pricing environment was not allocated")
    release_pricing_environment(
        Path(raw),
        Path(os.environ["RUNNER_TEMP"]).resolve(),
        ROOT / "measurements/pricing-database-lifetime",
        os.environ.get("KORA_PRICING_ISOLATION_VERIFIED") == "true",
    )


def release_pricing_environment(
    root: Path, temporary: Path, directory: Path, isolation_verified: bool
) -> None:
    if (
        root.is_symlink()
        or not root.resolve().is_relative_to(temporary.resolve())
        or root.resolve() == temporary.resolve()
    ):
        raise PricingEvidenceError("pricing environment cleanup ownership mismatch")
    proof: dict[str, JsonValue] = {
        "temporary_environment": str(root),
        "isolation_verified": isolation_verified,
        "managed_runtime_retention": (
            "pinned uv managed Python installation and "
            "download cache remain owned by existing setup-uv cache "
            "and runner lifecycle"
        ),
    }
    failures: list[BaseException] = []
    for name in (
        "original-environment.sha256",
        "original-packages.txt",
        "dependencies.sha256",
        "constraints.txt",
    ):
        try:
            content = read_pricing_bytes(root / name, MANIFEST_LIMIT)
            if len(content) > MANIFEST_LIMIT:
                raise PricingEvidenceError("environment provenance exceeds limit")
            proof[name] = hashlib.sha256(content).hexdigest()
        except FileNotFoundError:
            proof[name] = None
        except (OSError, ValueError) as error:
            failures.append(error)
            proof[name] = type(error).__name__
    try:
        shutil.rmtree(root)
    except OSError as error:
        failures.append(error)
    proof["temporary_environment_released"] = not root.exists()
    sanitized = directory.with_name(directory.name + "-sealed")
    manifest, completion, collection = recover_pricing_envelope(
        sanitized,
        {},
        (),
        reserve_directory=sanitized,
    )
    failures.extend(collection)
    manifest["environment_release"] = proof
    failures.extend(validate_sealed_pricing_evidence(sanitized, completion))
    if failures or not isolation_verified:
        completion["complete"] = False
        manifest["command_complete"] = False
    completion["environment_failures"] = [type(error).__name__ for error in failures]
    try:
        write_pricing_envelope(sanitized, manifest, completion)
    except (OSError, ValueError) as error:
        failures.append(error)
    if failures:
        raise BaseExceptionGroup(
            "pricing environment evidence and cleanup failures", failures
        )


def validate_sealed_pricing_evidence(
    directory: Path,
    completion: dict[str, JsonValue],
) -> list[BaseException]:
    expected = pricing_files(("sqlite", "postgresql"))
    expected["completion.json"] = COMPLETION_LIMIT
    failures: list[BaseException] = []
    actual: set[str] = set()
    used = 0
    payload_bytes = 0
    try:
        if directory.is_symlink():
            raise PricingEvidenceError("sealed evidence ownership mismatch")
        for path in directory.iterdir():
            metadata = path.lstat()
            if path.name not in expected or not stat.S_ISREG(metadata.st_mode):
                raise PricingEvidenceError(
                    "sealed publication contains an unselected or nonregular entry"
                )
            if metadata.st_size > expected[path.name]:
                raise PricingEvidenceError("sealed publication exceeds file limit")
            actual.add(path.name)
            used += metadata.st_size
            if path.name not in {"manifest.json", "completion.json"}:
                payload_bytes += metadata.st_size
        if payload_bytes + MANIFEST_LIMIT + COMPLETION_LIMIT > AGGREGATE_LIMIT:
            raise PricingEvidenceError("sealed publication exhausted envelope reserve")
        if used > AGGREGATE_LIMIT:
            raise PricingEvidenceError("sealed publication exceeds aggregate limit")
        if completion.get("complete") is True and actual != set(expected):
            raise PricingEvidenceError("complete sealed publication is missing files")
    except (OSError, ValueError) as error:
        failures.append(error)
    return failures


def write_pricing_envelope(
    directory: Path,
    manifest: dict[str, JsonValue],
    completion: dict[str, JsonValue],
    secrets: tuple[bytes, ...] = (),
) -> None:
    if directory.is_symlink():
        raise PricingEvidenceError("evidence directory ownership mismatch")
    encoded = redact_pricing_bytes(json.dumps(manifest).encode(), secrets)
    entries = parse_json_object(completion.get("entries", {}))
    entry = parse_json_object(entries.get("manifest.json", {}))
    entry.update(
        observed_bytes=len(encoded),
        retained_bytes=len(encoded),
        observed_is_lower_bound=False,
        overflow=False,
        truncated=False,
        modified_by_redaction=False,
        updated_by_command_envelope=True,
    )
    entries["manifest.json"] = entry
    completion["entries"] = entries
    write_pricing_bytes(directory / "manifest.json", encoded, MANIFEST_LIMIT)
    write_pricing_bytes(
        directory / "completion.json",
        redact_pricing_bytes(json.dumps(completion).encode(), secrets),
        COMPLETION_LIMIT,
    )


class PricingPublication(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    directory: Path
    device: int
    inode: int
    source_valid: bool = True
    preparation_seconds: float | None = None
    selected_bytes: int | None = None
    selected_files: int | None = None


class PricingPublicationOutcome(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, hide_input_in_errors=True)
    command_exit_code: int | None = None
    command_pid: int | None = None
    command_reaped: bool | None = None
    command_interruption: int | None = None
    private_owner_removed: bool | None = None
    private_owner_valid: bool | None = None


class PricingPublicationAllocation:
    def __init__(self, temporary: Path) -> None:
        self.temporary = temporary.resolve()
        self.directory: Path | None = None
        self.identity: tuple[int, int] | None = None
        self.previous_mask: set[int | signal.Signals] | None = None

    def allocate(self) -> Path:
        self.previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
        self.directory = Path(
            tempfile.mkdtemp(prefix="kora-pricing-upload-", dir=self.temporary)
        )
        return self.directory

    def receipt(self) -> PricingPublication:
        if self.directory is None:
            raise PricingEvidenceError("publication allocation is unavailable")
        metadata = self.directory.lstat()
        if not stat.S_ISDIR(metadata.st_mode):
            raise PricingEvidenceError("publication allocation identity is unavailable")
        self.identity = (metadata.st_dev, metadata.st_ino)
        publication = PricingPublication(
            directory=self.directory, device=metadata.st_dev, inode=metadata.st_ino
        )
        self.restore_interrupts()
        return publication

    def restore_interrupts(self) -> None:
        if self.previous_mask is not None:
            previous = self.previous_mask
            self.previous_mask = None
            signal.pthread_sigmask(signal.SIG_SETMASK, previous)

    def release(self) -> None:
        if self.directory is None:
            return
        if self.identity is None:
            raise PricingEvidenceError("publication allocation identity is unavailable")
        release_pricing_publication_directory(
            self.directory, self.identity[0], self.identity[1], self.temporary
        )


@contextmanager
def pricing_publication_allocation(
    temporary: Path,
) -> Generator[PricingPublicationAllocation, None, None]:
    allocation = PricingPublicationAllocation(temporary)
    try:
        yield allocation
    finally:
        original = sys.exception()
        failure = original
        try:
            allocation.restore_interrupts()
        except (KeyboardInterrupt, OSError) as restoration:
            failure = (
                restoration
                if original is None
                else BaseExceptionGroup(
                    "publication acquisition and signal restoration failures",
                    [original, restoration],
                )
            )
        if failure is not None:
            try:
                allocation.release()
            except (OSError, ValueError) as cleanup:
                raise BaseExceptionGroup(
                    "publication preparation and release failures", [failure, cleanup]
                ) from None
            if failure is not original:
                raise failure


def read_pricing_publication_envelope(
    directory: Path, name: Literal["manifest.json", "completion.json"]
) -> dict[str, JsonValue]:
    if not stat.S_ISDIR(directory.lstat().st_mode):
        raise PricingEvidenceError("publication source root is not a directory")
    limit = MANIFEST_LIMIT if name == "manifest.json" else COMPLETION_LIMIT
    raw = read_pricing_bytes(directory / name, limit)
    if len(raw) > limit:
        raise PricingEvidenceError("publication source envelope exceeds limit")
    try:
        decoded = json.loads(raw)
    except RecursionError:
        raise PricingEvidenceError(
            "publication source envelope exceeds depth"
        ) from None
    return parse_json_object(decoded)


def prepare_pricing_publication(
    directory: Path,
    temporary: Path,
    measurement_outcome: Literal["success", "failure", "cancelled", "skipped"],
    release_outcome: Literal["success", "failure", "cancelled", "skipped"],
) -> PricingPublication:
    started = time.perf_counter()
    if measurement_outcome not in {"success", "failure", "cancelled", "skipped"} or (
        release_outcome not in {"success", "failure", "cancelled", "skipped"}
    ):
        raise PricingEvidenceError("invalid publication step outcome")
    with pricing_publication_allocation(temporary) as allocation:
        selected = allocation.allocate()
        ownership = allocation.receipt()
        source = directory.with_name(directory.name + "-sealed")
        source_valid = True
        source_failure: str | None = None
        manifest: dict[str, JsonValue]
        try:
            completion = read_pricing_publication_envelope(source, "completion.json")
            manifest = read_pricing_publication_envelope(source, "manifest.json")
            if not isinstance(completion.get("complete"), bool) or (
                validate_sealed_pricing_evidence(source, completion)
            ):
                raise PricingEvidenceError("publication source validation failed")
            for name, limit in pricing_files(("sqlite", "postgresql")).items():
                if name == "manifest.json" or not (source / name).exists():
                    continue
                raw = read_pricing_bytes(source / name, limit)
                try:
                    write_pricing_bytes(selected / name, raw, limit)
                finally:
                    del raw
        except (OSError, ValueError) as error:
            source_valid = False
            source_failure = type(error).__name__
            for copied in selected.iterdir():
                copied.unlink()
            known = PricingPublicationOutcome()
            working_proof_available = False
            try:
                known = PricingPublicationOutcome.model_validate(
                    read_pricing_publication_envelope(directory, "completion.json")
                )
                working_proof_available = True
            except (OSError, ValueError):
                working_proof_available = False
            completion = parse_json_object(json.loads(known.model_dump_json()))
            completion.update(
                complete=False,
                working_evidence_available=False,
                working_proof_available=working_proof_available,
            )
            manifest = {"command_complete": False}
        if measurement_outcome != "success" or release_outcome != "success":
            completion["complete"] = False
            manifest["command_complete"] = False
        publication_status: dict[str, JsonValue] = {
            "source_valid": source_valid,
            "source_failure": source_failure,
            "measurement_outcome": measurement_outcome,
            "environment_release_outcome": release_outcome,
        }
        manifest["final_publication"] = publication_status
        write_pricing_envelope(selected, manifest, completion)
        if validate_sealed_pricing_evidence(selected, completion):
            raise PricingEvidenceError("final publication validation failed")
        files = list(selected.iterdir())
        selected_bytes = sum(path.stat().st_size for path in files)
        return PricingPublication(
            directory=selected,
            device=ownership.device,
            inode=ownership.inode,
            source_valid=source_valid,
            preparation_seconds=time.perf_counter() - started,
            selected_bytes=selected_bytes,
            selected_files=len(files),
        )


def release_pricing_publication(
    publication: PricingPublication, temporary: Path
) -> dict[str, JsonValue]:
    return release_pricing_publication_directory(
        publication.directory, publication.device, publication.inode, temporary
    )


def release_pricing_publication_directory(
    directory: Path, device: int, inode: int, temporary: Path
) -> dict[str, JsonValue]:
    started = time.perf_counter()
    metadata = directory.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or directory.parent != temporary.resolve()
        or not directory.name.startswith("kora-pricing-upload-")
        or (metadata.st_dev, metadata.st_ino) != (device, inode)
    ):
        raise PricingEvidenceError("publication cleanup ownership mismatch")
    shutil.rmtree(directory)
    return {
        "publication_released": not directory.exists(),
        "publication_release_seconds": time.perf_counter() - started,
    }


def pricing_publication_prepare() -> None:
    publication = prepare_pricing_publication(
        Path(os.environ["KORA_PRICING_EVIDENCE"]),
        Path(os.environ["RUNNER_TEMP"]),
        cast(
            Literal["success", "failure", "cancelled", "skipped"],
            os.environ["KORA_PRICING_MEASUREMENT_OUTCOME"],
        ),
        cast(
            Literal["success", "failure", "cancelled", "skipped"],
            os.environ["KORA_PRICING_RELEASE_OUTCOME"],
        ),
    )
    try:
        if "\n" in str(publication.directory) or "\r" in str(publication.directory):
            raise PricingEvidenceError("publication path contains line separator")
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
            output.write(f"directory={publication.directory}\n")
            output.write(f"identity={publication.model_dump_json()}\n")
            output.write("ready=true\n")
    finally:
        original = sys.exception()
        if original is not None:
            try:
                release_pricing_publication(
                    publication, Path(os.environ["RUNNER_TEMP"])
                )
            except (OSError, ValueError) as cleanup:
                raise BaseExceptionGroup(
                    "publication handoff and release failures", [original, cleanup]
                ) from None
    if not publication.source_valid:
        raise PricingEvidenceError(
            "unsafe publication source replaced with failure proof"
        )


def pricing_publication_release() -> None:
    publication = PricingPublication.model_validate_json(
        os.environ["KORA_PRICING_PUBLICATION"]
    )
    proof = release_pricing_publication(publication, Path(os.environ["RUNNER_TEMP"]))
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        output.write(f"release={json.dumps(proof)}\n")
