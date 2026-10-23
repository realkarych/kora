import fcntl
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from kora.conformance.journal import JournalSnapshot
from kora.protocols.lifetime import CleanupOutcome, ClosureReport, ProcessRegistration
from kora.protocols.time import SystemClock
from kora.runner.channels import BoundedChannel
from kora.runner.identity import NativeIdentityReader
from kora.runner.processes import ExitAnchor, RecoveredProcess, SignalResult


@pytest.mark.asyncio
async def test_completed_unknown_resume_disposes_volatile_descriptors_after_gc(
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
    invocation_id = probe.stdout.decode("ascii")
    path = next(tmp_path.rglob(invocation_id)) / "journal"
    snapshot = JournalSnapshot.model_validate_json(path.read_bytes())
    tree = path.parent / snapshot.entries[0].resource_id
    tree.rename(path.parent / "retained-tree")
    tree.mkdir(mode=0o700)
    registration = snapshot.entries[-1].registration
    assert isinstance(registration, ProcessRegistration)
    clock = SystemClock()
    independent = RecoveredProcess(registration, clock, NativeIdentityReader())
    first, second = socket.socketpair()
    parent = subprocess.Popen(
        [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("lifetime_resume_probe.py")),
            str(tmp_path),
            invocation_id,
            str(second.fileno()),
        ],
        env={"HOME": str(tmp_path), "XDG_STATE_HOME": str(tmp_path / "state")},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        pass_fds=(second.fileno(),),
    )
    second.close()
    channel = BoundedChannel(first, clock)
    anchor = ExitAnchor(parent.pid)
    deadline = clock.monotonic_ns() + 10_000_000_000

    try:
        report = ClosureReport.model_validate_json(
            await channel.read_record(deadline, 1048576)
        )
        retained = int.from_bytes(await channel.read_frame(deadline), "big")
        current = JournalSnapshot.model_validate_json(path.read_bytes())
        claim = os.open(path.parent / "claim", os.O_RDWR | os.O_NOFOLLOW)
        try:
            fcntl.flock(claim, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(claim)

        assert report.state is CleanupOutcome.UNKNOWN
        assert retained == 0
        assert current.entries == snapshot.entries
        assert current.generation == snapshot.generation + 1
        assert path.exists()
        await channel.write_frame(b"exit", deadline)
        await anchor.wait(deadline, clock)
        parent.wait(timeout=0)
        assert parent.returncode == 0
    finally:
        deadline = clock.monotonic_ns() + 5_000_000_000
        if parent.returncode is None:
            parent.send_signal(signal.SIGKILL)
            await anchor.wait(deadline, clock)
            parent.wait(timeout=0)
        assert independent.signal(signal.SIGKILL, deadline) in (
            SignalResult.SENT,
            SignalResult.SETTLED,
        )
        await independent.settle(deadline)
        independent.release(deadline)
        anchor.close()
        channel.close()
