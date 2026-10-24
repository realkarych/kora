import socket
import subprocess
import sys
from pathlib import Path

from kora.conformance.io_helper import InstalledIOHelperLauncher, IOHelper
from kora.protocols.time import Clock


class ProbeIOHelperLauncher:
    def __init__(
        self,
        home: Path,
        observation_descriptor: int,
        fifo_directory: int | None = None,
        phase: str = "before",
        pause_at: int | None = None,
        resist_term: bool = False,
    ) -> None:
        self.home = home
        self.observation_descriptor = observation_descriptor
        self.fifo_directory = fifo_directory
        self.phase = phase
        self.pause_at = pause_at
        self.resist_term = resist_term
        self.starts = 0

    def start(self, descriptors: tuple[int, ...], clock: Clock) -> IOHelper:
        self.starts += 1
        if self.pause_at is not None and self.starts != self.pause_at:
            return InstalledIOHelperLauncher(
                home=self.home, state_home=self.home / "state"
            ).start(descriptors, clock)
        parent, child = socket.socketpair()
        inherited = (child.fileno(), self.observation_descriptor, *descriptors)
        environment = {
            "HOME": str(self.home),
            "XDG_STATE_HOME": str(self.home / "state"),
            "KORA_IO_CONTROL_FD": str(child.fileno()),
            "KORA_IO_DESCRIPTORS": ",".join(str(value) for value in descriptors),
            "KORA_TEST_OBSERVATION_FD": str(self.observation_descriptor),
            "KORA_TEST_PAUSE_PHASE": self.phase,
            "KORA_TEST_RESIST_TERM": "1" if self.resist_term else "0",
        }
        if self.fifo_directory is not None:
            inherited = (*inherited, self.fifo_directory)
            environment["KORA_TEST_FIFO_DIRECTORY_FD"] = str(self.fifo_directory)
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    str(Path(__file__).with_name("lifetime_io_probe.py")),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=environment,
                close_fds=True,
                pass_fds=inherited,
                process_group=0,
            )
        except OSError:
            parent.close()
            raise
        finally:
            child.close()
        return IOHelper(process, parent, clock)


async def stalled_lifetime_parent(
    home: Path,
    invocation_id: str,
    control: int,
    observation: int,
    fifo: int,
    sink_mode: str,
    scenario: str = "reopen",
) -> None:
    import asyncio

    from kora.conformance.common import utc_now
    from kora.conformance.diagnostics import COMPONENT
    from kora.conformance.lifetime import LifetimeError, ParentLifetime
    from kora.observability import Level, LoggingSettings, configure_process
    from kora.protocols.lifetime import (
        Acquisition,
        CleanupReference,
        LifetimeLimits,
        ReservationRequest,
        ResourceKind,
        TemporaryTreeRequest,
        new_operation_id,
    )
    from kora.protocols.time import SystemClock
    from kora.runner.channels import BoundedChannel

    diagnostics = configure_process(
        COMPONENT, LoggingSettings(level=Level.DEBUG), "lifetime-exit", utc_now
    )
    clock = SystemClock()
    channel = BoundedChannel(socket.socket(fileno=control), clock)
    pause_at = 5 if scenario == "publisher" else 1 if scenario == "claim" else 2
    launcher = ProbeIOHelperLauncher(
        home,
        observation,
        fifo,
        phase="after" if scenario == "claim" else "before",
        pause_at=pause_at,
    )
    owner = ParentLifetime(
        limits=LifetimeLimits(
            cooperative_seconds=0.1,
            term_seconds=0.1,
            kill_observation_seconds=2.0,
            drain_seconds=1.0,
        ),
        launcher=launcher,
        recovery=None
        if scenario == "publisher"
        else CleanupReference(invocation_id=invocation_id),
    )
    pending: asyncio.Task[Acquisition] | None = None
    if scenario == "publisher":
        tree = await owner.reserve(
            ReservationRequest(
                operation_id=new_operation_id(), kind=ResourceKind.TEMPORARY_TREE
            )
        )
        pending = asyncio.create_task(
            owner.acquire(tree.token, new_operation_id(), TemporaryTreeRequest())
        )
    loop = asyncio.get_running_loop()
    deadline = owner.deadline()
    while launcher.starts < pause_at:
        if clock.monotonic_ns() >= deadline:
            raise TimeoutError
        wake: asyncio.Future[None] = loop.create_future()
        loop.call_soon(wake.set_result, None)
        await wake
    while (sink_mode == "broken" and not diagnostics.snapshot().sink_failed) or (
        sink_mode == "blocked" and diagnostics.writer_is_idle()
    ):
        if clock.monotonic_ns() >= deadline:
            raise TimeoutError
        wake = loop.create_future()
        loop.call_soon(wake.set_result, None)
        await wake
    await channel.write_frame(sink_mode.encode("ascii"), deadline)
    await channel.write_frame(owner.invocation_id.encode("ascii"), deadline)
    helper = owner.mutations.helpers[-1]
    if helper.identity is None:
        raise RuntimeError("helper identity unavailable")
    await channel.write_frame(helper.identity.model_dump_json().encode(), deadline)
    if await channel.read_frame(deadline) != b"close":
        raise ValueError("close control required")
    final_deadline = clock.monotonic_ns() + int(
        (
            owner.limits.cooperative_seconds
            + owner.limits.term_seconds
            + owner.limits.kill_observation_seconds
            + owner.limits.drain_seconds
        )
        * 1_000_000_000
    )
    report = await owner.close(None)
    if pending is not None:
        try:
            await pending
        except LifetimeError:
            if not pending.done():
                raise
    diagnostics.shutdown(
        max(0.0, min(1.0, (final_deadline - clock.monotonic_ns()) / 1_000_000_000))
    )
    await channel.write_record(report.model_dump_json().encode(), deadline)
    channel.close()


if __name__ == "__main__":
    import asyncio

    asyncio.run(
        stalled_lifetime_parent(
            Path(sys.argv[1]),
            sys.argv[2],
            int(sys.argv[3]),
            int(sys.argv[4]),
            int(sys.argv[5]),
            sys.argv[6],
            sys.argv[7] if len(sys.argv) > 7 else "reopen",
        )
    )
    sys.exit(1)
