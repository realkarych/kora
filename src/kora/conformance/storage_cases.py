import asyncio
import os
from collections.abc import Awaitable, Callable, Coroutine, Generator
from concurrent.futures import ThreadPoolExecutor
from contextvars import Context
from enum import StrEnum
from typing import TypeVar

from kora.conformance.bridge import ConformanceSelection, PreparedInvocation
from kora.protocols.lifetime import (
    ActivationResult,
    CleanupOutcome,
    ConformanceLifetime,
    ReservationRequest,
    ResourceId,
    ResourceKind,
    TemporaryTreeRegistration,
    TemporaryTreeRequest,
    new_operation_id,
)
from kora.runner.filesystem import DirectoryAnchor, DirectoryIdentity, FilesystemError

T = TypeVar("T")


class StorageCaseReason(StrEnum):
    SEQUENCE_CLOSED = "sequence_closed"
    LOOP_RUNNING = "loop_running"
    TASK_FACTORY_OCCUPIED = "task_factory_occupied"
    REGISTRATION_INVALID = "registration_invalid"
    ACTIVATION_REFUSED = "activation_refused"
    TASKS_LIVE = "tasks_live"
    CLEANUP_UNKNOWN = "cleanup_unknown"


class StorageCaseError(RuntimeError):
    def __init__(self, reason: StorageCaseReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


def require_current_directory(anchor: DirectoryAnchor) -> None:
    anchor.verify()
    current = os.stat(".")
    if (current.st_dev, current.st_ino) != (
        anchor.identity.device,
        anchor.identity.inode,
    ):
        raise FilesystemError("working_directory_changed")


class StorageCaseRunner:
    def __init__(
        self,
        runner: asyncio.Runner,
        selection: ConformanceSelection,
        adapter: object,
        lifetime: ConformanceLifetime,
        invocation_root: DirectoryAnchor,
        startup_directory: DirectoryAnchor,
    ) -> None:
        self._runner = runner
        self._selection = selection
        self._adapter = adapter
        self._lifetime = lifetime
        self._invocation_root = invocation_root
        self._startup_directory = startup_directory
        self._failed = False
        self._resource_id: ResourceId | None = None
        self._directory: DirectoryAnchor | None = None
        self._pool: ThreadPoolExecutor | None = None
        self._tasks: set[asyncio.Task[object]] = set()

    def _track(
        self,
        loop: asyncio.AbstractEventLoop,
        coro: Generator[asyncio.Future[object] | None, None, T]
        | Coroutine[object, object, T],
        /,
        *,
        name: str | None = None,
        context: Context | None = None,
    ) -> asyncio.Task[T]:
        task = asyncio.Task(coro, loop=loop, name=name, context=context)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _acquire(self) -> None:
        reservation = await self._lifetime.reserve(
            ReservationRequest(
                operation_id=new_operation_id(), kind=ResourceKind.TEMPORARY_TREE
            )
        )
        self._resource_id = reservation.token.resource_id
        acquisition = await self._lifetime.acquire(
            reservation.token, new_operation_id(), TemporaryTreeRequest()
        )
        registration = acquisition.registration
        if (
            not isinstance(registration, TemporaryTreeRegistration)
            or acquisition.token != reservation.token
            or registration.name != reservation.token.resource_id
            or registration.parent_resource_id != self._invocation_root.identity.name
        ):
            raise StorageCaseError(StorageCaseReason.REGISTRATION_INVALID)
        self._directory = self._invocation_root.open_child(
            DirectoryIdentity(
                name=registration.name,
                device=registration.device,
                inode=registration.inode,
                owner_uid=registration.owner_uid,
            )
        )
        activation = await self._lifetime.acknowledge(
            reservation.token, acquisition.operation_id, acquisition.revision
        )
        if (
            activation.result is not ActivationResult.ACTIVE
            or activation.token != reservation.token
            or activation.acquisition_operation_id != acquisition.operation_id
        ):
            raise StorageCaseError(StorageCaseReason.ACTIVATION_REFUSED)

    async def _exercise(
        self,
        exercise: Callable[[PreparedInvocation], Awaitable[None]],
        baseline: set[asyncio.Task[object]],
    ) -> None:
        async with self._selection.prepare(self._adapter, self._lifetime) as invocation:
            await exercise(invocation)
        self._require_tasks_closed(baseline)

    def _require_tasks_closed(self, baseline: set[asyncio.Task[object]]) -> None:
        current = asyncio.current_task()
        if any(task is not current and not task.done() for task in self._tasks) or any(
            task is not current for task in asyncio.all_tasks() - baseline
        ):
            raise StorageCaseError(StorageCaseReason.TASKS_LIVE)

    async def _complete(self, baseline: set[asyncio.Task[object]]) -> None:
        loop = asyncio.get_running_loop()
        completed: asyncio.Future[None] = loop.create_future()
        loop.call_soon(completed.set_result, None)
        await completed
        self._require_tasks_closed(baseline)

    async def _close(self, resource_id: ResourceId) -> None:
        report = await self._lifetime.close(resource_id)
        if (
            report.state is not CleanupOutcome.CLOSED
            or report.resource_id != resource_id
            or report.invocation_id != self._invocation_root.identity.name
        ):
            raise StorageCaseError(StorageCaseReason.CLEANUP_UNKNOWN)

    def run(self, exercise: Callable[[PreparedInvocation], Awaitable[None]]) -> None:
        if self._failed:
            raise StorageCaseError(StorageCaseReason.SEQUENCE_CLOSED)
        loop = self._runner.get_loop()
        if loop.get_task_factory() is not None:
            raise StorageCaseError(StorageCaseReason.TASK_FACTORY_OCCUPIED)
        self._failed = True
        if loop.is_running():
            raise StorageCaseError(StorageCaseReason.LOOP_RUNNING)
        require_current_directory(self._startup_directory)
        self._runner.run(self._acquire())
        directory = self._directory
        resource_id = self._resource_id
        if directory is None or resource_id is None:
            raise StorageCaseError(StorageCaseReason.REGISTRATION_INVALID)
        directory.verify()
        self._pool = ThreadPoolExecutor(thread_name_prefix="kora-storage-case")
        loop.set_default_executor(self._pool)
        baseline = asyncio.all_tasks(loop)
        loop.set_task_factory(self._track)
        try:
            os.fchdir(directory.descriptor)
            self._runner.run(self._exercise(exercise, baseline))
            self._pool.shutdown(wait=True)
            self._runner.run(self._complete(baseline))
        finally:
            loop.set_task_factory(None)
            self._tasks.clear()
        require_current_directory(directory)
        self._startup_directory.verify()
        os.fchdir(self._startup_directory.descriptor)
        directory.close()
        self._runner.run(self._close(resource_id))
        self._directory = None
        self._resource_id = None
        self._pool = None
        self._failed = False
