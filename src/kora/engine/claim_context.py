from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal, TypeVar

from pydantic import Field, StrictBool, StrictInt
from sqlalchemy import Table, and_, select
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import ColumnElement

from kora.db.claim_source_values import ClaimSourcesRejected
from kora.db.claim_sources import ClaimSourceBinding, ClaimSourceReadLimits
from kora.db.run_input_reads import RunInputBinding
from kora.db.tables import attempt, node_instance, run, worker, worker_incarnation
from kora.db.worker_claims import claim_row
from kora.engine.worker_registration import fingerprint
from kora.protocols.authentication import Principal
from kora.protocols.boundary import BoundaryModel
from kora.protocols.identity import Count, Identifier, ProcessIdentity

Record = TypeVar("Record", bound=BoundaryModel)


async def claim_record(
    connection: AsyncConnection,
    model: type[Record],
    owner: Table,
    predicate: ColumnElement[bool],
    maximum: int,
    *,
    lock: bool = False,
    skip_locked: bool = False,
) -> Record | None:
    row = await claim_row(
        connection,
        owner,
        predicate,
        tuple(model.model_fields),
        maximum,
        lock=lock,
        skip_locked=skip_locked,
    )
    return (
        None
        if row is None
        else model.model_validate({name: row[name] for name in model.model_fields})
    )


class ClaimAccessRejected(ValueError):
    def __init__(self, reason: Literal["forbidden", "conflict", "stale"]) -> None:
        self.reason = reason
        super().__init__("worker Claim authority was refused")


class ClaimQuery(BoundaryModel):
    worker_id: Identifier
    runtime: Identifier
    incarnation_id: Identifier
    execution_domain_id: Identifier
    claim_id: Identifier

    def digest(self) -> str:
        return fingerprint({**self.model_dump(), "max": 1})


class ClaimWorker(BoundaryModel):
    token_id: Identifier
    runtime: Identifier
    capacity: Annotated[StrictInt, Field(ge=1, le=2**31 - 1)]
    last_seen_at: datetime


class ClaimIncarnation(BoundaryModel):
    execution_domain_id: Identifier
    process: ProcessIdentity


class ClaimAttempt(BoundaryModel):
    id: Identifier
    number: Count
    node_instance_id: Identifier
    price_table_id: Identifier | None
    runtime: Identifier
    worker_id: Identifier | None
    execution_domain_id: Identifier | None
    execution_state: Literal["not_started", "unknown", "live", "stopped"]
    status: Identifier
    desired_state: Literal["run", "cancel"]
    cancel_cause: Identifier | None
    recovery_required: StrictBool
    recovery_version: Count
    lease_owner: Identifier | None
    lease_epoch: Count
    lease_until: datetime | None
    current_dispatcher_open: StrictBool | None
    started_at: datetime | None
    deadline: datetime | None
    deadline_expired_at: datetime | None
    acknowledged_segment_offset: Count | None


class ClaimNode(BoundaryModel):
    id: Identifier
    run_id: Identifier
    node_path: Identifier
    status: Identifier
    current_attempt_id: Identifier | None
    top_level: StrictBool
    deadline_expired_at: datetime | None


class ClaimRun(BoundaryModel):
    id: Identifier
    namespace_id: Identifier
    graph_id: Identifier
    graph_version_id: Identifier
    status: Identifier
    started_at: datetime | None
    deadline_expired_at: datetime | None


@dataclass(frozen=True, slots=True)
class ClaimTarget:
    attempt: ClaimAttempt
    node: ClaimNode
    run: ClaimRun

    def binding(self) -> ClaimSourceBinding:
        if self.attempt.price_table_id is None or not self.node.top_level:
            raise ClaimSourcesRejected()
        return ClaimSourceBinding(
            RunInputBinding(
                self.run.namespace_id,
                self.run.graph_id,
                self.run.graph_version_id,
                self.run.id,
            ),
            self.node.id,
            self.attempt.id,
            self.attempt.price_table_id,
            self.node.node_path,
        )


async def claim_target(
    connection: AsyncConnection,
    attempt_id: str,
    limits: ClaimSourceReadLimits,
    *,
    lock: bool = False,
    run_id: str | None = None,
    skip_locked: bool = False,
) -> ClaimTarget | None:
    predicate = attempt.c.id == attempt_id
    if lock and run_id is None:
        raise ClaimSourcesRejected()
    if run_id is not None:
        predicate &= (
            select(node_instance.c.id)
            .where(
                node_instance.c.id == attempt.c.node_instance_id,
                node_instance.c.run_id == run_id,
            )
            .exists()
        )
    physical = await claim_record(
        connection,
        ClaimAttempt,
        attempt,
        predicate,
        limits.attempt_row,
        lock=lock,
        skip_locked=skip_locked,
    )
    if physical is None:
        return None
    node = await claim_record(
        connection,
        ClaimNode,
        node_instance,
        node_instance.c.id == physical.node_instance_id,
        limits.node_row,
    )
    if node is None:
        return None
    parent = await claim_record(
        connection,
        ClaimRun,
        run,
        run.c.id == node.run_id,
        limits.run_row,
    )
    return None if parent is None else ClaimTarget(physical, node, parent)


async def claim_worker(
    connection: AsyncConnection,
    query: ClaimQuery,
    principal: Principal,
    maximum: int,
    *,
    lock: bool = False,
) -> tuple[ClaimWorker, ClaimIncarnation]:
    if principal.kind != "service" or (principal.owner, principal.runtime) != (
        query.worker_id,
        query.runtime,
    ):
        raise ClaimAccessRejected("forbidden")
    registered = await claim_record(
        connection,
        ClaimWorker,
        worker,
        worker.c.id == query.worker_id,
        maximum,
        lock=lock,
    )
    if registered is None:
        raise ClaimAccessRejected("forbidden")
    if (registered.token_id, registered.runtime) != (principal.token_id, query.runtime):
        raise ClaimAccessRejected("forbidden")
    incarnation = await claim_record(
        connection,
        ClaimIncarnation,
        worker_incarnation,
        and_(
            worker_incarnation.c.worker_id == query.worker_id,
            worker_incarnation.c.incarnation_id == query.incarnation_id,
        ),
        maximum,
    )
    if (
        incarnation is None
        or incarnation.execution_domain_id != query.execution_domain_id
    ):
        raise ClaimAccessRejected("forbidden")
    return registered, incarnation
