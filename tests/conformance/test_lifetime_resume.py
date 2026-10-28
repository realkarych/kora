import fcntl
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from kora.conformance.io_helper import HelperError, RecoverMutation
from kora.conformance.journal import JournalSnapshot
from kora.protocols.lifetime import CleanupOutcome, ClosureReport, LifetimeReason
from kora.protocols.time import SystemClock
from kora.runner.channels import BoundedChannel
from kora.runner.filesystem import DirectoryAnchor
from kora.runner.identity import NativeIdentityReader
from kora.runner.processes import ExitAnchor
from tests.conformance.lifetime_support import (
    SENT_OR_SETTLED,
    independent_process,
    installed_launcher,
    journal_file,
    kill_independent,
    retained_invocation,
    retained_journal,
)


@pytest.mark.asyncio
async def test_recovery_expected_root_refuses_replacement_before_takeover(
    tmp_path: Path,
) -> None:
    original = retained_invocation(tmp_path, "tree")
    old_path = journal_file(tmp_path, original.invocation_id)
    root = DirectoryAnchor.open_path(old_path.parent.parent)
    saved = old_path.read_bytes()
    old_root = old_path.parent.parent
    old_root.rename(old_root.with_name("retained-original"))
    replacement = retained_invocation(tmp_path, "tree")
    new_path = journal_file(tmp_path, replacement.invocation_id)
    new_saved = new_path.read_bytes()
    current = NativeIdentityReader().read(os.getpid())
    assert current.process is not None
    clock = SystemClock()
    helper = installed_launcher(tmp_path).start((), clock)
    deadline = clock.monotonic_ns() + 10_000_000_000

    try:
        mutation = RecoverMutation(
            invocation_id=replacement.invocation_id,
            owner=current.process.identity,
            expected_root=root.identity,
        )
        with pytest.raises(HelperError) as refused:
            await helper.execute(mutation, deadline)

        assert refused.value.reason is LifetimeReason.IDENTITY_MISMATCH
        assert new_path.read_bytes() == new_saved
        assert (
            old_root.with_name("retained-original") / original.invocation_id / "journal"
        ).read_bytes() == saved
        assert not helper.descriptors
    finally:
        if not helper.closed:
            helper.signal(signal.SIGKILL)
            assert await helper.settle(deadline)
        root.close()


@pytest.mark.asyncio
async def test_completed_unknown_resume_disposes_volatile_descriptors_after_gc(
    tmp_path: Path,
) -> None:
    invocation_id = retained_invocation(tmp_path, "live").invocation_id
    path, snapshot = retained_journal(tmp_path, invocation_id)
    tree = path.parent / snapshot.entries[0].resource_id
    tree.rename(path.parent / "retained-tree")
    tree.mkdir(mode=0o700)
    clock = SystemClock()
    independent = independent_process(snapshot, clock)
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
        await kill_independent(independent, deadline, SENT_OR_SETTLED)
        anchor.close()
        channel.close()
