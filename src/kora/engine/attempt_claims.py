import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal, cast

from sqlalchemy import and_, insert, select, update
from sqlalchemy.exc import SQLAlchemyError

from kora.cel_eval.errors import raise_safe
from kora.db.claim_source_values import ClaimSourcesRejected, prepare_claim_sources
from kora.db.claim_sources import ClaimSourceReadLimits
from kora.db.claim_sources_v1 import SOURCE_FIELDS
from kora.db.connection import clear_snapshot_error
from kora.db.grant_history import HistoryReadLimits, HistoryScope, read_history_rows
from kora.db.tables import attempt, attempt_dispatcher, event, namespace
from kora.db.types import POSTGRESQL, normalise_timestamp
from kora.db.worker_claims import claim_row, worker_slot_available
from kora.engine.claim_context import (
    ClaimAccessRejected,
    ClaimQuery,
    ClaimTarget,
    claim_record,
    claim_target,
    claim_worker,
)
from kora.engine.claim_selection import ClaimDelivery, SelectedClaim
from kora.engine.claim_sources import recheck_claim_sources, update_claim_sources
from kora.engine.claim_timing import first_claim_deadline
from kora.engine.deadline_records import (
    RecordedDeadlineBinding,
    deadline_document,
    prepare_deadline_record,
)
from kora.engine.expiry import record_attempt_expiry
from kora.engine.expiry_context import ACTIVE_ATTEMPT_STATUSES, ExpiryContext
from kora.engine.grant_history import append_grant, save_history_batch
from kora.engine.lease_recovery import (
    LeaseState,
    closed_reporting_lease,
    recover_expired_lease,
)
from kora.engine.locks import COMPONENT
from kora.engine.writer_mode import writable_postgres_transaction
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics
from kora.protocols.authentication import Principal
from kora.protocols.boundary import BoundaryModel
from kora.protocols.history import GrantHistorySnapshot
from kora.protocols.identity import Count, GrantBinding, Identifier, Sha256
from kora.protocols.worker_claim_system import PROCESS_BYTES, ClaimSystem

PREPARED: EventSpec = CATALOG.register(
    EventSpec(
        "engine.attempt_claim.prepared",
        COMPONENT,
        Level.DEBUG,
        "selected attempt claim decision prepared",
        {"outcome": FieldKind.STRING},
    )
)


class ClaimOutcome(StrEnum):
    INELIGIBLE = "ineligible"
    REDIRECT = "redirect"
    EXPIRE = "expire"
    MAINTAINED = "maintained"
    GRANTED = "granted"
    REPLAYED = "replayed"


@dataclass(frozen=True, slots=True, repr=False)
class ClaimDecision:
    outcome: ClaimOutcome
    delivery: ClaimDelivery | None = None
    redirect: str | None = None


class ClaimReceipt(BoundaryModel):
    id: Identifier
    attempt_id: Identifier
    execution_domain_id: Identifier
    lease_epoch: Count
    mode: Literal["execute", "recover"]
    claim_digest: Sha256
    prefix_count: Count
    prefix_digest: Sha256
    closed_at: datetime | None


def _delivery(
    target: ClaimTarget,
    prepared: SelectedClaim,
    grant: GrantBinding,
    history: GrantHistorySnapshot,
    mode: Literal["execute", "recover"],
    until: datetime,
    binding: RecordedDeadlineBinding,
) -> ClaimDelivery:
    saved = target.attempt
    if saved.acknowledged_segment_offset is None:
        raise ClaimSourcesRejected()
    system = ClaimSystem(
        attempt_id=saved.id,
        run_id=target.run.id,
        namespace_id=target.run.namespace_id,
        node_path=target.node.node_path,
        execution_domain_id=grant.execution_domain_id,
        dispatcher_id=grant.dispatcher_id,
        mode=mode,
        lease_epoch=grant.lease_epoch,
        lease_until=until,
        deadline=prepare_deadline_record(binding.deadline),
        process=grant.process,
        current_grant=grant,
        grant_history=history,
        execution_request=prepared.request,
        handle=prepared.handle,
        acknowledged_segment_offset=saved.acknowledged_segment_offset,
        permitted_actions=("seal_request",)
        if mode == "execute" and history.count == 1
        else ("observe", "retire"),
    )
    snapshot = prepared.snapshot
    if (
        snapshot.material.immutable.members | snapshot.policy.members
    ) & ClaimSystem.model_fields.keys():
        raise ClaimSourcesRejected()
    return ClaimDelivery(snapshot.material.immutable, snapshot.policy, system)


