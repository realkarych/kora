from dataclasses import dataclass

from sqlalchemy import Executable, case, column, func, select, table, text
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import ColumnElement

from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected
from kora.db.graph_version_reads import MAX_SQL_LENGTH
from kora.db.identifiers import is_identifier
from kora.db.tables import graph, graph_version, graph_version_read_admission
from kora.db.types import POSTGRESQL, SQLITE

SQLITE_SCOPE = text(
    "SELECT 1 FROM main.graph AS g CROSS JOIN main.graph_version AS v "
    "INDEXED BY ix_graph_version_graph_id_id "
    "WHERE g.id = :graph_id AND g.namespace_id = :namespace_id "
    "AND v.graph_id = g.id AND v.id = :version_id"
)
SQLITE_ADMISSION = table(
    graph_version_read_admission.name,
    *(column(field.name, field.type) for field in graph_version_read_admission.columns),
    schema="main",
)


@dataclass(frozen=True)
class GraphVersionReadAdmission:
    db_input_bytes: int
    resource_transport_bytes: int


def _scope_statement(
    dialect: str, namespace_id: str, graph_id: str, version_id: str
) -> Executable:
    if dialect == SQLITE:
        return SQLITE_SCOPE.bindparams(
            namespace_id=namespace_id, graph_id=graph_id, version_id=version_id
        )
    return (
        select(graph_version.c.id)
        .select_from(graph.join(graph_version, graph_version.c.graph_id == graph.c.id))
        .where(
            graph.c.namespace_id == namespace_id,
            graph.c.id == graph_id,
            graph_version.c.id == version_id,
        )
        .with_hint(graph, "ONLY", POSTGRESQL)
        .with_hint(graph_version, "ONLY", POSTGRESQL)
    )


def graph_version_admission_statement(
    dialect: str,
    version_id: str,
    max_db_input_bytes: int,
    max_resource_transport_bytes: int,
) -> Executable:
    fields = (SQLITE_ADMISSION if dialect == SQLITE else graph_version_read_admission).c
    version, db_bytes, transport_bytes = (
        fields.format_version,
        fields.db_input_bytes,
        fields.resource_transport_bytes,
    )
    invalid = GraphValueFailure.INVALID.value
    tests: list[tuple[ColumnElement[bool], str]] = []
    if dialect == SQLITE:
        tests.append((func.typeof(version) != "integer", invalid))
    tests.append((version.is_distinct_from(1), GraphValueFailure.VERSION.value))
    for value in (db_bytes, transport_bytes):
        if dialect == SQLITE:
            tests.append((func.typeof(value) != "integer", invalid))
        tests.append((value <= 0, invalid))
    budget = GraphValueFailure.BUDGET.value
    tests.extend(
        (
            (db_bytes > max_db_input_bytes, budget),
            (transport_bytes > max_resource_transport_bytes, budget),
        )
    )
    failure = case(*tests)
    statement = select(
        failure,
        case((failure.is_(None), db_bytes)),
        case((failure.is_(None), transport_bytes)),
    ).where(fields.graph_version_id == version_id)
    if dialect == SQLITE:
        return statement
    return statement.with_hint(graph_version_read_admission, "ONLY", POSTGRESQL)


async def read_graph_version_admission(
    connection: AsyncConnection,
    namespace_id: str,
    graph_id: str,
    version_id: str,
    *,
    max_db_input_bytes: int,
    max_resource_transport_bytes: int,
) -> GraphVersionReadAdmission | None:
    allowances = (max_db_input_bytes, max_resource_transport_bytes)
    if any(type(limit) is not int or limit <= 0 for limit in allowances):
        raise GraphValueRejected(GraphValueFailure.BUDGET)
    if not all(
        type(value) is str and is_identifier(value)
        for value in (namespace_id, graph_id, version_id)
    ):
        return None
    dialect = connection.dialect.name
    scoped = await connection.execute(
        _scope_statement(dialect, namespace_id, graph_id, version_id)
    )
    if scoped.first() is None:
        return None
    result = await connection.execute(
        graph_version_admission_statement(
            dialect, version_id, *(min(limit, MAX_SQL_LENGTH) for limit in allowances)
        )
    )
    row = result.first()
    if row is None:
        return None
    failure: object = row[0]
    db_bytes: object = row[1]
    transport_bytes: object = row[2]
    for reason in GraphValueFailure:
        if failure == reason.value:
            raise GraphValueRejected(reason)
    if (
        failure is not None
        or type(db_bytes) is not int
        or type(transport_bytes) is not int
    ):
        raise GraphValueRejected(GraphValueFailure.INVALID)
    return GraphVersionReadAdmission(db_bytes, transport_bytes)
