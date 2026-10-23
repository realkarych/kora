import asyncio
import json
import sqlite3
import stat
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import text

from kora.db.connection import Database, DatabaseSettings, Dialect, parse_database_url
from kora.db.migrations import current_revision
from tests.db.seed import Seeder, seed_predecessor
from tests.db.test_pricing_migration import price_check
from tests.observability.conftest import ProcessResult, run_kora

HEAD_REVISION: str = "0002"
HIDDEN_MARKER: str = "PASSWORD-MARKER-4471"
USER_MARKER: str = "usermarker4472"
HOST_MARKER: str = "hostmarker4473.invalid"
CONFIGURATION_PREFIX: str = "kora: invalid database configuration: KORA_DATABASE_URL: "
DEBUG_ENVIRONMENT: dict[str, str] = {
    "KORA_LOG_LEVEL": "DEBUG",
    "KORA_LOG_THIRD_PARTY": "sqlalchemy=DEBUG,alembic=DEBUG,asyncio=DEBUG",
    "KORA_LOG_COMPONENT_LEVELS": "db=DEBUG",
}


def upgrade(environment: Mapping[str, str]) -> ProcessResult:
    return run_kora(["db", "upgrade"], {**DEBUG_ENVIRONMENT, **environment})


def sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


def records(result: ProcessResult) -> list[dict[str, object]]:
    return [json.loads(line) for line in result.stderr_lines() if line.startswith("{")]


def events(result: ProcessResult) -> list[str]:
    return [str(record["event"]) for record in records(result)]


def test_db_upgrade_succeeds_and_reruns_as_a_no_op(tmp_path: Path) -> None:
    path = tmp_path / "kora.db"
    environment = {"KORA_DATABASE_URL": sqlite_url(path)}

    first = upgrade(environment)
    second = upgrade(environment)

    assert (first.code, first.stdout) == (0, f"{HEAD_REVISION}\n".encode())
    assert (second.code, second.stdout) == (0, f"{HEAD_REVISION}\n".encode())
    assert "db.migration.started" in events(first)
    assert "db.migration.finished" in events(first)
    first_finished = [
        record
        for record in records(first)
        if record["event"] == "db.migration.finished"
    ]
    second_finished = [
        record
        for record in records(second)
        if record["event"] == "db.migration.finished"
    ]
    assert first_finished[0]["steps"] == 2
    assert second_finished[0]["steps"] == 0
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with closing(sqlite3.connect(path)) as connection:
        version = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchall()
    assert version == [(HEAD_REVISION,)]


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "   ",
        "not a url",
        "mysql://x:y@h:1/d",
        "sqlite+aiosqlite://",
        "sqlite+aiosqlite:///:memory:",
        f"sqlite+aiosqlite://{HOST_MARKER}/path.db",
        f"postgresql+asyncpg://{USER_MARKER}:{HIDDEN_MARKER}@{HOST_MARKER}/db",
        f"postgresql+asyncpg://{USER_MARKER}@{HOST_MARKER}:5432/db",
        f"postgresql+asyncpg://{USER_MARKER}:@{HOST_MARKER}:5432/db",
        f"postgresql+asyncpg://{USER_MARKER}:{HIDDEN_MARKER}@{HOST_MARKER}:abc/db",
        f"postgresql+asyncpg://{USER_MARKER}:{HIDDEN_MARKER}@{HOST_MARKER}:/db",
        f"postgresql://{USER_MARKER}:{HIDDEN_MARKER}@{HOST_MARKER}:5432/db",
    ],
)
def test_db_upgrade_rejects_invalid_configuration_with_one_line(
    value: str | None,
) -> None:
    environment = {} if value is None else {"KORA_DATABASE_URL": value}

    result = upgrade(environment)

    text = result.stderr.decode("utf-8")
    assert result.code == 78
    assert result.stdout == b""
    configuration_lines = [
        line for line in result.stderr_lines() if line.startswith(CONFIGURATION_PREFIX)
    ]
    assert len(configuration_lines) == 1
    assert HIDDEN_MARKER not in text
    assert USER_MARKER not in text
    assert HOST_MARKER not in text
    assert events(result)[-1] == "process.stopped"


