from datetime import datetime

from sqlalchemy import case, func, literal_column, select, update

from kora.db.deadline_discovery_v1 import CANCEL_ROWS
from kora.db.tables import (
    approval_request,
    attempt,
    attempt_dispatcher,
    node_instance,
    run,
    wait,
)
from kora.db.types import POSTGRESQL, as_json_value, normalise_timestamp
from kora.engine.deadline_records import (
    MAX_DEADLINE_RECORD_BYTES,
    RecordedDeadlineBinding,
    decode_deadline_record,
)
from kora.engine.expiry_context import (
    ACTIVE_ATTEMPT_STATUSES,
    ExpiryContext,
    ExpiryState,
    bounded_json,
    expiry_transition,
    json_scalar,
    set_json_field,
)
from kora.engine.expiry_reads import (
    admit_attempt_read,
    read_expiry_run,
    timeout_reached,
)

TERMINAL_NODES = ("succeeded", "failed", "cancelled", "skipped", "upstream_failed")


async def request_cancellation(
    context: ExpiryContext, now: datetime, attempt_id: str | None = None
) -> None:
    nodes = select(node_instance.c.id).where(node_instance.c.run_id == context.run_id)
    owned_nodes = nodes
    while True:
        with await context.connection.execute(
            nodes.order_by(node_instance.c.id).limit(100)
        ) as result:
            owners = list(result.scalars())
        if not owners:
            return
        for node_id in owners:
            await _cancel_node_attempts(context, now, node_id, attempt_id)
            if attempt_id is None:
                await close_expired_waits(context, now, node_id)
        nodes = owned_nodes.where(node_instance.c.id > owners[-1])


async def _cancel_node_attempts(
    context: ExpiryContext, now: datetime, node_id: str, attempt_id: str | None
) -> None:
    granted = (
        select(attempt_dispatcher.c.id)
        .where(attempt_dispatcher.c.attempt_id == attempt.c.id)
        .limit(1)
        .scalar_subquery()
        .is_not(None)
    )
    uncertain = (
        granted
        | (attempt.c.execution_state != "not_started")
        | attempt.c.execution_domain_id.is_not(None)
        | attempt.c.handle.is_not(None)
    )
    statement = select(
        attempt.c.id,
        attempt.c.node_instance_id,
        attempt.c.number,
        attempt.c.recovery_required,
        uncertain.label("uncertain"),
    )
    candidates = select(attempt.c.id).where(
        attempt.c.node_instance_id == node_id, literal_column(CANCEL_ROWS)
    )
    if attempt_id is not None:
        candidates = candidates.where(attempt.c.id == attempt_id)
    remaining = candidates
    while True:
        page = remaining.order_by(attempt.c.id).limit(100)
        with await context.connection.execute(
            statement.where(attempt.c.id.in_(page)).order_by(attempt.c.id)
        ) as result:
            rows = result.all()
        if not rows:
            return
        for row in rows:
            if not await admit_attempt_read(context, row.id):
                raise ValueError("attempt cancellation exceeds read admission")
            await context.connection.execute(
                update(attempt)
                .where(attempt.c.id == row.id)
                .values(
                    desired_state="cancel",
                    cancel_cause=func.coalesce(attempt.c.cancel_cause, "timeout"),
                    recovery_required=attempt.c.recovery_required | uncertain,
                    recovery_version=attempt.c.recovery_version
                    + case((uncertain, 1), else_=0),
                    recovery=case(
                        (
                            uncertain,
                            set_json_field(
                                context.connection,
                                attempt.c.recovery,
                                "reason",
                                json_scalar(context.connection, "deadline_expired"),
                            ),
                        ),
                        else_=attempt.c.recovery,
                    ),
                    updated_at=now,
                )
            )
            if row.uncertain and not row.recovery_required:
                await context.event(
                    "attempt.recovery_required",
                    {"number": row.number, "reasons": ["deadline_expired"]},
                    now,
                    row.node_instance_id,
                    row.id,
                )
        remaining = candidates.where(attempt.c.id > rows[-1].id)


async def observe_run_expiry(
    context: ExpiryContext, *, observed_at: datetime | None = None
) -> ExpiryState:
    async with expiry_transition(context, "run_guard"):
        return await _observe_run_expiry(context, observed_at)


async def _observe_run_expiry(
    context: ExpiryContext, observed_at: datetime | None = None
) -> ExpiryState:
    saved = await read_expiry_run(context)
    if saved is None:
        return ExpiryState.REFUSED
    if saved.deadline_expired_at is not None:
        return ExpiryState.EXPIRED
    if saved.status != "running" or saved.started_at is None:
        return ExpiryState.REFUSED
    now = normalise_timestamp(context.clock()) if observed_at is None else observed_at
    if not timeout_reached(saved.started_at, now, saved.limits.timeout):
        return ExpiryState.ACTIVE
    await context.connection.execute(
        update(run).where(run.c.id == context.run_id).values(deadline_expired_at=now)
    )
    await request_cancellation(context, now)
    await context.mark(now)
    return ExpiryState.EXPIRED


