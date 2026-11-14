import asyncio
from dataclasses import replace

import pytest

from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database
from kora.engine.heartbeat_evidence import HeartbeatEvidenceRejected
from kora.engine.worker_history import HistoryRead, read_worker_history
from kora.engine.worker_registration import register_worker
from kora.protocols.evidence import ClosureEvidence
from kora.protocols.history import GrantHistoryPosition
from tests.api.deployment_cases import evaluator
from tests.engine.claim_support import HISTORY_LIMITS, WORKER_BYTES, claim_case, state
from tests.engine.heartbeat_bounds import maximal_closure
from tests.engine.heartbeat_support import heartbeat, heartbeat_body, self_closure
from tests.plugins.authentication.support import AuthorityClock

__all__ = ["evaluator"]


@pytest.mark.asyncio
@pytest.mark.parametrize("process_exit", [False, True])
async def test_recovery_relay_closes_earlier_grants(
    database: Database, evaluator: Evaluator, process_exit: bool
) -> None:
    process = maximal_closure().process
    assert process is not None
    case = await claim_case(database, evaluator, process=process)
    first = await case.claim(database)
    assert first.delivery is not None
    original = first.delivery.system
    case.clock.advance(121)
    next_registration = replace(
        case.registration,
        incarnation_id="next-incarnation",
        process=process.model_copy(update={"pid": process.pid - 1}),
    )
    async with database.transaction() as writer:
        await register_worker(
            writer,
            next_registration,
            case.principal,
            portable=False,
            now=case.clock.now(),
            event_id=case.identifiers.new(),
        )
    current = replace(
        case,
        registration=next_registration,
        query=case.query.model_copy(
            update={
                "incarnation_id": next_registration.incarnation_id,
                "claim_id": "second",
            }
        ),
    )
    second = await current.claim(database)
    assert second.delivery is not None
    granted = second.delivery.system
    closure = self_closure(original)
    if process_exit:
        closure = closure.model_copy(
            update={"provenance": "runtime_observed_process_exit", "process": process}
        )
    body = heartbeat_body(granted).model_copy(
        update={
            "grant_history_complete": granted.grant_history,
            "evidence": ClosureEvidence(
                runtime=None, dispatchers=(closure,), publication=None
            ),
        }
    )
    clock = AuthorityClock(case.clock.now())

    result = await heartbeat(database, current, clock, body)
    after = await state(database)
    page = await read_worker_history(
        database,
        HistoryRead(
            granted.attempt_id,
            granted.current_grant.dispatcher_id,
            granted.lease_epoch,
            granted.grant_history.tip(),
            GrantHistoryPosition(
                after_epoch=None,
                received_count=0,
                prefix_digest=granted.grant_history.seed(),
            ),
        ),
        current.principal,
        clock,
        HISTORY_LIMITS,
        WORKER_BYTES,
        deadline=asyncio.get_running_loop().time() + 10,
    )
    replay = await heartbeat(database, current, clock, body)
    repeated = await state(database)
    conflicting = (
        closure.model_copy(
            update={"process": process.model_copy(update={"leads_group": False})}
        )
        if process_exit
        else closure.model_copy(
            update={
                "quiescence": closure.quiescence.model_copy(
                    update={"evidence_ref": "different-record"}
                )
            }
        )
    )
    with pytest.raises(HeartbeatEvidenceRejected) as refusal:
        await heartbeat(
            database,
            current,
            clock,
            body.model_copy(
                update={
                    "evidence": ClosureEvidence(
                        runtime=None,
                        dispatchers=(self_closure(granted), conflicting),
                        publication=None,
                    )
                }
            ),
        )
    refused = await state(database)

    assert refusal.value.details.item == "dispatchers"
    assert refusal.value.details.index == 1
    assert refusal.value.details.reason == "evidence_conflict"
    assert refused == after
    assert result == replay
    assert result.mode == "execute"
    assert result.permitted_actions == ("observe", "retire")
    assert result.grant_history_verified == granted.grant_history
    assert after == repeated
    assert after["attempt"][0]["recovery_required"] is True
    assert after["attempt"][0]["current_dispatcher_open"] is True
    assert closure.quiescence.dispatcher_id.encode() in page
    assert (
        len(
            [
                item
                for item in after["attempt_dispatcher"]
                if item["closed_at"] is not None
            ]
        )
        == 1
    )


@pytest.mark.asyncio
async def test_process_exit_cannot_close_reporting_incarnation(
    database: Database, evaluator: Evaluator
) -> None:
    process = maximal_closure().process
    assert process is not None
    case = await claim_case(database, evaluator, process=process)
    decision = await case.claim(database)
    assert decision.delivery is not None
    granted = decision.delivery.system
    closure = self_closure(granted).model_copy(
        update={"provenance": "runtime_observed_process_exit", "process": process}
    )
    body = heartbeat_body(granted).model_copy(
        update={
            "evidence": ClosureEvidence(
                runtime=None, dispatchers=(closure,), publication=None
            )
        }
    )
    before = await state(database)

    with pytest.raises(HeartbeatEvidenceRejected) as refused:
        await heartbeat(database, case, AuthorityClock(), body)
    after = await state(database)

    assert refused.value.details.reason == "evidence_unbound"
    assert before == after
