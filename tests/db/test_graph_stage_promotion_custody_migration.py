from collections.abc import Mapping
from dataclasses import dataclass

import pytest
from alembic import command
from sqlalchemy import Connection, Executable, inspect, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.connection import Database, DatabaseSettings, Dialect
from kora.db.graph_stage_promotion_custody_v1 import (
    MAX_PROMOTION_DOCUMENT_BYTES_V1,
)
from kora.db.migrations import current_revision, upgrade_database
from kora.db.migrations.environment import alembic_config
from kora.db.tables import (
    graph_deployment_stage as stage,
)
from kora.db.tables import (
    graph_deployment_stage_occurrence as occurrence,
)
from kora.db.tables import (
    graph_deployment_stage_promotion_intent as intent,
)
from kora.db.tables import (
    graph_deployment_stage_promotion_settlement as settlement,
)
from kora.db.tables import (
    storage_address_fence,
)
from kora.protocols.identity_records import MAX_COUNT
from kora.protocols.storage_models import MAX_STORAGE_METADATA
from tests.db.closing import ConnectionLedger
from tests.db.seed import Seeder
from tests.db.test_graph_deployment_stage_migration import (
    ADDRESS_DIGEST,
    insert_fence,
    key_layout,
    occurrence_values,
    prepare_graph,
    stage_values,
)

