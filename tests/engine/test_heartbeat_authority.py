import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database
from kora.db.tables import attempt, attempt_dispatcher, worker, worker_incarnation
from kora.engine.worker_history import HistoryAccessRejected
from kora.engine.worker_registration import register_worker
from tests.api.deployment_cases import evaluator
from tests.engine.claim_support import claim_case, state, wait_for_claim_waiter
from tests.engine.heartbeat_support import heartbeat, heartbeat_body
from tests.plugins.authentication.support import AuthorityClock

__all__ = ["evaluator"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("personal", "forbidden"),
        ("namespace", "not_found"),
        ("worker", "forbidden"),
        ("token", "forbidden"),
        ("runtime", "forbidden"),
        ("epoch", "stale"),
        ("dispatcher", "stale"),
        ("lease_owner", "stale"),
        ("domain", "stale"),
        ("incarnation", "stale"),
    ],
)
async def test_worker_write_requires_its_grant(
    database: Database, evaluator: Evaluator, change: str, reason: str
) -> None:
    case = await claim_case(database, evaluator)
    decision = await case.claim(database)
    assert decision.delivery is not None
    granted = decision.delivery.system
    body = heartbeat_body(granted)
    principal_changes: dict[str, dict[str, object]] = {
        "personal": {"kind": "user"},
        "namespace": {"namespaces": ()},
        "worker": {"owner": "another-worker"},
        "token": {"token_id": "another-token"},
        "runtime": {"runtime": "another-runtime"},
    }
    principal = case.principal.model_copy(update=principal_changes.get(change, {}))
    if change == "epoch":
        body = body.model_copy(update={"lease_epoch": body.lease_epoch + 1})
    elif change == "dispatcher":
        body = body.model_copy(update={"dispatcher_id": "another-dispatcher"})
    elif change in ("lease_owner", "domain", "incarnation"):
        async with database.transaction() as writer:
            if change == "lease_owner":
                await writer.execute(
                    update(attempt).values(lease_owner="another-worker")
                )
            elif change == "domain":
                await writer.execute(
                    update(attempt_dispatcher).values(
                        execution_domain_id="another-domain"
                    )
                )
            else:
                await writer.execute(
                    update(worker_incarnation).values(
                        execution_domain_id="another-domain"
                    )
                )
    before = await state(database)

    with pytest.raises(HistoryAccessRejected) as refusal:
        await heartbeat(database, case, AuthorityClock(), body, principal)
    after = await state(database)

    assert refusal.value.reason == reason
    assert before == after


@pytest.mark.asyncio
async def test_new_registration_preserves_retained_heartbeat_grant(
    database: Database, evaluator: Evaluator
) -> None:
    case = await claim_case(database, evaluator)
    decision = await case.claim(database)
    assert decision.delivery is not None
    granted = decision.delivery.system
    clock = AuthorityClock(case.clock.now() + timedelta(seconds=1))
    changed = replace(
        case.registration,
        incarnation_id="new-incarnation",
        process=case.registration.process.model_copy(update={"pid": 124}),
    )
    async with database.transaction() as writer:
        await register_worker(
            writer,
            changed,
            case.principal,
            portable=False,
            now=clock.now(),
            event_id=case.identifiers.new(),
        )

    result = await heartbeat(database, case, clock, heartbeat_body(granted))
    after = await state(database)

    assert result.permitted_actions == ("seal_request",)
    assert result.grant_history == granted.grant_history
    assert (
        after["attempt_dispatcher"][0]["incarnation_id"]
        == case.registration.incarnation_id
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("held", ["run", "worker"])
async def test_heartbeat_rechecks_lease_after_lock_wait(
    database: Database, evaluator: Evaluator, held: str
) -> None:
    case = await claim_case(database, evaluator)
    decision = await case.claim(database)
    assert decision.delivery is not None
    granted = decision.delivery.system
    clock = AuthorityClock()
    before = await state(database)
    body = heartbeat_body(granted).model_copy(
        update={"grant_history_complete": granted.grant_history}
    )

    async def waiting_heartbeat() -> str:
        with pytest.raises(HistoryAccessRejected) as refusal:
            await heartbeat(database, case, clock, body)
        return refusal.value.reason

    async with asyncio.timeout(10), asyncio.TaskGroup() as group:
        async with database.transaction() as writer:
            if held == "run":
                await case.context(writer)
            else:
                await writer.execute(
                    select(worker.c.id)
                    .where(worker.c.id == case.principal.owner)
                    .with_for_update()
                )
            pending = group.create_task(waiting_heartbeat())
            await wait_for_claim_waiter(database)
            clock.moment = granted.lease_until
    after = await state(database)

    assert pending.result() == "stale"
    assert after == before
    assert database.units_in_flight() == 0
    assert database.writer_is_free()
