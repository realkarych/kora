from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import event, func, insert, literal, select, update
from sqlalchemy.exc import DBAPIError

from kora.db.connection import Database, DatabaseSettings, Dialect
from kora.db.cost_reads import (
    CostReadRefused,
    HistoricalState,
    byte_size,
    payload_projection,
    read_approval_cost,
    read_attempt_page,
    read_attempt_price,
    read_latest_price,
    read_price,
    read_usage_page,
    read_usage_source,
)
from kora.db.tables import approval_request, attempt, price_table, usage_record
from kora.ir.cost import PriceRevision, RunCost
from tests.db.predecessor import upgraded_predecessor
from tests.db.seed import START, Seeder


@pytest.mark.asyncio
async def test_price_table_namespace_override_is_explicit(database: Database) -> None:
    async with database.transaction() as connection:
        seeder = Seeder(connection)
        namespace_id = await seeder.namespace()
        for key, scope, prices in (
            (
                "default",
                None,
                {
                    "model": {
                        "input": "1",
                        "cached_input": None,
                        "cache_creation_5m": None,
                        "cache_creation_1h": None,
                        "output": None,
                        "reasoning": None,
                    }
                },
            ),
            ("empty", namespace_id, {}),
        ):
            await connection.execute(
                insert(price_table).values(
                    id=key,
                    namespace_id=scope,
                    model=None,
                    version=1,
                    normalization_version=1,
                    effective_from=START,
                    prices=prices,
                    **seeder.stamps(),
                )
            )

    async with database.read_transaction() as connection:
        selected = await read_latest_price(connection, namespace_id, START)
        default = await read_latest_price(connection, None, START)
        missing = await read_latest_price(connection, "other", START)
        future = await read_latest_price(
            connection, namespace_id, START - timedelta(seconds=1)
        )

    assert selected is not None
    assert dict(selected.prices) == {}
    assert default is not None
    assert set(default.prices) == {"model"}
    assert missing is future is None


@pytest.mark.asyncio
async def test_nullable_usage_usd_is_not_zero(database: Database) -> None:
    async with database.transaction() as connection:
        seeder = Seeder(connection)
        ns = await seeder.namespace()
        graph_id = await seeder.graph(ns)
        version_id = await seeder.graph_version(graph_id)
        run_id = await seeder.run(ns, graph_id, version_id)
        node_id = await seeder.node(run_id, "usage")
        att = await seeder.attempt(node_id)
        first = await seeder.usage(att, "unknown", Decimal("0"))
        second = await seeder.usage(att, "zero", Decimal("0"))
        await connection.execute(
            update(usage_record)
            .where(usage_record.c.id == first)
            .values(usd_estimate=None)
        )
        await connection.execute(
            update(usage_record)
            .where(usage_record.c.id == second)
            .values(
                normalization_version=1,
                known_token_fields=["input_tokens", "output_tokens"],
                observation_id="observed",
                observation_sequence=0,
                observed_at=START,
            )
        )

    async with database.read_transaction() as connection:
        records = await read_usage_page(connection, ns, run_id)
        attempts = await read_attempt_page(connection, ns, run_id)
        direct = tuple(
            (
                await connection.execute(
                    select(usage_record.c.usd_estimate).order_by(usage_record.c.id)
                )
            ).scalars()
        )
        cross_scope = await read_usage_page(connection, "another", run_id)
        after = await read_usage_page(connection, ns, run_id, after_id=first, limit=1)

    assert direct == (None, Decimal("0"))
    assert [(row.state, row.usd_estimate) for row in records] == [
        (HistoricalState.PREDECESSOR, None),
        (HistoricalState.VERSION_1, Decimal("0")),
    ]
    assert records[0].fields["known_token_fields"] is None
    assert records[1].fields["known_token_fields"] == ["input_tokens", "output_tokens"]
    assert "source" not in records[0].fields
    assert attempts[0].coverage is None
    assert cross_scope == ()
    assert [row.id for row in after] == [second]


