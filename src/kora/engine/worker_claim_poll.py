import asyncio
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated

from pydantic import Field, StrictInt
from sqlalchemy import and_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.cel_eval.errors import raise_safe
from kora.db.claim_source_values import ClaimSourcesRejected
from kora.db.connection import Database, clear_snapshot_error
from kora.db.grant_history import GrantHistoryRejected, HistoryReadLimits, HistoryScope
from kora.db.identifiers import IdentifierGenerator
from kora.db.tables import attempt_dispatcher, worker
from kora.db.types import POSTGRESQL, normalise_timestamp
from kora.db.worker_claim_poll import advance_claim_cursor, claim_candidates
from kora.db.worker_claims import unknown_worker_slot
from kora.engine.attempt_claims import (
    ClaimDecision,
    ClaimOutcome,
    ClaimReceipt,
    claim_attempt,
)
from kora.engine.claim_context import (
    ClaimAccessRejected,
    ClaimQuery,
    ClaimTarget,
    claim_record,
    claim_target,
    claim_worker,
)
from kora.engine.claim_material import ClaimRuntimeSelection, SelectedClaimNode
from kora.engine.claim_selection import (
    ClaimDelivery,
    SelectedClaim,
    prepare_selected_claim,
)
from kora.engine.claim_snapshot import (
    ClaimSnapshotLimits,
    acquire_claim_snapshot,
    prepare_claim_snapshot,
)
from kora.engine.expiry import observe_attempt_expiry, observe_run_expiry
from kora.engine.expiry_context import ExpiryContext
from kora.engine.grant_history import acquire_history, prepare_history
from kora.engine.graph_resource_references import GraphResourceScope
from kora.engine.graph_version_identity import GraphVersionPin
from kora.engine.locks import TransitionLocks
from kora.engine.run_promotion_read import PromotionReadLimits
from kora.engine.writer_mode import writable_postgres_transaction
from kora.protocols.authentication import Principal
from kora.protocols.boundary import BoundaryModel
from kora.protocols.identity import Identifier
from kora.protocols.runtime import RuntimeCapabilities
from kora.protocols.time import Clock


class ClaimCursor(BoundaryModel):
    claim_cursor: Identifier | None
    claim_cursor_generation: Annotated[StrictInt, Field(ge=0, lt=2**63 - 1)]


class ClaimCursorChanged(ValueError):
    pass


@dataclass(frozen=True)
class ClaimPollLimits:
    snapshot: ClaimSnapshotLimits
    history: HistoryReadLimits
    expiry: PromotionReadLimits
    worker_bytes: int
    read_seconds: float

    def __post_init__(self) -> None:
        self.snapshot.admit()
        if self.worker_bytes <= 0 or not 0 < self.read_seconds < math.inf:
            raise ValueError("worker Claim read admission must be finite and positive")


