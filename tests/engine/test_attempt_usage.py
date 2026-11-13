import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, insert, select, update

from kora.conformance.capture import captured
from kora.db.connection import Database
from kora.db.tables import (
    attempt,
    attempt_dispatcher,
    event,
    node_instance,
    price_table,
    run,
    usage_record,
    worker_incarnation,
)
from kora.engine.attempt_usage import (
    COMPONENT,
    UsageAccess,
    UsageAccessRejected,
    report_usage,
)
from kora.engine.locks import TransitionLocks
from kora.engine.usage_acceptance import UsageRejected
from kora.ir.cost import PriceRevision
from kora.observability import Level, LoggingSettings
from tests.db.closing import ConnectionLedger
from tests.db.seed import START
from tests.engine.lock_support import DEADLINE, lock_inventory, wait_for_lock_waiter
from tests.engine.test_usage_acceptance import coverage, usage
from tests.engine.worker_usage_support import submit_usage, usage_case, usage_snapshot
from tests.ir.test_cost_models import rates_payload, revision_payload


@pytest.mark.asyncio
async def test_usage_transaction_prices_marks_and_preserves_exact_replay(
    database: Database,
) -> None:
    case = await usage_case(database)
    row = usage(input_tokens=1_000_000, source={"private\0": [1, 1.0, -0.0]})
    ledger = ConnectionLedger(database)

    accepted = await submit_usage(database, case, (row,))
    async with database.transaction() as connection:
        stored = (await connection.execute(select(usage_record))).mappings().one()
        marked = await connection.scalar(select(run.c.needs_scheduling))
        audit_count = await connection.scalar(select(func.count()).select_from(event))
        await connection.execute(update(run).values(needs_scheduling=False))
    before = await usage_snapshot(database, case)
    case.clock.moment += timedelta(seconds=1)
    replayed = await submit_usage(database, case, (row,))
    after = await usage_snapshot(database, case)

    assert accepted.accepted_rows[0].usd_estimate == "1"
    assert stored["usd_estimate"] == stored["same_basis_usd_floor"] == Decimal("1")
    assert stored["worker_id"] == case.principal.owner
    assert stored["image_digest"] == "server-image"
    assert stored["source"] == row.model_dump(mode="json")["source"]
    assert marked is True
    assert audit_count == 1
    assert before == after
    assert replayed.changed_rows == ()
    assert replayed.coverage_changed is False
    assert ledger.outstanding == 0


@pytest.mark.asyncio
async def test_usage_batch_model_cap_is_atomic(database: Database) -> None:
    case = await usage_case(database)
    rows = tuple(usage(model=f"model-{index}") for index in range(64))
    await submit_usage(database, case, rows)
    before = await usage_snapshot(database, case)

    with pytest.raises(UsageRejected):
        await submit_usage(
            database,
            case,
            (usage(model="extra"), usage(model="model-0", observation_sequence=1)),
            coverage(1),
        )
    after = await usage_snapshot(database, case)

    assert after == before


@pytest.mark.asyncio
async def test_usage_persists_floor_across_unknown_estimates(
    database: Database,
) -> None:
    case = await usage_case(database, {"cache_creation_1h": "2"})
    first = usage(
        cache_creation_tokens=1_000_000,
        cache_creation_by_ttl={"5m": 0, "1h": 1_000_000},
    )
    unknown = usage(cache_creation_tokens=1_000_000, observation_sequence=1)
    lower = usage(
        cache_creation_tokens=1_000_000,
        cache_creation_by_ttl={"5m": 1_000_000, "1h": 0},
        observation_sequence=2,
    )
    await submit_usage(database, case, (first,))

    accepted = await submit_usage(database, case, (unknown,))
    before = await usage_snapshot(database, case)
    with pytest.raises(UsageRejected):
        await submit_usage(database, case, (lower,))
    after = await usage_snapshot(database, case)
    corrected = await submit_usage(
        database,
        case,
        (
            usage(
                cache_creation_tokens=1_000_000,
                observation_sequence=2,
                basis="harness_list",
                reported_usd="1",
            ),
        ),
    )

    assert accepted.accepted_rows[0].usd_estimate is None
    assert accepted.accepted_rows[0].same_basis_usd_floor == "2"
    assert after == before
    assert corrected.accepted_rows[0].usd_estimate == "1"
    assert corrected.accepted_rows[0].same_basis_usd_floor == "1"


