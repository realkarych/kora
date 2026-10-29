import asyncio
from pathlib import Path

import pytest
from sqlalchemy import Connection, inspect, text

from kora.db.connection import Database, DatabaseSettings
from tests.db.test_db_upgrade_process import records
from tests.observability.conftest import ProcessResult, run_kora
from tests.support.databases import (
    EMPTY_TEMPLATE,
    DatabaseKind,
    PostgresServer,
    sqlite_settings,
)
from tests.support.warning_filters import expression_index_warnings_ignored

LEVELS: tuple[str, ...] = ("ERROR", "DEBUG")


def upgrade_process(settings: DatabaseSettings, level: str) -> ProcessResult:
    url = settings.url.render_as_string(hide_password=False)
    return run_kora(
        ["db", "upgrade"],
        {
            "KORA_DATABASE_URL": url,
            "KORA_LOG_LEVEL": level,
            "KORA_LOG_COMPONENT_LEVELS": f"db={level}",
            "KORA_LOG_THIRD_PARTY": f"sqlalchemy={level},alembic={level}",
        },
    )


def schema_snapshot(connection: Connection) -> dict[str, object]:
    inspector = inspect(connection)
    tables: dict[str, object] = {}
    for table_name in sorted(inspector.get_table_names()):
        columns = [
            (str(column["name"]), str(column["type"]), bool(column["nullable"]))
            for column in inspector.get_columns(table_name)
        ]
        with expression_index_warnings_ignored():
            reflected = inspector.get_indexes(table_name)
        indexes = sorted(
            (str(index["name"]), list(index["column_names"]), bool(index["unique"]))
            for index in reflected
        )
        tables[table_name] = {"columns": columns, "indexes": indexes}
    version = connection.execute(text("SELECT version_num FROM alembic_version")).all()
    return {"tables": tables, "version": [tuple(row) for row in version]}


async def snapshot(settings: DatabaseSettings) -> dict[str, object]:
    database = Database(settings)
    try:
        async with database.read_transaction() as connection:
            return await connection.run_sync(schema_snapshot)
    finally:
        await database.close()


def test_log_filtering_does_not_change_durable_events(
    database_kind: DatabaseKind, request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    created: list[str] = []
    server: PostgresServer | None = None
    if database_kind is DatabaseKind.POSTGRESQL:
        server = request.getfixturevalue("postgres_server")
    settings_by_level: dict[str, DatabaseSettings] = {}
    for level in LEVELS:
        if server is None:
            settings_by_level[level] = sqlite_settings(tmp_path / f"{level}.db")
        else:
            name = f"kora_filtering_{level.lower()}_{id(request) & 0xFFFF:04x}"
            server.create_database(name, EMPTY_TEMPLATE)
            created.append(name)
            settings_by_level[level] = server.settings_for(name)
    try:
        results = {
            level: upgrade_process(settings, level)
            for level, settings in settings_by_level.items()
        }
        snapshots = {
            level: asyncio.run(snapshot(settings))
            for level, settings in settings_by_level.items()
        }
    finally:
        if server is not None:
            for name in created:
                server.drop_database(name)

    assert [result.code for result in results.values()] == [0, 0]
    assert [result.stdout for result in results.values()] == [b"0003\n", b"0003\n"]
    assert snapshots["ERROR"] == snapshots["DEBUG"]
    assert snapshots["ERROR"]["version"] == [("0003",)]
    assert len(records(results["ERROR"])) < len(records(results["DEBUG"]))
    debug_events = [str(record["event"]) for record in records(results["DEBUG"])]
    assert debug_events.index("db.migration.started") < debug_events.index(
        "db.migration.finished"
    )
    assert all(
        record["level"] in {"ERROR", "CRITICAL"} for record in records(results["ERROR"])
    )
