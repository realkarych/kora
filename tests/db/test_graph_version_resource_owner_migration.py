import pytest
from alembic import command
from sqlalchemy import Connection, Table, inspect, select
from sqlalchemy.exc import DBAPIError, IntegrityError

from kora.db.connection import Database, DatabaseSettings, Dialect
from kora.db.graph_version_resource_owner_v1 import REVERSE_INDEX
from kora.db.migrations import (
    current_revision,
    upgrade_database,
    upgrade_with_diagnostics,
)
from kora.db.migrations.environment import alembic_config
from kora.db.tables import (
    graph_deployment_preparation_capacity,
    graph_version,
    graph_version_read_admission,
)
from kora.db.tables import graph_version_resource_owner as owner
from kora.ir.json import JsonObject
from tests.db.seed import Seeder
from tests.db.test_graph_preparation_capacity_migration import (
    RETAINED_RELATIONS,
    populate_predecessor,
    upgrade_capacity_revision,
)
from tests.db.test_graph_read_admission import executed_queries
from tests.engine.test_graph_command_manifests import ZERO, authored_manifest, resource
from tests.engine.test_graph_resource_references import expected_ref

PREDECESSOR: str = "0012"
REVISION: str = "0013"
OWNER_TABLE_COLUMNS: tuple[str, ...] = (
    "graph_version_id",
    "ordinal",
    "subgraph_name",
    "node_name",
    "file_name",
    "store_id",
    "payload_digest",
    "object_incarnation",
    "total_size",
    "payload_state",
)
PREDECESSOR_TABLES: tuple[Table, ...] = (
    *RETAINED_RELATIONS,
    graph_deployment_preparation_capacity,
    graph_version_read_admission,
)


def owner_layout(
    connection: Connection,
) -> tuple[
    tuple[tuple[str, bool], ...],
    list[str] | None,
    list[tuple[list[str], str, list[str], bool]],
    list[list[str]],
    list[tuple[str | None, list[str | None]]],
]:
    inspector = inspect(connection)
    columns = tuple(
        (column["name"], column["nullable"])
        for column in inspector.get_columns(owner.name)
    )
    primary = inspector.get_pk_constraint(owner.name)["constrained_columns"]
    foreign = [
        (
            key["constrained_columns"],
            key["referred_table"],
            key["referred_columns"],
            bool(key.get("options")),
        )
        for key in inspector.get_foreign_keys(owner.name)
    ]
    unique = [
        key["column_names"] for key in inspector.get_unique_constraints(owner.name)
    ]
    indexes = [
        (index["name"], index["column_names"])
        for index in inspector.get_indexes(owner.name)
        if not index.get("duplicates_constraint")
    ]
    return columns, primary, foreign, unique, indexes


def owner_values(version_id: str) -> dict[str, object]:
    return {
        "graph_version_id": version_id,
        "ordinal": 0,
        "subgraph_name": None,
        "node_name": "producer",
        "file_name": "assets/é.sh",
        "store_id": "Store",
        "payload_digest": ZERO,
        "object_incarnation": "Provider-Original",
        "total_size": 0,
        "payload_state": "present",
    }


async def seed_versions(database: Database) -> tuple[str, str]:
    async with database.transaction() as connection:
        seeder = Seeder(connection)
        namespace_id = await seeder.namespace()
        graph_id = await seeder.graph(namespace_id)
        return (
            await seeder.graph_version(graph_id),
            await seeder.graph_version(graph_id, number=2),
        )


@pytest.mark.asyncio
async def test_resource_owner_native_keys_preserve_exact_occurrences(
    database: Database,
) -> None:
    version_id, other_id = await seed_versions(database)
    root = owner_values(version_id)
    body = root | {"ordinal": 63, "subgraph_name": "body"}
    replacement = root | {
        "ordinal": 1,
        "file_name": "replacement.sh",
        "store_id": "store",
        "object_incarnation": "a" * 32,
        "total_size": 262144,
    }
    other = owner_values(other_id) | {"payload_state": "missing"}
    older = root | {
        "ordinal": 2,
        "file_name": "older.sh",
        "payload_state": "expired",
    }
    expiring = root | {
        "ordinal": 3,
        "file_name": "expiring.sh",
        "object_incarnation": "~" * 256,
        "payload_state": "expiring",
    }
    fields = owner.c

    async with database.transaction() as connection:
        await connection.execute(
            owner.insert(), [root, body, replacement, other, older, expiring]
        )
    async with database.read_transaction() as connection:
        layout = await connection.run_sync(owner_layout)
        selected = (
            (
                await connection.execute(
                    select(owner)
                    .where(fields.graph_version_id == version_id)
                    .order_by(fields.ordinal)
                )
            )
            .mappings()
            .all()
        )
        present = (
            await connection.execute(
                select(fields.graph_version_id, fields.ordinal)
                .where(
                    fields.store_id == "Store",
                    fields.payload_digest == ZERO,
                    fields.payload_state == "present",
                )
                .order_by(fields.graph_version_id, fields.ordinal)
            )
        ).all()

    assert layout == (
        tuple((name, name == "subgraph_name") for name in OWNER_TABLE_COLUMNS),
        ["graph_version_id", "ordinal"],
        [(["graph_version_id"], "graph_version", ["id"], False)],
        [],
        [
            (
                REVERSE_INDEX,
                [
                    "store_id",
                    "payload_digest",
                    "payload_state",
                    "graph_version_id",
                    "ordinal",
                ],
            )
        ],
    )
    assert [dict(row) for row in selected] == [root, replacement, older, expiring, body]
    assert present == [(version_id, 0), (version_id, 63)]


