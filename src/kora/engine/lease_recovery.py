from datetime import datetime

from sqlalchemy import select, update

from kora.db.tables import attempt, attempt_dispatcher, node_instance
from kora.db.types import normalise_timestamp
from kora.engine.expiry_context import (
    ACTIVE_ATTEMPT_STATUSES,
    ExpiryContext,
    bounded_json,
    expiry_transition,
    json_scalar,
    set_json_field,
)
from kora.engine.expiry_reads import admit_attempt_read
from kora.engine.outcome_records import MAX_RECEIPT_BYTES, OutcomeReceipt
from kora.protocols.boundary import BoundaryModel
from kora.protocols.identity_records import Identifier


class LeaseState(BoundaryModel):
    id: Identifier
    node_instance_id: Identifier
    number: int
    status: str
    execution_state: str
    execution_domain_id: Identifier | None
    recovery_required: bool
    recovery_version: int
    lease_epoch: int
    lease_until: datetime | None


async def closed_reporting_lease(context: ExpiryContext, saved: LeaseState) -> bool:
    if saved.status != "finishing" or saved.execution_state != "stopped":
        return False
    with await context.connection.execute(
        select(
            bounded_json(attempt.c.outcome, MAX_RECEIPT_BYTES),
            attempt.c.outcome.is_(None),
        ).where(attempt.c.id == saved.id)
    ) as result:
        value, absent = result.one()
    if absent:
        return False
    receipt = OutcomeReceipt.model_validate(value)
    return (
        receipt.reporting_lease_epoch == saved.lease_epoch
        and await context.connection.scalar(
            select(
                select(attempt_dispatcher.c.id)
                .where(
                    attempt_dispatcher.c.id == receipt.reporting_dispatcher_id,
                    attempt_dispatcher.c.attempt_id == saved.id,
                    attempt_dispatcher.c.lease_epoch == saved.lease_epoch,
                    attempt_dispatcher.c.execution_domain_id
                    == saved.execution_domain_id,
                    attempt_dispatcher.c.closed_at.is_not(None),
                )
                .exists()
            )
        )
        is True
    )


async def recover_expired_lease(context: ExpiryContext, attempt_id: str) -> bool:
    async with expiry_transition(context, "lease_expiry"):
        if not await admit_attempt_read(context, attempt_id):
            return False
        fields = [attempt.c[name] for name in LeaseState.model_fields]
        with await context.connection.execute(
            select(*fields)
            .join(node_instance, attempt.c.node_instance_id == node_instance.c.id)
            .where(
                attempt.c.id == attempt_id,
                node_instance.c.run_id == context.run_id,
                attempt.c.status.in_(ACTIVE_ATTEMPT_STATUSES[1:]),
            )
        ) as result:
            row = result.mappings().one_or_none()
        if row is None:
            return False
        saved = LeaseState.model_validate(dict(row))
        now = normalise_timestamp(context.clock())
        if saved.lease_until is None or now < saved.lease_until:
            return False
        closed = await closed_reporting_lease(context, saved)
        if closed:
            await context.connection.execute(
                update(attempt)
                .where(attempt.c.id == attempt_id)
                .values(lease_owner=None, lease_until=None, updated_at=now)
            )
        else:
            await context.connection.execute(
                update(attempt)
                .where(attempt.c.id == attempt_id)
                .values(
                    lease_owner=None,
                    lease_until=None,
                    execution_state="unknown",
                    recovery_required=True,
                    recovery_version=saved.recovery_version + 1,
                    recovery=set_json_field(
                        context.connection,
                        attempt.c.recovery,
                        "reason",
                        json_scalar(context.connection, "lease_expired"),
                    ),
                    updated_at=now,
                )
            )
            await context.event(
                "attempt.execution_unknown",
                {
                    "version": saved.recovery_version + 1,
                    "domain": saved.execution_domain_id,
                    "reason": "lease_expired",
                    "actor": {"kind": "system", "component": "scheduler"},
                    "evidence": None,
                },
                now,
                saved.node_instance_id,
                attempt_id,
            )
            if not saved.recovery_required:
                await context.event(
                    "attempt.recovery_required",
                    {"number": saved.number, "reasons": ["lease_expired"]},
                    now,
                    saved.node_instance_id,
                    attempt_id,
                )
        await context.mark(now)
        return True


async def close_undispatched_attempt(context: ExpiryContext, attempt_id: str) -> bool:
    async with expiry_transition(context, "undispatched_closure"):
        if not await admit_attempt_read(context, attempt_id):
            return False
        grants = (
            select(attempt_dispatcher.c.id)
            .where(attempt_dispatcher.c.attempt_id == attempt.c.id)
            .exists()
        )
        with await context.connection.execute(
            select(attempt.c.node_instance_id, attempt.c.runtime, attempt.c.number)
            .join(node_instance, attempt.c.node_instance_id == node_instance.c.id)
            .where(
                attempt.c.id == attempt_id,
                node_instance.c.run_id == context.run_id,
                attempt.c.status == "created",
                node_instance.c.top_level.is_(True),
                attempt.c.desired_state == "cancel",
                attempt.c.execution_state == "not_started",
                attempt.c.execution_domain_id.is_(None),
                attempt.c.worker_id.is_(None),
                attempt.c.lease_owner.is_(None),
                attempt.c.lease_until.is_(None),
                attempt.c.lease_epoch == 0,
                attempt.c.started_at.is_(None),
                attempt.c.handle.is_(None),
                ~grants,
            )
        ) as result:
            row = result.one_or_none()
        if row is None:
            return False
        context.require_capacity(str(row.runtime))
        now = normalise_timestamp(context.clock())
        await context.connection.execute(
            update(attempt)
            .where(attempt.c.id == attempt_id)
            .values(
                status="cancelled",
                recovery_required=False,
                finished_at=now,
                updated_at=now,
            )
        )
        await context.event(
            "attempt.cancelled",
            {"number": row.number, "worker_id": None, "error_class": None},
            now,
            row.node_instance_id,
            attempt_id,
        )
        await context.mark(now)
        return True
