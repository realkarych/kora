import asyncio
import ctypes
import faulthandler
import os
import re
import resource
import secrets
import socket
import sys
import uuid
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from kora.observability import (
    CATALOG,
    ComponentLogger,
    EventSpec,
    FieldKind,
    Level,
    LoggingSettings,
    ProcessDiagnostics,
    configure_process,
)
from kora.plugins.runtime.local.agent import (
    AgentSession,
    AgentSources,
)
from kora.plugins.runtime.local.command import CommandSession
from kora.plugins.runtime.local.installation import (
    InstallationConfiguration,
    read_installation,
)
from kora.plugins.runtime.local.installation import RunnerRefused as RunnerRefused
from kora.plugins.runtime.local.production import production_sources
from kora.plugins.runtime.local.reaper import AdoptedReaper
from kora.plugins.runtime.local.serving import (
    COMPONENT,
    BoundSocket,
    ControlServer,
    bound_socket,
)
from kora.plugins.runtime.local.spawn import completion_frame
from kora.plugins.runtime.local.workload_session import WorkloadSession
from kora.protocols.errors import KoraProtocolError
from kora.protocols.identity import ProcessIdentity
from kora.protocols.secret_value import SecretScope
from kora.protocols.time import Clock, SystemClock
from kora.runner.filesystem import (
    DirectoryAnchor,
    FilesystemError,
    open_local_directory,
    open_private_directory,
    open_private_file,
)
from kora.runner.identity import (
    IdentityState,
    NativeIdentityReader,
    complete_identity,
)
from kora.runner.peers import PeerRefused, kernel_source, same_process
from kora.runner.registry import (
    FIRST_GRANT_ARGUMENT,
    LOCK_DEADLINE_NS,
    LOCK_RETRY_NS,
    READINESS_DESCRIPTOR,
    READY,
    SOCKET_NAME_LENGTH,
    AttemptKey,
    FirstGrantHead,
    LifecycleHead,
    LockOutcome,
    Registry,
    Starting,
    model_bytes,
    original_submitter,
    read_domain,
    register_cell,
    registry_exists,
    socket_path,
)

PR_SET_DUMPABLE: Final[int] = 4
PR_SET_CHILD_SUBREAPER: Final[int] = 36
LISTEN_BACKLOG: Final[int] = 16
EXIT_COMPLETED: Final[int] = 0
EXIT_REFUSED: Final[int] = 3
EXIT_FAILED: Final[int] = 1
TOKEN: Final[re.Pattern[str]] = re.compile(r"[\x21-\x7e]{1,256}")
NONCE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{32}")
STARTED: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "runtime.local.runner.started",
        COMPONENT,
        Level.INFO,
        "the attempt runner opened its state root",
        {},
    )
)
REGISTERED: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "runtime.local.runner.registered",
        COMPONENT,
        Level.INFO,
        "the attempt runner registered and is listening",
        {},
    )
)
COMPLETED: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "runtime.local.runner.completed",
        COMPONENT,
        Level.INFO,
        "the attempt runner exits after normal completion",
        {},
    )
)
REFUSED: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "runtime.local.runner.refused",
        COMPONENT,
        Level.WARNING,
        "the attempt runner exited without registering",
        {"reason": FieldKind.STRING},
    )
)


@dataclass(frozen=True)
class RunnerArguments:
    state_root: Path
    attempt_id: str
    execution_domain_id: str
    nonce: str
    first_grant: bool = False


def parsed_arguments(argv: Sequence[str]) -> RunnerArguments:
    first_grant = len(argv) == 5 and argv[0] == FIRST_GRANT_ARGUMENT
    if first_grant:
        argv = argv[1:]
    if len(argv) != 4:
        raise RunnerRefused("arguments")
    root, attempt, domain, nonce = argv
    if (
        not Path(root).is_absolute()
        or not TOKEN.fullmatch(attempt)
        or not TOKEN.fullmatch(domain)
        or not NONCE.fullmatch(nonce)
    ):
        raise RunnerRefused("arguments")
    return RunnerArguments(Path(root), attempt, domain, nonce, first_grant)


def harden() -> None:
    os.umask(0o077)
    os.chdir("/")
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if sys.platform == "linux":
        library = ctypes.CDLL(None, use_errno=True)
        for option in (PR_SET_CHILD_SUBREAPER, PR_SET_DUMPABLE):
            value = 1 if option == PR_SET_CHILD_SUBREAPER else 0
            if library.prctl(option, value, 0, 0, 0) != 0:
                failure = ctypes.get_errno()
                raise OSError(failure, os.strerror(failure))


def own_identity(reader: NativeIdentityReader) -> ProcessIdentity:
    observation = reader.read(os.getpid())
    process = observation.process
    if (
        observation.state is not IdentityState.PRESENT
        or process is None
        or not complete_identity(process.identity)
    ):
        raise RunnerRefused("identity")
    return process.identity


def private_child(parent: DirectoryAnchor, name: str) -> DirectoryAnchor:
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent.descriptor)
    except FileExistsError:
        pass
    return open_private_directory(parent, name)


