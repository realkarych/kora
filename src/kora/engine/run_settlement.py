import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from enum import StrEnum

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError

from kora.db.connection import clear_snapshot_error
from kora.db.identifiers import is_identifier
from kora.db.reads import has_physical_attempt, has_unfinished_top_level_instance
from kora.db.run_failure_summary import (
    ENCODINGS,
    RunFailureSummary,
    RunFailureSummaryUnavailable,
    bounded_text_bytes,
    encoding_code,
    read_run_failure_summary,
)
from kora.db.tables import run
from kora.db.transactions import raise_private_metadata_error
from kora.db.types import (
    POSTGRESQL,
    JsonObject,
    JsonValue,
    as_json_value,
    normalise_timestamp,
)
from kora.engine.completion import decide_run_completion
from kora.engine.completion_models import RunCompletionFacts, RunTerminalDecision
from kora.engine.expiry import expire_run, observe_run_expiry
from kora.engine.expiry_context import ExpiryContext, ExpiryFailed, ExpiryState
from kora.engine.expiry_reads import read_expiry_run, timeout_reached
from kora.engine.locks import COMPONENT, Scope, ScopeKind
from kora.engine.writer_mode import writable_postgres_transaction
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

PREPARED: EventSpec = CATALOG.register(
    EventSpec(
        "engine.run_settlement.prepared",
        COMPONENT,
        Level.DEBUG,
        "run settlement prepared for commit",
        {"outcome": FieldKind.STRING},
    )
)


class RunSettlementState(StrEnum):
    UNCHANGED = "unchanged"
    SETTLED = "settled"
    EXPIRED = "expired"
    REFUSED = "refused"


class RunSettlementFailed(RuntimeError):
    def __init__(self) -> None:
        super().__init__("run settlement failed")


@asynccontextmanager
async def settlement_transition(context: ExpiryContext) -> AsyncGenerator[None]:
    try:
        context.locks.require_connection(context.connection)
        context.require_capacity()
        if not is_identifier(context.run_id) or (
            context.connection.dialect.name == POSTGRESQL
            and not await writable_postgres_transaction(context.connection)
        ):
            raise ValueError("settlement requires a writable authority transaction")
        owner = select(run.c.id).where(
            run.c.id == context.run_id,
            run.c.namespace_id == context.scope.namespace_id,
            run.c.graph_id == context.scope.graph_id,
            run.c.graph_version_id == context.scope.version_id,
        )
        if await context.connection.scalar(owner) is None:
            raise ValueError("settlement requires the original run scope")
        yield
        return
    except asyncio.CancelledError as error:
        clear_snapshot_error(error)
        raise
    except (
        SQLAlchemyError,
        ValueError,
        TypeError,
        RecursionError,
        OverflowError,
        RunFailureSummaryUnavailable,
        ExpiryFailed,
    ):
        failure = RunSettlementFailed()
    finally:
        del context
    raise_private_metadata_error(failure)


async def releasable_target(context: ExpiryContext) -> bool:
    if await has_physical_attempt(context.connection, context.run_id):
        return False
    with await context.connection.execute(
        select(
            run.c.target_held_since.is_not(None),
            bounded_text_bytes(
                context.connection, run.c.target_key, context.limits.parent_bytes
            ),
            encoding_code(context.connection),
        ).where(run.c.id == context.run_id)
    ) as result:
        held, encoded, encoding = tuple(result.one())
    if not held:
        return False
    if (
        type(encoded) is not bytes
        or type(encoding) is not int
        or not 1 <= encoding <= len(ENCODINGS)
    ):
        raise ValueError("target identity is unavailable")
    key = encoded.decode(ENCODINGS[encoding - 1])
    context.locks.require(
        context.run_id,
        (Scope(ScopeKind.TARGET, (context.scope.namespace_id, key)),),
    )
    return True


def failure_payload(summary: RunFailureSummary) -> JsonObject:
    sample: list[JsonValue] = []
    for node in summary.sample:
        item: JsonObject = {"node_path": node.node_path, "state": node.state}
        if node.error_class is not None:
            item["error_class"] = node.error_class.value
        sample.append(item)
    return {"count": summary.count, "sample": sample}


async def settle_expired_run(
    context: ExpiryContext, release: bool
) -> RunSettlementState:
    if await expire_run(context) is not ExpiryState.EXPIRED:
        return RunSettlementState.REFUSED
    if release:
        await context.connection.execute(
            update(run).where(run.c.id == context.run_id).values(target_held_since=None)
        )
    return RunSettlementState.EXPIRED


async def _settle_run(context: ExpiryContext) -> RunSettlementState:
    saved = await read_expiry_run(context)
    if saved is None:
        return RunSettlementState.REFUSED
    if saved.status in ("succeeded", "failed", "cancelled"):
        if not await releasable_target(context):
            return RunSettlementState.UNCHANGED
        await context.connection.execute(
            update(run)
            .where(run.c.id == context.run_id)
            .values(
                target_held_since=None,
                needs_scheduling=True,
                updated_at=normalise_timestamp(context.clock()),
            )
        )
        return RunSettlementState.SETTLED
    if saved.status != "running":
        return (
            RunSettlementState.UNCHANGED
            if saved.status == "pending"
            else RunSettlementState.REFUSED
        )
    state = await observe_run_expiry(context)
    if state is ExpiryState.EXPIRED:
        return await settle_expired_run(context, await releasable_target(context))
    if state is not ExpiryState.ACTIVE:
        return RunSettlementState.REFUSED
    if await has_unfinished_top_level_instance(context.connection, context.run_id):
        return RunSettlementState.UNCHANGED
    summary = await read_run_failure_summary(
        context.connection, context.scope.namespace_id, context.run_id
    )
    if summary is None:
        return RunSettlementState.REFUSED
    release = await releasable_target(context)
    now = normalise_timestamp(context.clock())
    if saved.started_at is None:
        return RunSettlementState.REFUSED
    if timeout_reached(saved.started_at, now, saved.limits.timeout):
        await observe_run_expiry(context, observed_at=now)
        return await settle_expired_run(context, release)
    decision = decide_run_completion(
        RunCompletionFacts(
            context.run_id,
            "running",
            now,
            None,
            None,
            True,
            True,
            False,
            summary.count > 0,
        )
    )
    if not isinstance(decision, RunTerminalDecision):
        raise ValueError("checked run completion did not produce a terminal decision")
    values: dict[str, object] = {
        "status": decision.status,
        "finished_at": now,
        "updated_at": now,
        "needs_scheduling": True,
    }
    if release:
        values["target_held_since"] = None
    await context.connection.execute(
        update(run).where(run.c.id == context.run_id).values(**values)
    )
    payload: JsonObject = {
        "trigger": as_json_value(saved.trigger.model_dump(mode="json")),
        "graph_version": context.scope.version_id,
        "rerun_of": saved.rerun_of_run_id,
    }
    if decision.status == "failed":
        payload["failed_nodes"] = failure_payload(summary)
    await context.event(f"run.{decision.status}", payload, now)
    return RunSettlementState.SETTLED


async def settle_run(context: ExpiryContext) -> RunSettlementState:
    async with settlement_transition(context):
        result = await _settle_run(context)
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(PREPARED, outcome=result.value)
    return result
