import asyncio
from dataclasses import dataclass, field

from kora.conformance.io_helper import ChildWatch, KernelChildReaper


class HeldWatch:
    def __init__(self, kernel: ChildWatch | None) -> None:
        self._changed: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.kernel = kernel
        if kernel is not None:
            kernel.changed.add_done_callback(lambda _: self.notify())

    @property
    def changed(self) -> asyncio.Future[None]:
        return self._changed

    def notify(self) -> None:
        if not self._changed.done():
            self._changed.set_result(None)

    def close(self) -> None:
        if self.kernel is not None:
            self.kernel.close()
        self._changed.cancel()


@dataclass
class HeldExit:
    returncode: int
    refusals: int | None
    watches: list[HeldWatch] = field(default_factory=list[HeldWatch])


class DeferredReaper:
    def __init__(self, refusals: int = 2) -> None:
        self.kernel = KernelChildReaper()
        self.refusals = refusals
        self.held: dict[int, HeldExit] = {}
        self.deferred: set[int] = set()
        self.holding: set[int] = set()

    def hold(self, pid: int) -> None:
        self.holding.add(pid)

    def reap(self, pid: int) -> int | None:
        held = self.held.get(pid)
        if held is None:
            returncode = self.kernel.reap(pid)
            if returncode is None:
                return None
            held = HeldExit(returncode, None if pid in self.holding else self.refusals)
            self.held[pid] = held
            if held.refusals != 0:
                self.deferred.add(pid)
        if held.refusals == 0:
            del self.held[pid]
            return held.returncode
        if held.refusals is not None:
            held.refusals -= 1
            if held.refusals == 0:
                self.release_watches(held)
        return None

    def complete(self, pid: int) -> None:
        held = self.held[pid]
        held.refusals = 0
        self.release_watches(held)

    def release_watches(self, held: HeldExit) -> None:
        for watch in held.watches:
            watch.notify()
        held.watches.clear()

    def watch(self, pid: int) -> ChildWatch:
        held = self.held.get(pid)
        if held is None:
            return HeldWatch(self.kernel.watch(pid))
        watch = HeldWatch(None)
        held.watches.append(watch)
        watch.changed.add_done_callback(lambda _: self.forget(pid, watch))
        return watch

    def forget(self, pid: int, watch: HeldWatch) -> None:
        held = self.held.get(pid)
        if held is not None and watch in held.watches:
            held.watches.remove(watch)
