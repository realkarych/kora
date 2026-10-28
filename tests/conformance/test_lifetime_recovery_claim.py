import os
import signal
import socket
import subprocess
import sys
from contextlib import AsyncExitStack, ExitStack
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
from kora.runner.processes import ExitAnchor, RecoveredProcess
from tests.conformance.lifetime_helpers import (
    CleanupDeadline,
    RetainedHelperGroup,
    release_recovered,
    signal_recovered,
    stop_child,
    unwind_owned,
)
from tests.conformance.lifetime_support import (
    ControlledLifetimeClock,
    close_recovered_test_owner,
    dispose_test_owner,
    entered_fifo,
    limits,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["publisher", "claim"])
async def test_recovery_claim_excludes_unpublished_and_crashed_parent_writers(
    tmp_path: Path, scenario: str
) -> None:
    clock = SystemClock()
    invocation_id = "fresh"
    cleanup = CleanupDeadline(clock, 5_000_000_000)
    owned = AsyncExitStack()
    independent: RecoveredProcess | None = None

    try:
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
            owned.push_async_callback(release_recovered, independent, cleanup, clock)
        barrier = DirectoryAnchor.open_path(tmp_path)
        owned.callback(barrier.close)
        os.mkfifo("stall", 0o600, dir_fd=barrier.descriptor)
        reading, writing = os.pipe()
        owned.callback(os.close, reading)
        inherited = owned.enter_context(ExitStack())
        inherited.callback(os.close, writing)
        first, second = socket.socketpair()
        channel = BoundedChannel(first, clock)
        owned.callback(channel.close)
        inherited.callback(second.close)
        group = RetainedHelperGroup(clock)
        owned.push_async_callback(group.close, cleanup)
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
                str(group.process_group_id),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            pass_fds=(second.fileno(), writing, barrier.descriptor),
        )
        anchor = ExitAnchor(parent.pid)
        owned.push_async_callback(stop_child, parent, anchor, cleanup, clock)
        if independent is not None:
            owned.callback(signal_recovered, independent, cleanup)
        inherited.close()
        deadline = clock.monotonic_ns() + 15_000_000_000
        assert await channel.read_frame(deadline) == b"discard"
        reference = CleanupReference(
            invocation_id=(await channel.read_frame(deadline)).decode("ascii")
        )
        identity = ProcessIdentity.model_validate_json(
            await channel.read_frame(deadline)
        )
        helper_anchor = ExitAnchor(identity.pid, direct_child=False)
        owned.callback(helper_anchor.close)
        writer = await entered_fifo(barrier.descriptor, reading, deadline, clock)
        owned.callback(writer.close)
        path = next(tmp_path.rglob(reference.invocation_id)) / "journal"
        retained = path.read_bytes()
        snapshot = JournalSnapshot.model_validate_json(retained)
        assert snapshot.owner.pid == parent.pid
        if scenario == "publisher":
            assert identity not in snapshot.helpers
            assert snapshot.pending_effects
        recovery_clock = ControlledLifetimeClock()

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
        writer.close()
        await helper_anchor.wait(deadline, clock)
        published = JournalSnapshot.model_validate_json(path.read_bytes())
        recovered = ParentLifetime(
            clock=recovery_clock,
            limits=limits(),
            recovery=reference,
            launcher=InstalledIOHelperLauncher(
                home=tmp_path, state_home=tmp_path / "state"
            ),
        )
        owned.push_async_callback(dispose_test_owner, recovered, recovery_clock)
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
        await unwind_owned(owned)
