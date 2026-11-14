from datetime import timedelta
from typing import Literal

import pytest
from sqlalchemy import update

from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database
from kora.db.tables import attempt, run
from kora.ir.cost import UsageCoverage
from kora.protocols.evidence import (
    ClosureEvidence,
    EvidenceScope,
    NormalCompletionQuiescence,
    TerminationReceipt,
)
from kora.protocols.runtime import Liveness, RuntimeStatus
from tests.api.deployment_cases import evaluator
from tests.db.seed import START
from tests.engine.claim_support import claim_case, state
from tests.engine.heartbeat_support import heartbeat, heartbeat_body
from tests.plugins.authentication.support import AuthorityClock
from tests.protocols.runtime_documents import liveness

__all__ = ["evaluator"]


def counters(supervisor: bool = True, candidate: bool = True) -> Liveness:
    return Liveness.model_validate(
        liveness(
            supervisor_alive=supervisor,
            candidate_alive=candidate,
            observed_at=START,
            usd=None,
            known_usd="0.01",
            pricing_status="partially_priced",
            usage_coverage=UsageCoverage(
                version=1,
                observation_id="observation",
                observation_sequence=1,
                completeness="partial",
                observed_at=START,
                attribution_gap=False,
                observation_gap=False,
            ),
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runtime_state", "exit_code", "supervisor", "candidate", "expected", "barrier"),
    [
        ("queued", None, True, True, "live", False),
        ("starting", None, True, False, "unknown", False),
        ("running", None, False, True, "unknown", False),
        ("running", None, True, True, "live", False),
        ("exited", 0, True, True, "unknown", False),
        ("exited", 1, True, True, "unknown", True),
        ("lost", None, True, True, "unknown", True),
    ],
)
async def test_observation_and_liveness_preserve_execution_uncertainty(
    database: Database,
    evaluator: Evaluator,
    runtime_state: Literal["queued", "starting", "running", "exited", "lost"],
    exit_code: int | None,
    supervisor: bool,
    candidate: bool,
    expected: str,
    barrier: bool,
) -> None:
    case = await claim_case(database, evaluator)
    decision = await case.claim(database)
    assert decision.delivery is not None
    granted = decision.delivery.system
    observation = RuntimeStatus(
        state=runtime_state, exit_code=exit_code, phase="preparation", observed_at=START
    )
    request = heartbeat_body(granted).model_copy(
        update={"observation": observation, "counters": counters(supervisor, candidate)}
    )

    await heartbeat(database, case, AuthorityClock(), request)
    saved = await state(database)

    assert saved["attempt"][0]["execution_state"] == expected
    assert saved["attempt"][0]["recovery_required"] is barrier
    assert request.counters is not None
    assert saved["attempt"][0]["counters"] == request.counters.model_dump(mode="json")
    assert saved["attempt"][0]["acknowledged_segment_offset"] == 0


@pytest.mark.asyncio
async def test_unchanged_observation_keeps_recovery_version(
    database: Database, evaluator: Evaluator
) -> None:
    case = await claim_case(database, evaluator)
    decision = await case.claim(database)
    assert decision.delivery is not None
    granted = decision.delivery.system
    clock = AuthorityClock()
    observation = RuntimeStatus(
        state="running", exit_code=None, phase="execution", observed_at=START
    )
    body = heartbeat_body(granted).model_copy(update={"observation": observation})
    await heartbeat(database, case, clock, body)
    before = await state(database)
    clock.moment += timedelta(seconds=1)
    refreshed = body.model_copy(
        update={
            "observation": observation.model_copy(update={"observed_at": clock.now()})
        }
    )

    await heartbeat(database, case, clock, refreshed)
    after = await state(database)

    assert after["event"] == before["event"]
    assert (
        after["attempt"][0]["recovery_version"]
        == before["attempt"][0]["recovery_version"]
    )
    assert after["attempt"][0]["recovery"] != before["attempt"][0]["recovery"]


@pytest.mark.asyncio
async def test_only_a_new_live_observation_can_restore_live(
    database: Database, evaluator: Evaluator
) -> None:
    case = await claim_case(database, evaluator)
    decision = await case.claim(database)
    assert decision.delivery is not None
    granted = decision.delivery.system
    body = heartbeat_body(granted)
    clock = AuthorityClock()
    live = RuntimeStatus(
        state="running", exit_code=None, phase="execution", observed_at=START
    )
    await heartbeat(
        database, case, clock, body.model_copy(update={"observation": live})
    )
    await heartbeat(
        database,
        case,
        clock,
        body.model_copy(update={"counters": counters(candidate=False)}),
    )
    async with database.transaction() as writer:
        await writer.execute(
            update(attempt)
            .where(attempt.c.id == case.binding.attempt_id)
            .values(recovery_required=True)
        )

    await heartbeat(
        database, case, clock, body.model_copy(update={"counters": counters()})
    )
    from_counters = await state(database)
    await heartbeat(database, case, clock, body)
    omitted = await state(database)
    await heartbeat(
        database, case, clock, body.model_copy(update={"observation": live})
    )
    from_observation = await state(database)

    assert from_counters["attempt"][0]["execution_state"] == "unknown"
    assert omitted["attempt"][0]["execution_state"] == "unknown"
    assert from_observation["attempt"][0]["execution_state"] == "live"
    assert from_observation["attempt"][0]["recovery_required"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("termination_first", [False, True])
async def test_two_runtime_records_remain_immutable_and_stopped(
    database: Database, evaluator: Evaluator, termination_first: bool
) -> None:
    case = await claim_case(database, evaluator)
    decision = await case.claim(database)
    assert decision.delivery is not None
    granted = decision.delivery.system
    normal = NormalCompletionQuiescence(
        kind="normal_completion",
        attempt_id=granted.attempt_id,
        execution_domain_id=granted.execution_domain_id,
        record_ref="completed",
        observed_at=START,
        scope=EvidenceScope.ENV_SWEEP,
    )
    termination = TerminationReceipt(
        kind="termination",
        attempt_id=granted.attempt_id,
        execution_domain_id=granted.execution_domain_id,
        retirement_id="retired",
        stopped_at=START,
        evidence_ref="retirement",
        scope=EvidenceScope.ENV_SWEEP,
    )
    records = (termination, normal) if termination_first else (normal, termination)
    body = heartbeat_body(granted)
    clock = AuthorityClock()

    for record in records:
        await heartbeat(
            database,
            case,
            clock,
            body.model_copy(
                update={
                    "evidence": ClosureEvidence(
                        runtime=record, dispatchers=(), publication=None
                    )
                }
            ),
        )
    before = await state(database)
    await heartbeat(
        database,
        case,
        clock,
        body.model_copy(
            update={
                "observation": RuntimeStatus(
                    state="running",
                    exit_code=None,
                    phase="execution",
                    observed_at=START,
                )
            }
        ),
    )
    after = await state(database)

    recovery = after["attempt"][0]["recovery"]
    assert isinstance(recovery, dict)
    assert recovery["worker"]["normal_completion"] == normal.model_dump(mode="json")
    assert recovery["worker"]["termination"] == termination.model_dump(mode="json")
    assert (
        before["attempt"][0]["execution_state"]
        == after["attempt"][0]["execution_state"]
        == "stopped"
    )
    assert (
        len([item for item in after["event"] if item["kind"] == "attempt.retired"]) == 2
    )


@pytest.mark.asyncio
async def test_counter_only_heartbeat_preserves_source_age_without_events_or_marker(
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
            .values(needs_scheduling=False)
        )
    before = await state(database)
    observed = counters()
    body = heartbeat_body(granted).model_copy(update={"counters": observed})

    await heartbeat(database, case, AuthorityClock(START + timedelta(seconds=60)), body)
    after = await state(database)

    assert after["event"] == before["event"]
    assert after["run"][0]["needs_scheduling"] is False
    assert after["attempt"][0]["counters"] == observed.model_dump(mode="json")
    assert after["attempt"][0]["usage_coverage"] == observed.usage_coverage.model_dump(
        mode="json"
    )
