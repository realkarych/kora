from dataclasses import dataclass
from typing import cast

from sqlalchemy import LargeBinary, Table, Text, and_, case, func, literal, select
from sqlalchemy import cast as sql_cast
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import ColumnElement

from kora.db.grant_history_v1 import TIP_FIELDS, VERIFIED_PREFIX
from kora.db.identifiers import is_identifier
from kora.db.run_input_parent import sqlite_storage_width
from kora.db.tables import attempt, attempt_dispatcher, node_instance, run
from kora.db.types import POSTGRESQL, SQLITE


class GrantHistoryRejected(ValueError):
    def __init__(self) -> None:
        super().__init__("grant history is unavailable")


@dataclass(frozen=True, slots=True)
class HistoryScope:
    namespace_id: str
    run_id: str
    node_id: str
    attempt_id: str
    execution_domain_id: str

    def __post_init__(self) -> None:
        if not all(
            type(value) is str and is_identifier(value)
            for value in (self.namespace_id, self.run_id, self.node_id, self.attempt_id)
        ) or not (
            type(self.execution_domain_id) is str
            and 0 < len(self.execution_domain_id) <= 256
            and all("!" <= character <= "~" for character in self.execution_domain_id)
        ):
            raise GrantHistoryRejected()


@dataclass(frozen=True, slots=True)
class HistoryReadLimits:
    run_row: int
    node_row: int
    attempt_row: int
    dispatcher_row: int
    native_bytes: int
    raw_bytes: int

    def admit(self, rows: int, record_bytes: int, process_bytes: int) -> None:
        values = (
            self.run_row,
            self.node_row,
            self.attempt_row,
            self.dispatcher_row,
            self.native_bytes,
            self.raw_bytes,
        )
        if any(type(value) is not int or not 0 < value < 2**63 for value in values):
            raise GrantHistoryRejected()
        native = self.run_row + self.node_row + self.attempt_row
        native += rows * max(self.dispatcher_row, 96 * process_bytes + 4096)
        if (
            native > self.native_bytes
            or rows * (4 * record_bytes + 256) > self.raw_bytes
        ):
            raise GrantHistoryRejected()


@dataclass(frozen=True, slots=True)
class HistoryPosition:
    epoch: int
    count: int
    digest: str


@dataclass(frozen=True, slots=True, repr=False)
class RawGrant:
    dispatcher_id: str
    worker_id: str
    incarnation_id: str
    execution_domain_id: str
    epoch: int
    process: bytes | None
    prefix: HistoryPosition | None


def row_fits(
    owner: Table, maximum: int, replaced: tuple[str, ...] = (), added: int = 0
) -> ColumnElement[bool]:
    return (
        sum(
            (
                sqlite_storage_width(field)
                for field in owner.c
                if field.name not in replaced
            ),
            literal(9 * (len(owner.c) + 1) + added),
        )
        <= maximum
    )


async def verify_history_scope(
    connection: AsyncConnection,
    scope: HistoryScope,
    limits: HistoryReadLimits,
    *,
    writing: bool = False,
) -> bool:
    for owner, identity, matches, maximum in (
        (run, scope.run_id, {"namespace_id": scope.namespace_id}, limits.run_row),
        (node_instance, scope.node_id, {"run_id": scope.run_id}, limits.node_row),
        (
            attempt,
            scope.attempt_id,
            {
                "node_instance_id": scope.node_id,
                "execution_domain_id": scope.execution_domain_id,
            },
            limits.attempt_row,
        ),
    ):
        valid = and_(*(owner.c[key] == value for key, value in matches.items()))
        if connection.dialect.name == SQLITE:
            reserve = writing and owner is attempt
            valid = case(
                (
                    row_fits(
                        owner,
                        maximum,
                        TIP_FIELDS if reserve else (),
                        144 if reserve else 0,
                    ),
                    valid,
                ),
                else_=False,
            )
        if (
            await connection.scalar(select(valid).where(owner.c.id == identity))
            is not True
        ):
            return False
    return True


