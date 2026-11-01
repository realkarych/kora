import asyncio
import gc
import json
import os
import signal
import socket
import stat
import sys
import time
import tracemalloc
import weakref
from dataclasses import dataclass
from pathlib import Path

from kora.__main__ import InstalledPluginDiscovery
from kora.cli import CLI_COMPONENT
from kora.cli.plugins import PluginListing
from kora.observability import LoggingSettings, ProcessDiagnostics, configure_process
from kora.protocols.lifetime import ProcessRegistration
from kora.runner.processes import GatedProcess
from tests.fixtures.provider_tooling.src.kora_tooling_fixture.clock import ProbeClock
from tests.measurements.lifetime_observation import MeasuredLoop
from tests.measurements.listing_observation import (
    ChildCounters,
    ListingObserver,
    ListingPoint,
    assert_absent,
)
from tests.measurements.listing_population import assert_listing

WARMUP: tuple[str, ...] = ("success", "failure", "cancel", "shutdown") * 2 + (
    "success",
    "failure",
)
SEQUENCE: tuple[str, ...] = (
    WARMUP
    + ("success",) * 30
    + ("failure",) * 10
    + ("cancel",) * 10
    + ("shutdown",) * 10
)


async def read_counter(endpoint: socket.socket) -> ChildCounters | None:
    value = b""
    while not value.endswith(b"\n"):
        chunk = await asyncio.get_running_loop().sock_recv(endpoint, 1)
        if not chunk:
            if value:
                raise ValueError("truncated child allocation record")
            return None
        value += chunk
        if len(value) > 4096:
            raise ValueError("child allocation record capacity")
    return ChildCounters.model_validate_json(value)


@dataclass
class Cycle:
    invocation: InstalledPluginDiscovery
    operation: asyncio.Task[PluginListing]
    clock: ProbeClock
    endpoint: socket.socket
    registration: ProcessRegistration
    owner: GatedProcess
    barrier: ChildCounters
    started: float


class Workload:
    def __init__(
        self,
        observer: ListingObserver,
        listener: socket.socket,
        diagnostics: ProcessDiagnostics,
        count: int,
    ) -> None:
        self.observer = observer
        self.listener = listener
        self.diagnostics = diagnostics
        self.count = count
        self.active: Cycle | None = None
        self.clock: ProbeClock | None = None
        self.operation: asyncio.Task[PluginListing] | None = None
        self.endpoint: socket.socket | None = None
        self.references: list[weakref.ReferenceType[object]] = []

    async def acquire(self, index: int, case: str) -> None:
        clock = ProbeClock()
        self.clock = clock
        invocation = InstalledPluginDiscovery(self.diagnostics, clock)
        self.observer.invocation = invocation
        self.observer.cycle = index
        self.observer.workload = case
        self.observer.phase = "acquire"
        tracemalloc.reset_peak()
        self.observer.sample()
        started = time.perf_counter()
        operation = asyncio.create_task(invocation.discover())
        self.operation = operation
        async with asyncio.timeout(30):
            endpoint, _ = await asyncio.get_running_loop().sock_accept(self.listener)
            self.endpoint = endpoint
            barrier = await read_counter(endpoint)
        owner = invocation.process
        if barrier is None or owner is None or owner.registration is None:
            raise ValueError("missing listing measurement barrier")
        self.active = Cycle(
            invocation,
            operation,
            clock,
            endpoint,
            owner.registration,
            owner,
            barrier,
            started,
        )
        self.persist_active()
        self.observer.phase = "metadata"
        self.observer.record()
        self.references.extend(
            (weakref.ref(invocation), weakref.ref(owner), weakref.ref(operation))
        )

    def persist_active(self) -> None:
        active = self.active
        if active is None:
            return
        receipt = {
            "cycle": self.observer.cycle,
            "workload": self.observer.workload,
            "registration": active.registration.model_dump(mode="json"),
            "barrier": active.barrier.model_dump(mode="json"),
            "closed": active.owner.closed,
            "returncode": active.owner.returncode,
            "root_retained": Path(active.barrier.root).exists(),
        }
        (self.observer.folder / "active.json").write_text(
            json.dumps(receipt, sort_keys=True)
        )

    def collect_retained(self) -> None:
        self.persist_active()
        active = self.active
        if active is None:
            return
        path = Path(active.barrier.root) / "closure.json"
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 65536:
                raise ValueError("unexpected retained closure evidence")
            evidence = os.read(descriptor, 65537)
            if len(evidence) > 65536:
                raise ValueError("retained closure evidence capacity")
            (self.observer.folder / "retained-closure.json").write_bytes(evidence)
        finally:
            os.close(descriptor)

    async def exercise(self, case: str) -> None:
        active = self.active
        if active is None:
            raise ValueError("missing cycle")
        if case in {"success", "failure"}:
            await asyncio.get_running_loop().sock_sendall(
                active.endpoint, b"r" if case == "success" else b"f"
            )
            result = await active.operation
            assert_listing(result, self.count, failure=case == "failure")
            self.references.append(weakref.ref(result))
            self.observer.phase = "result"
            self.observer.record()
        elif case == "cancel":
            started = active.clock.monotonic_ns()
            active.operation.cancel()
            await active.clock.advance_to_timer(started + 5_000_000_000)
            await self.cancelled(active.operation)
        else:
            active.clock.use_system_time()
        self.observer.phase = "close"
        self.observer.record()

    @staticmethod
    async def cancelled(operation: asyncio.Task[PluginListing]) -> None:
        try:
            await operation
        except asyncio.CancelledError:
            return
        raise AssertionError("discovery was not cancelled")

    async def finish(self, case: str) -> dict[str, object]:
        active = self.active
        if active is None:
            raise ValueError("missing cycle")
        self.observer.stop()
        final = await read_counter(active.endpoint)
        active.endpoint.close()
        assert active.owner.closed
        assert active.owner.returncode is not None
        assert_absent(
            active.registration.process.pid, active.registration.process_group_id
        )
        assert not Path(active.barrier.root).exists()
        if case in {"cancel", "shutdown"}:
            assert active.operation.cancelled()
            assert final is None
        else:
            assert final is not None
            assert final.phase == "final"
        result: dict[str, object] = {
            "workload": case,
            "seconds": time.perf_counter() - active.started,
            "registration": active.registration.model_dump(mode="json"),
            "returncode": active.owner.returncode,
            "barrier": active.barrier.model_dump(),
            "final": None if final is None else final.model_dump(),
            "captured_bytes": active.owner.capture_bytes,
            "released": True,
        }
        self.persist_active()
        self.active = None
        self.clock = None
        self.operation = None
        self.endpoint = None
        return result

    async def settled(self) -> ListingPoint:
        point = await self.observer.settled()
        gc.collect()
        assert all(reference() is None for reference in self.references)
        self.references.clear()
        return point

    async def abort(self) -> None:
        if self.endpoint is not None:
            self.endpoint.close()
        clock = self.clock
        if clock is None:
            return
        clock.use_system_time()
        invocation = self.observer.invocation
        owner = None if invocation is None else invocation.process
        if owner is not None and not owner.closed:
            owner.signal(signal.SIGKILL, clock.monotonic_ns() + 5_000_000_000)
        if self.operation is not None:
            if not self.operation.done():
                self.operation.cancel()
            await asyncio.gather(self.operation, return_exceptions=True)
        if owner is not None and not owner.closed:
            deadline = clock.monotonic_ns() + 5_000_000_000
            await owner.settle(deadline)
            await owner.drain(deadline)
            owner.release(deadline)


