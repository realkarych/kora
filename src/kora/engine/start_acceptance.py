import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Never, Self

from sqlalchemy import insert, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.cel_eval.errors import raise_safe
from kora.db.connection import Database, clear_snapshot_error
from kora.db.graph_version_reads import acquire_graph_version
from kora.db.identifiers import IdentifierGenerator
from kora.db.tables import event, graph_version, inbound_event, run, run_inputs
from kora.db.transactions import raise_private_metadata_error
from kora.db.types import normalise_timestamp
from kora.engine.graph_resource_references import GraphResourceScope
from kora.engine.graph_version_identity import GraphVersionIdentityLimits
from kora.engine.locks import COMPONENT, TransitionLocks
from kora.engine.promotion_admission import record_manual_promotion_admission
from kora.engine.run_promotion_read import INPUT_SENTINEL
from kora.engine.start_capacity import (
    CapacityOutcome,
    StartCapacity,
    check_manual_start_capacity,
    manual_start_scopes,
)
from kora.engine.start_facts import (
    EMPTY_PARAMETERS,
    InstallationRefusal,
    ManualStartFacts,
    SelectedStartVersion,
    StartFactsResult,
    StartRefusal,
)
from kora.engine.start_records import (
    AcceptedStart,
    PreparedManualStartRequest,
    SkippedStart,
    StartOutcome,
    StartReceipt,
    StartRecordRejected,
    start_receipt_document,
)
from kora.engine.start_replay import (
    ReplayOutcome,
    StartReadFailed,
    StartReplay,
    read_manual_start_candidate,
    recheck_locked_manual_start_version,
)
from kora.ir.json import JsonObject
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics
from kora.protocols.authentication import Principal

COMPLETED: EventSpec = CATALOG.register(
    EventSpec(
        "engine.start_acceptance.completed",
        COMPONENT,
        Level.DEBUG,
        "manual start acceptance completed",
        {"outcome": FieldKind.STRING},
    )
)


class AcceptanceOutcome(StrEnum):
    ACCEPTED = "accepted"
    SKIPPED = "skipped"
    REPLAYED = "replayed"
    MISMATCHED = "mismatched"
    REFUSED = "refused"
    MISSING = "missing"
    UNAVAILABLE = "unavailable"
    FULL = "full"
    STALE = "stale"
    RETENTION_UNAVAILABLE = "retention_unavailable"


@dataclass(frozen=True, slots=True)
class StartAcceptance:
    outcome: AcceptanceOutcome
    response: StartOutcome | None = None
    limit: int | None = None
    used: int | None = None


class StartAcceptanceFailed(RuntimeError):
    def __init__(self) -> None:
        super().__init__("the manual start acceptance failed")


REPLAYS: dict[ReplayOutcome, AcceptanceOutcome] = {
    ReplayOutcome.MATCHED: AcceptanceOutcome.REPLAYED,
    ReplayOutcome.MISMATCHED: AcceptanceOutcome.MISMATCHED,
    ReplayOutcome.REFUSED: AcceptanceOutcome.REFUSED,
}
CAPACITY_REFUSALS: dict[CapacityOutcome, AcceptanceOutcome] = {
    CapacityOutcome.REFUSED: AcceptanceOutcome.REFUSED,
    CapacityOutcome.FULL: AcceptanceOutcome.FULL,
    CapacityOutcome.RETENTION_UNAVAILABLE: AcceptanceOutcome.RETENTION_UNAVAILABLE,
}


def _completed(result: StartAcceptance) -> StartAcceptance:
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(COMPLETED, outcome=result.outcome.value)
    return result


def _replayed(replay: StartReplay) -> StartAcceptance:
    return StartAcceptance(REPLAYS[replay.outcome], replay.response)


_START_CANDIDATE = object()