def append_only(directory: DirectoryAnchor, name: str) -> int:
    return open_private_file(
        directory, name, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC
    )


def bound_listener(directory: DirectoryAnchor, name: str) -> socket.socket:
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        os.fchdir(directory.descriptor)
        try:
            try:
                os.unlink(name)
            except FileNotFoundError:
                pass
            listener.bind(name)
        finally:
            os.chdir("/")
        listener.listen(LISTEN_BACKLOG)
        return listener
    except BaseException:
        listener.close()
        raise


async def register(
    registry: Registry,
    key: AttemptKey,
    nonce: str,
    identity: ProcessIdentity,
    path: str,
    clock: Clock,
    expected: bytes | None = None,
) -> None:
    def check(head: LifecycleHead | None) -> None:
        if expected is not None and (head is None or model_bytes(head) != expected):
            raise RunnerRefused("fenced")

    deadline = clock.monotonic_ns() + LOCK_DEADLINE_NS
    while True:
        applied = registry.try_transition(
            key, lambda entry: register_cell(entry, nonce, identity, path), check
        )
        if not isinstance(applied, LockOutcome):
            if applied.transition.refusal is not None:
                raise RunnerRefused("fenced")
            return
        if clock.monotonic_ns() >= deadline:
            raise RunnerRefused("lock")
        await clock.sleep_until(clock.monotonic_ns() + LOCK_RETRY_NS)


def signal_pipe(frame: bytes, *, close: bool = False) -> None:
    try:
        os.write(READINESS_DESCRIPTOR, frame)
    except BrokenPipeError:
        pass
    finally:
        if close:
            os.close(READINESS_DESCRIPTOR)


@dataclass
class PreparedRegistration:
    registry: Registry
    key: AttemptKey
    entry: Starting
    name: str
    bound: BoundSocket | None
    resources: ExitStack
    expected: bytes | None = None

    @property
    def path(self) -> str:
        return socket_path(self.entry.socket_directory, self.name)

    async def register(self, identity: ProcessIdentity, clock: Clock) -> None:
        bound = self.bound
        if bound is None:
            raise RunnerRefused("ownership")
        try:
            await register(
                self.registry,
                self.key,
                self.entry.nonce,
                identity,
                self.path,
                clock,
                self.expected,
            )
        except BaseException:
            bound.listener.close()
            os.unlink(self.name, dir_fd=bound.directory.descriptor)
            raise

    def server(
        self,
        clock: Clock,
        reader: NativeIdentityReader,
        identity: ProcessIdentity,
        log: ComponentLogger,
        session: WorkloadSession | None = None,
        agent: AgentSession | None = None,
    ) -> ControlServer:
        bound = self.bound
        if bound is None:
            raise RunnerRefused("ownership")
        if agent is not None:
            agent.bind_launch_socket(Path(self.entry.socket_directory) / self.name)
        server = ControlServer(
            bound,
            Path(self.entry.socket_directory),
            self.name,
            clock,
            kernel_source(reader),
            log,
            self.registry,
            self.key,
            identity,
            reader,
            session=session,
            agent=agent,
            launches=None if agent is None else agent.launches,
        )
        self.bound = None
        return server

    def close(self) -> None:
        bound, self.bound = self.bound, None
        try:
            if bound is not None:
                bound.close()
        finally:
            self.resources.close()


def registration_head(
    registry: Registry, key: AttemptKey, arguments: RunnerArguments
) -> tuple[Starting, bytes | None]:
    head = registry.read(key)
    if head is None or not isinstance(head.entry, Starting):
        raise RunnerRefused("fenced")
    entry = head.entry
    if entry.nonce != arguments.nonce or entry.fence:
        raise RunnerRefused("fenced")
    if not arguments.first_grant:
        return entry, None
    if (
        not isinstance(head, FirstGrantHead)
        or head.first_grant.closure is not None
        or entry.runner_prefix[-2:] != (FIRST_GRANT_ARGUMENT, str(arguments.state_root))
    ):
        raise RunnerRefused("fenced")
    original = registry.linked_grant(key, head).grant
    if original.process is None or not original_submitter(original, entry.submitter):
        raise RunnerRefused("fenced")
    try:
        same_process(NativeIdentityReader(), original.process)
    except PeerRefused:
        raise RunnerRefused("fenced") from None
    return entry, model_bytes(head)


def prepare_registration(arguments: RunnerArguments) -> PreparedRegistration:
    with ExitStack() as resources:
        root = open_local_directory(arguments.state_root)
        resources.callback(root.close)
        domain = read_domain(root)
        if (
            domain is None
            or domain.execution_domain_id != arguments.execution_domain_id
        ):
            raise RunnerRefused("domain")
        registry = Registry(
            open_private_directory(root, "registry"),
            arguments.execution_domain_id,
            lambda: secrets.token_hex(16),
            first_grant=arguments.first_grant,
        )
        resources.callback(registry.directory.close)
        key = registry.key(arguments.attempt_id)
        entry, expected = registration_head(registry, key, arguments)
        name = key.name[:SOCKET_NAME_LENGTH]
        with ExitStack() as binding:
            directory = open_local_directory(Path(entry.socket_directory))
            binding.callback(directory.close)
            listener = bound_listener(directory, name)
            binding.callback(listener.close)
            try:
                bound = bound_socket(listener, directory, name)
            except BaseException:
                listener.close()
                os.unlink(name, dir_fd=directory.descriptor)
                raise
            prepared = PreparedRegistration(
                registry, key, entry, name, bound, resources.pop_all(), expected
            )
            binding.pop_all()
            return prepared


