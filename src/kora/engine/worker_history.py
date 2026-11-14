from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal, cast

from sqlalchemy import event, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.cel_eval.errors import raise_safe
from kora.db.claim_source_values import ClaimSourcesRejected, decode_claim_source
from kora.db.connection import (
    SNAPSHOT_NATIVE_ERRORS,
    Database,
    DatabaseSnapshotRejected,
    require_snapshot_time,
)
from kora.db.grant_history import (
    GrantHistoryRejected,
    HistoryReadLimits,
    HistoryScope,
    RawGrant,
    read_history_rows,
)
from kora.db.tables import node_instance, run, worker, worker_incarnation
from kora.db.types import SQLITE
from kora.db.worker_history import scoped_history_attempt
from kora.engine.claim_context import claim_record
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics
from kora.protocols.authentication import Principal
from kora.protocols.boundary import BoundaryModel
from kora.protocols.history import (
    GRANT_HISTORY_LABEL,
    MAX_ENVELOPE_BYTES,
    MAX_PAGE_ROWS,
    GrantHistoryPage,
    GrantHistoryPosition,
    GrantHistorySnapshot,
    chain_seed,
    chain_step,
    record_bytes,
)
from kora.protocols.identity import (
    Count,
    GrantBinding,
    Identifier,
    ProcessIdentity,
    encoded_size,
    object_size,
)
from kora.protocols.time import Clock
from kora.protocols.worker_claim_system import GRANT_BYTES, PROCESS_BYTES

COMPONENT: str = CATALOG.register_component("engine")
READ: EventSpec = CATALOG.register(
    EventSpec(
        "engine.worker_history.read",
        COMPONENT,
        Level.DEBUG,
        "worker grant history read completed",
        {"rows": FieldKind.INTEGER, "bytes": FieldKind.INTEGER},
    )
)


class HistoryAccessRejected(ValueError):
    def __init__(
        self, reason: Literal["not_found", "forbidden", "stale", "conflict"]
    ) -> None:
        super().__init__("worker grant history was refused")
        self.reason = reason


class HistoryAttempt(BoundaryModel):
    node_instance_id: Identifier
    runtime: Identifier
    worker_id: Identifier | None
    lease_owner: Identifier | None
    execution_domain_id: Identifier | None
    lease_epoch: Count
    lease_until: datetime | None


class HistoryNode(BoundaryModel):
    run_id: Identifier


class HistoryRun(BoundaryModel):
    namespace_id: Identifier


class HistoryWorker(BoundaryModel):
    token_id: Identifier
    runtime: Identifier


class HistoryIncarnation(BoundaryModel):
    execution_domain_id: Identifier


@dataclass(frozen=True, slots=True)
class HistoryRead:
    attempt_id: str
    dispatcher_id: str
    lease_epoch: int
    tip: GrantHistoryPosition
    start: GrantHistoryPosition


@dataclass(frozen=True, slots=True, repr=False)
class HistoryAcquisition:
    snapshot: GrantHistorySnapshot
    rows: tuple[RawGrant, ...]
    encoding: str
    lease_until: datetime


