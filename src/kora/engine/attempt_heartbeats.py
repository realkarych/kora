import asyncio
from datetime import timedelta
from typing import cast as typed_cast

from sqlalchemy import func, insert, literal, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql.elements import ColumnElement

from kora.cel_eval.errors import raise_safe
from kora.db.connection import clear_snapshot_error
from kora.db.grant_history import GrantHistoryRejected, HistoryReadLimits
from kora.db.tables import attempt, attempt_dispatcher, event, run, worker
from kora.db.types import POSTGRESQL, JsonObject
from kora.db.worker_heartbeats import (
    HEARTBEAT_VALUE_BYTES,
    heartbeat_dispatchers,
    reserve_heartbeat_row,
)
from kora.engine.expiry_context import ExpiryContext, ExpiryFailed, set_json_field
from kora.engine.heartbeat_context import (
    HeartbeatContext,
    admit_heartbeat,
    heartbeat_actions,
    heartbeat_context,
    heartbeat_expiry,
    require_heartbeat_lease,
)
from kora.engine.heartbeat_evidence import (
    HeartbeatEvidenceRejected,
    HeartbeatGrant,
    ObservationGrant,
    WorkerRecovery,
    accept_heartbeat_evidence,
)
from kora.engine.locks import COMPONENT
from kora.engine.usage_acceptance import UsageRejected, changed_coverage
from kora.engine.worker_history import HistoryAccessRejected
from kora.engine.writer_mode import writable_postgres_transaction
from kora.ir.json import parse_json_object
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics
from kora.protocols.authentication import Principal
from kora.protocols.worker_heartbeats import HeartbeatResponse, WorkerHeartbeat

PREPARED: EventSpec = CATALOG.register(
    EventSpec(
        "engine.heartbeat.prepared",
        COMPONENT,
        Level.DEBUG,
        "worker heartbeat prepared for commit",
        {"events": FieldKind.INTEGER},
    )
)


def heartbeat_observation(
    context: HeartbeatContext, body: WorkerHeartbeat, recovery: WorkerRecovery
) -> tuple[WorkerRecovery, str, bool]:
    before = recovery.last_observation
    after = body.observation
    state = context.attempt.execution_state
    if after is not None:
        recovery = WorkerRecovery(
            last_observation=after,
            observation_grant=ObservationGrant(
                worker_id=context.grant.worker_id,
                incarnation_id=context.grant.incarnation_id,
                dispatcher_id=context.grant.id,
                lease_epoch=context.grant.lease_epoch,
            ),
            normal_completion=recovery.normal_completion,
            termination=recovery.termination,
        )
        state = (
            "live" if after.state in ("queued", "starting", "running") else "unknown"
        )
    if body.counters is not None and not (
        body.counters.supervisor_alive and body.counters.candidate_alive
    ):
        state = "unknown"
    stopped = (
        recovery.normal_completion is not None
        or recovery.termination is not None
        or context.attempt.execution_state == "stopped"
    )
    if stopped:
        state = "stopped"
    changed = (not stopped and state != context.attempt.execution_state) or (
        after is not None
        and (
            before is None or (before.state, before.phase) != (after.state, after.phase)
        )
    )
    return recovery, state, changed


async def heartbeat_attempt(
    authority: ExpiryContext,
    attempt_id: str,
    body: WorkerHeartbeat,
    principal: Principal,
    limits: HistoryReadLimits,
    worker_bytes: int,
) -> HeartbeatResponse:
    connection = authority.connection
    context: HeartbeatContext | None = None
    try:
        authority.locks.require_connection(connection)
        authority.locks.require(authority.run_id)
        admit_heartbeat(limits, worker_bytes, authority)
        if (
            connection.dialect.name == POSTGRESQL
            and not await writable_postgres_transaction(connection)
        ):
            raise GrantHistoryRejected()
        context = await heartbeat_context(
            connection,
            attempt_id,
            body,
            principal,
            authority.clock,
            limits,
            worker_bytes,
            run_id=authority.run_id,
        )
        return await _apply_heartbeat(
            authority, context, body, principal, limits, worker_bytes
        )
    except asyncio.CancelledError as error:
        clear_snapshot_error(error)
        raise
    except (HeartbeatEvidenceRejected, HistoryAccessRejected, UsageRejected):
        raise
    except (
        SQLAlchemyError,
        OSError,
        ValueError,
        TypeError,
        OverflowError,
        ExpiryFailed,
    ):
        raise_safe(GrantHistoryRejected())
    finally:
        del authority, context, body, principal