async def observe_attempt_expiry(
    context: ExpiryContext, attempt_id: str
) -> ExpiryState:
    async with expiry_transition(context, "attempt_guard"):
        parent = await observe_run_expiry(context)
        if parent is not ExpiryState.ACTIVE:
            return parent
        if not await admit_attempt_read(context, attempt_id):
            return ExpiryState.REFUSED
        with await context.connection.execute(
            select(
                attempt.c.deadline,
                bounded_json(attempt.c.deadline_binding, MAX_DEADLINE_RECORD_BYTES),
                attempt.c.deadline_expired_at,
                attempt.c.started_at,
            )
            .join(node_instance, attempt.c.node_instance_id == node_instance.c.id)
            .where(
                attempt.c.id == attempt_id,
                node_instance.c.run_id == context.run_id,
                attempt.c.status.in_(ACTIVE_ATTEMPT_STATUSES),
            )
        ) as result:
            row = result.one_or_none()
        if row is None or row[3] is None:
            return ExpiryState.REFUSED
        if row[2] is not None:
            return ExpiryState.EXPIRED
        binding = decode_deadline_record(as_json_value(row[1]), row[0])
        if not isinstance(binding, RecordedDeadlineBinding):
            return ExpiryState.REFUSED
        deadline = binding.deadline
        if deadline is None:
            return ExpiryState.ACTIVE
        if deadline.scope_kind == "run" and deadline.scope_id == context.run_id:
            observed_at = normalise_timestamp(context.clock())
            if observed_at < deadline.at:
                return ExpiryState.ACTIVE
            return await _observe_run_expiry(context, observed_at)
        if deadline.scope_kind != "attempt" or deadline.scope_id != attempt_id:
            return ExpiryState.REFUSED
        now = normalise_timestamp(context.clock())
        if now < deadline.at:
            return ExpiryState.ACTIVE
        await context.connection.execute(
            update(attempt)
            .where(attempt.c.id == attempt_id)
            .values(deadline_expired_at=now, updated_at=now)
        )
        await request_cancellation(context, now, attempt_id)
        await context.mark(now)
        return ExpiryState.EXPIRED


async def close_expired_waits(
    context: ExpiryContext, now: datetime, node_id: str
) -> None:
    connection = context.connection
    due = (
        select(wait.c.id)
        .where(wait.c.node_instance_id == node_id, wait.c.status == "open")
        .order_by(wait.c.id)
        .limit(100)
    )
    delivery = approval_request.c.delivery
    revision = func.coalesce(delivery["revision"].as_integer(), 0) + 1
    next_revision = (
        func.to_jsonb(revision) if connection.dialect.name == POSTGRESQL else revision
    )
    closed = set_json_field(
        connection, delivery, "desired_state", json_scalar(connection, "closed")
    )
    closed = set_json_field(connection, closed, "revision", next_revision)
    while True:
        with await connection.execute(due) as result:
            open_waits = list(result.scalars())
        if not open_waits:
            return
        await connection.execute(
            update(approval_request)
            .where(approval_request.c.wait_id.in_(open_waits))
            .values(
                status=case(
                    (approval_request.c.status == "pending", "cancelled"),
                    else_=approval_request.c.status,
                ),
                delivery=case(
                    (delivery["desired_state"].as_string() == "closed", delivery),
                    else_=closed,
                ),
                updated_at=now,
            )
        )
        await connection.execute(
            update(wait)
            .where(wait.c.id.in_(open_waits))
            .values(
                status="cancelled",
                observation=set_json_field(
                    connection,
                    wait.c.observation,
                    "current",
                    json_scalar(connection, None),
                ),
                updated_at=now,
            )
        )


async def expire_run(context: ExpiryContext) -> ExpiryState:
    async with expiry_transition(context, "run_expiry"):
        context.require_capacity()
        state = await observe_run_expiry(context)
        if state is not ExpiryState.EXPIRED:
            return state
        saved = await read_expiry_run(context)
        if saved is None:
            return ExpiryState.REFUSED
        if saved.status != "running":
            return ExpiryState.EXPIRED
        now = normalise_timestamp(context.clock())
        await request_cancellation(context, now)
        while True:
            with await context.connection.execute(
                select(node_instance.c.id, node_instance.c.node_path)
                .where(
                    node_instance.c.run_id == context.run_id,
                    node_instance.c.status.not_in(TERMINAL_NODES),
                )
                .order_by(node_instance.c.id)
                .limit(100)
            ) as result:
                rows = result.all()
            if not rows:
                break
            for row in rows:
                await context.connection.execute(
                    update(node_instance)
                    .where(node_instance.c.id == row.id)
                    .values(status="cancelled", updated_at=now)
                )
                await context.event(
                    "node.cancelled",
                    {"node_path": row.node_path, "reason": "timeout"},
                    now,
                    row.id,
                )
        await context.connection.execute(
            update(run)
            .where(run.c.id == context.run_id)
            .values(
                status="failed", finished_at=now, updated_at=now, needs_scheduling=True
            )
        )
        await context.event(
            "run.failed",
            {
                "trigger": as_json_value(saved.trigger.model_dump(mode="json")),
                "graph_version": context.scope.version_id,
                "rerun_of": saved.rerun_of_run_id,
                "deadline": {"scope": "run", "reason": "timeout"},
            },
            now,
        )
        return ExpiryState.EXPIRED