def _projection(
    connection: AsyncConnection,
    limits: HistoryReadLimits,
    process_bytes: int,
    reserve_prefix: bool,
) -> tuple[ColumnElement[object] | ColumnElement[bool], ...]:
    fields = attempt_dispatcher.c
    if connection.dialect.name == SQLITE:
        raw = sql_cast(fields.process, LargeBinary())
        process_valid = fields.process.is_(None) | and_(
            func.typeof(fields.process) == "text",
            func.octet_length(fields.process) <= 4 * process_bytes,
        )
    elif connection.dialect.name == POSTGRESQL:
        raw = func.pg_catalog.convert_to(
            sql_cast(fields.process, Text()), "UTF8", type_=LargeBinary()
        )
        process_valid = fields.process.is_(None) | (
            func.octet_length(raw) <= 4 * process_bytes
        )
    else:
        raise GrantHistoryRejected()
    valid = and_(
        *(
            and_(
                func.length(fields[name]) <= maximum,
                func.octet_length(fields[name]) <= 4 * maximum,
            )
            for name, maximum in (
                ("id", 26),
                ("worker_id", 255),
                ("incarnation_id", 256),
                ("execution_domain_id", 256),
            )
        ),
        fields.lease_epoch.between(0, 2**63 - 1),
        (fields.prefix_count.is_(None) & fields.prefix_digest.is_(None))
        | (
            fields.prefix_count.between(1, 2**63 - 1)
            & (func.length(fields.prefix_digest) == 64)
            & (func.octet_length(fields.prefix_digest) <= 256)
        ),
        process_valid,
    )
    if connection.dialect.name == SQLITE:
        valid = and_(
            valid,
            func.typeof(fields.lease_epoch) == "integer",
            fields.prefix_count.is_(None)
            | (func.typeof(fields.prefix_count) == "integer"),
        )
        valid = case(
            (
                row_fits(
                    attempt_dispatcher,
                    limits.dispatcher_row,
                    ("prefix_count", "prefix_digest") if reserve_prefix else (),
                    136 if reserve_prefix else 0,
                ),
                valid,
            ),
            else_=False,
        )
    return (
        valid,
        *(
            case((valid, fields[name]))
            for name in (
                "id",
                "worker_id",
                "incarnation_id",
                "execution_domain_id",
                "lease_epoch",
            )
        ),
        case((valid, raw)),
        case((valid, fields.prefix_count)),
        case((valid, fields.prefix_digest)),
    )


def _grant(row: tuple[object, ...]) -> RawGrant:
    if (
        row[0] is not True
        or not all(type(item) is str for item in row[1:5])
        or type(row[5]) is not int
        or (row[6] is not None and type(row[6]) is not bytes)
    ):
        raise GrantHistoryRejected()
    prefix: HistoryPosition | None = None
    if row[7] is not None or row[8] is not None:
        if type(row[7]) is not int or not 0 < row[7] < 2**63 or type(row[8]) is not str:
            raise GrantHistoryRejected()
        if len(row[8]) != 64 or any(
            character not in "0123456789abcdef" for character in row[8]
        ):
            raise GrantHistoryRejected()
        prefix = HistoryPosition(row[5], row[7], row[8])
    return RawGrant(
        cast(str, row[1]),
        cast(str, row[2]),
        cast(str, row[3]),
        cast(str, row[4]),
        row[5],
        row[6],
        prefix,
    )


async def read_history_rows(
    connection: AsyncConnection,
    scope: HistoryScope,
    limits: HistoryReadLimits,
    process_bytes: int,
    *,
    maximum: int,
    after: int | None = None,
    verified: bool = False,
    newest: bool = False,
    reserve_prefix: bool = False,
) -> tuple[RawGrant, ...]:
    if type(maximum) is not int or not 0 < maximum <= 129:
        raise GrantHistoryRejected()
    fields = attempt_dispatcher.c
    statement = select(
        *_projection(connection, limits, process_bytes, reserve_prefix)
    ).where(fields.attempt_id == scope.attempt_id)
    if verified:
        statement = statement.where(VERIFIED_PREFIX)
    if after is not None:
        statement = statement.where(fields.lease_epoch > after)
    statement = statement.order_by(
        fields.lease_epoch.desc() if newest else fields.lease_epoch
    ).limit(maximum)
    return tuple(_grant(tuple(row)) for row in await connection.execute(statement))