async def supervise(
    arguments: RunnerArguments,
    log: ComponentLogger,
    diagnostics: ProcessDiagnostics,
    sources: AgentSources,
) -> None:
    clock = SystemClock()
    reader = NativeIdentityReader()
    identity = own_identity(reader)
    attempt = attempt_directory(arguments)
    server: ControlServer | None = None
    reaper = AdoptedReaper()
    try:
        prepared = prepare_registration(arguments)
        try:
            await prepared.register(identity, clock)
            log.emit(REGISTERED)
            signal_pipe(READY, close=not arguments.first_grant)
            key = AttemptKey(arguments.execution_domain_id, arguments.attempt_id)
            candidate = str(arguments.state_root / "attempts" / key.name / "candidate")
            command = CommandSession(
                attempt,
                candidate,
                SecretScope(diagnostics),
                clock,
                diagnostics.logger(COMPONENT),
                reaper,
            )
            agent = AgentSession(
                attempt, candidate, diagnostics, clock, reaper, sources, reader
            )
            server = prepared.server(clock, reader, identity, log, command, agent)
            try:
                server.start()
                await server.completed()
            finally:
                try:
                    await server.close()
                finally:
                    if server.resources_settled:
                        await reaper.stop()
        finally:
            if server is None or server.resources_settled:
                prepared.close()
    finally:
        if server is None or server.resources_settled:
            attempt.close()
    if server is not None and server.resources_settled:
        head = server.completion_head
        if head is not None:
            signal_pipe(completion_frame(arguments.nonce, head), close=True)


def attempt_directory(arguments: RunnerArguments) -> DirectoryAnchor:
    root = open_local_directory(arguments.state_root)
    try:
        attempts = private_child(root, "attempts")
        try:
            key = AttemptKey(arguments.execution_domain_id, arguments.attempt_id)
            return private_child(attempts, key.name)
        finally:
            attempts.close()
    finally:
        root.close()


def run(
    argv: Sequence[str],
    compose: Callable[[ProcessDiagnostics, InstallationConfiguration], AgentSources],
) -> int:
    harden()
    try:
        arguments = parsed_arguments(argv)
        if arguments.first_grant:
            os.set_inheritable(READINESS_DESCRIPTOR, False)
            os.set_blocking(READINESS_DESCRIPTOR, False)
        with ExitStack() as resources:
            root = open_local_directory(arguments.state_root)
            resources.callback(root.close)
            if arguments.first_grant or registry_exists(root):
                registry = Registry(
                    open_private_directory(root, "registry"),
                    arguments.execution_domain_id,
                    lambda: secrets.token_hex(16),
                    first_grant=arguments.first_grant,
                )
                resources.callback(registry.directory.close)
                key = registry.key(arguments.attempt_id)
                if arguments.first_grant:
                    domain = read_domain(root)
                    if (
                        domain is None
                        or domain.execution_domain_id != key.execution_domain_id
                    ):
                        raise RunnerRefused("domain")
                    registration_head(registry, key, arguments)
                else:
                    registry.read(key)
        attempt = attempt_directory(arguments)
        try:
            faulthandler.enable(append_only(attempt, "runner.crash"))
            log_descriptor = append_only(attempt, "runner.log")
        finally:
            attempt.close()
    except (RunnerRefused, OSError, FilesystemError, KoraProtocolError):
        return EXIT_FAILED
    diagnostics = configure_process(
        COMPONENT,
        LoggingSettings(),
        uuid.uuid4().hex,
        lambda: datetime.now(UTC),
        descriptor=log_descriptor,
    )
    log = diagnostics.logger(COMPONENT).bind(attempt_id=arguments.attempt_id)
    log.emit(STARTED)
    try:
        root = open_local_directory(arguments.state_root)
        try:
            installation = read_installation(root, diagnostics, arguments.state_root)
        finally:
            root.close()
        asyncio.run(
            supervise(arguments, log, diagnostics, compose(diagnostics, installation))
        )
        log.emit(COMPLETED)
    except RunnerRefused as refusal:
        log.emit(REFUSED, reason=str(refusal.args[0]))
        return EXIT_REFUSED
    except (KoraProtocolError, OSError, FilesystemError) as failure:
        reason = failure.code if isinstance(failure, KoraProtocolError) else "io"
        log.emit(REFUSED, reason=reason)
        return EXIT_FAILED
    finally:
        diagnostics.shutdown()
    return EXIT_COMPLETED


def main() -> None:
    sys.exit(run(sys.argv[1:], production_sources))


if __name__ == "__main__":
    main()
