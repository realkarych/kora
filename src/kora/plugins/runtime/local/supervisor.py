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
from collections.abc import Sequence
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
    configure_process,
)
from kora.plugins.runtime.local.serving import COMPONENT, ControlServer, bound_socket
from kora.protocols.errors import KoraProtocolError
from kora.protocols.identity import ProcessIdentity
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
from kora.runner.peers import kernel_source
from kora.runner.registry import (
    LOCK_DEADLINE_NS,
    LOCK_RETRY_NS,
    READINESS_DESCRIPTOR,
    READY,
    SOCKET_NAME_LENGTH,
    AttemptKey,
    LockOutcome,
    Registry,
    Starting,
    read_domain,
    register_cell,
    socket_path,
)

PR_SET_DUMPABLE: Final[int] = 4
PR_SET_CHILD_SUBREAPER: Final[int] = 36
LISTEN_BACKLOG: Final[int] = 16
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
REFUSED: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "runtime.local.runner.refused",
        COMPONENT,
        Level.WARNING,
        "the attempt runner exited without registering",
        {"reason": FieldKind.STRING},
    )
)


class RunnerRefused(Exception):
    pass


@dataclass(frozen=True)
class RunnerArguments:
    state_root: Path
    attempt_id: str
    execution_domain_id: str
    nonce: str


def parsed_arguments(argv: Sequence[str]) -> RunnerArguments:
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
    return RunnerArguments(Path(root), attempt, domain, nonce)


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
) -> None:
    deadline = clock.monotonic_ns() + LOCK_DEADLINE_NS
    while True:
        applied = registry.try_transition(
            key, lambda entry: register_cell(entry, nonce, identity, path)
        )
        if not isinstance(applied, LockOutcome):
            if applied.transition.refusal is not None:
                raise RunnerRefused("fenced")
            return
        if clock.monotonic_ns() >= deadline:
            raise RunnerRefused("lock")
        await clock.sleep_until(clock.monotonic_ns() + LOCK_RETRY_NS)


def signal_ready() -> None:
    try:
        os.write(READINESS_DESCRIPTOR, READY)
    except BrokenPipeError:
        pass
    finally:
        os.close(READINESS_DESCRIPTOR)


async def supervise(arguments: RunnerArguments, log: ComponentLogger) -> None:
    clock = SystemClock()
    reader = NativeIdentityReader()
    identity = own_identity(reader)
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
        )
        resources.callback(registry.directory.close)
        key = registry.key(arguments.attempt_id)
        entry = registry.entry(key)
        if (
            not isinstance(entry, Starting)
            or entry.nonce != arguments.nonce
            or entry.fence
        ):
            raise RunnerRefused("fenced")
        directory = open_local_directory(Path(entry.socket_directory))
        resources.callback(directory.close)
        name = key.name[:SOCKET_NAME_LENGTH]
        path = socket_path(entry.socket_directory, name)
        listener = bound_listener(directory, name)
        resources.callback(listener.close)
        try:
            bound = bound_socket(listener, directory, name)
            await register(registry, key, arguments.nonce, identity, path, clock)
        except BaseException:
            listener.close()
            os.unlink(name, dir_fd=directory.descriptor)
            raise
        log.emit(REGISTERED)
        signal_ready()
        socket_directory = Path(entry.socket_directory)
        source = kernel_source(reader)
        server = ControlServer(bound, socket_directory, name, clock, source, log)
        try:
            server.start()
            await asyncio.Event().wait()
        finally:
            await server.close()


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


def run(argv: Sequence[str]) -> int:
    harden()
    try:
        arguments = parsed_arguments(argv)
        attempt = attempt_directory(arguments)
        try:
            faulthandler.enable(append_only(attempt, "runner.crash"))
            log_descriptor = append_only(attempt, "runner.log")
        finally:
            attempt.close()
    except (RunnerRefused, OSError, FilesystemError):
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
        asyncio.run(supervise(arguments, log))
    except RunnerRefused as refusal:
        log.emit(REFUSED, reason=str(refusal.args[0]))
        return EXIT_REFUSED
    except (KoraProtocolError, OSError, FilesystemError) as failure:
        reason = failure.code if isinstance(failure, KoraProtocolError) else "io"
        log.emit(REFUSED, reason=reason)
        return EXIT_FAILED
    finally:
        diagnostics.shutdown()
    return EXIT_FAILED


def main() -> None:
    sys.exit(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
