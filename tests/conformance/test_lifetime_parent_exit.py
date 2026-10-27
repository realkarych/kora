import json
import os
import signal
import socket
import subprocess
import sys
from io import FileIO
from pathlib import Path

import pytest

from kora.conformance.lifetime import ParentLifetime
from kora.protocols.identity import ProcessIdentity
from kora.protocols.lifetime import CleanupOutcome, ClosureReport
from kora.protocols.time import SystemClock
from kora.runner.channels import BoundedChannel, ChannelError
from kora.runner.filesystem import DirectoryAnchor
from kora.runner.identity import NativeIdentityReader
from kora.runner.processes import ExitAnchor
from tests.conformance.lifetime_support import (
    SENT_OR_SETTLED,
    ControlledLifetimeClock,
    close_recovered_test_owner,
    dispose_test_owner,
    entered_fifo,
    independent_process,
    installed_launcher,
    kill_independent,
    limits,
    retained_invocation,
    retained_journal,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("sink_mode", ["drained", "blocked", "broken"])
async def test_stalled_reopen_helper_does_not_prevent_parent_exit_and_later_recovery(
    tmp_path: Path, sink_mode: str
) -> None:
    reference = retained_invocation(tmp_path, "live")
    clock = SystemClock()
    _, journal = retained_journal(tmp_path, reference.invocation_id)
    independent = independent_process(journal, clock)
    barrier = DirectoryAnchor.open_path(tmp_path)
    os.mkfifo("stall", 0o600, dir_fd=barrier.descriptor)
    reading, writing = os.pipe()
    first, second = socket.socketpair()
    diagnostic_read, diagnostic_write = os.pipe()
    os.set_blocking(diagnostic_write, False)
    if sink_mode == "blocked":
        while True:
            try:
                os.write(diagnostic_write, b"x" * 4096)
            except BlockingIOError:
                break
    if sink_mode == "broken":
        os.close(diagnostic_read)
    parent = subprocess.Popen(
        [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("lifetime_helpers.py")),
            str(tmp_path),
            reference.invocation_id,
            str(second.fileno()),
            str(writing),
            str(barrier.descriptor),
            sink_mode,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=diagnostic_write,
        close_fds=True,
        pass_fds=(second.fileno(), writing, barrier.descriptor),
    )
    second.close()
    os.close(diagnostic_write)
    channel = BoundedChannel(first, clock)
    anchor = ExitAnchor(parent.pid)
    deadline = clock.monotonic_ns() + 10_000_000_000
    assert await channel.read_frame(deadline) == sink_mode.encode("ascii")
    assert await channel.read_frame(deadline) == reference.invocation_id.encode("ascii")
    identity = ProcessIdentity.model_validate_json(await channel.read_frame(deadline))
    helper_anchor = ExitAnchor(identity.pid, direct_child=False)
    recovery_clock = ControlledLifetimeClock()
    recovered: ParentLifetime | None = None
    phase = "fifo_barrier"
    writer: FileIO | None = None

    try:
        writer = await entered_fifo(barrier.descriptor, reading, deadline, clock)
        phase = "parent_close"
        await channel.write_frame(b"close", deadline)
        report = ClosureReport.model_validate_json(
            await channel.read_record(deadline, 1048576)
        )
        await anchor.wait(deadline, clock)
        parent.wait(timeout=0)
        await helper_anchor.wait(deadline, clock)
        recovered = ParentLifetime(
            clock=recovery_clock,
            limits=limits(),
            recovery=reference,
            launcher=installed_launcher(tmp_path),
        )
        await recovered.ready()
        final = await close_recovered_test_owner(
            recovered, recovery_clock, (independent,)
        )

        assert report.state is CleanupOutcome.UNKNOWN
        assert parent.returncode == 1
        assert helper_anchor.exited
        assert final.state is CleanupOutcome.CLOSED
        assert recovered.inventory().helpers.claim_descriptors == 0
        assert not tuple(tmp_path.rglob(reference.invocation_id))
        if sink_mode == "drained":
            encoded = os.read(diagnostic_read, 65536)
            records: list[dict[str, object]] = [
                json.loads(line) for line in encoded.splitlines()
            ]
            unknown = [
                record
                for record in records
                if record.get("event") == "conformance.lifetime.unknown"
            ]
            assert len(unknown) == 1
            assert unknown[0]["level"] == "WARNING"
            assert unknown[0]["invocation_id"] == reference.invocation_id
            assert str(tmp_path).encode() not in encoded
            assert b"signal.pause" not in encoded
    except ChannelError as failure:
        failure.add_note(
            f"phase={phase}, remaining_ns={deadline - clock.monotonic_ns()}, "
            f"parent_status={parent.poll()}, helper_identity={identity}"
        )
        raise
    finally:
        if writer is not None:
            writer.close()
        deadline = clock.monotonic_ns() + 5_000_000_000
        if parent.returncode is None:
            parent.send_signal(signal.SIGKILL)
            await anchor.wait(deadline, clock)
            parent.wait(timeout=0)
        if not helper_anchor.exited:
            current = NativeIdentityReader().read(identity.pid)
            if current.process is not None and current.process.identity == identity:
                os.kill(identity.pid, signal.SIGKILL)
            await helper_anchor.wait(deadline, clock)
        await kill_independent(independent, deadline, SENT_OR_SETTLED)
        if recovered is not None:
            await dispose_test_owner(recovered, recovery_clock)
        channel.close()
        anchor.close()
        helper_anchor.close()
        if sink_mode != "broken":
            os.close(diagnostic_read)
        os.close(reading)
        os.close(writing)
        barrier.close()

    assert parent.returncode is not None
    assert helper_anchor.exited
    assert channel.closed
