from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import Table, update

from kora.conformance.capture import captured
from kora.db.admission_snapshot_reads import (
    AdmissionCandidateBinding,
    read_admission_candidate,
    read_admission_price_id,
)
from kora.db.claim_snapshot_reads import PRICE_NATIVE_BYTES, acquire_claim_price
from kora.db.claim_source_values import ClaimSourcesRejected
from kora.db.connection import Database, Dialect
from kora.db.run_input_reads import RunInputBinding
from kora.db.tables import node_instance, price_table, run
from kora.db.types import PATH_LENGTH
from kora.engine.price_initialization import PriceConfiguration
from kora.observability import LoggingSettings
from tests.db.seed import START, FakeClock, Seeder
from tests.db.test_expiry_query_work import query_plan
from tests.db.test_graph_read_admission import executed_queries
from tests.db.test_run_input_parent import open_sqlite
from tests.engine.test_admission_initialization import initializer, rates

ROW_BYTES = 32_768


async def candidate(database: Database) -> AdmissionCandidateBinding:
    async with database.transaction() as writer:
        seed = Seeder(writer)
        namespace_id = await seed.namespace()
        graph_id = await seed.graph(namespace_id)
        version_id = await seed.graph_version(graph_id)
        run_id = await seed.run(namespace_id, graph_id, version_id)
        node_id = await seed.node(run_id, "agent")
    return AdmissionCandidateBinding(
        RunInputBinding(namespace_id, graph_id, version_id, run_id),
        node_id,
        "agent",
        "pending",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "ready", "waiting"])
async def test_admission_candidate_reads_exact_current_owner(
    database: Database, status: str, tmp_path: Path
) -> None:
    expected = await candidate(database)
    async with database.transaction() as writer:
        await writer.execute(
            update(node_instance)
            .where(node_instance.c.id == expected.node_id)
            .values(status=status, kind="agent")
        )

    async with database.read_transaction() as reader:
        with executed_queries(reader) as queries:
            acquired = await read_admission_candidate(
                reader, expected.node_id, node_row=ROW_BYTES, run_row=ROW_BYTES
            )
        plans = [
            await query_plan(reader, database.dialect is Dialect.POSTGRESQL, query)
            for query in queries
        ]
    (tmp_path / "candidate-query-plans.txt").write_text(
        "\n\n".join("\n".join(plan) for plan in plans)
    )

    assert acquired is not None
    assert (acquired.run, acquired.node_id, acquired.node_path) == (
        expected.run,
        expected.node_id,
        expected.node_path,
    )
    assert acquired.status == status
    assert len(queries) == 2
    if database.dialect is Dialect.SQLITE:
        assert any("ix_claim_node_locator" in row for row in plans[0])
        assert any("ix_run_input_locator" in row for row in plans[1])
    else:
        assert all("consumed_versions" not in sql for sql, _ in queries)
        assert all("params" not in sql for sql, _ in queries)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("owner", "change"),
    [
        (node_instance, {"status": "succeeded"}),
        (node_instance, {"kind": "human_input"}),
        (node_instance, {"attempt_count": 1}),
        (node_instance, {"current_attempt_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV"}),
        (node_instance, {"subgraph_attempt": 2}),
        (node_instance, {"top_level": False}),
        (node_instance, {"deadline_expired_at": START}),
        (node_instance, {"node_path": ""}),
        (run, {"status": "cancelled"}),
        (run, {"status": "pending"}),
        (run, {"deadline_expired_at": START}),
    ],
)
async def test_admission_candidate_refuses_ineligible_owner(
    database: Database, owner: Table, change: dict[str, object]
) -> None:
    expected = await candidate(database)
    identity = expected.node_id if owner is node_instance else expected.run.run_id
    async with database.transaction() as writer:
        await writer.execute(update(owner).where(owner.c.id == identity).values(change))

    async with database.read_transaction() as reader:
        with executed_queries(reader) as queries:
            acquired = await read_admission_candidate(
                reader, expected.node_id, node_row=ROW_BYTES, run_row=ROW_BYTES
            )

    assert acquired is None
    assert len(queries) == (1 if owner is node_instance else 2)


