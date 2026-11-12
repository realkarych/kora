import asyncio
import threading

import pytest

from kora.api.graph_deployment import PreparedDeployment
from kora.api.graph_deployment_worker import THREAD_NAME, DeploymentPreparer
from kora.cel_eval.binding import load_binding
from kora.cel_eval.thread import EvaluationThread, Evaluator
from tests.api.deployment_cases import (
    SELECTION,
    SELECTORS,
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


@pytest.mark.asyncio
async def test_preparer_prepares_off_the_caller_loop_and_releases_its_thread(
    evaluator: Evaluator,
) -> None:
    owner = EvaluationThread(load_binding())
    expected = await prepared(evaluator)

    async with DeploymentPreparer(AuthorityClock(), owner) as preparer:
        running = preparation_threads()
        cancelled = asyncio.create_task(
            preparer.prepare(skeleton_ir(), SELECTORS, SELECTION)
        )
        await until(lambda: preparer.busy)
        cancelled.cancel()
        outcome = await preparer.prepare(skeleton_ir(), SELECTORS, SELECTION)
    with pytest.raises(RuntimeError):
        await preparer.prepare(skeleton_ir(), SELECTORS, SELECTION)

    assert len(running) == 1
    assert running[0] is not threading.current_thread()
    assert isinstance(outcome, PreparedDeployment)
    assert outcome.inputs.logical.digests == expected.inputs.logical.digests
    assert cancelled.cancelled()
    assert not preparer.busy
    assert preparation_threads() == []
    assert_settled(owner)
