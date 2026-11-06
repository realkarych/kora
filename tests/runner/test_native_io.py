import asyncio
import errno
import os
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from kora.protocols.errors import KoraProtocolError
from kora.runner.native_io import (
    BorrowedInvocation,
    NativeExecutor,
    NativeIOBusy,
    NativeIONotInvoked,
    NativeIOSubmissionUncertain,
    NativeIOUnresolved,
    NativeOperation,
)
from kora.runner.registry import TransitionExecutor

pytestmark = pytest.mark.asyncio


async def reached(event: threading.Event) -> None:
    assert await asyncio.to_thread(event.wait, 5)


class FileOperation:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.opened = threading.Event()
        self.return_allowed = threading.Event()
        self.cleaning = threading.Event()
        self.close_allowed = threading.Event()
        self.descriptor: int | None = None
        self.worker: threading.Thread | None = None
        self.cleaner: threading.Thread | None = None
        self.closes = 0

    def acquire(self) -> int:
        self.worker = threading.current_thread()
        self.descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        self.opened.set()
        if not self.return_allowed.wait(5):
            os.close(self.descriptor)
            raise TimeoutError("test operation release")
        return self.descriptor

    def release(self, descriptor: int) -> None:
        self.cleaner = threading.current_thread()
        self.cleaning.set()
        if not self.close_allowed.wait(5):
            raise TimeoutError("test cleanup release")
        os.close(descriptor)
        self.closes += 1


async def test_native_cleanup_runs_off_loop_and_retains_admission(
    tmp_path: Path,
) -> None:
    executor = TransitionExecutor(workers=1, admitted=1)
    resource = FileOperation(tmp_path / "owned")
    deferred = threading.Event()
    caller = asyncio.create_task(
        executor.run(
            resource.acquire,
            resource.release,
            after_uncertain_submission=deferred.set,
        )
    )
    await reached(resource.opened)

    try:
        caller.cancel()
        resource.return_allowed.set()
        await reached(resource.cleaning)
        with pytest.raises(KoraProtocolError) as refused:
            await executor.run(lambda: None)
        assert refused.value.code == "registry_busy"
        assert len(executor.jobs) == 1
        assert resource.descriptor is not None
        os.fstat(resource.descriptor)
        assert resource.cleaner is resource.worker
        assert resource.cleaner is not threading.current_thread()
    finally:
        resource.return_allowed.set()
        resource.close_allowed.set()
        await asyncio.gather(caller, return_exceptions=True)
        await executor.close()

    assert caller.cancelled()
    assert resource.closes == 1
    assert executor.jobs == set()
    assert resource.worker is not None
    assert not resource.worker.is_alive()
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(resource.descriptor)
    assert not deferred.is_set()


async def test_native_close_waits_for_cancelled_cleanup(tmp_path: Path) -> None:
    executor = TransitionExecutor(workers=1, admitted=1)
    resource = FileOperation(tmp_path / "owned")
    caller = asyncio.create_task(executor.run(resource.acquire, resource.release))
    await reached(resource.opened)
    entered = asyncio.Event()

    async def close() -> None:
        entered.set()
        await executor.close()

    caller.cancel()
    resource.return_allowed.set()
    await reached(resource.cleaning)
    closing = asyncio.create_task(close())
    await entered.wait()
    try:
        caller.cancel()
        closing.cancel()
        with pytest.raises(KoraProtocolError) as refused:
            await executor.run(lambda: None)
        assert refused.value.code == "runtime_closed"
        assert not caller.done()
        assert not closing.done()
        assert len(executor.jobs) == 1
    finally:
        resource.return_allowed.set()
        resource.close_allowed.set()
        results = await asyncio.gather(caller, closing, return_exceptions=True)
        await executor.close()

    assert all(isinstance(value, asyncio.CancelledError) for value in results)
    assert resource.closes == 1
    assert executor.jobs == set()
    assert resource.worker is not None
    assert not resource.worker.is_alive()


