import asyncio
import errno
import gc
import json
import os
import resource
import sys
import threading
import time
import tracemalloc
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from kora.__main__ import InstalledPluginDiscovery
from kora.runner.identity import IdentityState, NativeIdentityReader
from tests.measurements.lifetime_measurement_support import (
    NativeCounters,
    descriptor_kinds,
)
from tests.measurements.lifetime_observation import MeasuredLoop


class ChildCounters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    phase: str
    pid: int
    heap_bytes: int
    heap_peak_bytes: int
    native_peak_bytes: int
    imports: int
    root: str


class ListingPoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cycle: int
    workload: str
    phase: str
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
    child_rss: int | None
    child_threads: int | None
    child_departed: bool
    retained_owners: dict[str, int]


def retained_owners() -> dict[str, int]:
    names = {
        "InstalledPluginDiscovery",
        "GatedProcess",
        "BoundedChannel",
        "CaptureDrain",
        "DirectoryAnchor",
        "ExitAnchor",
        "DescriptorReadiness",
        "PluginListing",
    }
    counts = dict.fromkeys(names, 0)
    for value in gc.get_objects():
        name = type(value).__name__
        if name in counts:
            counts[name] += 1
    return counts


class ListingObserver:
    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.output = (folder / "points.jsonl").open("xb")
        self.native = NativeCounters()
        self.started = time.perf_counter_ns()
        self.loop: MeasuredLoop | None = None
        self.invocation: InstalledPluginDiscovery | None = None
        self.timer: asyncio.TimerHandle | None = None
        self.cycle = -1
        self.workload = "startup"
        self.phase = "import"
        self.samples = 0
        self.failure: OSError | ValueError | None = None

    def point(self, *, owners: bool = False) -> ListingPoint:
        loop = self.loop
        if loop is None:
            raise ValueError("missing measured loop")
        heap, peak = tracemalloc.get_traced_memory()
        resident, threads = self.native.read(os.getpid())
        child_rss: int | None = None
        child_threads: int | None = None
        departed = False
        process = None if self.invocation is None else self.invocation.process
        pid: int | None = None
        if process is not None and process.registration is not None:
            pid = process.registration.process.pid
        if pid is not None:
            try:
                child_rss, child_threads = self.native.read(pid)
            except OSError as error:
                if error.errno not in (errno.ESRCH, errno.ENOENT):
                    raise
                departed = True
        return ListingPoint(
            cycle=self.cycle,
            workload=self.workload,
            phase=self.phase,
            elapsed_ns=time.perf_counter_ns() - self.started,
            heap_bytes=heap,
            heap_peak_bytes=peak,
            rss_bytes=resident,
            native_peak_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            * (1024 if sys.platform == "linux" else 1),
            python_threads=threading.active_count(),
            native_threads=threads,
            tasks=len(asyncio.all_tasks()),
            timers=loop.timers(),
            watches=loop.watches(),
            imports=len(sys.modules),
            descriptors=descriptor_kinds(),
            child_rss=child_rss,
            child_threads=child_threads,
            child_departed=departed,
            retained_owners=retained_owners() if owners else {},
        )

    def record(self, *, owners: bool = False) -> ListingPoint:
        if self.samples >= 100000:
            raise ValueError("listing sample capacity")
        point = self.point(owners=owners)
        encoded = point.model_dump_json().encode() + b"\n"
        if len(encoded) > 4096:
            raise ValueError("listing point capacity")
        self.output.write(encoded)
        self.samples += 1
        return point

    def sample(self) -> None:
        self.timer = None
        try:
            self.record()
        except (OSError, ValueError) as error:
            self.failure = error
            return
        if self.loop is None:
            raise ValueError("missing measured loop")
        self.timer = self.loop.call_later(0.02, self.sample)

    def stop(self) -> None:
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        if self.failure is not None:
            raise self.failure

    async def imported(self) -> None:
        self.phase = "import"
        self.record()

    async def settled(self) -> ListingPoint:
        self.stop()
        self.invocation = None
        self.phase = "settled"
        gc.collect()
        loop = asyncio.get_running_loop()
        ready: asyncio.Future[None] = loop.create_future()
        loop.call_soon(ready.set_result, None)
        await ready
        gc.collect()
        return self.record(owners=True)

    def snapshot(self, index: int) -> None:
        snapshot = tracemalloc.take_snapshot()
        snapshot.dump(str(self.folder / f"allocations-{index}.trace"))
        if index > 0:
            baseline = tracemalloc.Snapshot.load(
                str(self.folder / "allocations-0.trace")
            )
            differences = snapshot.compare_to(baseline, "lineno")[:20]
            (self.folder / f"allocations-{index}.json").write_text(
                json.dumps(
                    [
                        {
                            "traceback": str(item.traceback),
                            "size_diff": item.size_diff,
                            "count_diff": item.count_diff,
                        }
                        for item in differences
                    ],
                    sort_keys=True,
                )
            )

    def close(self) -> None:
        try:
            self.stop()
        finally:
            self.output.close()


def assert_absent(pid: int, group_id: int) -> None:
    reader = NativeIdentityReader()
    assert reader.read(pid).state is IdentityState.ABSENT
    group = reader.group(group_id)
    assert group.state is IdentityState.PRESENT
    assert not group.members
