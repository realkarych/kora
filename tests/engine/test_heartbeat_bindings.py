from dataclasses import replace

import pytest
from sqlalchemy import update

from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database
from kora.db.tables import attempt_dispatcher
from kora.engine.heartbeat_evidence import HeartbeatEvidenceRejected
from kora.engine.worker_registration import register_worker
from kora.protocols.evidence import (
    ClosureEvidence,
    EvidenceScope,
    NormalCompletionQuiescence,
)
from tests.api.deployment_cases import evaluator
from tests.engine.claim_support import claim_case, state
from tests.engine.heartbeat_bounds import maximal_closure
from tests.engine.heartbeat_support import heartbeat, heartbeat_body, self_closure
from tests.plugins.authentication.support import AuthorityClock

__all__ = ["evaluator"]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["attempt_id", "execution_domain_id"])
async def test_runtime_evidence_must_bind_original_attempt_and_domain(
    database: Database, evaluator: Evaluator, field: str
) -> None:
    case = await claim_case(database, evaluator)
    claim = await case.claim(database)
    assert claim.delivery is not None
    grant = claim.delivery.system
    record = NormalCompletionQuiescence(
        kind="normal_completion",
        attempt_id=grant.attempt_id,
        execution_domain_id=grant.execution_domain_id,
        record_ref="complete",
        observed_at=case.clock.now(),
        scope=EvidenceScope.ENV_SWEEP,
    ).model_copy(update={field: "foreign"})
    body = heartbeat_body(grant).model_copy(
        update={
            "grant_history_complete": grant.grant_history,
            "evidence": ClosureEvidence(
                runtime=record, dispatchers=(), publication=None
            ),
        }
    )
    before = await state(database)

    with pytest.raises(HeartbeatEvidenceRejected) as refusal:
        await heartbeat(database, case, AuthorityClock(), body)
    after = await state(database)

    assert refusal.value.details.item == "runtime"
    assert refusal.value.details.reason == "evidence_unbound"
    assert before == after


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "attempt_id",
        "execution_domain_id",
        "dispatcher_id",
        "worker_incarnation_id",
        "lease_generation",
        "process_mismatch",
        "saved_null",
        "saved_incomplete",
        "saved_nonleader",
    ],
)
async def test_relayed_process_exit_must_match_complete_retained_grant(
    database: Database, evaluator: Evaluator, change: str
) -> None:
    process = maximal_closure().process
    assert process is not None
    case = await claim_case(database, evaluator, process=process)
    first = await case.claim(database)
    assert first.delivery is not None
    original = first.delivery.system
    case.clock.advance(121)
    registration = replace(
        case.registration,
        incarnation_id="next-incarnation",
        process=process.model_copy(update={"pid": process.pid - 1}),
    )
    async with database.transaction() as writer:
        await register_worker(
            writer,
            registration,
            case.principal,
            portable=False,
            now=case.clock.now(),
            event_id=case.identifiers.new(),
        )
    current = replace(
        case,
        registration=registration,
        query=case.query.model_copy(
            update={
                "incarnation_id": registration.incarnation_id,
                "claim_id": "recovery",
            }
        ),
    )
    claimed = await current.claim(database)
    assert claimed.delivery is not None
    grant = claimed.delivery.system
    closure = self_closure(original).model_copy(
        update={
            "provenance": "runtime_observed_process_exit",
            "process": process,
        }
    )
    if change.startswith("saved_"):
        saved = (
            None
            if change == "saved_null"
            else process.model_copy(
                update={"host_id": None}
                if change == "saved_incomplete"
                else {"leads_group": False}
            ).model_dump(mode="json")
        )
        async with database.transaction() as writer:
            await writer.execute(
                update(attempt_dispatcher)
                .where(attempt_dispatcher.c.id == original.current_grant.dispatcher_id)
                .values(process=saved)
            )
    elif change == "process_mismatch":
        closure = closure.model_copy(
            update={"process": process.model_copy(update={"pid": process.pid - 2})}
        )
    else:
        closure = closure.model_copy(
            update={
                "quiescence": closure.quiescence.model_copy(
                    update={change: 999 if change == "lease_generation" else "foreign"}
                )
            }
        )
    body = heartbeat_body(grant).model_copy(
        update={
            "grant_history_complete": grant.grant_history,
            "evidence": ClosureEvidence(
                runtime=None, dispatchers=(closure,), publication=None
            ),
        }
    )
    before = await state(database)

    with pytest.raises(HeartbeatEvidenceRejected) as refusal:
        await heartbeat(database, current, AuthorityClock(case.clock.now()), body)
    after = await state(database)

    assert refusal.value.details.item == "dispatchers"
    assert refusal.value.details.index == 0
    assert refusal.value.details.dispatcher_id == closure.quiescence.dispatcher_id
    assert refusal.value.details.lease_epoch == closure.quiescence.lease_generation
    assert refusal.value.details.reason == "evidence_unbound"
    assert before == after