class StartCandidate:
    __slots__ = ("namespace_id", "graph_id", "version_id", "_request_digest")
    namespace_id: str
    graph_id: str
    version_id: str
    _request_digest: str

    def __new__(
        cls, issuer: object, request: PreparedManualStartRequest, version_id: str
    ) -> Self:
        if issuer is not _START_CANDIDATE:
            del issuer, request, version_id
            raise TypeError("a manual start candidate requires the selection read")
        identity = request.identity
        candidate = super().__new__(cls)
        for name, value in (
            ("namespace_id", identity.namespace_id),
            ("graph_id", identity.graph_id),
            ("version_id", version_id),
            ("_request_digest", identity.request_digest),
        ):
            object.__setattr__(candidate, name, value)
        return candidate

    def belongs_to(self, request: PreparedManualStartRequest) -> bool:
        identity = request.identity
        return (identity.namespace_id, identity.graph_id, identity.request_digest) == (
            self.namespace_id,
            self.graph_id,
            self._request_digest,
        )

    def __setattr__(self, name: str, value: object) -> Never:
        raise AttributeError("a manual start candidate is immutable")

    def __delattr__(self, name: str) -> Never:
        raise AttributeError("a manual start candidate is immutable")

    def __getstate__(self) -> Never:
        raise TypeError("a manual start candidate requires explicit access")


async def select_manual_start(
    database: Database,
    principal: Principal,
    request: PreparedManualStartRequest,
    key: str,
) -> StartAcceptance | StartCandidate:
    async with database.read_snapshot() as connection:
        candidate = await read_manual_start_candidate(
            connection, principal, request, key
        )
    replay = candidate.replay
    if replay.outcome is not ReplayOutcome.ABSENT:
        return _completed(_replayed(replay))
    if candidate.version_id is None:
        return _completed(StartAcceptance(AcceptanceOutcome.MISSING))
    return StartCandidate(_START_CANDIDATE, request, candidate.version_id)


async def _acquire(
    connection: AsyncConnection,
    candidate: StartCandidate,
    limits: GraphVersionIdentityLimits,
) -> StartAcceptance | SelectedStartVersion:
    status = await connection.scalar(
        select(graph_version.c.status).where(graph_version.c.id == candidate.version_id)
    )
    if status != "active":
        return StartAcceptance(AcceptanceOutcome.UNAVAILABLE)
    acquired = await acquire_graph_version(
        connection,
        candidate.namespace_id,
        candidate.graph_id,
        candidate.version_id,
        max_encoded_bytes=limits.max_encoded_bytes,
    )
    if acquired is None:
        return StartAcceptance(AcceptanceOutcome.MISSING)
    return SelectedStartVersion(
        candidate.namespace_id, candidate.graph_id, candidate.version_id, acquired
    )


async def acquire_manual_start(
    database: Database,
    request: PreparedManualStartRequest,
    candidate: StartCandidate,
    *,
    limits: GraphVersionIdentityLimits,
) -> StartAcceptance | SelectedStartVersion:
    if not candidate.belongs_to(request):
        raise_safe(StartRecordRejected())
    try:
        async with database.read_snapshot() as connection:
            acquired = await _acquire(connection, candidate, limits)
    except SQLAlchemyError:
        raise_private_metadata_error(StartReadFailed())
    if isinstance(acquired, StartAcceptance):
        return _completed(acquired)
    return acquired


@dataclass(frozen=True, slots=True)
class _Acceptance:
    principal: Principal
    request: PreparedManualStartRequest
    key: str
    facts: ManualStartFacts
    run_id: str
    at: datetime


def _response(
    acceptance: _Acceptance, capacity: StartCapacity
) -> AcceptedStart | SkippedStart:
    if capacity.outcome is CapacityOutcome.ROOM:
        return AcceptedStart(
            outcome="accepted",
            run_id=acceptance.run_id,
            graph_version_id=acceptance.facts.pin.version_id,
            status="pending",
        )
    if capacity.outcome is CapacityOutcome.SKIPPED:
        return SkippedStart(outcome="skipped", reason="overlap", run_id=None)
    raise StartRecordRejected()


