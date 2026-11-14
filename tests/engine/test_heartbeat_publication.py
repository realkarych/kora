from typing import Literal

import pytest

from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database
from kora.db.grant_history import GrantHistoryRejected
from kora.ir import VcsRef
from kora.protocols.evidence import ClosureEvidence, PublicationOutcome
from kora.protocols.worker_heartbeats import PublicationExpectation
from tests.api.deployment_cases import evaluator
from tests.engine.claim_support import claim_case, state
from tests.engine.heartbeat_support import heartbeat, heartbeat_body, self_closure
from tests.plugins.authentication.support import AuthorityClock

__all__ = ["evaluator"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "publication_state",
    ["not_applicable", "not_dispatched", "unknown", "failed", "completed"],
)
async def test_every_publication_evidence_requires_its_actual_saved_owner(
    database: Database,
    evaluator: Evaluator,
    publication_state: Literal[
        "not_applicable", "not_dispatched", "unknown", "failed", "completed"
    ],
) -> None:
    case = await claim_case(database, evaluator)
    decision = await case.claim(database)
    assert decision.delivery is not None
    granted = decision.delivery.system
    reference = (
        VcsRef.model_validate(
            dict(
                repo="/srv/git/repo.git",
                branch="kora/result",
                commit="c0ffee",
                base_commit="beef",
                changed_paths=[],
                diff_stat="",
                pushed_by={
                    "provider_instance": "local",
                    "stable_id": "service",
                    "kind": "local_service",
                    "display_name": "publisher",
                },
            )
        )
        if publication_state == "completed"
        else None
    )
    publication = PublicationOutcome(
        state=publication_state,
        operation_id="publication"
        if publication_state in ("failed", "completed")
        else None,
        ref=reference,
        code="publication_failed" if publication_state == "failed" else None,
    )
    body = heartbeat_body(granted).model_copy(
        update={
            "grant_history_complete": granted.grant_history,
            "evidence": ClosureEvidence(
                runtime=None,
                dispatchers=(self_closure(granted),),
                publication=publication,
            ),
        }
    )
    before = await state(database)

    with pytest.raises(GrantHistoryRejected):
        await heartbeat(database, case, AuthorityClock(), body)
    after = await state(database)

    assert after == before


@pytest.mark.asyncio
async def test_publication_expectation_cannot_create_its_own_authorization(
    database: Database, evaluator: Evaluator
) -> None:
    case = await claim_case(database, evaluator)
    decision = await case.claim(database)
    assert decision.delivery is not None
    granted = decision.delivery.system
    expectation = PublicationExpectation(
        binding="local",
        binding_revision=1,
        credential_mode="local_credential_free",
        operation_id="publish",
        prepared_digest="sha256:" + "a" * 64,
        destination_digest="sha256:" + "b" * 64,
    )
    body = heartbeat_body(granted).model_copy(update={"publication": expectation})
    before = await state(database)

    with pytest.raises(GrantHistoryRejected):
        await heartbeat(database, case, AuthorityClock(), body)
    after = await state(database)

    assert after == before
