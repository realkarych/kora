import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import insert, select, text, update

from kora.db.connection import Database, Dialect
from kora.db.identifiers import IdentifierGenerator
from kora.db.tables import attempt, event, node_instance, run, wait
from kora.engine.admission_wait import (
    AdmissionWaitFailed,
    AdmissionWaitState,
    save_admission_wait,
)
from kora.engine.attempt_capacity import CapacityLimit
from kora.engine.locks import LockError
from tests.db.closing import ConnectionLedger
from tests.db.seed import START, fixed_randomness
from tests.engine.admission_wait_support import INTERVAL, LIMITS, admission_case
from tests.engine.expiry_support import expiry_context
from tests.engine.lock_support import DEADLINE, DeliberateRollback, lock_inventory


@pytest.mark.asyncio
async def test_admission_refusal_creates_and_refreshes_one_wait(
    database: Database,
) -> None:
    case = await admission_case(database)
    before_run, before_nodes, before_events = await case.promotion.snapshot(database)
    async with database.read_transaction() as reader:
        with await reader.execute(select(attempt)) as result:
            before_attempts = result.mappings().all()

    async with database.transaction() as writer:
        first = await case.apply(await case.context(writer))
    [initial] = await case.saved_waits(database)
    case.promotion.clock.advance(1)
    async with database.transaction() as writer:
        await writer.execute(
            update(attempt)
            .where(attempt.c.id == case.blocker)
            .values(recovery_required=True)
        )
        second = await case.apply(await case.context(writer), str(initial["id"]))
    [refreshed] = await case.saved_waits(database)
    after_run, after_nodes, after_events = await case.promotion.snapshot(database)
    async with database.read_transaction() as reader:
        with await reader.execute(select(attempt)) as result:
            after_attempts = result.mappings().all()

    assert first is AdmissionWaitState.CREATED
    assert second is AdmissionWaitState.REFRESHED
    assert initial["id"] == refreshed["id"]
    assert initial["created_at"] == refreshed["created_at"] == START
    assert refreshed["updated_at"] == START + INTERVAL
    assert refreshed["wake_at"] == START + 2 * INTERVAL
    assert refreshed["reason"] == {
        "version": 1,
        "kind": "execution_recovery",
        "observed_at": (START + INTERVAL).isoformat(timespec="microseconds"),
        "blockers": {"count": 1, "ids": [case.blocker], "truncated": False},
    }
    assert refreshed["attempt_id"] is None
    assert refreshed["subscription"] is refreshed["observation"] is None
    assert len(before_attempts) == len(after_attempts) == 1
    assert after_attempts[0]["id"] == case.blocker
    assert before_run["started_at"] == after_run["started_at"]
    assert after_run["target_held_since"] is None
    assert after_run["needs_scheduling"] is True
    assert [node["id"] for node in before_nodes] == [node["id"] for node in after_nodes]
    candidate = next(node for node in after_nodes if node["id"] == case.binding.node_id)
    assert candidate["status"] == "waiting"
    assert candidate["current_attempt_id"] is candidate["started_at"] is None
    assert candidate["attempt_count"] == 0
    assert len(after_events) == len(before_events) + 1
    assert [event["kind"] for event in after_events].count("node.waiting") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"status": "pending"},
        {"status": "cancelled"},
        {"kind": "timer"},
        {"top_level": False},
        {"subgraph_attempt": 2},
        {"attempt_count": 1},
        {"deadline_expired_at": START},
        {"node_path": "other"},
    ],
)
async def test_admission_wait_refuses_ineligible_node(
    database: Database, change: dict[str, object]
) -> None:
    case = await admission_case(database)
    async with database.transaction() as writer:
        await writer.execute(
            update(node_instance)
            .where(node_instance.c.id == case.binding.node_id)
            .values(**change)
        )
    before = await case.promotion.snapshot(database)

    async with database.transaction() as writer:
        result = await case.apply(await case.context(writer))

    assert result is AdmissionWaitState.REFUSED
    assert await case.saved_waits(database) == []
    assert await case.promotion.snapshot(database) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["stale", "kind", "attempt", "duplicate", "closed"])
