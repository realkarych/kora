from typing import Never, cast

from sqlalchemy import (
    BigInteger,
    Executable,
    Integer,
    LargeBinary,
    String,
    bindparam,
    case,
    column,
    distinct,
    func,
    literal,
    or_,
    select,
    table,
    text,
)
from sqlalchemy import cast as sql_cast
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import BindParameter, ColumnElement
from sqlalchemy.sql.selectable import CTE, FromClause

from kora.db.connection import Dialect
from kora.db.graph_resource_transport import graph_resource_binary_projection
from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected
from kora.db.graph_version_reads import MAX_SQL_LENGTH
from kora.db.identifiers import is_identifier
from kora.db.tables import graph_version_resource_owner
from kora.db.types import IDENTIFIER_LENGTH

MAX_OWNER_ROWS = 64
MAX_OWNER_SIZE = 262_144
EMPTY = "empty"
COMPLETE = "complete"

SQLITE_OWNER = table(
    graph_version_resource_owner.name,
    *(column(field.name, field.type) for field in graph_version_resource_owner.columns),
    schema="main",
)
SQLITE_PREFIX = text("""
SELECT CASE
         WHEN typeof(ordinal) <> 'integer' THEN NULL
         WHEN ordinal BETWEEN 0 AND :last_ordinal THEN ordinal
       END AS ordinal,
       CASE
         WHEN typeof(total_size) <> 'integer'
           OR typeof(subgraph_name) NOT IN ('null', 'text')
           OR typeof(node_name) <> 'text' OR typeof(file_name) <> 'text'
           OR typeof(store_id) <> 'text' OR typeof(payload_digest) <> 'text'
           OR typeof(object_incarnation) <> 'text'
           OR typeof(payload_state) <> 'text' THEN NULL
         WHEN total_size NOT BETWEEN 0 AND :max_owner_size THEN NULL
         ELSE coalesce(octet_length(subgraph_name), 0)
           + octet_length(node_name) + octet_length(file_name)
           + octet_length(store_id) + octet_length(payload_digest)
           + octet_length(object_incarnation) + octet_length(payload_state)
       END AS native_width
FROM main.graph_version_resource_owner AS owner
     INDEXED BY sqlite_autoindex_graph_version_resource_owner_1
WHERE owner.graph_version_id = :selected_version
ORDER BY owner.ordinal
LIMIT :observation_limit
""")

NativeOwnerRow = tuple[int, int, bytes | None, bytes, bytes, bytes, bytes, bytes, bytes]


class AcquiredGraphResourceOwners:
    __slots__ = ("version_id", "rows")
    version_id: str
    rows: tuple[NativeOwnerRow, ...]

    def __init__(self, version_id: str, rows: tuple[NativeOwnerRow, ...]) -> None:
        if hasattr(self, "version_id"):
            del self, version_id, rows
            raise AttributeError("acquired graph owners are immutable")
        object.__setattr__(self, "version_id", version_id)
        object.__setattr__(self, "rows", rows)

    def __setattr__(self, name: str, value: object) -> Never:
        raise AttributeError("acquired graph owners are immutable")

    def __delattr__(self, name: str) -> Never:
        raise AttributeError("acquired graph owners are immutable")

    def __getstate__(self) -> Never:
        raise TypeError("acquired graph owners require explicit access")


def _text_fields(owner: FromClause) -> tuple[ColumnElement[str], ...]:
    fields = owner.c
    return (
        fields.subgraph_name,
        fields.node_name,
        fields.file_name,
        fields.store_id,
        fields.payload_digest,
        fields.object_incarnation,
        fields.payload_state,
    )


def _width_prefix(dialect: Dialect, version: BindParameter[str]) -> CTE:
    if dialect is Dialect.SQLITE:
        return (
            SQLITE_PREFIX.bindparams(
                version,
                last_ordinal=MAX_OWNER_ROWS - 1,
                max_owner_size=MAX_OWNER_SIZE,
                observation_limit=MAX_OWNER_ROWS + 1,
            )
            .columns(column("ordinal", Integer()), column("native_width", BigInteger()))
            .cte("owner_widths")
            .prefix_with("MATERIALIZED")
        )

    owner = graph_version_resource_owner
    fields = owner.c
    widths = tuple(
        sql_cast(func.pg_catalog.octet_length(field), BigInteger())
        for field in _text_fields(owner)
    )
    total = sum(widths[1:], func.coalesce(widths[0], 0))
    return (
        select(
            case((fields.ordinal.between(0, MAX_OWNER_ROWS - 1), fields.ordinal)).label(
                "ordinal"
            ),
            case((fields.total_size.between(0, MAX_OWNER_SIZE), total)).label(
                "native_width"
            ),
        )
        .where(fields.graph_version_id == version)
        .order_by(fields.ordinal)
        .limit(MAX_OWNER_ROWS + 1)
        .with_hint(owner, "ONLY", Dialect.POSTGRESQL.value)
        .cte("owner_widths")
        .prefix_with("MATERIALIZED")
    )


