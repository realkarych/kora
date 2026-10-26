from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError, StatementError
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.connection import Database
from kora.db.identifiers import (
    IdentifierError,
    IdentifierGenerator,
    decode,
    encode,
    is_identifier,
    timestamp_of,
)
from kora.db.tables import (
    api_token,
    artifact_type,
    attempt,
    namespace,
    node_instance,
    run,
    usage_record,
)
from tests.db.seed import START, FakeClock, Seeder, fixed_randomness

NESTED_POLICY: dict[str, object] = {
    "z": [1, 2.5, "три", {"k": None, "flag": True}],
    "a": {"nested": {"deep": "ключ"}},
    "unicode": "emoji \U0001f600",
}
PATHS: list[str] = ["a[1]", "a.b", "a@2", "a_b", "aB", "ab", "a", "Z", "[", "]"]


@pytest.mark.asyncio
async def test_column_types_round_trip_identically(database: Database) -> None:
    whole_second = datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)
    fractional = datetime(2026, 10, 20, 12, 0, 0, 123456, tzinfo=UTC)
    shifted = datetime(2026, 10, 20, 14, 0, 0, 1, tzinfo=timezone(timedelta(hours=2)))
    async with database.transaction() as connection:
        seeder = Seeder(connection)
        namespace_id = await seeder.namespace()
        await connection.execute(
            update(namespace)
            .where(namespace.c.id == namespace_id)
            .values(policy=NESTED_POLICY)
        )
        whole_token = await seeder.token("1" * 64, expires_at=whole_second)
        fractional_token = await seeder.token("2" * 64, expires_at=fractional)
        shifted_token = await seeder.token("3" * 64, expires_at=shifted)
        open_token = await seeder.token("4" * 64, expires_at=None)
        graph_id = await seeder.graph(namespace_id)
        version_id = await seeder.graph_version(graph_id)
        run_id = await seeder.run(namespace_id, graph_id, version_id)
        node_id = await seeder.node(run_id, "step")
        attempt_id = await seeder.attempt(node_id)
        await seeder.usage(attempt_id, "model-a", Decimal("12.3456789012"))

    async with database.read_transaction() as connection:
        policy = (
            await connection.execute(
                select(namespace.c.policy).where(namespace.c.id == namespace_id)
            )
        ).scalar_one()
        expiries = {
            row.id: row.expires_at
            for row in (
                await connection.execute(select(api_token.c.id, api_token.c.expires_at))
            ).all()
        }
        ordered = [
            row[0]
            for row in (
                await connection.execute(
                    select(api_token.c.id)
                    .where(api_token.c.expires_at.is_not(None))
                    .order_by(api_token.c.expires_at, api_token.c.id)
                )
            ).all()
        ]
        due_by_fractional = [
            row[0]
            for row in (
                await connection.execute(
                    select(api_token.c.id)
                    .where(api_token.c.expires_at <= fractional)
                    .order_by(api_token.c.id)
                )
            ).all()
        ]
        amount = (
            await connection.execute(
                select(usage_record.c.usd_estimate).where(
                    usage_record.c.attempt_id == attempt_id
                )
            )
        ).scalar_one()
        recovery = (
            await connection.execute(
                select(attempt.c.recovery, attempt.c.handle).where(
                    attempt.c.id == attempt_id
                )
            )
        ).one()

    assert policy == NESTED_POLICY
    assert expiries[whole_token] == whole_second
    assert expiries[fractional_token] == fractional
    assert expiries[shifted_token] == shifted.astimezone(UTC)
    assert expiries[shifted_token].tzinfo is not None
    assert expiries[open_token] is None
    assert ordered == sorted(
        [whole_token, fractional_token, shifted_token],
        key=lambda token: (expiries[token], token),
    )
    assert due_by_fractional == sorted([whole_token, fractional_token, shifted_token])
    assert amount == Decimal("12.3456789012")
    assert tuple(recovery) == (None, None)


@pytest.mark.asyncio
async def test_null_characters_and_naive_timestamps_are_rejected(
    database: Database,
) -> None:
    async with database.transaction() as connection:
        seeder = Seeder(connection)
        namespace_id = await seeder.namespace()

    for rejected_policy in ({"key\u0000": 1}, {"text": "a\u0000b"}, ["a\u0000"]):
        with pytest.raises(StatementError, match="null character"):
            async with database.transaction() as connection:
                await connection.execute(
                    update(namespace)
                    .where(namespace.c.id == namespace_id)
                    .values(policy=rejected_policy)
                )
    with pytest.raises(StatementError, match="null character"):
        async with database.transaction() as connection:
            await connection.execute(
                update(namespace)
                .where(namespace.c.id == namespace_id)
                .values(name="a\u0000b")
            )
    with pytest.raises(StatementError, match="timezone-aware"):
        async with database.transaction() as connection:
            await Seeder(connection).token("5" * 64, expires_at=datetime(2026, 1, 1))

    async with database.read_transaction() as connection:
        row = (
            await connection.execute(
                select(namespace.c.name, namespace.c.policy).where(
                    namespace.c.id == namespace_id
                )
            )
        ).one()
    assert tuple(row) == ("default", {"max_pending_runs": 1000})


