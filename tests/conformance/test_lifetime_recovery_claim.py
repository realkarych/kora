import os
import signal
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from kora.conformance.io_helper import InstalledIOHelperLauncher
from kora.conformance.journal import JournalSnapshot
from kora.conformance.lifetime import LifetimeError, ParentLifetime
from kora.protocols.identity import ProcessIdentity
from kora.protocols.lifetime import (
    CleanupOutcome,
    CleanupReference,
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
@pytest.mark.parametrize("scenario", ["publisher", "claim"])
async def test_recovery_claim_excludes_unpublished_and_crashed_parent_writers(
    tmp_path: Path, scenario: str
) -> None:
    clock = SystemClock()
    invocation_id = "fresh"
    independent: RecoveredProcess | None = None
    if scenario == "claim":
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
        saved = JournalSnapshot.model_validate_json(
            (next(tmp_path.rglob(invocation_id)) / "journal").read_bytes()
        )
        registration = saved.entries[-1].registration
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
            invocation_id,
            str(second.fileno()),
            str(writing),
            str(barrier.descriptor),
            "discard",
            scenario,
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
    deadline = clock.monotonic_ns() + 15_000_000_000
    assert await channel.read_frame(deadline) == b"discard"
    reference = CleanupReference(
        invocation_id=(await channel.read_frame(deadline)).decode("ascii")
    )
    identity = ProcessIdentity.model_validate_json(await channel.read_frame(deadline))
    helper_anchor = ExitAnchor(identity.pid, direct_child=False)
    await entered_fifo(identity.pid, reading, deadline, clock)
    path = next(tmp_path.rglob(reference.invocation_id)) / "journal"
    retained = path.read_bytes()
    snapshot = JournalSnapshot.model_validate_json(retained)
    assert snapshot.owner.pid == parent.pid
    if scenario == "publisher":
        assert identity not in snapshot.helpers
        assert snapshot.pending_effects

    try:
        for stage in ("live_parent", "dead_parent"):
            if stage == "dead_parent":
                parent.send_signal(signal.SIGKILL)
                await anchor.wait(deadline, clock)
                parent.wait(timeout=0)
                assert not helper_anchor.exited
            competing = ParentLifetime(
                limits=limits(),
                recovery=reference,
                launcher=InstalledIOHelperLauncher(
                    home=tmp_path, state_home=tmp_path / "state"
                ),
            )
            with pytest.raises(LifetimeError):
                await competing.ready()
            refused = await competing.close(None)
            assert refused.state is CleanupOutcome.UNKNOWN
            assert path.read_bytes() == retained
        writer = os.open(
            "stall", os.O_WRONLY | os.O_NONBLOCK, dir_fd=barrier.descriptor
        )
        os.close(writer)
        await helper_anchor.wait(deadline, clock)
        published = JournalSnapshot.model_validate_json(path.read_bytes())
        recovered = ParentLifetime(
            limits=limits(),
            recovery=reference,
            launcher=InstalledIOHelperLauncher(
                home=tmp_path, state_home=tmp_path / "state"
            ),
        )
        await recovered.ready()
        report = await recovered.close(None)

        assert published.generation == snapshot.generation
        assert published.owner == snapshot.owner
        if scenario == "publisher":
            assert published.revision > snapshot.revision
            assert published.helpers == (identity,)
            assert not published.pending_effects
        else:
            assert published == snapshot
        assert report.state is CleanupOutcome.CLOSED
        assert recovered.generation == (2 if scenario == "publisher" else 3)
        assert recovered.inventory().helpers.claim_descriptors == 0
        assert not path.parent.exists()
    finally:
        deadline = clock.monotonic_ns() + 5_000_000_000
        if parent.returncode is None:
            parent.send_signal(signal.SIGKILL)
            await anchor.wait(deadline, clock)
            parent.wait(timeout=0)
        if not helper_anchor.exited:
            observed = NativeIdentityReader().read(identity.pid)
            if observed.process is not None and observed.process.identity == identity:
                os.kill(identity.pid, signal.SIGKILL)
            await helper_anchor.wait(deadline, clock)
        if independent is not None:
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
