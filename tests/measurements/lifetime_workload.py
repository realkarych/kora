import asyncio
import contextvars
import gc
import os
import resource
import socket
import subprocess
import sys
import time
import tracemalloc
import weakref
from pathlib import Path
from types import TracebackType
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict

from kora.conformance.controllers import duration_ns
from kora.conformance.lifetime import LifetimeError, ParentLifetime, RecoveryAttempts
from kora.conformance.transport import LifetimeEndpoint
from kora.protocols.identity import ProcessIdentity
from kora.protocols.lifetime import (
    CleanupOutcome,
    CleanupReference,
    EnvironmentVariable,
    LifetimeReason,
    ProcessDomain,
    ProcessLaunch,
    ProcessOutput,
    ProcessRegistration,
    ReservationRequest,
    ResourceKind,
    TemporaryTreeRequest,
    new_operation_id,
)
from kora.protocols.time import SystemClock
from kora.runner.identity import NativeIdentityReader
from kora.runner.processes import ExitAnchor
from tests.conformance.test_lifetime_cleanup_controls import (
    close_recovered_test_owner,
    dispose_test_owner,
)
from tests.conformance.test_lifetime_pending_helpers import ControlledLifetimeClock
from tests.conformance.test_parent_lifetime import limits
from tests.measurements.lifetime_measurement_support import MeasuredIOHelperLauncher
from tests.measurements.lifetime_observation import MeasuredLoop, Observer, Point

Workload = Literal["success", "failure", "cancellation", "recovery", "shutdown"]
WORKLOADS: tuple[Workload, ...] = (
    "success",
    "failure",
    "cancellation",
    "recovery",
    "shutdown",
)
SEQUENCE: tuple[Workload, ...] = (
    *WORKLOADS,
    *WORKLOADS,
    *("success" for _ in range(30)),
    *("failure" for _ in range(10)),
    *("cancellation" for _ in range(10)),
    *("recovery" for _ in range(10)),
    *("shutdown" for _ in range(10)),
)


class CycleResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cycle: int
    workload: Workload
    seconds: float
    helpers: int
    captured_bytes: int
    process_code: int | None
    journal_removed: bool
    owner_collected: bool
    input_collected: bool
    original_parent_heap_peak_bytes: int | None
    original_parent_native_peak_bytes: int | None


class RunResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pid: int
    cycles: tuple[CycleResult, ...]
    settled: tuple[Point, ...]
    helpers: int
    samples: int
    retaining_paths: tuple[str, ...]
    retained_types: tuple[dict[str, int], ...]


class OriginalResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reference: CleanupReference
    helpers: int
    heap_peak_bytes: int
    native_peak_bytes: int
    identities: tuple[ProcessIdentity, ...]
    unobserved_pids: tuple[int, ...]


async def turn() -> None:
    loop = asyncio.get_running_loop()
    barrier: asyncio.Future[None] = loop.create_future()
    loop.call_soon(barrier.set_result, None)
    await barrier


async def establish(owner: ParentLifetime) -> tuple[str, str]:
    tree = await owner.reserve(
        ReservationRequest(
            operation_id=new_operation_id(), kind=ResourceKind.TEMPORARY_TREE
        )
    )
    await owner.acquire(tree.token, new_operation_id(), TemporaryTreeRequest())
    child = await owner.reserve(
        ReservationRequest(
            operation_id=new_operation_id(),
            kind=ResourceKind.PROCESS_DOMAIN,
            parent_resource_id=tree.token.resource_id,
        )
    )
    return tree.token.resource_id, child.token.resource_id


def launch_value(
    tree: str, address: str, workload: Workload, near: bool
) -> ProcessLaunch:
    return ProcessLaunch(
        executable="/kora-measurement-missing-executable"
        if workload == "failure"
        else sys.executable,
        argv=(
            sys.executable,
            "-I",
            str(Path(__file__).with_name("lifetime_client_workload.py")),
            address,
        )
        if workload == "success"
        else (sys.executable, "-I", "-c", "import signal; signal.pause()")
        if workload == "shutdown"
        else (sys.executable, "-I", "-c", "pass"),
        environment=tuple(
            EnvironmentVariable(name=f"KORA_INPUT_{number}", value="v" * 55000)
            for number in range(8)
        )
        if near
        else (),
        working_directory_resource_id=tree,
        stdin=None,
        stdout=ProcessOutput.CAPTURE,
        stderr=ProcessOutput.CAPTURE,
        domain=ProcessDomain.INVOCATION,
    )