@pytest.mark.asyncio
async def test_admission_candidate_refuses_invalid_and_missing_identity(
    database: Database,
) -> None:
    expected = await candidate(database)
    missing = "00000000000000000000000000"

    async with database.read_transaction() as reader:
        with executed_queries(reader) as invalid_queries:
            invalid = await read_admission_candidate(
                reader, "private-invalid" * 1000, node_row=ROW_BYTES, run_row=ROW_BYTES
            )
            invalid_limit = await read_admission_candidate(
                reader, expected.node_id, node_row=True, run_row=ROW_BYTES
            )
        with executed_queries(reader) as missing_queries:
            absent = await read_admission_candidate(
                reader, missing, node_row=ROW_BYTES, run_row=ROW_BYTES
            )

    assert invalid is invalid_limit is absent is None
    assert invalid_queries == []
    assert len(missing_queries) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [node_instance, run])
async def test_admission_candidate_guards_sqlite_row_before_later_fields(
    database: Database, owner: Table
) -> None:
    expected = await candidate(database)
    is_node = owner is node_instance
    identity = expected.node_id if is_node else expected.run.run_id
    field = "consumed_versions" if is_node else "params"
    async with database.transaction() as writer:
        await writer.execute(
            update(owner)
            .where(owner.c.id == identity)
            .values({field: {"private": "wide-source" * ROW_BYTES}})
        )

    async with database.read_transaction() as reader:
        with executed_queries(reader) as queries:
            acquired = await read_admission_candidate(
                reader, expected.node_id, node_row=ROW_BYTES, run_row=ROW_BYTES
            )

    if database.dialect is Dialect.SQLITE:
        assert acquired is None
        assert len(queries) == (1 if is_node else 2)
    else:
        assert acquired == expected
        assert all(field not in sql for sql, _ in queries)