@pytest.mark.asyncio
async def test_resource_owner_refuses_invalid_native_rows(database: Database) -> None:
    version_id, _ = await seed_versions(database)
    valid = owner_values(version_id)
    invalid: list[dict[str, object]] = [
        {"graph_version_id": "7" + "Z" * 25},
        {"graph_version_id": version_id.lower()},
        {"ordinal": None},
        {"ordinal": -1},
        {"ordinal": 64},
        {"node_name": "Upper"},
        {"subgraph_name": "a" * 65},
        {"file_name": ""},
        {"store_id": "contains a space"},
        {"payload_digest": "sha256:" + "A" * 64},
        {"object_incarnation": ""},
        {"object_incarnation": "has a space"},
        {"object_incarnation": "a" * 257},
        {"object_incarnation": "é"},
        {"total_size": -1},
        {"total_size": 262145},
        {"payload_state": "Present"},
    ]
    if database.dialect is Dialect.SQLITE:
        invalid.extend(
            [
                {"ordinal": 0.5},
                {"total_size": 0.5},
                {"file_name": b"assets/script.sh"},
                {"file_name": "assets/script.sh\x00suffix"},
            ]
        )

    for changes in invalid:
        with pytest.raises(DBAPIError):
            async with database.transaction() as connection:
                await connection.execute(owner.insert().values(**(valid | changes)))
    async with database.transaction() as connection:
        await connection.execute(owner.insert().values(**valid))
    with pytest.raises(IntegrityError):
        async with database.transaction() as connection:
            await connection.execute(
                owner.insert().values(**(valid | {"file_name": "different"}))
            )
    async with database.read_transaction() as connection:
        saved = (await connection.execute(select(owner))).mappings().one()

    assert dict(saved) == valid


async def seed_retained_history(database: Database) -> str:
    await populate_predecessor(database)
    async with database.migration_connection() as connection:
        await connection.run_sync(upgrade_capacity_revision)
    file = resource("assets/é.sh")
    physical: JsonObject = {
        "interpretation": "kora.graph-resource-refs.v1",
        "commands": [
            {
                "path": ["graph", "nodes", "producer"],
                "resource_set_digest": authored_manifest([file])["resource_set_digest"],
                "resources": [{**file, "storage_ref": expected_ref()}],
            },
        ],
    }
    async with database.transaction() as connection:
        version_id: str = (
            await connection.execute(select(graph_version.c.id))
        ).scalar_one()
        await connection.execute(graph_version.update().values(resources=physical))
        await connection.execute(
            graph_version_read_admission.insert().values(
                graph_version_id=version_id,
                format_version=1,
                db_input_bytes=4096,
                resource_transport_bytes=1024,
            )
        )
        await connection.execute(
            graph_deployment_preparation_capacity.insert().values(id=1, occupied=1)
        )
    return version_id


async def retained_predecessor_rows(
    database: Database,
) -> tuple[tuple[dict[str, object], ...], ...]:
    rows: list[tuple[dict[str, object], ...]] = []
    async with database.read_transaction() as connection:
        for table in PREDECESSOR_TABLES:
            selected = (await connection.execute(select(table))).mappings().all()
            rows.append(tuple(dict(row) for row in selected))
    return tuple(rows)


@pytest.mark.asyncio
async def test_resource_owner_upgrade_preserves_history_without_backfill(
    empty_database_settings: DatabaseSettings,
) -> None:
    database = Database(empty_database_settings)
    try:
        version_id = await seed_retained_history(database)
        before = await retained_predecessor_rows(database)

        async with database.migration_connection() as connection:
            with executed_queries(connection) as observed:
                report = await connection.run_sync(upgrade_with_diagnostics)
        async with database.transaction() as connection:
            initially_empty = (await connection.execute(select(owner))).all()
            await connection.execute(owner.insert().values(**owner_values(version_id)))
        repeated = await upgrade_database(empty_database_settings)
        with pytest.raises(RuntimeError, match="owner history cannot be downgraded"):
            async with database.migration_connection() as connection:
                await connection.run_sync(
                    lambda sync: command.downgrade(alembic_config(sync), PREDECESSOR)
                )
        after = await retained_predecessor_rows(database)
        async with database.read_transaction() as connection:
            revision = await connection.run_sync(current_revision)
            preserved = (await connection.execute(select(owner))).mappings().one()
    finally:
        await database.close()

    assert (report.from_revision, report.to_revision, report.steps) == (
        PREDECESSOR,
        REVISION,
        1,
    )
    assert (repeated.from_revision, repeated.to_revision, repeated.steps) == (
        REVISION,
        REVISION,
        0,
    )
    assert revision == REVISION
    assert initially_empty == []
    assert all(len(rows) == 1 for rows in before)
    assert after == before
    assert dict(preserved) == owner_values(version_id)
    statements = [" ".join(statement.lower().split()) for statement, _ in observed]
    assert (
        sum(
            statement.startswith(f"create table {owner.name}")
            for statement in statements
        )
        == 1
    )
    for statement in statements:
        for table in PREDECESSOR_TABLES:
            assert not any(
                f"{prefix}{table.name} " in f"{statement} "
                for prefix in (
                    "from ",
                    "join ",
                    "insert into ",
                    "update ",
                    "alter table ",
                )
            )
