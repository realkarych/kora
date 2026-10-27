import asyncio
import gc
import json
import os
import signal
import socket
import sys
import time
import tracemalloc
import weakref
from pathlib import Path

from kora.cli import CLI_COMPONENT
from kora.conformance.command import ConformanceCommand
from kora.conformance.lifetime import RecoveryAttempts
from kora.conformance.result import ChildInput, InputFiles
from kora.observability import LoggingSettings, ProcessDiagnostics, configure_process
from kora.protocols.lifetime import (
    CleanupOutcome,
    CleanupReference,
    LifetimeLimits,
    OriginalOutcome,
)
from kora.protocols.metadata import ProtocolName
from kora.protocols.time import SystemClock
from tests.fixtures.provider_tooling.src.kora_tooling_fixture.clock import ProbeClock
from tests.measurements.lifetime_measurement_support import MeasuredIOHelperLauncher
from tests.measurements.lifetime_observation import MeasuredLoop, Observer, Point
from tests.measurements.lifetime_workload import OriginalResult
from tests.measurements.listing_observation import assert_absent
from tests.measurements.listing_workload import read_counter

WARMUP: tuple[str, ...] = (
    "success",
    "prepare",
    "exercise",
    "cancel",
    "shutdown",
    "recovery",
    "success",
    "success",
    "success",
    "success",
)
SEQUENCE: tuple[str, ...] = (
    WARMUP
    + ("success",) * 30
    + ("prepare",) * 10
    + ("exercise",) * 10
    + ("cancel",) * 10
    + ("shutdown",) * 10
    + ("recovery",) * 10
)


async def ordinary(
    request: ChildInput,
    case: str,
    listener: socket.socket,
    observer: Observer,
    diagnostics: ProcessDiagnostics,
    references: list[weakref.ReferenceType[object]],
) -> dict[str, object]:
    clock = ProbeClock()
    command = ConformanceCommand(diagnostics, clock=clock, launcher=observer.launcher)
    observer.owner = command.owner
    operation = asyncio.create_task(command.run(request))
    endpoint: socket.socket | None = None
    try:
        async with asyncio.timeout(30):
            loop = asyncio.get_running_loop()
            endpoint, _ = await loop.sock_accept(listener)
            barrier = await read_counter(endpoint)
            assert barrier is not None
            observer.record()
            if case == "cancel":
                operation.cancel()
                await command.owner.closure_started.wait()
                clock.advance(5_000_000_000)
            else:
                await loop.sock_sendall(endpoint, b"a")
            if case == "shutdown":
                await command.owner.closure_started.wait()
                clock.advance(5_000_000_000)
            result = await operation
            final = await read_counter(endpoint)
            assert result.cleanup is not None
            assert result.cleanup.state is CleanupOutcome.CLOSED
            assert result.outcome is (
                OriginalOutcome.COMPLETE
                if case == "success"
                else OriginalOutcome.CANCELLED
                if case == "cancel"
                else OriginalOutcome.FAILED
            )
            if case == "success":
                assert result.completed == result.expected == 10
                assert final is not None
            process = command.process
            assert process is not None
            assert process.closed
            assert process.returncode is not None
            registration = process.registration
            assert registration is not None
            assert_absent(registration.process.pid, registration.process_group_id)
            assert not Path(barrier.root).exists()
            assert command.owner.inventory().helpers.retained_helpers == 0
            references.extend(
                weakref.ref(item)
                for item in (command, command.owner, operation, process)
            )
            return {
                "result": result.model_dump(mode="json"),
                "registration": registration.model_dump(mode="json"),
                "returncode": process.returncode,
                "barrier": barrier.model_dump(),
                "final": None if final is None else final.model_dump(),
                "captured_bytes": process.capture_bytes,
                "released": True,
            }
    finally:
        if endpoint is not None:
            endpoint.close()
        clock.use_system_time()
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)


