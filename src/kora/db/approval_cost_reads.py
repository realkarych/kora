from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, StrictBool
from sqlalchemy import Executable, select
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncMappingResult, AsyncResult
from sqlalchemy.sql.elements import ColumnElement

from kora.db.connection import complete, finish_releasing
from kora.db.cost_reads import (
    COVERAGE_BYTE_LIMIT,
    CostReadRefused,
    HistoricalState,
    admit_projection,
    attempt_run_join,
    payload,
    payload_projection,
    scalar_projection,
    stored_timestamp,
    usage_projection,
    usage_value,
)
from kora.db.tables import attempt, node_instance, run, usage_record
from kora.ir.cost import CostValue, Timestamp, UsageCoverage, UsageRow

COST_BATCH_ROWS: int = 128


class CostLifecycle(CostValue):
    status: Literal[
        "created",
        "claimed",
        "submitted",
        "running",
        "finishing",
        "succeeded",
        "failed",
        "cancelled",
        "lost",
    ]
    execution_state: Literal["not_started", "live", "unknown", "stopped"]
    recovery_required: StrictBool

    @property
    def unsettled(self) -> bool:
        return (
            self.status in {"created", "claimed", "submitted", "running", "finishing"}
            or self.execution_state != "stopped"
            or self.recovery_required
        )


class CostAttempt(CostLifecycle):
    kind: Literal["agent", "command"]
    started_at: Timestamp | None
    lease_epoch: Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
    domain_recorded: StrictBool
    worker_recorded: StrictBool
    has_usage: StrictBool
    coverage: UsageCoverage | None


@dataclass(frozen=True)
class CostUsage:
    usage: UsageRow
    amount: Decimal | None
    lifecycle: CostLifecycle


def lifecycle_projection(
    connection: AsyncConnection,
) -> Sequence[ColumnElement[object] | ColumnElement[int]]:
    return [
        *scalar_projection(connection, attempt.c.status),
        *scalar_projection(connection, attempt.c.execution_state),
        attempt.c.recovery_required,
    ]


def cost_attempt_statement(
    connection: AsyncConnection, namespace_id: str, run_id: str
) -> Executable:
    has_usage = (
        select(usage_record.c.id)
        .where(usage_record.c.attempt_id == attempt.c.id)
        .exists()
    )
    return (
        select(
            *scalar_projection(connection, node_instance.c.kind),
            *lifecycle_projection(connection),
            *payload_projection(connection, attempt.c.started_at, 128),
            *scalar_projection(connection, attempt.c.lease_epoch),
            attempt.c.execution_domain_id.is_not(None).label("domain_recorded"),
            attempt.c.worker_id.is_not(None).label("worker_recorded"),
            has_usage.label("has_usage"),
            *payload_projection(
                connection, attempt.c.usage_coverage, COVERAGE_BYTE_LIMIT
            ),
        )
        .select_from(attempt_run_join(attempt))
        .where(run.c.namespace_id == namespace_id, run.c.id == run_id)
    )


def cost_attempt_value(row: RowMapping) -> CostAttempt:
    admit_projection(row)
    return CostAttempt.model_validate(
        {
            "kind": row["kind"],
            "status": row["status"],
            "execution_state": row["execution_state"],
            "recovery_required": row["recovery_required"],
            "started_at": stored_timestamp(row["started_at"]),
            "lease_epoch": row["lease_epoch"],
            "domain_recorded": row["domain_recorded"],
            "worker_recorded": row["worker_recorded"],
            "has_usage": row["has_usage"],
            "coverage": payload(row, "usage_coverage", COVERAGE_BYTE_LIMIT),
        }
    )


def cost_usage_statement(
    connection: AsyncConnection, namespace_id: str, run_id: str
) -> Executable:
    return (
        select(*usage_projection(connection), *lifecycle_projection(connection))
        .select_from(attempt_run_join(usage_record.join(attempt)))
        .where(run.c.namespace_id == namespace_id, run.c.id == run_id)
    )


def cost_usage_value(row: RowMapping) -> CostUsage:
    saved = usage_value(row)
    if saved.state is not HistoricalState.VERSION_1:
        raise CostReadRefused()
    usage = UsageRow.model_validate(
        {
            **{
                key: value
                for key, value in saved.fields.items()
                if key in UsageRow.model_fields
            },
            "source": {},
        }
    )
    if usage.billing_class == "subscription" and saved.usd_estimate is not None:
        raise CostReadRefused()
    lifecycle = CostLifecycle.model_validate(
        {key: row[key] for key in CostLifecycle.model_fields}
    )
    return CostUsage(usage, saved.usd_estimate, lifecycle)


async def close_cost_result(result: AsyncResult[*tuple[object, ...]]) -> None:
    await result.close()


@asynccontextmanager
async def cost_rows(
    connection: AsyncConnection, statement: Executable
) -> AsyncGenerator[AsyncMappingResult]:
    result = await finish_releasing(
        connection.stream(statement, execution_options={"yield_per": COST_BATCH_ROWS}),
        close_cost_result,
    )
    try:
        yield result.mappings()
    finally:
        await complete(result.close())
