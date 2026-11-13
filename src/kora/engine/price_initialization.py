from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic import ValidationError
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.cel_eval.errors import raise_safe
from kora.db.cost_reads import read_latest_price
from kora.db.identifiers import is_identifier
from kora.db.tables import namespace, price_table
from kora.db.types import parse_json
from kora.engine.locks import COMPONENT, Scope, ScopeKind, TransitionLocks
from kora.ir.cost import PriceRates, PriceRevision
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

EARLIEST_PRICE_TIME: datetime = datetime.min.replace(tzinfo=UTC)
PREPARED: EventSpec = CATALOG.register(
    EventSpec(
        "engine.price_initialization.prepared",
        COMPONENT,
        Level.DEBUG,
        "price initialization prepared for commit",
        {"outcome": FieldKind.STRING},
    )
)


class PriceInitializationRefused(RuntimeError):
    def __init__(self) -> None:
        super().__init__("the price initialization was refused")


@dataclass(frozen=True, slots=True)
class PriceConfiguration:
    namespace_id: str | None
    effective_from: datetime
    prices: Mapping[str, PriceRates] = field(repr=False)

    def prepare(self, identifier: str) -> PriceRevision:
        if not is_identifier(identifier) or (
            self.namespace_id is not None and not is_identifier(self.namespace_id)
        ):
            raise PriceInitializationRefused()
        try:
            return PriceRevision(
                id=identifier,
                namespace_id=self.namespace_id,
                effective_from=self.effective_from,
                version=1,
                normalization_version=1,
                prices=self.prices,
            )
        except ValidationError:
            raise_safe(PriceInitializationRefused())


def price_scopes(namespace_id: str | None) -> tuple[Scope, ...]:
    scopes = (Scope(ScopeKind.GLOBAL),)
    if namespace_id is not None:
        scopes += (Scope(ScopeKind.NAMESPACE, (namespace_id,)),)
    return scopes


def record_price_prepared(outcome: str) -> None:
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(PREPARED, outcome=outcome)


async def import_price_revision(
    connection: AsyncConnection,
    locks: TransitionLocks,
    revision: PriceRevision,
    at: datetime,
) -> PriceRevision:
    locks.require_connection(connection)
    locks.require(None, price_scopes(revision.namespace_id))
    existing = await read_latest_price(
        connection, revision.namespace_id, revision.effective_from
    )
    if existing is not None and existing.effective_from == revision.effective_from:
        if existing.prices != revision.prices:
            raise PriceInitializationRefused()
        record_price_prepared("replayed")
        return existing
    if revision.namespace_id is not None:
        found = await connection.scalar(
            select(namespace.c.id).where(namespace.c.id == revision.namespace_id)
        )
        if found != revision.namespace_id:
            raise PriceInitializationRefused()
    prices = {
        model: parse_json(rates.model_dump_json())
        for model, rates in revision.prices.items()
    }
    await connection.execute(
        insert(price_table).values(
            id=revision.id,
            namespace_id=revision.namespace_id,
            model=None,
            version=revision.version,
            normalization_version=revision.normalization_version,
            effective_from=revision.effective_from,
            prices=prices,
            created_at=at,
            updated_at=at,
        )
    )
    record_price_prepared("inserted")
    return revision


async def ensure_empty_price_default(
    connection: AsyncConnection,
    locks: TransitionLocks,
    revision: PriceRevision,
    at: datetime,
) -> PriceRevision:
    locks.require_connection(connection)
    locks.require(None, price_scopes(None))
    if (
        revision.namespace_id is not None
        or revision.effective_from != EARLIEST_PRICE_TIME
        or revision.prices
    ):
        raise PriceInitializationRefused()
    existing = await read_latest_price(connection, None, EARLIEST_PRICE_TIME)
    if existing is not None:
        record_price_prepared("baseline_retained")
        return existing
    return await import_price_revision(connection, locks, revision, at)
