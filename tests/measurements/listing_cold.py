import asyncio
import os
import signal
import socket
import tempfile
import time
from pathlib import Path

from kora.__main__ import InstalledPluginDiscovery
from kora.cli import CLI_COMPONENT
from kora.ir.json import JsonObject
from kora.observability import LoggingSettings, configure_process
from kora.protocols.lifetime import (
    EnvironmentVariable,
    ProcessDomain,
    ProcessLaunch,
    ProcessOutput,
)
from kora.protocols.time import SystemClock
from kora.runner.channels import BoundedChannel
from kora.runner.processes import start_gated
from tests.measurements.lifetime_measurement_support import NativeCounters
from tests.measurements.listing_observation import assert_absent
from tests.measurements.listing_workload import read_counter


def listen(path: Path) -> socket.socket:
    endpoint = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    endpoint.bind(str(path))
    endpoint.listen(1)
    endpoint.setblocking(False)
    return endpoint


async def measure_cold_child(
    python: Path, control: Path, count: int, cutoff: float
) -> JsonObject:
    clock = SystemClock()
    remaining = min(30, cutoff - time.monotonic() - 5)
    if remaining <= 0:
        raise TimeoutError("qualification deadline before cold child")
    deadline = clock.monotonic_ns() + int(remaining * 1_000_000_000)
    loop = asyncio.get_running_loop()
    diagnostics = configure_process(
        CLI_COMPONENT, LoggingSettings(), "listing-cold", clock.now
    )
    invocation = InstalledPluginDiscovery(diagnostics)
    with tempfile.TemporaryDirectory(prefix="kora-list-cold-", dir="/tmp") as temporary:
        root = Path(temporary).resolve()
        cwd = root / "cwd"
        cwd.mkdir()
        control_path = control / f"{os.getpid()}.sock"
        paths = (root / "r", root / "e", control_path)
        listeners = tuple(listen(path) for path in paths)
        accepts = tuple(
            asyncio.create_task(loop.sock_accept(listener)) for listener in listeners
        )
        directory = os.open(cwd, os.O_RDONLY | os.O_DIRECTORY)
        launch = ProcessLaunch(
            executable=str(python),
            argv=(
                "-I",
                "-X",
                "tracemalloc=1",
                "-m",
                "kora_tooling_fixture.cold_child",
                str(paths[0]),
                str(paths[1]),
            ),
            environment=tuple(
                EnvironmentVariable(name=name, value=value)
                for name, value in {
                    "HOME": str(cwd),
                    "TMPDIR": str(cwd),
                    "PATH": os.defpath,
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                }.items()
            ),
            working_directory_resource_id="c" * 32,
            stdin=None,
            stdout=ProcessOutput.CAPTURE,
            stderr=ProcessOutput.CAPTURE,
            domain=ProcessDomain.INVOCATION,
        )
        owner = start_gated(launch, "d" * 32, directory, deadline, clock)
        os.close(directory)
        result_task = None
        events_task = None
        try:
            async with asyncio.timeout(remaining):
                registration = await owner.prepare()
                await owner.acknowledge()
                accepted = await asyncio.gather(*accepts)
                result_endpoint, event_endpoint, counter_endpoint = (
                    pair[0] for pair in accepted
                )
                channel = BoundedChannel(result_endpoint, clock)
                result_task = asyncio.create_task(
                    invocation.read_result(channel, deadline)
                )
                events_task = asyncio.create_task(
                    invocation.read_events(event_endpoint, deadline)
                )
                barrier = await read_counter(counter_endpoint)
                assert barrier is not None
                rss, native_threads = NativeCounters().read(barrier.pid)
                await loop.sock_sendall(counter_endpoint, b"r")
                final = await read_counter(counter_endpoint)
                result, events = await asyncio.gather(result_task, events_task)
                await owner.settle(deadline)
                await owner.drain(deadline)
                owner.release(deadline)

                assert final is not None
                assert final.phase == "final"
                assert len(result.providers) == count
                assert not result.failures
                assert events == count
                assert owner.returncode == 0
                assert_absent(registration.process.pid, registration.process_group_id)
                return {
                    "trace_scope": "startup_through_child_completion",
                    "wrapper": "kora_tooling_fixture.cold_child",
                    "barrier": barrier.model_dump(mode="json"),
                    "final": final.model_dump(mode="json"),
                    "barrier_rss": rss,
                    "barrier_native_threads": native_threads,
                    "released": True,
                }
        finally:
            for task in (*accepts, result_task, events_task):
                if task is not None:
                    task.cancel()
            await asyncio.gather(
                *accepts,
                *(task for task in (result_task, events_task) if task is not None),
                return_exceptions=True,
            )
            for accepted in accepts:
                if not accepted.cancelled() and accepted.exception() is None:
                    accepted.result()[0].close()
            if not owner.closed:
                cleanup_deadline = min(
                    clock.monotonic_ns() + 5_000_000_000, int(cutoff * 1_000_000_000)
                )
                owner.signal(signal.SIGKILL, cleanup_deadline)
                await owner.settle(cleanup_deadline)
                await owner.drain(cleanup_deadline)
                owner.release(cleanup_deadline)
            for listener in listeners:
                listener.close()
            control_path.unlink()
            diagnostics.shutdown()