def _receipt_values(
    acceptance: _Acceptance, response: StartOutcome, receipt_id: str
) -> dict[str, object]:
    identity = acceptance.request.identity
    receipt = StartReceipt(
        version=1,
        kind="manual",
        namespace_id=identity.namespace_id,
        graph_id=identity.graph_id,
        request_digest=identity.request_digest,
        response=response,
    )
    return {
        "id": receipt_id,
        "namespace_id": identity.namespace_id,
        "name": f"kora.start:{identity.graph_id}",
        "idempotency_key": acceptance.key,
        "payload": {},
        "auth_kind": "bearer",
        "nonce": None,
        "received_at": acceptance.at,
        "consumed_by": [],
        "start_outcome": start_receipt_document(receipt),
        "created_at": acceptance.at,
        "updated_at": acceptance.at,
    }


def _run_values(
    acceptance: _Acceptance, capacity: StartCapacity, trigger: JsonObject
) -> dict[str, object]:
    facts, retention = acceptance.facts, capacity.retention
    if retention is None:
        raise StartRecordRejected()
    return {
        "id": acceptance.run_id,
        "namespace_id": facts.namespace_id,
        "graph_id": facts.pin.graph_id,
        "graph_version_id": facts.pin.version_id,
        "rerun_of_run_id": None,
        "idempotency_key": acceptance.key,
        "target_key": None,
        "target_held_since": None,
        "params": INPUT_SENTINEL,
        "cel_params_projection": INPUT_SENTINEL,
        "allow_unenforced_secrets": acceptance.request.allow_unenforced_secrets,
        "status": "pending",
        "needs_scheduling": True,
        "trigger": trigger,
        "limits": {} if facts.timeout is None else {"timeout": facts.timeout},
        "retention": retention.to_json(),
        "retention_version": 1,
        "started_at": None,
        "deadline_expired_at": None,
        "finished_at": None,
        "created_at": acceptance.at,
        "updated_at": acceptance.at,
        "input_encoding_version": 1,
    }


def _timeline(
    acceptance: _Acceptance,
    response: StartOutcome,
    trigger: JsonObject,
    event_id: str,
) -> dict[str, object]:
    identity, principal = acceptance.request.identity, acceptance.principal
    payload: JsonObject = (
        {
            "trigger": trigger,
            "graph_version": acceptance.facts.pin.version_id,
            "rerun_of": None,
        }
        if isinstance(response, AcceptedStart)
        else {
            "graph_id": identity.graph_id,
            "occurrence_key": trigger["occurrence_key"],
            "request_digest": identity.request_digest,
            "reason": "overlap",
        }
    )
    return {
        "id": event_id,
        "namespace_id": identity.namespace_id,
        "run_id": response.run_id,
        "node_instance_id": None,
        "attempt_id": None,
        "kind": "run.created"
        if isinstance(response, AcceptedStart)
        else "trigger.skipped",
        "actor": {
            "kind": "user",
            "owner": principal.owner,
            "token_id": principal.token_id,
        },
        "payload": payload,
        "at": acceptance.at,
        "created_at": acceptance.at,
        "updated_at": acceptance.at,
    }


