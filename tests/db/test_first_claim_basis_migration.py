from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import DBAPIError

from kora.db.claim_source_values import ClaimSourcesRejected
from kora.db.connection import Database, DatabaseSettings, Dialect
from kora.db.first_claim_basis import (
    FirstClaimBasis,
    first_claim_basis_available,
    insert_first_claim_basis,
    read_first_claim_basis,
)
from kora.db.first_claim_basis_v1 import MAX_CONFIGURED_STALL_US
from kora.db.identifiers import encode
from kora.db.migrations import current_revision, upgrade_database
from kora.db.migrations.environment import alembic_config
from kora.db.tables import attempt_dispatcher
from kora.db.tables import attempt_first_claim_basis as basis
from kora.db.worker_claim_poll_v1 import CANDIDATE_ROWS
from tests.engine.test_grant_history import legacy, seed
from tests.support.databases import encoded_sqlite_settings

DISPATCHER_ID: str = encode(1)
HISTORY_DIGEST: str = "1" * 64
RENDERER_DIGEST: bytes = b"d" * 32
SIGNED_MAX: int = (1 << 63) - 1


async def upgrade_to_predecessor(database: Database) -> None:
    async with database.migration_connection() as migrator:
        await migrator.run_sync(
            lambda sync: command.upgrade(alembic_config(sync), "0032")
        )


async def change_candidate_collation(database: Database) -> None:
    async with database.transaction() as writer:
        if database.dialect is Dialect.POSTGRESQL:
            definition = (
                "SELECT pg_get_indexdef('ix_attempt_claim_candidate'::regclass)"
            )
            original = (await writer.exec_driver_sql(definition)).scalar_one()
            await writer.exec_driver_sql("DROP INDEX ix_attempt_claim_candidate")
            await writer.exec_driver_sql(
                "ALTER TABLE attempt ALTER COLUMN runtime TYPE varchar(255) "
                'COLLATE pg_catalog."POSIX"'
            )
            await writer.exec_driver_sql(
                f"CREATE INDEX ix_attempt_claim_candidate ON attempt "
                f"(runtime, id) WHERE {CANDIDATE_ROWS}"
            )
            assert (await writer.exec_driver_sql(definition)).scalar_one() == original
        else:
            await writer.exec_driver_sql("DROP INDEX ix_attempt_claim_candidate")
            await writer.exec_driver_sql(
                f"CREATE INDEX ix_attempt_claim_candidate ON attempt "
                f"(runtime COLLATE NOCASE, id) WHERE {CANDIDATE_ROWS}"
            )