async def test_native_single_slot_has_no_waiting_job() -> None:
    executor = NativeExecutor(workers=1, admitted=1)
    started = threading.Event()
    released = threading.Event()
    second_executed = threading.Event()

    def hold() -> bool:
        started.set()
        return released.wait(5)

    caller = asyncio.create_task(executor.run(hold))
    await reached(started)
    try:
        with pytest.raises(NativeIOBusy):
            await executor.run(second_executed.set)
        assert not second_executed.is_set()
        assert len(executor.jobs) == 1
    finally:
        released.set()
        result = await caller
        await executor.close()

    assert result
    assert not second_executed.is_set()
    assert executor.jobs == set()


async def test_native_waiter_cannot_overtake_cancelled_cleanup(tmp_path: Path) -> None:
    executor = NativeExecutor(workers=1, admitted=2)
    resource = FileOperation(tmp_path / "owned")
    waiting = asyncio.Event()
    executed = threading.Event()

    async def second() -> None:
        waiting.set()
        await executor.run(executed.set)

    first = asyncio.create_task(executor.run(resource.acquire, resource.release))
    await reached(resource.opened)
    pending = asyncio.create_task(second())
    await waiting.wait()

    try:
        first.cancel()
        resource.return_allowed.set()
        await reached(resource.cleaning)
        assert not executed.is_set()
        assert len(executor.jobs) == 2
    finally:
        resource.return_allowed.set()
        resource.close_allowed.set()
        await asyncio.gather(first, pending, return_exceptions=True)
        await executor.close()

    assert first.cancelled()
    assert executed.is_set()
    assert resource.closes == 1
    assert executor.jobs == set()


async def test_native_cancelled_waiters_keep_fifo_and_nonwaiting_refuses() -> None:
    executor = NativeExecutor(workers=1, admitted=4)
    entered = threading.Event()
    release = threading.Event()
    admitted = [asyncio.Event(), asyncio.Event()]
    order: list[int] = []

    def hold() -> bool:
        entered.set()
        return release.wait(5)

    def execute(index: int) -> int:
        order.append(index)
        return index

    def cleanup(index: int) -> None:
        order.append(index + 10)

    async def waiter(index: int) -> int:
        admitted[index].set()
        return await executor.run(lambda: execute(index), cleanup)

    first = asyncio.create_task(executor.run(hold))
    await reached(entered)
    second = asyncio.create_task(waiter(0))
    await admitted[0].wait()
    third = asyncio.create_task(waiter(1))
    await admitted[1].wait()

    try:
        second.cancel()
        with pytest.raises(NativeIOBusy):
            await executor.run_nowait(lambda: execute(2))
        assert len(executor.jobs) == 3
    finally:
        release.set()
        results = await asyncio.gather(first, second, third, return_exceptions=True)
        await executor.close()

    assert results[0] is True
    assert isinstance(results[1], asyncio.CancelledError)
    assert results[2] == 1
    assert order == [0, 10, 1]
    assert executor.jobs == set()


async def test_native_waiting_work_survives_global_shutdown(tmp_path: Path) -> None:
    resource = FileOperation(tmp_path / "owned")
    shutdown = threading.Event()
    owners: list[NativeExecutor] = []
    completed_waiter = threading.Event()
    cleaned_waiter = threading.Event()

    async def inner() -> None:
        executor = NativeExecutor(workers=1, admitted=2)
        owners.append(executor)
        pending_entered = asyncio.Event()
        probe_entered = asyncio.Event()
        close_entered = asyncio.Event()

        async def close() -> None:
            close_entered.set()
            await executor.close()

        async def pending() -> None:
            pending_entered.set()
            await executor.run(completed_waiter.set, lambda value: cleaned_waiter.set())

        async def probe() -> None:
            probe_entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                shutdown.set()

        tasks: list[asyncio.Task[object]] = [asyncio.create_task(probe())]
        await probe_entered.wait()
        tasks.append(
            asyncio.create_task(executor.run(resource.acquire, resource.release))
        )
        await reached(resource.opened)
        tasks.append(asyncio.create_task(pending()))
        await pending_entered.wait()
        tasks.append(asyncio.create_task(close()))
        await close_entered.wait()
        assert all(not task.done() for task in tasks)

    parent = asyncio.create_task(asyncio.to_thread(asyncio.run, inner()))

    try:
        await reached(shutdown)
        assert not completed_waiter.is_set()
        resource.return_allowed.set()
        await reached(resource.cleaning)
        assert not completed_waiter.is_set()
        resource.close_allowed.set()
        await parent
        assert resource.worker is not None
        joined = not resource.worker.is_alive()
    finally:
        resource.return_allowed.set()
        resource.close_allowed.set()
        await parent
        for owner in owners:
            await asyncio.to_thread(owner.executor.shutdown, wait=True)

    assert completed_waiter.is_set()
    assert joined
    assert cleaned_waiter.is_set()
    assert resource.closes == 1
    assert owners[0].jobs == set()
    assert resource.worker is not None
    assert not resource.worker.is_alive()


