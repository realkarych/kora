import asyncio
from collections.abc import Awaitable, Callable

from sqlalchemy import exists, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.completion import complete
from kora.db.connection import Database
from kora.db.first_claim_basis import read_position_revision
from kora.db.maintenance import (
    NativeReadExpired,
    NativeReadGuard,
    NativeReadUnavailable,
)
from kora.db.run_event_position_reads import (
    BATCH_EVENTS,
    FrozenRun,
    PositionTransition,
    RunPositionCheckpoint,
    bounded_position_text,
    next_frozen_events,
    next_frozen_run,
    position_identifier,
    read_position_transition,
    read_run_position_checkpoint,
)
from kora.db.run_event_position_v1 import (
    MAX_POSITION,
    POSITION_REVISION,
    RunEventPositionRejected,
)
from kora.db.tables import (
    event,
    run,
)
from kora.db.tables import (
    run_event_position as positions,
)
from kora.db.tables import (
    run_event_position_state as states,
)
from kora.db.tables import (
    run_event_position_transition as transition,
)
from kora.engine.locks import TransitionLocks

PREPARATION_BATCH_SECONDS: float = 5.0


async def _verify_position_tail(
    connection: AsyncConnection,
    frozen: FrozenRun,
    checkpoint: RunPositionCheckpoint,
) -> None:
    cols = positions.c
    exact_event = exists().where(
        event.c.id == cols.event_id,
        event.c.run_id == frozen.id,
        event.c.namespace_id == frozen.namespace_id,
    )
    with await connection.execute(
        select(
            cols.position,
            bounded_position_text(connection, cols.event_id, 26),
            exact_event,
        )
        .where(cols.run_id == frozen.id)
        .order_by(cols.position.desc())
        .limit(1)
    ) as result:
        tail = result.one_or_none()
    if tail is None and checkpoint.high_water == 0:
        return
    if (
        tail is None
        or type(tail[0]) is not int
        or tail[0] != checkpoint.high_water
        or tail[1] != checkpoint.legacy_event_id
        or tail[2] is not True
    ):
        raise RunEventPositionRejected()


async def _verify_previous_run(connection: AsyncConnection, run_id: str) -> None:
    namespace: object = await connection.scalar(
        select(bounded_position_text(connection, run.c.namespace_id, 26)).where(
            run.c.id == run_id
        )
    )
    frozen = FrozenRun(run_id, position_identifier(namespace))
    checkpoint = await read_run_position_checkpoint(connection, frozen)
    if checkpoint is None or not checkpoint.ready:
        raise RunEventPositionRejected()
    await _verify_position_tail(connection, frozen, checkpoint)


async def _advance_run(connection: AsyncConnection, frozen: FrozenRun) -> bool:
    locks = TransitionLocks(connection)
    if not await locks.acquire_run(frozen.id, wait=False):
        raise RunEventPositionRejected()
    previous = await read_run_position_checkpoint(connection, frozen)
    checkpoint = previous or RunPositionCheckpoint(0, None, False)
    if checkpoint.ready:
        raise RunEventPositionRejected()
    await _verify_position_tail(connection, frozen, checkpoint)
    keys = await next_frozen_events(connection, frozen, checkpoint.legacy_event_id)
    high = checkpoint.high_water + len(keys)
    if high > MAX_POSITION:
        raise RunEventPositionRejected()
    if keys:
        (
            await connection.execute(
                insert(positions),
                [
                    {"run_id": frozen.id, "position": index, "event_id": key}
                    for index, key in enumerate(keys, start=checkpoint.high_water + 1)
                ],
            )
        ).close()
    ready = len(keys) < BATCH_EVENTS
    statement = (
        insert(states).values(run_id=frozen.id, namespace_id=frozen.namespace_id)
        if previous is None
        else update(states).where(states.c.run_id == frozen.id)
    )
    (
        await connection.execute(
            statement.values(
                high_water=high,
                legacy_event_id=keys[-1] if keys else checkpoint.legacy_event_id,
                ready=ready,
            )
        )
    ).close()
    return ready


async def _advance_transition(
    connection: AsyncConnection, current: PositionTransition
) -> bool:
    if current.after_run_id is not None:
        await _verify_previous_run(connection, current.after_run_id)
    frozen = await next_frozen_run(connection, current.after_run_id)
    if frozen is None:
        values: dict[str, object] = {"ready": True}
    elif await _advance_run(connection, frozen):
        values = {"after_run_id": frozen.id}
    else:
        return False
    (
        await connection.execute(
            update(transition).where(transition.c.id == 1).values(**values)
        )
    ).close()
    return frozen is None


async def _settle_guard(guard: NativeReadGuard) -> None:
    try:
        await guard.close()
    except (NativeReadUnavailable, *guard.native_errors):
        await guard.retire()
        raise


async def _prepare_batch(
    database: Database,
    operation: str,
    proof: str,
    deadline: float,
    verify_writer: Callable[[AsyncConnection], Awaitable[None]] | None,
) -> bool:
    async with database.transaction(admission_deadline=deadline) as connection:
        guard = NativeReadGuard(connection, deadline)
        try:
            async with asyncio.timeout_at(deadline):
                await guard.install()
                if verify_writer is not None:
                    await verify_writer(connection)
                if await read_position_revision(connection) != POSITION_REVISION:
                    raise RunEventPositionRejected()
                current = await read_position_transition(connection)
                if (current.operation, current.proof) != (operation, proof):
                    raise RunEventPositionRejected()
                done = current.ready or await _advance_transition(connection, current)
                guard.require_time()
        except asyncio.CancelledError:
            await complete(guard.interrupt())
            raise
        finally:
            await complete(_settle_guard(guard))
    return done


async def prepare_run_event_positions(
    database: Database,
    operation: str,
    proof: str,
    check_fence: Callable[[], Awaitable[None]],
    *,
    verify_writer: Callable[[AsyncConnection], Awaitable[None]] | None = None,
) -> None:
    while True:
        await check_fence()
        deadline = asyncio.get_running_loop().time() + PREPARATION_BATCH_SECONDS
        try:
            if await _prepare_batch(
                database, operation, proof, deadline, verify_writer
            ):
                return
        except (TimeoutError, NativeReadExpired, NativeReadUnavailable):
            raise RunEventPositionRejected() from None
