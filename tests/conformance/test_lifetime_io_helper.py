import asyncio
import fcntl
import os
import signal
from pathlib import Path

import pytest

from kora.conformance.io_helper import (
    MUTATION,
    BootstrapMutation,
    CreateTreeMutation,
    HelperError,
    InstalledIOHelperLauncher,
    IOHelperLauncher,
    MutationResult,
    OpenTreeMutation,
    PublishMutation,
    RemoveTreeMutation,
    open_claim,
    read_journal,
)
from kora.conformance.journal import JournalSnapshot
from kora.protocols.lifetime import LifetimeReason, OriginalOutcome
from kora.protocols.time import Clock, SystemClock
from kora.runner.channels import DescriptorReadiness, wait_future
from kora.runner.filesystem import DirectoryAnchor
from kora.runner.identity import NativeIdentityReader
from tests.conformance.lifetime_helpers import ProbeIOHelperLauncher
from tests.measurements.lifetime_measurement_support import MeasuredIOHelperLauncher


def initial_snapshot() -> JournalSnapshot:
    observed = NativeIdentityReader().read(os.getpid())
    assert observed.process is not None
    return JournalSnapshot(
        invocation_id="1" * 32,
        owner=observed.process.identity,
        generation=1,
        revision=1,
        admission_open=True,
        original_outcome=OriginalOutcome.COMPLETE,
    )


async def read_probe(descriptor: int, deadline: int, clock: Clock) -> bytes:
    readiness = DescriptorReadiness(descriptor, writing=False)
    try:
        await wait_future(readiness.ready, deadline, clock)
        return os.read(descriptor, 1)
    finally:
        readiness.close()


async def entered_fifo(pid: int, observation: int, deadline: int, clock: Clock) -> None:
    assert await read_probe(observation, deadline, clock) == b"r"
    while True:
        os.kill(pid, signal.SIGUSR1)
        response = await read_probe(observation, deadline, clock)
        assert response in (b"e", b"n")
        if response == b"e":
            return


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "effect", ["bootstrap", "tree", "refused", "failed_reply", "lost_tree_transfer"]
)
async def test_fixed_helper_claim_survives_result_error_and_transfer_finalizers(
    tmp_path: Path, effect: str
) -> None:
    clock = SystemClock()
    deadline = clock.monotonic_ns() + 10_000_000_000
    barrier = DirectoryAnchor.open_path(tmp_path)
    fifo_directory = os.dup(barrier.descriptor)
    os.mkfifo("stall", 0o600, dir_fd=fifo_directory)
    reading, writing = os.pipe()
    launcher = ProbeIOHelperLauncher(
        tmp_path,
        writing,
        fifo_directory,
        "both" if effect == "failed_reply" else "after",
    )
    inherited: tuple[int, ...] = ()
    claim = None
    competitor = None
    if effect == "bootstrap":
        mutation = BootstrapMutation(
            invocation_id="1" * 32, journal=initial_snapshot(), capacity=32
        )
    else:
        claim = open_claim(barrier, "claim")
        inherited = (barrier.descriptor, claim)
        if effect == "refused":
            os.mkdir("2" * 32, 0o700, dir_fd=barrier.descriptor)
        mutation = CreateTreeMutation(directory=barrier.identity, name="2" * 32)
    helper = launcher.start(inherited, clock)
    await helper.channel.write_record(MUTATION.dump_json(mutation), deadline)
    if effect == "failed_reply":
        await entered_fifo(helper.process.pid, reading, deadline, clock)
        helper.channel.close()
        writer = os.open("stall", os.O_WRONLY | os.O_NONBLOCK, dir_fd=fifo_directory)
        os.close(writer)
    else:
        result = MutationResult.model_validate_json(
            await helper.channel.read_record(deadline, 1048576)
        )
        if effect == "lost_tree_transfer":
            assert result.descriptor_count == 1
            helper.channel.close()
        elif result.descriptor_count:
            await helper.receive_descriptors(result.descriptor_count, deadline)
            received = helper.take_descriptors()
            if effect == "bootstrap":
                _root_fd, invocation_fd, _received_claim = received
                competitor = os.open(
                    "claim", os.O_RDWR | os.O_NOFOLLOW, dir_fd=invocation_fd
                )
            for descriptor in received:
                os.close(descriptor)
    if effect != "bootstrap":
        competitor = os.open(
            "claim", os.O_RDWR | os.O_NOFOLLOW, dir_fd=barrier.descriptor
        )
    if claim is not None:
        os.close(claim)
    await entered_fifo(helper.process.pid, reading, deadline, clock)

    assert competitor is not None
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert not helper.anchor.exited
        writer = os.open("stall", os.O_WRONLY | os.O_NONBLOCK, dir_fd=fifo_directory)
        os.close(writer)
        assert await helper.settle(deadline)
        fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)

        assert helper.closed
    finally:
        if not helper.closed:
            helper.signal(signal.SIGKILL)
            assert await helper.settle(deadline)
        for descriptor in (competitor, reading, writing, fifo_directory):
            os.close(descriptor)
        barrier.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("implementation", ["installed", "probe", "measured"])
