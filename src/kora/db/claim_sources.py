from dataclasses import dataclass

from sqlalchemy import (
    BigInteger,
    Executable,
    LargeBinary,
    Table,
    Text,
    and_,
    case,
    cast,
    column,
    false,
    func,
    literal,
    literal_column,
    select,
    text,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import ColumnElement, TextClause

from kora.db import DB_COMPONENT, DB_LOGGER_NAME
from kora.db.claim_source_values import ClaimSourcesRejected, SourceAllowance
from kora.db.claim_sources_v1 import (
    MAX_SOURCE_COUNT,
    SOURCE_FIELDS,
    SQLITE_ADMISSION_KEY,
    SQLITE_KEY,
)
from kora.db.identifiers import is_identifier
from kora.db.run_input_parent import sqlite_storage_width, verify_run_input_parent
from kora.db.run_input_reads import RunInputBinding
from kora.db.tables import attempt, attempt_claim_read_admission, node_instance
from kora.db.types import PATH_LENGTH, POSTGRESQL, SQLITE
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

ACQUIRED: EventSpec = CATALOG.register(
    EventSpec(
        "db.claim_sources.acquired",
        DB_COMPONENT,
        Level.DEBUG,
        "claim source acquisition completed",
        {"acquired": FieldKind.BOOLEAN},
    )
)
LOCATED_SOURCES: dict[Table, tuple[TextClause, TextClause]] = {
    attempt: (
        text("main.attempt INDEXED BY ix_claim_attempt_locator"),
        text("ONLY attempt"),
    ),
    node_instance: (
        text("main.node_instance INDEXED BY ix_claim_node_locator"),
        text("ONLY node_instance"),
    ),
    attempt_claim_read_admission: (
        text("main.attempt_claim_read_admission INDEXED BY ix_claim_admission_locator"),
        text("ONLY attempt_claim_read_admission"),
    ),
}


@dataclass(frozen=True, slots=True)
class ClaimSourceBinding:
    run: RunInputBinding
    node_id: str
    attempt_id: str
    price_table_id: str
    node_path: str

    def __post_init__(self) -> None:
        if (
            not all(
                type(value) is str and is_identifier(value)
                for value in (
                    self.run.namespace_id,
                    self.run.graph_id,
                    self.run.version_id,
                    self.run.run_id,
                    self.node_id,
                    self.attempt_id,
                    self.price_table_id,
                )
            )
            or type(self.node_path) is not str
            or len(self.node_path) > PATH_LENGTH
            or "\0" in self.node_path
        ):
            raise ClaimSourcesRejected()


@dataclass(frozen=True, slots=True)
class ClaimSourceReadLimits:
    run_row: int
    node_row: int
    attempt_row: int
    fields: tuple[SourceAllowance, ...]

    def __post_init__(self) -> None:
        if len(self.fields) != len(SOURCE_FIELDS) or any(
            type(value) is not int or not 0 < value <= MAX_SOURCE_COUNT
            for value in (self.run_row, self.node_row, self.attempt_row)
        ):
            raise ClaimSourcesRejected()


@dataclass(frozen=True, slots=True, repr=False)
class AcquiredClaimSources:
    binding: ClaimSourceBinding
    revision: int
    values: tuple[bytes | None, ...]
    encoding: str


def _located(
    owner: Table, dialect: str, identity: str
) -> tuple[TextClause, ColumnElement[bool]]:
    sources = LOCATED_SOURCES.get(owner)
    if sources is None:
        raise ClaimSourcesRejected()
    admission = owner is attempt_claim_read_admission
    key = column("attempt_id" if admission else "id", Text())
    if dialect == SQLITE:
        predicate = SQLITE_ADMISSION_KEY if admission else SQLITE_KEY
        return (
            sources[0],
            and_(key.collate("BINARY") == identity, literal_column(predicate)),
        )
    if dialect == POSTGRESQL:
        return sources[1], key == identity
    raise ClaimSourcesRejected()


def _width(owner: Table) -> ColumnElement[int]:
    return sum(
        (sqlite_storage_width(column(field.name, field.type)) for field in owner.c),
        literal(9 * (len(owner.c) + 1)),
    )


def _row_statement(
    owner: Table,
    dialect: str,
    identity: str,
    matches: dict[str, str],
    row_bytes: int,
) -> Executable:
    source, predicate = _located(owner, dialect, identity)
    match = and_(
        *(
            column(name).collate("BINARY" if dialect == SQLITE else "C") == value
            for name, value in matches.items()
        )
    )
    if dialect == SQLITE:
        match = case((_width(owner) <= row_bytes, match), else_=false())
    return select(match).select_from(source).where(predicate)


async def verify_claim_source_binding(
    connection: AsyncConnection,
    binding: ClaimSourceBinding,
    limits: ClaimSourceReadLimits,
    *,
    include_attempt: bool = True,
) -> bool:
    transaction = connection.get_transaction()
    if transaction is None or not transaction.is_active:
        return False
    scope = binding.run
    if not await verify_run_input_parent(
        connection,
        scope.namespace_id,
        scope.graph_id,
        scope.version_id,
        scope.run_id,
        max_parent_bytes=limits.run_row,
    ):
        return False
    rows = [
        (
            node_instance,
            binding.node_id,
            {"run_id": scope.run_id, "node_path": binding.node_path},
            limits.node_row,
        )
    ]
    if include_attempt:
        rows.append(
            (
                attempt,
                binding.attempt_id,
                {
                    "node_instance_id": binding.node_id,
                    "price_table_id": binding.price_table_id,
                },
                limits.attempt_row,
            )
        )
    for owner, identity, matches, allowance in rows:
        if (
            await connection.scalar(
                _row_statement(
                    owner, connection.dialect.name, identity, matches, allowance
                )
            )
            is not True
        ):
            return False
    return connection.get_transaction() is transaction and transaction.is_active


def claim_source_admission_statement(
    dialect: str, attempt_id: str, limits: ClaimSourceReadLimits
) -> Executable:
    owner = attempt_claim_read_admission
    source, predicate = _located(owner, dialect, attempt_id)
    names = (
        "format_version",
        "source_revision",
        *(
            f"{field}_{kind}_bytes"
            for field in SOURCE_FIELDS
            for kind in ("native", "transport")
        ),
    )
    fields = tuple(column(name, BigInteger()) for name in names)
    tests = [fields[0] == 1, fields[1].between(1, MAX_SOURCE_COUNT), fields[2] > 0]
    for index, allowance in enumerate(limits.fields):
        native, transport = fields[2 + 2 * index : 4 + 2 * index]
        tests.extend(
            (
                native.between(0, allowance.native),
                transport.between(0, allowance.transport),
                (native == 0) == (transport == 0),
            )
        )
    valid = and_(*tests)
    if dialect == SQLITE:
        typed = and_(*(func.typeof(value) == "integer" for value in fields))
        valid = case((typed, valid), else_=false())
        fixed_width = 9 * (len(owner.c) + 1) + 52 + 8 * len(fields) + 128
        valid = case((_width(owner) <= fixed_width, valid), else_=false())
    return (
        select(*(case((valid, value)) for value in fields[1:]))
        .select_from(source)
        .where(predicate)
    )


async def read_claim_source_admission(
    connection: AsyncConnection,
    attempt_id: str,
    limits: ClaimSourceReadLimits,
) -> tuple[int, tuple[tuple[int, int], ...]] | None:
    result = await connection.execute(
        claim_source_admission_statement(connection.dialect.name, attempt_id, limits)
    )
    row = result.first()
    if row is None or not all(type(value) is int for value in row):
        return None
    values = tuple(int(value) for value in row)
    return values[0], tuple(
        (values[index], values[index + 1]) for index in range(1, len(values), 2)
    )


def claim_source_value_statement(
    dialect: str, attempt_id: str, name: str, quantities: tuple[int, int]
) -> Executable:
    if name not in SOURCE_FIELDS:
        raise ClaimSourcesRejected()
    source, predicate = _located(attempt, dialect, attempt_id)
    native, transport = quantities
    value = column(name, Text())
    if dialect == SQLITE:
        raw = cast(value, LargeBinary())
        fits = and_(func.typeof(value) == "text", func.octet_length(value) <= native)
        projected = case((fits, case((func.length(raw) <= transport, raw))))
    else:
        raw = func.pg_catalog.convert_to(
            cast(value, Text()), "UTF8", type_=LargeBinary()
        )
        projected = case((func.pg_catalog.octet_length(raw) <= transport, raw))
    if native == 0:
        absent = func.typeof(value) == "null" if dialect == SQLITE else value.is_(None)
        projected = case((absent, literal(b"", type_=LargeBinary())))
    return select(projected).select_from(source).where(predicate)


async def _acquire_claim_sources(
    connection: AsyncConnection,
    binding: ClaimSourceBinding,
    limits: ClaimSourceReadLimits,
) -> AcquiredClaimSources | None:
    if not await verify_claim_source_binding(connection, binding, limits):
        return None
    admission = await read_claim_source_admission(
        connection, binding.attempt_id, limits
    )
    if admission is None:
        return None
    revision, quantities = admission
    encoding = "utf-8"
    if connection.dialect.name == SQLITE:
        observed: object = await connection.scalar(text("PRAGMA encoding"))
        if observed not in ("UTF-8", "UTF-16le", "UTF-16be"):
            return None
        encoding = str(observed)
    values: list[bytes | None] = []
    for name, bounds in zip(SOURCE_FIELDS, quantities, strict=True):
        value: object = await connection.scalar(
            claim_source_value_statement(
                connection.dialect.name, binding.attempt_id, name, bounds
            )
        )
        if type(value) is not bytes or len(value) > bounds[1]:
            return None
        if bounds == (0, 0):
            values.append(None)
        elif not value:
            return None
        else:
            values.append(value)
    return AcquiredClaimSources(binding, revision, tuple(values), encoding)


async def acquire_claim_sources(
    connection: AsyncConnection,
    binding: ClaimSourceBinding,
    limits: ClaimSourceReadLimits,
) -> AcquiredClaimSources | None:
    try:
        result = await _acquire_claim_sources(connection, binding, limits)
    except (OSError, SQLAlchemyError):
        result = None
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(DB_COMPONENT, name=DB_LOGGER_NAME).emit(
            ACQUIRED, acquired=result is not None
        )
    return result
