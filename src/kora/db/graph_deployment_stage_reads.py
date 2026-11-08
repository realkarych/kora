from dataclasses import dataclass

from sqlalchemy import (
    CheckConstraint,
    Executable,
    LargeBinary,
    and_,
    case,
    func,
    select,
)
from sqlalchemy import cast as sql_cast
from sqlalchemy.exc import StatementError
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import ColumnElement, Grouping
from sqlalchemy.sql.selectable import Subquery

from kora.db.graph_deployment_stage_v1 import (
    MAX_STAGE_FILE_BYTES,
    MAX_STAGE_OCCURRENCES,
    occurrence_checks,
    stage_checks,
)
from kora.db.identifiers import IDENTIFIER_LENGTH, is_identifier
from kora.db.tables import graph_deployment_stage as stage_table
from kora.db.tables import graph_deployment_stage_occurrence as occurrence_table
from kora.db.transactions import raise_private_metadata_error
from kora.db.types import POSTGRESQL, SQLITE

STORE_ID_WIDTH: int = 256
PAYLOAD_DIGEST_WIDTH: int = 71


class GraphStageInventoryRejected(RuntimeError):
    def __init__(self) -> None:
        super().__init__("graph deployment stage inventory rejected")


class GraphStageInventoryReadFailed(RuntimeError):
    def __init__(self) -> None:
        super().__init__("graph deployment stage inventory read failed")


@dataclass(frozen=True, repr=False)
class GraphStageScope:
    preparation_id: str
    namespace_id: str


@dataclass(frozen=True, repr=False)
class GraphStage:
    stage_id: str
    namespace_id: str
    preparation_id: str
    store_id: str
    payload_digest: str
    fence_incarnation: int
    total_size: int


@dataclass(frozen=True, repr=False)
class GraphStageOccurrence:
    stage_id: str
    namespace_id: str
    preparation_id: str
    ordinal: int


@dataclass(frozen=True, repr=False)
class GraphStageInventory:
    stages: tuple[GraphStage, ...]
    occurrences: tuple[GraphStageOccurrence, ...]


def _projection(
    source: Subquery,
    checks: tuple[CheckConstraint, ...],
    scope: GraphStageScope,
    dialect: str,
    allowance: int,
    text_widths: tuple[tuple[str, int], ...],
    order: tuple[str, ...],
) -> Executable:
    fields = source.c
    valid = and_(
        func.count().over() <= allowance,
        fields.namespace_id == scope.namespace_id,
        fields.preparation_id == scope.preparation_id,
        *(Grouping[bool](check.sqltext) for check in checks),
    )
    if dialect == SQLITE:
        valid = and_(
            valid,
            *(
                func.length(sql_cast(fields[name], LargeBinary)) <= 2 * width
                for name, width in text_widths
            ),
        )
    return (
        select(*(case((valid, field), else_=None) for field in fields))
        .select_from(source)
        .order_by(*(fields[name] for name in order))
    )


def _stages_statement(dialect: str, scope: GraphStageScope) -> Executable:
    fields = stage_table.c
    bounded = (
        select(
            fields.stage_id,
            fields.namespace_id,
            fields.preparation_id,
            fields.store_id,
            fields.payload_digest,
            fields.fence_incarnation,
            fields.total_size,
        )
        .where(fields.preparation_id == scope.preparation_id)
        .order_by(fields.stage_id)
        .limit(MAX_STAGE_OCCURRENCES + 1)
        .subquery()
    )
    return _projection(
        bounded,
        stage_checks(dialect),
        scope,
        dialect,
        MAX_STAGE_OCCURRENCES,
        (
            ("stage_id", IDENTIFIER_LENGTH),
            ("namespace_id", IDENTIFIER_LENGTH),
            ("preparation_id", IDENTIFIER_LENGTH),
            ("store_id", STORE_ID_WIDTH),
            ("payload_digest", PAYLOAD_DIGEST_WIDTH),
        ),
        ("stage_id",),
    )


def _occurrences_statement(
    dialect: str, scope: GraphStageScope, stage_id: str | None, allowance: int
) -> Executable:
    fields = occurrence_table.c
    order = ("ordinal",) if stage_id is None else ("preparation_id", "ordinal")
    predicate: ColumnElement[bool] = (
        fields.preparation_id == scope.preparation_id
        if stage_id is None
        else fields.stage_id == stage_id
    )
    bounded = (
        select(
            fields.stage_id, fields.namespace_id, fields.preparation_id, fields.ordinal
        )
        .where(predicate)
        .order_by(*(fields[name] for name in order))
        .limit(allowance + 1)
        .subquery()
    )
    return _projection(
        bounded,
        occurrence_checks(dialect),
        scope,
        dialect,
        allowance,
        (
            ("stage_id", IDENTIFIER_LENGTH),
            ("namespace_id", IDENTIFIER_LENGTH),
            ("preparation_id", IDENTIFIER_LENGTH),
        ),
        order,
    )


