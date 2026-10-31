import asyncio
import os
import re
import secrets
import signal
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from functools import partial
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
from kora.protocols.deadlines import ExecutionDeadline, duration_ns
from kora.protocols.errors import KoraProtocolError
from kora.protocols.evidence import TerminationReceipt
from kora.protocols.identity import Handle, ProcessIdentity
from kora.protocols.runtime import RuntimeCapabilities, RuntimeStatus, Workload
from kora.protocols.time import Clock
from kora.protocols.vcs import WorkspaceTarget, printable_path
from kora.runner.channels import ChannelError
from kora.runner.filesystem import (
    DirectoryAnchor,
    FilesystemError,
    open_local_directory,
)
from kora.runner.identity import (
    SCAN_NS,
    ArgumentMatch,
    IdentityReader,
    IdentityState,
    NativeIdentityReader,
    ProcessScanFailure,
)
from kora.runner.processes import ExitAnchor, SignalResult, wait_member_change
from kora.runner.registry import (
    COMPONENT,
    RUNTIME_NAME,
    Applied,
    AttemptKey,
    Entry,
    Fence,
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
    runner_argv,
    status_cell,
    submit_cell,
    tombstone_cell,
)

STATE_ROOT_LENGTH: Final[int] = 4052
STOP_MATCHES: Final[int] = 8
SWEEP_WORKERS: Final[int] = 1
SWEEP_ADMITTED: Final[int] = 4
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

SWEPT: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "runtime.local.retire",
        COMPONENT,
        Level.INFO,
        "a local attempt's unregistered runners were swept",
        {
            "outcome": FieldKind.STRING,
            "matched": FieldKind.INTEGER,
            "killed": FieldKind.INTEGER,
            "waited_ms": FieldKind.FLOAT,
            "scan_failure": FieldKind.STRING,
        },
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


class SweepOutcome(StrEnum):
    TOMBSTONED = "tombstoned"
    LISTING_INCOMPLETE = "listing_incomplete"
    MATCH_UNREADABLE = "match_unreadable"
    SIGNAL_UNKNOWN = "signal_unknown"
    TOO_MANY_MATCHES = "too_many_matches"
    SURVIVED_GRACE = "survived_grace"


class SweepRefused(Exception):
    def __init__(
        self, outcome: SweepOutcome, scan_failure: ProcessScanFailure | None = None
    ) -> None:
        self.outcome = outcome
        self.scan_failure = scan_failure
        super().__init__(outcome.value)


@dataclass
class Sweep:
    started_ns: int
    matched: int = 0
    killed: int = 0


def readable(observation: IdentityState) -> None:
    if observation is IdentityState.UNKNOWN:
        raise SweepRefused(SweepOutcome.MATCH_UNREADABLE)


def verified(
    identities: IdentityReader,
    scanner: NativeIdentityReader,
    pid: int,
    argv: tuple[str, ...],
) -> ProcessIdentity | None:
    before = identities.read(pid)
    readable(before.state)
    if before.process is None or before.process.dead:
        return None
    carried = scanner.carries(pid, argv)
    if carried is ArgumentMatch.UNKNOWN:
        raise SweepRefused(SweepOutcome.MATCH_UNREADABLE)
    after = identities.read(pid)
    readable(after.state)
    if (
        carried is not ArgumentMatch.EQUAL
        or after.process is None
        or after.process.dead
        or after.process.identity != before.process.identity
    ):
        return None
    return before.process.identity


def vanished(identities: IdentityReader, pid: int) -> None:
    observation = identities.read(pid)
    readable(observation.state)
    if observation.process is not None and not observation.process.dead:
        raise SweepRefused(SweepOutcome.MATCH_UNREADABLE)


def stopped(identities: IdentityReader, matches: tuple[ProcessIdentity, ...]) -> None:
    for identity in matches:
        observation = identities.read(identity.pid)
        readable(observation.state)
        process = observation.process
        if process is not None and not process.dead and process.identity == identity:
            raise SweepRefused(SweepOutcome.SURVIVED_GRACE)