PREDECESSOR = "0015"
REVISION = "0016"
REQUEST_DIGEST = "sha256:" + "d" * 64
BOUNDARY_BYTES = bytes(range(256)) * (MAX_PROMOTION_DOCUMENT_BYTES_V1 // 256)
INTENT_INSERT = text(
    "INSERT INTO graph_deployment_stage_promotion_intent "
    "(stage_id, store_id, payload_digest, fence_incarnation, operation_id, "
    "request_digest, content_generation, request_bytes) "
    "VALUES (:stage_id, :store_id, :payload_digest, :fence_incarnation, :operation_id, "
    ":request_digest, :content_generation, :request_bytes)"
)
SETTLEMENT_INSERT = text(
    "INSERT INTO graph_deployment_stage_promotion_settlement "
    "(stage_id, observation_bytes) VALUES (:stage_id, :observation_bytes)"
)
COLLISION = text(
    "CREATE TABLE graph_deployment_stage_promotion_settlement (marker INTEGER NOT NULL)"
)
COLLISION_INSERT = text(
    "INSERT INTO graph_deployment_stage_promotion_settlement (marker) VALUES (41)"
)
COLLISION_SELECT = text(
    "SELECT marker FROM graph_deployment_stage_promotion_settlement"
)
COLLISION_DROP = text("DROP TABLE graph_deployment_stage_promotion_settlement")


@dataclass(frozen=True)
class OriginalStage:
    stage_id: str
    store_id: str
    payload_digest: str
    fence_incarnation: int

    def request(self, operation_id: str, body: bytes) -> dict[str, object]:
        return {
            "stage_id": self.stage_id,
            "store_id": self.store_id,
            "payload_digest": self.payload_digest,
            "fence_incarnation": self.fence_incarnation,
            "operation_id": operation_id,
            "request_digest": REQUEST_DIGEST,
            "content_generation": MAX_COUNT,
            "request_bytes": body,
        }


async def seed_stage(
    connection: AsyncConnection,
    namespace: str,
    store: str = "Store",
    *,
    incarnation: int = MAX_COUNT,
) -> OriginalStage:
    scope = await prepare_graph(connection, namespace)
    stage_id = Seeder(connection).new_id()
    await connection.execute(
        stage.insert().values(
            **{**stage_values(scope, stage_id, incarnation), "store_id": store}
        )
    )
    await connection.execute(
        occurrence.insert().values(**occurrence_values(scope, stage_id, 0))
    )
    return OriginalStage(stage_id, store, ADDRESS_DIGEST, incarnation)


async def refuse(
    database: Database,
    statement: Executable,
    values: Mapping[str, object] | None = None,
) -> None:
    with pytest.raises(IntegrityError):
        async with database.transaction() as connection:
            await connection.execute(statement, values)


def native_column_order(
    connection: Connection,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    inspector = inspect(connection)
    return (
        tuple(column["name"] for column in inspector.get_columns(intent.name)),
        tuple(column["name"] for column in inspector.get_columns(settlement.name)),
    )


@pytest.mark.asyncio
async def test_promotion_custody_uses_exact_order_and_native_keys(
    database: Database,
) -> None:
    async with database.read_transaction() as connection:
        columns = await connection.run_sync(native_column_order)
        intent_keys = await connection.run_sync(key_layout, intent)
        settlement_keys = await connection.run_sync(key_layout, settlement)

    assert columns == (
        (
            "stage_id",
            "store_id",
            "payload_digest",
            "fence_incarnation",
            "operation_id",
            "request_digest",
            "content_generation",
            "request_bytes",
        ),
        ("stage_id", "observation_bytes"),
    )
    assert intent_keys == (["stage_id"], [["store_id", "operation_id"]], [])
    assert settlement_keys == (["stage_id"], [], [])
    assert MAX_PROMOTION_DOCUMENT_BYTES_V1 == MAX_STORAGE_METADATA == 16_384


@pytest.mark.asyncio
async def test_promotion_custody_keys_protect_each_original_witness(
    database: Database,
) -> None:
    async with database.transaction() as connection:
        first = await seed_stage(connection, "original")
        second = await seed_stage(connection, "case_operation")
        third = await seed_stage(connection, "case_store", "store")
        unmatched = await seed_stage(connection, "unmatched")
        nonexistent = Seeder(connection).new_id()
    operation = "A" * 256
    original_request = first.request(operation, BOUNDARY_BYTES)
    observation = {"stage_id": first.stage_id, "observation_bytes": BOUNDARY_BYTES}

    async with database.transaction() as connection:
        await connection.execute(INTENT_INSERT, original_request)
        await connection.execute(
            INTENT_INSERT, second.request(operation.lower(), b"{}")
        )
        await connection.execute(INTENT_INSERT, third.request(operation, b"{}"))
        await connection.execute(SETTLEMENT_INSERT, observation)
        await connection.execute(
            occurrence.delete().where(occurrence.c.stage_id == second.stage_id)
        )
    await refuse(database, INTENT_INSERT, first.request("different", b"{}"))
    await refuse(database, INTENT_INSERT, unmatched.request(operation, b"{}"))
    await refuse(
        database,
        INTENT_INSERT,
        {**unmatched.request("orphan", b"{}"), "stage_id": nonexistent},
    )
    await refuse(database, SETTLEMENT_INSERT, observation)
    await refuse(
        database, SETTLEMENT_INSERT, {**observation, "stage_id": unmatched.stage_id}
    )
    await refuse(database, intent.delete().where(intent.c.stage_id == first.stage_id))
    await refuse(database, stage.delete().where(stage.c.stage_id == second.stage_id))
    async with database.read_transaction() as connection:
        saved_request = (
            (
                await connection.execute(
                    select(intent).where(intent.c.stage_id == first.stage_id)
                )
            )
            .mappings()
            .one()
        )
        saved_observation = (
            (await connection.execute(select(settlement))).mappings().one()
        )
        count = (await connection.execute(select(intent.c.stage_id))).all()

    assert dict(saved_request) == original_request
    assert dict(saved_observation) == observation
    assert len(count) == 3


@pytest.mark.asyncio
async def test_promotion_custody_native_domains_reject_invalid_values(
    database: Database,
) -> None:
    async with database.transaction() as connection:
        original = await seed_stage(connection, "native_domains")
    valid_request = {**original.request("op", b"x"), "content_generation": 0}
    valid_settlement: dict[str, object] = {
        "stage_id": original.stage_id,
        "observation_bytes": b"x",
    }
    oversized = BOUNDARY_BYTES + b"x"
    invalid: list[dict[str, object]] = [
        {"stage_id": "8" + "0" * 25},
        {"store_id": "x" * 257},
        {"store_id": "with space"},
        {"operation_id": ""},
        {"operation_id": "x" * 257},
        {"operation_id": "café"},
        {"payload_digest": "sha256:" + "F" * 64},
        {"request_digest": "sha256:" + "s" * 64},
        {"request_digest": "sha256:" + "f" * 63},
        {"request_digest": "SHA256:" + "f" * 64},
        {"fence_incarnation": 0},
        {"content_generation": -1},
        {"request_bytes": b""},
        {"request_bytes": oversized},
    ]
    sqlite = database.dialect is Dialect.SQLITE
    if sqlite:
        invalid.extend(
            (
                {"stage_id": original.stage_id.encode()},
                {"store_id": "Store\x00hidden"},
                {"operation_id": "op\x00hidden"},
                {"payload_digest": ADDRESS_DIGEST + "\x00hidden"},
                {"request_digest": REQUEST_DIGEST + "\x00hidden"},
                {"fence_incarnation": 1.5},
                {"content_generation": b"0"},
                {"request_bytes": "{}"},
            )
        )

    for change in invalid:
        await refuse(database, INTENT_INSERT, {**valid_request, **change})
    async with database.transaction() as connection:
        await connection.execute(INTENT_INSERT, valid_request)
    for bad in (b"", oversized, "{}") if sqlite else (b"", oversized):
        await refuse(
            database, SETTLEMENT_INSERT, {**valid_settlement, "observation_bytes": bad}
        )
    async with database.transaction() as connection:
        await connection.execute(SETTLEMENT_INSERT, valid_settlement)
        final = (await connection.execute(select(settlement))).mappings().one()

    assert dict(final) == valid_settlement


async def stage_snapshot(connection: AsyncConnection) -> tuple[object, ...]:
    stage_rows = (await connection.execute(select(stage))).all()
    occurrence_rows = (await connection.execute(select(occurrence))).all()
    fence_rows = (await connection.execute(select(storage_address_fence))).all()
    return stage_rows, occurrence_rows, fence_rows


@pytest.mark.asyncio
async def test_promotion_custody_migration_rolls_back_collision_and_preserves_history(
    empty_database_settings: DatabaseSettings,
) -> None:
    database = Database(empty_database_settings)
    ledger = ConnectionLedger(database)
    migration_owners: list[tuple[Database, ConnectionLedger]] = []

    def open_observed(settings: DatabaseSettings) -> Database:
        owner = Database(settings)
        migration_owners.append((owner, ConnectionLedger(owner)))
        return owner

    try:
        async with database.migration_connection() as connection:
            await connection.run_sync(
                lambda sync: command.upgrade(alembic_config(sync), PREDECESSOR)
            )
        async with database.transaction() as connection:
            incarnation = await insert_fence(connection, "Store")
            original = await seed_stage(connection, "retained", incarnation=incarnation)
            before = await stage_snapshot(connection)
            await connection.execute(COLLISION)
            await connection.execute(COLLISION_INSERT)

        with pytest.raises(DBAPIError, match="already exists"):
            await upgrade_database(empty_database_settings, open_observed)
        async with database.read_transaction() as connection:
            refused_revision = await connection.run_sync(current_revision)
            refused_relations = await connection.run_sync(
                lambda sync: inspect(sync).get_table_names()
            )
            refused_history = await stage_snapshot(connection)
            collision_marker = await connection.scalar(COLLISION_SELECT)
        async with database.transaction() as connection:
            await connection.execute(COLLISION_DROP)
        report = await upgrade_database(empty_database_settings, open_observed)
        repeated = await upgrade_database(empty_database_settings, open_observed)
        async with database.transaction() as connection:
            empty = (
                (await connection.execute(select(intent))).all(),
                (await connection.execute(select(settlement))).all(),
            )
            await connection.execute(
                INTENT_INSERT, original.request("retained-op", b"{}")
            )
            await connection.execute(
                SETTLEMENT_INSERT,
                {"stage_id": original.stage_id, "observation_bytes": b"{}"},
            )
        with pytest.raises(
            RuntimeError, match="graph stage promotion custody cannot be downgraded"
        ):
            async with database.migration_connection() as connection:
                await connection.run_sync(
                    lambda sync: command.downgrade(alembic_config(sync), PREDECESSOR)
                )
        async with database.read_transaction() as connection:
            final_revision = await connection.run_sync(current_revision)
            final_history = await stage_snapshot(connection)
            final_intent = (
                await connection.execute(select(intent.c.stage_id))
            ).scalar_one()
            final_settlement = (
                await connection.execute(select(settlement.c.stage_id))
            ).scalar_one()
    finally:
        await database.close()

    assert refused_revision == PREDECESSOR
    assert intent.name not in refused_relations
    assert settlement.name in refused_relations
    assert collision_marker == 41
    assert refused_history == final_history == before
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
    assert empty == ([], [])
    assert final_revision == REVISION
    assert final_intent == final_settlement == original.stage_id
    assert ledger.outstanding == database.units_in_flight() == 0
    assert len(migration_owners) == 3
    assert all(
        observed.checkouts > 0
        and observed.opened == observed.closed
        and observed.closed_while_checked_out == 0
        and observed.outstanding == owner.units_in_flight() == 0
        for owner, observed in migration_owners
    )
