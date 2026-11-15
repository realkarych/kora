import asyncio
import gc
import sys
import threading
from types import FrameType

import pytest
from sqlalchemy import select, update

from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.api.run_trigger_route import manual_start_preparation
from kora.cel_eval.binding import load_binding
from kora.cel_eval.thread import EvaluationThread, Evaluator
from kora.db.connection import Database
from kora.db.tables import graph_version
from kora.engine.start_acceptance import StartCandidate, select_manual_start
from kora.engine.start_facts import SelectedStartVersion, read_manual_start_facts
from tests.api.deployment_cases import HARNESS, evaluator, preparer
from tests.api.support import deployment_services
from tests.api.test_graph_deployment_worker import preparation_threads
from tests.cel_eval.pattern_validation_scenarios import assert_settled
from tests.db.closing import ConnectionLedger, until
from tests.engine.start_cases import KEY, deploy, request_for
from tests.plugins.authentication.support import AuthorityClock
from tests.support.output_bindings import publication_binding
from tests.support.parameter_bindings import parameter_binding

__all__ = ["evaluator", "preparer"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_trigger_preparation_releases_snapshot_with_retained_failure(
    database: Database, evaluator: Evaluator, cancelled: bool
) -> None:
    case = await deploy(database, evaluator)
    request = request_for(case)
    candidate = await select_manual_start(database, case.user, request, KEY)
    assert isinstance(candidate, StartCandidate)
    if not cancelled:
        async with database.transaction() as writer:
            stored = await writer.scalar(
                select(graph_version.c.ir).where(graph_version.c.id == case.version_id)
            )
            assert isinstance(stored, str)
            await writer.execute(
                update(graph_version)
                .where(graph_version.c.id == case.version_id)
                .values(ir=stored.replace("repository", "repositorx"))
            )
    owner = EvaluationThread(load_binding())
    entered, release = threading.Event(), threading.Event()
    selected_ids: list[int] = []

    def observe(frame: FrameType, event: str, _: object) -> None:
        if event == "call" and frame.f_code is read_manual_start_facts.__code__:
            selected_ids.append(id(frame.f_locals["selected"]))
            entered.set()
            assert release.wait(5)

    incumbent = threading.getprofile()
    threading.setprofile(observe)

    try:
        async with DeploymentPreparer(
            AuthorityClock(),
            owner,
            parameter_binding=parameter_binding(),
            binding=publication_binding(),
        ) as preparer:
            prepare = manual_start_preparation(
                deployment_services(database, preparer, HARNESS), request, KEY
            )
            caller = asyncio.ensure_future(prepare(candidate))
            try:
                await until(entered.is_set)
                if cancelled:
                    caller.cancel()
                else:
                    release.set()
                expected = asyncio.CancelledError if cancelled else RuntimeError
                with pytest.raises(expected) as failure:
                    await caller
                held_on_cancellation = not cancelled or preparer.busy
            finally:
                release.set()
            async with preparer.hold():
                released = not any(
                    type(value) is SelectedStartVersion and id(value) in selected_ids
                    for value in gc.get_objects()
                )
    finally:
        release.set()
        threading.setprofile(incumbent)

    assert selected_ids
    assert failure.value.__traceback__ is not None
    assert held_on_cancellation
    assert released
    assert preparation_threads() == []
    assert_settled(owner)


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