async def _apply_heartbeat(
    authority: ExpiryContext,
    context: HeartbeatContext,
    body: WorkerHeartbeat,
    principal: Principal,
    limits: HistoryReadLimits,
    worker_bytes: int,
) -> HeartbeatResponse:
    connection = authority.connection
    attempt_id, run_id = context.attempt.id, context.run.id
    saved = context.attempt
    history = context.history()
    if body.publication is not None or (
        body.evidence is not None and body.evidence.publication is not None
    ):
        raise GrantHistoryRejected()
    if (
        body.grant_history_complete is not None
        and body.grant_history_complete != history
    ):
        raise HistoryAccessRejected("conflict")
    keys = (
        ()
        if body.evidence is None
        else tuple(
            (item.quiescence.dispatcher_id, item.quiescence.lease_generation)
            for item in body.evidence.dispatchers
        )
    )
    rows = await heartbeat_dispatchers(
        connection, attempt_id, keys, tuple(HeartbeatGrant.model_fields), limits
    )
    before = (
        WorkerRecovery()
        if saved.recovery is None or saved.recovery.worker is None
        else saved.recovery.worker
    )
    recovery, closures, runtime_changed = accept_heartbeat_evidence(
        before,
        context.grant,
        body.evidence,
        tuple(HeartbeatGrant.model_validate(row) for row in rows),
        history.count,
    )
    recovery, state, observation_changed = heartbeat_observation(
        context, body, recovery
    )
    values: dict[str, object] = {}
    if body.counters is not None:
        if changed_coverage(saved.usage_coverage, body.counters.usage_coverage):
            values["usage_coverage"] = body.counters.usage_coverage.model_dump(
                mode="json"
            )
        if body.counters != saved.counters:
            values["counters"] = body.counters.model_dump(mode="json")
    verified = (
        saved.verified_history_epoch,
        saved.verified_history_count,
        saved.verified_history_digest,
    ) == (history.upper_epoch, history.count, history.digest)
    if body.grant_history_complete is not None and not verified:
        values.update(
            verified_history_epoch=history.upper_epoch,
            verified_history_count=history.count,
            verified_history_digest=history.digest,
        )
        verified = True
    if recovery != before:
        encoded = literal(
            recovery.model_dump(mode="json"), type_=attempt.c.recovery.type
        )
        values["recovery"] = set_json_field(
            connection,
            attempt.c.recovery,
            "worker",
            typed_cast(
                ColumnElement[object],
                encoded
                if connection.dialect.name == POSTGRESQL
                else func.json_extract(encoded, "$"),
            ),
        )
    closed = (
        context.grant.closed_at is not None
        or saved.current_dispatcher_open is False
        or any(item.quiescence.dispatcher_id == context.grant.id for item in closures)
    )
    if closed and saved.current_dispatcher_open is not False:
        values["current_dispatcher_open"] = False
    now = authority.clock()
    require_heartbeat_lease(saved, body, principal, now)
    until = now + timedelta(seconds=120)
    if saved.lease_until != until or saved.last_heartbeat_at != now:
        values.update(lease_until=until, last_heartbeat_at=now)
    if state != saved.execution_state:
        values["execution_state"] = state
    revoked = (
        context.node.current_attempt_id != attempt_id
        or context.run.status != "running"
        or saved.desired_state != "run"
        or saved.cancel_cause is not None
    )
    expired = saved.deadline_expired_at is not None or (
        saved.deadline is not None and saved.deadline <= now
    )
    lost = body.observation is not None and (
        body.observation.state == "lost"
        or (
            body.observation.state == "exited"
            and body.observation.exit_code not in (None, 0)
        )
    )
    if not saved.recovery_required and (revoked or lost):
        values["recovery_required"] = True
    events: list[tuple[str, JsonObject]] = []
    runtime = None if body.evidence is None else body.evidence.runtime
    if runtime_changed and runtime is not None:
        events.append(
            ("attempt.retired", parse_json_object(runtime.model_dump(mode="json")))
        )
    events.extend(
        ("attempt.dispatcher_closed", parse_json_object(item.model_dump(mode="json")))
        for item in closures
    )
    if observation_changed:
        events.append(
            (
                "attempt.execution_unknown"
                if state == "unknown"
                else "attempt.execution_observed",
                {"execution_state": state},
            )
        )
    version = saved.recovery_version + len(events) + int("recovery_required" in values)
    if version >= 2**63:
        raise GrantHistoryRejected()
    if version != saved.recovery_version:
        values["recovery_version"] = version
    await reserve_heartbeat_row(
        connection,
        attempt,
        attempt_id,
        limits.attempt_row,
        tuple(values) + ("updated_at",),
        sum(
            2 * HEARTBEAT_VALUE_BYTES
            if name in ("recovery", "counters", "usage_coverage")
            else 128
            for name in (*values, "updated_at")
        ),
    )
    for closure in closures:
        identity = closure.quiescence.dispatcher_id
        await reserve_heartbeat_row(
            connection,
            attempt_dispatcher,
            identity,
            limits.dispatcher_row,
            ("evidence", "closed_at", "updated_at"),
            2 * HEARTBEAT_VALUE_BYTES + 128,
        )
        await connection.execute(
            update(attempt_dispatcher)
            .where(attempt_dispatcher.c.id == identity)
            .values(
                closed_at=closure.quiescence.closed_at,
                evidence=closure.model_dump(mode="json"),
                updated_at=now,
            )
        )
    if values:
        await connection.execute(
            update(attempt)
            .where(attempt.c.id == attempt_id)
            .values(**values, updated_at=now)
        )
    if now - context.worker.last_seen_at >= timedelta(seconds=15):
        await reserve_heartbeat_row(
            connection,
            worker,
            principal.owner,
            worker_bytes,
            ("last_seen_at", "updated_at"),
            128,
        )
        await connection.execute(
            update(worker)
            .where(worker.c.id == principal.owner)
            .values(last_seen_at=now, updated_at=now)
        )
    actor: JsonObject = {
        "kind": "service",
        "owner": principal.owner,
        "token_id": principal.token_id,
    }
    events = [
        (
            kind,
            {
                "version": saved.recovery_version + index,
                "domain": context.grant.execution_domain_id,
                "reason": "worker_heartbeat",
                "actor": actor,
                "evidence": payload,
            },
        )
        for index, (kind, payload) in enumerate(events, 1)
    ]
    if "recovery_required" in values:
        events.append(
            (
                "attempt.recovery_required",
                {
                    "number": saved.number,
                    "reasons": ["authority_revoked" if revoked else "execution_lost"],
                },
            )
        )
    for kind, payload in events:
        await connection.execute(
            insert(event).values(
                id=authority.identifiers.new(),
                namespace_id=context.run.namespace_id,
                run_id=run_id,
                node_instance_id=context.node.id,
                attempt_id=attempt_id,
                kind=kind,
                actor=actor,
                payload=payload,
                at=now,
                created_at=now,
                updated_at=now,
            )
        )
    if events or "recovery_required" in values:
        await connection.execute(
            update(run)
            .where(run.c.id == run_id)
            .values(needs_scheduling=True, updated_at=now)
        )
    while True:
        final_now = max(now, authority.clock())
        require_heartbeat_lease(saved, body, principal, final_now)
        refreshed = await heartbeat_expiry(authority, context, final_now, limits)
        if refreshed is context:
            break
        context = refreshed
    expired = expired or (saved.deadline is not None and saved.deadline <= final_now)
    revoked = revoked or context.attempt.desired_state != "run"
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(PREPARED, events=len(events))
    return HeartbeatResponse(
        desired_state="cancel" if revoked or expired else "run",
        lease_until=until,
        mode="recover"
        if revoked or expired or saved.status == "finishing"
        else context.grant.mode,
        permitted_actions=heartbeat_actions(
            context, final_now, closed, state == "stopped", verified
        ),
        grant_history_verified=history if verified else None,
        grant_history=None if verified else history,
    )