def assert_same_resources(actual: ListingPoint, expected: ListingPoint) -> None:
    for field in (
        "python_threads",
        "native_threads",
        "tasks",
        "timers",
        "watches",
        "imports",
        "descriptors",
        "retained_owners",
    ):
        assert getattr(actual, field) == getattr(expected, field), (
            field,
            actual,
            expected,
        )
    assert not any(actual.retained_owners.values())


def main() -> None:
    folder = Path(sys.argv[1])
    control = Path(sys.argv[2]) / f"{os.getpid()}.sock"
    count = int(sys.argv[3])
    observer = ListingObserver(folder)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(control))
    listener.listen(1)
    listener.setblocking(False)
    diagnostics = configure_process(
        CLI_COMPONENT, LoggingSettings(), "listing-measurement", ProbeClock().now
    )
    workload = Workload(observer, listener, diagnostics, count)
    runner = asyncio.Runner(loop_factory=MeasuredLoop)
    cycles: list[dict[str, object]] = []
    settled: list[ListingPoint] = []
    try:
        for index, case in enumerate(SEQUENCE):
            loop = runner.get_loop()
            assert isinstance(loop, MeasuredLoop)
            observer.loop = loop
            if index == 0:
                runner.run(observer.imported())
            runner.run(workload.acquire(index, case))
            runner.run(workload.exercise(case))
            if case == "shutdown":
                runner.close()
                runner = asyncio.Runner(loop_factory=MeasuredLoop)
                loop = runner.get_loop()
                assert isinstance(loop, MeasuredLoop)
                observer.loop = loop
            cycles.append(runner.run(workload.finish(case)))
            point = runner.run(workload.settled())
            if index == 9:
                settled.append(point)
                observer.snapshot(0)
            elif index > 9:
                assert_same_resources(point, settled[0])
                if (index + 1) % 10 == 0:
                    settled.append(point)
                    observer.snapshot(index + 1)
        os.write(
            1,
            json.dumps(
                {
                    "cycles": cycles,
                    "settled": [point.model_dump() for point in settled],
                    "samples": observer.samples,
                },
                sort_keys=True,
            ).encode(),
        )
    finally:
        try:
            try:
                runner.run(workload.abort())
            finally:
                runner.close()
        finally:
            try:
                workload.collect_retained()
            finally:
                diagnostics.shutdown()
                listener.close()
                control.unlink()
                observer.close()


if __name__ == "__main__":
    main()
