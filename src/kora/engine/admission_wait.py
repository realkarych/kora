import asyncio
from datetime import timedelta
from enum import StrEnum

from sqlalchemy import insert, select, update
from sqlalchemy.exc import SQLAlchemyError

from kora.db.admission_waits import open_wait_matches
from kora.db.attempt_capacity import RunRecovery
from kora.db.claim_sources import (
    ClaimSourceBinding,
    ClaimSourceReadLimits,
    verify_claim_source_binding,
)
from kora.db.connection import clear_snapshot_error
from kora.db.identifiers import is_identifier
from kora.db.tables import node_instance, wait
from kora.db.transactions import raise_private_metadata_error
from kora.db.types import POSTGRESQL, normalise_timestamp
from kora.engine.admission_wait_reason import admission_wait_reason
from kora.engine.attempt_capacity import CapacityLimit
from kora.engine.expiry import observe_run_expiry
from kora.engine.expiry_context import ExpiryContext, ExpiryFailed, ExpiryState
from kora.engine.locks import COMPONENT
from kora.engine.writer_mode import writable_postgres_transaction
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

PREPARED: EventSpec = CATALOG.register(
    EventSpec(
        "engine.admission_wait.prepared",
        COMPONENT,
        Level.DEBUG,
        "admission wait prepared for commit",
        {"outcome": FieldKind.STRING},
    )
)


class AdmissionWaitState(StrEnum):
    CREATED = "created"
    REFRESHED = "refreshed"
    EXPIRED = "expired"
    REFUSED = "refused"


class AdmissionWaitFailed(RuntimeError):
    def __init__(self) -> None:
        super().__init__("admission wait transition failed")


async def _save(
    context: ExpiryContext,
    binding: ClaimSourceBinding,
    limits: ClaimSourceReadLimits,
    runtime: str,
    observation: CapacityLimit | RunRecovery,
    interval: timedelta,
    expected_wait_id: str | None,
) -> AdmissionWaitState:
    connection = context.connection
    context.locks.require_connection(connection)
    context.require_capacity(runtime)
    parent = binding.run
    if (
        interval <= timedelta(0)
        or (expected_wait_id is not None and not is_identifier(expected_wait_id))
        or (parent.namespace_id, parent.graph_id, parent.version_id, parent.run_id)
        != (
            context.scope.namespace_id,
            context.scope.graph_id,
            context.scope.version_id,
            context.run_id,
        )
        or (
            connection.dialect.name == POSTGRESQL
            and not await writable_postgres_transaction(connection)
        )
        or not await verify_claim_source_binding(
            connection, binding, limits, include_attempt=False
        )
    ):
        return AdmissionWaitState.REFUSED
    eligible = select(node_instance.c.id).where(
        node_instance.c.id == binding.node_id,
        node_instance.c.status == ("ready" if expected_wait_id is None else "waiting"),
        node_instance.c.kind.in_(("agent", "command")),
        node_instance.c.top_level.is_(True),
        node_instance.c.subgraph_attempt == 1,
        node_instance.c.attempt_count == 0,
        node_instance.c.current_attempt_id.is_(None),
        node_instance.c.deadline_expired_at.is_(None),
    )
    if await connection.scalar(eligible) is None or not await open_wait_matches(
        connection, binding.node_id, expected_wait_id
    ):
        return AdmissionWaitState.REFUSED
    now = normalise_timestamp(context.clock())
    reason = admission_wait_reason(observation, context.scope, runtime, now)
    wake_at = now + interval
    state = await observe_run_expiry(context, observed_at=now)
    if state is not ExpiryState.ACTIVE:
        return (
            AdmissionWaitState.EXPIRED
            if state is ExpiryState.EXPIRED
            else AdmissionWaitState.REFUSED
        )
    if expected_wait_id is None:
        await connection.execute(
            insert(wait).values(
                id=context.identifiers.new(),
                node_instance_id=binding.node_id,
                attempt_id=None,
                kind="admission",
                wake_at=wake_at,
                reason=reason,
                status="open",
                created_at=now,
                updated_at=now,
            )
        )
        await connection.execute(
            update(node_instance)
            .where(node_instance.c.id == binding.node_id)
            .values(status="waiting", updated_at=now)
        )
        await context.event(
            "node.waiting",
            {"node_path": binding.node_path, "reason": reason["kind"]},
            now,
            binding.node_id,
        )
    else:
        await connection.execute(
            update(wait)
            .where(wait.c.id == expected_wait_id)
            .values(wake_at=wake_at, reason=reason, updated_at=now)
        )
    await context.mark(now)
    return (
        AdmissionWaitState.CREATED
        if expected_wait_id is None
        else AdmissionWaitState.REFRESHED
    )


async def save_admission_wait(
    context: ExpiryContext,
    binding: ClaimSourceBinding,
    limits: ClaimSourceReadLimits,
    runtime: str,
    observation: CapacityLimit | RunRecovery,
    *,
    interval: timedelta,
    expected_wait_id: str | None = None,
) -> AdmissionWaitState:
    try:
        result = await _save(
            context, binding, limits, runtime, observation, interval, expected_wait_id
        )
    except asyncio.CancelledError as error:
        clear_snapshot_error(error)
        raise
    except (
        SQLAlchemyError,
        ValueError,
        TypeError,
        RecursionError,
        OverflowError,
        ExpiryFailed,
    ):
        del context, binding, limits, runtime, observation
        raise_private_metadata_error(AdmissionWaitFailed())
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(PREPARED, outcome=result.value)
    return result