def test_db_upgrade_unreachable_database_exits_seventy_without_the_url() -> None:
    url = (
        f"postgresql+asyncpg://{USER_MARKER}:{HIDDEN_MARKER}@127.0.0.1:1/{USER_MARKER}"
    )

    result = upgrade({"KORA_DATABASE_URL": url})

    text = result.stderr.decode("utf-8")
    assert result.code == 70
    assert result.stdout == b""
    assert events(result).count("db.migration.failed") == 1
    assert events(result).count("process.failed") == 1
    assert HIDDEN_MARKER not in text
    assert USER_MARKER not in text
    assert "127.0.0.1" not in text
    failed = [
        record for record in records(result) if record["event"] == "db.migration.failed"
    ]
    chain = cast(list[dict[str, object]], failed[0]["exception"])
    assert chain
    assert all(set(entry) <= {"type", "module", "frames"} for entry in chain)


def test_db_upgrade_without_a_command_prints_usage() -> None:
    result = run_kora(["db"], {})

    assert result.code == 2
    assert result.stdout == b""
    assert result.stderr_lines()[0].startswith("Usage: kora db")


def test_sqlite_files_are_private(tmp_path: Path) -> None:
    path = tmp_path / "private.db"
    environment = {"KORA_DATABASE_URL": sqlite_url(path)}

    result = upgrade(environment)
    modes_while_writing = asyncio.run(companion_modes_during_a_write(path))

    assert result.code == 0
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert sorted(modes_while_writing) == [
        "private.db",
        "private.db-shm",
        "private.db-wal",
    ]
    assert all(mode == 0o600 for mode in modes_while_writing.values()), (
        modes_while_writing
    )


async def companion_modes_during_a_write(path: Path) -> dict[str, int]:
    database = Database(parse_database_url(sqlite_url(path)))
    try:
        async with database.transaction() as connection:
            await Seeder(connection).namespace("private")
            return {
                candidate.name: stat.S_IMODE(candidate.stat().st_mode)
                for candidate in path.parent.iterdir()
                if candidate.name.startswith(path.name)
            }
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_db_upgrade_partial_failure_is_content_free(
    empty_database_settings: DatabaseSettings,
) -> None:
    database = Database(empty_database_settings)
    marker = "PRIVATE-ACCOUNTING-MARKER"
    try:
        async with database.transaction() as connection:
            await connection.run_sync(seed_predecessor)
            if database.dialect is Dialect.POSTGRESQL:
                await connection.exec_driver_sql(
                    "ALTER TABLE price_table ADD COLUMN version INTEGER"
                )
                await connection.exec_driver_sql(
                    "ALTER TABLE price_table ADD COLUMN normalization_version INTEGER"
                )
                await connection.execute(
                    text(
                        "UPDATE price_table SET version = 1, model = :marker "
                        "WHERE id = 'old-price'"
                    ),
                    {"marker": marker},
                )
                await connection.exec_driver_sql(
                    "ALTER TABLE price_table ADD CONSTRAINT "
                    f"ck_price_table_revision CHECK ({price_check()}) NOT VALID"
                )
            else:
                await connection.exec_driver_sql(
                    "CREATE TABLE pricing_blocker (price_id VARCHAR(26) "
                    "REFERENCES price_table(id))"
                )
                await connection.exec_driver_sql(
                    "INSERT INTO pricing_blocker VALUES ('old-price')"
                )
        await database.close()
        environment = {
            "KORA_DATABASE_URL": empty_database_settings.url.render_as_string(
                hide_password=False
            )
        }

        failed = await asyncio.to_thread(upgrade, environment)
        database = Database(empty_database_settings)
        async with database.transaction() as connection:
            revision = await connection.run_sync(current_revision)
            if database.dialect is Dialect.POSTGRESQL:
                await connection.exec_driver_sql(
                    "UPDATE price_table SET version = NULL WHERE id = 'old-price'"
                )
            else:
                await connection.exec_driver_sql("DROP TABLE pricing_blocker")
        await database.close()
        recovered = await asyncio.to_thread(upgrade, environment)

        assert revision == "0001"
        assert failed.code == 70
        assert failed.stdout == b""
        assert marker not in failed.stderr.decode()
        assert "db.migration.finished" not in events(failed)
        assert events(failed).count("db.transaction.failed") == 1
        assert events(failed).count("db.migration.failed") == 1
        assert recovered.code == 0
        assert recovered.stdout == b"0002\n"
        assert events(recovered).count("db.migration.finished") == 1
    finally:
        await database.close()
