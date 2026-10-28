import asyncio
import fcntl
import gc
import os
import signal
from contextlib import AsyncExitStack, ExitStack
from io import FileIO
from pathlib import Path

import pytest

from kora.conformance.io_helper import (
    MUTATION,
    BootstrapMutation,
    CreateTreeMutation,
    HelperError,
    IOHelper,
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
from kora.protocols.time import SystemClock
from kora.runner.channels import ChannelError, ChannelFailure
from kora.runner.filesystem import DirectoryAnchor
from kora.runner.identity import IdentityState, NativeIdentityReader
from kora.runner.processes import ExitAnchor
from tests.conformance.lifetime_helpers import (
    CleanupDeadline,
    ProbeIOHelperLauncher,
    join_helper_operation,
    stop_child,
    stop_io_helper,
    unwind_owned,
)
from tests.conformance.lifetime_support import (
    entered_fifo,
    installed_launcher,
    open_fifo_writer,
    stall_barrier,
)
from tests.measurements.lifetime_measurement_support import MeasuredIOHelperLauncher
from tests.support.event_loop import turn


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


async def exercise_helper_claim_finalizers(
    tmp_path: Path,
    effect: str,
    failure_phase: str | None,
    helpers: list[IOHelper],
    waiting: asyncio.Event | None = None,
) -> None:
    clock = SystemClock()
    cleanup = CleanupDeadline(clock, 5_000_000_000)
    deadline = clock.monotonic_ns() + 10_000_000_000
    owned = AsyncExitStack()
    try:
        resources = owned.enter_context(ExitStack())
        barrier = stall_barrier(resources, tmp_path)
        launcher = barrier.launcher(
            phase="both" if effect == "failed_reply" else "after"
        )
        inherited: tuple[int, ...] = ()
        claim: FileIO | None = None
        competitor: FileIO | None = None
        if effect == "bootstrap":
            mutation = BootstrapMutation(
                invocation_id="1" * 32, journal=initial_snapshot(), capacity=32
            )
        else:
            claim = resources.enter_context(
                FileIO(open_claim(barrier.directory, "claim"), "r+b")
            )
            mutation_directory = os.dup(barrier.directory.descriptor)
            resources.callback(os.close, mutation_directory)
            inherited = (mutation_directory, claim.fileno())
            if effect == "refused":
                os.mkdir("2" * 32, 0o700, dir_fd=barrier.directory.descriptor)
            mutation = CreateTreeMutation(
                directory=barrier.directory.identity, name="2" * 32
            )
        helper = launcher.start(inherited, clock)
        helpers.append(helper)
        owned.push_async_callback(stop_io_helper, helper, cleanup)
        await helper.channel.write_record(MUTATION.dump_json(mutation), deadline)
        if effect == "failed_reply":
            entered = (
                clock.monotonic_ns() if failure_phase == "failed_reply" else deadline
            )
            if waiting is not None:
                waiting.set()
            with await barrier.entered(entered, clock):
                helper.channel.close()
        else:
            result = MutationResult.model_validate_json(
                await helper.channel.read_record(deadline, 1048576)
            )
            if effect == "lost_tree_transfer":
                assert result.descriptor_count == 1
                helper.channel.close()
            elif result.descriptor_count:
                await helper.receive_descriptors(result.descriptor_count, deadline)
                with ExitStack() as received:
                    descriptors = helper.take_descriptors()
                    for descriptor in descriptors:
                        received.callback(os.close, descriptor)
                    if effect == "bootstrap":
                        _root_fd, invocation_fd, _received_claim = descriptors
                        competitor = resources.enter_context(
                            FileIO(
                                os.open(
                                    "claim",
                                    os.O_RDWR | os.O_NOFOLLOW,
                                    dir_fd=invocation_fd,
                                ),
                                "r+b",
                            )
                        )
        if effect != "bootstrap":
            competitor = resources.enter_context(
                FileIO(
                    os.open(
                        "claim",
                        os.O_RDWR | os.O_NOFOLLOW,
                        dir_fd=barrier.directory.descriptor,
                    ),
                    "r+b",
                )
            )
        if claim is not None:
            claim.close()
        entered = clock.monotonic_ns() if failure_phase == "finalizer" else deadline
        if waiting is not None:
            waiting.set()
        writer = await barrier.entered(entered, clock)
        owned.callback(writer.close)
        assert competitor is not None

        with pytest.raises(BlockingIOError):
            fcntl.flock(competitor.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert not helper.anchor.exited
        writer.close()
        assert await helper.settle(deadline)
        fcntl.flock(competitor.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        assert helper.closed
    finally:
        await unwind_owned(owned)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "effect", ["bootstrap", "tree", "refused", "failed_reply", "lost_tree_transfer"]
)
async def test_fixed_helper_claim_survives_result_error_and_transfer_finalizers(
    tmp_path: Path, effect: str
) -> None:
    await exercise_helper_claim_finalizers(tmp_path, effect, None, [])


async def assert_helper_barrier_failure(tmp_path: Path, phase: str) -> None:
    gc.collect()
    baseline = set(os.listdir("/dev/fd"))
    helpers: list[IOHelper] = []
    clock = SystemClock()
    cleanup = CleanupDeadline(clock, 5_000_000_000)
    try:
        scenario = (
            exercise_helper_fifo_lease(tmp_path, "writer", True, helpers)
            if phase == "pending_execute"
            else exercise_helper_claim_finalizers(
                tmp_path,
                "failed_reply" if phase == "failed_reply" else "tree",
                phase,
                helpers,
            )
        )
        with pytest.raises(ChannelError) as failure:
            await scenario

        assert failure.value.reason is ChannelFailure.DEADLINE
        assert len(helpers) == 1
        helper = helpers[0]
        assert helper.closed
        assert helper.anchor.exited
        assert helper.process.returncode is not None
        reader = NativeIdentityReader()
        assert reader.read(helper.process.pid).state is IdentityState.ABSENT
        group = reader.group(helper.process.pid, cleanup())
        assert group.state is IdentityState.PRESENT
        assert group.members == ()
        del failure
        gc.collect()
        assert set(os.listdir("/dev/fd")) == baseline
    finally:
        for helper in helpers:
            await stop_io_helper(helper, cleanup)
    del helpers, helper
    gc.collect()

    assert set(os.listdir("/dev/fd")) == baseline


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["failed_reply", "finalizer", "pending_execute"])
async def test_io_helper_barrier_failure_releases_acquired_resources(
    tmp_path: Path, phase: str
) -> None:
    await assert_helper_barrier_failure(tmp_path, phase)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["failed_reply", "finalizer", "pending_execute"])
