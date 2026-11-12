import asyncio
import os
import sys
import threading
import time
from collections.abc import Callable
from typing import Final

from kora.runner.identity import PROCESS_SCAN_ENTRIES

GRACE_NS: Final[int] = 1_000_000_000
ADOPTED_LIST_BYTES: Final[int] = PROCESS_SCAN_ENTRIES * 11 + 1


def adopted_pids() -> tuple[int, ...] | None:
    found: set[int] = set()
    try:
        with os.scandir("/proc/self/task") as tasks:
            for index, task in enumerate(tasks):
                if index >= PROCESS_SCAN_ENTRIES:
                    return None
                with open(f"/proc/self/task/{task.name}/children", "rb") as source:
                    children = source.read(ADOPTED_LIST_BYTES)
                if len(children) >= ADOPTED_LIST_BYTES:
                    return None
                found.update(int(item) for item in children.split())
                if len(found) > PROCESS_SCAN_ENTRIES:
                    return None
    except (OSError, ValueError):
        return None
    return tuple(found)


class AdoptedReaper:
    def __init__(self, grace_ns: int = GRACE_NS, interval: float = 1.0) -> None:
        self._grace_ns = grace_ns
        self._interval = interval
        self._lock = threading.Condition()
        self.scans = 0
        self._waited: dict[int, int] = {}
        self._holders: list[Callable[[], int | None]] = []
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        with self._lock:
            if self._thread is not None or sys.platform != "linux":
                return
            if self._stop.is_set():
                return
            self._thread = threading.Thread(
                target=self._reap, name="kora-adopted", daemon=True
            )
            self._thread.start()

    def hold(self, holder: Callable[[], int | None]) -> None:
        with self._lock:
            self._holders.append(holder)

    def register(self, pid: int) -> None:
        with self._lock:
            self._waited[pid] = self._waited.get(pid, 0) + 1

    def release(self, pid: int) -> None:
        with self._lock:
            count = self._waited.get(pid, 0) - 1
            if count > 0:
                self._waited[pid] = count
            else:
                self._waited.pop(pid, None)

    def wake(self) -> None:
        self._wake.set()

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._lock:
            thread = self._thread
        if thread is not None:
            await asyncio.get_running_loop().run_in_executor(None, thread.join)

    def _excluded(self) -> set[int]:
        with self._lock:
            excluded = set(self._waited)
            holders = tuple(self._holders)
        for holder in holders:
            pid = holder()
            if pid is not None:
                excluded.add(pid)
        return excluded

    def _reap(self) -> None:
        seen: dict[int, int] = {}
        while not self._stop.is_set():
            self._wake.wait(self._interval)
            self._wake.clear()
            if self._stop.is_set():
                return
            listed = adopted_pids()
            if listed is None:
                self._scanned()
                continue
            now = time.monotonic_ns()
            excluded = self._excluded()
            seen = {pid: seen.get(pid, now) for pid in listed if pid not in excluded}
            for pid, first in tuple(seen.items()):
                if now - first < self._grace_ns or self._waits_on(pid):
                    continue
                try:
                    reaped, _status = os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    seen.pop(pid, None)
                    continue
                if reaped == pid:
                    seen.pop(pid, None)
            self._scanned()

    def _waits_on(self, pid: int) -> bool:
        with self._lock:
            return pid in self._waited

    def _scanned(self) -> None:
        with self._lock:
            self.scans += 1
            self._lock.notify_all()

    def wait_scans(self, count: int, timeout: float) -> bool:
        with self._lock:
            return self._lock.wait_for(lambda: self.scans >= count, timeout)
