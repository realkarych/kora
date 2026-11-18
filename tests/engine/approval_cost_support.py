from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from hashlib import sha256

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.connection import Database
from kora.db.tables import attempt, node_instance, usage_record, worker
from kora.engine.approval_cost import calculate_approval_cost
from kora.engine.approval_cost_totals import TOKEN_FIELDS
from kora.engine.locks import TransitionLocks
from kora.ir.cost import BillingClass, Completeness, RunCost, UsageCoverage
from tests.db.seed import START, Seeder


@dataclass(frozen=True)
class CostCase:
    namespace_id: str
    run_id: str
    worker_id: str
    domain_id: str

    async def calculate(self, database: Database, at: datetime = START) -> RunCost:
        async with database.transaction() as writer:
            locks = TransitionLocks(writer)
            await locks.acquire_run(self.run_id)
            return await calculate_approval_cost(
                writer, locks, self.namespace_id, self.run_id, at
            )


async def cost_case(database: Database, name: str = "accounting") -> CostCase:
    async with database.transaction() as writer:
        seed = Seeder(writer)
        namespace_id = await seed.namespace(name)
        graph_id = await seed.graph(namespace_id)
        version_id = await seed.graph_version(graph_id)
        run_id = await seed.run(namespace_id, graph_id, version_id)
        token = await seed.token(sha256(seed.new_id().encode()).hexdigest(), "service")
        worker_id = await seed.worker(token, seed.new_id())
        domain = await writer.scalar(
            select(worker.c.execution_domain_id).where(worker.c.id == worker_id)
        )
    assert isinstance(domain, str)
    return CostCase(namespace_id, run_id, worker_id, domain)


def coverage(
    *,
    at: datetime = START,
    completeness: Completeness = "complete",
    attribution_gap: bool = False,
    observation_gap: bool = False,
) -> UsageCoverage:
    return UsageCoverage(
        version=1,
        observation_id="observation",
        observation_sequence=1,
        completeness=completeness,
        observed_at=at,
        attribution_gap=attribution_gap,
        observation_gap=observation_gap,
    )


async def cost_attempt(
    writer: AsyncConnection,
    case: CostCase,
    path: str,
    *,
    status: str = "succeeded",
    started: bool = True,
    kind: str = "agent",
    saved_coverage: UsageCoverage | None = None,
    number: int = 1,
    node_id: str | None = None,
) -> tuple[str, str]:
    seed = Seeder(writer)
    node = await seed.node(case.run_id, path) if node_id is None else node_id
    await writer.execute(
        update(node_instance).where(node_instance.c.id == node).values(kind=kind)
    )
    identity = await seed.attempt(node, status, number)
    await writer.execute(
        update(attempt)
        .where(attempt.c.id == identity)
        .values(
            started_at=START if started else None,
            lease_epoch=1 if started else 0,
            worker_id=case.worker_id if started else None,
            execution_domain_id=case.domain_id if started else None,
            execution_state="stopped" if started else "not_started",
            usage_coverage=None
            if saved_coverage is None
            else saved_coverage.model_dump(mode="json"),
        )
    )
    return node, identity


def usage_values(
    seed: Seeder,
    attempt_id: str,
    model: str = "model",
    *,
    amount: str | None = "0.1",
    billing: BillingClass = "api_key",
    completeness: Completeness = "complete",
    at: datetime = START,
) -> dict[str, object]:
    return {
        "id": seed.new_id(),
        "attempt_id": attempt_id,
        "model": model,
        "input_tokens": 3,
        "cached_input_tokens": 2,
        "cache_creation_tokens": 1,
        "output_tokens": 5,
        "reasoning_tokens": 4,
        "known_token_fields": list(TOKEN_FIELDS),
        "completeness": completeness,
        "basis": "price_table",
        "billing_class": billing,
        "usd_estimate": None if amount is None else Decimal(amount),
        "delta_of_session_totals": False,
        "source": {"private": "must-not-be-read"},
        "normalization_version": 1,
        "observation_id": "observation",
        "observation_sequence": 1,
        "observed_at": at,
        **seed.stamps(),
    }


async def cost_usage(
    writer: AsyncConnection,
    attempt_id: str,
    model: str = "model",
    *,
    amount: str | None = "0.1",
    billing: BillingClass = "api_key",
    completeness: Completeness = "complete",
    at: datetime = START,
) -> str:
    values = usage_values(
        Seeder(writer),
        attempt_id,
        model,
        amount=amount,
        billing=billing,
        completeness=completeness,
        at=at,
    )
    await writer.execute(insert(usage_record).values(**values))
    return str(values["id"])
