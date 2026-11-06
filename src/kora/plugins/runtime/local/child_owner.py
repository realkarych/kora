import asyncio
import os
import signal
import threading
from collections.abc import Callable
from typing import Final

REAP_INTERVAL_SECONDS: Final[float] = 0.01


class ChildOwner:
    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop | None = None,
        on_exit: Callable[[int], None] | None = None,
    ) -> None:
        self.loop = asyncio.get_running_loop() if loop is None else loop
        self.lock = threading.Lock()
        self.bound = threading.Event()
        self.wake = threading.Event()
        self.finished = threading.Event()
        self.exited = asyncio.Event()
        self.pid: int | None = None
        self.status: int | None = None
        self.reaped = False
        self.error: OSError | None = None
        self.joining: asyncio.Future[None] | None = None
        self.thread = threading.Thread(
            target=self.observe, args=(on_exit,), name="kora-child-reaper", daemon=True
        )
        try:
            self.thread.start()
        except BaseException:
            self.bound.set()
            raise

    def start(self, spawn: Callable[[], int]) -> int:
        with self.lock:
            if self.bound.is_set():
                raise RuntimeError("child already bound")
            try:
                self.pid = spawn()
                return self.pid
            finally:
                self.bound.set()

    def abort(self) -> None:
        with self.lock:
            if not self.bound.is_set():
                self.bound.set()

    def probe(self) -> bool:
        if self.pid is None:
            return True
        try:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            self.reaped = True
            return True
        except OSError as error:
            self.error = error
            return True
        if pid == self.pid:
            self.status = status
            self.reaped = True
        return self.reaped

    def observe(self, on_exit: Callable[[int], None] | None) -> None:
        self.bound.wait()
        while True:
            with self.lock:
                if self.probe():
                    break
            self.wake.wait(REAP_INTERVAL_SECONDS)
            self.wake.clear()
        self.finished.set()
        try:
            self.loop.call_soon_threadsafe(self.exited.set)
        except RuntimeError:
            if not self.loop.is_closed():
                raise
        if self.status is not None and on_exit is not None:
            on_exit(self.status)

    def signal(self, value: signal.Signals) -> bool:
        with self.lock:
            if self.error is not None:
                raise self.error
            if self.pid is None or self.reaped:
                return False
            try:
                os.kill(self.pid, value)
            except ProcessLookupError:
                self.wake.set()
                return False
            self.wake.set()
            return True

    def collected(self) -> bool:
        with self.lock:
            if self.error is not None:
                raise self.error
            return self.reaped

    async def join(self) -> None:
        if self.joining is None:
            self.joining = asyncio.get_running_loop().run_in_executor(
                None, self.thread.join
            )
        joining = self.joining
        cancelled = False
        while not joining.done():
            try:
                await asyncio.wait({joining})
            except asyncio.CancelledError:
                cancelled = True
        joining.result()
        if cancelled:
            raise asyncio.CancelledError
