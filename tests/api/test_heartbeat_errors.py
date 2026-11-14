import json

import pytest

from kora.api.exchange import ApiExchange
from kora.client import KoraClient
from kora.client.errors import ApiError
from kora.db.connection import Database
from kora.protocols.evidence import (
    ClosureEvidence,
    EvidenceScope,
    NormalCompletionQuiescence,
)
from kora.protocols.worker_heartbeats import PublicationExpectation, WorkerHeartbeat
from tests.api.claim_support import BEARER, request
from tests.api.deployment_cases import evaluator
from tests.api.heartbeat_support import heartbeat_api
from tests.client.support import running_app
from tests.engine.claim_support import ClaimCase, state
from tests.engine.heartbeat_support import self_closure
from tests.engine.test_heartbeat_observations import counters
from tests.plugins.authentication.support import AuthorityClock

__all__ = ["evaluator", "heartbeat_api"]


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "asgi"])
@pytest.mark.parametrize(
    ("problem", "code", "reason"),
    [
        ("runtime_unbound", "API003", "evidence_unbound"),
        ("runtime_conflict", "API002", "evidence_conflict"),
        ("dispatcher_unbound", "API003", "evidence_unbound"),
        ("dispatcher_conflict", "API002", "evidence_conflict"),
        ("usage_conflict", "API002", "usage_conflict"),
        ("usage_regression", "API002", "usage_regression"),
        ("publication", "API010", None),
    ],
)
async def test_heartbeat_reports_exact_errors_without_partial_renewal(
    database: Database,
    heartbeat_api: tuple[ClaimCase, AuthorityClock, ApiExchange],
    transport: str,
    problem: str,
    code: str,
    reason: str | None,
) -> None:
    case, clock, app = heartbeat_api
    async with (
        running_app(app) as origin,
        KoraClient(
            "http://127.0.0.1" if transport == "asgi" else origin,
            BEARER,
            asgi_app=app if transport == "asgi" else None,
        ) as client,
    ):
        granted = (await client.claim_worker(case.query.worker_id, request(case))).claim
        assert granted is not None
        normal = NormalCompletionQuiescence(
            kind="normal_completion",
            attempt_id=granted.attempt_id,
            execution_domain_id=granted.execution_domain_id,
            record_ref="private-runtime-evidence",
            observed_at=clock.now(),
            scope=EvidenceScope.ENV_SWEEP,
        )
        closure = self_closure(granted)
        initial = WorkerHeartbeat(
            dispatcher_id=granted.current_grant.dispatcher_id,
            lease_epoch=granted.lease_epoch,
            counters=counters(),
            evidence=ClosureEvidence(
                runtime=normal, dispatchers=(closure,), publication=None
            ),
        )
        await client.heartbeat_attempt(granted.attempt_id, initial)
        details: dict[str, object] = {}
        changed: dict[str, object] = {}
        if problem.startswith("runtime"):
            item = normal.model_copy(
                update={
                    "attempt_id"
                    if problem.endswith("unbound")
                    else "record_ref": "private-conflict-evidence"
                }
            )
            changed["evidence"] = ClosureEvidence(
                runtime=item, dispatchers=(), publication=None
            )
            details = {"reason": reason, "item": "runtime"}
        elif problem.startswith("dispatcher"):
            item = closure.model_copy(
                update={
                    "quiescence": closure.quiescence.model_copy(
                        update={
                            "attempt_id"
                            if problem.endswith("unbound")
                            else "evidence_ref": "private-conflict-evidence"
                        }
                    )
                }
            )
            changed["evidence"] = ClosureEvidence(
                runtime=None, dispatchers=(item,), publication=None
            )
            details = {
                "reason": reason,
                "item": "dispatchers",
                "index": 0,
                "dispatcher_id": closure.quiescence.dispatcher_id,
                "lease_epoch": closure.quiescence.lease_generation,
            }
        elif problem.startswith("usage"):
            observation = counters()
            coverage = observation.usage_coverage.model_copy(
                update={"observation_sequence": 0}
                if problem.endswith("regression")
                else {"observation_gap": True}
            )
            changed["counters"] = observation.model_copy(
                update={"usage_coverage": coverage}
            )
            details = {"reason": reason}
        else:
            changed["publication"] = PublicationExpectation(
                binding="local",
                binding_revision=1,
                credential_mode="local_credential_free",
                operation_id="private-operation",
                prepared_digest="sha256:" + "a" * 64,
                destination_digest="sha256:" + "b" * 64,
            )
        body = initial.model_copy(update=changed)
        before = await state(database)

        with pytest.raises(ApiError) as caught:
            await client.heartbeat_attempt(granted.attempt_id, body)
        after = await state(database)

    envelope = json.loads(caught.value.content)
    assert envelope["error"]["code"] == code
    assert envelope["error"]["details"] == details
    assert envelope["error"]["retryable"] is False
    assert b"private-" not in caught.value.content
    assert before == after