async def recovery(
    request: ChildInput,
    listener: socket.socket,
    observer: Observer,
    home: Path,
    folder: Path,
) -> dict[str, object]:
    with (folder / "original-stderr.txt").open("xb") as errors:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-X",
            "tracemalloc=1",
            "-m",
            "tests.measurements.command_original",
            str(home),
            request.model_dump_json(),
            str(folder),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=errors,
            close_fds=True,
            limit=65536,
        )
        assert process.stdout is not None
        endpoint: socket.socket | None = None
        try:
            async with asyncio.timeout(30):
                endpoint, _ = await asyncio.get_running_loop().sock_accept(listener)
                barrier = await read_counter(endpoint)
                assert barrier is not None
                original = OriginalResult.model_validate_json(
                    await process.stdout.readline()
                )
                assert not original.unobserved_pids
                observer.launcher.identities.update(original.identities)
                process.kill()
                assert await process.wait() == -signal.SIGKILL
                reference = CleanupReference(invocation_id=Path(barrier.root).name)
                assert original.reference == reference
                report = await observer.attempts.resume(
                    reference,
                    LifetimeLimits(),
                    launcher=observer.launcher,
                    home=str(home),
                    state_home=str(home / "state"),
                )
                assert report.state is CleanupOutcome.CLOSED
                assert not Path(barrier.root).exists()
                final = await read_counter(endpoint)
                return {
                    "cleanup": report.model_dump(mode="json"),
                    "original_parent_pid": process.pid,
                    "original_parent_reaped": True,
                    "original_parent": original.model_dump(mode="json"),
                    "barrier": barrier.model_dump(),
                    "final": None if final is None else final.model_dump(),
                    "released": True,
                }
        finally:
            if endpoint is not None:
                endpoint.close()
            if process.returncode is None:
                process.kill()
            async with asyncio.timeout(5):
                await process.wait()


def compare_resources(actual: Point, baseline: Point) -> None:
    for field in (
        "descriptors",
        "tasks",
        "timers",
        "watches",
        "python_threads",
        "native_threads",
        "imports",
        "recovery",
    ):
        assert getattr(actual, field) == getattr(baseline, field), (
            field,
            actual,
            baseline,
        )
    assert not any(actual.recovery.values())


def run(home: Path, folder: Path, sequence: tuple[str, ...] = SEQUENCE) -> None:
    helper_output = os.open(
        folder / "helpers.jsonl", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
    )
    launcher = MeasuredIOHelperLauncher(home, helper_output)
    attempts = RecoveryAttempts()
    runner = asyncio.Runner(loop_factory=MeasuredLoop)
    loop = runner.get_loop()
    assert isinstance(loop, MeasuredLoop)
    observer = Observer(folder / "points.jsonl", loop, attempts, launcher)
    diagnostics = configure_process(
        CLI_COMPONENT, LoggingSettings(), "command-study", SystemClock().now
    )
    cycles: list[dict[str, object]] = []
    settled: list[Point] = []
    references: list[weakref.ReferenceType[object]] = []
    address = home / "feedback"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(address))
        listener.listen(1)
        listener.setblocking(False)
        try:
            for index, case in enumerate(sequence):
                cycle_folder = folder / f"cycle-{index}"
                cycle_folder.mkdir(mode=0o700)
                configuration = home / "configuration.json"
                data = home / "data.json"
                configuration.write_text(
                    json.dumps({"value": f"private-command-value-{index % 2}"})
                )
                data.write_text(
                    json.dumps(
                        {
                            "mode": "normal" if case == "success" else case,
                            "feedback": str(address),
                        }
                    )
                )
                request = ChildInput(
                    protocol=ProtocolName.SECRETS,
                    name="configured",
                    fixtures="kora_tooling_fixture.fixtures_measured_command",
                    files=InputFiles(
                        configuration=str(configuration), fixture_data=str(data)
                    ),
                )
                started = time.perf_counter()
                before = launcher.starts
                runner.run(observer.begin(index, case, "acquire"))
                result = runner.run(
                    recovery(request, listener, observer, home, cycle_folder)
                    if case == "recovery"
                    else ordinary(
                        request, case, listener, observer, diagnostics, references
                    )
                )
                result.update(
                    cycle=index,
                    workload=case,
                    warmup=index < 10,
                    seconds=time.perf_counter() - started,
                    helpers=launcher.starts - before,
                )
                cycles.append(result)
                (cycle_folder / "result.json").write_text(
                    json.dumps(result, sort_keys=True)
                )
                point = runner.run(observer.settled())
                gc.collect()
                assert all(reference() is None for reference in references)
                references.clear()
                if index == 9:
                    settled.append(point)
                    tracemalloc.take_snapshot().dump(
                        str(folder / "allocations-0.trace")
                    )
                elif index > 9:
                    compare_resources(point, settled[0])
                    if (index + 1) % 10 == 0:
                        settled.append(point)
                        tracemalloc.take_snapshot().dump(
                            str(folder / f"allocations-{index + 1}.trace")
                        )
                assert not tuple(home.rglob("journal"))
                assert not tuple(home.rglob("claim"))
            os.write(
                1,
                json.dumps(
                    {
                        "cycles": cycles,
                        "settled": [point.model_dump() for point in settled],
                        "samples": observer.count,
                        "helpers": launcher.starts,
                    },
                    sort_keys=True,
                ).encode(),
            )
        finally:
            try:
                runner.run(observer.close())
                runner.close()
            finally:
                diagnostics.shutdown()
                os.close(helper_output)
                address.unlink()


if __name__ == "__main__":
    run(Path(sys.argv[1]), Path(sys.argv[2]))
