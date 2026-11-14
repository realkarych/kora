from dataclasses import replace

import pytest
from sqlalchemy import update

from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database
from kora.db.grant_history import HistoryScope
from kora.db.tables import attempt
from kora.engine.grant_history import append_grant
from kora.engine.heartbeat_evidence import HeartbeatEvidenceRejected
from kora.protocols.evidence import (
    ClosureEvidence,
    DispatcherClosure,
    DispatcherQuiescence,
)
from kora.protocols.identity import GrantBinding
from tests.api.deployment_cases import evaluator
from tests.engine.claim_support import HISTORY_LIMITS, claim_case, state
from tests.engine.heartbeat_support import heartbeat, heartbeat_body, self_closure
from tests.plugins.authentication.support import AuthorityClock

__all__ = ["evaluator"]


@pytest.mark.asyncio
async def test_maximum_heartbeat_batch_is_atomic_and_replays_without_new_events(
    database: Database, evaluator: Evaluator
) -> None:
    case = await claim_case(database, evaluator)
    first = await case.claim(database)
    assert first.delivery is not None
    original = first.delivery.system
    await heartbeat(
        database,
        case,
        AuthorityClock(),
        heartbeat_body(original).model_copy(
            update={
                "evidence": ClosureEvidence(
                    runtime=None,
                    dispatchers=(self_closure(original),),
                    publication=None,
                )
            }
        ),
    )
    scope = HistoryScope(
        case.binding.run.namespace_id,
        case.binding.run.run_id,
        case.binding.node_id,
        original.attempt_id,
        original.execution_domain_id,
    )
    closures: list[DispatcherClosure] = []
    async with database.transaction() as writer:
        context = await case.context(writer)
        for epoch in range(2, 129):
            grant = GrantBinding(
                dispatcher_id=f"retained-{epoch}",
                worker_id=case.principal.owner,
                incarnation_id=case.query.incarnation_id,
                lease_epoch=epoch,
                execution_domain_id=original.execution_domain_id,
                process=None,
            )
            await append_grant(
                writer,
                context.locks,
                scope,
                grant,
                "recover",
                case.clock.now(),
                HISTORY_LIMITS,
            )
            closures.append(
                DispatcherClosure(
                    quiescence=DispatcherQuiescence(
                        attempt_id=original.attempt_id,
                        execution_domain_id=original.execution_domain_id,
                        dispatcher_id=grant.dispatcher_id,
                        worker_incarnation_id=grant.incarnation_id,
                        lease_generation=epoch,
                        closed_at=case.clock.now(),
                        evidence_ref="registry/closure",
                    ),
                    provenance="registry_acknowledgement",
                    process=None,
                )
            )
        await writer.execute(
            update(attempt)
            .where(attempt.c.id == original.attempt_id)
            .values(lease_epoch=128)
        )
    case.clock.advance(121)
    current = replace(
        case, query=case.query.model_copy(update={"claim_id": "batch-recovery"})
    )
    claimed = await current.claim(database)
    assert claimed.delivery is not None
    granted = claimed.delivery.system
    closures.append(self_closure(granted))
    body = heartbeat_body(granted).model_copy(
        update={
            "grant_history_complete": granted.grant_history,
            "evidence": ClosureEvidence(
                runtime=None, dispatchers=tuple(closures), publication=None
            ),
        }
    )
    broken = closures[-1].model_copy(
        update={
            "quiescence": closures[-1].quiescence.model_copy(
                update={"lease_generation": 130}
            )
        }
    )
    refused_body = body.model_copy(
        update={
            "evidence": ClosureEvidence(
                runtime=None, dispatchers=(*closures[:-1], broken), publication=None
            )
        }
    )
    clock = AuthorityClock(case.clock.now())
    before = await state(database)

    with pytest.raises(HeartbeatEvidenceRejected) as refusal:
        await heartbeat(database, current, clock, refused_body)
    after_refusal = await state(database)
    accepted = await heartbeat(database, current, clock, body)
    after_acceptance = await state(database)
    replay = await heartbeat(database, current, clock, body)
    after_replay = await state(database)

    assert refusal.value.details.item == "dispatchers"
    assert refusal.value.details.index == 127
    assert refusal.value.details.reason == "evidence_unbound"
    assert before == after_refusal
    assert accepted == replay
    assert after_acceptance == after_replay
    assert accepted.grant_history_verified == granted.grant_history
    assert granted.grant_history.count == 129
    assert len(after_acceptance["event"]) == len(before["event"]) + 128
    previous_version = before["attempt"][0]["recovery_version"]
    assert isinstance(previous_version, int)
    assert after_acceptance["attempt"][0]["recovery_version"] == previous_version + 128
    assert after_acceptance["attempt"][0]["current_dispatcher_open"] is False
    assert all(
        row["closed_at"] is not None for row in after_acceptance["attempt_dispatcher"]
    )
    assert database.units_in_flight() == 0
    assert database.writer_is_free()