@pytest.mark.asyncio
async def test_price_snapshot_survives_request_retention(database: Database) -> None:
    async with database.transaction() as connection:
        seeder = Seeder(connection)
        ns = await seeder.namespace()
        graph_id = await seeder.graph(ns)
        version_id = await seeder.graph_version(graph_id)
        run_id = await seeder.run(ns, graph_id, version_id)
        node_id = await seeder.node(run_id, "retained")
        att = await seeder.attempt(node_id)
        await connection.execute(
            insert(price_table).values(
                id="saved",
                namespace_id=None,
                model=None,
                version=1,
                normalization_version=1,
                effective_from=START,
                prices={},
                **seeder.stamps(),
            )
        )
        await connection.execute(
            update(attempt)
            .where(attempt.c.id == att)
            .values(price_table_id="saved", execution_request={"retained": "request"})
        )
    async with database.read_transaction() as connection:
        before = await read_attempt_price(connection, ns, att)

    async with database.transaction() as connection:
        await connection.execute(
            update(attempt).where(attempt.c.id == att).values(execution_request=None)
        )
    async with database.read_transaction() as connection:
        after = await read_attempt_price(connection, ns, att)
        refused = await read_attempt_price(connection, "other", att)

    assert after == before
    assert after.state is HistoricalState.VERSION_1
    assert after.revision is not None
    assert after.revision.id == "saved"
    assert refused.state is HistoricalState.ABSENT


