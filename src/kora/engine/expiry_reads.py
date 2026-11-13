import re
from datetime import datetime

from sqlalchemy import select

from kora.db.tables import attempt, run
from kora.db.types import SQLITE
from kora.engine.deadline_records import duration_us
from kora.engine.expiry_context import ExpiryContext, bounded_json
from kora.engine.run_promotion_read import (
    ManualPromotionHeader,
    admitted_promotion_receipt,
    sqlite_record_width,
)
from kora.ir.scalars import Duration
from kora.protocols.boundary import BoundaryModel
from kora.protocols.identity_records import Identifier

UNITS: dict[str, int] = {
    "ms": 1000,
    "s": 1_000_000,
    "m": 60_000_000,
    "h": 3_600_000_000,
    "d": 86_400_000_000,
}


async def admit_attempt_read(context: ExpiryContext, attempt_id: str) -> bool:
    if context.connection.dialect.name != SQLITE:
        return True
    return (
        await context.connection.scalar(
            select(sqlite_record_width(list(attempt.c)) <= context.attempt_bytes).where(
                attempt.c.id == attempt_id
            )
        )
        is True
    )


def timeout_reached(start: datetime, now: datetime, timeout: str | None) -> bool:
    if timeout is None or now < start:
        return False
    match = re.fullmatch(r"([0-9]+)(ms|s|m|h|d)", timeout)
    if match is None:
        raise ValueError("the run timeout is invalid")
    digits, unit = match.groups()
    digits = digits.lstrip("0") or "0"
    elapsed = str(duration_us(now - start) // UNITS[unit])
    return len(digits) < len(elapsed) or (
        len(digits) == len(elapsed) and digits <= elapsed
    )


class RunLimits(BoundaryModel):
    timeout: Duration | None = None


class ExpiryRun(BoundaryModel):
    status: str
    started_at: datetime | None
    deadline_expired_at: datetime | None
    rerun_of_run_id: Identifier | None
    trigger: ManualPromotionHeader
    limits: RunLimits


async def read_expiry_run(context: ExpiryContext) -> ExpiryRun | None:
    connection, scope, limits = context.connection, context.scope, context.limits
    receipt = await admitted_promotion_receipt(
        connection, scope, context.run_id, limits
    )
    if receipt is None:
        return None
    if connection.dialect.name == SQLITE:
        fits = await connection.scalar(
            select(sqlite_record_width(list(run.c)) <= limits.parent_bytes).where(
                run.c.id == context.run_id
            )
        )
        if fits is not True:
            return None
    with await connection.execute(
        select(
            run.c.status,
            run.c.started_at,
            run.c.deadline_expired_at,
            run.c.rerun_of_run_id,
            bounded_json(run.c.trigger, limits.parent_bytes).label("trigger"),
            bounded_json(run.c.limits, limits.parent_bytes).label("limits"),
        ).where(
            run.c.id == context.run_id,
            run.c.namespace_id == scope.namespace_id,
            run.c.graph_id == scope.graph_id,
            run.c.graph_version_id == scope.version_id,
        )
    ) as result:
        row = result.mappings().one_or_none()
    if row is None:
        return None
    try:
        saved = ExpiryRun.model_validate(dict(row))
    except ValueError:
        return None
    return saved if saved.trigger.occurrence_key == receipt else None
