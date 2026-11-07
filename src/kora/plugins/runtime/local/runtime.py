import asyncio
import os
import re
import secrets
import signal
import sys
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Callable,
    Coroutine,
    Mapping,
    Sequence,
)
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from functools import partial
from pathlib import Path
from threading import Event
from typing import Final, TypeVar

from kora.executor.digests import object_digest
from kora.executor.launcher import ProcessLauncher, StepLauncher
from kora.ir.json import JsonObject
from kora.observability import CATALOG, EventSpec, FieldKind, Level
from kora.plugins.runtime.local.channel import (
    MESSAGE_PROGRESS_NS,
    SECTION_FRAME_BYTES,
    checked_output_name,
    delivery_error,
    transfer_cutoff,
    workload_digest,
    write_transfer_bytes,
)
from kora.plugins.runtime.local.configuration import CONFIG_SCHEMA as CONFIG_SCHEMA
from kora.plugins.runtime.local.configuration import (
    STATE_ROOT_LENGTH as STATE_ROOT_LENGTH,
)
from kora.plugins.runtime.local.configuration import (
    LocalRuntimeConfiguration as LocalRuntimeConfiguration,
)
from kora.plugins.runtime.local.configuration import StateRoot as StateRoot
from kora.plugins.runtime.local.control import (
    ListenerRefused,
    deliver_workload,
    runner_status,
    start_command,
    transfer_input,
    transfer_output,
)
from kora.plugins.runtime.local.services import (
    SERVICE_REFUSED,
    ServiceProbe,
    ServiceReport,
    service_cgroup,
    service_tools,
    unmanaged_services,
)
from kora.plugins.runtime.local.spawn import (
    Launchers,
    Readiness,
    default_socket_directory,
    launch_deadline,
    open_socket_directory,
    runner_command,
    runner_environment,
    spawn_runner,
)
from kora.plugins.runtime.local.spool import (
    SWEEP,
    SpoolLease,
    SpoolOwner,
    SpoolSweepOutcome,
    close_spool,
)
from kora.protocols.common import ProtocolContext
from kora.protocols.deadlines import ExecutionDeadline, duration_ns
from kora.protocols.errors import KoraProtocolError
from kora.protocols.evidence import TerminationReceipt
from kora.protocols.identity import Handle, ProcessIdentity
from kora.protocols.runtime import (
    RuntimeCapabilities,
    RuntimeStatus,
    TransferItem,
    Workload,
)
from kora.protocols.time import Clock
from kora.protocols.vcs import WorkspaceTarget
from kora.runner.channels import ChannelError, ChannelFailure, wait_future
from kora.runner.filesystem import (
    DirectoryAnchor,
    FilesystemError,
    open_local_directory,
)
from kora.runner.identity import (
    SCAN_NS,
    ArgumentMatch,
    IdentityObservation,
    IdentityReader,
    IdentityState,
    NativeIdentityReader,
    ProcessScanFailure,
)
from kora.runner.native_io import NativeIOSubmissionUncertain, observed
from kora.runner.peers import PeerSource, kernel_source
from kora.runner.processes import ExitAnchor, SignalResult, wait_member_change
from kora.runner.registry import (
    COMPONENT,
    RUNTIME_NAME,
    Applied,
    AttemptKey,
    DomainRecord,
    Entry,
    Fence,
    LockOutcome,
    Registry,
    Retired,
    Running,
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
    takeover_cell,
    tombstone_cell,
)

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
LISTENER_REFUSED: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "runtime.local.listener_refused",
        COMPONENT,
        Level.WARNING,
        "a local attempt runner's listener failed attestation",
        {"reason": FieldKind.STRING},
    )
)


def rejection(code: str) -> KoraProtocolError:
    return KoraProtocolError(code, REJECTIONS[code], False, {})


OUTPUT_ATTEMPTS: Final[int] = 4
OUTPUT_PULLS: Final[int] = 4
T = TypeVar("T")


