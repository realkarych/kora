from dataclasses import dataclass
from typing import cast

from sqlalchemy import LargeBinary, Table, Text, and_, case, func, literal, select
from sqlalchemy import cast as sql_cast
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import ColumnElement

from kora.db.claim_source_values import ClaimSourcesRejected
from kora.db.native_values import NAMESPACE_POLICY_NATIVE_LIMIT
from kora.db.run_input_parent import sqlite_storage_width
from kora.db.tables import namespace, price_table
from kora.db.types import NAME_LENGTH, SQLITE
from kora.ir.cost import PriceRevision

PRICE_NATIVE_BYTES: int = 96 * PriceRevision.byte_limit
PRICE_ROW_BYTES: int = PRICE_NATIVE_BYTES + 1024
POLICY_ROW_BYTES: int = NAMESPACE_POLICY_NATIVE_LIMIT + 4 * NAME_LENGTH + 512


@dataclass(frozen=True, slots=True, repr=False)
class AcquiredClaimPrice:
    namespace_id: str | None
    effective_from: bytes
    prices: bytes


@dataclass(frozen=True, slots=True, repr=False)
class AcquiredClaimPolicy:
    revision: int
    payload: bytes


def _row_fits(owner: Table, maximum: int) -> ColumnElement[bool]:
    return (
        sum(
            (sqlite_storage_width(field) for field in owner.c),
            literal(9 * (len(owner.c) + 1)),
        )
        <= maximum
    )


def _raw(
    connection: AsyncConnection, value: ColumnElement[object], maximum: int
) -> ColumnElement[bytes]:
    if connection.dialect.name == SQLITE:
        fits = and_(func.typeof(value) == "text", func.octet_length(value) <= maximum)
        return case((fits, sql_cast(value, LargeBinary())))
    raw = func.pg_catalog.convert_to(
        sql_cast(value, Text()), "UTF8", type_=LargeBinary()
    )
    return case((func.pg_catalog.octet_length(raw) <= maximum, raw))


async def acquire_claim_price(
    connection: AsyncConnection, namespace_id: str, price_id: str
) -> AcquiredClaimPrice:
    fields = price_table.c
    valid = and_(
        fields.version == 1,
        fields.normalization_version == 1,
        fields.model.is_(None),
        fields.namespace_id.is_(None) | (fields.namespace_id == namespace_id),
    )
    if connection.dialect.name == SQLITE:
        valid = case((_row_fits(price_table, PRICE_ROW_BYTES), valid), else_=False)
    statement = select(
        case((valid, literal(True))),
        case((valid, fields.namespace_id)),
        case((valid, _raw(connection, fields.effective_from, 128))),
        case((valid, _raw(connection, fields.prices, PRICE_NATIVE_BYTES))),
    ).where(fields.id == price_id)
    row = (await connection.execute(statement)).first()
    if (
        row is None
        or row[0] is not True
        or row[1] not in (None, namespace_id)
        or type(row[2]) is not bytes
        or type(row[3]) is not bytes
    ):
        raise ClaimSourcesRejected()
    return AcquiredClaimPrice(cast(str | None, row[1]), row[2], row[3])


async def acquire_claim_policy(
    connection: AsyncConnection, namespace_id: str
) -> AcquiredClaimPolicy:
    fields = namespace.c
    valid = fields.policy_revision.between(1, 2**31 - 1)
    if connection.dialect.name == SQLITE:
        valid = case((_row_fits(namespace, POLICY_ROW_BYTES), valid), else_=False)
    row = (
        await connection.execute(
            select(
                case((valid, fields.policy_revision)),
                case(
                    (
                        valid,
                        _raw(connection, fields.policy, NAMESPACE_POLICY_NATIVE_LIMIT),
                    )
                ),
            ).where(fields.id == namespace_id)
        )
    ).first()
    if row is None or type(row[0]) is not int or type(row[1]) is not bytes:
        raise ClaimSourcesRejected()
    return AcquiredClaimPolicy(row[0], row[1])
