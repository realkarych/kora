import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql.elements import ColumnElement

from kora.db.admission_waits import open_wait_matches
from kora.db.claim_source_values import PreparedClaimSources
from kora.db.claim_sources import (
    ClaimSourceBinding,
    ClaimSourceReadLimits,
    verify_claim_source_binding,
)
from kora.db.connection import clear_snapshot_error
from kora.db.dependency_reads import read_dependency_node
from kora.db.native_values import native_value_bound
from kora.db.tables import attempt, namespace, node_instance, price_table, wait
from kora.db.transactions import raise_private_metadata_error
from kora.db.types import POSTGRESQL, normalise_timestamp
from kora.engine.admission_wait import (
    AdmissionWaitFailed,
    AdmissionWaitState,
    save_admission_wait,
)
from kora.engine.attempt_capacity import PreparedGraphCaps, check_attempt_capacity
from kora.engine.claim_sources import insert_claim_attempt
from kora.engine.dependency_rules import (
    FAILURES,
    DependencyOutcome,
    decide_dependencies,
)
from kora.engine.expiry import observe_run_expiry
from kora.engine.expiry_context import ExpiryContext, ExpiryFailed, ExpiryState
from kora.engine.graph_version_identity import GraphVersionPin
from kora.engine.locks import COMPONENT
from kora.engine.readiness import ReadinessNode
from kora.engine.run_promotion_read import read_promotion_status
from kora.engine.writer_mode import writable_postgres_transaction
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

PREPARED: EventSpec = CATALOG.register(
    EventSpec(
        "engine.attempt_admission.prepared",
        COMPONENT,
        Level.DEBUG,
        "attempt admission prepared for commit",
        {"outcome": FieldKind.STRING},
    )
)


@dataclass(frozen=True, slots=True, repr=False)
class PreparedAttemptAdmission:
    binding: ClaimSourceBinding
    pin: GraphVersionPin
    kind: str
    trigger_rule: str
    runtime: str
    namespace_revision: int
    sources: PreparedClaimSources
    predecessors: tuple[ReadinessNode, ...]
    absent_producers: tuple[str, ...]


class AttemptAdmissionState(StrEnum):
    QUEUED = "queued"
    WAITING = "waiting"
    EXPIRED = "expired"
    REFUSED = "refused"


class AttemptAdmissionFailed(RuntimeError):
    def __init__(self) -> None:
        super().__init__("attempt admission failed")


def _latest_price(namespace_id: str | None, at: datetime) -> ColumnElement[str]:
    return (
        select(price_table.c.id)
        .where(
            price_table.c.namespace_id == namespace_id,
            text("price_table.version = 1"),
            price_table.c.effective_from <= at,
        )
        .order_by(price_table.c.effective_from.desc())
        .limit(1)
        .scalar_subquery()
    )


def _initial_values(
    prepared: PreparedAttemptAdmission, now: datetime
) -> dict[str, object]:
    binding = prepared.binding
    return {
        "id": binding.attempt_id,
        "node_instance_id": binding.node_id,
        "number": 1,
        "runtime": prepared.runtime,
        "execution_state": "not_started",
        "recovery_version": 0,
        "recovery_required": False,
        "status": "created",
        "desired_state": "run",
        "record_version": 1,
        "infrastructure_retries": 0,
        "lease_epoch": 0,
        "counters": {},
        "scratch": {},
        "price_table_id": binding.price_table_id,
        "created_at": now,
        "updated_at": now,
    }


async def _eligible(
    context: ExpiryContext,
    prepared: PreparedAttemptAdmission,
    limits: ClaimSourceReadLimits,
    expected_wait_id: str | None,
) -> str | None:
    connection, binding = context.connection, prepared.binding
    parent = binding.run
    context.locks.require_connection(connection)
    context.require_capacity(prepared.runtime)
    if (
        (parent.namespace_id, parent.graph_id, parent.version_id, parent.run_id)
        != (
            context.scope.namespace_id,
            context.scope.graph_id,
            context.scope.version_id,
            context.run_id,
        )
        or prepared.kind not in ("agent", "command")
        or (
            connection.dialect.name == POSTGRESQL
            and not await writable_postgres_transaction(connection)
        )
        or not await verify_claim_source_binding(
            connection, binding, limits, include_attempt=False
        )
        or await read_promotion_status(
            connection, context.scope, prepared.pin, context.run_id, context.limits
        )
        != "running"
        or await connection.scalar(
            select(namespace.c.policy_revision).where(
                namespace.c.id == parent.namespace_id
            )
        )
        != prepared.namespace_revision
    ):
        return None
    states = ("pending", "ready") if expected_wait_id is None else ("waiting",)
    state: object = await connection.scalar(
        select(node_instance.c.status).where(
            node_instance.c.id == binding.node_id,
            node_instance.c.status.in_(states),
            node_instance.c.kind == prepared.kind,
            node_instance.c.trigger_rule == prepared.trigger_rule,
            node_instance.c.top_level.is_(True),
            node_instance.c.subgraph_attempt == 1,
            node_instance.c.attempt_count == 0,
            node_instance.c.current_attempt_id.is_(None),
            node_instance.c.started_at.is_(None),
            node_instance.c.deadline_expired_at.is_(None),
            node_instance.c.consumed_versions == {},
        )
    )
    return (
        state
        if isinstance(state, str)
        and await open_wait_matches(connection, binding.node_id, expected_wait_id)
        and await _dependencies_match(context, prepared)
        else None
    )


