from dataclasses import dataclass
from enum import Enum

from sqlalchemy import (
    Executable,
    LargeBinary,
    String,
    case,
    column,
    false,
    func,
    literal_column,
    select,
    text,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql.elements import ColumnElement

from kora.db.connection import SNAPSHOT_NATIVE_ERRORS, Database
from kora.db.run_input_locator_v1 import SQLITE_RUN_INPUT_KEY
from kora.db.run_input_parent import (
    SQLITE_MAX_PARENT_ALLOWANCE,
    verify_run_input_parent,
)
from kora.db.tables import run_inputs
from kora.db.types import SQLITE

MIN_OBJECT_BYTES: int = 2
SQLITE_INPUTS = text("main.run_inputs INDEXED BY ix_run_inputs_locator")
SQLITE_PROJECTION = column("cel_projection", LargeBinary())
SQLITE_PARAMS = column("params", LargeBinary())
SQLITE_KEY = column("run_id", String()).collate("BINARY")


@dataclass(frozen=True, slots=True)
class RunInputBinding:
    namespace_id: str
    graph_id: str
    version_id: str
    run_id: str


@dataclass(frozen=True, slots=True)
class RunInputReadLimits:
    parent: int
    projection: int
    params: int


class RunInputField(Enum):
    PROJECTION = "projection"
    PARAMS = "params"


class _RunInputReadRefused(RuntimeError):
    pass


def _fits(
    dialect: str, value: ColumnElement[bytes], allowance: int
) -> ColumnElement[bool]:
    if dialect == SQLITE:
        return case(
            (
                func.typeof(value) == "blob",
                func.length(value).between(MIN_OBJECT_BYTES, allowance),
            ),
            else_=false(),
        )
    return func.pg_catalog.octet_length(value).between(MIN_OBJECT_BYTES, allowance)


def run_input_value_statement(
    dialect: str,
    run_id: str,
    field: RunInputField,
    limits: RunInputReadLimits,
) -> Executable:
    if dialect == SQLITE:
        projection_column, raw_column, key = (
            SQLITE_PROJECTION,
            SQLITE_PARAMS,
            SQLITE_KEY,
        )
    else:
        columns = run_inputs.c
        projection_column, raw_column, key = (
            columns.cel_projection,
            columns.params,
            columns.run_id,
        )
    projection = _fits(dialect, projection_column, limits.projection)
    if field is RunInputField.PROJECTION:
        value = case((projection, projection_column))
    else:
        raw = _fits(dialect, raw_column, limits.params)
        value = case((projection, case((raw, raw_column))))
    statement = select(value).where(key == run_id)
    if dialect == SQLITE:
        return statement.select_from(SQLITE_INPUTS).where(
            literal_column(SQLITE_RUN_INPUT_KEY)
        )
    return statement


async def acquire_run_input(
    database: Database,
    binding: RunInputBinding,
    field: RunInputField,
    limits: RunInputReadLimits,
) -> bytes | None:
    if type(field) is not RunInputField or not all(
        type(limit) is int and 0 < limit <= SQLITE_MAX_PARENT_ALLOWANCE
        for limit in (limits.parent, limits.projection, limits.params)
    ):
        return None
    namespace_id, graph_id, version_id, run_id = (
        binding.namespace_id,
        binding.graph_id,
        binding.version_id,
        binding.run_id,
    )
    limit = limits.projection if field is RunInputField.PROJECTION else limits.params
    admitted = RunInputReadLimits(limits.parent, limits.projection, limits.params)
    try:
        async with database.read_snapshot() as connection:
            if not await verify_run_input_parent(
                connection,
                namespace_id,
                graph_id,
                version_id,
                run_id,
                max_parent_bytes=admitted.parent,
            ):
                raise _RunInputReadRefused() from None
            try:
                payload: object = await connection.scalar(
                    run_input_value_statement(database.dialect, run_id, field, admitted)
                )
            except (OSError, SQLAlchemyError):
                raise _RunInputReadRefused() from None
    except (_RunInputReadRefused, *SNAPSHOT_NATIVE_ERRORS):
        return None
    if type(payload) is bytes and MIN_OBJECT_BYTES <= len(payload) <= limit:
        return payload
    return None
