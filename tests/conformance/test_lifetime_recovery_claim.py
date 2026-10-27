import os
import signal
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from kora.conformance.journal import JournalSnapshot
from kora.conformance.lifetime import LifetimeError, ParentLifetime
from kora.protocols.identity import ProcessIdentity
from kora.protocols.lifetime import CleanupOutcome, CleanupReference
from kora.protocols.time import SystemClock
from kora.runner.channels import BoundedChannel
from kora.runner.filesystem import DirectoryAnchor
from kora.runner.identity import NativeIdentityReader
from kora.runner.processes import ExitAnchor, RecoveredProcess
from tests.conformance.lifetime_support import (
    SENT_OR_SETTLED,
    ControlledLifetimeClock,
    close_recovered_test_owner,
    dispose_test_owner,
    entered_fifo,
    independent_process,
    installed_launcher,
    journal_file,
    kill_independent,
    limits,
    retained_invocation,
    retained_journal,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["publisher", "claim"])
async def test_recovery_claim_excludes_unpublished_and_crashed_parent_writers(
    tmp_path: Path, scenario: str
) -> None:
    clock = SystemClock()
    invocation_id = "fresh"
    independent: RecoveredProcess | None = None
    if scenario == "claim":
        invocation_id = retained_invocation(tmp_path, "live").invocation_id
        _, saved = retained_journal(tmp_path, invocation_id)
        independent = independent_process(saved, clock)
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
    with await entered_fifo(barrier.descriptor, reading, deadline, clock) as writer:
        path = journal_file(tmp_path, reference.invocation_id)
        retained = path.read_bytes()
        snapshot = JournalSnapshot.model_validate_json(retained)
        assert snapshot.owner.pid == parent.pid
        if scenario == "publisher":
            assert identity not in snapshot.helpers
            assert snapshot.pending_effects
        recovery_clock = ControlledLifetimeClock()
        recovered: ParentLifetime | None = None

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
                    launcher=installed_launcher(tmp_path),
                )
                with pytest.raises(LifetimeError):
                    await competing.ready()
                refused = await competing.close(None)
                assert refused.state is CleanupOutcome.UNKNOWN
                assert path.read_bytes() == retained
            writer.close()
            await helper_anchor.wait(deadline, clock)
            published = JournalSnapshot.model_validate_json(path.read_bytes())
            recovered = ParentLifetime(
                clock=recovery_clock,
                limits=limits(),
                recovery=reference,
                launcher=installed_launcher(tmp_path),
            )
            await recovered.ready()
            report = await close_recovered_test_owner(
                recovered, recovery_clock, () if independent is None else (independent,)
            )

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
            writer.close()
            deadline = clock.monotonic_ns() + 5_000_000_000
            if parent.returncode is None:
                parent.send_signal(signal.SIGKILL)
                await anchor.wait(deadline, clock)
                parent.wait(timeout=0)
            if not helper_anchor.exited:
                observed = NativeIdentityReader().read(identity.pid)
                if (
                    observed.process is not None
                    and observed.process.identity == identity
                ):
                    os.kill(identity.pid, signal.SIGKILL)
                await helper_anchor.wait(deadline, clock)
            if independent is not None:
                await kill_independent(independent, deadline, SENT_OR_SETTLED)
            if recovered is not None:
                await dispose_test_owner(recovered, recovery_clock)
            channel.close()
            anchor.close()
            helper_anchor.close()
            os.close(reading)
            os.close(writing)
            barrier.close()
