from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import exists, func, select, true
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.tables import (
    FINISHED_NODE_STATUSES,
    NON_TERMINAL_ATTEMPT_STATUSES,
    OUTSTANDING_RUN_STATUSES,
    api_token,
    attempt,
    event,
    graph,
    graph_version,
    namespace,
    node_instance,
    run,
    worker,
)
from kora.db.types import JsonValue

DEFAULT_PAGE_SIZE: int = 100
MAX_PAGE_SIZE: int = 1000


class PageSizeError(ValueError):
    pass


def bounded_page(size: int) -> int:
    if size < 1 or size > MAX_PAGE_SIZE:
        raise PageSizeError("a page holds between one and one thousand rows")
    return size


@dataclass(frozen=True)
class NamespaceRow:
    id: str
    name: str
    policy_revision: int
    policy: JsonValue
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class TokenRow:
    id: str
    kind: str
    digest: str
    owner: str
    namespaces: JsonValue
    approve: bool
    platform: bool
    runtime: str | None
    expires_at: datetime | None


@dataclass(frozen=True)
class WorkerRow:
    id: str
    incarnation_id: str
    execution_domain_id: str
    runtime: str
    version: str
    capacity: int
    last_seen_at: datetime
    token_id: str


@dataclass(frozen=True)
class GraphRow:
    id: str
    namespace_id: str
    name: str


@dataclass(frozen=True)
class GraphVersionRow:
    id: str
    graph_id: str
    number: int
    ir: JsonValue
    ir_digest: str
    source_digest: str
    execution_contract: JsonValue
    execution_contract_digest: str
    deployment_digest: str
    resolved_digests: JsonValue
    resources: JsonValue
    deployed_by: str
    deployed_at: datetime
    status: str


@dataclass(frozen=True)
class RunRow:
    id: str
    namespace_id: str
    graph_id: str
    graph_version_id: str
    rerun_of_run_id: str | None
    idempotency_key: str
    target_key: str | None
    target_held_since: datetime | None
    params: JsonValue
    cel_params_projection: JsonValue
    allow_unenforced_secrets: bool
    status: str
    needs_scheduling: bool
    trigger: JsonValue
    limits: JsonValue
    retention: JsonValue
    started_at: datetime | None
    deadline_expired_at: datetime | None
    finished_at: datetime | None


@dataclass(frozen=True)
class NodeInstanceRow:
    id: str
    run_id: str
    node_path: str
    kind: str
    status: str
    trigger_rule: str
    subgraph_attempt: int
    top_level: bool
    consumed_versions: JsonValue
    current_attempt_id: str | None
    branch_selection: JsonValue
    attempt_count: int
    started_at: datetime | None
    deadline_expired_at: datetime | None
    provider_wait_deadline: datetime | None


@dataclass(frozen=True)
class EventRow:
    id: str
    namespace_id: str
    run_id: str | None
    node_instance_id: str | None
    attempt_id: str | None
    kind: str
    actor: JsonValue
    payload: JsonValue
    at: datetime


@dataclass(frozen=True)
class TargetHolder:
    run_id: str
    target_held_since: datetime


@dataclass(frozen=True)
class SweepPage:
    run_ids: Sequence[str]
    cursor: str | None
    ceiling: str | None
    wrapped: bool