async def test_fixed_io_helper_shares_mutation_claim_and_lifecycle_contract(
    tmp_path: Path, implementation: str
) -> None:
    clock = SystemClock()
    reading, writing = os.pipe()
    launcher: IOHelperLauncher = (
        InstalledIOHelperLauncher(home=tmp_path, state_home=tmp_path / "state")
        if implementation == "installed"
        else MeasuredIOHelperLauncher(tmp_path, writing)
        if implementation == "measured"
        else ProbeIOHelperLauncher(tmp_path, writing)
    )
    deadline = clock.monotonic_ns() + 10_000_000_000
    helper = launcher.start((), clock)

    result = await helper.execute(
        BootstrapMutation(
            invocation_id="1" * 32, journal=initial_snapshot(), capacity=32
        ),
        deadline,
    )

    assert result.journal is not None
    assert result.directory is not None
    assert result.root is not None
    assert helper.closed
    root_fd, invocation_fd, claim = helper.take_descriptors()
    root = DirectoryAnchor(root_fd, result.root)
    invocation = DirectoryAnchor(invocation_fd, result.directory)
    competitor = os.open("claim", os.O_RDWR | os.O_NOFOLLOW, dir_fd=invocation_fd)
    with pytest.raises(BlockingIOError):
        fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.close(competitor)
    publication = launcher.start((invocation_fd, claim), clock)
    snapshot = result.journal.model_copy(update={"revision": 2})
    await publication.execute(
        PublishMutation(directory=invocation.identity, revision=2, journal=snapshot),
        deadline,
    )
    assert publication.identity is not None
    expected = snapshot.model_copy(update={"helpers": (publication.identity,)})
    assert read_journal(invocation) == expected
    stale = launcher.start((invocation_fd, claim), clock)
    with pytest.raises(HelperError) as refused:
        await stale.execute(
            PublishMutation(
                directory=invocation.identity, revision=1, journal=result.journal
            ),
            deadline,
        )
    assert refused.value.reason is LifetimeReason.JOURNAL_UNAVAILABLE
    assert read_journal(invocation) == expected
    tree = launcher.start((invocation_fd, claim), clock)
    created = await tree.execute(
        CreateTreeMutation(directory=invocation.identity, name="2" * 32), deadline
    )
    assert created.directory is not None
    assert created.descriptor_count == 1
    (child_descriptor,) = tree.take_descriptors()
    child = DirectoryAnchor(child_descriptor, created.directory)
    child.verify()
    child.close()
    opener = launcher.start((invocation_fd, claim), clock)
    opened = await opener.execute(
        OpenTreeMutation(directory=invocation.identity, target=created.directory),
        deadline,
    )
    assert opened.directory == created.directory
    assert opened.descriptor_count == 1
    (reopened_descriptor,) = opener.take_descriptors()
    reopened = DirectoryAnchor(reopened_descriptor, created.directory)
    reopened.verify()
    reopened.close()
    removal = launcher.start((invocation_fd, claim), clock)
    await removal.execute(
        RemoveTreeMutation(directory=invocation.identity, target=created.directory),
        deadline,
    )
    with pytest.raises(FileNotFoundError):
        os.stat("2" * 32, dir_fd=invocation_fd, follow_symlinks=False)
    os.close(claim)
    invocation.close()
    root.close()
    os.close(reading)
    os.close(writing)


@pytest.mark.asyncio
@pytest.mark.parametrize("completion", ["writer", "kill"])
async def test_fixed_io_helper_acknowledges_actual_fifo_call_and_retains_claim(
    tmp_path: Path, completion: str
) -> None:
    clock = SystemClock()
    directory = DirectoryAnchor.open_path(tmp_path)
    claim = open_claim(directory, "claim")
    competitor = os.open(
        "claim", os.O_RDWR | os.O_NOFOLLOW, dir_fd=directory.descriptor
    )
    os.mkfifo("stall", 0o600, dir_fd=directory.descriptor)
    reading, writing = os.pipe()
    launcher = ProbeIOHelperLauncher(tmp_path, writing, directory.descriptor)
    helper = launcher.start((directory.descriptor, claim), clock)
    deadline = clock.monotonic_ns() + 5_000_000_000
    operation = asyncio.create_task(
        helper.execute(
            CreateTreeMutation(directory=directory.identity, name="3" * 32), deadline
        )
    )
    assert await read_probe(reading, deadline, clock) == b"r"

    entered = False
    while not entered:
        os.kill(helper.process.pid, signal.SIGUSR1)
        response = await read_probe(reading, deadline, clock)
        assert response in (b"e", b"n")
        entered = response == b"e"
    os.close(claim)

    with pytest.raises(BlockingIOError):
        fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert not operation.done()
    if completion == "writer":
        writer = os.open(
            "stall", os.O_WRONLY | os.O_NONBLOCK, dir_fd=directory.descriptor
        )
        os.close(writer)
        result = await operation
        assert result.directory is not None
        assert result.descriptor_count == 1
        (transferred,) = helper.take_descriptors()
        os.close(transferred)
    else:
        assert helper.signal(signal.SIGKILL)
        from kora.runner.channels import ChannelError

        with pytest.raises(ChannelError):
            await operation
        assert await helper.settle(deadline)
    assert helper.closed
    fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.close(competitor)
    os.close(reading)
    os.close(writing)
    directory.close()
