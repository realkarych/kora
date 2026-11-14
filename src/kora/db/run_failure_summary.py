import asyncio
from dataclasses import dataclass
from typing import Literal

from pydantic import TypeAdapter
from sqlalchemy import Executable, LargeBinary, case, cast, func, literal, select, true
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import ColumnElement

from kora.db.connection import clear_snapshot_error
from kora.db.tables import NON_TERMINAL_ATTEMPT_STATUSES, attempt, node_instance, run
from kora.db.transactions import raise_private_metadata_error
from kora.db.types import POSTGRESQL
from kora.ir import ErrorClass
from kora.ir.scalars import NodeId

FailureState = Literal["failed", "upstream_failed", "cancelled"]
FAILURE_STATES: tuple[FailureState, ...] = ("failed", "upstream_failed", "cancelled")
ATTEMPT_STATUSES: tuple[str, ...] = (
    *NON_TERMINAL_ATTEMPT_STATUSES,
    "succeeded",
    "failed",
    "cancelled",
    "lost",
)
ERROR_CLASSES: tuple[ErrorClass, ...] = tuple(ErrorClass)
NODE_ID: TypeAdapter[str] = TypeAdapter(NodeId)
ENCODINGS: tuple[str, ...] = ("utf-8", "utf-16-le", "utf-16-be")


class RunFailureSummaryUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__("run failure summary is unavailable")


@dataclass(frozen=True)
class FailedNode:
    node_path: str
    state: FailureState
    error_class: ErrorClass | None


@dataclass(frozen=True)
class RunFailureSummary:
    count: int
    sample: tuple[FailedNode, ...]


def encoding_code(connection: AsyncConnection) -> ColumnElement[int]:
    if connection.dialect.name == POSTGRESQL:
        return literal(1)
    encoding = func.pragma_encoding().table_valued("encoding")
    return select(
        case(
            {"UTF-8": 1, "UTF-16le": 2, "UTF-16be": 3},
            value=encoding.c.encoding,
            else_=0,
        )
    ).scalar_subquery()


def bounded_text_bytes(
    connection: AsyncConnection, value: ColumnElement[object], maximum: int
) -> ColumnElement[object]:
    if connection.dialect.name == POSTGRESQL:
        encoded = func.convert_to(value, "UTF8")
        admitted = func.octet_length(encoded) <= maximum
    else:
        encoded = cast(value, LargeBinary)
        admitted = (func.typeof(value) == "text") & (func.length(encoded) <= maximum)
    return case((admitted, encoded))


def failure_summary_statement(
    connection: AsyncConnection, namespace_id: str, run_id: str
) -> Executable:
    owner = (
        select(run.c.id)
        .where(run.c.namespace_id == namespace_id, run.c.id == run_id)
        .cte("failure_owner")
    )
    failures = (
        select(node_instance.c.id, node_instance.c.node_path, node_instance.c.status)
        .join(owner, owner.c.id == node_instance.c.run_id)
        .where(
            node_instance.c.top_level.is_(True),
            node_instance.c.status.in_(FAILURE_STATES),
        )
        .cte("failed_top_level")
    )
    path = failures.c.node_path
    order = (
        path.collate("C")
        if connection.dialect.name == POSTGRESQL
        else (path + func.char(0)).collate("RTRIM")
    )
    sample = (
        select(
            failures.c.id,
            path,
            case(
                dict(zip(FAILURE_STATES, (1, 2, 3), strict=True)),
                value=failures.c.status,
            ).label("state"),
            case((failures.c.status == "failed", 0), else_=1).label("rank"),
            order.label("sort_key"),
        )
        .order_by("rank", "sort_key")
        .limit(20)
        .cte("failure_sample")
    )
    status = case(
        {value: index for index, value in enumerate(ATTEMPT_STATUSES, 1)},
        value=attempt.c.status,
        else_=0,
    )
    error = case(
        (
            attempt.c.status == "failed",
            case(
                {value.value: index for index, value in enumerate(ERROR_CLASSES, 1)},
                value=attempt.c.error_class,
                else_=0,
            ),
        )
    )
    last_status = (
        select(status)
        .where(attempt.c.node_instance_id == sample.c.id)
        .order_by(attempt.c.number.desc())
        .limit(1)
        .correlate(sample)
        .scalar_subquery()
    )
    last_error = (
        select(error)
        .where(attempt.c.node_instance_id == sample.c.id)
        .order_by(attempt.c.number.desc())
        .limit(1)
        .correlate(sample)
        .scalar_subquery()
    )
    maximum = 64 if connection.dialect.name == POSTGRESQL else 128
    return (
        select(
            select(func.count()).select_from(failures).scalar_subquery(),
            sample.c.id.is_not(None),
            bounded_text_bytes(connection, sample.c.node_path, maximum),
            encoding_code(connection),
            sample.c.state,
            last_status,
            last_error,
        )
        .select_from(owner.outerjoin(sample, true()))
        .order_by(sample.c.rank, sample.c.sort_key)
    )


def decode_failed_node(values: tuple[object, ...]) -> FailedNode:
    encoded, encoding, state, status, error = values
    if (
        type(encoded) is not bytes
        or type(encoding) is not int
        or not 1 <= encoding <= len(ENCODINGS)
        or type(state) is not int
        or not 1 <= state <= len(FAILURE_STATES)
        or (
            status is not None
            and (type(status) is not int or not 1 <= status <= len(ATTEMPT_STATUSES))
        )
    ):
        raise ValueError("invalid failure summary scalar")
    path = NODE_ID.validate_python(encoded.decode(ENCODINGS[encoding - 1]), strict=True)
    failed = status is not None and ATTEMPT_STATUSES[status - 1] == "failed"
    if failed and (type(error) is not int or not 1 <= error <= len(ERROR_CLASSES)):
        raise ValueError("invalid applied error class")
    if not failed and error is not None:
        raise ValueError("unexpected applied error class")
    error_class = ERROR_CLASSES[error - 1] if type(error) is int else None
    return FailedNode(path, FAILURE_STATES[state - 1], error_class)


async def read_run_failure_summary(
    connection: AsyncConnection, namespace_id: str, run_id: str
) -> RunFailureSummary | None:
    rows: list[tuple[object, ...]] = []
    try:
        with await connection.execute(
            failure_summary_statement(connection, namespace_id, run_id)
        ) as result:
            rows = [tuple(row) for row in result]
        if not rows:
            return None
        count = rows[0][0]
        if type(count) is not int or count < 0 or len(rows) > 20:
            raise ValueError("invalid failure count")
        sample = tuple(decode_failed_node(row[2:]) for row in rows if row[1] is True)
        if len(sample) != min(count, 20):
            raise ValueError("incomplete failure sample")
        return RunFailureSummary(count, sample)
    except asyncio.CancelledError as error:
        clear_snapshot_error(error)
        raise
    except (SQLAlchemyError, ValueError, TypeError, OverflowError, RecursionError):
        failure = RunFailureSummaryUnavailable()
    finally:
        del rows
    raise_private_metadata_error(failure)