async def _rows(
    connection: AsyncConnection, statement: Executable, allowance: int
) -> tuple[tuple[object, ...], ...]:
    rows: tuple[tuple[object, ...], ...] | None
    try:
        result = await connection.execute(statement)
        try:
            rows = tuple(tuple(row) for row in result.all())
        finally:
            result.close()
    except StatementError:
        rows = None
    if rows is None:
        raise_private_metadata_error(GraphStageInventoryReadFailed())
    if len(rows) > allowance:
        raise_private_metadata_error(GraphStageInventoryRejected())
    return rows


def _scope_fields(
    row: tuple[object, ...], scope: GraphStageScope
) -> tuple[str, str, str]:
    stage_id, namespace_id, preparation_id = row[:3]
    if (
        type(stage_id) is not str
        or not is_identifier(stage_id)
        or type(namespace_id) is not str
        or namespace_id != scope.namespace_id
        or type(preparation_id) is not str
        or preparation_id != scope.preparation_id
    ):
        raise_private_metadata_error(GraphStageInventoryRejected())
    return stage_id, namespace_id, preparation_id


def _stage(row: tuple[object, ...], scope: GraphStageScope) -> GraphStage:
    scope_fields = _scope_fields(row, scope)
    store_id, payload_digest, fence_incarnation, total_size = row[3:]
    if (
        type(store_id) is not str
        or type(payload_digest) is not str
        or type(fence_incarnation) is not int
        or not 0 < fence_incarnation < 2**63
        or type(total_size) is not int
        or not 0 <= total_size <= MAX_STAGE_FILE_BYTES
    ):
        raise_private_metadata_error(GraphStageInventoryRejected())
    return GraphStage(
        *scope_fields, store_id, payload_digest, fence_incarnation, total_size
    )


def _occurrence(
    row: tuple[object, ...], scope: GraphStageScope
) -> GraphStageOccurrence:
    scope_fields = _scope_fields(row, scope)
    ordinal = row[3]
    if type(ordinal) is not int or not 0 <= ordinal < MAX_STAGE_OCCURRENCES:
        raise_private_metadata_error(GraphStageInventoryRejected())
    return GraphStageOccurrence(*scope_fields, ordinal)


async def read_graph_stage_inventory(
    connection: AsyncConnection, scope: GraphStageScope
) -> GraphStageInventory:
    dialect = connection.dialect.name
    if (
        dialect not in (SQLITE, POSTGRESQL)
        or type(scope) is not GraphStageScope
        or type(scope.preparation_id) is not str
        or not is_identifier(scope.preparation_id)
        or type(scope.namespace_id) is not str
        or not is_identifier(scope.namespace_id)
    ):
        raise_private_metadata_error(GraphStageInventoryRejected())
    limit = MAX_STAGE_OCCURRENCES
    stage_rows = await _rows(connection, _stages_statement(dialect, scope), limit)
    stages = tuple(_stage(row, scope) for row in stage_rows)
    stage_ids = {stage.stage_id for stage in stages}
    addresses = {(stage.store_id, stage.payload_digest) for stage in stages}
    if len(stage_ids) != len(stages) or len(addresses) != len(stages):
        raise_private_metadata_error(GraphStageInventoryRejected())
    preparation_rows = await _rows(
        connection, _occurrences_statement(dialect, scope, None, limit), limit
    )
    occurrences = tuple(_occurrence(row, scope) for row in preparation_rows)
    expected = {entry.ordinal: entry for entry in occurrences}
    if (
        len(expected) != len(occurrences)
        or {entry.stage_id for entry in occurrences} != stage_ids
    ):
        raise_private_metadata_error(GraphStageInventoryRejected())
    independent: dict[int, GraphStageOccurrence] = {}
    for stage in stages:
        remaining = limit - len(independent)
        rows = await _rows(
            connection,
            _occurrences_statement(dialect, scope, stage.stage_id, remaining),
            remaining,
        )
        if not rows:
            raise_private_metadata_error(GraphStageInventoryRejected())
        for row in rows:
            entry = _occurrence(row, scope)
            if entry.stage_id != stage.stage_id or entry.ordinal in independent:
                raise_private_metadata_error(GraphStageInventoryRejected())
            independent[entry.ordinal] = entry
    if expected != independent:
        raise_private_metadata_error(GraphStageInventoryRejected())
    return GraphStageInventory(stages, occurrences)
