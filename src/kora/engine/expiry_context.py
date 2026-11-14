import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import cast as typed_cast

from sqlalchemy import Text, case, cast, func, insert, literal, select, update
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import ColumnElement

from kora.db.connection import clear_snapshot_error
from kora.db.identifiers import Clock, IdentifierGenerator, is_identifier
from kora.db.tables import event, run
from kora.db.transactions import raise_private_metadata_error
from kora.db.types import POSTGRESQL, JsonObject, canonical_json
from kora.engine.graph_resource_references import GraphResourceScope
from kora.engine.locks import COMPONENT, Scope, ScopeKind, TransitionLocks
from kora.engine.run_promotion_read import PromotionReadLimits
from kora.engine.writer_mode import writable_postgres_transaction
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

PREPARED: EventSpec = CATALOG.register(
    EventSpec(
        "engine.expiry.prepared",
        COMPONENT,
        Level.DEBUG,
        "execution authority expiry prepared for commit",
        {"action": FieldKind.STRING},
    )
)
ACTIVE_ATTEMPT_STATUSES = ("created", "claimed", "submitted", "running", "finishing")


class ExpiryState(StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"
    REFUSED = "refused"


class ExpiryFailed(RuntimeError):
    def __init__(self) -> None:
        super().__init__("execution authority expiry failed")


def bounded_json(value: ColumnElement[object], limit: int) -> ColumnElement[object]:
    return case((func.octet_length(cast(value, Text())) <= limit, value))


def set_json_field(
    connection: AsyncConnection,
    document: ColumnElement[object],
    key: str,
    value: ColumnElement[object],
) -> ColumnElement[object]:
    if connection.dialect.name == POSTGRESQL:
        return func.jsonb_set(
            func.coalesce(document, cast(literal("{}"), JSONB)),
            cast(literal([key]), ARRAY(Text)),
            value,
        )
    return func.json_set(func.coalesce(document, "{}"), "$." + key, value)


def json_scalar(
    connection: AsyncConnection, value: str | int | None
) -> ColumnElement[object]:
    if connection.dialect.name == POSTGRESQL:
        return typed_cast(
            ColumnElement[object], cast(literal(canonical_json(value)), JSONB)
        )
    return literal(value)


@dataclass(frozen=True)
class ExpiryContext:
    connection: AsyncConnection
    locks: TransitionLocks
    scope: GraphResourceScope
    run_id: str
    clock: Clock
    identifiers: IdentifierGenerator
    limits: PromotionReadLimits
    attempt_bytes: int

    def __post_init__(self) -> None:
        if type(self.attempt_bytes) is not int or self.attempt_bytes <= 0:
            raise ValueError("attempt read admission must be positive")

    def require_capacity(self, runtime: str | None = None) -> None:
        scopes = (
            Scope(ScopeKind.GLOBAL),
            Scope(ScopeKind.NAMESPACE, (self.scope.namespace_id,)),
            Scope(ScopeKind.GRAPH, (self.scope.graph_id,)),
        )
        if runtime is not None:
            scopes += (Scope(ScopeKind.RUNTIME, (runtime,)),)
        self.locks.require(self.run_id, scopes)

    async def mark(self, now: datetime) -> None:
        await self.connection.execute(
            update(run)
            .where(run.c.id == self.run_id)
            .values(needs_scheduling=True, updated_at=now)
        )

    async def event(
        self,
        kind: str,
        payload: JsonObject,
        now: datetime,
        node_id: str | None = None,
        attempt_id: str | None = None,
    ) -> None:
        await self.connection.execute(
            insert(event).values(
                id=self.identifiers.new(),
                namespace_id=self.scope.namespace_id,
                run_id=self.run_id,
                node_instance_id=node_id,
                attempt_id=attempt_id,
                kind=kind,
                actor={"kind": "system", "component": "scheduler"},
                payload=payload,
                at=now,
                created_at=now,
                updated_at=now,
            )
        )


@asynccontextmanager
async def expiry_transition(
    context: ExpiryContext, action: str
) -> AsyncGenerator[None]:
    failed = False
    try:
        context.locks.require_connection(context.connection)
        context.locks.require(context.run_id)
        if not is_identifier(context.run_id) or (
            context.connection.dialect.name == POSTGRESQL
            and not await writable_postgres_transaction(context.connection)
        ):
            raise ValueError("expiry requires a writable authority transaction")
        owner = select(run.c.id).where(
            run.c.id == context.run_id,
            run.c.namespace_id == context.scope.namespace_id,
            run.c.graph_id == context.scope.graph_id,
            run.c.graph_version_id == context.scope.version_id,
        )
        if await context.connection.scalar(owner) is None:
            raise ValueError("expiry requires the original run scope")
        yield
    except asyncio.CancelledError as error:
        clear_snapshot_error(error)
        raise
    except (SQLAlchemyError, ValueError, TypeError, RecursionError, OverflowError):
        failed = True
    finally:
        del context
    if failed:
        raise_private_metadata_error(ExpiryFailed())
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(PREPARED, action=action)
