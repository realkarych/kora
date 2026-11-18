import asyncio
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.approval_cost_reads import (
    COST_BATCH_ROWS,
    cost_attempt_statement,
    cost_attempt_value,
    cost_rows,
    cost_usage_statement,
    cost_usage_value,
)
from kora.db.connection import clear_snapshot_error
from kora.db.cost_reads import CostReadRefused
from kora.db.identifiers import is_identifier
from kora.db.tables import run
from kora.db.transactions import WriterTransactionError, raise_private_metadata_error
from kora.db.types import POSTGRESQL
from kora.engine.approval_cost_totals import ApprovalCostOverflow, ApprovalCostTotals
from kora.engine.locks import COMPONENT, LockError, TransitionLocks
from kora.engine.writer_mode import writable_postgres_transaction
from kora.ir.cost import RunCost
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

CALCULATED: EventSpec = CATALOG.register(
    EventSpec(
        "engine.approval_cost.calculated",
        COMPONENT,
        Level.DEBUG,
        "approval cost calculation finished",
        {"outcome": FieldKind.STRING},
    )
)


class ApprovalCostUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__("the approval cost is unavailable")


def record_cost(outcome: Literal["prepared", "unavailable", "overflow"]) -> None:
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(CALCULATED, outcome=outcome)


async def _calculate(
    connection: AsyncConnection,
    locks: TransitionLocks,
    namespace_id: str,
    run_id: str,
    as_of: datetime,
) -> RunCost:
    locks.require_connection(connection)
    locks.require(run_id)
    if (
        not is_identifier(namespace_id)
        or not is_identifier(run_id)
        or as_of.tzinfo is None
        or (
            connection.dialect.name == POSTGRESQL
            and not await writable_postgres_transaction(connection)
        )
    ):
        raise CostReadRefused()
    with await connection.execute(
        select(run.c.id).where(run.c.id == run_id, run.c.namespace_id == namespace_id)
    ) as result:
        if result.scalar_one_or_none() is None:
            raise CostReadRefused()
    totals = ApprovalCostTotals(as_of.astimezone(UTC))
    async with cost_rows(
        connection, cost_attempt_statement(connection, namespace_id, run_id)
    ) as rows:
        while batch := await rows.fetchmany(COST_BATCH_ROWS):
            for row in batch:
                totals.add_attempt(cost_attempt_value(row))
    async with cost_rows(
        connection, cost_usage_statement(connection, namespace_id, run_id)
    ) as rows:
        while batch := await rows.fetchmany(COST_BATCH_ROWS):
            for row in batch:
                totals.add_usage(cost_usage_value(row))
    return totals.value()


async def calculate_approval_cost(
    connection: AsyncConnection,
    locks: TransitionLocks,
    namespace_id: str,
    run_id: str,
    as_of: datetime,
) -> RunCost:
    failure: ApprovalCostUnavailable | ApprovalCostOverflow
    try:
        result = await _calculate(connection, locks, namespace_id, run_id, as_of)
        record_cost("prepared")
        return result
    except asyncio.CancelledError as error:
        clear_snapshot_error(error)
        raise
    except ApprovalCostOverflow:
        failure = ApprovalCostOverflow()
        record_cost("overflow")
    except (
        SQLAlchemyError,
        LockError,
        WriterTransactionError,
        ValueError,
        TypeError,
        ArithmeticError,
        RecursionError,
    ):
        failure = ApprovalCostUnavailable()
        record_cost("unavailable")
    finally:
        del connection, locks
    raise_private_metadata_error(failure)
