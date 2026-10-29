import asyncio
import os
import re
import secrets
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Final

from pydantic import AfterValidator, Field, StrictStr

from kora.executor.digests import object_digest
from kora.ir.json import JsonObject, parse_json_object
from kora.observability import CATALOG, EventSpec, FieldKind, Level
from kora.plugins.runtime.local.spawn import (
    Launchers,
    Readiness,
    default_socket_directory,
    open_socket_directory,
    runner_command,
    runner_environment,
    spawn_runner,
)
from kora.protocols.common import BoundaryModel, ProtocolContext
from kora.protocols.deadlines import ExecutionDeadline
from kora.protocols.errors import KoraProtocolError
from kora.protocols.evidence import TerminationReceipt
from kora.protocols.identity import Handle, ProcessIdentity
from kora.protocols.runtime import RuntimeCapabilities, RuntimeStatus, Workload
from kora.protocols.time import Clock
from kora.protocols.vcs import WorkspaceTarget, printable_path
from kora.runner.filesystem import (
    DirectoryAnchor,
    FilesystemError,
    open_local_directory,
)
from kora.runner.identity import (
    IdentityReader,
    IdentityState,
    NativeIdentityReader,
)
from kora.runner.registry import (
    COMPONENT,
    RUNTIME_NAME,
    Applied,
    AttemptKey,
    Entry,
    Registry,
    Retired,
    Starting,
    Submitter,
    Transition,
    TransitionExecutor,
    bind_workspace,
    lineage_of,
    lookup_cell,
    open_domain,
    refusal,
    retire_cell,
    status_cell,
    submit_cell,
)

STATE_ROOT_LENGTH: Final[int] = 4052
ATTEMPT: Final[re.Pattern[str]] = re.compile(r"[\x21-\x7e]{1,256}")
REJECTIONS: Final[dict[str, str]] = {
    "state_root_unsafe": "the local state root is missing or unsafe",
    "attempt_invalid": "the attempt id is not valid for this call",
    "handle_invalid": "the handle does not belong to this runtime",
    "socket_directory_unsafe": (
        "the local socket directory must be owned by this user with mode 0700"
    ),
    "execution_domain_mismatch": "the workload names another execution domain",
    "dispatcher_process_mismatch": (
        "the workload's dispatcher process is not the submitting process"
    ),
}
SPAWNED: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "runtime.local.spawn",
        COMPONENT,
        Level.INFO,
        "a local attempt runner was spawned",
        {"outcome": FieldKind.STRING, "waited_ms": FieldKind.FLOAT},
    )
)

StateRoot = Annotated[
    StrictStr,
    Field(min_length=1, max_length=STATE_ROOT_LENGTH),
    AfterValidator(printable_path),
]


class LocalRuntimeConfiguration(BoundaryModel):
    state_root: StateRoot | None


CONFIG_SCHEMA: Final[JsonObject] = parse_json_object(
    LocalRuntimeConfiguration.model_json_schema()
)


def rejection(code: str) -> KoraProtocolError:
    return KoraProtocolError(code, REJECTIONS[code], False, {})


def default_state_root(
    platform: str, environment: Mapping[str, str], home: Path
) -> Path:
    if platform == "darwin":
        return home / "Library" / "Application Support" / "kora" / "local"
    configured = Path(environment.get("XDG_STATE_HOME", ""))
    base = configured if configured.is_absolute() else home / ".local" / "state"
    return base / "kora" / "local"


def anchored_root(
    configuration: LocalRuntimeConfiguration,
    environment: Mapping[str, str],
    home: Callable[[], Path],
) -> tuple[Path, DirectoryAnchor] | None:
    try:
        configured = configuration.state_root
        if configured is None:
            configured = str(default_state_root(sys.platform, environment, home()))
            LocalRuntimeConfiguration(state_root=configured)
        path = Path(configured)
        return path, open_local_directory(path)
    except (OSError, FilesystemError, ValueError, RuntimeError):
        return None


def release_root(located: tuple[Path, DirectoryAnchor] | None) -> None:
    if located is not None:
        located[1].close()


def safe_socket_directory(path: Path) -> bool:
    try:
        open_socket_directory(path).close()
    except (OSError, FilesystemError, ValueError):
        return False
    return True


def verified_socket_directory(path: Path) -> None:
    if not safe_socket_directory(path):
        raise rejection("socket_directory_unsafe")


def default_socket_path() -> Path | None:
    try:
        return default_socket_directory(sys.platform, os.getuid())
    except (OSError, FilesystemError, ValueError):
        return None


def chosen_socket_directory(configured: Path | None) -> Path:
    path = configured or default_socket_path()
    if path is None:
        raise rejection("socket_directory_unsafe")
    verified_socket_directory(path)
    return path


def environment_of(home: Callable[[], Path]) -> dict[str, str] | None:
    try:
        return runner_environment(sys.platform, home())
    except (OSError, FilesystemError, RuntimeError, KeyError):
        return None


def launch_environment(home: Callable[[], Path]) -> dict[str, str]:
    environment = environment_of(home)
    if environment is None:
        raise rejection("state_root_unsafe")
    return environment


def own_identity(identities: IdentityReader) -> ProcessIdentity:
    observation = identities.read(os.getpid())
    if observation.state is not IdentityState.PRESENT or observation.process is None:
        raise refusal("lineage_unavailable")
    return observation.process.identity


def expired(deadline: ExecutionDeadline | None, now: datetime) -> bool:
    return deadline is not None and now >= deadline.at - deadline.clock_uncertainty


def random_identifier() -> str:
    return secrets.token_hex(16)


@dataclass(frozen=True)
class RunnerLaunch:
    command: tuple[str, ...]
    environment: dict[str, str]
    socket_directory: Path
    identity: ProcessIdentity
    clock: Clock

    def argv(self, root: Path, key: AttemptKey, nonce: str) -> tuple[str, ...]:
        return (
            *self.command,
            str(root),
            key.attempt_id,
            key.execution_domain_id,
            nonce,
        )


class LocalRuntime:
    name: str = RUNTIME_NAME
    config_schema: JsonObject = CONFIG_SCHEMA
    execution_domain_id: str

    def __init__(
        self,
        root_path: Path,
        root: DirectoryAnchor,
        registry: Registry,
        executor: TransitionExecutor,
        new_identifier: Callable[[], str],
        launch: "RunnerLaunch",
    ) -> None:
        self.execution_domain_id = registry.execution_domain_id
        self.registry = registry
        self.executor = executor
        self.launchers = Launchers()
        self._root_path = root_path
        self._root = root
        self._new_identifier = new_identifier
        self._launch = launch
        self._closing: asyncio.Task[None] | None = None

    @classmethod
    async def open(
        cls,
        configuration: LocalRuntimeConfiguration,
        ctx: ProtocolContext,
        *,
        identities: IdentityReader | None = None,
        new_identifier: Callable[[], str] = random_identifier,
        environment: Mapping[str, str] = os.environ,
        home: Callable[[], Path] = Path.home,
        runner: Sequence[str] | None = None,
        socket_directory: Path | None = None,
    ) -> "LocalRuntime":
        executor = TransitionExecutor()
        try:
            reader = (
                identities
                if identities is not None
                else await executor.run(NativeIdentityReader)
            )
            lineage = await executor.run(lambda: lineage_of(reader))
            identity = await executor.run(lambda: own_identity(reader))
            sockets = await executor.run(
                lambda: chosen_socket_directory(socket_directory)
            )
            launch = RunnerLaunch(
                command=tuple(runner_command() if runner is None else runner),
                environment=await executor.run(lambda: launch_environment(home)),
                socket_directory=sockets,
                identity=identity,
                clock=ctx.clock,
            )
            located = await executor.run(
                lambda: anchored_root(configuration, environment, home), release_root
            )
            if located is None:
                raise rejection("state_root_unsafe")
            path, root = located
            try:
                domain, directory = await executor.locked(
                    lambda: open_domain(root, lineage, new_identifier, new_identifier),
                    ctx,
                    "open",
                    lambda opened: opened[1].close(),
                )
            except BaseException:
                root.close()
                raise
        except BaseException:
            await executor.close()
            raise
        registry = Registry(directory, domain.execution_domain_id, new_identifier)
        return cls(path, root, registry, executor, new_identifier, launch)

    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(
            supports_resume=False,
            portable_handles=False,
            enforces_network_policy=False,
            isolates_tool_secrets=False,
            credential_proxy=False,
            protected_execution=False,
            plan_executors=frozenset({"runner", "worker_host"}),
            max_concurrent=None,
            default_stall_after=timedelta(minutes=15),
        )

    def key(self, attempt_id: str, ctx: ProtocolContext) -> AttemptKey:
        if not ATTEMPT.fullmatch(attempt_id) or ctx.attempt_id not in (
            None,
            attempt_id,
        ):
            raise rejection("attempt_invalid")
        return self.registry.key(attempt_id)

    def key_of(self, handle: Handle, ctx: ProtocolContext) -> AttemptKey:
        key = self.key(handle.attempt_id, ctx)
        if handle != key.handle():
            raise rejection("handle_invalid")
        return key

    async def transition(
        self,
        key: AttemptKey,
        decide: Callable[[Entry | None], Transition],
        ctx: ProtocolContext,
        operation: str,
    ) -> Applied:
        applied = await self.executor.locked(
            lambda: self.registry.try_transition(key, decide), ctx, operation
        )
        if applied.transition.refusal is not None:
            raise refusal(applied.transition.refusal)
        return applied

    async def entry(self, key: AttemptKey) -> Entry | None:
        return await self.executor.run(lambda: self.registry.entry(key))

    async def workspace_target(
        self, attempt_id: str, ctx: ProtocolContext
    ) -> WorkspaceTarget:
        key = self.key(attempt_id, ctx)
        deadline = ctx.deadline
        await self.transition(
            key,
            lambda entry: bind_workspace(
                entry, deadline, expired(deadline, ctx.clock.now())
            ),
            ctx,
            "workspace_target",
        )
        target = str(self._root_path / "workspaces" / key.name)
        return WorkspaceTarget(root=target, host_path=target)

    async def lookup(self, attempt_id: str, ctx: ProtocolContext) -> Handle | None:
        key = self.key(attempt_id, ctx)
        return lookup_cell(await self.entry(key), key)

    async def status(self, handle: Handle, ctx: ProtocolContext) -> RuntimeStatus:
        key = self.key_of(handle, ctx)
        return status_cell(await self.entry(key), ctx.clock.now())

    def checked_digest(self, workload: Workload, ctx: ProtocolContext) -> str:
        if workload.execution_domain_id != self.execution_domain_id:
            raise rejection("execution_domain_mismatch")
        process = workload.dispatcher_process
        if process is None or process != self._launch.identity:
            raise rejection("dispatcher_process_mismatch")
        digest = object_digest(workload.execution_request)
        if (
            digest != workload.execution_request_digest
            or ctx.request_digest not in (None, digest)
            or workload.deadline != ctx.deadline
        ):
            raise refusal("workload_conflict")
        return digest

    async def submit(self, workload: Workload, ctx: ProtocolContext) -> Handle:
        key = self.key(workload.attempt_id, ctx)
        digest = self.checked_digest(workload, ctx)
        launch = self._launch
        await self.executor.run(
            lambda: verified_socket_directory(launch.socket_directory)
        )
        nonce = self._new_identifier()
        starting = Starting(
            state="starting",
            deadline=workload.deadline,
            gate_closed=False,
            request_digest=digest,
            submitter=Submitter(
                dispatcher_id=workload.dispatcher_id, process=launch.identity
            ),
            nonce=nonce,
            started_at=ctx.clock.now(),
            socket_directory=str(launch.socket_directory),
            fence=None,
        )
        overdue = expired(workload.deadline, ctx.clock.now())
        applied = await self.transition(
            key,
            lambda entry: submit_cell(
                entry, digest, workload.deadline, overdue, lambda: starting
            ),
            ctx,
            "submit",
        )
        if applied.transition.entry is starting:
            await self.spawn(launch.argv(self._root_path, key, nonce), ctx)
        return key.handle()

    async def spawn(self, argv: tuple[str, ...], ctx: ProtocolContext) -> None:
        started = ctx.clock.monotonic_ns()
        outcome = "failed"
        try:
            readiness = await spawn_runner(
                argv, self._launch.environment, ctx.clock, self.launchers
            )
            outcome = readiness.value
        except OSError:
            pass
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except KoraProtocolError:
            outcome = "closed"
            raise
        finally:
            waited = (ctx.clock.monotonic_ns() - started) / 1_000_000
            level = Level.INFO if outcome == Readiness.READY.value else Level.WARNING
            ctx.logger.emit_at(level, SPAWNED, outcome=outcome, waited_ms=waited)
        if outcome != Readiness.READY.value:
            raise refusal("runner_starting")

    async def retire(
        self, attempt_id: str, grace: timedelta, ctx: ProtocolContext
    ) -> TerminationReceipt:
        key = self.key(attempt_id, ctx)
        retirement_id = self._new_identifier()
        stopped_at = ctx.clock.now()
        applied = await self.transition(
            key,
            lambda entry: retire_cell(entry, key, retirement_id, stopped_at),
            ctx,
            "retire",
        )
        head = applied.head
        if head is None or not isinstance(head.entry, Retired):
            raise refusal("lineage_unavailable")
        return head.entry.receipt

    async def close(self) -> None:
        if self._closing is None:
            self.executor.closed = True
            self._closing = asyncio.create_task(self._release())
        await asyncio.shield(self._closing)

    async def _release(self) -> None:
        try:
            await self.launchers.drain(self._launch.clock)
        finally:
            await self.executor.close()
            self.registry.directory.close()
            self._root.close()
