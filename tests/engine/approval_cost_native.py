from sqlalchemy import delete, insert, select, text
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.connection import Database
from kora.db.tables import attempt, node_instance, run, usage_record
from tests.db.seed import Seeder
from tests.engine.approval_cost_support import (
    CostCase,
    cost_attempt,
    cost_case,
    cost_usage,
    coverage,
)


async def inventory(
    writer: AsyncConnection,
    run_id: str,
    nodes: int,
    attempt_template: dict[str, object],
    usage_template: dict[str, object],
) -> None:
    seed = Seeder(writer)
    for start in range(0, nodes, 64):
        node_rows = [
            {
                **seed.node_values(run_id, f"node{number}", "succeeded", True),
                "kind": "agent",
            }
            for number in range(start, min(start + 64, nodes))
        ]
        attempts: list[dict[str, object]] = []
        usages: list[dict[str, object]] = []
        for node in node_rows:
            for number in range(1, 11):
                identity = seed.new_id()
                attempts.append(
                    {
                        **attempt_template,
                        "id": identity,
                        "node_instance_id": node["id"],
                        "number": number,
                    }
                )
                for model in ("first", "second"):
                    usages.append(
                        {
                            **usage_template,
                            "id": seed.new_id(),
                            "attempt_id": identity,
                            "model": model,
                        }
                    )
        await writer.execute(insert(node_instance), node_rows)
        await writer.execute(insert(attempt), attempts)
        await writer.execute(insert(usage_record), usages)


async def native_case(database: Database) -> CostCase:
    case = await cost_case(database)
    async with database.transaction() as writer:
        seed = Seeder(writer)
        node_id, attempt_id = await cost_attempt(
            writer, case, "template", saved_coverage=coverage()
        )
        usage_id = await cost_usage(writer, attempt_id)
        attempt_template = dict(
            (await writer.execute(select(attempt).where(attempt.c.id == attempt_id)))
            .mappings()
            .one()
        )
        usage_template = dict(
            (
                await writer.execute(
                    select(usage_record).where(usage_record.c.id == usage_id)
                )
            )
            .mappings()
            .one()
        )
        foreign = dict(
            (await writer.execute(select(run).where(run.c.id == case.run_id)))
            .mappings()
            .one()
        )
        foreign_id = seed.new_id()
        foreign.update(id=foreign_id, idempotency_key=foreign_id)
        await writer.execute(insert(run).values(**foreign))
        await writer.execute(delete(usage_record).where(usage_record.c.id == usage_id))
        await writer.execute(delete(attempt).where(attempt.c.id == attempt_id))
        await writer.execute(delete(node_instance).where(node_instance.c.id == node_id))
        await inventory(writer, case.run_id, 13, attempt_template, usage_template)
        await inventory(writer, foreign_id, 1300, attempt_template, usage_template)
        for table in ("run", "node_instance", "attempt", "usage_record"):
            await writer.execute(text(f"ANALYZE {table}"))
    return case
