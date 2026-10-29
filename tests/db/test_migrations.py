import ast
import asyncio
import shutil
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, inspect, select, text
from sqlalchemy.exc import DBAPIError

from kora.db.connection import Database, DatabaseSettings, Dialect
from kora.db.migrations import (
    current_revision,
    schema_differences,
    upgrade_database,
    upgrade_to_head,
)
from kora.db.migrations import environment as environment_module
from kora.db.tables import metadata
from tests.support.warning_filters import expression_index_warnings_ignored

HEAD_REVISION: str = "0003"


def differences_ignoring_expression_indexes(connection: Connection) -> list[str]:
    with expression_index_warnings_ignored():
        return list(schema_differences(connection))


def server_defaults(connection: Connection) -> list[tuple[str, str]]:
    inspector = inspect(connection)
    found: list[tuple[str, str]] = []
    for table_name in inspector.get_table_names():
        for column in inspector.get_columns(table_name):
            if column.get("default") is not None:
                found.append((table_name, str(column["name"])))
    return found


@pytest.mark.asyncio
async def test_migration_reaches_head_and_matches_the_tables(
    empty_database_settings: DatabaseSettings,
) -> None:
    report = await upgrade_database(empty_database_settings)

    database = Database(empty_database_settings)
    try:
        async with database.read_transaction() as connection:
            revision = await connection.run_sync(current_revision)
            differences = await connection.run_sync(
                differences_ignoring_expression_indexes
            )
            table_names = await connection.run_sync(
                lambda sync: inspect(sync).get_table_names()
            )
    finally:
        await database.close()
    assert (report.from_revision, report.to_revision, report.steps) == (
        None,
        HEAD_REVISION,
        3,
    )
    assert revision == HEAD_REVISION
    assert differences == []
    assert set(metadata.tables) <= set(table_names)


@pytest.mark.asyncio
async def test_migration_rerun_is_a_no_op(
    empty_database_settings: DatabaseSettings,
) -> None:
    first = await upgrade_database(empty_database_settings)

    second = await upgrade_database(empty_database_settings)

    assert (first.steps, second.steps) == (3, 0)
    assert second.from_revision == HEAD_REVISION
    assert second.to_revision == HEAD_REVISION


@pytest.mark.asyncio
async def test_no_column_has_a_server_default(database: Database) -> None:
    async with database.read_transaction() as connection:
        defaults = await connection.run_sync(server_defaults)

    assert defaults == []


@pytest.mark.asyncio
async def test_migration_lock_wait_is_bounded(database: Database) -> None:
    if database.dialect is not Dialect.POSTGRESQL:
        async with database.transaction() as connection:
            report = await connection.run_sync(upgrade_to_head)
        assert report.steps == 0
        return
    async with database.transaction() as holder:
        await holder.execute(
            text("LOCK TABLE alembic_version IN ACCESS EXCLUSIVE MODE")
        )
        locked_run = asyncio.create_task(run_upgrade(database))

        with pytest.raises(DBAPIError, match="lock timeout"):
            await locked_run

    async with database.transaction() as connection:
        report = await connection.run_sync(upgrade_to_head)
        revision = (
            await connection.execute(
                select(text("version_num")).select_from(text("alembic_version"))
            )
        ).scalar_one()

    assert report.steps == 0
    assert revision == HEAD_REVISION


async def run_upgrade(database: Database) -> None:
    async with database.transaction() as connection:
        await connection.run_sync(upgrade_to_head)


def test_script_template_renders_without_comments(tmp_path: Path) -> None:
    source = Path(environment_module.__file__).resolve().parent
    scripts = tmp_path / "migrations"
    shutil.copytree(source, scripts, ignore=shutil.ignore_patterns("__pycache__"))
    config = Config()
    config.set_main_option("script_location", str(scripts))
    config.set_main_option("prepend_sys_path", "")

    script = ScriptDirectory.from_config(config).generate_revision(
        "0004", "template probe", head="0003"
    )

    assert script is not None
    rendered = Path(script.path).read_text(encoding="utf-8")
    tree = ast.parse(rendered)
    assert not any(line.lstrip().startswith("#") for line in rendered.splitlines())
    assert ast.get_docstring(tree) is None
    assert "def upgrade() -> None:" in rendered
    assert "down_revision: str | None = '0003'" in rendered
