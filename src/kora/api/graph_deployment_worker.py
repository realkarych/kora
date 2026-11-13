import asyncio
import threading
from collections.abc import AsyncGenerator, Callable, Coroutine
from concurrent.futures import Future
from contextlib import asynccontextmanager
from types import TracebackType
from typing import Self, TypeVar

from kora.api.graph_deployment import (
    DeploymentPreparation,
    DeploymentRefusal,
    prepare_graph_deployment,
)
from kora.cel_eval.thread import EvaluationThread, Evaluator
from kora.engine.graph_command_manifests import GraphDeploymentSelectors
from kora.ir.deployment_checks import DeploymentSelection
from kora.ir.json import JsonObject
from kora.protocols.time import Clock

THREAD_NAME = "kora-deploy-preparation"
T = TypeVar("T")


class PreparationThread:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._serve, name=THREAD_NAME)

    def _serve(self) -> None:
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_forever()
        finally:
            self.loop.close()

    def start(self) -> None:
        self.thread.start()

    async def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        await asyncio.to_thread(self.thread.join)


class PreparationReservation:
    def __init__(
        self,
        worker: PreparationThread,
        evaluator: Evaluator,
        slot: asyncio.Semaphore,
    ) -> None:
        self._worker = worker
        self._evaluator = evaluator
        self._slot = slot
        self._caller = asyncio.get_running_loop()
        self._running = 0
        self._closed = False

    def _settled(self) -> None:
        self._running -= 1
        if self._closed and self._running == 0:
            self._slot.release()

    def close(self) -> None:
        self._closed = True
        if self._running == 0:
            self._slot.release()

    async def run(self, work: Callable[[Evaluator], Coroutine[None, None, T]]) -> T:
        if self._closed:
            raise RuntimeError("The preparation reservation is closed.")
        worker, evaluator = self._worker, self._evaluator
        result: Future[T] = Future()

        def finished(task: asyncio.Task[T]) -> None:
            self._caller.call_soon_threadsafe(self._settled)
            if task.cancelled():
                result.cancel()
                return
            error = task.exception()
            if error is not None:
                result.set_exception(error)
            else:
                result.set_result(task.result())

        def start() -> None:
            task = worker.loop.create_task(work(evaluator))
            task.add_done_callback(finished)

        self._running += 1
        worker.loop.call_soon_threadsafe(start)
        return await asyncio.shield(asyncio.wrap_future(result))


class DeploymentPreparer:
    def __init__(self, clock: Clock, owner: EvaluationThread | None = None) -> None:
        self._clock = clock
        self._owner = owner
        self._slot = asyncio.Semaphore(1)
        self._worker: PreparationThread | None = None
        self._evaluator: Evaluator | None = None

    @property
    def busy(self) -> bool:
        return self._slot.locked()

    async def _open(self) -> Evaluator:
        return Evaluator(self._clock.now, self._owner)

    async def __aenter__(self) -> Self:
        worker = PreparationThread()
        worker.start()
        opened = False
        try:
            self._evaluator = await asyncio.wrap_future(
                asyncio.run_coroutine_threadsafe(self._open(), worker.loop)
            )
            opened = True
        finally:
            if not opened:
                await worker.stop()
        self._worker = worker
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        worker, self._worker = self._worker, None
        evaluator, self._evaluator = self._evaluator, None
        if worker is None:
            return
        try:
            async with self.hold():
                if evaluator is not None:
                    await asyncio.wrap_future(
                        asyncio.run_coroutine_threadsafe(evaluator.close(), worker.loop)
                    )
        finally:
            await worker.stop()

    @asynccontextmanager
    async def hold(self) -> AsyncGenerator[None]:
        async with self._slot:
            yield

    @asynccontextmanager
    async def reserve(self) -> AsyncGenerator[PreparationReservation | None]:
        worker, evaluator = self._worker, self._evaluator
        if worker is None or evaluator is None:
            raise RuntimeError("The deployment preparer is not open.")
        if self._slot.locked():
            yield None
            return
        await self._slot.acquire()
        reservation = PreparationReservation(worker, evaluator, self._slot)
        try:
            yield reservation
        finally:
            reservation.close()

    async def prepare(
        self,
        ir: JsonObject,
        selectors: GraphDeploymentSelectors,
        selection: DeploymentSelection,
    ) -> DeploymentPreparation:
        async with self.reserve() as reservation:
            if reservation is None:
                return DeploymentRefusal.BUSY
            return await reservation.run(
                lambda evaluator: prepare_graph_deployment(
                    ir, selectors, selection, evaluator
                )
            )
