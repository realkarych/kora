from datetime import timedelta

import pytest
from sqlalchemy import update

from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database
from kora.db.tables import run
from kora.engine.heartbeat_evidence import HeartbeatEvidenceRejected
from kora.engine.worker_history import HistoryAccessRejected
from kora.models.errors import DispatcherEvidenceDetails
from kora.protocols.evidence import ClosureEvidence
from tests.api.deployment_cases import evaluator
from tests.engine.claim_support import claim_case, state
from tests.engine.heartbeat_support import heartbeat, heartbeat_body, self_closure
from tests.plugins.authentication.support import AuthorityClock

__all__ = ["evaluator"]


@pytest.mark.asyncio
async def test_heartbeat_renews_lease_and_same_time_replay_writes_nothing(
    database: Database, evaluator: Evaluator
) -> None:
    case = await claim_case(database, evaluator)
    decision = await case.claim(database)
    assert decision.delivery is not None
    granted = decision.delivery.system
    body = heartbeat_body(granted)
    clock = AuthorityClock(case.clock.now() + timedelta(seconds=15))

    accepted = await heartbeat(database, case, clock, body)
    after = await state(database)
    replay = await heartbeat(database, case, clock, body)
    repeated = await state(database)

    assert accepted == replay
    assert accepted.lease_until == clock.now() + timedelta(seconds=120)
    assert accepted.permitted_actions == ("seal_request",)
    assert accepted.grant_history == granted.grant_history
    assert accepted.grant_history_verified is None
    assert "publication" not in accepted.model_dump(exclude_unset=True)
    assert repeated == after
    assert not after["attempt"][0]["recovery_required"]
    assert after["worker"][0]["last_seen_at"] == clock.now()


@pytest.mark.asyncio
async def test_history_ack_and_self_closure_survive_response_loss(
    database: Database, evaluator: Evaluator
) -> None:
    case = await claim_case(database, evaluator)
    decision = await case.claim(database)
    assert decision.delivery is not None
    granted = decision.delivery.system
    closure = self_closure(granted)
    body = heartbeat_body(granted).model_copy(
        update={
            "grant_history_complete": granted.grant_history,
            "evidence": ClosureEvidence(
                runtime=None, dispatchers=(closure,), publication=None
            ),
        }
    )
    clock = AuthorityClock()

    first = await heartbeat(database, case, clock, body)
    after = await state(database)
    replay = await heartbeat(database, case, clock, body)
    repeated = await state(database)

    assert first == replay
    assert first.permitted_actions == ("observe", "retire", "report_result")
    assert first.grant_history_verified == granted.grant_history
    assert after == repeated
    assert after["attempt"][0]["current_dispatcher_open"] is False
    assert after["attempt_dispatcher"][0]["evidence"] == closure.model_dump(mode="json")
    assert (
        len(
            [
                item
                for item in after["event"]
                if item["kind"] == "attempt.dispatcher_closed"
            ]
        )
        == 1
    )


@pytest.mark.asyncio
async def test_conflicting_closure_refuses_whole_heartbeat(
    database: Database, evaluator: Evaluator
) -> None:
    case = await claim_case(database, evaluator)
    decision = await case.claim(database)
    assert decision.delivery is not None
    granted = decision.delivery.system
    closure = self_closure(granted)
    body = heartbeat_body(granted).model_copy(
        update={
            "evidence": ClosureEvidence(
                runtime=None, dispatchers=(closure,), publication=None
            )
        }
    )
    clock = AuthorityClock()
    await heartbeat(database, case, clock, body)
    before = await state(database)
    conflicting = closure.model_copy(
        update={
            "quiescence": closure.quiescence.model_copy(
                update={"evidence_ref": "other"}
            )
        }
    )
    request = body.model_copy(
        update={
            "grant_history_complete": granted.grant_history,
            "evidence": ClosureEvidence(
                runtime=None, dispatchers=(conflicting,), publication=None
            ),
        }
    )
    clock.moment += timedelta(seconds=15)

    with pytest.raises(HeartbeatEvidenceRejected) as refusal:
        await heartbeat(database, case, clock, request)
    after = await state(database)

    assert refusal.value.details == DispatcherEvidenceDetails(
        reason="evidence_conflict",
        item="dispatchers",
        index=0,
        dispatcher_id=closure.quiescence.dispatcher_id,
        lease_epoch=closure.quiescence.lease_generation,
    )
    assert before == after


@pytest.mark.asyncio
async def test_expired_heartbeat_never_renews_or_accepts_ack(
    database: Database, evaluator: Evaluator
) -> None:
    case = await claim_case(database, evaluator)
    decision = await case.claim(database)
    assert decision.delivery is not None
    granted = decision.delivery.system
    before = await state(database)
    body = heartbeat_body(granted).model_copy(
        update={"grant_history_complete": granted.grant_history}
    )

    with pytest.raises(HistoryAccessRejected) as refusal:
        await heartbeat(database, case, AuthorityClock(granted.lease_until), body)
    after = await state(database)

    assert refusal.value.reason == "stale"
    assert after == before


@pytest.mark.asyncio
async def test_terminal_run_heartbeat_retains_control_and_resources(
    database: Database, evaluator: Evaluator
) -> None:
    case = await claim_case(database, evaluator)
    decision = await case.claim(database)
    assert decision.delivery is not None
    granted = decision.delivery.system
    async with database.transaction() as writer:
        await writer.execute(
            update(run)
            .where(run.c.id == case.binding.run.run_id)
            .values(status="cancelled")
        )
    body = heartbeat_body(granted).model_copy(
        update={"grant_history_complete": granted.grant_history}
    )

    result = await heartbeat(database, case, AuthorityClock(), body)
    after = await state(database)

    assert result.desired_state == "cancel"
    assert result.permitted_actions == ("observe", "retire")
    assert after["attempt"][0]["recovery_required"] is True
    assert after["attempt"][0]["status"] == "claimed"
    assert after["attempt"][0]["current_dispatcher_open"] is True
