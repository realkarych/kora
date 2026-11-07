import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import Connection, Table, inspect, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.connection import Database, DatabaseSettings, Dialect
from kora.db.migrations import current_revision, upgrade_database
from kora.db.migrations.environment import alembic_config, pending_steps
from kora.db.tables import (
    graph_deployment_preparation as preparation,
)
from kora.db.tables import (
    graph_deployment_stage as stage,
)
from kora.db.tables import (
    graph_deployment_stage_occurrence as occurrence,
)
from kora.db.tables import (
    graph_version,
    storage_address_fence,
)
from tests.db.seed import Seeder
from tests.support.databases import sqlite_settings

PREDECESSOR: str = "0010"
REVISION: str = "0011"
ADDRESS_DIGEST: str = "sha256:" + "a" * 64
MAX_COUNT: int = 2**63 - 1
MAX_STAGE_ID: str = "7" + "Z" * 25
STAGE_INSERT = text(
    "INSERT INTO graph_deployment_stage (stage_id, namespace_id, preparation_id, "
    "store_id, payload_digest, fence_incarnation, total_size) "
    "VALUES (:stage_id, :namespace_id, :preparation_id, "
    ":store_id, :payload_digest, :fence_incarnation, :total_size)"
)
OCCURRENCE_INSERT = text(
    "INSERT INTO graph_deployment_stage_occurrence "
    "(namespace_id, preparation_id, ordinal, stage_id) "
    "VALUES (:namespace_id, :preparation_id, :ordinal, :stage_id)"
)


@dataclass(frozen=True)
class PreparedScope:
    namespace_id: str
    preparation_id: str


async def prepare_graph(
    connection: AsyncConnection, namespace_name: str = "default"
) -> PreparedScope:
    seeder = Seeder(connection)
    namespace_id = await seeder.namespace(namespace_name)
    graph_id = await seeder.graph(namespace_id)
    await seeder.graph_version(graph_id)
    preparation_id = seeder.new_id()
    await connection.execute(
        preparation.insert().values(
            id=preparation_id,
            namespace_id=namespace_id,
            graph_name="pipeline",
            initial_graph_id=graph_id,
            deployment_digest="a" * 64,
            state="open",
        )
    )
    return PreparedScope(namespace_id, preparation_id)


def stage_values(
    scope: PreparedScope, stage_id: str, incarnation: int = MAX_COUNT
) -> dict[str, str | int]:
    return {
        "stage_id": stage_id,
        "namespace_id": scope.namespace_id,
        "preparation_id": scope.preparation_id,
        "store_id": "Store",
        "payload_digest": ADDRESS_DIGEST,
        "fence_incarnation": incarnation,
        "total_size": 0,
    }


def occurrence_values(
    scope: PreparedScope, stage_id: str, ordinal: int
) -> dict[str, str | int]:
    return {
        "namespace_id": scope.namespace_id,
        "preparation_id": scope.preparation_id,
        "ordinal": ordinal,
        "stage_id": stage_id,
    }


def key_layout(
    connection: Connection, table: Table
) -> tuple[list[str] | None, list[list[str]], list[list[str | None]]]:
    inspector = inspect(connection)
    primary = inspector.get_pk_constraint(table.name)["constrained_columns"]
    unique = [
        key["column_names"] for key in inspector.get_unique_constraints(table.name)
    ]
    indexes = [
        index["column_names"]
        for index in inspector.get_indexes(table.name)
        if index.get("duplicates_constraint") is None
    ]
    return primary, unique, indexes


@pytest.mark.asyncio
async def test_stage_relations_have_exact_native_index_order(
    database: Database,
) -> None:
    async with database.read_transaction() as connection:
        stage_layout = await connection.run_sync(key_layout, stage)
        occurrence_layout = await connection.run_sync(key_layout, occurrence)

    assert stage_layout == (
        ["stage_id"],
        [["store_id", "payload_digest", "preparation_id"]],
        [["preparation_id", "stage_id"]],
    )
    assert occurrence_layout == (
        ["preparation_id", "ordinal"],
        [],
        [["stage_id", "preparation_id", "ordinal"]],
    )