async def _authority(
    connection: AsyncConnection,
    request: HistoryRead,
    principal: Principal,
    clock: Clock,
    limits: HistoryReadLimits,
    worker_bytes: int,
) -> tuple[HistoryScope, datetime]:
    if principal.kind != "service":
        raise HistoryAccessRejected("forbidden")
    raw = await scoped_history_attempt(
        connection,
        request.attempt_id,
        tuple(grant.namespace_id for grant in principal.namespaces),
        limits,
    )
    if raw is None:
        raise HistoryAccessRejected("not_found")
    target = HistoryAttempt.model_validate(
        {name: raw[name] for name in HistoryAttempt.model_fields}
    )
    registered = await claim_record(
        connection, HistoryWorker, worker, worker.c.id == principal.owner, worker_bytes
    )
    if (
        target.worker_id != principal.owner
        or target.runtime != principal.runtime
        or registered is None
        or registered.token_id != principal.token_id
        or registered.runtime != principal.runtime
    ):
        raise HistoryAccessRejected("forbidden")
    until = target.lease_until
    if until is not None and until.utcoffset() is None:
        raise GrantHistoryRejected()
    if (
        target.lease_epoch != request.lease_epoch
        or target.lease_owner != principal.owner
        or until is None
        or until <= clock.now()
        or target.execution_domain_id is None
    ):
        raise HistoryAccessRejected("stale")
    node = await claim_record(
        connection,
        HistoryNode,
        node_instance,
        node_instance.c.id == target.node_instance_id,
        limits.node_row,
    )
    if node is None:
        raise GrantHistoryRejected()
    parent = await claim_record(
        connection, HistoryRun, run, run.c.id == node.run_id, limits.run_row
    )
    if parent is None or parent.namespace_id not in {
        grant.namespace_id for grant in principal.namespaces
    }:
        raise HistoryAccessRejected("not_found")
    scope = HistoryScope(
        parent.namespace_id,
        node.run_id,
        target.node_instance_id,
        request.attempt_id,
        target.execution_domain_id,
    )
    grants = await read_history_rows(
        connection,
        scope,
        limits,
        PROCESS_BYTES,
        maximum=1,
        exact=request.lease_epoch,
        dispatcher_id=request.dispatcher_id,
    )
    if (
        not grants
        or grants[0].worker_id != principal.owner
        or grants[0].execution_domain_id != scope.execution_domain_id
    ):
        raise HistoryAccessRejected("stale")
    incarnation = await claim_record(
        connection,
        HistoryIncarnation,
        worker_incarnation,
        (worker_incarnation.c.worker_id == principal.owner)
        & (worker_incarnation.c.incarnation_id == grants[0].incarnation_id),
        worker_bytes,
    )
    if (
        incarnation is None
        or incarnation.execution_domain_id != scope.execution_domain_id
    ):
        raise HistoryAccessRejected("stale")
    return scope, until


async def _prefix(
    connection: AsyncConnection,
    scope: HistoryScope,
    limits: HistoryReadLimits,
    position: GrantHistoryPosition,
    seed: str,
) -> None:
    if position.after_epoch is None:
        if position.received_count != 0 or position.prefix_digest != seed:
            raise HistoryAccessRejected("conflict")
        return
    rows = await read_history_rows(
        connection, scope, limits, PROCESS_BYTES, maximum=1, exact=position.after_epoch
    )
    if not rows or rows[0].prefix is None:
        raise HistoryAccessRejected("conflict")
    prefix = rows[0].prefix
    if (prefix.count, prefix.digest) != (
        position.received_count,
        position.prefix_digest,
    ):
        raise HistoryAccessRejected("conflict")


async def _acquire(
    database: Database,
    request: HistoryRead,
    principal: Principal,
    clock: Clock,
    limits: HistoryReadLimits,
    worker_bytes: int,
    deadline: float,
) -> HistoryAcquisition:
    def before_statement(connection: Connection, *arguments: object) -> None:
        require_snapshot_time(deadline)

    if type(worker_bytes) is not int or not 0 < worker_bytes < 2**63:
        raise GrantHistoryRejected()
    replace(
        limits,
        native_bytes=limits.native_bytes - 2 * worker_bytes,
        raw_bytes=limits.raw_bytes - 20 * 4096,
    ).admit(MAX_PAGE_ROWS + 4, GRANT_BYTES, PROCESS_BYTES)
    async with database.read_snapshot(deadline=deadline) as connection:
        event.listen(
            connection.sync_connection, "before_cursor_execute", before_statement
        )
        try:
            scope, until = await _authority(
                connection, request, principal, clock, limits, worker_bytes
            )
            tip = request.tip
            seed = chain_seed(
                GRANT_HISTORY_LABEL,
                scope.namespace_id,
                scope.attempt_id,
                scope.execution_domain_id,
            )
            await _prefix(connection, scope, limits, tip, seed)
            snapshot = GrantHistorySnapshot(
                version=1,
                namespace_id=scope.namespace_id,
                attempt_id=scope.attempt_id,
                execution_domain_id=scope.execution_domain_id,
                upper_epoch=tip.after_epoch,
                count=tip.received_count,
                digest=tip.prefix_digest,
            )
            if not snapshot.holds(request.start):
                raise HistoryAccessRejected("conflict")
            await _prefix(connection, scope, limits, request.start, snapshot.seed())
            rows = (
                ()
                if request.start == snapshot.tip()
                else await read_history_rows(
                    connection,
                    scope,
                    limits,
                    PROCESS_BYTES,
                    maximum=MAX_PAGE_ROWS + 1,
                    after=request.start.after_epoch,
                    upper=snapshot.upper_epoch,
                )
            )
            encoding = "UTF-8"
            if connection.dialect.name == SQLITE:
                value = await connection.scalar(text("PRAGMA encoding"))
                if value not in ("UTF-8", "UTF-16le", "UTF-16be"):
                    raise GrantHistoryRejected()
                encoding = cast(str, value)
            acquired = HistoryAcquisition(snapshot, rows, encoding, until)
        finally:
            event.remove(
                connection.sync_connection, "before_cursor_execute", before_statement
            )
    return acquired