async def _reconcile(
    context: ExpiryContext, target: ClaimTarget, maximum: int, now: datetime
) -> ClaimDecision:
    saved = target.attempt
    row = await claim_row(
        context.connection,
        attempt_dispatcher,
        and_(
            attempt_dispatcher.c.attempt_id == saved.id,
            attempt_dispatcher.c.lease_epoch == saved.lease_epoch,
        ),
        ("worker_id", "execution_domain_id", "closed_at"),
        maximum,
    )
    if row is None or (row["worker_id"], row["execution_domain_id"]) != (
        saved.worker_id,
        saved.execution_domain_id,
    ):
        raise ClaimSourcesRejected()
    await context.connection.execute(
        update(attempt)
        .where(attempt.c.id == saved.id)
        .values(
            current_dispatcher_open=row["closed_at"] is None,
            updated_at=now,
        )
    )
    return ClaimDecision(ClaimOutcome.MAINTAINED)


def _first(target: ClaimTarget, prepared: SelectedClaim) -> bool:
    saved = target.attempt
    return (
        saved.status == "created"
        and saved.execution_state == "not_started"
        and saved.lease_epoch == 0
        and saved.started_at is None
        and saved.execution_domain_id is None
        and saved.worker_id is None
        and saved.lease_owner is None
        and saved.lease_until is None
        and saved.deadline is None
        and saved.deadline_expired_at is None
        and not saved.recovery_required
        and prepared.handle is None
        and prepared.request is None
        and prepared.snapshot.sources.values["deadline_binding"] is None
        and prepared.snapshot.sources.values["execution_policy"] is None
    )


async def _grant(
    context: ExpiryContext,
    query: ClaimQuery,
    principal: Principal,
    target: ClaimTarget,
    prepared: SelectedClaim,
    scope: HistoryScope,
    grant: GrantBinding,
    mode: Literal["execute", "recover"],
    binding: RecordedDeadlineBinding,
    first: bool,
    now: datetime,
    limits: ClaimSourceReadLimits,
    history_limits: HistoryReadLimits,
) -> ClaimDecision:
    connection, saved = context.connection, target.attempt
    until = now + timedelta(seconds=120)
    if first:
        values = {
            "deadline_binding": deadline_document(
                prepare_deadline_record(binding.deadline)
            )
        }
        allowance = dict(zip(SOURCE_FIELDS, limits.fields, strict=True))[
            "deadline_binding"
        ]
        sources = prepare_claim_sources(values, {"deadline_binding": allowance})
        await update_claim_sources(
            connection,
            context.locks,
            target.binding(),
            sources,
            limits,
            prepared.snapshot.sources.revision,
            now,
        )
    else:
        await recover_expired_lease(context, saved.id)
    await connection.execute(
        update(attempt)
        .where(attempt.c.id == saved.id)
        .values(
            worker_id=query.worker_id,
            lease_owner=query.worker_id,
            lease_epoch=grant.lease_epoch,
            lease_until=until,
            execution_domain_id=query.execution_domain_id,
            execution_state="stopped"
            if saved.execution_state == "stopped"
            else "unknown",
            current_dispatcher_open=True,
            status="claimed" if first else saved.status,
            started_at=now if first else saved.started_at,
            deadline=None if binding.deadline is None else binding.deadline.at,
            updated_at=now,
        )
    )
    history = await append_grant(
        connection, context.locks, scope, grant, mode, now, history_limits
    )
    if history is None:
        raise ClaimSourcesRejected()
    delivery = _delivery(target, prepared, grant, history, mode, until, binding)
    await connection.execute(
        update(attempt_dispatcher)
        .where(attempt_dispatcher.c.id == grant.dispatcher_id)
        .values(
            claim_id=query.claim_id,
            claim_digest=query.digest(),
        )
    )
    if first:
        await connection.execute(
            insert(event).values(
                id=context.identifiers.new(),
                namespace_id=target.run.namespace_id,
                run_id=target.run.id,
                node_instance_id=target.node.id,
                attempt_id=saved.id,
                kind="attempt.claimed",
                actor={
                    "kind": "service",
                    "owner": principal.owner,
                    "token_id": principal.token_id,
                },
                payload={
                    "number": saved.number,
                    "worker_id": query.worker_id,
                    "error_class": None,
                },
                at=now,
                created_at=now,
                updated_at=now,
            )
        )
        if binding.deadline is not None and binding.deadline.at <= now:
            await record_attempt_expiry(context, saved.id, now)
    return ClaimDecision(ClaimOutcome.GRANTED, delivery)


