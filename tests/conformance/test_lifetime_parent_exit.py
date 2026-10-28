import gc
import json
import os
import socket
import subprocess
import sys
from contextlib import AsyncExitStack, ExitStack
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
from kora.protocols.time import Clock, SystemClock
from kora.runner.channels import BoundedChannel, ChannelError, ChannelFailure
from kora.runner.filesystem import DirectoryAnchor
from kora.runner.identity import NativeIdentityReader
from kora.runner.processes import ExitAnchor, RecoveredProcess
from tests.conformance.lifetime_helpers import (
    CleanupDeadline,
    RetainedHelperGroup,
    release_recovered,
    signal_recovered,
    stop_child,
)
from tests.conformance.test_lifetime_cleanup_controls import (
    close_recovered_test_owner,
    dispose_test_owner,
)
from tests.conformance.test_lifetime_io_helper import entered_fifo, read_probe
from tests.conformance.test_lifetime_pending_helpers import ControlledLifetimeClock
from tests.conformance.test_parent_lifetime import limits


async def observation_closed(
    descriptor: int, cleanup: CleanupDeadline, clock: Clock
) -> None:
    deadline = cleanup()
    while await read_probe(descriptor, deadline, clock):
        assert clock.monotonic_ns() < deadline


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sink_mode", "failure_phase"),
    [
        ("drained", None),
        ("blocked", None),
        ("broken", None),
        ("drained", "fifo_barrier"),
        ("drained", "identity"),
    ],
)
async def test_stalled_reopen_helper_does_not_prevent_parent_exit_and_later_recovery(
    tmp_path: Path, sink_mode: str, failure_phase: str | None
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
    recovery_clock = ControlledLifetimeClock()
    cleanup = CleanupDeadline(clock, 5_000_000_000)
    caught: ChannelError | None = None
    identity: ProcessIdentity | None = None
    phase = "handshake"
    owned = AsyncExitStack()
    gc.collect()
    baseline_descriptors = len(os.listdir("/dev/fd"))

    try:
        reading, writing = os.pipe()
        owned.callback(os.close, reading)
        owned.push_async_callback(observation_closed, reading, cleanup, clock)
        inherited = owned.enter_context(ExitStack())
        inherited.callback(os.close, writing)
        independent = RecoveredProcess(registration, clock, NativeIdentityReader())
        owned.push_async_callback(release_recovered, independent, cleanup, clock)
        barrier = DirectoryAnchor.open_path(tmp_path)
        owned.callback(barrier.close)
        os.mkfifo("stall", 0o600, dir_fd=barrier.descriptor)
        first, second = socket.socketpair()
        channel = BoundedChannel(first, clock)
        owned.callback(channel.close)
        inherited.callback(second.close)
        diagnostic_read, diagnostic_write = os.pipe()
        inherited.callback(os.close, diagnostic_write)
        os.set_blocking(diagnostic_write, False)
        if sink_mode == "blocked":
            while True:
                try:
                    os.write(diagnostic_write, b"x" * 4096)
                except BlockingIOError:
                    break
        if sink_mode == "broken":
            os.close(diagnostic_read)
        else:
            owned.callback(os.close, diagnostic_read)
        group = RetainedHelperGroup(clock)
        owned.push_async_callback(group.close, cleanup)
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
                "reopen",
                str(group.process_group_id),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=diagnostic_write,
            close_fds=True,
            pass_fds=(second.fileno(), writing, barrier.descriptor),
        )
        anchor = ExitAnchor(parent.pid)
        owned.push_async_callback(stop_child, parent, anchor, cleanup, clock)
        owned.callback(signal_recovered, independent, cleanup)
        inherited.close()
        deadline = clock.monotonic_ns() + 10_000_000_000

        try:
            assert await channel.read_frame(deadline) == sink_mode.encode("ascii")
            assert await channel.read_frame(deadline) == reference.invocation_id.encode(
                "ascii"
            )
            if failure_phase == "identity":
                assert await read_probe(reading, deadline, clock) == b"r"
                deadline = clock.monotonic_ns()
            phase = "identity"
            identity = ProcessIdentity.model_validate_json(
                await channel.read_frame(deadline)
            )
            helper_anchor = ExitAnchor(identity.pid, direct_child=False)
            owned.callback(helper_anchor.close)
            phase = "fifo_barrier"
            if failure_phase == "fifo_barrier":
                deadline = clock.monotonic_ns()
            writer = await entered_fifo(barrier.descriptor, reading, deadline, clock)
            owned.callback(writer.close)
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
                launcher=InstalledIOHelperLauncher(
                    home=tmp_path, state_home=tmp_path / "state"
                ),
            )
            owned.push_async_callback(dispose_test_owner, recovered, recovery_clock)
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
            if not (
                failure_phase == phase and failure.reason is ChannelFailure.DEADLINE
            ):
                raise
            caught = failure
    finally:
        await owned.aclose()
    stopped = group.stopped
    exited = group.exited
    parent_status = parent.returncode
    leader_status = group.leader.returncode
    channel_closed = channel.closed
    del group, parent, anchor, channel, first, second
    gc.collect()

    assert (caught is not None) is (failure_phase is not None)
    assert parent_status is not None
    assert leader_status is not None
    assert channel_closed
    assert len(os.listdir("/dev/fd")) == baseline_descriptors
    assert (identity is None) is (failure_phase == "identity")
    assert len(stopped) == (0 if failure_phase is None else 1)
    assert exited == stopped
    assert identity is None or failure_phase is None or stopped == (identity,)
