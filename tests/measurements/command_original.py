import asyncio
import os
import resource
import sys
import tracemalloc
from pathlib import Path

from kora.cli import CLI_COMPONENT
from kora.conformance.command import ConformanceCommand
from kora.conformance.result import ChildInput
from kora.observability import LoggingSettings, configure_process
from kora.protocols.identity import ProcessIdentity
from kora.protocols.lifetime import CleanupReference, ProcessRegistration
from kora.protocols.time import SystemClock
from kora.runner.identity import NativeIdentityReader
from tests.measurements.lifetime_measurement_support import MeasuredIOHelperLauncher
from tests.measurements.lifetime_workload import OriginalResult, turn


async def run(home: Path, request: ChildInput, folder: Path) -> None:
    clock = SystemClock()
    diagnostics = configure_process(
        CLI_COMPONENT, LoggingSettings(), "original-command", clock.now
    )
    descriptor = os.open(
        folder / "original-helpers.jsonl", os.O_WRONLY | os.O_CREAT, 0o600
    )
    launcher = MeasuredIOHelperLauncher(home, descriptor)
    command = ConformanceCommand(diagnostics, launcher=launcher)
    operation = asyncio.create_task(command.run(request))
    try:
        while command.result.phase != "ready" and not operation.done():
            await turn()
        process = command.process
        assert process is not None
        registration = process.registration
        assert isinstance(registration, ProcessRegistration)
        current = NativeIdentityReader().read(os.getpid())
        assert current.process is not None
        identities: tuple[ProcessIdentity, ...] = (
            current.process.identity,
            registration.process,
            *launcher.identities,
        )
        os.write(
            1,
            OriginalResult(
                reference=CleanupReference(invocation_id=command.owner.invocation_id),
                helpers=launcher.starts,
                heap_peak_bytes=tracemalloc.get_traced_memory()[1],
                native_peak_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                * (1024 if sys.platform == "linux" else 1),
                identities=identities,
                unobserved_pids=tuple(launcher.unobserved_pids),
            )
            .model_dump_json()
            .encode()
            + b"\n",
        )
        await operation
    finally:
        operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)
        diagnostics.shutdown()
        os.close(descriptor)


if __name__ == "__main__":
    asyncio.run(
        run(
            Path(sys.argv[1]),
            ChildInput.model_validate_json(sys.argv[2]),
            Path(sys.argv[3]),
        )
    )