@pytest.mark.asyncio
async def test_first_claim_basis_upgrade_preserves_receipt_and_refuses_downgrade(
    empty_database_settings: DatabaseSettings,
) -> None:
    database = Database(empty_database_settings)
    try:
        await upgrade_to_predecessor(database)
        scope = await seed(database)
        await legacy(database, scope, range(1, 2))
        async with database.transaction() as writer:
            await writer.execute(
                update(attempt_dispatcher)
                .where(attempt_dispatcher.c.id == DISPATCHER_ID)
                .values(
                    claim_id="old-claim",
                    claim_digest=HISTORY_DIGEST,
                    prefix_count=1,
                    prefix_digest=HISTORY_DIGEST,
                )
            )
        async with database.read_snapshot() as reader:
            predecessor = await first_claim_basis_available(reader)
            original = (await reader.execute(select(attempt_dispatcher))).one()

        report = await upgrade_database(empty_database_settings)
        async with database.read_snapshot() as reader:
            migrated = await first_claim_basis_available(reader)
            retained = (await reader.execute(select(attempt_dispatcher))).one()
            absent = await read_first_claim_basis(reader, DISPATCHER_ID)
        with pytest.raises(RuntimeError, match="restore a compatible backup"):
            async with database.migration_connection() as migrator:
                await migrator.run_sync(
                    lambda sync: command.downgrade(alembic_config(sync), "0032")
                )
        async with database.read_snapshot() as reader:
            revision = await reader.run_sync(current_revision)
            still_available = await first_claim_basis_available(reader)

        assert predecessor is False
        assert migrated is still_available is True
        assert (report.from_revision, report.to_revision, report.steps) == (
            "0032",
            "0033",
            1,
        )
        assert revision == "0033"
        assert retained == original
        assert absent is None
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "damage", ["cursor", "missing-index", "wrong-index", "wrong-collation"]
)
async def test_first_claim_basis_requires_verified_predecessor_schema(
    empty_database_settings: DatabaseSettings,
    damage: str,
) -> None:
    database = Database(empty_database_settings)
    try:
        await upgrade_to_predecessor(database)
        if damage == "wrong-collation":
            await change_candidate_collation(database)
        else:
            async with database.transaction() as writer:
                if damage == "cursor":
                    await writer.exec_driver_sql(
                        "ALTER TABLE worker DROP COLUMN claim_cursor"
                    )
                else:
                    await writer.exec_driver_sql(
                        "DROP INDEX ix_attempt_claim_candidate"
                    )
                    if damage == "wrong-index":
                        await writer.exec_driver_sql(
                            "CREATE INDEX ix_attempt_claim_candidate "
                            "ON attempt (id, runtime) WHERE status = 'created'"
                        )

        with pytest.raises(ClaimSourcesRejected, match="claim sources are unavailable"):
            async with database.read_snapshot() as reader:
                await first_claim_basis_available(reader)
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("replace_relation", [False, True])
async def test_first_claim_basis_refuses_damage_on_current_revision(
    database: Database, replace_relation: bool
) -> None:
    async with database.transaction() as writer:
        if database.dialect is Dialect.POSTGRESQL and replace_relation:
            await writer.exec_driver_sql(
                "ALTER TABLE attempt_first_claim_basis "
                "DROP CONSTRAINT ck_first_claim_basis_domain, "
                "ADD CONSTRAINT ck_first_claim_basis_domain CHECK (true)"
            )
        else:
            await writer.exec_driver_sql("DROP TABLE attempt_first_claim_basis")
            if replace_relation:
                await writer.exec_driver_sql(
                    "CREATE TABLE attempt_first_claim_basis (dispatcher_id TEXT)"
                )

    with pytest.raises(ClaimSourcesRejected, match="claim sources are unavailable"):
        async with database.read_snapshot() as reader:
            await first_claim_basis_available(reader)


@pytest.mark.asyncio
async def test_first_claim_basis_insert_is_owned_by_one_dispatcher(
    database: Database,
) -> None:
    scope = await seed(database)
    await legacy(database, scope, range(1, 3))

    async with database.transaction() as writer:
        assert await first_claim_basis_available(writer)
        await insert_first_claim_basis(
            writer, DISPATCHER_ID, 2, MAX_CONFIGURED_STALL_US
        )
        await insert_first_claim_basis(writer, encode(2), SIGNED_MAX, None)
    with pytest.raises(DBAPIError):
        async with database.transaction() as writer:
            await insert_first_claim_basis(writer, DISPATCHER_ID, 3, 1)
    with pytest.raises(DBAPIError):
        async with database.transaction() as writer:
            await insert_first_claim_basis(writer, encode(3), 2, 1)
    with pytest.raises(DBAPIError):
        async with database.transaction() as writer:
            await writer.execute(
                attempt_dispatcher.delete().where(
                    attempt_dispatcher.c.id == DISPATCHER_ID
                )
            )
    async with database.read_snapshot() as reader:
        wide = await read_first_claim_basis(reader, DISPATCHER_ID)
        authored = await read_first_claim_basis(reader, encode(2))
        missing = await read_first_claim_basis(reader, encode(3))

    assert wide == FirstClaimBasis(2, MAX_CONFIGURED_STALL_US, *([None] * 7))
    assert authored == FirstClaimBasis(SIGNED_MAX, None, *([None] * 7))
    assert missing is None


def selected_basis() -> dict[str, object]:
    return {
        "dispatcher_id": DISPATCHER_ID,
        "format_version": 1,
        "source_revision": SIGNED_MAX,
        "configured_stall_us": str(MAX_CONFIGURED_STALL_US),
        "renderer_behavior_sha256": RENDERER_DIGEST,
        "runtime_selection_id": encode(10),
        "runtime_selection_revision": SIGNED_MAX,
        "vcs_selection_id": encode(11),
        "vcs_selection_revision": SIGNED_MAX,
        "harness_selection_id": encode(12),
        "harness_selection_revision": SIGNED_MAX,
    }