class OutputPull:
    def __init__(self) -> None:
        self.lease: SpoolLease | None = None
        self.pending: asyncio.Task[object] | None = None
        self.uncertain = False

    def dispose(self) -> None:
        lease = self.lease
        if lease is not None:
            lease.close()
            if lease.closed:
                self.lease = None

    def descriptor(self) -> int:
        lease = self.lease
        if lease is None or lease.closed:
            raise RuntimeError("missing output spool")
        return lease.descriptor

    async def run(
        self,
        operation: Coroutine[object, object, T],
        clock: Clock,
        deadline: int | None = None,
    ) -> T:
        task = asyncio.create_task(operation)
        self.pending = task
        try:
            if deadline is not None:
                await wait_future(task, deadline, clock)
            return await task
        finally:
            try:
                if not task.done():
                    task.cancel()
                    if await observed(task):
                        raise asyncio.CancelledError
            finally:
                if not task.cancelled() and isinstance(
                    task.exception(), NativeIOSubmissionUncertain
                ):
                    self.uncertain = True
                self.pending = None


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
    deadline_ns: int,
) -> ProcessIdentity | None:
    before = identities.read(pid)
    readable(before.state)
    if before.process is None or before.process.dead:
        return None
    carried = scanner.carries(pid, argv, deadline_ns)
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


def output_deadline_reached(
    entry: Running, clock: Clock, captured: int | None = None
) -> bool:
    if entry.gate_closed:
        return True
    deadline = entry.deadline
    if deadline is None:
        return False
    if clock.now() >= deadline.at - deadline.clock_uncertainty:
        return True
    if captured is not None and clock.monotonic_ns() >= captured:
        return True
    cutoff = transfer_cutoff(clock, deadline)
    return cutoff is not None and clock.monotonic_ns() >= cutoff