NAMESPACE_COLUMNS = (
    namespace.c.id,
    namespace.c.name,
    namespace.c.policy_revision,
    namespace.c.policy,
    namespace.c.created_at,
    namespace.c.updated_at,
)
TOKEN_COLUMNS = (
    api_token.c.id,
    api_token.c.kind,
    api_token.c.digest,
    api_token.c.owner,
    api_token.c.namespaces,
    api_token.c.approve,
    api_token.c.platform,
    api_token.c.runtime,
    api_token.c.expires_at,
)
WORKER_COLUMNS = (
    worker.c.id,
    worker.c.incarnation_id,
    worker.c.execution_domain_id,
    worker.c.runtime,
    worker.c.version,
    worker.c.capacity,
    worker.c.last_seen_at,
    worker.c.token_id,
)
GRAPH_COLUMNS = (graph.c.id, graph.c.namespace_id, graph.c.name)
GRAPH_VERSION_COLUMNS = (
    graph_version.c.id,
    graph_version.c.graph_id,
    graph_version.c.number,
    graph_version.c.ir,
    graph_version.c.ir_digest,
    graph_version.c.source_digest,
    graph_version.c.execution_contract,
    graph_version.c.execution_contract_digest,
    graph_version.c.deployment_digest,
    graph_version.c.resolved_digests,
    graph_version.c.resources,
    graph_version.c.deployed_by,
    graph_version.c.deployed_at,
    graph_version.c.status,
)
RUN_COLUMNS = (
    run.c.id,
    run.c.namespace_id,
    run.c.graph_id,
    run.c.graph_version_id,
    run.c.rerun_of_run_id,
    run.c.idempotency_key,
    run.c.target_key,
    run.c.target_held_since,
    run.c.params,
    run.c.cel_params_projection,
    run.c.allow_unenforced_secrets,
    run.c.status,
    run.c.needs_scheduling,
    run.c.trigger,
    run.c.limits,
    run.c.retention,
    run.c.started_at,
    run.c.deadline_expired_at,
    run.c.finished_at,
)
NODE_INSTANCE_COLUMNS = (
    node_instance.c.id,
    node_instance.c.run_id,
    node_instance.c.node_path,
    node_instance.c.kind,
    node_instance.c.status,
    node_instance.c.trigger_rule,
    node_instance.c.subgraph_attempt,
    node_instance.c.top_level,
    node_instance.c.consumed_versions,
    node_instance.c.current_attempt_id,
    node_instance.c.branch_selection,
    node_instance.c.attempt_count,
    node_instance.c.started_at,
    node_instance.c.deadline_expired_at,
    node_instance.c.provider_wait_deadline,
)
EVENT_COLUMNS = (
    event.c.id,
    event.c.namespace_id,
    event.c.run_id,
    event.c.node_instance_id,
    event.c.attempt_id,
    event.c.kind,
    event.c.actor,
    event.c.payload,
    event.c.at,
)


async def namespace_by_name(
    connection: AsyncConnection, name: str
) -> NamespaceRow | None:
    statement = select(*NAMESPACE_COLUMNS).where(namespace.c.name == name)
    row = (await connection.execute(statement)).first()
    return None if row is None else NamespaceRow(*row)


async def namespace_by_id(
    connection: AsyncConnection, namespace_id: str
) -> NamespaceRow | None:
    statement = select(*NAMESPACE_COLUMNS).where(namespace.c.id == namespace_id)
    row = (await connection.execute(statement)).first()
    return None if row is None else NamespaceRow(*row)


async def token_by_digest(connection: AsyncConnection, digest: str) -> TokenRow | None:
    statement = select(*TOKEN_COLUMNS).where(api_token.c.digest == digest)
    row = (await connection.execute(statement)).first()
    return None if row is None else TokenRow(*row)


async def worker_by_id(connection: AsyncConnection, worker_id: str) -> WorkerRow | None:
    statement = select(*WORKER_COLUMNS).where(worker.c.id == worker_id)
    row = (await connection.execute(statement)).first()
    return None if row is None else WorkerRow(*row)


async def graph_by_name(
    connection: AsyncConnection, namespace_id: str, name: str
) -> GraphRow | None:
    statement = select(*GRAPH_COLUMNS).where(
        graph.c.namespace_id == namespace_id, graph.c.name == name
    )
    row = (await connection.execute(statement)).first()
    return None if row is None else GraphRow(*row)


async def graph_version_by_number(
    connection: AsyncConnection, namespace_id: str, graph_id: str, number: int
) -> GraphVersionRow | None:
    statement = (
        select(*GRAPH_VERSION_COLUMNS)
        .join(graph, graph.c.id == graph_version.c.graph_id)
        .where(
            graph.c.namespace_id == namespace_id,
            graph_version.c.graph_id == graph_id,
            graph_version.c.number == number,
        )
    )
    row = (await connection.execute(statement)).first()
    return None if row is None else GraphVersionRow(*row)


async def run_by_id(
    connection: AsyncConnection, namespace_id: str, run_id: str
) -> RunRow | None:
    statement = select(*RUN_COLUMNS).where(
        run.c.namespace_id == namespace_id, run.c.id == run_id
    )
    row = (await connection.execute(statement)).first()
    return None if row is None else RunRow(*row)


async def node_instances_of_run(
    connection: AsyncConnection, namespace_id: str, run_id: str
) -> list[NodeInstanceRow]:
    statement = (
        select(*NODE_INSTANCE_COLUMNS)
        .join(run, run.c.id == node_instance.c.run_id)
        .where(run.c.namespace_id == namespace_id, node_instance.c.run_id == run_id)
        .order_by(node_instance.c.node_path)
    )
    rows = (await connection.execute(statement)).all()
    return [NodeInstanceRow(*row) for row in rows]