@pytest.mark.asyncio
async def test_usage_rechecks_grant_after_waiting_for_run_lock(
    database: Database,
) -> None:
    case = await usage_case(database)
    ready, release = asyncio.Event(), asyncio.Event()
    ledger = ConnectionLedger(database)

    async def predecessor() -> None:
        async with database.transaction() as connection:
            locks = TransitionLocks(connection)
            await locks.acquire_run(case.context.run_id)
            await connection.execute(
                update(attempt)
                .where(attempt.c.id == case.attempt_id)
                .values(lease_epoch=2)
            )
            ready.set()
            await asyncio.wait_for(release.wait(), DEADLINE)

    owner = asyncio.create_task(predecessor())
    await asyncio.wait_for(ready.wait(), DEADLINE)
    contender = asyncio.create_task(submit_usage(database, case, (usage(),)))

    try:
        await wait_for_lock_waiter(database)
        release.set()
        await asyncio.wait_for(owner, DEADLINE)
        with pytest.raises(UsageAccessRejected) as refused:
            await asyncio.wait_for(contender, DEADLINE)
    finally:
        release.set()
        await asyncio.gather(owner, contender, return_exceptions=True)
    snapshot = await usage_snapshot(database, case)

    assert refused.value.reason is UsageAccess.STALE_GRANT
    assert snapshot[0] == ()
    assert snapshot[2] is False
    assert await lock_inventory(database) == []
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_usage_uses_pinned_price_and_content_free_diagnostics(
    database: Database,
) -> None:
    case = await usage_case(database)
    row = usage(
        input_tokens=1_000_000, source={"private-usage-source": "private-value"}
    )
    next_price = PriceRevision.model_validate(
        {
            **revision_payload({"model": rates_payload("100")}),
            "id": "later-price",
            "effective_from": START + timedelta(seconds=1),
        }
    )
    async with database.transaction() as connection:
        await connection.execute(
            insert(price_table).values(
                **next_price.model_dump(mode="python"),
                model=None,
                created_at=START,
                updated_at=START,
            )
        )
    case.clock.moment += timedelta(seconds=2)

    with captured(
        COMPONENT, LoggingSettings(level=Level.DEBUG), "usage-test", case.clock.now
    ) as capture:
        accepted = await submit_usage(database, case, (row,))
        records = capture.finish()

    assert accepted.accepted_rows[0].usd_estimate == "1"
    prepared = [
        record for record in records if record["event"] == "engine.usage.prepared"
    ]
    assert len(prepared) == 1
    assert prepared[0]["changed_rows"] == 1
    assert prepared[0]["coverage_changed"] is True
    assert "private-usage-source" not in capture.reader.data.decode()
    assert "private-value" not in capture.reader.data.decode()


@pytest.mark.asyncio
@pytest.mark.parametrize("binding", ["namespace", "graph", "node", "digest", "version"])
async def test_usage_refuses_changed_prepared_scope(
    database: Database, binding: str
) -> None:
    case = await usage_case(database)
    fields = {
        "namespace": "namespace_id",
        "graph": "graph_id",
        "node": "node_path",
        "digest": "execution_contract_digest",
        "version": "graph_version_id",
    }
    changed = replace(case, context=replace(case.context, **{fields[binding]: "other"}))
    before = await usage_snapshot(database, case)

    with pytest.raises(UsageAccessRejected) as refused:
        await submit_usage(database, changed, (usage(),))
    after = await usage_snapshot(database, case)

    assert refused.value.reason is UsageAccess.NOT_FOUND
    assert after == before


@pytest.mark.asyncio
async def test_usage_model_union_serializes_two_competing_reports(
    database: Database,
) -> None:
    case = await usage_case(database)
    await submit_usage(
        database, case, tuple(usage(model=f"model-{i}") for i in range(63))
    )
    ready, release = asyncio.Event(), asyncio.Event()

    async def first_report() -> None:
        async with database.transaction() as connection:
            locks = TransitionLocks(connection)
            await locks.acquire_run(case.context.run_id)
            await report_usage(
                connection,
                locks,
                case.context,
                case.attempt_id,
                case.dispatcher_id,
                1,
                case.principal,
                (usage(model="first"),),
                coverage(),
                case.clock,
                case.identifiers,
            )
            ready.set()
            await asyncio.wait_for(release.wait(), DEADLINE)

    first = asyncio.create_task(first_report())
    await asyncio.wait_for(ready.wait(), DEADLINE)
    second = asyncio.create_task(submit_usage(database, case, (usage(model="second"),)))

    try:
        await wait_for_lock_waiter(database)
        release.set()
        await asyncio.wait_for(first, DEADLINE)
        with pytest.raises(UsageRejected):
            await asyncio.wait_for(second, DEADLINE)
    finally:
        release.set()
        await asyncio.gather(first, second, return_exceptions=True)
    async with database.read_transaction() as connection:
        models = (
            (await connection.execute(select(usage_record.c.model))).scalars().all()
        )

    assert len(models) == 64
    assert "first" in models
    assert "second" not in models
    assert await lock_inventory(database) == []