class LocalRuntime:
    name: str = RUNTIME_NAME
    config_schema: JsonObject = CONFIG_SCHEMA
    execution_domain_id: str
    service_report: ServiceReport | None

    def __init__(
        self,
        configuration: LocalRuntimeConfiguration,
        root_path: Path,
        root: DirectoryAnchor,
        registry: Registry,
        executor: TransitionExecutor,
        new_identifier: Callable[[], str],
        launch: "RunnerLaunch",
        identities: IdentityReader,
        scanner: NativeIdentityReader,
        peers: PeerSource,
        spool: SpoolOwner,
    ) -> None:
        self._configuration: LocalRuntimeConfiguration = configuration
        self.execution_domain_id = registry.execution_domain_id
        self.service_report = None
        self.registry = registry
        self.executor = executor
        self.sweeps = TransitionExecutor(SWEEP_WORKERS, SWEEP_ADMITTED)
        self._identities = identities
        self._scanner = scanner
        self._peers = peers
        self._observations: set[asyncio.Task[RuntimeStatus]] = set()
        self._deliveries: set[asyncio.Task[None]] = set()
        self._spool = spool
        self._spool_admission = asyncio.Lock()
        self._spool_allocations = 0
        self._output_pulls: set[OutputPull] = set()
        self._expired_outputs: set[AttemptKey] = set()
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
        scanner: NativeIdentityReader | None = None,
        peers: PeerSource | None = None,
        service_launcher: StepLauncher | None = None,
    ) -> "LocalRuntime":
        executor = TransitionExecutor()
        located: tuple[Path, DirectoryAnchor] | None = None
        opened_registry: DirectoryAnchor | None = None
        spool: SpoolOwner | None = None

        def locate() -> tuple[Path, DirectoryAnchor] | None:
            nonlocal located
            located = anchored_root(configuration, environment, home)
            return located

        def close_root() -> None:
            nonlocal located
            if located is not None:
                owned, located = located, None
                owned[1].close()

        def close_registry() -> None:
            nonlocal opened_registry
            if opened_registry is not None:
                owned, opened_registry = opened_registry, None
                owned.close()

        def close_spool_after_join() -> None:
            nonlocal spool
            if spool is not None:
                owned, spool = spool, None
                owned.close()

        try:
            reader = (
                identities
                if identities is not None
                else await executor.run(NativeIdentityReader)
            )

            async def service_identity(pid: int) -> IdentityObservation:
                return await executor.run(lambda: reader.read(pid))

            report: ServiceReport | None = None
            launch_env = await executor.run(lambda: launch_environment(home))
            try:
                cgroup = await executor.run(lambda: service_cgroup(sys.platform))
                if cgroup is not None:
                    tools = await executor.run(service_tools)
                    service_home = Path(launch_env["HOME"])
                    probe = ServiceProbe(
                        service_launcher
                        if service_launcher is not None
                        else ProcessLauncher(),
                        tools,
                        os.getuid(),
                        os.getpid(),
                        service_home,
                        service_identity,
                    )
                    report = await probe.inspect(cgroup)
                elif sys.platform == "linux":
                    report = unmanaged_services(os.getuid(), os.getpid())
            except KoraProtocolError as error:
                ctx.logger.emit(SERVICE_REFUSED, reason=error.details["reason"])
                raise
            if sys.platform == "linux":
                report.require_safe(ctx)
                if socket_directory is None:
                    socket_directory = report.socket_directory
            if scanner is None:
                scanner = await executor.run(
                    lambda: NativeIdentityReader(clock=ctx.clock)
                )
            source = (
                peers
                if peers is not None
                else await executor.run(lambda: kernel_source(reader))
            )
            lineage = await executor.run(lambda: lineage_of(reader))
            identity = await executor.run(lambda: own_identity(reader))
            sockets = await executor.run(
                lambda: chosen_socket_directory(socket_directory)
            )
            launch = RunnerLaunch(
                command=tuple(runner_command() if runner is None else runner),
                environment=launch_env,
                socket_directory=sockets,
                identity=identity,
                clock=ctx.clock,
            )
            result = await executor.run(locate, after_uncertain_submission=close_root)
            if result is None:
                raise rejection("state_root_unsafe")
            path, root = result

            def register() -> tuple[DomainRecord, DirectoryAnchor] | LockOutcome:
                nonlocal opened_registry
                registered = open_domain(root, lineage, new_identifier, new_identifier)
                if not isinstance(registered, LockOutcome):
                    opened_registry = registered[1]
                return registered

            domain, directory = await executor.locked(
                register, ctx, "open", after_uncertain_submission=close_registry
            )
            started = ctx.clock.monotonic_ns()

            def open_spool() -> SpoolOwner:
                nonlocal spool
                try:
                    spool = SpoolOwner.open(
                        root, domain.execution_domain_id, identity, reader, ctx.clock
                    )
                except (OSError, FilesystemError):
                    raise refusal("registry_unavailable") from None
                return spool

            owner = await executor.run(
                open_spool, after_uncertain_submission=close_spool_after_join
            )
            ctx.logger.emit_at(
                Level.INFO
                if owner.outcome is SpoolSweepOutcome.COMPLETE
                else Level.WARNING,
                SWEEP,
                outcome=owner.outcome.value,
                waited_ms=max(0, ctx.clock.monotonic_ns() - started) / 1_000_000,
            )
            registry = Registry(directory, domain.execution_domain_id, new_identifier)
            runtime = cls(
                configuration,
                path,
                root,
                registry,
                executor,
                new_identifier,
                launch,
                reader,
                scanner,
                source,
                owner,
            )
        except BaseException:
            try:
                await executor.close()
            finally:
                if executor.joined:
                    try:
                        await close_spool(spool)
                    finally:
                        try:
                            close_registry()
                        finally:
                            close_root()
            raise
        runtime.service_report = report
        return runtime

    def capabilities(self) -> RuntimeCapabilities:
        return self._configuration.capabilities()

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
        self.key_of(handle, ctx)
        if self._closing is not None:
            raise refusal("runtime_closed")
        task = asyncio.create_task(self._status(handle, ctx))
        self._observations.add(task)
        try:
            return await task
        finally:
            self._observations.discard(task)

    async def _status(self, handle: Handle, ctx: ProtocolContext) -> RuntimeStatus:
        key = self.key_of(handle, ctx)
        entry = await self.entry(key)
        if isinstance(entry, Running):
            return await self.observed(entry, ctx)
        return status_cell(entry, ctx.clock.now())

    async def observed(self, entry: Running, ctx: ProtocolContext) -> RuntimeStatus:
        try:
            value = await runner_status(
                entry.socket_path,
                entry.runner,
                self._peers,
                self.executor,
                ctx.clock,
                self._new_identifier(),
            )
        except ListenerRefused as refused:
            ctx.logger.emit(LISTENER_REFUSED, reason=refused.reason)
            raise refusal("runner_unreachable") from None
        except (ChannelError, OSError):
            raise refusal("runner_unreachable") from None
        except KoraProtocolError as error:
            if error.code != "message_refused":
                raise
            raise refusal("runner_unreachable") from None
        return value.status

    async def start(self, handle: Handle, ctx: ProtocolContext) -> None:
        key = self.key_of(handle, ctx)
        if self._closing is not None:
            raise refusal("runtime_closed")
        entry = await self.entry(key)
        if not isinstance(entry, Running):
            raise refusal("runner_starting")
        try:
            await start_command(
                entry.socket_path,
                entry.runner,
                entry.submitter.dispatcher_id,
                self._peers,
                self.executor,
                ctx.clock,
                self._new_identifier(),
            )
        except ListenerRefused:
            raise refusal("runner_unreachable") from None
        except (ChannelError, OSError):
            raise refusal("runner_unreachable") from None
        except KoraProtocolError as error:
            if error.code == "message_refused":
                raise refusal("runner_unreachable") from None
            raise

    async def transfer_in(
        self,
        handle: Handle,
        item: TransferItem,
        chunks: AsyncIterator[bytes],
        ctx: ProtocolContext,
    ) -> None:
        key = self.key_of(handle, ctx)
        if self._closing is not None:
            raise refusal("runtime_closed")
        task = asyncio.create_task(self._transfer(key, item, chunks, ctx))
        self._deliveries.add(task)
        try:
            await task
        finally:
            self._deliveries.discard(task)

    async def _transfer(
        self,
        key: AttemptKey,
        item: TransferItem,
        chunks: AsyncIterator[bytes],
        ctx: ProtocolContext,
    ) -> None:
        clock = ctx.clock
        deadline = clock.monotonic_ns() + MESSAGE_PROGRESS_NS
        entry = await self.entry(key)
        if not isinstance(entry, Running):
            raise refusal("runner_starting")
        if entry.gate_closed:
            raise delivery_error("deadline_expired")
        cutoff = transfer_cutoff(clock, entry.deadline)
        if cutoff is not None:
            deadline = min(deadline, cutoff)
        if clock.monotonic_ns() >= deadline:
            await self._check_transfer_deadline(key, entry, ctx, cutoff)
            raise refusal("runner_unreachable")
        stopped = Event()
        task = asyncio.create_task(
            self._spool_and_send(entry, item, chunks, ctx, deadline, stopped)
        )
        try:
            try:
                await wait_future(task, deadline, clock)
            finally:
                stopped.set()
                task.cancel()
                if await observed(task):
                    raise asyncio.CancelledError
        except (ListenerRefused, ChannelError, OSError, FilesystemError):
            await self._check_transfer_deadline(key, entry, ctx, cutoff)
            raise refusal("runner_unreachable") from None
        except KoraProtocolError as error:
            if error.code != "message_refused":
                raise
            await self._check_transfer_deadline(key, entry, ctx, cutoff)
            raise refusal("runner_unreachable") from None
        await self._check_transfer_deadline(key, entry, ctx, cutoff)

    async def _acquire_spool(
        self, acquire: Callable[[], None], dispose: Callable[[], None]
    ) -> None:
        if self._spool_allocations >= self.executor.admitted:
            raise refusal("registry_busy")
        self._spool_allocations += 1
        try:
            async with self._spool_admission:
                await self.executor.run(acquire, after_uncertain_submission=dispose)
        finally:
            self._spool_allocations -= 1

    def _output_entry(
        self,
        key: AttemptKey,
        clock: Clock,
        expected: Running | None = None,
        captured: int | None = None,
    ) -> Running:
        if self._closing is not None:
            raise refusal("runtime_closed")
        entry = self.registry.entry(key)
        if not isinstance(entry, Running):
            raise refusal("runner_starting")
        if entry.submitter.process != self._launch.identity:
            raise delivery_error("execution_unauthorized")
        if expected is not None and (
            entry.runner != expected.runner
            or entry.submitter != expected.submitter
            or entry.socket_path != expected.socket_path
        ):
            raise delivery_error("submitter_superseded")
        if key in self._expired_outputs or output_deadline_reached(
            entry, clock, captured
        ):
            raise delivery_error("deadline_expired")
        return entry

    async def _current_output(
        self,
        key: AttemptKey,
        ctx: ProtocolContext,
        pull: OutputPull,
        expected: Running | None = None,
        captured: int | None = None,
    ) -> Running:
        try:
            return await self.executor.run(
                lambda: self._output_entry(key, ctx.clock, expected, captured),
                after_uncertain_submission=pull.dispose,
            )
        except KoraProtocolError as error:
            if error.code != "deadline_expired":
                raise
            self._expired_outputs.add(key)
            current = expected
            if current is None:
                found = await self.entry(key)
                if not isinstance(found, Running):
                    raise
                current = found
            await self._settle_output_expiry(key, current, ctx)
            raise

    async def _settle_output_expiry(
        self, key: AttemptKey, entry: Running, ctx: ProtocolContext
    ) -> None:
        self._expired_outputs.add(key)
        await self.transition(
            key,
            lambda current: takeover_cell(current, entry, entry.submitter, True),
            ctx,
            "transfer_out",
        )

    def transfer_out(
        self, handle: Handle, name: str, ctx: ProtocolContext
    ) -> AsyncGenerator[bytes]:
        return self._transfer_out(handle, checked_output_name(name), ctx)

    async def _transfer_out(
        self,
        handle: Handle,
        name: str,
        ctx: ProtocolContext,
    ) -> AsyncGenerator[bytes]:
        key = self.key_of(handle, ctx)
        if self._closing is not None:
            raise refusal("runtime_closed")
        if len(self._output_pulls) >= OUTPUT_PULLS:
            raise delivery_error("workload_busy")
        deadline = ctx.clock.monotonic_ns() + MESSAGE_PROGRESS_NS
        pull = OutputPull()
        self._output_pulls.add(pull)
        try:
            entry, size, captured = await pull.run(
                self._fetch_output(key, name, pull, ctx, deadline),
                ctx.clock,
                deadline,
            )

            def read(length: int, offset: int) -> bytes:
                self._output_entry(key, ctx.clock, entry, captured)
                try:
                    chunk = os.pread(pull.descriptor(), length, offset)
                except OSError:
                    raise refusal("runner_unreachable") from None
                self._output_entry(key, ctx.clock, entry, captured)
                return chunk

            if self._closing is not None:
                raise refusal("runtime_closed")
            offset = 0
            while offset < size:
                if self._closing is not None:
                    raise refusal("runtime_closed")
                try:
                    chunk = await pull.run(
                        self.executor.run(
                            partial(
                                read,
                                min(size - offset, SECTION_FRAME_BYTES),
                                offset,
                            ),
                            after_uncertain_submission=pull.dispose,
                        ),
                        ctx.clock,
                    )
                except KoraProtocolError as error:
                    if error.code != "deadline_expired":
                        raise
                    await self._settle_output_expiry(key, entry, ctx)
                    raise
                if self._closing is not None:
                    raise refusal("runtime_closed")
                if not chunk:
                    raise delivery_error("integrity_error")
                offset += len(chunk)
                yield chunk
        except ChannelError:
            raise refusal("runner_unreachable") from None
        finally:
            if self._closing is None and not pull.uncertain:
                try:
                    pull.dispose()
                except OSError:
                    raise refusal("runner_unreachable") from None
                if pull.lease is None:
                    self._output_pulls.discard(pull)

    async def _fetch_output(
        self,
        key: AttemptKey,
        name: str,
        pull: OutputPull,
        ctx: ProtocolContext,
        deadline: int,
    ) -> tuple[Running, int, int | None]:
        entry = await self._current_output(key, ctx, pull)
        captured = transfer_cutoff(ctx.clock, entry.deadline)

        def active() -> None:
            if self._closing is not None:
                raise refusal("runtime_closed")
            if ctx.clock.monotonic_ns() >= deadline:
                raise ChannelError(ChannelFailure.DEADLINE)

        def open_spool() -> None:
            try:
                pull.lease = self._spool.acquire(active)
            except (OSError, FilesystemError):
                raise refusal("runner_unreachable") from None

        async def acquire() -> None:
            await self._acquire_spool(open_spool, pull.dispose)

        async def check_entry() -> None:
            await self._current_output(key, ctx, pull, entry, captured)

        for _ in range(OUTPUT_ATTEMPTS):
            await check_entry()
            try:
                active()
                await acquire()
                size = await transfer_output(
                    entry.socket_path,
                    entry.runner,
                    entry.submitter.dispatcher_id,
                    self._peers,
                    self.executor,
                    ctx.clock,
                    self._new_identifier(),
                    pull.descriptor(),
                    name,
                    deadline,
                    close_spool=pull.dispose,
                    active=active,
                )
                await check_entry()
                active()
            except ListenerRefused:
                raise delivery_error("execution_unauthorized") from None
            except (ChannelError, OSError):
                try:
                    pull.dispose()
                except OSError:
                    raise refusal("runner_unreachable") from None
                if pull.lease is not None:
                    raise refusal("runner_unreachable") from None
            else:
                return entry, size, captured
        raise refusal("runner_unreachable")

    async def _check_transfer_deadline(
        self, key: AttemptKey, entry: Running, ctx: ProtocolContext, cutoff: int | None
    ) -> None:
        if cutoff is not None and ctx.clock.monotonic_ns() >= cutoff:
            await self.transition(
                key,
                lambda current: takeover_cell(current, entry, entry.submitter, True),
                ctx,
                "transfer_in",
            )

    async def _spool_and_send(
        self,
        entry: Running,
        item: TransferItem,
        chunks: AsyncIterator[bytes],
        ctx: ProtocolContext,
        deadline: int,
        stopped: Event,
    ) -> None:
        lease: SpoolLease | None = None
        caller_owned = True

        def dispose() -> None:
            if lease is not None:
                lease.close()

        def active() -> None:
            if stopped.is_set() or self._closing is not None:
                raise ChannelError(ChannelFailure.CLOSED)
            if ctx.clock.monotonic_ns() >= deadline:
                raise ChannelError(ChannelFailure.DEADLINE)

        def acquire() -> None:
            nonlocal lease
            lease = self._spool.acquire(active)

        def write(spool: int, piece: memoryview) -> None:
            active()
            write_transfer_bytes(spool, piece)

        try:
            await self._acquire_spool(acquire, dispose)
            if lease is None:
                raise RuntimeError("missing spool descriptor")
            spool = lease.descriptor
            total = 0
            async for chunk in chunks:
                total += len(chunk)
                if total > item.size:
                    raise delivery_error("integrity_error")
                view = memoryview(chunk)
                if not view:
                    active()
                    await asyncio.sleep(0)
                    continue
                for offset in range(0, len(view), SECTION_FRAME_BYTES):
                    piece = view[offset : offset + SECTION_FRAME_BYTES]
                    await self.executor.run(
                        partial(write, spool, piece),
                        after_uncertain_submission=dispose,
                    )
            if total != item.size:
                raise delivery_error("integrity_error")
            await transfer_input(
                entry.socket_path,
                entry.runner,
                entry.submitter.dispatcher_id,
                self._peers,
                self.executor,
                ctx.clock,
                self._new_identifier(),
                spool,
                item,
                deadline,
                close_spool=dispose,
                active=active,
            )
        except NativeIOSubmissionUncertain:
            caller_owned = False
            raise
        finally:
            if caller_owned:
                dispose()

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
        self.key(workload.attempt_id, ctx)
        if self._closing is not None:
            raise refusal("runtime_closed")
        key = self.key(workload.attempt_id, ctx)
        digest = await self.executor.run(lambda: self.checked_digest(workload, ctx))
        logical_digest = await self.executor.run(lambda: workload_digest(workload))
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
            workload_digest=logical_digest,
            submitter=Submitter(
                dispatcher_id=workload.dispatcher_id, process=launch.identity
            ),
            nonce=nonce,
            started_at=ctx.clock.now(),
            socket_directory=str(launch.socket_directory),
            runner_prefix=launch.prefix(self._root_path),
            fence=None,
        )
        applied = await self.transition(
            key,
            lambda entry: submit_cell(
                entry,
                digest,
                logical_digest,
                workload.deadline,
                expired(workload.deadline, ctx.clock.now()),
                lambda: starting,
            ),
            ctx,
            "submit",
        )
        if applied.transition.entry is starting:
            await self.spawn(runner_argv(starting.runner_prefix, key, nonce), ctx)
        entry = await self.entry(key)
        if isinstance(entry, Running):
            task = asyncio.create_task(self.deliver(key, entry, workload, ctx))
            self._deliveries.add(task)
            try:
                await task
            finally:
                self._deliveries.discard(task)
        return key.handle()

    def submitter_gone(self, submitter: Submitter) -> bool:
        recorded = submitter.process
        own = self._launch.identity
        if any(
            getattr(recorded, field) is None
            or getattr(recorded, field) != getattr(own, field)
            for field in ("host_id", "boot_id", "pid_namespace")
        ):
            raise refusal("runner_unreachable")
        observation = self._identities.read(recorded.pid)
        if observation.state is IdentityState.ABSENT:
            return True
        process = observation.process
        if observation.state is not IdentityState.PRESENT or process is None:
            raise refusal("runner_unreachable")
        if any(
            getattr(process.identity, field) != getattr(recorded, field)
            for field in ("host_id", "boot_id", "pid_namespace")
        ):
            raise refusal("runner_unreachable")
        return process.dead or process.identity.start_time != recorded.start_time

    async def deliver(
        self, key: AttemptKey, entry: Running, workload: Workload, ctx: ProtocolContext
    ) -> None:
        submitter = Submitter(
            dispatcher_id=workload.dispatcher_id, process=self._launch.identity
        )
        for attempt in range(2):
            await self.transition(
                key,
                lambda current: bind_workspace(
                    current,
                    workload.deadline,
                    expired(workload.deadline, ctx.clock.now()),
                ),
                ctx,
                "submit",
            )
            if entry.submitter == submitter:
                break
            if not await self.executor.run(
                partial(self.submitter_gone, entry.submitter)
            ):
                return

            def take_over(expected: Running = entry) -> Applied | LockOutcome:
                return self.registry.try_transition(
                    key,
                    lambda current: takeover_cell(
                        current,
                        expected,
                        submitter,
                        expired(workload.deadline, ctx.clock.now()),
                    ),
                )

            applied = await self.executor.locked(take_over, ctx, "submit")
            if applied.transition.refusal is None:
                break
            if applied.transition.refusal != "runner_unreachable":
                raise refusal(applied.transition.refusal)
            fresh = await self.entry(key)
            if attempt or not isinstance(fresh, Running):
                raise refusal("runner_unreachable")
            entry = fresh
        try:
            await deliver_workload(
                entry.socket_path,
                entry.runner,
                workload,
                self._peers,
                self.executor,
                ctx.clock,
                self._new_identifier(),
            )
        except ListenerRefused as refused:
            ctx.logger.emit(LISTENER_REFUSED, reason=refused.reason)
            raise refusal("runner_unreachable") from None
        except (ChannelError, OSError):
            raise refusal("runner_unreachable") from None
        except KoraProtocolError as error:
            if error.code != "message_refused":
                raise
            raise refusal("runner_unreachable") from None

    async def spawn(self, argv: tuple[str, ...], ctx: ProtocolContext) -> None:
        started = ctx.clock.monotonic_ns()
        cutoff = launch_deadline(ctx.clock, ctx.deadline)
        outcome = "failed"
        try:
            readiness = await spawn_runner(
                argv,
                self._launch.environment,
                ctx.clock,
                self.launchers,
                deadline_ns=cutoff,
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
                partial(verified, identities, scanner, pid, argv, scan_deadline)
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
            for delivery in self._deliveries:
                delivery.cancel()
            for observation in self._observations:
                observation.cancel()
            for pull in self._output_pulls:
                if pull.pending is not None:
                    pull.pending.cancel()
            self.executor.closed = True
            self.sweeps.closed = True
            self._stopping.set_result(None)
            self._closing = asyncio.create_task(self._release())
        await asyncio.shield(self._closing)

    async def _release(self) -> None:
        try:
            observations = tuple(self._observations)
            deliveries = tuple(self._deliveries)
            outputs = tuple(
                pull.pending for pull in self._output_pulls if pull.pending is not None
            )
            if outputs:
                await asyncio.gather(*outputs, return_exceptions=True)
            if deliveries:
                await asyncio.gather(*deliveries, return_exceptions=True)
            if observations:
                await asyncio.gather(*observations, return_exceptions=True)
            await self.launchers.drain(self._launch.clock)
        finally:
            try:
                await self.executor.close()
            finally:
                try:
                    await self._swept.wait()
                    await self.sweeps.close()
                finally:
                    if self.executor.joined:
                        try:
                            await close_spool(self._spool)
                            self._output_pulls.clear()
                        finally:
                            try:
                                self.registry.directory.close()
                            finally:
                                self._root.close()
