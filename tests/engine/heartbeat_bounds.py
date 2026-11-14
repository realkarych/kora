import json
from datetime import UTC, datetime
from typing import get_args

from kora.engine.heartbeat_evidence import (
    HeartbeatRecovery,
    ObservationGrant,
    WorkerRecovery,
)
from kora.ir.cost import MAX_MONEY, MAX_SUM, PricingStatus, UsageCoverage
from kora.protocols.boundary import BoundaryModel
from kora.protocols.evidence import (
    DispatcherClosure,
    DispatcherQuiescence,
    EvidenceScope,
    NormalCompletionQuiescence,
    TerminationReceipt,
)
from kora.protocols.identity import ProcessIdentity
from kora.protocols.identity_records import MAX_COUNT, MAX_PID
from kora.protocols.runtime import MIN_EXIT_CODE, Liveness, RuntimePhase, RuntimeStatus

MAX_TEXT: str = '"' * 256
MAX_INSTANT = datetime(9999, 12, 31, 23, 59, 59, 999999, UTC)


def object_bound(
    model: type[BoundaryModel] | type[UsageCoverage], sizes: dict[str, int]
) -> int:
    assert set(sizes) == set(model.model_fields)
    return (
        2
        + sum(len(json.dumps(key)) + 2 + value for key, value in sizes.items())
        + 2 * max(0, len(sizes) - 1)
    )


def producer_bounds() -> tuple[int, int, int, int]:
    identifier = 2 + 2 * 256
    scope = max(len(json.dumps(item.value)) for item in EvidenceScope)
    process = object_bound(
        ProcessIdentity,
        dict(
            host_id=identifier,
            boot_id=identifier,
            pid_namespace=identifier,
            pid=10,
            start_time=19,
            leads_group=5,
        ),
    )
    quiescence = object_bound(
        DispatcherQuiescence,
        dict(
            attempt_id=identifier,
            execution_domain_id=identifier,
            dispatcher_id=identifier,
            worker_incarnation_id=identifier,
            lease_generation=19,
            closed_at=29,
            evidence_ref=identifier,
        ),
    )
    closure = object_bound(
        DispatcherClosure,
        dict(
            quiescence=quiescence,
            provenance=len(json.dumps("runtime_observed_process_exit")),
            process=process,
        ),
    )
    observation = object_bound(
        RuntimeStatus,
        dict(
            state=10,
            exit_code=11,
            phase=max(len(json.dumps(item)) for item in get_args(RuntimePhase)),
            observed_at=29,
        ),
    )
    grant = object_bound(
        ObservationGrant,
        dict(
            worker_id=identifier,
            incarnation_id=identifier,
            dispatcher_id=identifier,
            lease_epoch=19,
        ),
    )
    normal = object_bound(
        NormalCompletionQuiescence,
        dict(
            kind=len(json.dumps("normal_completion")),
            attempt_id=identifier,
            execution_domain_id=identifier,
            record_ref=identifier,
            observed_at=29,
            scope=scope,
        ),
    )
    termination = object_bound(
        TerminationReceipt,
        dict(
            kind=len(json.dumps("termination")),
            attempt_id=identifier,
            execution_domain_id=identifier,
            retirement_id=identifier,
            stopped_at=29,
            evidence_ref=identifier,
            scope=scope,
        ),
    )
    worker = object_bound(
        WorkerRecovery,
        dict(
            version=1,
            last_observation=observation,
            observation_grant=grant,
            normal_completion=normal,
            termination=termination,
        ),
    )
    recovery = object_bound(HeartbeatRecovery, dict(reason=identifier, worker=worker))
    coverage = object_bound(
        UsageCoverage,
        dict(
            version=1,
            observation_id=2 + 12 * 256,
            observation_sequence=16,
            completeness=13,
            observed_at=29,
            attribution_gap=5,
            observation_gap=5,
        ),
    )
    liveness = object_bound(
        Liveness,
        dict(
            observed_at=29,
            supervisor_alive=5,
            candidate_alive=5,
            journal_next=19,
            last_event_at=29,
            limited_events=16,
            events=16,
            input_tokens=16,
            output_tokens=16,
            usd=27,
            known_usd=27,
            pricing_status=max(
                len(json.dumps(item)) for item in get_args(PricingStatus)
            ),
            usage_coverage=coverage,
            last_event_kind=identifier,
        ),
    )
    return closure, recovery, liveness, coverage


def maximal_counters() -> Liveness:
    return Liveness(
        observed_at=MAX_INSTANT,
        supervisor_alive=False,
        candidate_alive=False,
        journal_next=MAX_COUNT,
        last_event_at=MAX_INSTANT,
        limited_events=MAX_SUM,
        events=MAX_SUM,
        input_tokens=MAX_SUM,
        output_tokens=MAX_SUM,
        usd=str(MAX_MONEY),
        known_usd=str(MAX_MONEY),
        pricing_status="estimated",
        last_event_kind=MAX_TEXT,
        usage_coverage=UsageCoverage(
            version=1,
            observation_id="𐀀" * 256,
            observation_sequence=MAX_SUM,
            completeness="partial",
            observed_at=MAX_INSTANT,
            attribution_gap=True,
            observation_gap=True,
        ),
    )


def maximal_recovery() -> HeartbeatRecovery:
    return HeartbeatRecovery(
        reason=MAX_TEXT,
        worker=WorkerRecovery(
            last_observation=RuntimeStatus(
                state="exited",
                exit_code=MIN_EXIT_CODE,
                phase="awaiting_authorization",
                observed_at=MAX_INSTANT,
            ),
            observation_grant=ObservationGrant(
                worker_id=MAX_TEXT,
                incarnation_id=MAX_TEXT,
                dispatcher_id=MAX_TEXT,
                lease_epoch=MAX_COUNT,
            ),
            normal_completion=NormalCompletionQuiescence(
                kind="normal_completion",
                attempt_id=MAX_TEXT,
                execution_domain_id=MAX_TEXT,
                record_ref=MAX_TEXT,
                observed_at=MAX_INSTANT,
                scope=EvidenceScope.SUBREAPER_LEADER_GROUP_ENV_SWEEP,
            ),
            termination=TerminationReceipt(
                kind="termination",
                attempt_id=MAX_TEXT,
                execution_domain_id=MAX_TEXT,
                retirement_id=MAX_TEXT,
                stopped_at=MAX_INSTANT,
                evidence_ref=MAX_TEXT,
                scope=EvidenceScope.SUBREAPER_LEADER_GROUP_ENV_SWEEP,
            ),
        ),
    )


def maximal_closure() -> DispatcherClosure:
    return DispatcherClosure(
        quiescence=DispatcherQuiescence(
            attempt_id=MAX_TEXT,
            execution_domain_id=MAX_TEXT,
            dispatcher_id=MAX_TEXT,
            worker_incarnation_id=MAX_TEXT,
            lease_generation=MAX_COUNT,
            closed_at=MAX_INSTANT,
            evidence_ref=MAX_TEXT,
        ),
        provenance="runtime_observed_process_exit",
        process=ProcessIdentity(
            host_id=MAX_TEXT,
            boot_id=MAX_TEXT,
            pid_namespace=MAX_TEXT,
            pid=MAX_PID,
            start_time=MAX_COUNT,
            leads_group=True,
        ),
    )
