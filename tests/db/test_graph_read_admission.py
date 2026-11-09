import asyncio
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass

import pytest
from alembic import command
from sqlalchemy import Connection, event, func, insert, inspect, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.util import await_

from kora.db.connection import Database, DatabaseSettings, Dialect
from kora.db.graph_read_admission import (
    GraphVersionReadAdmission,
    graph_version_admission_statement,
    read_graph_version_admission,
)
from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected
from kora.db.migrations import current_revision, upgrade_database
from kora.db.migrations.environment import alembic_config
from kora.db.reads import graph_version_by_number
from kora.db.tables import (
    GRAPH_VERSION_SCOPE_INDEX,
    graph_deployment_preparation,
    graph_version,
    graph_version_read_admission,
)
from tests.db.cancel_cause_support import migration_lock_count
from tests.db.closing import CONDITION_DEADLINE_SECONDS, ConnectionLedger
from tests.db.seed import DIGEST, Seeder

ADMISSION = GraphVersionReadAdmission(101, 53)
WIDE = "wide-legacy-value" * 16_384
SQLITE_COLUMN_TYPE_ONLY = 0x80


@dataclass(frozen=True)
class Scope:
    namespace_id: str
    graph_id: str
    version_id: str


@contextmanager
def executed_queries(
    connection: AsyncConnection,
) -> Generator[list[tuple[str, tuple[object, ...]]]]:
    statements: list[tuple[str, tuple[object, ...]]] = []

    def capture(
        connection: object,
        cursor: object,
        statement: str,
        parameters: tuple[object, ...],
        *arguments: object,
    ) -> None:
        statements.append((statement, parameters))

    event.listen(connection.sync_connection, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(connection.sync_connection, "before_cursor_execute", capture)


async def sqlite_instructions(
    connection: AsyncConnection, statement: str, arguments: tuple[object, ...]
) -> list[tuple[object, ...]]:
    rows = (await connection.exec_driver_sql("EXPLAIN " + statement, arguments)).all()
    return [tuple(row) for row in rows]


def assert_sqlite_refuses_before_following_the_wide_field(
    instructions: list[tuple[object, ...]],
    arguments: tuple[object, ...],
) -> None:
    early_type_checks = [
        index
        for index, instruction in enumerate(instructions)
        if instruction[1] == "Column"
        and instruction[3] == 2
        and instruction[6] == SQLITE_COLUMN_TYPE_ONLY
    ]
    assert early_type_checks
    for index in early_type_checks:
        type_call, valid_branch, refusal, skip_reads = instructions[
            index + 1 : index + 5
        ]
        assert type_call[1] == "Function"
        assert type_call[5] == "typeof(1)"
        assert valid_branch[1] == "Eq"
        assert valid_branch[3] == index + 5
        assert refusal[1] == "Variable"
        assert type(refusal[2]) is int
        assert arguments[refusal[2] - 1] == GraphValueFailure.INVALID.value
        assert skip_reads[1] == "Goto"
        assert type(skip_reads[3]) is int
        assert skip_reads[3] > index + 5


async def insert_admission(connection: AsyncConnection, version_id: str) -> None:
    await connection.execute(
        insert(graph_version_read_admission).values(
            graph_version_id=version_id,
            format_version=1,
            db_input_bytes=ADMISSION.db_input_bytes,
            resource_transport_bytes=ADMISSION.resource_transport_bytes,
        )
    )


async def read_admission(
    connection: AsyncConnection,
    scope: Scope,
    db_bytes: int = ADMISSION.db_input_bytes,
    transport_bytes: int = ADMISSION.resource_transport_bytes,
) -> GraphVersionReadAdmission | None:
    return await read_graph_version_admission(
        connection,
        scope.namespace_id,
        scope.graph_id,
        scope.version_id,
        max_db_input_bytes=db_bytes,
        max_resource_transport_bytes=transport_bytes,
    )


async def seed_scope(database: Database) -> Scope:
    async with database.transaction() as connection:
        seeder = Seeder(connection)
        namespace_id = await seeder.namespace()
        graph_id = await seeder.graph(namespace_id)
        version_id = await seeder.graph_version(graph_id)
        await insert_admission(connection, version_id)
    return Scope(namespace_id, graph_id, version_id)


@pytest.mark.asyncio
async def test_graph_admission_matches_exact_scope_and_independent_allowances(
    database: Database,
) -> None:
    scope = await seed_scope(database)
    async with database.transaction() as connection:
        seeder = Seeder(connection)
        other_graph = await seeder.graph(scope.namespace_id, "other")
        other_namespace = await seeder.namespace("other")
        legacy = await seeder.graph_version(scope.graph_id, 2)
        missing = seeder.new_id()
    ledger = ConnectionLedger(database)

    async with database.read_snapshot() as connection:
        selected = await read_admission(connection, scope)
        refused = [
            await read_admission(connection, candidate)
            for candidate in (
                Scope(other_namespace, scope.graph_id, scope.version_id),
                Scope(scope.namespace_id, other_graph, scope.version_id),
                Scope(scope.namespace_id, scope.graph_id, legacy),
                Scope(scope.namespace_id, scope.graph_id, missing),
            )
        ]
    with pytest.raises(GraphValueRejected) as db_limit:
        async with database.read_snapshot() as connection:
            await read_admission(connection, scope, db_bytes=100)
    with pytest.raises(GraphValueRejected) as resource_limit:
        async with database.read_snapshot() as connection:
            await read_admission(connection, scope, transport_bytes=52)

    assert selected == ADMISSION
    assert refused == [None, None, None, None]
    assert db_limit.value.reason is GraphValueFailure.BUDGET
    assert resource_limit.value.reason is GraphValueFailure.BUDGET
    assert ledger.checkouts == 3
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_graph_admission_remains_in_the_original_snapshot(
    database: Database,
) -> None:
    async with database.transaction() as connection:
        seeder = Seeder(connection)
        namespace_id = await seeder.namespace()
        graph_id = await seeder.graph(namespace_id)
        version_id = await seeder.graph_version(graph_id)
    scope = Scope(namespace_id, graph_id, version_id)
    ledger = ConnectionLedger(database)
    interleaved: list[str] = []

    async def insert_witness() -> None:
        async with database.transaction() as connection:
            await insert_admission(connection, scope.version_id)

    def commit_after_scope(
        connection: object, cursor: object, statement: str, *arguments: object
    ) -> None:
        if not interleaved:
            await_(asyncio.wait_for(insert_witness(), CONDITION_DEADLINE_SECONDS))
            interleaved.append(statement)

    async with database.read_snapshot() as connection:
        before = await read_admission(connection, scope)
        event.listen(
            connection.sync_connection, "after_cursor_execute", commit_after_scope
        )
        try:
            during = await read_admission(connection, scope)
        finally:
            event.remove(
                connection.sync_connection, "after_cursor_execute", commit_after_scope
            )
    async with database.read_snapshot() as connection:
        after = await read_admission(connection, scope)

    assert before is during is None
    assert after == ADMISSION
    [scope_statement] = interleaved
    assert "graph_version_read_admission" not in scope_statement
    assert "graph_version" in scope_statement
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_graph_admission_rejects_malformed_and_foreign_stored_witnesses(
    database: Database,
) -> None:
    scope = await seed_scope(database)
    async with database.transaction() as connection:
        foreign = await Seeder(connection).namespace("foreign")
        fields = graph_version_read_admission.c
        witness = update(graph_version_read_admission).where(
            fields.graph_version_id == scope.version_id
        )
        if database.dialect is Dialect.SQLITE:
            await connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
            try:
                await connection.execute(witness.values(db_input_bytes=WIDE.encode()))
            finally:
                await connection.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")
            expected = GraphValueFailure.INVALID
        else:
            await connection.exec_driver_sql(
                "ALTER TABLE graph_version_read_admission DROP CONSTRAINT "
                "ck_graph_version_read_admission_format_version"
            )
            await connection.execute(witness.values(format_version=2))
            expected = GraphValueFailure.VERSION
    ledger = ConnectionLedger(database)

    async with database.read_snapshot() as connection:
        hidden = await read_admission(
            connection, Scope(foreign, scope.graph_id, scope.version_id)
        )
        with executed_queries(connection) as statements:
            raw = (
                await connection.execute(
                    graph_version_admission_statement(
                        database.dialect, scope.version_id, 101, 53
                    )
                )
            ).one()
        if database.dialect is Dialect.SQLITE:
            [(statement, arguments)] = statements
            instructions = await sqlite_instructions(connection, statement, arguments)
        else:
            instructions = []
            arguments = ()
    with pytest.raises(GraphValueRejected) as refused:
        async with database.read_snapshot() as connection:
            await read_admission(connection, scope)

    assert hidden is None
    assert tuple(raw) == (expected.value, None, None)
    assert refused.value.reason is expected
    assert refused.value.args == (expected.value,)
    assert ledger.outstanding == database.units_in_flight() == 0
    if database.dialect is Dialect.SQLITE:
        assert_sqlite_refuses_before_following_the_wide_field(instructions, arguments)


@pytest.mark.asyncio
async def test_graph_admission_uses_only_narrow_scope_on_a_wide_version(
    database: Database,
) -> None:
    async with database.transaction() as connection:
        if database.dialect is Dialect.POSTGRESQL:
            await connection.exec_driver_sql(
                "ALTER TABLE graph_version ALTER COLUMN ir SET STORAGE EXTERNAL"
            )
        seeder = Seeder(connection)
        namespace_id = await seeder.namespace()
        graph_id = await seeder.graph(namespace_id)
        version_id = await seeder.graph_version(graph_id, ir={"legacy": WIDE})
    scope = Scope(namespace_id, graph_id, version_id)
    opened: set[object] = set()
    details: list[str] = []

    async with database.read_snapshot() as connection:
        with executed_queries(connection) as statements:
            absent = await read_admission(connection, scope)
        [(scope_sql, scope_args), (admission_sql, admission_args)] = statements
        if database.dialect is Dialect.SQLITE:
            roots = await connection.exec_driver_sql(
                "SELECT rootpage, name FROM sqlite_schema WHERE rootpage > 0"
            )
            by_root: dict[object, object] = {}
            for entry in roots:
                root, name = tuple(entry)
                by_root[root] = name
            instructions = await sqlite_instructions(connection, scope_sql, scope_args)
            opened = {
                by_root[opcode[3]] for opcode in instructions if opcode[1] == "OpenRead"
            }
            plan = await connection.exec_driver_sql(
                "EXPLAIN QUERY PLAN " + scope_sql, scope_args
            )
            details = [str(tuple(row)[-1]) for row in plan]
            toast_size = 0
        else:
            toast_size = (
                await connection.exec_driver_sql(
                    "SELECT pg_relation_size(reltoastrelid) FROM pg_class "
                    "WHERE oid = 'graph_version'::regclass"
                )
            ).scalar_one()
        history = await graph_version_by_number(connection, namespace_id, graph_id, 1)

    assert absent is None
    assert history is not None
    assert history.ir == {"legacy": WIDE}
    assert "graph_version.ir" not in scope_sql
    assert "graph_version.resources" not in scope_sql
    assert "graph_version_read_admission" in admission_sql
    assert scope_args
    assert admission_args
    if database.dialect is Dialect.SQLITE:
        assert GRAPH_VERSION_SCOPE_INDEX in opened
        assert "graph_version" not in opened
        assert (
            f"SEARCH v USING COVERING INDEX {GRAPH_VERSION_SCOPE_INDEX} "
            "(graph_id=? AND id=?)"
        ) in details
    else:
        assert type(toast_size) is int
        assert toast_size > 0


async def install_sqlite_decoys(
    connection: AsyncConnection, original: Scope, foreign: Scope, other: Scope
) -> None:
    for statement in (
        "CREATE TEMP TABLE graph AS SELECT id, namespace_id FROM main.graph",
        "CREATE TEMP TABLE graph_version AS "
        "SELECT id, graph_id FROM main.graph_version",
        f"CREATE INDEX temp.{GRAPH_VERSION_SCOPE_INDEX} "
        "ON graph_version (graph_id, id)",
        "CREATE TEMP TABLE graph_version_read_admission AS "
        "SELECT * FROM main.graph_version_read_admission WHERE 0",
    ):
        await connection.exec_driver_sql(statement)
    await connection.execute(
        text("INSERT INTO temp.graph (id, namespace_id) VALUES (:graph, :namespace)"),
        {"graph": original.graph_id, "namespace": foreign.namespace_id},
    )
    await connection.execute(
        text("INSERT INTO temp.graph_version (id, graph_id) VALUES (:version, :graph)"),
        {"version": original.version_id, "graph": other.graph_id},
    )


async def install_postgres_decoys(
    connection: AsyncConnection, original: Scope, foreign: Scope, other: Scope
) -> None:
    for statement in (
        "CREATE TABLE graph_child () INHERITS (graph)",
        "CREATE TABLE graph_version_child () INHERITS (graph_version)",
        "CREATE TABLE admission_child () INHERITS (graph_version_read_admission)",
    ):
        await connection.exec_driver_sql(statement)
    await connection.execute(
        text("INSERT INTO graph_child SELECT * FROM ONLY graph WHERE id = :graph"),
        {"graph": original.graph_id},
    )
    await connection.execute(
        text("UPDATE ONLY graph_child SET namespace_id = :namespace"),
        {"namespace": foreign.namespace_id},
    )
    await connection.execute(
        text(
            "INSERT INTO graph_version_child SELECT * FROM ONLY graph_version "
            "WHERE id = :version"
        ),
        {"version": original.version_id},
    )
    await connection.execute(
        text("UPDATE ONLY graph_version_child SET graph_id = :graph"),
        {"graph": other.graph_id},
    )


async def insert_decoy_witnesses(
    connection: AsyncConnection, dialect: Dialect, original: Scope, missing: Scope
) -> None:
    statement = (
        text(
            "INSERT INTO temp.graph_version_read_admission VALUES (:version, 1, 97, 51)"
        )
        if dialect is Dialect.SQLITE
        else text("INSERT INTO admission_child VALUES (:version, 1, 97, 51)")
    )
    for scope in (original, missing):
        await connection.execute(statement, {"version": scope.version_id})


async def assert_original_admission_with_decoys(
    connection: AsyncConnection,
    dialect: Dialect,
    original: Scope,
    foreign: Scope,
    other: Scope,
    missing: Scope,
) -> None:
    ordinary_scope = text(
        "SELECT v.id FROM graph g JOIN graph_version v ON v.graph_id = g.id "
        "WHERE g.namespace_id = :namespace AND g.id = :graph AND v.id = :version"
    )
    ordinary_witness = text(
        "SELECT db_input_bytes FROM graph_version_read_admission "
        "WHERE graph_version_id = :version ORDER BY db_input_bytes"
    )

    ordinary_scopes: list[object] = []
    decoy_results: list[GraphVersionReadAdmission | None] = []
    scope_queries: list[list[tuple[str, tuple[object, ...]]]] = []
    for scope in (foreign, other):
        ordinary_scopes.append(
            (
                await connection.execute(
                    ordinary_scope,
                    {
                        "namespace": scope.namespace_id,
                        "graph": scope.graph_id,
                        "version": scope.version_id,
                    },
                )
            ).scalar_one()
        )
        with executed_queries(connection) as queries:
            decoy_results.append(await read_admission(connection, scope))
        scope_queries.append(queries)
    ordinary_widths: list[object] = list(
        (
            await connection.execute(ordinary_witness, {"version": original.version_id})
        ).scalars()
    )
    ordinary_missing: object = (
        await connection.execute(ordinary_witness, {"version": missing.version_id})
    ).scalar_one()
    actual = await read_admission(connection, original)
    absent = await read_admission(connection, missing)
    native = (
        await connection.execute(
            graph_version_admission_statement(
                dialect,
                original.version_id,
                ADMISSION.db_input_bytes,
                ADMISSION.resource_transport_bytes,
            )
        )
    ).one()

    assert ordinary_scopes == [original.version_id, original.version_id]
    assert ordinary_widths == ([97] if dialect is Dialect.SQLITE else [97, 101])
    assert ordinary_missing == 97
    assert decoy_results == [None, None]
    for queries in scope_queries:
        [(statement, _)] = queries
        assert "graph_version_read_admission" not in statement
    assert actual == ADMISSION
    assert absent is None
    assert tuple(native) == (
        None,
        ADMISSION.db_input_bytes,
        ADMISSION.resource_transport_bytes,
    )


@pytest.mark.asyncio
async def test_graph_admission_binds_scope_and_witness_to_original_relations(
    database: Database,
) -> None:
    dialect = database.dialect
    ledger = ConnectionLedger(database)

    async with database.transaction() as writer:
        seeder = Seeder(writer)
        namespace = await seeder.namespace()
        foreign_namespace = await seeder.namespace("foreign")
        graph_id = await seeder.graph(namespace)
        other_graph = await seeder.graph(namespace, "other")
        version = await seeder.graph_version(graph_id)
        witnessless = await seeder.graph_version(graph_id, 2)
        original = Scope(namespace, graph_id, version)
        foreign = Scope(foreign_namespace, graph_id, version)
        other = Scope(namespace, other_graph, version)
        missing = Scope(namespace, graph_id, witnessless)
        await insert_admission(writer, version)
        if dialect is Dialect.SQLITE:
            await install_sqlite_decoys(writer, original, foreign, other)
            try:
                await insert_decoy_witnesses(writer, dialect, original, missing)
                await assert_original_admission_with_decoys(
                    writer, dialect, original, foreign, other, missing
                )
            finally:
                for statement in (
                    "DROP TABLE temp.graph_version_read_admission",
                    "DROP TABLE temp.graph_version",
                    "DROP TABLE temp.graph",
                ):
                    await writer.exec_driver_sql(statement)
        else:
            await install_postgres_decoys(writer, original, foreign, other)
            await insert_decoy_witnesses(writer, dialect, original, missing)
    if dialect is Dialect.POSTGRESQL:
        async with database.read_snapshot() as reader:
            await assert_original_admission_with_decoys(
                reader, dialect, original, foreign, other, missing
            )

    assert ledger.outstanding == database.units_in_flight() == 0


async def upgrade_predecessor(database: Database) -> None:
    async with database.migration_connection() as connection:
        await connection.run_sync(
            lambda sync: command.upgrade(alembic_config(sync), "0008")
        )


class AdmissionMigrationRefused(RuntimeError):
    pass


@dataclass
class LateMigrationFailure:
    database: Database | None = None
    ledger: ConnectionLedger | None = None
    observed_stamp: bool = False

    def open(self, settings: DatabaseSettings) -> Database:
        self.database = Database(settings)
        self.ledger = ConnectionLedger(self.database)
        event.listen(
            self.database.writer_engine.sync_engine, "after_cursor_execute", self.after
        )
        return self.database

    def after(
        self, connection: Connection, cursor: object, statement: str, *arguments: object
    ) -> None:
        if statement.lstrip().startswith("UPDATE alembic_version SET version_num="):
            self.observed_stamp = True
            raise AdmissionMigrationRefused("late graph admission stamp refusal")


@pytest.mark.asyncio
async def test_graph_admission_migration_recovers_without_rewriting_history(
    empty_database_settings: DatabaseSettings,
) -> None:
    database = Database(empty_database_settings)
    interruption = LateMigrationFailure()
    try:
        await upgrade_predecessor(database)
        async with database.transaction() as connection:
            seeder = Seeder(connection)
            namespace_id = await seeder.namespace()
            graph_id = await seeder.graph(namespace_id)
            version_id = await seeder.graph_version(graph_id, ir={"legacy": WIDE})
            preparation = {
                "id": seeder.new_id(),
                "namespace_id": namespace_id,
                "graph_name": "pipeline",
                "initial_graph_id": graph_id,
                "deployment_digest": DIGEST,
                "state": "open",
                "result_graph_id": None,
                "result_graph_version_id": None,
            }
            await connection.execute(
                graph_deployment_preparation.insert().values(**preparation)
            )
        async with database.read_transaction() as connection:
            before = (await connection.execute(select(graph_version))).all()
        preparation_query = select(graph_deployment_preparation)

        with pytest.raises(
            AdmissionMigrationRefused, match="late graph admission stamp"
        ):
            await upgrade_database(empty_database_settings, interruption.open)
        async with database.read_transaction() as connection:
            rolled_back = await connection.run_sync(current_revision)
            absent = await connection.run_sync(
                lambda sync: not inspect(sync).has_table("graph_version_read_admission")
            )
            previous_indexes = await connection.run_sync(
                lambda sync: inspect(sync).get_indexes("graph_version")
            )
            rolled_back_preparation = (
                (await connection.execute(preparation_query)).mappings().one()
            )
        locks = await migration_lock_count(database)
        recovered = await upgrade_database(empty_database_settings)
        async with database.read_snapshot() as connection:
            after = (await connection.execute(select(graph_version))).all()
            recovered_preparation = (
                (await connection.execute(preparation_query)).mappings().one()
            )
            indexes = await connection.run_sync(
                lambda sync: inspect(sync).get_indexes("graph_version")
            )
            witness = await read_admission(
                connection, Scope(namespace_id, graph_id, version_id)
            )
            admitted_count = await connection.scalar(
                select(func.count()).select_from(graph_version_read_admission)
            )
            primary = await connection.run_sync(
                lambda sync: inspect(sync).get_pk_constraint(
                    "graph_version_read_admission"
                )
            )
            foreign_keys = await connection.run_sync(
                lambda sync: inspect(sync).get_foreign_keys(
                    "graph_version_read_admission"
                )
            )

        assert interruption.observed_stamp
        assert interruption.database is not None
        assert interruption.ledger is not None
        assert (
            interruption.ledger.outstanding
            == interruption.database.units_in_flight()
            == locks
            == 0
        )
        assert rolled_back == "0008"
        assert absent
        assert all(
            index["name"] != GRAPH_VERSION_SCOPE_INDEX for index in previous_indexes
        )
        assert (recovered.from_revision, recovered.to_revision, recovered.steps) == (
            "0008",
            "0020",
            12,
        )
        assert before == after
        assert dict(rolled_back_preparation) == preparation
        assert dict(recovered_preparation) == preparation
        assert witness is None
        assert admitted_count == 0
        assert primary["constrained_columns"] == ["graph_version_id"]
        [foreign_key] = foreign_keys
        assert foreign_key["constrained_columns"] == ["graph_version_id"]
        assert foreign_key["referred_table"] == "graph_version"
        assert foreign_key["referred_columns"] == ["id"]
        keys = [
            index["column_names"]
            for index in indexes
            if index["name"] == GRAPH_VERSION_SCOPE_INDEX
        ]
        assert keys == (
            [["graph_id", "id"]] if database.dialect is Dialect.SQLITE else []
        )
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("format_version", 2),
        ("db_input_bytes", 0),
        ("resource_transport_bytes", -1),
        ("db_input_bytes", "bad native count"),
    ],
)
async def test_graph_admission_schema_refuses_invalid_values(
    database: Database,
    field: str,
    value: int | str,
) -> None:
    async with database.transaction() as connection:
        seeder = Seeder(connection)
        namespace_id = await seeder.namespace()
        graph_id = await seeder.graph(namespace_id)
        version_id = await seeder.graph_version(graph_id)
    values: dict[str, int | str] = {
        "graph_version_id": version_id,
        "format_version": 1,
        "db_input_bytes": 101,
        "resource_transport_bytes": 53,
    }
    values[field] = value

    with pytest.raises(DBAPIError):
        async with database.transaction() as connection:
            await connection.execute(
                insert(graph_version_read_admission).values(values)
            )
    async with database.read_snapshot() as connection:
        stored = await read_admission(
            connection, Scope(namespace_id, graph_id, version_id)
        )

    assert stored is None