@pytest.mark.asyncio
async def test_stage_keys_and_shared_bytes_follow_exact_preparation(
    database: Database,
) -> None:
    async with database.transaction() as connection:
        first = await prepare_graph(connection)
        other = await prepare_graph(connection, "other")
        seeder = Seeder(connection)
        other_stage, case_stage, duplicate_id = (seeder.new_id() for _ in range(3))
        first_values = stage_values(first, MAX_STAGE_ID)
        other_values = {**stage_values(other, other_stage), "total_size": 262144}
        case_values = {**stage_values(first, case_stage), "store_id": "store"}
    bindings = [
        occurrence_values(first, MAX_STAGE_ID, 0),
        occurrence_values(first, MAX_STAGE_ID, 63),
    ]

    async with database.transaction() as connection:
        await connection.execute(
            stage.insert(), [first_values, other_values, case_values]
        )
        await connection.execute(occurrence.insert(), bindings)
    for bad_stage in (
        {**first_values, "stage_id": duplicate_id},
        {**first_values, "store_id": "elsewhere"},
    ):
        with pytest.raises(IntegrityError):
            async with database.transaction() as connection:
                await connection.execute(stage.insert().values(**bad_stage))
    with pytest.raises(IntegrityError):
        async with database.transaction() as connection:
            await connection.execute(
                occurrence.insert().values(**occurrence_values(first, case_stage, 0))
            )
    async with database.read_transaction() as connection:
        found = (
            (
                await connection.execute(
                    select(stage).where(
                        stage.c.store_id == "Store",
                        stage.c.payload_digest == ADDRESS_DIGEST,
                        stage.c.preparation_id == first.preparation_id,
                    )
                )
            )
            .mappings()
            .one()
        )
        stored = (
            (
                await connection.execute(
                    select(occurrence)
                    .where(occurrence.c.stage_id == MAX_STAGE_ID)
                    .order_by(occurrence.c.preparation_id, occurrence.c.ordinal)
                )
            )
            .mappings()
            .all()
        )

    assert dict(found) == first_values
    assert [dict(row) for row in stored] == bindings


@pytest.mark.asyncio
async def test_stage_parent_existence_and_scope_contradictions(
    database: Database,
) -> None:
    async with database.transaction() as connection:
        first = await prepare_graph(connection)
        other = await prepare_graph(connection, "other")
        seeder = Seeder(connection)
        stage_id = seeder.new_id()
        missing = seeder.new_id()
        wrong_namespace = {
            **occurrence_values(first, stage_id, 0),
            "namespace_id": other.namespace_id,
        }
        wrong_preparation = occurrence_values(other, stage_id, 1)
        await connection.execute(stage.insert().values(**stage_values(first, stage_id)))

    with pytest.raises(IntegrityError):
        async with database.transaction() as connection:
            await connection.execute(
                stage.insert().values(
                    **{**stage_values(first, missing), "preparation_id": missing}
                )
            )
    with pytest.raises(IntegrityError):
        async with database.transaction() as connection:
            await connection.execute(
                occurrence.insert().values(**occurrence_values(first, missing, 0))
            )
    async with database.transaction() as connection:
        await connection.execute(
            occurrence.insert(), [wrong_namespace, wrong_preparation]
        )
    with pytest.raises(IntegrityError):
        async with database.transaction() as connection:
            await connection.execute(stage.delete().where(stage.c.stage_id == stage_id))
    with pytest.raises(IntegrityError):
        async with database.transaction() as connection:
            await connection.execute(
                preparation.delete().where(preparation.c.id == first.preparation_id)
            )
    async with database.read_transaction() as connection:
        by_preparation = (
            (
                await connection.execute(
                    select(occurrence)
                    .where(occurrence.c.preparation_id == first.preparation_id)
                    .order_by(occurrence.c.ordinal)
                )
            )
            .mappings()
            .all()
        )
        by_stage = (
            (
                await connection.execute(
                    select(occurrence).where(occurrence.c.stage_id == stage_id)
                )
            )
            .mappings()
            .all()
        )

    assert [dict(row) for row in by_preparation] == [wrong_namespace]
    assert {row["preparation_id"]: dict(row) for row in by_stage} == {
        first.preparation_id: wrong_namespace,
        other.preparation_id: wrong_preparation,
    }