async def test_admission_wait_refuses_changed_current_owner(
    database: Database, changed: str
) -> None:
    case = await admission_case(database)
    async with database.transaction() as writer:
        await case.apply(await case.context(writer))
    [saved] = await case.saved_waits(database)
    expected = str(saved["id"])
    async with database.transaction() as writer:
        if changed == "stale":
            expected = case.binding.attempt_id
        elif changed == "duplicate":
            await writer.execute(
                insert(wait).values(**{**saved, "id": case.binding.attempt_id})
            )
        else:
            values: dict[str, object] = (
                {"kind": "timer"}
                if changed == "kind"
                else {"attempt_id": case.blocker}
                if changed == "attempt"
                else {"status": "cancelled"}
            )
            await writer.execute(
                update(wait).where(wait.c.id == saved["id"]).values(**values)
            )
    before = await case.saved_waits(database)
    before_run = await case.promotion.snapshot(database)

    async with database.transaction() as writer:
        result = await case.apply(await case.context(writer), expected)

    assert result is AdmissionWaitState.REFUSED
    assert await case.saved_waits(database) == before
    assert await case.promotion.snapshot(database) == before_run


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
async def test_admission_wait_guard_latches_exact_expiry(
    database: Database, existing: bool
) -> None:
    case = await admission_case(database)
    expected: str | None = None
    if existing:
        async with database.transaction() as writer:
            await case.apply(await case.context(writer))
        [saved] = await case.saved_waits(database)
        expected = str(saved["id"])
    case.promotion.clock.advance(10)

    async with database.transaction() as writer:
        expired = await case.apply(await case.context(writer), expected)
    case.promotion.clock.advance(-10)
    async with database.transaction() as writer:
        repeated = await case.apply(await case.context(writer), expected)
    saved_run, nodes, events = await case.promotion.snapshot(database)
    waits = await case.saved_waits(database)

    assert expired is AdmissionWaitState.EXPIRED
    assert repeated in (AdmissionWaitState.EXPIRED, AdmissionWaitState.REFUSED)
    assert saved_run["deadline_expired_at"] == START + timedelta(seconds=10)
    assert saved_run["status"] == "running"
    assert len(waits) == int(existing)
    assert all(row["status"] == "cancelled" for row in waits)
    assert all(row["wake_at"] == START + INTERVAL for row in waits)
    assert [event["kind"] for event in events].count("node.waiting") == int(existing)
    assert all(node["current_attempt_id"] is None for node in nodes)


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["rollback", "cancel"])
async def test_admission_wait_rolls_back_all_effects(
    database: Database, ending: str
) -> None:
    case = await admission_case(database)
    ledger = ConnectionLedger(database)
    before = await case.promotion.snapshot(database)
    written, release = asyncio.Event(), asyncio.Event()

    async def apply() -> None:
        async with database.transaction() as writer:
            assert (
                await case.apply(await case.context(writer))
                is AdmissionWaitState.CREATED
            )
            written.set()
            await asyncio.wait_for(release.wait(), DEADLINE)
            raise DeliberateRollback()

    task = asyncio.create_task(apply())
    try:
        await asyncio.wait_for(written.wait(), DEADLINE)
    finally:
        if ending == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, DEADLINE)
        else:
            release.set()
            with pytest.raises(DeliberateRollback):
                await asyncio.wait_for(task, DEADLINE)

    assert await case.saved_waits(database) == []
    assert await case.promotion.snapshot(database) == before
    assert await lock_inventory(database) == []
    assert database.units_in_flight() == ledger.outstanding == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
async def test_admission_wait_requires_original_writer_and_runtime_scopes(
    database: Database,
) -> None:
    case = await admission_case(database)
    observation = CapacityLimit("global", "max_running_attempts", 1, 1, None)

    async with database.transaction() as writer:
        context = await expiry_context(writer, case.promotion, capacity=False)
        with pytest.raises(LockError):
            await save_admission_wait(
                context, case.binding, LIMITS, "local", observation, interval=INTERVAL
            )
    async with database.transaction() as writer:
        context = await case.context(writer)
        async with database.read_transaction() as reader:
            with pytest.raises(LockError):
                await save_admission_wait(
                    replace(context, connection=reader),
                    case.binding,
                    LIMITS,
                    "local",
                    observation,
                    interval=INTERVAL,
                )

    assert await case.saved_waits(database) == []
    assert await lock_inventory(database) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["readonly", "repeatable"])
