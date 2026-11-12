import asyncio
import threading
from concurrent.futures import Future
from types import TracebackType
from typing import Self

from kora.api.graph_deployment import DeploymentPreparation, prepare_graph_deployment
from kora.cel_eval.thread import EvaluationThread, Evaluator
from kora.engine.graph_command_manifests import GraphDeploymentSelectors
from kora.ir.deployment_checks import DeploymentSelection
from kora.ir.json import JsonObject
from kora.protocols.time import Clock

THREAD_NAME = "kora-deploy-preparation"


class DeploymentPreparer:
    def __init__(self, clock: Clock, owner: EvaluationThread | None = None) -> None:
        self._clock = clock
        self._owner = owner
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._serve, name=THREAD_NAME)
        self._slot = asyncio.Semaphore(1)
        self._evaluator: Evaluator | None = None

    def _serve(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    async def _open(self) -> Evaluator:
        return Evaluator(self._clock.now, self._owner)

    async def __aenter__(self) -> Self:
        self._thread.start()
        self._evaluator = await asyncio.wrap_future(
            asyncio.run_coroutine_threadsafe(self._open(), self._loop)
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        async with self._slot:
            evaluator, self._evaluator = self._evaluator, None
            if evaluator is not None:
                await asyncio.wrap_future(
                    asyncio.run_coroutine_threadsafe(evaluator.close(), self._loop)
                )
            self._loop.call_soon_threadsafe(self._loop.stop)
            await asyncio.to_thread(self._thread.join)

    async def prepare(
        self,
        ir: JsonObject,
        selectors: GraphDeploymentSelectors,
        selection: DeploymentSelection,
    ) -> DeploymentPreparation:
        await self._slot.acquire()
        evaluator = self._evaluator
        if evaluator is None:
            self._slot.release()
            raise RuntimeError("The deployment preparer is not open.")
        caller = asyncio.get_running_loop()
        work = asyncio.run_coroutine_threadsafe(
            prepare_graph_deployment(ir, selectors, selection, evaluator), self._loop
        )

        def settled(_: Future[DeploymentPreparation]) -> None:
            caller.call_soon_threadsafe(self._slot.release)

        work.add_done_callback(settled)
        return await asyncio.wrap_future(work)
