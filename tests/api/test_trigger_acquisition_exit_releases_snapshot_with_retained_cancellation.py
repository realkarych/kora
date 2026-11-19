import asyncio
import gc
import sys
from types import FrameType

import pytest

from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.api.run_trigger_route import manual_start_preparation
from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database
from kora.engine.start_acceptance import StartCandidate, select_manual_start
from kora.engine.start_facts import SelectedStartVersion
from tests.api.deployment_cases import HARNESS, evaluator, preparer
from tests.api.support import deployment_services
from tests.db.closing import ConnectionLedger
from tests.engine.start_cases import KEY, deploy, request_for

__all__ = ["evaluator", "preparer"]


@pytest.mark.asyncio
async def test_trigger_acquisition_exit_releases_snapshot_with_retained_cancellation(
    database: Database, evaluator: Evaluator, preparer: DeploymentPreparer
) -> None:
    case = await deploy(database, evaluator)
    request = request_for(case)
    candidate = await select_manual_start(database, case.user, request, KEY)
    assert isinstance(candidate, StartCandidate)
    selected_ids: list[int] = []
    cancelled = False
    ledger = ConnectionLedger(database)

    def observe(frame: FrameType, event: str, value: object) -> None:
        nonlocal cancelled
        if (
            event == "return"
            and frame.f_code.co_name == "_acquire"
            and frame.f_globals.get("__name__") == "kora.engine.start_acceptance"
            and isinstance(value, SelectedStartVersion)
        ):
            selected_ids.append(id(value))
        if (
            selected_ids
            and not cancelled
            and event == "call"
            and frame.f_code.co_name == "read_snapshot"
            and frame.f_globals.get("__name__") == "kora.db.connection"
        ):
            task = asyncio.current_task()
            assert task is not None
            cancelled = True
            task.cancel()

    incumbent = sys.getprofile()
    prepare = manual_start_preparation(
        deployment_services(database, preparer, HARNESS), request, KEY
    )

    sys.setprofile(observe)
    try:
        caller = asyncio.ensure_future(prepare(candidate))
        with pytest.raises(asyncio.CancelledError) as failure:
            await caller
    finally:
        sys.setprofile(incumbent)
    async with preparer.hold():
        released = not any(
            type(value) is SelectedStartVersion and id(value) in selected_ids
            for value in gc.get_objects()
        )

    assert cancelled
    assert selected_ids
    assert failure.value.__traceback__ is not None
    assert released
    assert ledger.outstanding == database.units_in_flight() == 0