async def test_io_helper_barrier_cancellation_releases_acquired_resources(
    tmp_path: Path, phase: str
) -> None:
    gc.collect()
    baseline = set(os.listdir("/dev/fd"))
    helpers: list[IOHelper] = []
    waiting = asyncio.Event()
    clock = SystemClock()
    cleanup = CleanupDeadline(clock, 5_000_000_000)
    scenario = (
        exercise_helper_fifo_lease(tmp_path, "writer", False, helpers, waiting)
        if phase == "pending_execute"
        else exercise_helper_claim_finalizers(
            tmp_path,
            "failed_reply" if phase == "failed_reply" else "tree",
            None,
            helpers,
            waiting,
        )
    )
    pending = asyncio.create_task(scenario)
    try:
        await asyncio.wait_for(waiting.wait(), 5)
        assert not pending.done()

        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending

        assert len(helpers) == 1
        helper = helpers[0]
        assert helper.closed
        assert helper.anchor.exited
        assert helper.process.returncode is not None
        reader = NativeIdentityReader()
        assert reader.read(helper.process.pid).state is IdentityState.ABSENT
        group = reader.group(helper.process.pid, cleanup())
        assert group.state is IdentityState.PRESENT
        assert group.members == ()
        gc.collect()
        assert set(os.listdir("/dev/fd")) == baseline
    finally:
        try:
            await join_helper_operation(pending)
        finally:
            for helper in helpers:
                await stop_io_helper(helper, cleanup)
    del helpers, helper, pending, scenario
    gc.collect()

    assert set(os.listdir("/dev/fd")) == baseline


