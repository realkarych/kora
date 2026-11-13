from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import cast, insert, literal, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import ColumnElement

from kora.cel_eval.errors import raise_safe
from kora.db.claim_source_values import (
    ClaimSourcesRejected,
    PreparedClaimSources,
    decode_claim_source,
)
from kora.db.claim_sources import (
    AcquiredClaimSources,
    ClaimSourceBinding,
    ClaimSourceReadLimits,
    read_claim_source_admission,
    verify_claim_source_binding,
)
from kora.db.claim_sources_v1 import MAX_SOURCE_COUNT, SOURCE_FIELDS
from kora.db.tables import attempt, attempt_claim_read_admission
from kora.db.types import POSTGRESQL
from kora.engine.locks import COMPONENT, TransitionLocks
from kora.ir.json import JsonObject
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

PREPARED: EventSpec = CATALOG.register(
    EventSpec(
        "engine.claim_sources.prepared",
        COMPONENT,
        Level.DEBUG,
        "claim source revision prepared for commit",
        {"created": FieldKind.BOOLEAN},
    )
)


@dataclass(frozen=True, slots=True, repr=False)
class DecodedClaimSources:
    binding: ClaimSourceBinding
    revision: int
    values: JsonObject


def decode_claim_sources(acquired: AcquiredClaimSources) -> DecodedClaimSources:
    try:
        return DecodedClaimSources(
            acquired.binding,
            acquired.revision,
            {
                name: None
                if value is None
                else decode_claim_source(value, acquired.encoding)
                for name, value in zip(SOURCE_FIELDS, acquired.values, strict=True)
            },
        )
    except ValueError:
        del acquired
        raise_safe(ClaimSourcesRejected())


def _source_values(
    dialect: str, prepared: PreparedClaimSources
) -> dict[str, ColumnElement[object]]:
    return {
        field.name: cast(literal(field.text), JSONB())
        if dialect == POSTGRESQL and field.text is not None
        else literal(field.text)
        for field in prepared.fields
    }


def _quantities(prepared: PreparedClaimSources) -> dict[str, int]:
    return {
        f"{field.name}_{kind}_bytes": value
        for field in prepared.fields
        for kind, value in (
            ("native", field.native_bytes),
            ("transport", field.transport_bytes),
        )
    }


def _require_locks(
    connection: AsyncConnection, locks: TransitionLocks, binding: ClaimSourceBinding
) -> None:
    locks.require_connection(connection)
    locks.require(binding.run.run_id)


def _prepared(created: bool) -> None:
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(PREPARED, created=created)


def _require_prepared(
    prepared: PreparedClaimSources, limits: ClaimSourceReadLimits
) -> None:
    allowances = dict(zip(SOURCE_FIELDS, limits.fields, strict=True))
    if len({field.name for field in prepared.fields}) != len(prepared.fields):
        raise ClaimSourcesRejected()
    for field in prepared.fields:
        allowance = allowances.get(field.name)
        if (
            allowance is None
            or field.native_bytes > allowance.native
            or field.transport_bytes > allowance.transport
        ):
            raise ClaimSourcesRejected()


async def _insert_claim_attempt(
    connection: AsyncConnection,
    locks: TransitionLocks,
    binding: ClaimSourceBinding,
    values: Mapping[str, object],
    prepared: PreparedClaimSources,
    limits: ClaimSourceReadLimits,
    now: datetime,
) -> None:
    _require_locks(connection, locks, binding)
    _require_prepared(prepared, limits)
    if (
        {field.name for field in prepared.fields} != set(SOURCE_FIELDS)
        or values.keys() & {*SOURCE_FIELDS, "acknowledged_segment_offset"}
        or (
            values.get("id"),
            values.get("node_instance_id"),
            values.get("price_table_id"),
        )
        != (binding.attempt_id, binding.node_id, binding.price_table_id)
        or not await verify_claim_source_binding(
            connection, binding, limits, include_attempt=False
        )
    ):
        raise ClaimSourcesRejected()
    await connection.execute(
        insert(attempt).values(
            **dict(values),
            **_source_values(connection.dialect.name, prepared),
            acknowledged_segment_offset=0,
        )
    )
    await connection.execute(
        insert(attempt_claim_read_admission).values(
            attempt_id=binding.attempt_id,
            format_version=1,
            source_revision=1,
            **_quantities(prepared),
            created_at=now,
            updated_at=now,
        )
    )
    _prepared(True)


