import asyncio
import threading

import pytest

from kora.api.graph_deployment import DeploymentRefusal, PreparedDeployment
from kora.api.graph_deployment_worker import THREAD_NAME, DeploymentPreparer
from kora.cel_eval.binding import load_binding
from kora.cel_eval.errors import EvaluatorUnavailable
from kora.cel_eval.thread import EvaluationThread, Evaluator, Settings
from kora.ir.json import JsonObject
from tests.api.deployment_cases import (
    DEFAULT_POLICY,
    SELECTION,
    SELECTORS,
    evaluation_clock,
    evaluator,
    prepared,
    skeleton_ir,
)
from tests.cel_eval.pattern_validation_scenarios import assert_settled
from tests.db.closing import until
from tests.plugins.authentication.support import AuthorityClock

__all__ = ["evaluator"]


def preparation_threads() -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name == THREAD_NAME]


def heavy_ir() -> JsonObject:
    ir = skeleton_ir()
    graph = ir["graph"]
    assert isinstance(graph, dict)
    nodes = graph["nodes"]
    assert isinstance(nodes, dict)
    agent = nodes["agent"]
    assert isinstance(agent, dict)
    config = agent["config"]
    assert isinstance(config, dict)
    config["prompt"] = "x" * 100_000
    return ir


@pytest.mark.asyncio
async def test_preparer_prepares_off_the_caller_loop_and_releases_its_thread(
    evaluator: Evaluator,
) -> None:
    owner = EvaluationThread(load_binding())
    expected = await prepared(evaluator)

    async with DeploymentPreparer(AuthorityClock(), owner) as preparer:
        running = preparation_threads()
        outcome = await preparer.prepare(
            skeleton_ir(), SELECTORS, SELECTION, DEFAULT_POLICY
        )
    with pytest.raises(RuntimeError):
        await preparer.prepare(skeleton_ir(), SELECTORS, SELECTION, DEFAULT_POLICY)

    assert len(running) == 1
    assert running[0] is not threading.current_thread()
    assert isinstance(outcome, PreparedDeployment)
    assert outcome.inputs.logical.digests == expected.inputs.logical.digests
    assert preparation_threads() == []
    assert_settled(owner)


@pytest.mark.asyncio
async def test_preparer_keeps_its_slot_until_cancelled_work_ends() -> None:
    owner = EvaluationThread(load_binding())

    async with DeploymentPreparer(AuthorityClock(), owner) as preparer:
        cancelled = asyncio.create_task(
            preparer.prepare(heavy_ir(), SELECTORS, SELECTION, DEFAULT_POLICY)
        )
        await until(lambda: preparer.busy)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        held_after_cancel = preparer.busy
        refused = await preparer.prepare(
            skeleton_ir(), SELECTORS, SELECTION, DEFAULT_POLICY
        )
        async with preparer.hold():
            pass
        admitted = await preparer.prepare(
            skeleton_ir(), SELECTORS, SELECTION, DEFAULT_POLICY
        )

    assert held_after_cancel
    assert refused is DeploymentRefusal.BUSY
    assert isinstance(admitted, PreparedDeployment)
    assert preparation_threads() == []
    assert_settled(owner)


@pytest.mark.asyncio
async def test_preparer_stops_its_thread_when_opening_fails() -> None:
    owner = EvaluationThread(load_binding(), Settings(handles=1))
    occupying = Evaluator(evaluation_clock, owner)

    with pytest.raises(EvaluatorUnavailable):
        async with DeploymentPreparer(AuthorityClock(), owner):
            pass

    await occupying.close()
    assert preparation_threads() == []
    assert_settled(owner)


@pytest.mark.asyncio
async def test_reservation_keeps_its_slot_until_cancelled_work_ends() -> None:
    owner = EvaluationThread(load_binding())
    gate = threading.Event()

    async def blocked(_: Evaluator) -> str:
        await asyncio.to_thread(gate.wait)
        return "settled"

    async with DeploymentPreparer(AuthorityClock(), owner) as preparer:

        async def caller() -> str:
            async with preparer.reserve() as reservation:
                assert reservation is not None
                return await reservation.run(blocked)

        task = asyncio.create_task(caller())
        await until(lambda: preparer.busy)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        held_after_cancel = preparer.busy
        async with preparer.reserve() as second:
            refused = second is None
        gate.set()
        async with preparer.hold():
            pass
        released = not preparer.busy

    assert held_after_cancel
    assert refused
    assert released
    assert preparation_threads() == []
    assert_settled(owner)