@pytest.mark.asyncio
async def test_stage_native_domains_refuse_malformed_rows(database: Database) -> None:
    async with database.transaction() as connection:
        scope = await prepare_graph(connection)
    candidate = stage_values(scope, MAX_STAGE_ID)
    binding = occurrence_values(scope, MAX_STAGE_ID, 0)
    bad_stage = (
        ("stage_id", "8" + "Z" * 25),
        ("stage_id", MAX_STAGE_ID + " "),
        ("stage_id", MAX_STAGE_ID + "\0"),
        ("namespace_id", MAX_STAGE_ID.lower()),
        ("store_id", "a" * 256 + " "),
        ("store_id", "store\0"),
        ("payload_digest", ADDRESS_DIGEST + " "),
        ("payload_digest", ADDRESS_DIGEST[:-1] + "A"),
        ("fence_incarnation", 0),
        ("total_size", -1),
        ("total_size", 262145),
    )
    bad_binding = (("ordinal", -1), ("ordinal", 64), ("namespace_id", "Z" * 26))
    if database.dialect is Dialect.SQLITE:
        bad_stage = (
            *bad_stage,
            ("stage_id", MAX_STAGE_ID.encode()),
            ("payload_digest", ADDRESS_DIGEST.encode()),
            ("fence_incarnation", 1.5),
            ("total_size", 1.5),
        )
        bad_binding = (*bad_binding, ("ordinal", 1.5))

    for field, value in bad_stage:
        with pytest.raises(DBAPIError):
            async with database.transaction() as connection:
                await connection.execute(STAGE_INSERT, {**candidate, field: value})
    async with database.transaction() as connection:
        await connection.execute(STAGE_INSERT, candidate)
    for field, value in bad_binding:
        with pytest.raises(DBAPIError):
            async with database.transaction() as connection:
                await connection.execute(OCCURRENCE_INSERT, {**binding, field: value})
    if database.dialect is Dialect.SQLITE:
        with pytest.raises(IntegrityError):
            async with database.transaction() as connection:
                await connection.execute(
                    text(
                        "UPDATE graph_deployment_stage "
                        "SET store_id = CAST(X'ff' AS TEXT) WHERE stage_id = :stage_id"
                    ),
                    {"stage_id": MAX_STAGE_ID},
                )
    async with database.transaction() as connection:
        await connection.execute(OCCURRENCE_INSERT, binding)
        retained = (await connection.execute(select(stage))).mappings().one()
        associated = (await connection.execute(select(occurrence))).mappings().one()

    assert dict(retained) == candidate
    assert dict(associated) == binding


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("encoding", "malformed"),
    [("UTF-16le", b"s\x00\x00\xd8"), ("UTF-16be", b"\x00s\xd8\x00")],
)
async def test_stage_native_text_domains_support_sqlite_utf16(
    tmp_path: Path, encoding: str, malformed: bytes
) -> None:
    path = tmp_path / "utf16.db"
    with closing(sqlite3.connect(path)) as seed:
        seed.execute(
            "PRAGMA encoding = 'UTF-16le'"
            if encoding == "UTF-16le"
            else "PRAGMA encoding = 'UTF-16be'"
        )
        seed.execute("CREATE TABLE encoding_anchor (id INTEGER)")
    settings = sqlite_settings(path)
    await upgrade_database(settings)
    database = Database(settings)
    try:
        async with database.transaction() as connection:
            scope = await prepare_graph(connection)
            candidate = stage_values(scope, MAX_STAGE_ID)
            binding = occurrence_values(scope, MAX_STAGE_ID, 63)

        async with database.transaction() as connection:
            await connection.execute(STAGE_INSERT, candidate)
            await connection.execute(OCCURRENCE_INSERT, binding)
        with pytest.raises(IntegrityError):
            async with database.transaction() as connection:
                await connection.execute(
                    text(
                        "UPDATE graph_deployment_stage "
                        "SET store_id = CAST(:malformed AS TEXT) "
                        "WHERE stage_id = :stage_id"
                    ),
                    {"stage_id": MAX_STAGE_ID, "malformed": malformed},
                )
        async with database.read_transaction() as connection:
            actual_encoding = await connection.scalar(text("PRAGMA encoding"))
            stored = (await connection.execute(select(stage))).mappings().one()
            associated = (await connection.execute(select(occurrence))).mappings().one()
    finally:
        await database.close()

    assert actual_encoding == encoding
    assert dict(stored) == candidate
    assert dict(associated) == binding