async def _dependencies_match(
    context: ExpiryContext, prepared: PreparedAttemptAdmission
) -> bool:
    states: list[str] = []
    for predecessor in prepared.predecessors:
        saved = await read_dependency_node(
            context.connection,
            context.run_id,
            predecessor.path,
            predecessor.kind,
            predecessor.trigger_rule,
        )
        if saved is None or (
            predecessor.path in prepared.absent_producers
            and saved.status not in ("skipped", *FAILURES)
        ):
            return False
        states.append(saved.status)
    return (
        decide_dependencies(prepared.trigger_rule, tuple(states))
        is DependencyOutcome.SATISFIED
    )


async def _admit(
    context: ExpiryContext,
    prepared: PreparedAttemptAdmission,
    limits: ClaimSourceReadLimits,
    caps: PreparedGraphCaps,
    global_limit: int | None,
    runtime_limit: int | None,
    interval: timedelta,
    expected_wait_id: str | None,
) -> AttemptAdmissionState:
    state = await _eligible(context, prepared, limits, expected_wait_id)
    if state is None or interval <= timedelta(0):
        return AttemptAdmissionState.REFUSED
    observation = await check_attempt_capacity(
        context,
        prepared.runtime,
        caps,
        global_limit=global_limit,
        runtime_limit=runtime_limit,
    )
    now = normalise_timestamp(context.clock())
    expiry = await observe_run_expiry(context, observed_at=now)
    if expiry is not ExpiryState.ACTIVE:
        return AttemptAdmissionState(expiry.value)
    binding, connection = prepared.binding, context.connection
    if observation is not None:
        if state == "pending":
            await connection.execute(
                update(node_instance)
                .where(node_instance.c.id == binding.node_id)
                .values(status="ready", updated_at=now)
            )
            await context.event(
                "node.ready",
                {"node_path": binding.node_path, "reason": None},
                now,
                binding.node_id,
            )
        waiting = await save_admission_wait(
            context,
            binding,
            limits,
            prepared.runtime,
            observation,
            interval=interval,
            expected_wait_id=expected_wait_id,
        )
        if waiting is AdmissionWaitState.REFUSED:
            raise AdmissionWaitFailed()
        return (
            AttemptAdmissionState.EXPIRED
            if waiting is AdmissionWaitState.EXPIRED
            else AttemptAdmissionState.WAITING
        )
    price = await connection.scalar(
        select(
            func.coalesce(
                _latest_price(context.scope.namespace_id, now), _latest_price(None, now)
            )
        )
    )
    values = _initial_values(prepared, now)
    record_bytes = (
        native_value_bound(values)
        + 128 * len(attempt.c)
        + sum(field.native_bytes for field in prepared.sources.fields)
    )
    if price != binding.price_table_id or record_bytes > min(
        limits.attempt_row, context.attempt_bytes
    ):
        return AttemptAdmissionState.REFUSED
    await insert_claim_attempt(
        connection, context.locks, binding, values, prepared.sources, limits, now
    )
    if expected_wait_id is not None:
        await connection.execute(
            update(wait)
            .where(wait.c.id == expected_wait_id)
            .values(status="satisfied", updated_at=now)
        )
    if state != "ready":
        await context.event(
            "node.ready",
            {"node_path": binding.node_path, "reason": None},
            now,
            binding.node_id,
        )
    await connection.execute(
        update(node_instance)
        .where(node_instance.c.id == binding.node_id)
        .values(
            status="queued",
            current_attempt_id=binding.attempt_id,
            attempt_count=1,
            updated_at=now,
        )
    )
    await context.event(
        "node.queued",
        {"node_path": binding.node_path, "reason": None},
        now,
        binding.node_id,
        binding.attempt_id,
    )
    await context.mark(now)
    if not await verify_claim_source_binding(connection, binding, limits):
        raise ValueError("the admitted attempt exceeds its final row allowance")
    return AttemptAdmissionState.QUEUED


async def admit_attempt(
    context: ExpiryContext,
    prepared: PreparedAttemptAdmission,
    limits: ClaimSourceReadLimits,
    caps: PreparedGraphCaps,
    *,
    global_limit: int | None,
    runtime_limit: int | None,
    interval: timedelta,
    expected_wait_id: str | None = None,
) -> AttemptAdmissionState:
    try:
        result = await _admit(
            context,
            prepared,
            limits,
            caps,
            global_limit,
            runtime_limit,
            interval,
            expected_wait_id,
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
        AdmissionWaitFailed,
    ):
        del context, prepared, limits, caps
        raise_private_metadata_error(AttemptAdmissionFailed())
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(PREPARED, outcome=result.value)
    return result
