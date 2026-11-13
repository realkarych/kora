from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.cost_reads import (
    SOURCE_BYTE_LIMIT,
    CostReadRefused,
    HistoricalState,
    admit_projection,
    payload_projection,
    required_text,
    usage_projection,
    usage_source_value,
    usage_value,
)
from kora.db.identifiers import is_identifier
from kora.db.tables import attempt, node_instance, run, usage_record
from kora.ir.cost import UsageRow, format_money

MAX_SAVED_USAGE_ROWS: int = 64


@dataclass(frozen=True, repr=False)
class StoredUsage:
    row: UsageRow
    usd_estimate: str | None
    dispatcher_id: str
    same_basis_usd_floor: str | None


def stored_usage(row: RowMapping) -> StoredUsage:
    admit_projection(row)
    saved = usage_value(row)
    if saved.state is not HistoricalState.VERSION_1:
        raise CostReadRefused()
    fields = {
        key: value
        for key, value in saved.fields.items()
        if key in UsageRow.model_fields
    }
    fields["source"] = usage_source_value(row)
    try:
        normalized = UsageRow.model_validate(fields)
        raw_floor = row["same_basis_usd_floor"]
        floor = (
            None
            if raw_floor is None
            else format_money(Decimal(required_text(raw_floor)))
        )
        estimate = (
            None if saved.usd_estimate is None else format_money(saved.usd_estimate)
        )
        dispatcher = required_text(row["dispatcher_id"])
        if (
            not is_identifier(dispatcher)
            or estimate is not None
            and floor != estimate
            or normalized.billing_class == "subscription"
            and floor is not None
        ):
            raise CostReadRefused()
    except (ValueError, ArithmeticError) as error:
        raise CostReadRefused() from error
    return StoredUsage(normalized, estimate, dispatcher, floor)


async def read_attempt_usage(
    connection: AsyncConnection, namespace_id: str, attempt_id: str
) -> tuple[StoredUsage, ...]:
    node_id = (
        select(attempt.c.node_instance_id)
        .where(attempt.c.id == attempt_id)
        .scalar_subquery()
    )
    run_id = (
        select(node_instance.c.run_id)
        .where(node_instance.c.id == node_id)
        .scalar_subquery()
    )
    scope = select(run.c.namespace_id).where(run.c.id == run_id).scalar_subquery()
    statement = (
        select(
            *usage_projection(connection),
            *payload_projection(connection, usage_record.c.source, SOURCE_BYTE_LIMIT),
            *payload_projection(connection, usage_record.c.same_basis_usd_floor, 128),
        )
        .where(scope == namespace_id, usage_record.c.attempt_id == attempt_id)
        .order_by(usage_record.c.model)
        .limit(MAX_SAVED_USAGE_ROWS + 1)
    )
    result = await connection.execute(statement)
    try:
        rows = result.mappings().all()
    finally:
        result.close()
    if len(rows) > MAX_SAVED_USAGE_ROWS:
        raise CostReadRefused()
    return tuple(stored_usage(row) for row in rows)
