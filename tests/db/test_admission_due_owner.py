from datetime import timedelta

import pytest
from sqlalchemy import insert, select, update

from kora.db.admission_waits import due_admission_wait
from kora.db.connection import Database, Dialect
from kora.db.tables import wait
from tests.db.closing import ConnectionLedger
from tests.db.seed import START, Seeder
from tests.db.test_graph_read_admission import executed_queries
from tests.engine.admission_wait_support import admission_case


@pytest.mark.asyncio
@pytest.mark.parametrize("offset", [-1, 0, 1])
async def test_due_owner_uses_exact_wake_without_legacy_payload(
    database: Database, offset: int
) -> None:
    case = await admission_case(database)
    async with database.transaction() as writer:
        await case.apply(await case.context(writer))
    [saved] = await case.saved_waits(database)
    async with database.transaction() as writer:
        await writer.execute(
            update(wait)
            .where(wait.c.id == saved["id"])
            .values(
                wake_at=START,
                reason="private-reason" * 100_000,
                observation={"private": "old-observation" * 100_000},
            )
        )
        if database.dialect is Dialect.SQLITE:
            await writer.exec_driver_sql(
                "UPDATE wait SET reason = 'invalid-json' WHERE id = ?", (saved["id"],)
            )
    ledger = ConnectionLedger(database)

    async with database.read_transaction() as reader:
        with executed_queries(reader) as queries:
            selected = await due_admission_wait(
                reader, case.binding.node_id, START + timedelta(microseconds=offset)
            )

    assert selected == (saved["id"] if offset >= 0 else None)
    assert len(queries) == 2
    assert all("reason" not in sql and "observation" not in sql for sql, _ in queries)
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["closed", "kind", "attempt", "ambiguous", "null"])
async def test_due_owner_refuses_incompatible_current_wait(
    database: Database, changed: str
) -> None:
    case = await admission_case(database)
    async with database.transaction() as writer:
        await case.apply(await case.context(writer))
    [saved] = await case.saved_waits(database)
    async with database.transaction() as writer:
        seed = Seeder(writer)
        if changed == "ambiguous":
            duplicate = dict(saved)
            duplicate["id"] = seed.new_id()
            await writer.execute(insert(wait).values(**duplicate))
        else:
            values: dict[str, object] = {"wake_at": START}
            if changed == "closed":
                values["status"] = "satisfied"
            elif changed == "kind":
                values["kind"] = "timer"
            elif changed == "attempt":
                values["attempt_id"] = await seed.attempt(case.binding.node_id)
            else:
                values["wake_at"] = None
            await writer.execute(
                update(wait).where(wait.c.id == saved["id"]).values(**values)
            )

    async with database.read_transaction() as reader:
        found = await due_admission_wait(reader, case.binding.node_id, START)
        count = len((await reader.execute(select(wait.c.id))).all())

    assert found is None
    assert count == (2 if changed == "ambiguous" else 1)
    assert database.units_in_flight() == 0
