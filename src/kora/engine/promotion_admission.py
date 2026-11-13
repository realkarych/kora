import math
from collections.abc import Mapping
from datetime import datetime
from typing import cast

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.tables import inbound_event, run, run_promotion_admission
from kora.engine.graph_resource_references import GraphResourceScope
from kora.engine.locks import COMPONENT, Scope, ScopeKind, TransitionLocks
from kora.observability import CATALOG, EventSpec, Level, current_diagnostics

PREPARED: EventSpec = CATALOG.register(
    EventSpec(
        "engine.promotion_admission.prepared",
        COMPONENT,
        Level.DEBUG,
        "manual promotion admission prepared for commit",
        {},
    )
)


def _native_bound(value: object) -> int:
    if value is None:
        return 64
    if type(value) is bool:
        return 32
    if type(value) is datetime:
        return 64
    if type(value) is int:
        return 64 + 2 * len(str(value))
    if type(value) is float and math.isfinite(value):
        return 1024
    if type(value) is str:
        return 64 + 12 * len(value.encode("utf-8"))
    if type(value) is dict:
        fields = cast(dict[object, object], value)
        if not all(type(key) is str for key in fields):
            raise ValueError("promotion admission requires string member names")
        return 64 + sum(
            32 + _native_bound(key) + _native_bound(child)
            for key, child in fields.items()
        )
    if type(value) is list:
        return 64 + sum(
            32 + _native_bound(child) for child in cast(list[object], value)
        )
    raise ValueError("promotion admission requires database values")


def record_bound(values: Mapping[str, object], columns: tuple[str, ...]) -> int:
    if not values.keys() <= set(columns):
        raise ValueError("promotion admission contains unknown columns")
    bound = 9 * (len(columns) + 1) + sum(
        _native_bound(values.get(column)) for column in columns
    )
    if bound >= 2**63:
        raise ValueError("promotion admission exceeds native integer capacity")
    return bound


async def record_manual_promotion_admission(
    connection: AsyncConnection,
    locks: TransitionLocks,
    scope: GraphResourceScope,
    run_values: Mapping[str, object],
    receipt_values: Mapping[str, object],
) -> None:
    run_id, receipt_id = run_values.get("id"), receipt_values.get("id")
    if type(run_id) is not str or type(receipt_id) is not str:
        raise ValueError("promotion admission requires exact identities")
    locks.require_connection(connection)
    locks.require(
        run_id,
        (
            Scope(ScopeKind.NAMESPACE, (scope.namespace_id,)),
            Scope(ScopeKind.GRAPH, (scope.graph_id,)),
        ),
    )
    if (
        run_values.get("namespace_id"),
        run_values.get("graph_id"),
        run_values.get("graph_version_id"),
        receipt_values.get("namespace_id"),
    ) != (scope.namespace_id, scope.graph_id, scope.version_id, scope.namespace_id):
        raise ValueError("promotion admission does not match its producer")
    await connection.execute(
        insert(run_promotion_admission).values(
            run_id=run_id,
            receipt_id=receipt_id,
            namespace_id=scope.namespace_id,
            graph_id=scope.graph_id,
            graph_version_id=scope.version_id,
            version=1,
            run_bytes=record_bound(run_values, tuple(run.c.keys())),
            receipt_bytes=record_bound(receipt_values, tuple(inbound_event.c.keys())),
        )
    )
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(PREPARED)
