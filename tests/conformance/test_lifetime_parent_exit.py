import os
import signal
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from kora.conformance.io_helper import InstalledIOHelperLauncher
from kora.conformance.journal import JournalSnapshot
from kora.conformance.lifetime import ParentLifetime
from kora.protocols.identity import ProcessIdentity
from kora.protocols.lifetime import (
    CleanupOutcome,
    CleanupReference,
    ClosureReport,
    ProcessRegistration,
)
from kora.protocols.time import SystemClock
from kora.runner.channels import BoundedChannel
from kora.runner.filesystem import DirectoryAnchor
from kora.runner.identity import NativeIdentityReader
from kora.runner.processes import ExitAnchor, RecoveredProcess, SignalResult
from tests.conformance.test_lifetime_io_helper import entered_fifo
from tests.conformance.test_parent_lifetime import limits


@pytest.mark.asyncio
async def test_stalled_reopen_helper_does_not_prevent_parent_exit_and_later_recovery(
    tmp_path: Path,
) -> None:
    probe = subprocess.run(
        [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("lifetime_recovery_probe.py")),
            str(tmp_path),
            "live",
        ],
        check=True,
        capture_output=True,
    )
    reference = CleanupReference(invocation_id=probe.stdout.decode("ascii"))
    clock = SystemClock()
    path = next(tmp_path.rglob(reference.invocation_id)) / "journal"
    journal = JournalSnapshot.model_validate_json(path.read_bytes())
    registration = journal.entries[-1].registration
    assert isinstance(registration, ProcessRegistration)
    independent = RecoveredProcess(registration, clock, NativeIdentityReader())
    barrier = DirectoryAnchor.open_path(tmp_path)
    os.mkfifo("stall", 0o600, dir_fd=barrier.descriptor)
    reading, writing = os.pipe()
    first, second = socket.socketpair()
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
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        pass_fds=(second.fileno(), writing, barrier.descriptor),
    )
    second.close()
    channel = BoundedChannel(first, clock)
    anchor = ExitAnchor(parent.pid)
    deadline = clock.monotonic_ns() + 10_000_000_000
    identity = ProcessIdentity.model_validate_json(await channel.read_frame(deadline))
    helper_anchor = ExitAnchor(identity.pid, direct_child=False)
    await entered_fifo(identity.pid, reading, deadline, clock)

    try:
        await channel.write_frame(b"close", deadline)
        report = ClosureReport.model_validate_json(
            await channel.read_record(deadline, 1048576)
        )
        await anchor.wait(deadline, clock)
        parent.wait(timeout=0)
        await helper_anchor.wait(deadline, clock)
        recovered = ParentLifetime(
            limits=limits(),
            recovery=reference,
            launcher=InstalledIOHelperLauncher(
                home=tmp_path, state_home=tmp_path / "state"
            ),
        )
        await recovered.ready()
        final = await recovered.close(None)

        assert report.state is CleanupOutcome.UNKNOWN
        assert parent.returncode == 1
        assert helper_anchor.exited
        assert final.state is CleanupOutcome.CLOSED
        assert recovered.inventory().helpers.claim_descriptors == 0
        assert not tuple(tmp_path.rglob(reference.invocation_id))
    finally:
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
        assert independent.signal(signal.SIGKILL, deadline) in (
            SignalResult.SENT,
            SignalResult.SETTLED,
        )
        await independent.settle(deadline)
        independent.release(deadline)
        channel.close()
        anchor.close()
        helper_anchor.close()
        os.close(reading)
        os.close(writing)
        barrier.close()
