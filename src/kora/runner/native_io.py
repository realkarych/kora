import asyncio
import threading
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TypeVar

Result = TypeVar("Result")


class NativeIOBusy(Exception):
    pass


class NativeIOClosed(Exception):
    pass


class NativeIOUnresolved(Exception):
    pass


class NativeIONotInvoked(NativeIOUnresolved):
    pass


class NativeIOSubmissionUncertain(NativeIOUnresolved):
    pass


@dataclass
class BorrowedInvocation:
    lock: threading.Lock = field(default_factory=threading.Lock)
    worker: threading.Thread | None = None
    sealed: bool = False

    def enter(self) -> None:
        with self.lock:
            if self.sealed:
                raise NativeIONotInvoked
            self.worker = threading.current_thread()

    def seal(self) -> threading.Thread | None:
        with self.lock:
            self.sealed = True
            return self.worker


@dataclass(eq=False)
class NativeOperation:
    settled: asyncio.Event = field(default_factory=asyncio.Event)
    retained: object | None = None
    borrower: BorrowedInvocation | None = None
    deferred_cleanup: Callable[[], None] | None = None
    cleanup_attempted: bool = False

    def invoke(self, function: Callable[[], Result]) -> Result:
        if self.borrower is not None:
            self.borrower.enter()
        result = function()
        self.retained = result
        return result

    def join_borrower(self) -> None:
        if self.borrower is not None:
            worker = self.borrower.seal()
            if worker is not None:
                worker.join()

    def release_deferred(self) -> None:
        cleanup = self.deferred_cleanup
        borrower = self.borrower
        if cleanup is None or borrower is None or self.cleanup_attempted:
            return
        self.join_borrower()
        self.cleanup_attempted = True
        cleanup()
        self.deferred_cleanup = None


async def observed(observation: asyncio.Future[Result]) -> bool:
    cancelled = False
    while not observation.done():
        try:
            await asyncio.wait({observation})
        except asyncio.CancelledError:
            cancelled = True
    if not observation.cancelled() and observation.done():
        observation.exception()
    return cancelled


async def completed(future: Future[Result]) -> bool:
    return await observed(asyncio.wrap_future(future))


class NativePool(ThreadPoolExecutor):
    def __init__(self, workers: int, on_shutdown: Callable[[], None]) -> None:
        super().__init__(max_workers=workers, thread_name_prefix="kora-runtime-local")
        self.on_shutdown = on_shutdown

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        self.on_shutdown()
        super().shutdown(wait, cancel_futures=cancel_futures)


class NativeExecutor:
    def __init__(self, workers: int, admitted: int) -> None:
        self.executor = NativePool(workers, self.pool_shutdown)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.available = workers
        self.waiting: deque[asyncio.Future[None]] = deque()
        self.admitted = admitted
        self.jobs: set[NativeOperation] = set()
        self.closed = False
        self.unresolved = False
        self._joined = False
        self.closing: asyncio.Task[None] | None = None

    @property
    def joined(self) -> bool:
        return self._joined

    async def run(
        self,
        function: Callable[[], Result],
        on_cancel: Callable[[Result], None] | None = None,
        *,
        after_uncertain_submission: Callable[[], None] | None = None,
    ) -> Result:
        return await self.execute(
            function, on_cancel, after_uncertain_submission, wait=True
        )

    async def run_nowait(
        self,
        function: Callable[[], Result],
        on_cancel: Callable[[Result], None] | None = None,
        *,
        after_uncertain_submission: Callable[[], None] | None = None,
    ) -> Result:
        return await self.execute(
            function, on_cancel, after_uncertain_submission, wait=False
        )

    def pool_shutdown(self) -> None:
        if self.loop is not None and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.pool_unavailable)

    def pool_unavailable(self) -> None:
        if any(not job.settled.is_set() for job in self.jobs):
            self.unresolved = True
            self.wake_waiters()

    def wake_waiters(self) -> None:
        while self.waiting and (self.available or self.unresolved):
            ready = self.waiting.popleft()
            if not self.unresolved:
                self.available -= 1
            ready.set_result(None)

    async def execute(
        self,
        function: Callable[[], Result],
        on_cancel: Callable[[Result], None] | None,
        after_uncertain_submission: Callable[[], None] | None,
        *,
        wait: bool,
    ) -> Result:
        if self.closed:
            raise NativeIOClosed
        if self.unresolved:
            raise NativeIONotInvoked
        if len(self.jobs) >= self.admitted:
            raise NativeIOBusy
        if not wait and (not self.available or self.waiting):
            raise NativeIOBusy
        self.loop = asyncio.get_running_loop()
        owned = NativeOperation(
            borrower=BorrowedInvocation()
            if after_uncertain_submission is not None
            else None
        )
        self.jobs.add(owned)
        unresolved = True
        acquired = False
        cancelled = False

        try:
            if self.available and not self.waiting:
                self.available -= 1
            else:
                ready = self.loop.create_future()
                self.waiting.append(ready)
                cancelled = await observed(ready)
                if self.unresolved:
                    raise NativeIONotInvoked
            acquired = True
            try:
                future = self.executor.submit(owned.invoke, function)
            except (RuntimeError, MemoryError):
                owned.deferred_cleanup = after_uncertain_submission
                if owned.borrower is not None:
                    owned.borrower.seal()
                raise NativeIOSubmissionUncertain from None
            unresolved = False
            cancelled = await completed(future) or cancelled
            if future.cancelled():
                unresolved = True
                raise NativeIONotInvoked
            try:
                result = future.result()
            except Exception:
                if cancelled:
                    raise asyncio.CancelledError from None
                raise
            if cancelled and on_cancel is not None:
                unresolved = True
                cleanup = self.executor.submit(on_cancel, result)
                await completed(cleanup)
                cleanup.result()
                unresolved = False
            if cancelled:
                raise asyncio.CancelledError
            return result
        finally:
            if unresolved:
                self.unresolved = True
            else:
                owned.retained = None
                self.jobs.discard(owned)
            owned.settled.set()
            if acquired:
                self.available += 1
            self.wake_waiters()

    def join_workers(self) -> None:
        self.executor.shutdown(wait=True)
        for job in self.jobs:
            job.join_borrower()
        self._joined = True
        for job in self.jobs:
            job.release_deferred()

    async def drain(self) -> None:
        for job in tuple(self.jobs):
            while not job.settled.is_set():
                try:
                    await job.settled.wait()
                except asyncio.CancelledError:
                    continue
        joining = asyncio.get_running_loop().run_in_executor(None, self.join_workers)
        while not joining.done():
            try:
                await asyncio.wait({joining})
            except asyncio.CancelledError:
                continue
        failure = joining.exception()
        if failure is not None and not any(job.cleanup_attempted for job in self.jobs):
            raise failure
        if self.unresolved:
            raise NativeIOUnresolved

    async def close(self) -> None:
        self.closed = True
        cancelled = False
        while True:
            if self.closing is None or self.closing.cancelled():
                self.closing = asyncio.create_task(self.drain())
            if self.closing.done():
                self.closing.result()
                break
            try:
                await asyncio.shield(self.closing)
            except asyncio.CancelledError:
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError
