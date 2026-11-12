import asyncio
import os
import sys
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, TypeVar

from kora.executor.entry import Destination, Executor
from kora.executor.failures import ExecutorError
from kora.executor.launcher import ProcessLauncher
from kora.executor.scan import InspectionSource
from kora.executor.steps import Areas, ToolMissing, resolve_tools
from kora.ir import VcsRef
from kora.ir.json import JsonObject
from kora.observability import CATALOG, ComponentLogger, EventSpec, FieldKind, Level
from kora.plugins.runtime.local.channel import delivery_error
from kora.plugins.runtime.local.reaper import AdoptedReaper
from kora.plugins.runtime.local.spawn import SYSTEM_PATH
from kora.protocols.activity import NoActivity
from kora.protocols.common import ProtocolContext
from kora.protocols.runtime import CheckoutSource, Workload
from kora.protocols.time import Clock
from kora.protocols.vcs import CheckoutPlan, CheckoutRequest
from kora.runner.filesystem import (
    DirectoryAnchor,
    DirectoryIdentity,
    FilesystemError,
    open_local_directory,
    open_private_directory,
)
from kora.runner.journal import RunningJournal
from kora.runner.native_io import NativeExecutor, NativeIOUnresolved
from kora.runner.registry import synchronize

CONTROL: Final[str] = "control"
CONTEXT_NAMESPACE: Final[str] = "local-runner"
RUNNER_PYTHON: Final[str] = "python"
COMPONENT: Final[str] = CATALOG.register_component("runtime.local.runner")
WORKSPACE: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "runtime.local.workspace",
        COMPONENT,
        Level.INFO,
        "the attempt runner prepared a workload workspace",
        {"outcome": FieldKind.STRING, "waited_ms": FieldKind.INTEGER},
    )
)


Result = TypeVar("Result")
PathIdentity = tuple[DirectoryIdentity, tuple[DirectoryIdentity, ...]]
BoundAreas = tuple[PathIdentity, PathIdentity]


class WorkspaceState(StrEnum):
    ABSENT = "absent"
    PENDING = "pending"
    PREPARED = "prepared"
    FAILED = "failed"


@dataclass(frozen=True)
class PreparedWorkspace:
    repo: str
    commit: str
    base_commit: str
    branch: str | None


class WorkspaceRefused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class CheckoutOnly:
    def destination(self, repo: str) -> Destination:
        raise WorkspaceRefused("publication_unavailable")

    def open(
        self, prepared_location: str
    ) -> AbstractAsyncContextManager[InspectionSource]:
        raise WorkspaceRefused("inspection_unavailable")

    async def head_sha(self, ref: VcsRef, ctx: ProtocolContext) -> str:
        raise WorkspaceRefused("publication_unavailable")


class RegisteringLauncher(ProcessLauncher):
    def __init__(self, reaper: AdoptedReaper) -> None:
        super().__init__()
        self._reaper = reaper

    def started(self, pid: int) -> None:
        self._reaper.register(pid)

    def collected(self, pid: int) -> None:
        self._reaper.release(pid)


def runner_tools(names: tuple[str, ...]) -> dict[str, str]:
    searched = tuple(name for name in names if name != RUNNER_PYTHON)
    tools = resolve_tools(searched, SYSTEM_PATH.split(":"))
    if RUNNER_PYTHON in names:
        tools[RUNNER_PYTHON] = sys.executable
    return tools


@dataclass(frozen=True)
class Admitted:
    source: CheckoutSource
    plan: CheckoutPlan


def checkout_admission(workload: Workload) -> Admitted | str | None:
    source = workload.checkout_request
    if source is None:
        return None
    if source.protected_paths:
        return "protected_paths_unsupported"
    if not isinstance(workload.checkout, CheckoutPlan):
        return "workload_kind_unavailable"
    return Admitted(source, workload.checkout)


class WorkspacePreparation:
    def __init__(
        self,
        attempt: DirectoryAnchor,
        candidate: str,
        journal: RunningJournal,
        reaper: AdoptedReaper,
        clock: Clock,
        log: ComponentLogger,
    ) -> None:
        self._attempt = attempt
        self._candidate = candidate
        self._journal = journal
        self._reaper = reaper
        self._clock = clock
        self._log = log
        self._state = WorkspaceState.ABSENT
        self._task: asyncio.Task[None] | None = None

    @property
    def state(self) -> WorkspaceState:
        return self._state

    def begin(self, admitted: Admitted, attempt_id: str) -> None:
        if self._task is not None:
            return
        self._state = WorkspaceState.PENDING
        self._task = asyncio.get_running_loop().create_task(
            self._prepare(admitted.source, admitted.plan, attempt_id)
        )

    def gate(self) -> None:
        if self._state is WorkspaceState.ABSENT:
            raise delivery_error("workspace_failed")
        if self._state is WorkspaceState.PENDING:
            raise delivery_error("workload_busy")
        if self._state is WorkspaceState.FAILED:
            raise delivery_error("workspace_failed")

    def cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()

    async def settle(self) -> None:
        task = self._task
        if task is None:
            return
        await asyncio.wait({task})
        if not task.cancelled() and task.exception() is not None:
            task.result()

    async def _prepare(
        self, source: CheckoutSource, plan: CheckoutPlan, attempt_id: str
    ) -> None:
        started = self._clock.monotonic_ns()
        try:
            await self._settled(source, plan, attempt_id, started)
        finally:
            if self._state is WorkspaceState.PENDING:
                self._finish(WorkspaceState.FAILED, "cancelled", started)

    async def _settled(
        self, source: CheckoutSource, plan: CheckoutPlan, attempt_id: str, started: int
    ) -> None:
        try:
            prepared = await self._checkout(source, plan, attempt_id)
        except ExecutorError as error:
            await self._fail(error.code, started)
            return
        except WorkspaceRefused as refused:
            await self._fail(refused.code, started)
            return
        except ToolMissing:
            await self._fail("tool_missing", started)
            return
        except (OSError, FilesystemError, NativeIOUnresolved):
            await self._fail("workspace_unavailable", started)
            return
        data: JsonObject = {
            "repo": prepared.repo,
            "commit": prepared.commit,
            "base_commit": prepared.base_commit,
            "branch": prepared.branch,
        }
        if not await self._append("runner.workspace_prepared", data):
            self._finish(WorkspaceState.FAILED, "journal_unavailable", started)
            return
        self._finish(WorkspaceState.PREPARED, "prepared", started)

    async def _checkout(
        self, source: CheckoutSource, plan: CheckoutPlan, attempt_id: str
    ) -> PreparedWorkspace:
        root = Path(self._candidate)
        control = root.parent / CONTROL
        bound = await self._native(lambda: self._bind(root, control))
        tools = runner_tools(plan.required_tools)
        refusals = CheckoutOnly()
        executor = Executor(
            tools, RegisteringLauncher(self._reaper), refusals, refusals, refusals
        )
        self._reaper.start()
        result = await executor.prepare_workspace(
            plan,
            CheckoutRequest(
                repo=source.repo,
                rev=source.rev,
                root=str(root),
                attempt_id=attempt_id,
                branch=source.branch,
            ),
            Areas(root, control, None),
            (),
            False,
            self._context(attempt_id),
        )
        if await self._native(lambda: self._bound_paths(root, control)) != bound:
            raise WorkspaceRefused("workspace_identity_changed")
        pinned = source.pinned
        if pinned is not None and pinned.commit != result.commit:
            raise WorkspaceRefused("pinned_commit_mismatch")
        base = result.commit if pinned is None else pinned.base_commit
        return PreparedWorkspace(source.repo, result.commit, base, result.branch)

    def _bind(self, root: Path, control: Path) -> BoundAreas:
        anchored = (
            self._ensure_directory(root.name),
            self._ensure_directory(CONTROL),
        )
        bound = self._bound_paths(root, control)
        if (bound[0][0], bound[1][0]) != anchored:
            raise WorkspaceRefused("workspace_identity_changed")
        return bound

    def _bound_paths(self, root: Path, control: Path) -> BoundAreas:
        return (self._path_identity(root), self._path_identity(control))

    def _path_identity(self, path: Path) -> PathIdentity:
        opened = open_local_directory(path)
        try:
            return opened.identity, opened.ancestors
        finally:
            opened.close()

    def _ensure_directory(self, name: str) -> DirectoryIdentity:
        try:
            os.mkdir(name, mode=0o700, dir_fd=self._attempt.descriptor)
        except FileExistsError:
            pass
        opened = open_private_directory(self._attempt, name)
        try:
            synchronize(self._attempt.descriptor)
            return opened.identity
        finally:
            opened.close()

    def _context(self, attempt_id: str) -> ProtocolContext:
        return ProtocolContext(
            namespace_id=CONTEXT_NAMESPACE,
            run_id=None,
            attempt_id=attempt_id,
            logger=self._log,
            secrets=None,
            secret_admission=None,
            request_digest=None,
            deadline=None,
            clock=self._clock,
            activity=NoActivity(),
        )

    async def _fail(self, code: str, started: int) -> None:
        await self._append("runner.workspace_failed", {"code": code})
        self._finish(WorkspaceState.FAILED, "failed", started)

    async def _native(self, operation: Callable[[], Result]) -> Result:
        executor = NativeExecutor(1, 1)
        try:
            return await executor.run(operation)
        finally:
            await executor.close()

    async def _append(self, kind: str, data: JsonObject) -> bool:
        try:
            return await self._native(lambda: self._journal.append(kind, data))
        except NativeIOUnresolved:
            return False

    def _finish(self, state: WorkspaceState, outcome: str, started: int) -> None:
        self._state = state
        waited = max(0, (self._clock.monotonic_ns() - started) // 1_000_000)
        level = Level.INFO if outcome == "prepared" else Level.WARNING
        self._log.emit_at(level, WORKSPACE, outcome=outcome, waited_ms=waited)