def graph_resource_owner_statement(
    dialect: Dialect, version_id: str, *, max_text_bytes: int
) -> Executable:
    if type(version_id) is not str or not is_identifier(version_id):
        del version_id
        raise GraphValueRejected(GraphValueFailure.INVALID)
    if type(max_text_bytes) is not int or max_text_bytes <= 0:
        del max_text_bytes
        raise GraphValueRejected(GraphValueFailure.BUDGET)
    version = bindparam("selected_version", version_id, type_=String(IDENTIFIER_LENGTH))
    allowance = literal(min(max_text_bytes, MAX_SQL_LENGTH), type_=BigInteger())
    widths = _width_prefix(dialect, version)
    ordinal, native_width = widths.c.ordinal, widths.c.native_width
    count = func.count()
    status = case(
        (count == 0, EMPTY),
        (
            or_(
                count > MAX_OWNER_ROWS,
                func.count(distinct(ordinal)) != count,
                func.min(ordinal) != 0,
                func.max(ordinal) != count - 1,
                func.count(native_width) != count,
            ),
            GraphValueFailure.INVALID.value,
        ),
        (func.sum(native_width) > allowance, GraphValueFailure.BUDGET.value),
        else_=COMPLETE,
    ).label("status")
    gate = (
        select(status).select_from(widths).cte("owner_gate").prefix_with("MATERIALIZED")
    )
    owner = SQLITE_OWNER if dialect is Dialect.SQLITE else graph_version_resource_owner
    fields = owner.c
    complete = gate.c.status == COMPLETE
    cells = (
        fields.ordinal,
        fields.total_size,
        *(
            func.pg_catalog.convert_to(
                field, func.pg_catalog.getdatabaseencoding(), type_=LargeBinary()
            )
            if dialect is Dialect.POSTGRESQL
            else graph_resource_binary_projection(dialect, field)
            for field in _text_fields(owner)
        ),
    )
    statement = (
        select(gate.c.status, *(case((complete, cell)) for cell in cells))
        .select_from(
            gate.outerjoin(owner, fields.graph_version_id == case((complete, version)))
        )
        .order_by(fields.ordinal)
    )
    if dialect is Dialect.POSTGRESQL:
        return statement.with_hint(owner, "ONLY", Dialect.POSTGRESQL.value)
    return statement


def _checked_owner_row(row: tuple[object, ...], position: int) -> NativeOwnerRow:
    if len(row) != 10:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    marker, ordinal, total_size, subgraph, *fields = row
    if (
        marker != COMPLETE
        or type(ordinal) is not int
        or ordinal != position
        or type(total_size) is not int
        or not 0 <= total_size <= MAX_OWNER_SIZE
        or (subgraph is not None and type(subgraph) is not bytes)
        or any(type(value) is not bytes for value in fields)
    ):
        raise GraphValueRejected(GraphValueFailure.INVALID)
    return cast(NativeOwnerRow, row[1:])


async def _acquire(
    connection: AsyncConnection, version_id: str, max_text_bytes: int
) -> AcquiredGraphResourceOwners:
    statement = graph_resource_owner_statement(
        Dialect(connection.dialect.name), version_id, max_text_bytes=max_text_bytes
    )
    result = await connection.execute(statement)
    rows: list[tuple[object, ...]] = [tuple(row) for row in result.all()]
    if not rows or len(rows) > MAX_OWNER_ROWS:
        raise GraphValueRejected(GraphValueFailure.INVALID)
    first = rows[0]
    if (
        len(rows) == 1
        and len(first) == 10
        and all(value is None for value in first[1:])
    ):
        if first[0] == EMPTY:
            return AcquiredGraphResourceOwners(version_id, ())
        if first[0] == GraphValueFailure.BUDGET.value:
            raise GraphValueRejected(GraphValueFailure.BUDGET)
    return AcquiredGraphResourceOwners(
        version_id,
        tuple(_checked_owner_row(row, index) for index, row in enumerate(rows)),
    )


async def acquire_graph_resource_owners(
    connection: AsyncConnection, version_id: str, *, max_text_bytes: int
) -> AcquiredGraphResourceOwners:
    try:
        return await _acquire(connection, version_id, max_text_bytes)
    except GraphValueRejected as refusal:
        reason = refusal.reason
    del connection, version_id, max_text_bytes
    error = GraphValueRejected(reason)
    try:
        raise error
    finally:
        error.__cause__ = None
        error.__context__ = None