async def ordinary_cycle(
    owner: ParentLifetime,
    clock: ControlledLifetimeClock,
    observer: Observer,
    home: Path,
    index: int,
    workload: Workload,
) -> tuple[int, int | None, tuple[weakref.ReferenceType[ProcessLaunch], ...]]:
    address = home / "rpc"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(address))
        listener.listen(1)
        listener.setblocking(False)
        acquisition = None
        replay = None
        endpoint_task = None
        try:
            tree, child = await establish(owner)
            token = owner.records[child].token
            operation_id = new_operation_id()
            request = launch_value(
                tree, str(address), workload, workload == "success" and index % 2 == 0
            )
            copied = ProcessLaunch.model_validate_json(request.model_dump_json())
            assert copied == request
            assert copied is not request
            if request.environment:
                assert copied.environment[0].value is not request.environment[0].value
            references = (weakref.ref(request), weakref.ref(copied))
            acquisition = asyncio.create_task(
                owner.acquire(token, operation_id, request)
            )
            replay = asyncio.create_task(owner.acquire(token, operation_id, copied))
            if workload == "cancellation":
                while not owner.inventory().pending_operations:
                    if acquisition.done():
                        raise AssertionError(await acquisition)
                    await turn()
                acquisition.cancel()
                try:
                    await acquisition
                except asyncio.CancelledError:
                    if not acquisition.cancelled():
                        raise
                acquired = await replay
            else:
                acquired = await acquisition
                assert await replay == acquired
            await observer.begin(index, workload, "use")
            if workload == "failure":
                with pytest.raises(LifetimeError) as refused:
                    await owner.acquire(
                        token,
                        operation_id,
                        request.model_copy(
                            update={"argv": (*request.argv, "conflict")}
                        ),
                    )
                assert refused.value.reason is LifetimeReason.IDENTITY_MISMATCH
            process = owner.records[child].process
            assert process is not None
            if workload != "shutdown" or index % 2:
                await owner.acknowledge(token, acquired.operation_id, acquired.revision)
            if workload == "success":
                endpoint, _ = await asyncio.get_running_loop().sock_accept(listener)
                transport = LifetimeEndpoint(owner, child, endpoint)
                endpoint_task = asyncio.create_task(transport.run())
                await observer.begin(index, workload, "close")
                closed = await endpoint_task
            else:
                if workload != "shutdown":
                    await process.settle(owner.deadline())
                await observer.begin(index, workload, "close")
                closing = owner.request_close(None)
                if workload == "shutdown":
                    clock.advance(duration_ns(owner.limits.cooperative_seconds) + 1)
                closed = await asyncio.shield(closing)
            assert closed.state is CleanupOutcome.CLOSED
            assert process.closed
            assert process.readiness() == ()
            registration = owner.records[child].entry.registration
            assert isinstance(registration, ProcessRegistration)
            try:
                os.waitpid(registration.process.pid, os.WNOHANG)
            except ChildProcessError:
                pass
            else:
                raise AssertionError("child was not already reaped")
            if workload in ("success", "cancellation"):
                assert process.returncode == 0
            if workload == "failure":
                assert process.returncode != 0
            if workload == "success":
                assert process.capture_bytes == 65536
            return process.capture_bytes, process.returncode, references
        finally:
            if acquisition is not None:
                acquisition.cancel()
                await asyncio.gather(acquisition, return_exceptions=True)
            if replay is not None:
                replay.cancel()
                await asyncio.gather(replay, return_exceptions=True)
            if endpoint_task is not None:
                endpoint_task.cancel()
                await asyncio.gather(endpoint_task, return_exceptions=True)
            address.unlink(missing_ok=True)


