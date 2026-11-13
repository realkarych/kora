from datetime import datetime
from enum import StrEnum

from sqlalchemy import insert, select, update
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.cost_reads import (
    COVERAGE_BYTE_LIMIT,
    CostReadRefused,
    HistoricalState,
    admit_projection,
    payload,
    payload_projection,
    read_price,
    required_text,
    scalar_projection,
    stored_timestamp,
)
from kora.db.identifiers import IdentifierGenerator
from kora.db.tables import (
    attempt,
    attempt_dispatcher,
    graph_version,
    node_instance,
    price_table,
    run,
    usage_record,
    worker,
)
from kora.db.usage_context import UsageBinding
from kora.db.worker_usage import read_attempt_usage
from kora.engine.locks import TransitionLocks
from kora.engine.usage_acceptance import PricedUsage, UsageAcceptance, accept_usage
from kora.engine.usage_context import UsageContext
from kora.ir.cost import UsageCoverage, UsageRow
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics
from kora.protocols.authentication import Principal
from kora.protocols.time import Clock

COMPONENT: str = CATALOG.register_component("engine")
PREPARED: EventSpec = CATALOG.register(
    EventSpec(
        "engine.usage.prepared",
        COMPONENT,
        Level.DEBUG,
        "worker usage prepared for commit",
        {"changed_rows": FieldKind.INTEGER, "coverage_changed": FieldKind.BOOLEAN},
    )
)


class UsageAccess(StrEnum):
    NOT_FOUND = "not_found"
    FORBIDDEN = "forbidden"
    STALE_GRANT = "stale_grant"


class UsageAccessRejected(ValueError):
    def __init__(self, reason: UsageAccess) -> None:
        super().__init__("the usage report is not authorized")
        self.reason = reason


async def current_usage_target(
    connection: AsyncConnection,
    context: UsageBinding | None,
    attempt_id: str,
    principal: Principal,
) -> RowMapping:
    if principal.kind != "service":
        raise UsageAccessRejected(UsageAccess.FORBIDDEN)
    if context is not None and context.namespace_id not in {
        grant.namespace_id for grant in principal.namespaces
    }:
        raise UsageAccessRejected(UsageAccess.NOT_FOUND)
    result = await connection.execute(
        select(
            *(
                item
                for name in (
                    "worker_id",
                    "runtime",
                    "image_digest",
                    "execution_domain_id",
                    "strategy",
                    "strategy_fallback",
                    "price_table_id",
                )
                for item in scalar_projection(connection, attempt.c[name])
            ),
            *payload_projection(
                connection, attempt.c.usage_coverage, COVERAGE_BYTE_LIMIT
            ),
            *payload_projection(connection, attempt.c.lease_until, 128),
            *scalar_projection(
                connection, price_table.c.namespace_id.label("price_namespace")
            ),
            *scalar_projection(connection, worker.c.token_id.label("worker_token_id")),
            attempt.c.lease_epoch,
        )
        .select_from(
            attempt.join(node_instance)
            .join(run)
            .join(graph_version)
            .outerjoin(price_table, attempt.c.price_table_id == price_table.c.id)
            .outerjoin(worker, attempt.c.worker_id == worker.c.id)
        )
        .where(
            attempt.c.id == attempt_id,
            run.c.namespace_id.in_(
                grant.namespace_id for grant in principal.namespaces
            ),
            *(
                (
                    run.c.id == context.run_id,
                    run.c.namespace_id == context.namespace_id,
                    run.c.graph_id == context.graph_id,
                    graph_version.c.id == context.graph_version_id,
                    graph_version.c.graph_id == context.graph_id,
                    graph_version.c.execution_contract_digest
                    == context.execution_contract_digest,
                    node_instance.c.node_path == context.node_path,
                )
                if context is not None
                else ()
            ),
            node_instance.c.top_level.is_(True),
        )
    )
    try:
        target = result.mappings().one_or_none()
    finally:
        result.close()
    if target is None:
        raise UsageAccessRejected(UsageAccess.NOT_FOUND)
    if (
        target["worker_id"] != principal.owner
        or target["runtime"] != principal.runtime
        or target["worker_token_id"] != principal.token_id
    ):
        raise UsageAccessRejected(UsageAccess.FORBIDDEN)
    return target


