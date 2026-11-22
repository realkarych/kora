import asyncio
from dataclasses import replace

import pytest
from sqlalchemy import select

from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.conformance.capture import Capture
from kora.db.connection import Database, Dialect
from kora.db.tables import attempt, attempt_dispatcher, run
from kora.engine.attempt_admission import AttemptAdmissionState
from kora.engine.run_promotion import PromotionOutcome
from kora.models.runs import AcceptedRun
from tests.api.deployment_cases import preparer
from tests.api.scheduler_claim_support import node_id, scheduler_claim_setup
from tests.api.support import api_capture, client_for
from tests.engine.test_http_trigger_admission import KEY, RUNS
from tests.engine.test_pending_promotion import deploy, pending_promotion, trigger

__all__ = ["api_capture", "preparer"]


@pytest.mark.asyncio
@pytest.mark.parametrize("overwidth", [False, True])
async def test_scheduler_preserves_accepted_run_at_worker_parent_bound(
    database: Database,
    preparer: DeploymentPreparer,
    api_capture: Capture,
    overwidth: bool,
) -> None:
    setup = await scheduler_claim_setup(database, preparer, api_capture)
    headers = {"Authorization": str(setup.user.request.authorization)}
    author_timeout = "0" * (4096 if overwidth else 2048) + "1h"
    configuration = setup.processor.limits
    preparation = configuration.preparation
    source = preparation.snapshot
    bounded = replace(source, inputs=replace(source.inputs, parent=4096))
    admission = replace(
        setup.processor,
        limits=replace(
            configuration, preparation=replace(preparation, snapshot=bounded)
        ),
        initial_root_only=True,
    )
    promotion = pending_promotion(database)

    async with client_for(setup.app) as client:
        await deploy(client, headers, author_timeout)
        accepted = await trigger(client, headers)
        run_id = accepted.run_id.removeprefix("run_")
        deadline = asyncio.get_running_loop().time() + 10.0
        promoted = await promotion.process(
            run_id, deadline=deadline, stopped=lambda: False
        )
        root = await node_id(database, run_id, "agent")
        admitted = await admission.process(
            root, periodic_recheck=True, deadline=deadline
        )
        replay = await client.post(
            RUNS, json={"params": {}}, headers=headers | {"Idempotency-Key": KEY}
        )
    async with database.read_transaction() as reader:
        with await reader.execute(select(run.c.id)) as result:
            runs = list(result.scalars())
        with await reader.execute(select(attempt.c.id)) as result:
            reservations = list(result.scalars())
        grant = await reader.scalar(select(attempt_dispatcher.c.id))

    refused = overwidth and database.dialect is Dialect.SQLITE
    assert promoted is PromotionOutcome.APPLIED
    assert admitted is (
        AttemptAdmissionState.REFUSED if refused else AttemptAdmissionState.QUEUED
    )
    assert len(reservations) == (0 if refused else 1)
    assert runs == [run_id]
    assert grant is None
    assert replay.status_code == 200
    assert AcceptedRun.model_validate(replay.json()) == accepted
    assert database.units_in_flight() == 0