async def events_of_run(
    connection: AsyncConnection,
    namespace_id: str,
    run_id: str,
    after_id: str | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> list[EventRow]:
    statement = select(*EVENT_COLUMNS).where(
        event.c.namespace_id == namespace_id, event.c.run_id == run_id
    )
    if after_id is not None:
        statement = statement.where(event.c.id > after_id)
    statement = statement.order_by(event.c.id).limit(bounded_page(page_size))
    rows = (await connection.execute(statement)).all()
    return [EventRow(*row) for row in rows]


async def has_unfinished_top_level_instance(
    connection: AsyncConnection, run_id: str
) -> bool:
    unfinished = exists().where(
        node_instance.c.run_id == run_id,
        node_instance.c.top_level.is_(True),
        node_instance.c.status.not_in(FINISHED_NODE_STATUSES),
    )
    return bool((await connection.execute(select(unfinished))).scalar())


async def has_physical_attempt(connection: AsyncConnection, run_id: str) -> bool:
    physical = exists().where(
        node_instance.c.run_id == run_id,
        attempt.c.node_instance_id == node_instance.c.id,
        attempt.c.status.in_(NON_TERMINAL_ATTEMPT_STATUSES),
    )
    return bool((await connection.execute(select(physical))).scalar())


async def execution_run_usage(connection: AsyncConnection, graph_id: str) -> int:
    physical = exists().where(
        node_instance.c.run_id == run.c.id,
        attempt.c.node_instance_id == node_instance.c.id,
        attempt.c.status.in_(NON_TERMINAL_ATTEMPT_STATUSES),
    )
    statement = select(func.count(run.c.id)).where(run.c.graph_id == graph_id, physical)
    return int((await connection.execute(statement)).scalar_one())


async def namespace_outstanding_usage(
    connection: AsyncConnection, namespace_id: str
) -> int:
    physical = exists().where(
        node_instance.c.run_id == run.c.id,
        attempt.c.node_instance_id == node_instance.c.id,
        attempt.c.status.in_(NON_TERMINAL_ATTEMPT_STATUSES),
    )
    statement = select(func.count(run.c.id)).where(
        run.c.namespace_id == namespace_id,
        run.c.status.in_(OUTSTANDING_RUN_STATUSES)
        | physical
        | run.c.target_held_since.is_not(None),
    )
    return int((await connection.execute(statement)).scalar_one())


async def target_holders(
    connection: AsyncConnection, namespace_id: str, target_key: str
) -> list[TargetHolder]:
    statement = (
        select(run.c.id, run.c.target_held_since)
        .where(
            run.c.namespace_id == namespace_id,
            run.c.target_key == target_key,
            run.c.target_held_since.is_not(None),
        )
        .order_by(run.c.target_held_since, run.c.id)
    )
    rows = (await connection.execute(statement)).all()
    return [TargetHolder(*row) for row in rows]


async def highest_run_id(connection: AsyncConnection) -> str | None:
    value = (await connection.execute(select(func.max(run.c.id)))).scalar()
    return None if value is None else str(value)


async def marked_run_page(
    connection: AsyncConnection,
    after_id: str | None,
    ceiling: str,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> list[str]:
    statement = select(run.c.id).where(
        run.c.needs_scheduling == true(), run.c.id <= ceiling
    )
    if after_id is not None:
        statement = statement.where(run.c.id > after_id)
    statement = statement.order_by(run.c.id).limit(bounded_page(page_size))
    rows = (await connection.execute(statement)).all()
    return [str(row[0]) for row in rows]


class MarkedRunSweep:
    def __init__(self, page_size: int = DEFAULT_PAGE_SIZE) -> None:
        self.page_size = bounded_page(page_size)
        self._cursor: str | None = None
        self._ceiling: str | None = None

    @property
    def cursor(self) -> str | None:
        return self._cursor

    @property
    def ceiling(self) -> str | None:
        return self._ceiling

    async def next_page(self, connection: AsyncConnection) -> SweepPage:
        wrapped = False
        if self._ceiling is None:
            self._ceiling = await highest_run_id(connection)
            self._cursor = None
            wrapped = True
            if self._ceiling is None:
                return SweepPage((), None, None, wrapped)
        run_ids = await marked_run_page(
            connection, self._cursor, self._ceiling, self.page_size
        )
        if len(run_ids) < self.page_size:
            self._ceiling = None
        else:
            self._cursor = run_ids[-1]
        return SweepPage(run_ids, self._cursor, self._ceiling, wrapped)