@pytest.mark.asyncio
async def test_first_claim_basis_rejects_native_domain_and_partial_selection(
    database: Database,
) -> None:
    scope = await seed(database)
    await legacy(database, scope, range(1, 2))
    valid = selected_basis()
    rejected = (
        {"dispatcher_id": None},
        {"dispatcher_id": "8" + DISPATCHER_ID[1:]},
        {"format_version": 2},
        {"source_revision": 1},
        {"configured_stall_us": "0"},
        {"configured_stall_us": "01"},
        {"configured_stall_us": "1\x002"},
        {"configured_stall_us": "١"},
        {"configured_stall_us": str(MAX_CONFIGURED_STALL_US + 1)},
        {"renderer_behavior_sha256": b"d" * 31},
        {"renderer_behavior_sha256": None},
        {"runtime_selection_id": None},
        {"runtime_selection_id": "8" + DISPATCHER_ID[1:]},
        {"runtime_selection_id": "a" * 26},
        {"runtime_selection_revision": None},
        {"vcs_selection_revision": 0},
        {"vcs_selection_id": None, "vcs_selection_revision": None},
        {"harness_selection_id": None},
        {"harness_selection_revision": None},
    )

    for override in rejected:
        with pytest.raises(DBAPIError):
            async with database.transaction() as writer:
                await writer.execute(insert(basis).values(valid | override))
    async with database.transaction() as writer:
        await writer.execute(insert(basis).values(valid))
    async with database.read_snapshot() as reader:
        row = await read_first_claim_basis(reader, DISPATCHER_ID)

    assert row == FirstClaimBasis(
        SIGNED_MAX,
        MAX_CONFIGURED_STALL_US,
        RENDERER_DIGEST,
        encode(10),
        SIGNED_MAX,
        encode(11),
        SIGNED_MAX,
        encode(12),
        SIGNED_MAX,
    )


@pytest.mark.asyncio
async def test_first_claim_basis_refuses_malformed_indexed_row(
    database: Database,
) -> None:
    scope = await seed(database)
    await legacy(database, scope, range(1, 2))
    invalid = selected_basis() | {"harness_selection_revision": None}
    async with database.transaction() as writer:
        if database.dialect is Dialect.SQLITE:
            await writer.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        else:
            await writer.exec_driver_sql(
                "ALTER TABLE attempt_first_claim_basis "
                "DROP CONSTRAINT ck_first_claim_basis_domain"
            )
        await writer.execute(insert(basis).values(invalid))

    async with database.read_snapshot() as reader:
        unrelated = await read_first_claim_basis(reader, encode(2))
        with pytest.raises(ClaimSourcesRejected, match="claim sources are unavailable"):
            await read_first_claim_basis(reader, DISPATCHER_ID)

    assert unrelated is None


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["UTF-16le", "UTF-16be"])
async def test_sqlite_basis_admits_exact_utf16_row_and_refuses_native_overflow(
    tmp_path: Path, encoding: str
) -> None:
    settings = encoded_sqlite_settings(tmp_path / "basis.db", encoding)
    await upgrade_database(settings)
    database = Database(settings)
    try:
        scope = await seed(database)
        await legacy(database, scope, range(1, 3))
        async with database.transaction() as writer:
            await writer.execute(insert(basis).values(selected_basis()))
            await writer.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
            await writer.execute(
                insert(basis).values(
                    dispatcher_id=encode(2),
                    format_version=1,
                    source_revision=2,
                    configured_stall_us=func.zeroblob(1_000_000),
                )
            )

        async with database.read_snapshot() as reader:
            assert await first_claim_basis_available(reader)
            valid = await read_first_claim_basis(reader, DISPATCHER_ID)
            with pytest.raises(
                ClaimSourcesRejected, match="claim sources are unavailable"
            ):
                await read_first_claim_basis(reader, encode(2))

        assert valid is not None
        assert valid.configured_stall_us == MAX_CONFIGURED_STALL_US
        assert valid.harness_selection_revision == SIGNED_MAX
    finally:
        await database.close()