@dataclass(frozen=True)
class RunnerLaunch:
    command: tuple[str, ...]
    environment: dict[str, str]
    socket_directory: Path
    identity: ProcessIdentity
    clock: Clock

    def prefix(self, root: Path) -> tuple[str, ...]:
        return (*self.command, str(root))


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
        identities: IdentityReader,
        scanner: NativeIdentityReader,
    ) -> None:
        self.execution_domain_id = registry.execution_domain_id
        self.registry = registry
        self.executor = executor
        self.sweeps = TransitionExecutor(SWEEP_WORKERS, SWEEP_ADMITTED)
        self._identities = identities
        self._scanner = scanner
        self._sweeping = 0
        self._swept = asyncio.Event()
        self._swept.set()
        self._stopping: asyncio.Future[None] = (
            asyncio.get_running_loop().create_future()
        )
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
        sweep_scan_entries: int = 65536,
    ) -> "LocalRuntime":
        executor = TransitionExecutor()
        try:
            reader = (
                identities
                if identities is not None
                else await executor.run(NativeIdentityReader)
            )
            scanner = await executor.run(
                lambda: NativeIdentityReader(
                    clock=ctx.clock, max_scan_entries=sweep_scan_entries
                )
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
        return cls(
            path, root, registry, executor, new_identifier, launch, reader, scanner
        )

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
            runner_prefix=launch.prefix(self._root_path),
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
            await self.spawn(runner_argv(starting.runner_prefix, key, nonce), ctx)
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
        entry = None if applied.head is None else applied.head.entry
        if isinstance(entry, Starting) and entry.fence is not None:
            entry = await self.retire_starting(key, entry, entry.fence, grace, ctx)
        if not isinstance(entry, Retired):
            raise refusal("lineage_unavailable")
        return entry.receipt

    async def retire_starting(
        self,
        key: AttemptKey,
        entry: Starting,
        fence: Fence,
        grace: timedelta,
        ctx: ProtocolContext,
    ) -> Entry | None:
        if self.sweeps.closed:
            raise refusal("runtime_closed")
        if self._sweeping >= SWEEP_ADMITTED:
            raise refusal("registry_busy")
        self._sweeping += 1
        self._swept.clear()
        try:
            return await self.sweep_and_tombstone(key, entry, fence, grace, ctx)
        finally:
            self._sweeping -= 1
            if not self._sweeping:
                self._swept.set()

    async def sweep_and_tombstone(
        self,
        key: AttemptKey,
        entry: Starting,
        fence: Fence,
        grace: timedelta,
        ctx: ProtocolContext,
    ) -> Entry | None:
        sweep = Sweep(ctx.clock.monotonic_ns())
        argv = runner_argv(entry.runner_prefix, key, entry.nonce)
        deadline_ns = sweep.started_ns + max(0, duration_ns(grace))
        try:
            await self.stop_carriers(argv, sweep, deadline_ns, ctx)
        except SweepRefused as refused:
            if self._stopping.done():
                raise refusal("runtime_closed") from None
            self.report(refused.outcome, sweep, ctx, refused.scan_failure)
            raise refusal("runner_stop_unconfirmed") from None
        applied = await self.transition(
            key,
            lambda current: tombstone_cell(current, key, fence, ctx.clock.now()),
            ctx,
            "retire",
        )
        self.report(SweepOutcome.TOMBSTONED, sweep, ctx)
        return None if applied.head is None else applied.head.entry

    def report(
        self,
        outcome: SweepOutcome,
        sweep: Sweep,
        ctx: ProtocolContext,
        scan_failure: ProcessScanFailure | None = None,
    ) -> None:
        level = Level.INFO if outcome is SweepOutcome.TOMBSTONED else Level.WARNING
        waited = (ctx.clock.monotonic_ns() - sweep.started_ns) / 1_000_000
        scan_fields: dict[str, str] = {}
        if outcome is SweepOutcome.LISTING_INCOMPLETE and scan_failure is not None:
            scan_fields["scan_failure"] = scan_failure.value
        ctx.logger.emit_at(
            level,
            SWEPT,
            outcome=outcome.value,
            matched=sweep.matched,
            killed=sweep.killed,
            waited_ms=waited,
            **scan_fields,
        )

    async def stop_carriers(
        self,
        argv: tuple[str, ...],
        sweep: Sweep,
        deadline_ns: int,
        ctx: ProtocolContext,
    ) -> None:
        scanner = self._scanner
        scan_deadline = sweep.started_ns + SCAN_NS
        listed = await self.sweeps.run(
            lambda: scanner.scan_processes_carrying(argv, scan_deadline)
        )
        if isinstance(listed, ProcessScanFailure):
            raise SweepRefused(SweepOutcome.LISTING_INCOMPLETE, listed)
        sweep.matched = len(listed)
        if len(listed) > STOP_MATCHES:
            raise SweepRefused(SweepOutcome.TOO_MANY_MATCHES)
        anchors: list[ExitAnchor] = []
        try:
            pinned = await self.pin(listed, argv, anchors, scan_deadline, ctx)
            if pinned and ctx.clock.monotonic_ns() >= deadline_ns:
                raise SweepRefused(SweepOutcome.SURVIVED_GRACE)
            if self._stopping.done():
                raise refusal("runtime_closed")
            for anchor, _ in pinned:
                result = anchor.signal(signal.SIGKILL)
                if result is SignalResult.UNKNOWN:
                    raise SweepRefused(SweepOutcome.SIGNAL_UNKNOWN)
                sweep.killed += result is SignalResult.SENT
            await self.confirm(pinned, deadline_ns, ctx)
        finally:
            for anchor in anchors:
                anchor.close()

    async def pin(
        self,
        listed: tuple[int, ...],
        argv: tuple[str, ...],
        anchors: list[ExitAnchor],
        scan_deadline: int,
        ctx: ProtocolContext,
    ) -> list[tuple[ExitAnchor, ProcessIdentity]]:
        identities = self._identities
        scanner = self._scanner
        pinned: list[tuple[ExitAnchor, ProcessIdentity]] = []
        for pid in listed:
            if ctx.clock.monotonic_ns() >= scan_deadline:
                raise SweepRefused(SweepOutcome.LISTING_INCOMPLETE)
            anchor = ExitAnchor(pid, direct_child=False)
            anchors.append(anchor)
            if anchor.unknown:
                await self.sweeps.run(partial(vanished, identities, pid))
                continue
            identity = await self.sweeps.run(
                partial(verified, identities, scanner, pid, argv)
            )
            if identity is not None and anchor.readiness.done():
                await self.sweeps.run(partial(vanished, identities, pid))
                continue
            if identity is not None:
                pinned.append((anchor, identity))
        return pinned

    async def confirm(
        self,
        pinned: list[tuple[ExitAnchor, ProcessIdentity]],
        deadline_ns: int,
        ctx: ProtocolContext,
    ) -> None:
        while not self._stopping.done():
            pending = tuple(
                anchor.readiness for anchor, _ in pinned if not anchor.readiness.done()
            )
            if not pending:
                break
            try:
                await wait_member_change(
                    (*pending, self._stopping), deadline_ns, ctx.clock
                )
            except ChannelError:
                break
        if self._stopping.done():
            raise refusal("runtime_closed")
        identities = self._identities
        unconfirmed = tuple(
            identity for anchor, identity in pinned if not anchor.exited
        )
        await self.sweeps.run(lambda: stopped(identities, unconfirmed))

    async def close(self) -> None:
        if self._closing is None:
            self.executor.closed = True
            self.sweeps.closed = True
            self._stopping.set_result(None)
            self._closing = asyncio.create_task(self._release())
        await asyncio.shield(self._closing)

    async def _release(self) -> None:
        try:
            await self.launchers.drain(self._launch.clock)
        finally:
            await self.executor.close()
            await self._swept.wait()
            await self.sweeps.close()
            self.registry.directory.close()
            self._root.close()