async def run_original(home: Path, observation: int) -> None:
    launcher = MeasuredIOHelperLauncher(home, observation, cold_first=False)
    clock = ControlledLifetimeClock()
    owner = ParentLifetime(clock=clock, launcher=launcher, limits=limits())
    retained = False
    try:
        async with asyncio.timeout(10):
            tree, child = await establish(owner)
            token = owner.records[child].token
            request = launch_value(tree, "", "shutdown", False)
            acquired = await owner.acquire(token, new_operation_id(), request)
            await owner.acknowledge(token, acquired.operation_id, acquired.revision)
            observed = NativeIdentityReader().read(os.getpid())
            assert observed.process is not None
            assert isinstance(acquired.registration, ProcessRegistration)
            launcher.identities.update(
                (observed.process.identity, acquired.registration.process)
            )
            encoded = (
                OriginalResult(
                    reference=CleanupReference(invocation_id=owner.invocation_id),
                    helpers=launcher.starts,
                    heap_peak_bytes=tracemalloc.get_traced_memory()[1],
                    native_peak_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                    * (1024 if sys.platform == "linux" else 1),
                    identities=tuple(launcher.identities),
                    unobserved_pids=tuple(launcher.unobserved_pids),
                )
                .model_dump_json()
                .encode("ascii")
            )
            if len(encoded) > 4096:
                raise ValueError("original parent observation exceeds bound")
            if os.write(1, encoded) != len(encoded):
                raise OSError("incomplete recovery reference")
            retained = True
    finally:
        if not retained:
            await dispose_test_owner(owner, clock)