def _page(
    acquired: HistoryAcquisition, start: GrantHistoryPosition, deadline: float
) -> tuple[bytes, int]:
    snapshot = acquired.snapshot
    end = start
    grants: list[GrantBinding] = []
    grants_bytes = 2
    for row in acquired.rows[:MAX_PAGE_ROWS]:
        require_snapshot_time(deadline)
        grant = GrantBinding(
            dispatcher_id=row.dispatcher_id,
            worker_id=row.worker_id,
            incarnation_id=row.incarnation_id,
            lease_epoch=row.epoch,
            execution_domain_id=row.execution_domain_id,
            process=None
            if row.process is None
            else ProcessIdentity.model_validate(
                decode_claim_source(row.process, acquired.encoding)
            ),
        )
        record = record_bytes(grant)
        candidate = GrantHistoryPosition(
            after_epoch=grant.lease_epoch,
            received_count=end.received_count + 1,
            prefix_digest=chain_step(end.prefix_digest, record),
        )
        if row.prefix is None or (row.prefix.count, row.prefix.digest) != (
            candidate.received_count,
            candidate.prefix_digest,
        ):
            raise GrantHistoryRejected()
        next_bytes = grants_bytes + len(record) + bool(grants)
        size = object_size(
            {
                "snapshot": encoded_size(snapshot),
                "start": encoded_size(start),
                "grants": next_bytes,
                "end": encoded_size(candidate),
                "complete": 4 if candidate == snapshot.tip() else 5,
            }
        )
        if size > MAX_ENVELOPE_BYTES:
            break
        grants.append(grant)
        grants_bytes = next_bytes
        end = candidate
    page = GrantHistoryPage(
        snapshot=snapshot,
        start=start,
        grants=tuple(grants),
        end=end,
        complete=end == snapshot.tip(),
    )
    encoded = page.model_dump_json().encode("utf-8")
    if len(encoded) > MAX_ENVELOPE_BYTES:
        raise GrantHistoryRejected()
    require_snapshot_time(deadline)
    return encoded, len(grants)


async def read_worker_history(
    database: Database,
    request: HistoryRead,
    principal: Principal,
    clock: Clock,
    limits: HistoryReadLimits,
    worker_bytes: int,
    *,
    deadline: float,
) -> bytes:
    try:
        acquired = await _acquire(
            database, request, principal, clock, limits, worker_bytes, deadline
        )
        encoded, rows = _page(acquired, request.start, deadline)
        if acquired.lease_until <= clock.now():
            raise HistoryAccessRejected("stale")
        diagnostics = current_diagnostics()
        if diagnostics is not None:
            diagnostics.logger(COMPONENT).emit(READ, rows=rows, bytes=len(encoded))
        return encoded
    except HistoryAccessRejected:
        raise
    except (
        ValueError,
        ClaimSourcesRejected,
        DatabaseSnapshotRejected,
        *SNAPSHOT_NATIVE_ERRORS,
    ):
        del request, principal
        raise_safe(GrantHistoryRejected())