@pytest.mark.asyncio
@pytest.mark.parametrize("implementation", ["installed", "probe", "measured"])
async def test_fixed_io_helper_shares_mutation_claim_and_lifecycle_contract(
    tmp_path: Path, implementation: str
) -> None:
    clock = SystemClock()
    reading, writing = os.pipe()
    launcher: IOHelperLauncher = (
        installed_launcher(tmp_path)
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


async def exercise_helper_fifo_lease(
    tmp_path: Path,
    completion: str,
    fail_barrier: bool,
    helpers: list[IOHelper],
    waiting: asyncio.Event | None = None,
) -> None:
    clock = SystemClock()
    cleanup = CleanupDeadline(clock, 5_000_000_000)
    owned = AsyncExitStack()
    try:
        resources = owned.enter_context(ExitStack())
        barrier = stall_barrier(resources, tmp_path)
        directory = barrier.directory
        claim = resources.enter_context(FileIO(open_claim(directory, "claim"), "r+b"))
        competitor = resources.enter_context(
            FileIO(
                os.open(
                    "claim", os.O_RDWR | os.O_NOFOLLOW, dir_fd=directory.descriptor
                ),
                "r+b",
            )
        )
        helper = barrier.launcher().start((directory.descriptor, claim.fileno()), clock)
        helpers.append(helper)
        owned.push_async_callback(stop_io_helper, helper, cleanup)
        deadline = clock.monotonic_ns() + 5_000_000_000
        operation = asyncio.create_task(
            helper.execute(
                CreateTreeMutation(directory=directory.identity, name="3" * 32),
                deadline,
            )
        )
        observed_errors: list[BaseException] = []
        owned.push_async_callback(join_helper_operation, operation, observed_errors)
        entered = clock.monotonic_ns() if fail_barrier else deadline
        if waiting is not None:
            waiting.set()
        writer = await barrier.entered(entered, clock)
        owned.callback(writer.close)
        claim.close()

        with pytest.raises(BlockingIOError):
            fcntl.flock(competitor.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert not operation.done()
        if completion == "writer":
            writer.close()
            result = await operation
            assert result.directory is not None
            assert result.descriptor_count == 1
            (transferred,) = helper.take_descriptors()
            os.close(transferred)
        else:
            assert helper.signal(signal.SIGKILL)
            with pytest.raises(ChannelError) as failure:
                await operation
            observed_errors.append(failure.value)
            assert await helper.settle(deadline)
        assert helper.closed
        writer.close()
        fcntl.flock(competitor.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        await unwind_owned(owned)


@pytest.mark.asyncio
@pytest.mark.parametrize("completion", ["writer", "kill"])
async def test_fixed_io_helper_retains_claim_while_fifo_lease_blocks_progress(
    tmp_path: Path, completion: str
) -> None:
    await exercise_helper_fifo_lease(tmp_path, completion, False, [])


@pytest.mark.asyncio
async def test_fifo_lease_closes_writer_when_reader_acknowledgment_fails(
    tmp_path: Path,
) -> None:
    clock = SystemClock()
    with ExitStack() as resources:
        directory = DirectoryAnchor.open_path(tmp_path)
        resources.callback(directory.close)
        os.mkfifo("stall", 0o600, dir_fd=directory.descriptor)
        reader = os.open(
            "stall", os.O_RDONLY | os.O_NONBLOCK, dir_fd=directory.descriptor
        )
        resources.callback(os.close, reader)
        reading, writing = os.pipe()
        resources.callback(os.close, reading)
        os.write(writing, b"r")
        os.close(writing)
        deadline = clock.monotonic_ns() + 5_000_000_000

        with pytest.raises(AssertionError):
            await entered_fifo(directory.descriptor, reading, deadline, clock)

        assert os.read(reader, 1) == b""


@pytest.mark.asyncio
async def test_fifo_writer_waits_for_a_real_reader(tmp_path: Path) -> None:
    clock = SystemClock()
    deadline = clock.monotonic_ns() + 5_000_000_000
    barrier = DirectoryAnchor.open_path(tmp_path)
    with ExitStack() as resources:
        resources.callback(barrier.close)
        os.mkfifo("stall", 0o600, dir_fd=barrier.descriptor)
        async with asyncio.TaskGroup() as tasks:
            pending = tasks.create_task(
                open_fifo_writer(barrier.descriptor, deadline, clock)
            )
            await turn()
            assert not pending.done()

            reading = os.open(
                "stall", os.O_RDONLY | os.O_NONBLOCK, dir_fd=barrier.descriptor
            )
            resources.callback(os.close, reading)
            writer = await pending
            resources.callback(os.close, writer)

            os.write(writer, b"ready")
            assert os.read(reading, 5) == b"ready"


@pytest.mark.asyncio
async def test_fifo_writer_refuses_an_expired_deadline(tmp_path: Path) -> None:
    clock = SystemClock()
    barrier = DirectoryAnchor.open_path(tmp_path)

    with ExitStack() as resources:
        resources.callback(barrier.close)
        os.mkfifo("stall", 0o600, dir_fd=barrier.descriptor)

        with pytest.raises(TimeoutError, match="FIFO reader"):
            await open_fifo_writer(barrier.descriptor, clock.monotonic_ns(), clock)


@pytest.mark.asyncio
async def test_completed_helper_task_failure_keeps_remaining_cleanup(
    tmp_path: Path,
) -> None:
    gc.collect()
    baseline = set(os.listdir("/dev/fd"))
    clock = SystemClock()
    cleanup = CleanupDeadline(clock, 5_000_000_000)
    entered = asyncio.Event()
    owned = AsyncExitStack()
    resources = owned.enter_context(ExitStack())
    barrier = stall_barrier(resources, tmp_path)
    helper = barrier.launcher().start((), clock)
    owned.push_async_callback(stop_io_helper, helper, cleanup)

    async def fail() -> None:
        entered.set()
        raise ValueError("helper task failed")

    pending = asyncio.create_task(fail())
    owned.push_async_callback(join_helper_operation, pending)

    async def failed_body() -> None:
        try:
            raise ChannelError(ChannelFailure.DEADLINE)
        finally:
            await unwind_owned(owned)

    try:
        await entered.wait()
        assert pending.done()

        with pytest.raises(ValueError, match="helper task failed") as failure:
            await failed_body()

        assert isinstance(failure.value.__context__, ChannelError)
        assert helper.closed
        assert helper.process.returncode is not None
        del failure
        gc.collect()
        assert set(os.listdir("/dev/fd")) == baseline
    finally:
        try:
            pending.exception()
        finally:
            await stop_io_helper(helper, cleanup)
            await unwind_owned(owned)
    del helper, pending
    gc.collect()

    assert set(os.listdir("/dev/fd")) == baseline


@pytest.mark.asyncio
async def test_helper_cleanup_closes_observation_handles_after_signal_refusal(
    tmp_path: Path,
) -> None:
    gc.collect()
    original_descriptors = set(os.listdir("/dev/fd"))
    clock = SystemClock()
    cleanup = CleanupDeadline(clock, 5_000_000_000)
    cleanup.value = clock.monotonic_ns() - 1
    with ExitStack() as resources:
        barrier = stall_barrier(resources, tmp_path)
        gc.collect()
        baseline = set(os.listdir("/dev/fd"))
        helper = barrier.launcher().start((), clock)
        try:
            with pytest.raises(AssertionError):
                await stop_io_helper(helper, cleanup)

            assert not helper.closed
            assert helper.channel.closed
            assert helper.anchor.readiness.cancelled()
            gc.collect()
            assert set(os.listdir("/dev/fd")) == baseline
        finally:
            anchor = ExitAnchor(helper.process.pid)
            await stop_child(
                helper.process, anchor, CleanupDeadline(clock, 5_000_000_000), clock
            )
            helper.channel.close()
            helper.anchor.close()
        del helper
        gc.collect()

    assert set(os.listdir("/dev/fd")) == original_descriptors
