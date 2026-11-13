import asyncio
from collections.abc import Callable, Sequence

from sqlalchemy.exc import SQLAlchemyError

from kora.cel_eval.errors import raise_safe
from kora.db.connection import Database
from kora.db.cost_reads import CostReadRefused
from kora.engine.locks import COMPONENT, Scope, ScopeKind, TransitionLocks
from kora.engine.namespace_policy import (
    NamespacePolicyConfiguration,
    NamespacePolicyRefused,
    write_namespace_policy,
)
from kora.engine.price_initialization import (
    EARLIEST_PRICE_TIME,
    PriceConfiguration,
    PriceInitializationRefused,
    ensure_empty_price_default,
    import_price_revision,
)
from kora.ir.cost import PriceRevision
from kora.observability import CATALOG, EventSpec, FieldKind, Level, ProcessDiagnostics
from kora.protocols.time import Clock

FINISHED: EventSpec = CATALOG.register(
    EventSpec(
        "engine.admission_initialization.finished",
        COMPONENT,
        Level.INFO,
        "admission initialization finished",
        {"outcome": FieldKind.STRING},
    )
)
REFUSED: EventSpec = CATALOG.register(
    EventSpec(
        "engine.admission_initialization.refused",
        COMPONENT,
        Level.ERROR,
        "admission initialization was refused",
        {},
    )
)


class AdmissionInitializationRefused(RuntimeError):
    def __init__(self) -> None:
        super().__init__("admission initialization was refused")


class AdmissionInitialization:
    def __init__(
        self,
        database: Database,
        clock: Clock,
        identifier: Callable[[], str],
        *,
        policies: Sequence[NamespacePolicyConfiguration] = (),
        prices: Sequence[PriceConfiguration] = (),
    ) -> None:
        self.database = database
        self.clock = clock
        self.policies = tuple(policies)
        self.prices: tuple[PriceRevision, ...] = tuple(
            price.prepare(identifier()) for price in prices
        )
        self.baseline = PriceConfiguration(None, EARLIEST_PRICE_TIME, {}).prepare(
            identifier()
        )
        namespaces = {policy.namespace_id for policy in self.policies}
        namespaces.update(
            price.namespace_id
            for price in self.prices
            if price.namespace_id is not None
        )
        self.scopes = (Scope(ScopeKind.GLOBAL),) + tuple(
            Scope(ScopeKind.NAMESPACE, (namespace,)) for namespace in sorted(namespaces)
        )

    async def initialize(self, diagnostics: ProcessDiagnostics) -> None:
        logger = diagnostics.logger(COMPONENT)
        try:
            async with self.database.transaction() as connection:
                locks = TransitionLocks(connection)
                await locks.acquire_scopes(self.scopes)
                at = self.clock.now()
                for policy in self.policies:
                    await write_namespace_policy(connection, locks, policy, at)
                for price in self.prices:
                    await import_price_revision(connection, locks, price, at)
                await ensure_empty_price_default(connection, locks, self.baseline, at)
        except asyncio.CancelledError:
            logger.emit(FINISHED, outcome="cancelled")
            raise
        except (
            SQLAlchemyError,
            CostReadRefused,
            NamespacePolicyRefused,
            PriceInitializationRefused,
        ):
            logger.emit(REFUSED)
            raise_safe(AdmissionInitializationRefused())
        logger.emit(FINISHED, outcome="committed")