async def _claim(
    context: ExpiryContext,
    attempt_id: str,
    query: ClaimQuery,
    principal: Principal,
    prepared: SelectedClaim | None,
    limits: ClaimSourceReadLimits,
    history_limits: HistoryReadLimits,
    worker_bytes: int,
    uncertainty: timedelta,
) -> ClaimDecision:
    connection = context.connection
    context.locks.require_connection(connection)
    context.locks.require(context.run_id)
    if (
        connection.dialect.name == POSTGRESQL
        and not await writable_postgres_transaction(connection)
    ):
        raise ClaimSourcesRejected()
    registered, incarnation = await claim_worker(
        connection, query, principal, worker_bytes, lock=True
    )
    receipt = await claim_record(
        connection,
        ClaimReceipt,
        attempt_dispatcher,
        and_(
            attempt_dispatcher.c.worker_id == query.worker_id,
            attempt_dispatcher.c.incarnation_id == query.incarnation_id,
            attempt_dispatcher.c.claim_id == query.claim_id,
        ),
        history_limits.dispatcher_row,
    )
    if receipt is not None:
        if receipt.claim_digest != query.digest():
            raise ClaimAccessRejected("conflict")
        if receipt.attempt_id != attempt_id:
            return ClaimDecision(ClaimOutcome.REDIRECT, redirect=receipt.attempt_id)
    target = await claim_target(
        connection,
        attempt_id,
        limits,
        lock=True,
        run_id=context.run_id,
        skip_locked=receipt is None,
    )
    if target is None:
        if receipt is not None:
            raise ClaimAccessRejected("stale")
        return ClaimDecision(ClaimOutcome.INELIGIBLE)
    saved, node, parent = target.attempt, target.node, target.run
    if (parent.id, parent.namespace_id, parent.graph_id, parent.graph_version_id) != (
        context.run_id,
        context.scope.namespace_id,
        context.scope.graph_id,
        context.scope.version_id,
    ) or parent.namespace_id not in {
        entry.namespace_id for entry in principal.namespaces
    }:
        if receipt is not None:
            raise ClaimAccessRejected("forbidden")
        return ClaimDecision(ClaimOutcome.INELIGIBLE)
    now = normalise_timestamp(context.clock())
    if (
        receipt is None
        and saved.worker_id == query.worker_id
        and saved.current_dispatcher_open is None
        and saved.lease_until is not None
        and saved.lease_until > now
    ):
        return await _reconcile(context, target, history_limits.dispatcher_row, now)
    if receipt is not None:
        if (
            (
                saved.worker_id,
                saved.lease_owner,
                saved.lease_epoch,
                saved.execution_domain_id,
            )
            != (
                query.worker_id,
                query.worker_id,
                receipt.lease_epoch,
                receipt.execution_domain_id,
            )
            or receipt.execution_domain_id != query.execution_domain_id
            or saved.lease_until is None
            or saved.lease_until <= now
        ):
            raise ClaimAccessRejected("stale")
    if prepared is None:
        return ClaimDecision(
            ClaimOutcome.INELIGIBLE if receipt is None else ClaimOutcome.REDIRECT,
            redirect=None if receipt is None else receipt.attempt_id,
        )
    if prepared.snapshot.sources.binding != target.binding():
        raise ClaimSourcesRejected()
    if not await recheck_claim_sources(
        connection, context.locks, prepared.snapshot.sources, limits
    ):
        raise ClaimSourcesRejected()
    if (
        prepared.snapshot.policy_revision is not None
        and await connection.scalar(
            select(namespace.c.policy_revision).where(
                namespace.c.id == parent.namespace_id
            )
        )
        != prepared.snapshot.policy_revision
    ):
        raise ClaimSourcesRejected()
    if receipt is not None:
        if not isinstance(prepared.deadline, RecordedDeadlineBinding):
            raise ClaimSourcesRejected()
        grant = GrantBinding(
            dispatcher_id=receipt.id,
            worker_id=query.worker_id,
            incarnation_id=query.incarnation_id,
            lease_epoch=receipt.lease_epoch,
            execution_domain_id=receipt.execution_domain_id,
            process=incarnation.process,
        )
        history = GrantHistorySnapshot(
            version=1,
            namespace_id=parent.namespace_id,
            attempt_id=saved.id,
            execution_domain_id=receipt.execution_domain_id,
            upper_epoch=receipt.lease_epoch,
            count=receipt.prefix_count,
            digest=receipt.prefix_digest,
        )
        return ClaimDecision(
            ClaimOutcome.REPLAYED,
            _delivery(
                target,
                prepared,
                grant,
                history,
                receipt.mode,
                cast(datetime, saved.lease_until),
                prepared.deadline,
            ),
        )
    if (
        saved.runtime != query.runtime
        or (saved.status not in ACTIVE_ATTEMPT_STATUSES and not saved.recovery_required)
        or (saved.lease_until is not None and saved.lease_until > now)
    ):
        return ClaimDecision(ClaimOutcome.INELIGIBLE)
    first = _first(target, prepared)
    current = node.current_attempt_id == saved.id and node.status in (
        "queued",
        "running",
    )
    eligible = (
        current
        and parent.status == "running"
        and saved.desired_state == "run"
        and saved.cancel_cause is None
        and all(
            value is None
            for value in (
                saved.deadline_expired_at,
                node.deadline_expired_at,
                parent.deadline_expired_at,
            )
        )
    )
    if (
        parent.status == "running"
        and parent.deadline_expired_at is None
        and parent.started_at is not None
        and isinstance(prepared.run_timeout, int)
        and (now - parent.started_at) // timedelta(microseconds=1)
        >= prepared.run_timeout
    ):
        return ClaimDecision(ClaimOutcome.EXPIRE)
    if first:
        if not eligible or node.status != "queued" or parent.started_at is None:
            return ClaimDecision(ClaimOutcome.INELIGIBLE)
        exists = await connection.scalar(
            select(
                select(attempt_dispatcher.c.id)
                .where(attempt_dispatcher.c.attempt_id == saved.id)
                .exists()
            )
        )
        if exists:
            raise ClaimSourcesRejected()
        binding = RecordedDeadlineBinding(
            first_claim_deadline(
                parent.id,
                saved.id,
                parent.started_at,
                now,
                prepared.run_timeout,
                prepared.attempt_timeout,
                uncertainty,
            )
        )
    else:
        if (
            saved.execution_domain_id != query.execution_domain_id
            or saved.started_at is None
            or saved.lease_epoch == 0
        ):
            return ClaimDecision(ClaimOutcome.INELIGIBLE)
        if not isinstance(prepared.deadline, RecordedDeadlineBinding):
            raise ClaimSourcesRejected()
        binding = prepared.deadline
        if binding.deadline is not None and binding.deadline.at <= now:
            if (
                saved.deadline_expired_at is None
                and parent.status == "running"
                and parent.deadline_expired_at is None
            ):
                return ClaimDecision(ClaimOutcome.EXPIRE)
            eligible = False
        state = LeaseState.model_validate(
            {name: getattr(saved, name) for name in LeaseState.model_fields}
        )
        if not saved.recovery_required and await closed_reporting_lease(context, state):
            return ClaimDecision(ClaimOutcome.INELIGIBLE)
    scope = HistoryScope(
        parent.namespace_id, parent.id, node.id, saved.id, query.execution_domain_id
    )
    if not first:
        if prepared.history is None or prepared.history.scope != scope:
            raise ClaimSourcesRejected()
        if prepared.history.grants:
            await save_history_batch(
                connection, context.locks, prepared.history, history_limits
            )
            return ClaimDecision(ClaimOutcome.MAINTAINED)
        latest = await read_history_rows(
            connection, scope, history_limits, PROCESS_BYTES, maximum=1, newest=True
        )
        if not latest or latest[0].prefix is None:
            raise ClaimSourcesRejected()
    if not await worker_slot_available(
        connection, query.worker_id, registered.capacity, now
    ):
        return ClaimDecision(ClaimOutcome.INELIGIBLE)
    grant = GrantBinding(
        dispatcher_id=context.identifiers.new(),
        worker_id=query.worker_id,
        incarnation_id=query.incarnation_id,
        lease_epoch=saved.lease_epoch + 1,
        execution_domain_id=query.execution_domain_id,
        process=incarnation.process,
    )
    mode = "execute" if eligible and saved.status != "finishing" else "recover"
    return await _grant(
        context,
        query,
        principal,
        target,
        prepared,
        scope,
        grant,
        mode,
        binding,
        first,
        now,
        limits,
        history_limits,
    )


async def claim_attempt(
    context: ExpiryContext,
    attempt_id: str,
    query: ClaimQuery,
    principal: Principal,
    prepared: SelectedClaim | None,
    limits: ClaimSourceReadLimits,
    history_limits: HistoryReadLimits,
    worker_bytes: int,
    uncertainty: timedelta,
) -> ClaimDecision:
    try:
        result = await _claim(
            context,
            attempt_id,
            query,
            principal,
            prepared,
            limits,
            history_limits,
            worker_bytes,
            uncertainty,
        )
    except asyncio.CancelledError as error:
        clear_snapshot_error(error)
        raise
    except ClaimAccessRejected:
        raise
    except (SQLAlchemyError, OSError, ValueError, TypeError, OverflowError):
        raise_safe(ClaimSourcesRejected())
    finally:
        del context, query, principal, prepared
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(PREPARED, outcome=result.outcome.value)
    return result
