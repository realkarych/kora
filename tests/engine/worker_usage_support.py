from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import insert, select, update

from kora.db.connection import Database
from kora.db.identifiers import IdentifierGenerator
from kora.db.tables import (
    attempt,
    attempt_dispatcher,
    node_instance,
    price_table,
    run,
    usage_record,
)
from kora.engine.attempt_usage import report_usage
from kora.engine.graph_execution_contract import check_detached_execution_contract
from kora.engine.graph_resource_references import GraphResourceScope
from kora.engine.locks import TransitionLocks
from kora.engine.usage_acceptance import UsageAcceptance
from kora.engine.usage_context import UsageContext, prepare_usage_context
from kora.engine.worker_registration import Registration, register_worker
from kora.ir.cost import PriceRevision, UsageCoverage, UsageRow
from kora.protocols.authentication import Principal
from tests.api.worker_support import registration
from tests.db.seed import START, Seeder, fixed_randomness
from tests.engine.test_graph_execution_contract import (
    LIMITS,
    child,
    complete_inputs,
    insert_case,
    prepared_case,
    transport,
)
from tests.engine.test_usage_acceptance import coverage as observed_coverage
from tests.ir.test_cost_models import rates_payload, revision_payload
from tests.plugins.authentication.support import AuthorityClock, seed_case


@dataclass(frozen=True)
class UsageCase:
    context: UsageContext
    attempt_id: str
    dispatcher_id: str
    principal: Principal
    clock: AuthorityClock
    identifiers: IdentifierGenerator


async def usage_case(
    database: Database,
    rate_changes: dict[str, object] | None = None,
    *,
    harness: str = "claude",
    node_path: str = "agent",
) -> UsageCase:
    authority = await seed_case(database, "usage", "service")
    principal = authority.principal
    namespace_id = principal.namespaces[0].namespace_id
    body = registration(authority)
    clock = AuthorityClock()
    async with database.transaction() as connection:
        seed = Seeder(connection)
        body = body.model_copy(
            update={
                "incarnation_id": seed.new_id(),
                "execution_domain_id": seed.new_id(),
            }
        )
        await register_worker(
            connection,
            Registration(
                body.name,
                body.incarnation_id,
                body.runtime,
                body.execution_domain_id,
                body.version,
                body.capacity,
                body.process,
            ),
            principal,
            portable=False,
            now=START,
            event_id=seed.new_id(),
        )
        graph_id = await seed.graph(namespace_id)
        scope = GraphResourceScope(namespace_id, graph_id, seed.new_id())
    raw, envelope, refs, owners = complete_inputs(scope, resources=False)
    child(raw, "graph", "nodes", "agent", "config")["harness"] = harness
    prepared, pin = prepared_case(raw, envelope, scope)
    checked = check_detached_execution_contract(
        transport(prepared, scope), pin, scope, refs, owners, limits=LIMITS
    )
    async with database.transaction() as connection:
        seed = Seeder(connection)
        await insert_case(seed, scope, 1, prepared, refs)
        run_id = await seed.run(
            namespace_id, graph_id, scope.version_id, needs_scheduling=False
        )
        node_id = await seed.node(run_id, node_path)
        attempt_id = await seed.attempt(node_id)
        await connection.execute(
            update(node_instance)
            .where(node_instance.c.id == node_id)
            .values(status="running", current_attempt_id=attempt_id)
        )
        await connection.execute(
            update(run)
            .where(run.c.id == run_id)
            .values(status="running", started_at=START)
        )
        dispatcher = seed.new_id()
        revision = PriceRevision.model_validate(
            {
                **revision_payload(
                    {"model": {**rates_payload(), **(rate_changes or {})}}
                ),
                "effective_from": START,
            }
        )
        await connection.execute(
            insert(price_table).values(
                **revision.model_dump(mode="python"), model=None, **seed.stamps()
            )
        )
        await connection.execute(
            update(attempt)
            .where(attempt.c.id == attempt_id)
            .values(
                worker_id=principal.owner,
                execution_domain_id=body.execution_domain_id,
                lease_epoch=1,
                lease_until=START + timedelta(seconds=120),
                price_table_id=revision.id,
                image_digest="server-image",
            )
        )
        await connection.execute(
            insert(attempt_dispatcher).values(
                id=dispatcher,
                attempt_id=attempt_id,
                execution_domain_id=body.execution_domain_id,
                worker_id=principal.owner,
                incarnation_id=body.incarnation_id,
                lease_epoch=1,
                mode="execute",
                granted_at=START,
                closed_at=None,
                evidence=None,
                **seed.stamps(),
            )
        )
    context = prepare_usage_context(checked, run_id, node_path, max_ir_bytes=1_000_000)
    return UsageCase(
        context,
        attempt_id,
        dispatcher,
        principal,
        clock,
        IdentifierGenerator(clock.now, fixed_randomness(500_000)),
    )


async def submit_usage(
    database: Database,
    case: UsageCase,
    rows: tuple[UsageRow, ...],
    coverage: UsageCoverage | None = None,
) -> UsageAcceptance:
    async with database.transaction() as connection:
        locks = TransitionLocks(connection)
        await locks.acquire_run(case.context.run_id)
        return await report_usage(
            connection,
            locks,
            case.context,
            case.attempt_id,
            case.dispatcher_id,
            1,
            case.principal,
            rows,
            observed_coverage() if coverage is None else coverage,
            case.clock,
            case.identifiers,
        )


async def usage_snapshot(database: Database, case: UsageCase) -> tuple[object, ...]:
    async with database.read_transaction() as connection:
        rows = (
            (
                await connection.execute(
                    select(usage_record)
                    .where(usage_record.c.attempt_id == case.attempt_id)
                    .order_by(usage_record.c.model)
                )
            )
            .mappings()
            .all()
        )
        current = (
            await connection.execute(
                select(attempt.c.usage_coverage, attempt.c.updated_at).where(
                    attempt.c.id == case.attempt_id
                )
            )
        ).one()
        marker = await connection.scalar(
            select(run.c.needs_scheduling).where(run.c.id == case.context.run_id)
        )
    return tuple(dict(row) for row in rows), tuple(current), marker