async def test_native_worker_loss_does_not_release_unresolved_capacity() -> None:
    executor = NativeExecutor(workers=1, admitted=2)
    started = threading.Event()
    released = threading.Event()
    queued = asyncio.Event()
    second_executed = threading.Event()
    deferred = threading.Event()

    def hold() -> bool:
        started.set()
        return released.wait(5)

    async def second() -> None:
        queued.set()
        await executor.run(second_executed.set, after_uncertain_submission=deferred.set)

    first = asyncio.create_task(executor.run(hold))
    await reached(started)
    pending = asyncio.create_task(second())
    await queued.wait()
    try:
        executor.executor.shutdown(wait=False, cancel_futures=True)
        with pytest.raises(NativeIONotInvoked):
            await pending
        with pytest.raises(NativeIONotInvoked):
            await executor.run(
                second_executed.set, after_uncertain_submission=deferred.set
            )
        assert len(executor.jobs) == 2
        assert not second_executed.is_set()
    finally:
        released.set()
        await first
        with pytest.raises(NativeIOUnresolved):
            await executor.close()

    assert len(executor.jobs) == 1
    assert not second_executed.is_set()
    assert not deferred.is_set()


@pytest.mark.parametrize("cleanup_raises", [False, True])
async def test_uncertain_submission_cleanup_follows_actual_pool_join(
    tmp_path: Path, cleanup_raises: bool
) -> None:
    executor = TransitionExecutor(workers=2, admitted=2)
    descriptor = os.open(tmp_path / "borrowed", os.O_RDWR | os.O_CREAT, 0o600)
    loop_thread = threading.current_thread()
    held = threading.Event()
    release = threading.Event()
    invoked = threading.Event()
    close_started = asyncio.Event()
    cleanup_observations: list[tuple[bool, bool, int]] = []
    workers: list[threading.Thread] = []

    def hold() -> None:
        workers.append(threading.current_thread())
        held.set()
        if not release.wait(5):
            raise TimeoutError("test worker release")
        os.fstat(descriptor)

    def cleanup() -> None:
        cleanup_observations.append(
            (
                workers[0].is_alive(),
                threading.current_thread() is loop_thread,
                os.fstat(descriptor).st_ino,
            )
        )
        os.close(descriptor)
        if cleanup_raises:
            raise asyncio.CancelledError

    async def close() -> None:
        close_started.set()
        with pytest.raises(NativeIOUnresolved):
            await executor.close()

    inode = os.fstat(descriptor).st_ino
    first = asyncio.create_task(executor.run(hold))
    await reached(held)

    try:
        executor.executor.shutdown(wait=False)
        with pytest.raises(NativeIOSubmissionUncertain):
            await executor.run(invoked.set, after_uncertain_submission=cleanup)
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(close())
            tasks.create_task(close())
            await close_started.wait()
            alive_during_close = workers[0].is_alive()
            inode_during_close = os.fstat(descriptor).st_ino
            observations_before_join = tuple(cleanup_observations)
            release.set()
        await first
        await close()
        with pytest.raises(OSError, match=os.strerror(errno.EBADF)) as closed:
            os.fstat(descriptor)
    finally:
        release.set()
        await first
        await close()
        if not cleanup_observations:
            os.close(descriptor)

    assert alive_during_close
    assert inode_during_close == inode
    assert observations_before_join == ()
    assert not invoked.is_set()
    assert cleanup_observations == [(False, False, inode)]
    assert closed.value.errno == errno.EBADF
    assert len(executor.jobs) == 1


class ObservedJoinThread(threading.Thread):
    def __init__(self, target: Callable[[], None]) -> None:
        super().__init__(target=target)
        self.joined = threading.Event()

    def join(self, timeout: float | None = None) -> None:
        self.joined.set()
        super().join(timeout)


