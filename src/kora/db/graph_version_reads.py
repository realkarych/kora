from typing import Literal, Never, cast

from sqlalchemy import (
    BigInteger,
    Executable,
    LargeBinary,
    Text,
    and_,
    case,
    column,
    func,
    literal,
    select,
)
from sqlalchemy import cast as sql_cast
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import ColumnElement

from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected
from kora.db.tables import graph, graph_version
from kora.db.types import DIGEST_LENGTH, POSTGRESQL

TransportEncoding = Literal["ascii", "utf-8", "utf-16le", "utf-16be"]
TRANSPORT_ENCODINGS = frozenset({"ascii", "utf-8", "utf-16le", "utf-16be"})
DOUBLE_WIDTH = frozenset({"utf-16le", "utf-16be"})
MAX_SQL_LENGTH = (1 << 63) - 1
TRANSPORT_COLUMNS = (
    graph_version.c.ir,
    graph_version.c.execution_contract,
    graph_version.c.ir_digest,
    graph_version.c.execution_contract_digest,
    graph_version.c.deployment_digest,
)


class AcquiredGraphVersion:
    __slots__ = (
        "_namespace_id",
        "_graph_id",
        "_version_id",
        "_encoding",
        "_payloads",
        "_digests",
    )
    _namespace_id: str
    _graph_id: str
    _version_id: str
    _encoding: TransportEncoding
    _payloads: tuple[bytes, bytes]
    _digests: tuple[bytes, bytes, bytes]

    def __init__(
        self,
        namespace_id: str,
        graph_id: str,
        version_id: str,
        encoding: TransportEncoding,
        payloads: tuple[bytes, bytes],
        digests: tuple[bytes, bytes, bytes],
    ) -> None:
        object.__setattr__(self, "_namespace_id", namespace_id)
        object.__setattr__(self, "_graph_id", graph_id)
        object.__setattr__(self, "_version_id", version_id)
        object.__setattr__(self, "_encoding", encoding)
        object.__setattr__(self, "_payloads", payloads)
        object.__setattr__(self, "_digests", digests)

    def __setattr__(self, name: str, value: object) -> Never:
        raise AttributeError("graph version transport is immutable")

    def __delattr__(self, name: str) -> Never:
        raise AttributeError("graph version transport is immutable")

    @property
    def namespace_id(self) -> str:
        return self._namespace_id

    @property
    def graph_id(self) -> str:
        return self._graph_id

    @property
    def id(self) -> str:
        return self._version_id

    @property
    def encoding(self) -> TransportEncoding:
        return self._encoding

    @property
    def payloads(self) -> tuple[bytes, bytes]:
        return self._payloads

    @property
    def digests(self) -> tuple[bytes, bytes, bytes]:
        return self._digests

    def __getstate__(self) -> Never:
        raise TypeError("graph version transport requires explicit access")


def transport_limit(max_encoded_bytes: int, width: int) -> int:
    if type(max_encoded_bytes) is not int or max_encoded_bytes <= 0:
        raise GraphValueRejected(GraphValueFailure.BUDGET)
    return min(width * (2 * max_encoded_bytes + 4), MAX_SQL_LENGTH)


def binary_projection(
    connection: AsyncConnection,
) -> tuple[ColumnElement[str], ColumnElement[bool], tuple[ColumnElement[bytes], ...]]:
    if connection.dialect.name == POSTGRESQL:
        encoding = literal("ascii", type_=Text())
        valid = and_(
            func.jsonb_typeof(graph_version.c.ir) == "string",
            func.jsonb_typeof(graph_version.c.execution_contract) == "string",
        )
        values = tuple(
            func.convert_to(
                sql_cast(value, Text()), func.getdatabaseencoding(), type_=LargeBinary()
            )
            for value in TRANSPORT_COLUMNS
        )
    else:
        pragma = func.pragma_encoding().table_valued(column("encoding", Text()))
        encoding = sql_cast(
            select(func.lower(pragma.c.encoding)).select_from(pragma).scalar_subquery(),
            Text(),
        )
        valid = and_(*(func.typeof(value) == "text" for value in TRANSPORT_COLUMNS))
        values = tuple(sql_cast(value, LargeBinary()) for value in TRANSPORT_COLUMNS)
    return encoding, valid, values