class WorkerClaimPoll:
    def __init__(
        self,
        database: Database,
        runtimes: Mapping[str, RuntimeCapabilities],
        clock: Clock,
        limits: ClaimPollLimits,
        uncertainty: timedelta,
    ) -> None:
        if uncertainty < timedelta(0):
            raise ValueError("worker Claim clock uncertainty cannot be negative")
        self.database = database
        self.runtimes = dict(runtimes)
        self.clock = clock
        self.limits = limits
        self.uncertainty = uncertainty
        self.identifiers = IdentifierGenerator(clock.now)

    async def _guard(
        self, connection: AsyncConnection, query: ClaimQuery, principal: Principal
    ) -> None:
        await connection.execute(select(1))
        if (
            connection.dialect.name == POSTGRESQL
            and not await writable_postgres_transaction(connection)
        ):
            raise ClaimSourcesRejected()
        await claim_worker(
            connection, query, principal, self.limits.worker_bytes, lock=True
        )

    async def _receipt(
        self, connection: AsyncConnection, query: ClaimQuery
    ) -> ClaimReceipt | None:
        receipt = await claim_record(
            connection,
            ClaimReceipt,
            attempt_dispatcher,
            and_(
                attempt_dispatcher.c.worker_id == query.worker_id,
                attempt_dispatcher.c.incarnation_id == query.incarnation_id,
                attempt_dispatcher.c.claim_id == query.claim_id,
            ),
            self.limits.history.dispatcher_row,
        )
        if receipt is not None and receipt.claim_digest != query.digest():
            raise ClaimAccessRejected("conflict")
        return receipt

    async def _cursor(
        self, connection: AsyncConnection, query: ClaimQuery
    ) -> ClaimCursor:
        cursor = await claim_record(
            connection,
            ClaimCursor,
            worker,
            worker.c.id == query.worker_id,
            self.limits.worker_bytes,
        )
        if cursor is None:
            raise ClaimAccessRejected("forbidden")
        return cursor

    async def _seen(self, connection: AsyncConnection, query: ClaimQuery) -> None:
        now = normalise_timestamp(self.clock.now())
        await connection.execute(
            update(worker)
            .where(
                worker.c.id == query.worker_id,
                worker.c.last_seen_at <= now - timedelta(seconds=15),
            )
            .values(last_seen_at=now, updated_at=now)
        )

    async def _advance(
        self,
        connection: AsyncConnection,
        query: ClaimQuery,
        cursor: ClaimCursor,
        position: str | None,
    ) -> None:
        if not await advance_claim_cursor(
            connection,
            query.worker_id,
            cursor.claim_cursor,
            cursor.claim_cursor_generation,
            position,
            self.clock.now(),
        ):
            raise ClaimCursorChanged()

    def _runtime(
        self, selected: SelectedClaimNode, expected: str
    ) -> ClaimRuntimeSelection:
        runtime, vcs = selected.context["runtime"], selected.context["vcs"]
        if (
            runtime != expected
            or not isinstance(runtime, str)
            or not isinstance(vcs, str)
        ):
            raise ClaimSourcesRejected()
        capabilities = self.runtimes.get(runtime)
        if capabilities is None:
            raise ClaimSourcesRejected()
        return ClaimRuntimeSelection(runtime, vcs, capabilities.default_stall_after)

    async def _prepare(self, target: ClaimTarget) -> SelectedClaim:
        deadline = asyncio.get_running_loop().time() + self.limits.read_seconds
        acquired = await acquire_claim_snapshot(
            self.database,
            target.binding(),
            self.limits.snapshot,
            deadline=deadline,
        )
        pin = GraphVersionPin(
            target.run.graph_id,
            target.run.graph_version_id,
            acquired.graph.digests[1].decode(acquired.graph.encoding),
        )
        snapshot = prepare_claim_snapshot(
            acquired,
            pin,
            lambda selected: self._runtime(selected, target.attempt.runtime),
            self.limits.snapshot,
        )
        del acquired
        domain = target.attempt.execution_domain_id
        history = (
            None
            if domain is None
            else prepare_history(
                await acquire_history(
                    self.database,
                    HistoryScope(
                        target.run.namespace_id,
                        target.run.id,
                        target.node.id,
                        target.attempt.id,
                        domain,
                    ),
                    self.limits.history,
                    deadline=deadline,
                )
            )
        )
        return prepare_selected_claim(snapshot, history, target.attempt.deadline)

    def _context(
        self, connection: AsyncConnection, locks: TransitionLocks, target: ClaimTarget
    ) -> ExpiryContext:
        return ExpiryContext(
            connection,
            locks,
            GraphResourceScope(
                target.run.namespace_id,
                target.run.graph_id,
                target.run.graph_version_id,
            ),
            target.run.id,
            self.clock.now,
            self.identifiers,
            self.limits.expiry,
            self.limits.snapshot.sources.attempt_row,
        )

    async def _selected(
        self,
        attempt_id: str,
        query: ClaimQuery,
        principal: Principal,
        cursor: ClaimCursor,
        replay: ClaimReceipt | None = None,
    ) -> ClaimDecision:
        async with self.database.read_transaction() as reader:
            target = await claim_target(
                reader, attempt_id, self.limits.snapshot.sources
            )
        if target is None:
            if replay is not None:
                raise ClaimAccessRejected("stale")
            return ClaimDecision(ClaimOutcome.INELIGIBLE)
        if target.run.namespace_id not in {
            grant.namespace_id for grant in principal.namespaces
        }:
            if replay is not None:
                raise ClaimAccessRejected("forbidden")
            return ClaimDecision(ClaimOutcome.INELIGIBLE)
        saved = target.attempt
        now = normalise_timestamp(self.clock.now())
        if replay is not None and (
            (
                saved.worker_id,
                saved.lease_owner,
                saved.lease_epoch,
                saved.execution_domain_id,
            )
            != (
                query.worker_id,
                query.worker_id,
                replay.lease_epoch,
                query.execution_domain_id,
            )
            or saved.lease_until is None
            or saved.lease_until <= now
        ):
            raise ClaimAccessRejected("stale")
        legacy = (
            replay is None
            and saved.worker_id == query.worker_id
            and saved.current_dispatcher_open is None
            and saved.lease_until is not None
            and saved.lease_until > now
        )
        if (
            replay is None
            and not legacy
            and (
                saved.runtime != query.runtime
                or (saved.lease_until is not None and saved.lease_until > now)
                or saved.execution_domain_id not in (None, query.execution_domain_id)
            )
        ):
            return ClaimDecision(ClaimOutcome.INELIGIBLE)
        prepared = None if legacy else await self._prepare(target)
        async with self.database.transaction() as writer:
            locks = TransitionLocks(writer)
            if not await locks.acquire_run(target.run.id, wait=replay is not None):
                return ClaimDecision(ClaimOutcome.INELIGIBLE)
            decision = await claim_attempt(
                self._context(writer, locks, target),
                attempt_id,
                query,
                principal,
                prepared,
                self.limits.snapshot.sources,
                self.limits.history,
                self.limits.worker_bytes,
                self.uncertainty,
            )
            if decision.outcome is ClaimOutcome.GRANTED:
                await self._advance(writer, query, cursor, attempt_id)
            if decision.outcome in (
                ClaimOutcome.GRANTED,
                ClaimOutcome.MAINTAINED,
                ClaimOutcome.EXPIRE,
            ):
                await self._seen(writer, query)
        if decision.outcome is ClaimOutcome.EXPIRE:
            async with self.database.transaction() as writer:
                locks = TransitionLocks(writer)
                await locks.acquire_run(target.run.id)
                context = self._context(writer, locks, target)
                if target.attempt.started_at is None:
                    await observe_run_expiry(context)
                else:
                    await observe_attempt_expiry(context, attempt_id)
        return decision

    async def _empty(
        self,
        query: ClaimQuery,
        principal: Principal,
        cursor: ClaimCursor,
        position: str | None,
    ) -> ClaimDecision:
        async with self.database.transaction() as writer:
            await self._guard(writer, query, principal)
            receipt = await self._receipt(writer, query)
            if receipt is not None:
                return ClaimDecision(ClaimOutcome.REDIRECT, redirect=receipt.attempt_id)
            await self._advance(writer, query, cursor, position)
            await self._seen(writer, query)
        return ClaimDecision(ClaimOutcome.INELIGIBLE)

    async def _replay(
        self,
        query: ClaimQuery,
        principal: Principal,
        cursor: ClaimCursor,
    ) -> ClaimDelivery | None:
        async with self.database.read_transaction() as reader:
            receipt = await self._receipt(reader, query)
        if receipt is None:
            raise ClaimSourcesRejected()
        result = await self._selected(
            receipt.attempt_id, query, principal, cursor, receipt
        )
        if result.outcome is not ClaimOutcome.REPLAYED:
            raise ClaimSourcesRejected()
        return result.delivery

    async def _poll(
        self, query: ClaimQuery, principal: Principal
    ) -> ClaimDelivery | None:
        if query.runtime not in self.runtimes:
            raise ClaimAccessRejected("forbidden")
        async with self.database.read_transaction() as reader:
            await claim_worker(reader, query, principal, self.limits.worker_bytes)
            cursor = await self._cursor(reader, query)
            receipt = await self._receipt(reader, query)
            unknown = (
                None
                if receipt is not None
                else await unknown_worker_slot(
                    reader,
                    query.worker_id,
                    self.clock.now(),
                )
            )
            candidates = (
                ()
                if receipt is not None
                else (
                    (unknown,)
                    if unknown is not None
                    else await claim_candidates(
                        reader, query.runtime, cursor.claim_cursor
                    )
                )
            )
        if receipt is not None:
            return await self._replay(query, principal, cursor)
        position = cursor.claim_cursor if unknown is not None else None
        for attempt_id in candidates:
            if unknown is None:
                position = attempt_id
            try:
                result = await self._selected(attempt_id, query, principal, cursor)
            except (ClaimSourcesRejected, GrantHistoryRejected):
                try:
                    terminal = await self._empty(query, principal, cursor, position)
                except ClaimCursorChanged:
                    raise_safe(ClaimSourcesRejected())
                if terminal.outcome is ClaimOutcome.REDIRECT:
                    return await self._replay(query, principal, cursor)
                raise_safe(ClaimSourcesRejected())
            if result.outcome is ClaimOutcome.REDIRECT:
                return await self._replay(query, principal, cursor)
            if result.outcome is not ClaimOutcome.INELIGIBLE:
                return result.delivery
        result = await self._empty(query, principal, cursor, position)
        if result.outcome is ClaimOutcome.REDIRECT:
            return await self._replay(query, principal, cursor)
        return None

    async def poll(
        self, query: ClaimQuery, principal: Principal
    ) -> ClaimDelivery | None:
        try:
            return await self._poll(query, principal)
        except ClaimCursorChanged:
            return None
        except ClaimAccessRejected:
            raise
        except asyncio.CancelledError as error:
            clear_snapshot_error(error)
            raise
        except (SQLAlchemyError, OSError, ValueError, TypeError, OverflowError):
            raise_safe(ClaimSourcesRejected())
        finally:
            del query, principal
