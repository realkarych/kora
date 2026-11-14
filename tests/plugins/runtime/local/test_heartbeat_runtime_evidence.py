from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from kora.cel_eval.thread import Evaluator
from kora.conformance.capture import captured
from kora.db.connection import Database
from kora.engine.heartbeat_evidence import HeartbeatEvidenceRejected
from kora.engine.lease_recovery import recover_expired_lease
from kora.observability import Level, LoggingSettings
from kora.protocols.evidence import ClosureEvidence, EvidenceScope
from tests.api.deployment_cases import evaluator
from tests.engine.claim_support import claim_case, state
from tests.engine.heartbeat_support import heartbeat, heartbeat_body, self_closure
from tests.plugins.authentication.support import AuthorityClock
from tests.support.local_runtime import open_runtime, runtime_context, state_root

__all__ = ["evaluator"]


@pytest.mark.asyncio
async def test_actual_local_retirement_is_relayed_without_releasing_reservations(
    database: Database, evaluator: Evaluator, tmp_path: Path
) -> None:
    clock = AuthorityClock()
    root = state_root(tmp_path)
    with captured(
        "runtime.local", LoggingSettings(), "heartbeat-domain", clock.now
    ) as capture:
        initialized = await open_runtime(root, runtime_context(capture))
        try:
            domain = initialized.execution_domain_id
        finally:
            await initialized.close()
    case = await claim_case(database, evaluator, execution_domain_id=domain)
    decision = await case.claim(database)
    assert decision.delivery is not None
    granted = decision.delivery.system
    with captured(
        "runtime.local",
        LoggingSettings(level=Level.DEBUG),
        "heartbeat-retire",
        clock.now,
    ) as capture:
        ctx = runtime_context(capture)
        runtime = await open_runtime(root, ctx)
        try:
            scoped = replace(
                ctx,
                namespace_id=case.binding.run.namespace_id,
                run_id=case.binding.run.run_id,
                attempt_id=granted.attempt_id,
            )
            receipt = await runtime.retire(granted.attempt_id, timedelta(0), scoped)
            body = heartbeat_body(granted).model_copy(
                update={
                    "evidence": ClosureEvidence(
                        runtime=receipt,
                        dispatchers=(self_closure(granted),),
                        publication=None,
                    )
                }
            )

            await heartbeat(database, case, clock, body)
            after = await state(database)
            await heartbeat(database, case, clock, body)
            replay = await state(database)
            delayed = receipt.model_copy(
                update={"evidence_ref": "conflicting-reference"}
            )
            with pytest.raises(HeartbeatEvidenceRejected) as conflict:
                await heartbeat(
                    database,
                    case,
                    clock,
                    body.model_copy(
                        update={
                            "evidence": ClosureEvidence(
                                runtime=delayed, dispatchers=(), publication=None
                            )
                        }
                    ),
                )
            refused = await state(database)
        finally:
            await runtime.close()
    case.clock.advance(121)
    async with database.transaction() as writer:
        expired = await recover_expired_lease(
            await case.context(writer), granted.attempt_id
        )
    recovered = await state(database)
    current = replace(
        case, query=case.query.model_copy(update={"claim_id": "after-retirement"})
    )
    recovery_claim = await current.claim(database)
    after_reclaim = await state(database)

    assert receipt.scope is EvidenceScope.NO_RUNNER_STARTED
    assert after == replay == refused
    assert after["attempt"][0]["execution_state"] == "stopped"
    assert after["attempt"][0]["status"] == "claimed"
    assert after["attempt"][0]["current_dispatcher_open"] is False
    assert conflict.value.details.item == "runtime"
    assert conflict.value.details.reason == "evidence_conflict"
    assert len([row for row in after["event"] if row["kind"] == "attempt.retired"]) == 1
    assert expired
    assert recovered["attempt"][0]["execution_state"] == "stopped"
    assert recovered["attempt"][0]["recovery_required"] is True
    assert recovery_claim.delivery is not None
    assert after_reclaim["attempt"][0]["execution_state"] == "stopped"
