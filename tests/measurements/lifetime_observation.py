import asyncio
import errno
import gc
import os
import resource
import selectors
import sys
import threading
import time
import tracemalloc
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from kora.conformance.lifetime import ParentLifetime, RecoveryAttempts
from kora.protocols.lifetime import ProcessRegistration
from tests.measurements.lifetime_measurement_support import (
    NativeCounters,
    descriptor_kinds,
)

Phase = Literal["import", "acquire", "use", "close", "settled"]
INTERVAL_SECONDS: float = 0.02
MAX_POINTS: int = 32768
MAX_POINT_BYTES: int = 4096


class MeasuredLoop(asyncio.SelectorEventLoop):
    _scheduled: list[asyncio.TimerHandle]
    _selector: selectors.BaseSelector

    def timers(self) -> int:
        return sum(not handle.cancelled() for handle in self._scheduled)

    def watches(self) -> int:
        return len(self._selector.get_map())


class Point(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cycle: int
    workload: str
    phase: Phase
    elapsed_ns: int
    heap_bytes: int
    heap_peak_bytes: int
    rss_bytes: int
    native_peak_bytes: int
    python_threads: int
    native_threads: int
    tasks: int
    timers: int
    watches: int
    imports: int
    descriptors: dict[str, int]
    child_rss: dict[int, int]
    departed_child_counters: int
    ownership: dict[str, int]
    recovery: dict[str, int]


def ownership(owner: ParentLifetime | None) -> dict[str, int]:
    if owner is None:
        return {}
    current = owner.inventory()
    helpers = current.helpers
    return {
        "resources": current.resources,
        "pending_operations": current.pending_operations,
        "processes": current.retained_processes,
        "trees": current.retained_trees,
        "endpoints": current.endpoint_count,
        "control_bytes": current.control_bytes,
        "execution_bytes": current.execution_bytes,
        "ordinary_owners": current.ordinary_owners,
        "cleanup_drivers": current.cleanup_driver_tasks,
        "cleanup_effects": current.cleanup_effect_tasks,
        "closure_results": current.closure_results,
        "member_capacity": current.member_capacity,
        "process_readiness": current.pending_process_readiness,
        "live_helpers": helpers.live_helpers,
        "retained_helpers": helpers.retained_helpers,
        "pending_mutations": helpers.pending_mutations,
        "directory_descriptors": helpers.directory_descriptors,
        "claim_descriptors": helpers.claim_descriptors,
        "journal_bytes": helpers.journal_bytes,
        "helper_origins": helpers.retained_origins,
        "cleanup_permits": helpers.cleanup_permits,
    }


class Observer:
    def __init__(
        self, path: Path, loop: MeasuredLoop, attempts: RecoveryAttempts
    ) -> None:
        self.descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        self.loop = loop
        self.attempts = attempts
        self.native = NativeCounters()
        self.started = time.perf_counter_ns()
        self.count = 0
        self.cycle = -1
        self.workload = "startup"
        self.phase: Phase = "import"
        self.owner: ParentLifetime | None = None
        self.monitor: asyncio.Task[None] | None = None
        self.stopping = asyncio.Event()

    def point(self) -> Point:
        heap, peak = tracemalloc.get_traced_memory()
        resident, threads = self.native.read(os.getpid())
        native_peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        recovery = self.attempts.inventory()
        child_rss: dict[int, int] = {}
        departed = 0
        pids: set[int] = set()
        if self.owner is not None:
            pids.update(
                helper.process.pid
                for helper in self.owner.mutations.helpers
                if not helper.closed
            )
            pids.update(
                registration.process.pid
                for record in self.owner.records.values()
                if isinstance(
                    registration := record.entry.registration, ProcessRegistration
                )
                and (process := record.process or record.recovered) is not None
                and not process.closed
            )
        for pid in pids:
            try:
                child_rss[pid] = self.native.read(pid)[0]
            except OSError as failure:
                if failure.errno not in (errno.ESRCH, errno.ENOENT):
                    raise
                departed += 1
        return Point(
            cycle=self.cycle,
            workload=self.workload,
            phase=self.phase,
            elapsed_ns=time.perf_counter_ns() - self.started,
            heap_bytes=heap,
            heap_peak_bytes=peak,
            rss_bytes=resident,
            native_peak_bytes=native_peak * (1024 if sys.platform == "linux" else 1),
            python_threads=threading.active_count(),
            native_threads=threads,
            tasks=len(asyncio.all_tasks()),
            timers=self.loop.timers(),
            watches=self.loop.watches(),
            imports=len(sys.modules),
            descriptors=descriptor_kinds(),
            child_rss=child_rss,
            departed_child_counters=departed,
            ownership=ownership(self.owner),
            recovery={
                "contexts": recovery.contexts,
                "attempts": recovery.attempts,
                "placeholders": recovery.placeholders,
                "active_tasks": recovery.active_tasks,
                "completed_tasks": recovery.completed_tasks,
                "callbacks": recovery.dependency_callbacks,
            },
        )

    def record(self) -> Point:
        if self.count >= MAX_POINTS:
            raise ValueError("measurement sample capacity exceeded")
        point = self.point()
        encoded = point.model_dump_json().encode("ascii") + b"\n"
        if len(encoded) > MAX_POINT_BYTES:
            raise ValueError("measurement sample exceeds bound")
        if os.write(self.descriptor, encoded) != len(encoded):
            raise OSError("incomplete measurement sample")
        self.count += 1
        return point

    async def sample(self) -> None:
        while not self.stopping.is_set():
            self.record()
            try:
                async with asyncio.timeout(INTERVAL_SECONDS):
                    await self.stopping.wait()
            except TimeoutError:
                continue

    async def begin(self, cycle: int, workload: str, phase: Phase) -> None:
        await self.finish()
        self.cycle = cycle
        self.workload = workload
        self.phase = phase
        self.stopping.clear()
        tracemalloc.reset_peak()
        self.record()
        self.monitor = asyncio.create_task(self.sample())

    async def finish(self) -> None:
        if self.monitor is not None:
            self.stopping.set()
            task = self.monitor
            self.monitor = None
            await task
            self.record()

    async def settled(self) -> Point:
        await self.finish()
        self.owner = None
        self.phase = "settled"
        gc.collect()
        barrier: asyncio.Future[None] = self.loop.create_future()
        self.loop.call_soon(barrier.set_result, None)
        await barrier
        return self.record()

    async def close(self) -> None:
        try:
            await self.finish()
        finally:
            os.close(self.descriptor)