@pytest.mark.asyncio
async def test_cost_reads_preserve_predecessor_distinctions(
    empty_database_settings: DatabaseSettings,
) -> None:
    database = await upgraded_predecessor(empty_database_settings)
    try:
        async with database.read_transaction() as connection:
            saved = await read_attempt_price(connection, "old-namespace", "old-attempt")
            unmatched = await read_attempt_price(
                connection, "old-namespace", "old-terminal"
            )
            absent = await read_attempt_price(
                connection, "old-namespace", "old-unpriced"
            )
            approval = await read_approval_cost(
                connection, "old-namespace", "old-approval"
            )
            usage = await read_usage_page(connection, "old-namespace", "old-run")

        assert saved.state is HistoricalState.PREDECESSOR
        assert saved.revision is None
        assert saved.predecessor is not None
        assert saved.predecessor["prices"] == {"legacy": "0.10000000000"}
        assert unmatched.state is HistoricalState.UNMATCHED
        assert unmatched.reference == "missing-price"
        assert absent.state is HistoricalState.ABSENT
        assert approval.state is HistoricalState.PREDECESSOR
        assert approval.predecessor == {"version": 1, "arbitrary": "historical"}
        legacy = next(row for row in usage if row.id == "old-out-of-domain")
        expected = (
            Decimal("0.1234567890123456789012345")
            if database.dialect is Dialect.SQLITE
            else Decimal("-1.2345678901")
        )
        assert legacy.usd_estimate == expected
        assert legacy.fields["reasoning_tokens"] == 7
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "size", [65537, 450000], ids=["canonical-limit", "transport-limit"]
)
async def test_cost_reads_bound_stored_payloads(
    empty_database_settings: DatabaseSettings, size: int
) -> None:
    database = await upgraded_predecessor(empty_database_settings)
    try:
        marker = "PRIVATE-COST-MARKER"
        large = {"marker": marker + "x" * size}
        huge_approval = {"marker": marker + "x" * (size * 20)}
        async with database.transaction() as connection:
            await connection.execute(
                update(price_table)
                .where(price_table.c.id == "old-price")
                .values(prices=large)
            )
            await connection.execute(
                update(usage_record)
                .where(usage_record.c.id == "old-zero")
                .values(source=large)
            )
            await connection.execute(
                update(approval_request)
                .where(approval_request.c.id == "old-approval")
                .values(run_cost=huge_approval)
            )
            await connection.execute(
                insert(price_table).values(
                    id="malformed-modern",
                    namespace_id=None,
                    model=None,
                    version=1,
                    normalization_version=1,
                    effective_from=START,
                    prices=large,
                    created_at=START,
                    updated_at=START,
                )
            )

        async with database.read_transaction() as connection:
            for reader in (
                read_price(connection, None, "old-price"),
                read_latest_price(connection, None, START),
                read_usage_source(connection, "old-namespace", "old-zero"),
                read_approval_cost(connection, "old-namespace", "old-approval"),
            ):
                with pytest.raises(CostReadRefused) as refused:
                    await reader
                assert marker not in str(refused.value)
            rows = await read_usage_page(connection, "old-namespace", "old-run")
            projection = (
                await connection.execute(
                    select(
                        *payload_projection(connection, price_table.c.prices, 65536)
                    ).where(price_table.c.id == "old-price")
                )
            ).one()
            stored_size = (
                await connection.execute(
                    select(byte_size(connection, price_table.c.prices)).where(
                        price_table.c.id == "old-price"
                    )
                )
            ).scalar_one()

        assert len(rows) == 2
        assert all("source" not in row.fields for row in rows)
        assert stored_size > size
        if size == 450000:
            assert projection[0] is None
        else:
            assert isinstance(projection[0], str)
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_latest_price_uses_scope_index_with_generic_plan(
    database: Database,
) -> None:
    async with database.transaction() as connection:
        seeder = Seeder(connection)
        ns = await seeder.namespace()
        await connection.execute(
            insert(price_table),
            [
                dict(
                    id=f"price-{number}",
                    namespace_id=ns,
                    model=None,
                    version=1,
                    normalization_version=1,
                    effective_from=START + timedelta(seconds=number),
                    prices={},
                    **seeder.stamps(),
                )
                for number in range(20)
            ],
        )
    statements: list[tuple[str, tuple[object, ...]]] = []

    def capture(
        connection: object,
        cursor: object,
        statement: str,
        parameters: tuple[object, ...],
        *arguments: object,
    ) -> None:
        if "FROM price_table" in statement:
            statements.append((statement, parameters))

    async with database.read_transaction() as connection:
        event.listen(connection.sync_connection, "before_cursor_execute", capture)
        selected = await read_latest_price(connection, ns, START)
        event.remove(connection.sync_connection, "before_cursor_execute", capture)
        statement, parameters = statements[0]
        if database.dialect is Dialect.POSTGRESQL:
            await connection.exec_driver_sql(
                "SET LOCAL plan_cache_mode = force_generic_plan"
            )
            await connection.exec_driver_sql("SET LOCAL enable_seqscan = off")
            await connection.exec_driver_sql(f"PREPARE kora_price_probe AS {statement}")
            arguments = ", ".join(
                str(
                    literal(value).compile(
                        dialect=connection.dialect,
                        compile_kwargs={"literal_binds": True},
                    )
                )
                for value in parameters
            )
            plan = list(
                (
                    await connection.exec_driver_sql(
                        f"EXPLAIN EXECUTE kora_price_probe({arguments})"
                    )
                ).scalars()
            )
            await connection.exec_driver_sql("DEALLOCATE kora_price_probe")
        else:
            plan = [
                str(row)
                for row in (
                    await connection.exec_driver_sql(
                        f"EXPLAIN QUERY PLAN {statement}", parameters
                    )
                )
            ]

    assert selected is not None
    assert selected.id == "price-0"
    assert "ix_price_table_namespace_effective" in "\n".join(plan)