@pytest.mark.asyncio
async def test_text_keys_order_by_bytes_on_both_databases(database: Database) -> None:
    async with database.transaction() as connection:
        seeder = Seeder(connection)
        namespace_id = await seeder.namespace()
        graph_id = await seeder.graph(namespace_id)
        version_id = await seeder.graph_version(graph_id)
        run_id = await seeder.run(namespace_id, graph_id, version_id)
        await seeder.nodes(run_id, PATHS, "pending", True)

    async with database.read_transaction() as connection:
        ordered = [
            row[0]
            for row in (
                await connection.execute(
                    select(node_instance.c.node_path).order_by(
                        node_instance.c.node_path
                    )
                )
            ).all()
        ]

    assert ordered == sorted(PATHS, key=lambda path: path.encode("utf-8"))


def test_identifiers_are_sortable_monotonic_and_injected() -> None:
    clock = FakeClock()
    generator = IdentifierGenerator(clock.now, fixed_randomness(7))

    first = generator.new()
    second = generator.new()
    clock.advance(0.001)
    third = generator.new()
    clock.advance(-0.5)
    fourth = generator.new()

    assert [len(value) for value in (first, second, third, fourth)] == [26] * 4
    assert first < second < third < fourth
    assert decode(second) - decode(first) == 1
    assert timestamp_of(first) == START
    assert timestamp_of(third) == START + timedelta(milliseconds=1)
    assert timestamp_of(fourth) == START + timedelta(milliseconds=1)
    assert encode(decode(third)) == third
    assert is_identifier(first)
    assert not is_identifier(first.lower())
    assert not is_identifier("8" * 26)
    with pytest.raises(IdentifierError):
        IdentifierGenerator(lambda: datetime(2026, 1, 1)).new()


@pytest.mark.asyncio
async def test_inserted_rows_use_the_injected_clock(database: Database) -> None:
    moment = datetime(2030, 1, 2, 3, 4, 5, 678000, tzinfo=UTC)
    async with database.transaction() as connection:
        seeder = Seeder.at(connection, moment)
        namespace_id = await seeder.namespace()

    async with database.read_transaction() as connection:
        row = (
            await connection.execute(
                select(namespace.c.created_at, namespace.c.updated_at).where(
                    namespace.c.id == namespace_id
                )
            )
        ).one()

    assert tuple(row) == (moment, moment)
    assert timestamp_of(namespace_id) == moment


@pytest.mark.asyncio
async def test_boolean_columns_refuse_other_values(database: Database) -> None:
    async with database.transaction() as connection:
        seeder = Seeder(connection)
        namespace_id = await seeder.namespace()
        graph_id = await seeder.graph(namespace_id)
        version_id = await seeder.graph_version(graph_id)
        run_id = await seeder.run(namespace_id, graph_id, version_id)

    with pytest.raises(DBAPIError):
        async with database.transaction() as connection:
            await connection.execute(
                text("UPDATE run SET needs_scheduling = 2 WHERE id = :run_id"),
                {"run_id": run_id},
            )

    async with database.read_transaction() as connection:
        marked = (
            await connection.execute(
                select(run.c.needs_scheduling).where(run.c.id == run_id)
            )
        ).scalar_one()
    assert marked is True


@pytest.mark.asyncio
async def test_built_in_artifact_type_names_are_unique(database: Database) -> None:
    async with database.transaction() as connection:
        seeder = Seeder(connection)
        namespace_id = await seeder.namespace()
        await insert_artifact_type(connection, seeder, None, "VcsRef")
        await insert_artifact_type(connection, seeder, namespace_id, "VcsRef")

    with pytest.raises(IntegrityError):
        async with database.transaction() as connection:
            await insert_artifact_type(connection, Seeder(connection), None, "VcsRef")
    with pytest.raises(IntegrityError):
        async with database.transaction() as connection:
            await insert_artifact_type(
                connection, Seeder(connection), namespace_id, "VcsRef"
            )

    async with database.read_transaction() as connection:
        count = (
            await connection.execute(select(func.count()).select_from(artifact_type))
        ).scalar_one()
    assert count == 2


async def insert_artifact_type(
    connection: AsyncConnection, seeder: Seeder, namespace_id: str | None, name: str
) -> None:
    await connection.execute(
        insert(artifact_type).values(
            id=seeder.new_id(),
            namespace_id=namespace_id,
            name=name,
            schema={"type": "object"},
            parent=None,
            behavior={},
            **seeder.stamps(),
        )
    )


@pytest.mark.asyncio
async def test_price_snapshot_decimal_round_trip(database: Database) -> None:
    from decimal import Context, localcontext

    async with database.transaction() as connection:
        seeder = Seeder(connection)
        ns = await seeder.namespace()
        graph_id = await seeder.graph(ns)
        version_id = await seeder.graph_version(graph_id)
        run_id = await seeder.run(ns, graph_id, version_id)
        node_id = await seeder.node(run_id, "money")
        attempt_id = await seeder.attempt(node_id)
    exact = Decimal("99999999999999.9999999999")

    with localcontext(Context(prec=3)):
        async with database.transaction() as connection:
            usage_id = await Seeder(connection).usage(attempt_id, "exact", exact)
        async with database.read_transaction() as connection:
            stored = (
                await connection.execute(
                    select(usage_record.c.usd_estimate).where(
                        usage_record.c.id == usage_id
                    )
                )
            ).scalar_one()

    assert stored == exact
    for invalid in (
        Decimal("0.00000000001"),
        Decimal("-1"),
        Decimal("100000000000000"),
        Decimal("NaN"),
        0.1,
        "1.00",
    ):
        with pytest.raises(StatementError):
            async with database.transaction() as connection:
                await connection.execute(
                    update(usage_record)
                    .where(usage_record.c.id == usage_id)
                    .values(usd_estimate=invalid)
                )