@pytest.mark.asyncio
async def test_admission_candidate_bounds_path_before_transfer(
    database: Database,
) -> None:
    expected = await candidate(database)
    path = "界" * PATH_LENGTH
    async with database.transaction() as writer:
        await writer.execute(
            update(node_instance)
            .where(node_instance.c.id == expected.node_id)
            .values(node_path=path)
        )

    async with database.read_transaction() as reader:
        acquired = await read_admission_candidate(
            reader, expected.node_id, node_row=ROW_BYTES, run_row=ROW_BYTES
        )

    assert acquired is not None
    assert acquired.node_path == path


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["UTF-8", "UTF-16le", "UTF-16be"])
async def test_admission_candidate_sqlite_encoding_and_scalar_refusal(
    tmp_path: Path, encoding: str
) -> None:
    database = await open_sqlite(tmp_path, encoding)
    try:
        expected = await candidate(database)
        identities = [expected.node_id]
        async with database.transaction() as writer:
            await writer.execute(
                update(node_instance)
                .where(node_instance.c.id == expected.node_id)
                .values(node_path="é😀")
            )
            seed = Seeder(writer)
            for index, invalid_path in enumerate(
                ("x" * (PATH_LENGTH * 4 + 1), b"binary-path", "nul\0path")
            ):
                node_id = await seed.node(expected.run.run_id, f"invalid-{index}")
                identities.append(node_id)
                await writer.exec_driver_sql(
                    "UPDATE main.node_instance SET node_path = ? WHERE id = ?",
                    (invalid_path, node_id),
                )

        async with database.read_transaction() as reader:
            acquired, *rejected = [
                await read_admission_candidate(
                    reader, identity, node_row=ROW_BYTES, run_row=ROW_BYTES
                )
                for identity in identities
            ]
    finally:
        await database.close()

    assert acquired is not None
    assert acquired.node_path == "é😀"
    assert rejected == [None, None, None]
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["UTF-8", "UTF-16le", "UTF-16be"])
@pytest.mark.parametrize("prefix", ["", "nul\0"])
async def test_admission_candidate_bounds_text_before_driver_transfer(
    tmp_path: Path, encoding: str, prefix: str
) -> None:
    database = await open_sqlite(tmp_path, encoding)
    try:
        expected = await candidate(database)
        path = prefix + "界" * (2 * PATH_LENGTH - len(prefix))
        async with database.transaction() as writer:
            await writer.exec_driver_sql(
                "UPDATE main.node_instance SET node_path = ? WHERE id = ?",
                (path, expected.node_id),
            )

        async with database.read_transaction() as reader:
            with executed_queries(reader) as queries:
                acquired = await read_admission_candidate(
                    reader, expected.node_id, node_row=ROW_BYTES, run_row=ROW_BYTES
                )
            [(statement, arguments)] = queries
            with await reader.exec_driver_sql(statement, arguments) as result:
                projected: tuple[object, ...] = tuple(result.one())
                projected_path = projected[1]
    finally:
        await database.close()

    assert acquired is None
    assert projected_path is None
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_admission_price_selects_latest_effective_whole_revision(
    database: Database, tmp_path: Path
) -> None:
    expected = await candidate(database)
    namespace_id = expected.run.namespace_id
    initialization = initializer(
        database,
        prices=(
            PriceConfiguration(None, START, {"model": rates()}),
            PriceConfiguration(namespace_id, START + timedelta(seconds=1), {}),
            PriceConfiguration(
                namespace_id, START + timedelta(seconds=2), {"model": rates("2")}
            ),
        ),
    )
    with captured(
        "engine", LoggingSettings(), "initialize", FakeClock().now
    ) as capture:
        await initialization.initialize(capture.diagnostics)

    async with database.read_transaction() as reader:
        with executed_queries(reader) as queries:
            selected = [
                await read_admission_price_id(
                    reader, namespace_id, START + timedelta(seconds=offset)
                )
                for offset in (-1, 0, 1, 2)
            ]
        plans = [
            await query_plan(reader, database.dialect is Dialect.POSTGRESQL, query)
            for query in queries
        ]
    (tmp_path / "price-query-plans.txt").write_text(
        "\n\n".join("\n".join(plan) for plan in plans)
    )

    assert selected == [
        initialization.baseline.id,
        *(p.id for p in initialization.prices),
    ]
    assert len(queries) == 6
    assert all("prices" not in sql for sql, _ in queries)
    if database.dialect is Dialect.SQLITE:
        assert all(any("ix_price_table_" in row for row in plan) for plan in plans)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_identity", [False, True])
async def test_admission_price_never_falls_back_from_invalid_selected_revision(
    database: Database, invalid_identity: bool
) -> None:
    expected = await candidate(database)
    namespace_id = expected.run.namespace_id
    initialization = initializer(
        database, prices=(PriceConfiguration(namespace_id, START, {}),)
    )
    with captured(
        "engine", LoggingSettings(), "initialize", FakeClock().now
    ) as capture:
        await initialization.initialize(capture.diagnostics)
    selected_id = initialization.prices[0].id
    async with database.transaction() as writer:
        await writer.execute(
            update(price_table)
            .where(price_table.c.id == selected_id)
            .values(
                {"id": "invalid"}
                if invalid_identity
                else {"prices": {"private": "x" * (PRICE_NATIVE_BYTES + 1)}}
            )
        )

    async with database.read_transaction() as reader:
        with executed_queries(reader) as queries:
            selected = await read_admission_price_id(reader, namespace_id, START)
        if not invalid_identity:
            assert selected is not None
            with pytest.raises(ClaimSourcesRejected):
                await acquire_claim_price(reader, namespace_id, selected)

    assert selected == (None if invalid_identity else selected_id)
    assert len(queries) == 1