async def recovery_cycle(
    home: Path,
    observation: int,
    launcher: MeasuredIOHelperLauncher,
    attempts: RecoveryAttempts,
    observer: Observer,
    index: int,
) -> tuple[bool, int, int, int, weakref.ReferenceType[ParentLifetime]]:
    with subprocess.Popen(
        [
            sys.executable,
            "-X",
            "tracemalloc=1",
            "-m",
            "tests.measurements.lifetime_workload",
            "original",
            str(home),
            str(observation),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        pass_fds=(observation,),
        process_group=0,
    ) as original:
        assert original.stdout is not None
        anchor = ExitAnchor(original.pid)
        actual = SystemClock()
        deadline = actual.monotonic_ns() + 10_000_000_000
        try:
            await anchor.wait(deadline, actual)
            assert original.wait() == 0
            result = OriginalResult.model_validate_json(original.stdout.read(4096))
            reference = result.reference
            launcher.identities.update(result.identities)
            launcher.unobserved_pids.update(result.unobserved_pids)
        finally:
            anchor.close()
            if original.poll() is None:
                original.kill()
            original.wait()
    clock = ControlledLifetimeClock()
    await observer.begin(index, "recovery", "use")
    resuming = asyncio.create_task(
        attempts.resume(
            reference,
            limits(),
            launcher=launcher,
            home=str(home),
            state_home=str(home / "state"),
            clock=clock,
        )
    )
    owner = None
    try:
        while attempts.context is None or not attempts.context.attempts:
            if resuming.done():
                raise AssertionError(await resuming)
            await turn()
        attempt = next(iter(attempts.context.attempts.values()))
        while attempt.owner is None:
            if resuming.done():
                raise AssertionError(await resuming)
            await turn()
        owner = attempt.owner
        observer.owner = owner
        await owner.ready()
        await observer.begin(index, "recovery", "close")
        observers = tuple(
            record.recovered
            for record in owner.records.values()
            if record.recovered is not None
        )
        await close_recovered_test_owner(owner, clock, observers)
        report = await resuming
        assert report.state is CleanupOutcome.CLOSED
        assert not attempts.inventory().attempts
        assert owner.mutations.claim is None
        assert all(process.closed for process in observers)
        assert all(process.watcher_count == 0 for process in observers)
        return (
            not invocation_path(home, reference.invocation_id).exists(),
            result.helpers,
            result.heap_peak_bytes,
            result.native_peak_bytes,
            weakref.ref(owner),
        )
    finally:
        if owner is not None:
            await dispose_test_owner(owner, clock)
        resuming.cancel()
        await asyncio.gather(resuming, return_exceptions=True)


def invocation_path(home: Path, invocation_id: str) -> Path:
    root = (
        home / "Library" / "Application Support"
        if sys.platform == "darwin"
        else home / "state"
    )
    return root / "kora" / "conformance" / invocation_id


def retained_types() -> dict[str, int]:
    counts = {"execution_snapshots": 0, "contexts": 0, "tracebacks": 0}
    for value in gc.get_objects():
        if isinstance(value, ProcessLaunch):
            counts["execution_snapshots"] += 1
        elif isinstance(value, contextvars.Context):
            counts["contexts"] += 1
        elif isinstance(value, TracebackType):
            counts["tracebacks"] += 1
    return counts


async def measure(home: Path, artifacts: Path, loop: MeasuredLoop) -> RunResult:
    helper_fd = os.open(
        artifacts / "helpers.jsonl",
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND | os.O_NOFOLLOW,
        0o600,
    )
    launcher = MeasuredIOHelperLauncher(home, helper_fd)
    attempts = RecoveryAttempts()
    observer = Observer(artifacts / "points.jsonl", loop, attempts, launcher)
    cycles: list[CycleResult] = []
    settled: list[Point] = []
    retained: list[dict[str, int]] = []
    baseline = None
    try:
        observer.record()
        for index, workload in enumerate(SEQUENCE):
            started = time.perf_counter()
            before_helpers = launcher.starts
            await observer.begin(index, workload, "acquire")
            owner = None
            request_references: tuple[weakref.ReferenceType[ProcessLaunch], ...] = ()
            captured = 0
            code = None
            owner_reference = None
            removed = False
            original_helpers = 0
            original_peak = None
            original_native = None
            clock = ControlledLifetimeClock()
            try:
                async with asyncio.timeout(10):
                    if workload == "recovery":
                        (
                            removed,
                            original_helpers,
                            original_peak,
                            original_native,
                            owner_reference,
                        ) = await recovery_cycle(
                            home, helper_fd, launcher, attempts, observer, index
                        )
                    else:
                        owner = ParentLifetime(
                            clock=clock, launcher=launcher, limits=limits()
                        )
                        owner_reference = weakref.ref(owner)
                        observer.owner = owner
                        captured, code, request_references = await ordinary_cycle(
                            owner, clock, observer, home, index, workload
                        )
                        assert owner.inventory().retained_processes == 0
                        assert owner.inventory().retained_trees == 0
                        assert owner.inventory().endpoint_count == 0
                        assert owner.inventory().ordinary_owners == 0
                        assert owner.inventory().cleanup_driver_tasks == 0
                        assert owner.inventory().cleanup_effect_tasks == 0
                        assert owner.mutations.claim is None
                        removed = not invocation_path(
                            home, owner.invocation_id
                        ).exists()
            finally:
                if owner is not None:
                    await dispose_test_owner(owner, clock)
                owner = None
            point = await observer.settled()
            collected = owner_reference() is None
            input_collected = all(
                reference() is None for reference in request_references
            )
            assert collected
            assert input_collected
            assert removed
            assert not point.recovery["attempts"]
            cycles.append(
                CycleResult(
                    cycle=index,
                    workload=workload,
                    seconds=time.perf_counter() - started,
                    helpers=launcher.starts - before_helpers + original_helpers,
                    captured_bytes=captured,
                    process_code=code,
                    journal_removed=removed,
                    owner_collected=collected,
                    input_collected=input_collected,
                    original_parent_heap_peak_bytes=original_peak,
                    original_parent_native_peak_bytes=original_native,
                )
            )
            if (index + 1) % 10 == 0:
                settled.append(point)
                retained.append(retained_types())
                snapshot = tracemalloc.take_snapshot()
                snapshot.dump(str(artifacts / f"heap-{index + 1}.snapshot"))
                if baseline is None:
                    baseline = snapshot
        assert baseline is not None
        final = tracemalloc.take_snapshot()
        retaining = tuple(
            f"{entry.traceback}: {entry.size_diff} bytes, {entry.count_diff} objects"
            for entry in final.compare_to(baseline, "lineno")[:20]
        )
        return RunResult(
            pid=os.getpid(),
            cycles=tuple(cycles),
            settled=tuple(settled),
            helpers=sum(cycle.helpers for cycle in cycles),
            samples=observer.count,
            retaining_paths=retaining,
            retained_types=tuple(retained),
        )
    finally:
        await observer.close()
        os.close(helper_fd)
        gc.collect()


def main() -> None:
    if sys.argv[1] == "original":
        asyncio.run(run_original(Path(sys.argv[2]), int(sys.argv[3])))
        return
    if not tracemalloc.is_tracing():
        raise RuntimeError("interpreter allocation tracing required")
    loop = MeasuredLoop()
    with asyncio.Runner(loop_factory=lambda: loop) as runner:
        result = runner.run(measure(Path(sys.argv[1]), Path(sys.argv[2]), loop))
    os.write(1, result.model_dump_json().encode("ascii"))


if __name__ == "__main__":
    main()