@pytest.mark.asyncio
async def test_cost_reads_bound_predecessor_scalar_bytes(
    empty_database_settings: DatabaseSettings,
) -> None:
    database = await upgraded_predecessor(empty_database_settings)
    try:
        oversized_id = "x" * 10000
        if database.dialect is Dialect.POSTGRESQL:
            with pytest.raises(DBAPIError):
                async with database.transaction() as connection:
                    await connection.execute(
                        update(usage_record)
                        .where(usage_record.c.id == "old-zero")
                        .values(id=oversized_id)
                    )
            return
        async with database.transaction() as connection:
            await connection.execute(
                update(usage_record)
                .where(usage_record.c.id == "old-zero")
                .values(id=oversized_id)
            )

        with pytest.raises(CostReadRefused):
            async with database.read_transaction() as connection:
                await read_usage_page(connection, "old-namespace", "old-run")

        async with database.read_transaction() as connection:
            retained = (
                await connection.execute(
                    select(func.length(usage_record.c.id)).where(
                        usage_record.c.id == oversized_id
                    )
                )
            ).scalar_one()
        assert retained == 10000
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_price_and_approval_snapshots_round_trip_and_refuse_malformed_versions(
    empty_database_settings: DatabaseSettings,
) -> None:
    database = await upgraded_predecessor(empty_database_settings)
    try:
        snapshot = RunCost(
            version=1,
            as_of=START,
            observed_at=None,
            completeness="complete",
            usd_estimate="0",
            known_usd_estimate="0",
            pricing_status="estimated",
            missing_report_attempts=0,
            attribution_gap_attempts=0,
            observation_gap_attempts=0,
            models=(),
        )
        revision = PriceRevision.model_validate(
            dict(
                id="exact-prices",
                namespace_id=None,
                effective_from=START,
                version=1,
                normalization_version=1,
                prices={
                    "model": dict(
                        input="99999999999999.9999999999",
                        cached_input="0.0000000001",
                        cache_creation_5m=None,
                        cache_creation_1h=None,
                        output="0",
                        reasoning="1.2345678901",
                    )
                },
            )
        )
        async with database.transaction() as connection:
            await connection.execute(
                update(approval_request)
                .where(approval_request.c.id == "old-approval")
                .values(run_cost_version=1, run_cost=snapshot.model_dump(mode="json"))
            )
            await connection.execute(
                insert(price_table).values(
                    id=revision.id,
                    namespace_id=None,
                    effective_from=START,
                    model=None,
                    version=1,
                    normalization_version=1,
                    prices=revision.model_dump(mode="json")["prices"],
                    created_at=START,
                    updated_at=START,
                )
            )

        async with database.read_transaction() as connection:
            saved_price = await read_price(connection, None, revision.id)
            saved_snapshot = await read_approval_cost(
                connection, "old-namespace", "old-approval"
            )
            denied = await read_approval_cost(
                connection, "other-namespace", "old-approval"
            )
        assert saved_price.revision == revision
        assert saved_snapshot.snapshot == snapshot
        assert denied.state is HistoricalState.ABSENT
        async with database.transaction() as connection:
            await connection.execute(
                update(approval_request)
                .where(approval_request.c.id == "old-approval")
                .values(
                    run_cost={
                        "version": 1,
                        "private-field-marker": "private-value-marker",
                    }
                )
            )
            await connection.execute(
                update(price_table)
                .where(price_table.c.id == revision.id)
                .values(prices={"private-model-marker": {"input": "1.00"}})
            )
        async with database.read_transaction() as connection:
            for reader in (
                read_price(connection, None, revision.id),
                read_approval_cost(connection, "old-namespace", "old-approval"),
            ):
                with pytest.raises(CostReadRefused) as refused:
                    await reader
                assert "private" not in str(refused.value)
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_modern_source_is_validated_without_reclassifying_it(
    empty_database_settings: DatabaseSettings,
) -> None:
    database = await upgraded_predecessor(empty_database_settings)
    try:
        async with database.transaction() as connection:
            await connection.execute(
                update(usage_record)
                .where(usage_record.c.id == "old-zero")
                .values(
                    normalization_version=1,
                    known_token_fields=[],
                    observation_id="observed",
                    observation_sequence=0,
                    observed_at=START,
                    source=["private-source-marker"],
                )
            )

        with pytest.raises(CostReadRefused) as refused:
            async with database.read_transaction() as connection:
                await read_usage_source(connection, "old-namespace", "old-zero")

        assert "private-source-marker" not in str(refused.value)
    finally:
        await database.close()