def graph_version_transport_statement(
    connection: AsyncConnection,
    namespace_id: str,
    graph_id: str,
    version_id: str,
    *,
    max_encoded_bytes: int,
) -> Executable:
    single = transport_limit(max_encoded_bytes, 1)
    double = transport_limit(max_encoded_bytes, 2)
    encoding, shape, values = binary_projection(connection)
    wide = encoding.in_(DOUBLE_WIDTH)
    maximum = case(
        (wide, literal(double, type_=BigInteger())),
        else_=literal(single, type_=BigInteger()),
    )
    digest_length = case((wide, 2 * DIGEST_LENGTH), else_=DIGEST_LENGTH)
    valid = and_(
        encoding.in_(TRANSPORT_ENCODINGS),
        shape,
        *(func.length(value) == digest_length for value in values[2:]),
    )
    total = sql_cast(func.length(values[0]), BigInteger()) + sql_cast(
        func.length(values[1]), BigInteger()
    )
    failure = case(
        (
            graph_version.c.value_encoding_version.is_distinct_from(1),
            GraphValueFailure.VERSION.value,
        ),
        (valid.is_not(True), GraphValueFailure.INVALID.value),
        (total > maximum, GraphValueFailure.BUDGET.value),
        else_=None,
    )
    projections = tuple(
        case((failure.is_(None), value), else_=None).label(str(source.key))
        for source, value in zip(TRANSPORT_COLUMNS, values, strict=True)
    )
    return (
        select(failure.label("failure"), encoding.label("encoding"), *projections)
        .select_from(graph_version.join(graph, graph.c.id == graph_version.c.graph_id))
        .where(
            graph.c.namespace_id == namespace_id,
            graph_version.c.graph_id == graph_id,
            graph_version.c.id == version_id,
        )
    )


async def _acquire_graph_version(
    connection: AsyncConnection,
    namespace_id: str,
    graph_id: str,
    version_id: str,
    *,
    max_encoded_bytes: int,
) -> AcquiredGraphVersion | None:
    statement = graph_version_transport_statement(
        connection,
        namespace_id,
        graph_id,
        version_id,
        max_encoded_bytes=max_encoded_bytes,
    )
    row = (await connection.execute(statement)).first()
    if row is None:
        return None
    failure: object = row[0]
    if failure is not None:
        for reason in GraphValueFailure:
            if failure == reason.value:
                raise GraphValueRejected(reason)
        raise GraphValueRejected(GraphValueFailure.INVALID)
    encoding: object = row[1]
    raw: tuple[object, ...] = tuple(row[index] for index in range(2, 7))
    if (
        type(encoding) is not str
        or encoding not in TRANSPORT_ENCODINGS
        or any(type(value) is not bytes for value in raw)
    ):
        raise GraphValueRejected(GraphValueFailure.INVALID)
    first, second, ir_digest, contract_digest, deployment_digest = cast(
        tuple[bytes, bytes, bytes, bytes, bytes], raw
    )
    return AcquiredGraphVersion(
        namespace_id,
        graph_id,
        version_id,
        cast(TransportEncoding, encoding),
        (first, second),
        (ir_digest, contract_digest, deployment_digest),
    )


async def acquire_graph_version(
    connection: AsyncConnection,
    namespace_id: str,
    graph_id: str,
    version_id: str,
    *,
    max_encoded_bytes: int,
) -> AcquiredGraphVersion | None:
    try:
        return await _acquire_graph_version(
            connection,
            namespace_id,
            graph_id,
            version_id,
            max_encoded_bytes=max_encoded_bytes,
        )
    except GraphValueRejected as refusal:
        reason = refusal.reason
    error = GraphValueRejected(reason)
    try:
        raise error
    finally:
        error.__cause__ = None
        error.__context__ = None