async def require_usage_grant(
    connection: AsyncConnection,
    target: RowMapping,
    attempt_id: str,
    dispatcher_id: str,
    epoch: int,
    principal: Principal,
    now: datetime,
    *,
    lock_worker: bool = True,
) -> None:
    until = stored_timestamp(target["lease_until"])
    if until is not None and until.utcoffset() is None:
        raise CostReadRefused()
    if target["lease_epoch"] != epoch or until is None or until <= now:
        raise UsageAccessRejected(UsageAccess.STALE_GRANT)
    statement = (
        select(worker.c.id)
        .select_from(attempt_dispatcher.join(worker))
        .where(
            attempt_dispatcher.c.id == dispatcher_id,
            attempt_dispatcher.c.attempt_id == attempt_id,
            attempt_dispatcher.c.lease_epoch == epoch,
            attempt_dispatcher.c.execution_domain_id == target["execution_domain_id"],
            attempt_dispatcher.c.incarnation_id == worker.c.incarnation_id,
            worker.c.id == principal.owner,
            worker.c.token_id == principal.token_id,
            worker.c.runtime == principal.runtime,
        )
    )
    valid = await connection.scalar(
        statement.with_for_update(of=worker) if lock_worker else statement
    )
    if not valid:
        raise UsageAccessRejected(UsageAccess.STALE_GRANT)


async def report_usage(
    connection: AsyncConnection,
    locks: TransitionLocks,
    context: UsageContext,
    attempt_id: str,
    dispatcher_id: str,
    epoch: int,
    principal: Principal,
    rows: tuple[UsageRow, ...],
    coverage: UsageCoverage,
    clock: Clock,
    identifiers: IdentifierGenerator,
) -> UsageAcceptance:
    locks.require_connection(connection)
    locks.require(context.run_id)
    target = await current_usage_target(connection, context, attempt_id, principal)
    await require_usage_grant(
        connection, target, attempt_id, dispatcher_id, epoch, principal, clock.now()
    )
    admit_projection(target)
    if target["strategy"] == "resume" and target["strategy_fallback"] is None:
        raise CostReadRefused()
    price_namespace = (
        None
        if target["price_namespace"] is None
        else required_text(target["price_namespace"])
    )
    if price_namespace not in (None, context.namespace_id):
        raise CostReadRefused()
    price = await read_price(
        connection, price_namespace, required_text(target["price_table_id"])
    )
    if price.state is not HistoricalState.VERSION_1 or price.revision is None:
        raise CostReadRefused()
    prior = await read_attempt_usage(connection, context.namespace_id, attempt_id)
    saved_coverage = payload(target, "usage_coverage", COVERAGE_BYTE_LIMIT)
    try:
        prior_coverage = (
            None
            if saved_coverage is None
            else UsageCoverage.model_validate(saved_coverage)
        )
    except ValueError as error:
        raise CostReadRefused() from error
    accepted = accept_usage(
        tuple(
            PricedUsage(
                item.row,
                item.usd_estimate,
                item.dispatcher_id,
                item.same_basis_usd_floor,
            )
            for item in prior
        ),
        prior_coverage,
        rows,
        coverage,
        price.revision,
        context.mapping,
        dispatcher_id,
    )
    now = clock.now()
    await require_usage_grant(
        connection, target, attempt_id, dispatcher_id, epoch, principal, now
    )
    existing = {item.row.model for item in prior}
    for changed in accepted.changed_rows:
        values: dict[str, object] = changed.row.model_dump(mode="json")
        values.update(
            observed_at=changed.row.observed_at,
            usd_estimate=changed.usd_estimate,
            same_basis_usd_floor=changed.same_basis_usd_floor,
            dispatcher_id=dispatcher_id,
            worker_id=principal.owner,
            image_digest=target["image_digest"],
            normalization_version=1,
            updated_at=now,
        )
        statement = (
            update(usage_record).where(
                usage_record.c.attempt_id == attempt_id,
                usage_record.c.model == changed.row.model,
            )
            if changed.row.model in existing
            else insert(usage_record).values(
                id=identifiers.new(), attempt_id=attempt_id, created_at=now
            )
        )
        await connection.execute(statement.values(**values))
    if accepted.coverage_changed:
        await connection.execute(
            update(attempt)
            .where(attempt.c.id == attempt_id)
            .values(usage_coverage=coverage.model_dump(mode="json"), updated_at=now)
        )
    if accepted.changed_rows or accepted.coverage_changed:
        await connection.execute(
            update(run).where(run.c.id == context.run_id).values(needs_scheduling=True)
        )
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(
            PREPARED,
            changed_rows=len(accepted.changed_rows),
            coverage_changed=accepted.coverage_changed,
        )
    return accepted