async def insert_fence(connection: AsyncConnection, store: str) -> int:
    incarnation: object = await connection.scalar(
        storage_address_fence.insert()
        .values(
            store_id=store, payload_digest=ADDRESS_DIGEST, last_deletion_generation=0
        )
        .returning(storage_address_fence.c.fence_incarnation)
    )
    assert type(incarnation) is int
    return incarnation


def upgrade_stage_revision(
    connection: Connection,
) -> tuple[str | None, str | None, int]:
    config = alembic_config(connection)
    previous = current_revision(connection)
    steps = pending_steps(config, previous, REVISION)
    connection.commit()
    command.upgrade(config, REVISION)
    return previous, current_revision(connection), steps


@pytest.mark.asyncio
async def test_stage_migration_preserves_preparation_graph_and_fence_allocator(
    empty_database_settings: DatabaseSettings,
) -> None:
    before = Database(empty_database_settings)
    try:
        async with before.migration_connection() as connection:
            await connection.run_sync(
                lambda sync: command.upgrade(alembic_config(sync), PREDECESSOR)
            )
        async with before.transaction() as connection:
            scope = await prepare_graph(connection)
            retained_id = await insert_fence(connection, "Store")
            removed_id = await insert_fence(connection, "removed")
            previous = (
                (await connection.execute(select(preparation))).one(),
                (await connection.execute(select(graph_version))).one(),
                (
                    await connection.execute(
                        select(storage_address_fence).where(
                            storage_address_fence.c.fence_incarnation == retained_id
                        )
                    )
                ).one(),
            )
        async with before.transaction() as connection:
            await connection.execute(
                storage_address_fence.delete().where(
                    storage_address_fence.c.fence_incarnation == removed_id
                )
            )
    finally:
        await before.close()
    expected_stage = stage_values(scope, MAX_STAGE_ID, retained_id)
    expected_binding = occurrence_values(scope, MAX_STAGE_ID, 0)

    after = Database(empty_database_settings)
    try:
        async with after.migration_connection() as connection:
            report = await connection.run_sync(upgrade_stage_revision)
        async with after.migration_connection() as connection:
            repeated = await connection.run_sync(upgrade_stage_revision)
        async with after.transaction() as connection:
            initial_stage = (await connection.execute(select(stage))).all()
            initial_binding = (await connection.execute(select(occurrence))).all()
            next_id = await insert_fence(connection, "removed")
            await connection.execute(stage.insert().values(**expected_stage))
            await connection.execute(occurrence.insert().values(**expected_binding))
        with pytest.raises(RuntimeError, match="graph deployment stage history"):
            async with after.migration_connection() as connection:
                await connection.run_sync(
                    lambda sync: command.downgrade(alembic_config(sync), PREDECESSOR)
                )
        async with after.read_transaction() as connection:
            revision = await connection.run_sync(current_revision)
            surviving = (
                (await connection.execute(select(preparation))).one(),
                (await connection.execute(select(graph_version))).one(),
                (
                    await connection.execute(
                        select(storage_address_fence).where(
                            storage_address_fence.c.fence_incarnation == retained_id
                        )
                    )
                ).one(),
            )
            saved_stage = (await connection.execute(select(stage))).mappings().one()
            saved_binding = (
                (await connection.execute(select(occurrence))).mappings().one()
            )
    finally:
        await after.close()

    assert report == (PREDECESSOR, REVISION, 1)
    assert repeated == (REVISION, REVISION, 0)
    assert revision == REVISION
    assert initial_stage == initial_binding == []
    assert surviving == previous
    assert next_id > removed_id > retained_id > 0
    assert dict(saved_stage) == expected_stage
    assert dict(saved_binding) == expected_binding