async def _update_claim_sources(
    connection: AsyncConnection,
    locks: TransitionLocks,
    binding: ClaimSourceBinding,
    prepared: PreparedClaimSources,
    limits: ClaimSourceReadLimits,
    expected_revision: int,
    now: datetime,
) -> int:
    _require_locks(connection, locks, binding)
    _require_prepared(prepared, limits)
    if (
        type(expected_revision) is not int
        or not 0 < expected_revision < MAX_SOURCE_COUNT
        or not prepared.fields
        or any(field.name == "input_bindings" for field in prepared.fields)
    ):
        raise ClaimSourcesRejected()
    if not await verify_claim_source_binding(connection, binding, limits):
        raise ClaimSourcesRejected()
    previous = await read_claim_source_admission(connection, binding.attempt_id, limits)
    if previous is None or previous[0] != expected_revision:
        raise ClaimSourcesRejected()
    revision = expected_revision + 1
    await connection.execute(
        update(attempt)
        .where(attempt.c.id == binding.attempt_id)
        .values(**_source_values(connection.dialect.name, prepared), updated_at=now)
    )
    await connection.execute(
        update(attempt_claim_read_admission)
        .where(attempt_claim_read_admission.c.attempt_id == binding.attempt_id)
        .values(**_quantities(prepared), source_revision=revision, updated_at=now)
    )
    _prepared(False)
    return revision


async def _recheck_claim_sources(
    connection: AsyncConnection,
    locks: TransitionLocks,
    acquired: AcquiredClaimSources,
    limits: ClaimSourceReadLimits,
) -> bool:
    _require_locks(connection, locks, acquired.binding)
    if not await verify_claim_source_binding(connection, acquired.binding, limits):
        return False
    current = await read_claim_source_admission(
        connection, acquired.binding.attempt_id, limits
    )
    return current is not None and current[0] == acquired.revision


async def insert_claim_attempt(
    connection: AsyncConnection,
    locks: TransitionLocks,
    binding: ClaimSourceBinding,
    values: Mapping[str, object],
    prepared: PreparedClaimSources,
    limits: ClaimSourceReadLimits,
    now: datetime,
) -> None:
    try:
        await _insert_claim_attempt(
            connection, locks, binding, values, prepared, limits, now
        )
    except (ValueError, OSError, SQLAlchemyError):
        del connection, locks, binding, values, prepared, limits, now
        raise_safe(ClaimSourcesRejected())


async def update_claim_sources(
    connection: AsyncConnection,
    locks: TransitionLocks,
    binding: ClaimSourceBinding,
    prepared: PreparedClaimSources,
    limits: ClaimSourceReadLimits,
    expected_revision: int,
    now: datetime,
) -> int:
    try:
        return await _update_claim_sources(
            connection, locks, binding, prepared, limits, expected_revision, now
        )
    except (ValueError, OSError, SQLAlchemyError):
        del connection, locks, binding, prepared, limits, expected_revision, now
        raise_safe(ClaimSourcesRejected())


async def recheck_claim_sources(
    connection: AsyncConnection,
    locks: TransitionLocks,
    acquired: AcquiredClaimSources,
    limits: ClaimSourceReadLimits,
) -> bool:
    try:
        return await _recheck_claim_sources(connection, locks, acquired, limits)
    except (ValueError, OSError, SQLAlchemyError):
        del connection, locks, acquired, limits
        raise_safe(ClaimSourcesRejected())