async def test_deferred_cleanup_joins_borrower_outside_pool(tmp_path: Path) -> None:
    borrowed = tmp_path / "borrowed"
    descriptor = os.open(borrowed, os.O_RDWR | os.O_CREAT, 0o600)
    returned = threading.Event()
    release_thread = threading.Event()
    closed = threading.Event()
    owner = NativeOperation(borrower=BorrowedInvocation())

    def native_thread() -> None:
        owner.invoke(lambda: os.fstat(descriptor))
        returned.set()
        if not release_thread.wait(5):
            raise TimeoutError("test thread release")

    def cleanup() -> None:
        os.close(descriptor)
        closed.set()

    owner.deferred_cleanup = cleanup
    worker = ObservedJoinThread(native_thread)
    worker.start()
    await reached(returned)

    closing = asyncio.create_task(asyncio.to_thread(owner.release_deferred))
    try:
        await reached(worker.joined)
        descriptor_is_open = os.fstat(descriptor).st_ino == borrowed.stat().st_ino
        cleaned_during_join = closed.is_set()
    finally:
        release_thread.set()
        await closing
        if not closed.is_set():
            os.close(descriptor)

    assert descriptor_is_open
    assert not cleaned_during_join
    assert not worker.is_alive()
    assert closed.is_set()


async def test_deferred_cleanup_disarms_a_late_borrower() -> None:
    entered = threading.Event()
    closed = threading.Event()
    owner = NativeOperation(borrower=BorrowedInvocation(), deferred_cleanup=closed.set)

    await asyncio.to_thread(owner.release_deferred)
    with pytest.raises(NativeIONotInvoked):
        owner.invoke(entered.set)

    assert closed.is_set()
    assert not entered.is_set()


async def test_native_cleanup_error_retains_resource_owner(tmp_path: Path) -> None:
    executor = NativeExecutor(workers=1, admitted=1)
    resource = FileOperation(tmp_path / "owned")

    def failed_release(descriptor: int) -> None:
        os.fstat(descriptor)
        raise OSError("cleanup refused")

    caller = asyncio.create_task(executor.run(resource.acquire, failed_release))
    await reached(resource.opened)
    caller.cancel()
    resource.return_allowed.set()
    try:
        with pytest.raises(OSError, match="cleanup refused"):
            await caller
        assert len(executor.jobs) == 1
        owned = next(iter(executor.jobs))
        assert owned.retained == resource.descriptor
        assert resource.descriptor is not None
        os.fstat(resource.descriptor)
        with pytest.raises(NativeIOUnresolved):
            await executor.close()
    finally:
        if resource.descriptor is not None:
            os.close(resource.descriptor)

    assert executor.unresolved
    assert resource.worker is not None
    assert not resource.worker.is_alive()


async def test_native_close_survives_event_loop_shutdown(tmp_path: Path) -> None:
    resource = FileOperation(tmp_path / "owned")
    shutdown_cancelled = threading.Event()
    owners: list[NativeExecutor] = []

    async def inner() -> None:
        executor = NativeExecutor(workers=1, admitted=1)
        owners.append(executor)
        close_entered = asyncio.Event()
        probe_started = asyncio.Event()

        async def close() -> None:
            close_entered.set()
            await executor.close()

        async def probe() -> None:
            probe_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                shutdown_cancelled.set()

        observer = asyncio.create_task(probe())
        await probe_started.wait()
        caller = asyncio.create_task(executor.run(resource.acquire, resource.release))
        await reached(resource.opened)
        caller.cancel()
        resource.return_allowed.set()
        await reached(resource.cleaning)
        closing = asyncio.create_task(close())
        await close_entered.wait()
        assert not closing.done()
        assert not observer.done()

    parent = asyncio.create_task(asyncio.to_thread(asyncio.run, inner()))
    try:
        await reached(shutdown_cancelled)
        resource.close_allowed.set()
        await parent
        assert resource.worker is not None
        joined = not resource.worker.is_alive()
    finally:
        resource.return_allowed.set()
        resource.close_allowed.set()
        try:
            await parent
        finally:
            for owner in owners:
                await asyncio.to_thread(owner.executor.shutdown, wait=True)

    assert joined
    assert resource.closes == 1
    assert owners[0].jobs == set()
