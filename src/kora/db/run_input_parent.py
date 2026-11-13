from sqlalchemy import (
    Executable,
    Integer,
    String,
    and_,
    case,
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
from sqlalchemy.sql.elements import ColumnElement

from kora.db import DB_COMPONENT, DB_LOGGER_NAME, RUN_INPUT_PARENT_CHECKED
from kora.db.identifiers import is_identifier
from kora.db.run_input_locator_v1 import SQLITE_RUN_KEY
from kora.db.tables import run
from kora.db.types import POSTGRESQL, SQLITE
from kora.observability import current_diagnostics

INPUT_SENTINEL: str = "kora.run-inputs.v1"
SQLITE_MAX_PARENT_ALLOWANCE: int = (1 << 63) - 1
SQLITE_RECORD_HEADER_MAX: int = 9 * (len(run.c) + 1)
POSTGRES_MARKER_BYTES: int = 64
SQLITE_INLINE_VALUES = (
    column("params", String()),
    column("cel_params_projection", String()),
)
POSTGRES_INLINE_VALUES = (run.c.params, run.c.cel_params_projection)
SQLITE_RUN = text("main.run INDEXED BY ix_run_input_locator")


def sqlite_storage_width(field: ColumnElement[object]) -> ColumnElement[int]:
    kind = func.typeof(field)
    return case(
        (kind == "null", 0),
        (kind.in_(("integer", "real")), 8),
        else_=func.octet_length(field),
    )


def _sqlite_markers() -> ColumnElement[bool]:
    valid = and_(
        *(func.typeof(value) == "text" for value in SQLITE_INLINE_VALUES),
        *(func.instr(value, func.char(0)) == 0 for value in SQLITE_INLINE_VALUES),
        *(func.instr(value, r"\u0000") == 0 for value in SQLITE_INLINE_VALUES),
        *(func.json_valid(value) == 1 for value in SQLITE_INLINE_VALUES),
    )
    match = and_(
        *(func.json_type(value) == "text" for value in SQLITE_INLINE_VALUES),
        *(
            func.json_extract(value, "$").collate("BINARY") == INPUT_SENTINEL
            for value in SQLITE_INLINE_VALUES
        ),
    )
    return case((valid, match), else_=false())


def _sqlite_parent_match(
    namespace_id: str, graph_id: str, version_id: str, max_parent_bytes: int
) -> ColumnElement[bool]:
    width = sum(
        (sqlite_storage_width(column(field.name)) for field in run.c),
        literal(SQLITE_RECORD_HEADER_MAX),
    )
    tag = column("input_encoding_version", Integer())
    match = and_(
        column("namespace_id").collate("BINARY") == namespace_id,
        column("graph_id").collate("BINARY") == graph_id,
        column("graph_version_id").collate("BINARY") == version_id,
        func.typeof(tag) == "integer",
        tag == 1,
        _sqlite_markers(),
    )
    return case((width <= max_parent_bytes, match), else_=false())


def _postgres_parent_match(
    namespace_id: str, graph_id: str, version_id: str
) -> ColumnElement[bool]:
    fields = run.c
    narrow = and_(
        *(
            func.pg_catalog.pg_column_compression(value).is_(None)
            for value in POSTGRES_INLINE_VALUES
        ),
        *(
            func.pg_catalog.pg_column_size(value) <= POSTGRES_MARKER_BYTES
            for value in POSTGRES_INLINE_VALUES
        ),
    )
    return and_(
        fields.namespace_id == namespace_id,
        fields.graph_id == graph_id,
        fields.graph_version_id == version_id,
        fields.input_encoding_version == 1,
        case(
            (
                narrow,
                and_(*(value == INPUT_SENTINEL for value in POSTGRES_INLINE_VALUES)),
            ),
            else_=false(),
        ),
    )


def run_input_parent_statement(
    dialect: str,
    namespace_id: str,
    graph_id: str,
    version_id: str,
    run_id: str,
    *,
    max_parent_bytes: int,
) -> Executable:
    if dialect == SQLITE:
        match = _sqlite_parent_match(
            namespace_id, graph_id, version_id, max_parent_bytes
        )
        return (
            select(match)
            .select_from(SQLITE_RUN)
            .where(
                column("id").collate("BINARY") == run_id, literal_column(SQLITE_RUN_KEY)
            )
        )
    if dialect == POSTGRESQL:
        match = _postgres_parent_match(namespace_id, graph_id, version_id)
        return select(match).where(run.c.id == run_id)
    raise ValueError("the run input parent backend is unsupported")


def _observed(verified: bool) -> bool:
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(DB_COMPONENT, name=DB_LOGGER_NAME).emit(
            RUN_INPUT_PARENT_CHECKED, verified=verified
        )
    return verified


async def verify_run_input_parent(
    connection: AsyncConnection,
    namespace_id: str,
    graph_id: str,
    version_id: str,
    run_id: str,
    *,
    max_parent_bytes: int,
) -> bool:
    if (
        type(max_parent_bytes) is not int
        or not 0 < max_parent_bytes <= SQLITE_MAX_PARENT_ALLOWANCE
        or not all(
            type(value) is str and is_identifier(value)
            for value in (namespace_id, graph_id, version_id, run_id)
        )
    ):
        return _observed(False)
    try:
        dialect = connection.dialect.name
        transaction = connection.get_transaction()
        if (
            dialect not in (SQLITE, POSTGRESQL)
            or transaction is None
            or not transaction.is_active
        ):
            return _observed(False)
        statement = run_input_parent_statement(
            dialect,
            namespace_id,
            graph_id,
            version_id,
            run_id,
            max_parent_bytes=max_parent_bytes,
        )
        matched: object = await connection.scalar(statement)
        verified = (
            matched is True
            and connection.get_transaction() is transaction
            and transaction.is_active
        )
    except (OSError, SQLAlchemyError):
        verified = False
    return _observed(verified)
