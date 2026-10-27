from collections.abc import Callable, Iterable, Sequence
from dataclasses import Field, dataclass, fields
from datetime import datetime
from typing import TypeAlias, TypeVar, TypeVarTuple

from sqlalchemy import (
    Column,
    ColumnElement,
    Executable,
    Exists,
    Select,
    Table,
    exists,
    func,
    select,
    true,
)
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

RowT = TypeVar("RowT")
Columns = TypeVarTuple("Columns")


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


RowColumns: TypeAlias = tuple[Column[object], ...]


def columns_of(table: Table, row_fields: Iterable[Field[object]]) -> RowColumns:
    return tuple(table.c[row_field.name] for row_field in row_fields)


NAMESPACE_COLUMNS: RowColumns = columns_of(namespace, fields(NamespaceRow))
TOKEN_COLUMNS: RowColumns = columns_of(api_token, fields(TokenRow))
WORKER_COLUMNS: RowColumns = columns_of(worker, fields(WorkerRow))
GRAPH_COLUMNS: RowColumns = columns_of(graph, fields(GraphRow))
GRAPH_VERSION_COLUMNS: RowColumns = columns_of(graph_version, fields(GraphVersionRow))
RUN_COLUMNS: RowColumns = columns_of(run, fields(RunRow))
NODE_INSTANCE_COLUMNS: RowColumns = columns_of(node_instance, fields(NodeInstanceRow))
EVENT_COLUMNS: RowColumns = columns_of(event, fields(EventRow))


async def first_row(
    connection: AsyncConnection, row: Callable[..., RowT], statement: Executable
) -> RowT | None:
    found = (await connection.execute(statement)).first()
    return None if found is None else row(*found)


def keyset_after(
    statement: Select[*Columns], key: ColumnElement[str], after_id: str | None
) -> Select[*Columns]:
    return statement if after_id is None else statement.where(key > after_id)


def physical_attempt(run_id: str | ColumnElement[str]) -> Exists:
    return exists().where(
        node_instance.c.run_id == run_id,
        attempt.c.node_instance_id == node_instance.c.id,
        attempt.c.status.in_(NON_TERMINAL_ATTEMPT_STATUSES),
    )


async def namespace_by_name(
    connection: AsyncConnection, name: str
) -> NamespaceRow | None:
    statement = select(*NAMESPACE_COLUMNS).where(namespace.c.name == name)
    return await first_row(connection, NamespaceRow, statement)


async def namespace_by_id(
    connection: AsyncConnection, namespace_id: str
) -> NamespaceRow | None:
    statement = select(*NAMESPACE_COLUMNS).where(namespace.c.id == namespace_id)
    return await first_row(connection, NamespaceRow, statement)


async def token_by_digest(connection: AsyncConnection, digest: str) -> TokenRow | None:
    statement = select(*TOKEN_COLUMNS).where(api_token.c.digest == digest)
    return await first_row(connection, TokenRow, statement)


async def worker_by_id(connection: AsyncConnection, worker_id: str) -> WorkerRow | None:
    statement = select(*WORKER_COLUMNS).where(worker.c.id == worker_id)
    return await first_row(connection, WorkerRow, statement)


async def graph_by_name(
    connection: AsyncConnection, namespace_id: str, name: str
) -> GraphRow | None:
    statement = select(*GRAPH_COLUMNS).where(
        graph.c.namespace_id == namespace_id, graph.c.name == name
    )
    return await first_row(connection, GraphRow, statement)


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
    return await first_row(connection, GraphVersionRow, statement)


async def run_by_id(
    connection: AsyncConnection, namespace_id: str, run_id: str
) -> RunRow | None:
    statement = select(*RUN_COLUMNS).where(
        run.c.namespace_id == namespace_id, run.c.id == run_id
    )
    return await first_row(connection, RunRow, statement)


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
    statement = keyset_after(statement, event.c.id, after_id)
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
    physical = physical_attempt(run_id)
    return bool((await connection.execute(select(physical))).scalar())


async def execution_run_usage(connection: AsyncConnection, graph_id: str) -> int:
    physical = physical_attempt(run.c.id)
    statement = select(func.count(run.c.id)).where(run.c.graph_id == graph_id, physical)
    return int((await connection.execute(statement)).scalar_one())


async def namespace_outstanding_usage(
    connection: AsyncConnection, namespace_id: str
) -> int:
    physical = physical_attempt(run.c.id)
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
    statement = keyset_after(statement, run.c.id, after_id)
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