async def test_admission_wait_refuses_unsupported_writer_mode(
    database: Database, mode: str
) -> None:
    case = await admission_case(database)
    observation = CapacityLimit("global", "max_running_attempts", 1, 1, None)

    async with database.transaction() as writer:
        if database.dialect is Dialect.POSTGRESQL:
            await writer.execute(
                text(
                    "SET TRANSACTION READ ONLY"
                    if mode == "readonly"
                    else "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"
                )
            )
        result = await save_admission_wait(
            await case.context(writer),
            case.binding,
            LIMITS,
            "local",
            observation,
            interval=INTERVAL,
        )

    assert result is (
        AdmissionWaitState.REFUSED
        if database.dialect is Dialect.POSTGRESQL
        else AdmissionWaitState.CREATED
    )


@pytest.mark.asyncio
async def test_admission_wait_cannot_revive_cancelled_run(database: Database) -> None:
    case = await admission_case(database)
    async with database.transaction() as writer:
        await writer.execute(
            update(run)
            .where(run.c.id == case.promotion.run_id)
            .values(status="cancelled")
        )
    before = await case.promotion.snapshot(database)

    async with database.transaction() as writer:
        result = await case.apply(await case.context(writer))

    assert result is AdmissionWaitState.REFUSED
    assert await case.saved_waits(database) == []
    assert await case.promotion.snapshot(database) == before


@pytest.mark.asyncio
async def test_admission_wait_statement_failure_rolls_back_partial_writes(
    database: Database,
) -> None:
    case = await admission_case(database)
    ledger = ConnectionLedger(database)
    identifiers = IdentifierGenerator(
        case.promotion.clock.now, fixed_randomness(1_000_000)
    )
    probe = IdentifierGenerator(case.promotion.clock.now, fixed_randomness(1_000_000))
    probe.new()
    collision = probe.new()
    async with database.transaction() as writer:
        await writer.execute(
            insert(event).values(
                id=collision,
                namespace_id=case.promotion.scope.namespace_id,
                run_id=case.promotion.run_id,
                kind="fixture.existing",
                actor={"kind": "system"},
                payload={"private": "must-not-escape"},
                at=START,
                created_at=START,
                updated_at=START,
            )
        )
    before = await case.promotion.snapshot(database)

    async def apply() -> None:
        async with database.transaction() as writer:
            context = replace(await case.context(writer), identifiers=identifiers)
            await case.apply(context)

    with pytest.raises(AdmissionWaitFailed) as caught:
        await apply()

    assert str(caught.value) == "admission wait transition failed"
    assert caught.value.__cause__ is caught.value.__context__ is None
    assert await case.saved_waits(database) == []
    assert await case.promotion.snapshot(database) == before
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed", ["run", "namespace", "pointer", "interval", "bounds"]
)
async def test_admission_wait_refuses_inconsistent_binding_or_admission(
    database: Database, changed: str
) -> None:
    case = await admission_case(database)
    binding = case.binding
    limits = LIMITS
    interval = INTERVAL
    if changed in ("run", "namespace"):
        values = {"run_id" if changed == "run" else "namespace_id": binding.attempt_id}
        binding = replace(binding, run=replace(binding.run, **values))
    elif changed == "bounds":
        limits = replace(limits, node_row=1)
    elif changed == "interval":
        interval = timedelta(0)
    else:
        async with database.transaction() as writer:
            await writer.execute(
                update(node_instance)
                .where(node_instance.c.id == binding.node_id)
                .values(current_attempt_id=case.blocker)
            )
    before = await case.promotion.snapshot(database)

    async with database.transaction() as writer:
        result = await save_admission_wait(
            await case.context(writer),
            binding,
            limits,
            "local",
            CapacityLimit("global", "max_running_attempts", 1, 1, None),
            interval=interval,
        )

    if changed == "bounds" and database.dialect is Dialect.POSTGRESQL:
        assert result is AdmissionWaitState.CREATED
    else:
        assert result is AdmissionWaitState.REFUSED
        assert await case.saved_waits(database) == []
        assert await case.promotion.snapshot(database) == before
