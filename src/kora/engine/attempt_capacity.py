import asyncio
from dataclasses import dataclass
from typing import Literal

from pydantic import MISSING
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from kora.cel_eval.errors import raise_safe
from kora.db.attempt_capacity import (
    RunRecovery,
    read_attempt_capacity,
    read_graph_cap_source,
    read_run_recovery,
)
from kora.db.connection import clear_snapshot_error
from kora.db.graph_value_json import TextBudget
from kora.db.run_input_parent import verify_run_input_parent
from kora.db.tables import namespace
from kora.db.types import POSTGRESQL
from kora.engine.expiry_context import ExpiryContext
from kora.engine.graph_execution_contract import CheckedExecutionContract
from kora.engine.graph_resource_references import GraphResourceScope
from kora.engine.graph_types import read_document
from kora.engine.locks import COMPONENT
from kora.engine.namespace_policy import running_attempt_limit
from kora.engine.writer_mode import writable_postgres_transaction
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

CHECKED: EventSpec = CATALOG.register(
    EventSpec(
        "engine.attempt_capacity.checked",
        COMPONENT,
        Level.DEBUG,
        "physical attempt capacity observed",
        {"outcome": FieldKind.STRING},
    )
)


class AttemptCapacityRefused(ValueError):
    def __init__(self) -> None:
        super().__init__("physical attempt capacity is unavailable")


@dataclass(frozen=True, slots=True, repr=False)
class PreparedGraphCaps:
    scope: GraphResourceScope
    nodes: int | None
    runs: int | None


@dataclass(frozen=True, slots=True, repr=False)
class CapacityLimit:
    scope: Literal["global", "namespace", "graph", "runtime"]
    name: str
    limit: int
    used: int
    source_graph_version_id: str | None


def prepare_graph_caps(
    checked: CheckedExecutionContract, *, max_ir_bytes: int
) -> PreparedGraphCaps:
    try:
        concurrency = read_document(
            checked.canonical_ir, TextBudget(max_ir_bytes)
        ).runtime.concurrency
        return PreparedGraphCaps(
            checked.scope,
            None
            if concurrency is MISSING or concurrency.max_running_nodes is MISSING
            else concurrency.max_running_nodes,
            None
            if concurrency is MISSING or concurrency.max_running_runs is MISSING
            else concurrency.max_running_runs,
        )
    except ValueError:
        del checked
        raise_safe(AttemptCapacityRefused())


async def _check(
    context: ExpiryContext,
    runtime: str,
    caps: PreparedGraphCaps,
    global_limit: int | None,
    runtime_limit: int | None,
) -> CapacityLimit | RunRecovery | None:
    connection, scope = context.connection, context.scope
    context.locks.require_connection(connection)
    context.require_capacity(runtime)
    if any(
        value is not None and (type(value) is not int or value <= 0)
        for value in (global_limit, runtime_limit)
    ) or (caps.scope.namespace_id, caps.scope.graph_id) != (
        scope.namespace_id,
        scope.graph_id,
    ):
        raise AttemptCapacityRefused()
    if (
        connection.dialect.name == POSTGRESQL
        and not await writable_postgres_transaction(connection)
    ) or not await verify_run_input_parent(
        connection,
        scope.namespace_id,
        scope.graph_id,
        scope.version_id,
        context.run_id,
        max_parent_bytes=context.limits.parent_bytes,
    ):
        raise AttemptCapacityRefused()
    recovery = await read_run_recovery(connection, context.run_id)
    if recovery.count:
        return recovery
    source = await read_graph_cap_source(connection, scope.graph_id)
    if source is None or source.version_id != caps.scope.version_id:
        raise AttemptCapacityRefused()
    policy = await connection.scalar(
        select(namespace.c.policy).where(namespace.c.id == scope.namespace_id)
    )
    namespace_limit = running_attempt_limit(policy)
    used = await read_attempt_capacity(
        connection,
        namespace_id=scope.namespace_id,
        graph_id=scope.graph_id,
        run_id=context.run_id,
        runtime=runtime,
    )
    limits: tuple[
        tuple[Literal["global", "namespace", "graph", "runtime"], str, int | None, int],
        ...,
    ] = (
        ("global", "max_running_attempts", global_limit, used.global_attempts),
        ("namespace", "max_running_attempts", namespace_limit, used.namespace_attempts),
        ("graph", "max_running_nodes", caps.nodes, used.graph_attempts),
        (
            "graph",
            "max_running_runs",
            None if used.run_attempts else caps.runs,
            used.graph_runs,
        ),
        ("runtime", "max_concurrent", runtime_limit, used.runtime_attempts),
    )
    for kind, name, limit, occupied in limits:
        if limit is not None and occupied >= limit:
            return CapacityLimit(
                kind,
                name,
                limit,
                occupied,
                source.version_id if kind == "graph" else None,
            )
    return None


async def check_attempt_capacity(
    context: ExpiryContext,
    runtime: str,
    caps: PreparedGraphCaps,
    *,
    global_limit: int | None,
    runtime_limit: int | None,
) -> CapacityLimit | RunRecovery | None:
    try:
        result = await _check(context, runtime, caps, global_limit, runtime_limit)
    except asyncio.CancelledError as error:
        clear_snapshot_error(error)
        raise
    except (SQLAlchemyError, ValueError, TypeError, RecursionError, OverflowError):
        del context, runtime, caps, global_limit, runtime_limit
        raise_safe(AttemptCapacityRefused())
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(
            CHECKED,
            outcome="room"
            if result is None
            else "execution_recovery"
            if isinstance(result, RunRecovery)
            else "concurrency_limit",
        )
    return result