@pytest.mark.asyncio
async def test_usage_cancellation_rolls_back_rows_coverage_and_marker(
    database: Database,
) -> None:
    case = await usage_case(database)
    ready, never = asyncio.Event(), asyncio.Event()
    before = await usage_snapshot(database, case)
    ledger = ConnectionLedger(database)

    async def interrupted_report() -> None:
        async with database.transaction() as connection:
            locks = TransitionLocks(connection)
            await locks.acquire_run(case.context.run_id)
            await report_usage(
                connection,
                locks,
                case.context,
                case.attempt_id,
                case.dispatcher_id,
                1,
                case.principal,
                (usage(),),
                coverage(),
                case.clock,
                case.identifiers,
            )
            ready.set()
            await asyncio.wait_for(never.wait(), DEADLINE)

    writer = asyncio.create_task(interrupted_report())
    await asyncio.wait_for(ready.wait(), DEADLINE)

    writer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(writer, DEADLINE)
    after = await usage_snapshot(database, case)
    accepted = await submit_usage(database, case, (usage(),))

    assert after == before
    assert len(accepted.changed_rows) == 1
    assert await lock_inventory(database) == []
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_usage_coverage_only_is_atomic_and_replayed(database: Database) -> None:
    case = await usage_case(database)
    await submit_usage(database, case, (), coverage())
    before = await usage_snapshot(database, case)

    replay = await submit_usage(database, case, (), coverage())
    with pytest.raises(UsageRejected):
        await submit_usage(database, case, (), coverage(observation_gap=True))
    after = await usage_snapshot(database, case)

    assert replay.changed_rows == ()
    assert replay.coverage_changed is False
    assert after == before
    assert before[0] == ()
    assert before[2] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state", ["cancelled", "superseded", "finished", "closed", "expired"]
)
async def test_usage_accepts_late_history_without_result_authority(
    database: Database, state: str
) -> None:
    case = await usage_case(database)
    async with database.transaction() as connection:
        if state == "closed":
            await connection.execute(update(attempt_dispatcher).values(closed_at=START))
        elif state == "finished":
            await connection.execute(update(attempt).values(status="succeeded"))
            await connection.execute(
                update(run).values(status="succeeded", finished_at=START)
            )
        elif state == "superseded":
            await connection.execute(
                update(node_instance).values(current_attempt_id=None)
            )
        elif state == "expired":
            await connection.execute(update(attempt).values(deadline_expired_at=START))
        else:
            await connection.execute(update(attempt).values(desired_state="cancel"))
            await connection.execute(
                update(run).values(status="cancelled", finished_at=START)
            )

    accepted = await submit_usage(database, case, (usage(),))

    assert len(accepted.changed_rows) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed",
    [
        "epoch",
        "dispatcher",
        "incarnation",
        "retained_domain",
        "domain",
        "expiry",
        "worker",
        "token",
    ],
)
async def test_usage_refuses_current_grant_changes_without_writes(
    database: Database, changed: str
) -> None:
    case = await usage_case(database)
    async with database.transaction() as connection:
        if changed == "epoch":
            await connection.execute(update(attempt).values(lease_epoch=2))
        elif changed == "incarnation":
            await connection.execute(
                update(attempt_dispatcher).values(incarnation_id="unregistered")
            )
        elif changed == "retained_domain":
            await connection.execute(
                update(worker_incarnation).values(execution_domain_id="other")
            )
        elif changed == "domain":
            await connection.execute(
                update(attempt).values(execution_domain_id="new-domain")
            )
        elif changed == "expiry":
            case.clock.moment += timedelta(seconds=120)
        elif changed == "worker":
            case = replace(
                case, principal=case.principal.model_copy(update={"owner": "other"})
            )
        elif changed == "token":
            case = replace(
                case, principal=case.principal.model_copy(update={"token_id": "other"})
            )
        else:
            case = replace(case, dispatcher_id="other")
    before = await usage_snapshot(database, case)

    with pytest.raises(UsageAccessRejected) as refused:
        await submit_usage(database, case, (usage(),))
    after = await usage_snapshot(database, case)

    assert refused.value.reason is (
        UsageAccess.FORBIDDEN
        if changed in {"worker", "token"}
        else UsageAccess.STALE_GRANT
    )
    assert after == before