async def _accept(
    connection: AsyncConnection,
    locks: TransitionLocks,
    acceptance: _Acceptance,
    identifiers: IdentifierGenerator,
) -> StartAcceptance:
    facts = acceptance.facts
    if not facts.belongs_to(acceptance.request):
        raise StartRecordRejected()
    recheck = await recheck_locked_manual_start_version(
        connection,
        locks,
        acceptance.principal,
        acceptance.request,
        acceptance.key,
        acceptance.run_id,
        facts.pin.version_id,
    )
    if recheck.replay.outcome is not ReplayOutcome.ABSENT:
        return _replayed(recheck.replay)
    if not recheck.version_matches:
        return StartAcceptance(AcceptanceOutcome.STALE)
    capacity = await check_manual_start_capacity(
        connection,
        locks,
        acceptance.principal,
        facts.namespace_id,
        facts.pin.graph_id,
        acceptance.run_id,
        facts.overlap,
        graph_retention=facts.retention,
    )
    refusal = CAPACITY_REFUSALS.get(capacity.outcome)
    if refusal is not None:
        return StartAcceptance(refusal, None, capacity.limit, capacity.used)
    response = _response(acceptance, capacity)
    receipt_id, event_id = identifiers.new(), identifiers.new()
    trigger: JsonObject = {
        "kind": "manual",
        "overlap": facts.overlap,
        "occurrence_key": receipt_id,
    }
    receipt_values = _receipt_values(acceptance, response, receipt_id)
    locks.require_connection(connection)
    if isinstance(response, AcceptedStart):
        run_values = _run_values(acceptance, capacity, trigger)
        await connection.execute(insert(run).values(**run_values))
        await connection.execute(
            insert(run_inputs).values(
                run_id=acceptance.run_id,
                cel_projection=EMPTY_PARAMETERS,
                params=EMPTY_PARAMETERS,
            )
        )
        await connection.execute(insert(inbound_event).values(**receipt_values))
        await record_manual_promotion_admission(
            connection,
            locks,
            GraphResourceScope(
                facts.namespace_id, facts.pin.graph_id, facts.pin.version_id
            ),
            run_values,
            receipt_values,
        )
    else:
        await connection.execute(insert(inbound_event).values(**receipt_values))
    await connection.execute(
        insert(event).values(**_timeline(acceptance, response, trigger, event_id))
    )
    outcome = (
        AcceptanceOutcome.ACCEPTED
        if isinstance(response, AcceptedStart)
        else AcceptanceOutcome.SKIPPED
    )
    return StartAcceptance(outcome, response)


async def commit_manual_start(
    database: Database,
    principal: Principal,
    request: PreparedManualStartRequest,
    key: str,
    facts: ManualStartFacts,
    *,
    run_id: str,
    identifiers: IdentifierGenerator,
    at: datetime,
) -> StartAcceptance:
    acceptance = _Acceptance(
        principal, request, key, facts, run_id, normalise_timestamp(at)
    )

    async def unit(connection: AsyncConnection) -> StartAcceptance:
        locks = TransitionLocks(connection)
        await locks.acquire_run(acceptance.run_id)
        await locks.acquire_scopes(
            manual_start_scopes(facts.namespace_id, facts.pin.graph_id)
        )
        return await _accept(connection, locks, acceptance, identifiers)

    try:
        result = await database.run(unit)
    except asyncio.CancelledError as error:
        clear_snapshot_error(error)
        raise
    except (
        SQLAlchemyError,
        ValueError,
        TypeError,
        RecursionError,
        OverflowError,
    ):
        result = None
    finally:
        del principal, request, key
    if result is None:
        raise_private_metadata_error(StartAcceptanceFailed())
    return _completed(result)


StartPreparation = Callable[
    [StartCandidate], Awaitable[StartAcceptance | StartFactsResult | None]
]
StartResult = StartAcceptance | StartRefusal | InstallationRefusal | None
START_ATTEMPTS = 2


async def accept_manual_start(
    database: Database,
    principal: Principal,
    request: PreparedManualStartRequest,
    key: str,
    prepare: StartPreparation,
    *,
    identifiers: IdentifierGenerator,
    clock: Callable[[], datetime],
) -> StartResult:
    stale = StartAcceptance(AcceptanceOutcome.STALE)
    for _ in range(START_ATTEMPTS):
        candidate = await select_manual_start(database, principal, request, key)
        if isinstance(candidate, StartAcceptance):
            return candidate
        prepared = await prepare(candidate)
        if not isinstance(prepared, ManualStartFacts):
            return prepared
        committed = await commit_manual_start(
            database,
            principal,
            request,
            key,
            prepared,
            run_id=identifiers.new(),
            identifiers=identifiers,
            at=clock(),
        )
        if committed.outcome is not AcceptanceOutcome.STALE:
            return committed
    return stale
