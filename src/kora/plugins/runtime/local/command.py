import asyncio
import os
import secrets
import select
import signal
import sys
import threading
import time
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Final, Literal

from pydantic import MISSING, ValidationError

from kora.ir.config import CommandConfig
from kora.observability import CATALOG, ComponentLogger, EventSpec, FieldKind, Level
from kora.plugins.runtime.local.channel import delivery_error
from kora.plugins.runtime.local.child_owner import ChildOwner
from kora.plugins.runtime.local.environment import candidate_environment
from kora.plugins.runtime.local.spawn import DEFAULT_SIGNALS
from kora.protocols.errors import KoraProtocolError
from kora.protocols.runtime import Workload
from kora.protocols.secret_value import SecretScope, SecretValue
from kora.protocols.time import Clock
from kora.runner.channels import CaptureDrain, cancel_task
from kora.runner.filesystem import (
    DirectoryAnchor,
    FilesystemError,
    open_private_directory,
    verify_private,
)
from kora.runner.journal import SYNC_DEADLINE_NS, RunningJournal, space_error
from kora.runner.registry import Submitter, synchronize

COMPONENT: Final[str] = CATALOG.register_component("runtime.local.runner")
COMMAND: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "runtime.local.command",
        COMPONENT,
        Level.INFO,
        "the attempt runner handled a command",
        {"outcome": FieldKind.STRING, "waited_ms": FieldKind.INTEGER},
    )
)
PREAMBLE: Final[str] = "\n".join(
    (
        "import os, sys",
        "try:",
        "    os.setsid()",
        "    os.chdir(sys.argv[1])",
        "    os.set_inheritable(3, False)",
        "    os.closerange(4, os.sysconf('SC_OPEN_MAX'))",
        "    os.execv(sys.argv[2], sys.argv[2:])",
        "except OSError as error:",
        "    os.write(3, bytes((error.errno & 255,)))",
        "os._exit(127)",
    )
)
OPTIONAL: Final[tuple[str, ...]] = (
    "output_file",
    "resources",
    "resource_set_digest",
    "env",
    "secret_env",
    "network_policy",
    "success_exit_codes",
    "runtime",
    "vcs",
)
TOKEN_BYTES: Final[int] = 32
GRACE_NS: Final[int] = 1_000_000_000
PhaseName = Literal["preparation", "execution", "complete", "failed"]
StateName = Literal["starting", "running", "exited"]


class _Stage(StrEnum):
    IDLE = "idle"
    LAUNCHING = "launching"
    RUNNING = "running"
    FAILED = "failed"
    FENCED = "fenced"


def command_argv(workload: Workload) -> tuple[str, ...] | None:
    if (
        workload.kind != "command"
        or workload.plan_executor != "runner"
        or workload.publish is not None
        or workload.session_spec is not None
        or workload.deadline is not None
        or dict(workload.limits) != {}
        or workload.env
        or workload.secret_refs
        or workload.harness_credentials
        or workload.artifact_inputs
        or workload.resources
    ):
        return None
    try:
        config = CommandConfig.model_validate(dict(workload.resolved_config))
    except ValidationError:
        return None
    if config.extensions:
        return None
    for name in OPTIONAL:
        if getattr(config, name) is not MISSING:
            return None
    return config.cmd


def parked(descriptor: int) -> int:
    while descriptor < 4:
        duplicate = os.dup(descriptor)
        os.close(descriptor)
        descriptor = duplicate
    os.set_inheritable(descriptor, True)
    return descriptor


def observed_exit(status: int) -> int | None:
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return -os.WTERMSIG(status)
    return None


def adopted_pids() -> tuple[int, ...] | None:
    found: list[int] = []
    try:
        tasks = os.listdir("/proc/self/task")
    except OSError:
        return None
    for task in tasks:
        try:
            descriptor = os.open(
                f"/proc/self/task/{task}/children", os.O_RDONLY | os.O_CLOEXEC
            )
        except OSError:
            return None
        try:
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 4096)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            os.close(descriptor)
        try:
            found.extend(int(item) for item in b"".join(chunks).split())
        except ValueError:
            return None
    return tuple(found)


class CommandSession:
    def __init__(
        self,
        attempt: DirectoryAnchor,
        candidate: str,
        scope: SecretScope,
        clock: Clock,
        log: ComponentLogger,
    ) -> None:
        self._attempt = attempt
        self._candidate = candidate
        self._scope = scope
        self._clock = clock
        self._log = log
        self._journal = RunningJournal(attempt, clock)
        self._lock = threading.Condition()
        self._stop = threading.Event()
        self._stage = _Stage.IDLE
        self._execution: Submitter | None = None
        self._owner: ChildOwner | None = None
        self._token: SecretValue | None = None
        self._candidate_ready = False
        self._stdout: int | None = None
        self._stderr: int | None = None
        self._error: int | None = None
        self._drains: list[CaptureDrain] = []
        self._tasks: set[asyncio.Task[None]] = set()
        self._reaper: threading.Thread | None = None
        self._reported_exit = False
        self._running_ns = 0
        self._warned = False

    def note(self, submitter: Submitter) -> None:
        with self._lock:
            if self._execution is None:
                self._execution = submitter

    def prepare(self) -> None:
        if self._stage is _Stage.IDLE and self._owner is None:
            self._owner = ChildOwner()

    def abort_unused(self) -> None:
        with self._lock:
            owner = self._owner
            idle = self._stage is _Stage.IDLE
        if owner is not None and owner.pid is None and idle:
            owner.abort()
            with self._lock:
                if self._owner is owner and self._stage is _Stage.IDLE:
                    self._owner = None

    def attach_drains(self) -> None:
        stdout, stderr = self._stdout, self._stderr
        if stdout is None or stderr is None:
            return
        self._stdout = None
        self._stderr = None
        for descriptor in (stdout, stderr):
            drain = CaptureDrain(descriptor)
            self._drains.append(drain)
            task = asyncio.create_task(self._release_drain(drain))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _release_drain(self, drain: CaptureDrain) -> None:
        await drain.readiness
        drain.close()

    def begin(
        self,
        submitter: Submitter,
        observational: bool,
        workload: Workload | None,
        authorize: Callable[[], object],
    ) -> None:
        started = self._clock.monotonic_ns()
        argv = self._claim(submitter, observational, workload, started)
        if argv is None:
            return
        try:
            self._activate(argv, authorize, started)
        finally:
            self._abandon()

    def status(self) -> tuple[StateName, PhaseName, int | None]:
        with self._lock:
            owner = self._owner
            running = self._stage is _Stage.RUNNING
            report = not self._reported_exit
            running_ns = self._running_ns
        if owner is None or owner.pid is None or not running:
            return "starting", "preparation", None
        if not owner.collected() or owner.status is None:
            return "running", "execution", None
        code = observed_exit(owner.status)
        if code is None:
            return "running", "execution", None
        if report:
            with self._lock:
                if not self._reported_exit:
                    self._reported_exit = True
                    self._emit("exited", running_ns)
        phase: PhaseName = "complete" if code == 0 else "failed"
        return "exited", phase, code

    async def aclose(self) -> None:
        self._stop.set()
        for task in tuple(self._tasks):
            await cancel_task(task)
        for drain in self._drains:
            drain.close()
        self._close_pipes()
        self._journal.close()
        self._scope.release()

    def _claim(
        self,
        submitter: Submitter,
        observational: bool,
        workload: Workload | None,
        started: int,
    ) -> tuple[str, ...] | None:
        with self._lock:
            while self._stage is _Stage.LAUNCHING:
                self._lock.wait()
            if self._stage is _Stage.FENCED:
                self._emit("journal_unavailable", started)
                raise delivery_error("journal_unavailable")
            if self._stage is _Stage.FAILED:
                self._emit("refused", started)
                raise delivery_error("command_not_started")
            execution = self._execution
            if observational or (execution is not None and submitter != execution):
                self._emit("refused", started)
                raise delivery_error("execution_unauthorized")
            if self._stage is _Stage.RUNNING:
                self._emit("started", started)
                return None
            argv = None if workload is None else command_argv(workload)
            if argv is None:
                self._emit("refused", started)
                raise delivery_error("workload_kind_unavailable")
            self._stage = _Stage.LAUNCHING
            if execution is None:
                self._execution = submitter
            return argv

    def _activate(
        self,
        argv: tuple[str, ...],
        authorize: Callable[[], object],
        started: int,
    ) -> None:
        try:
            authorize()
            self._prepare_files()
        except KoraProtocolError:
            self._abort_unspawned()
            self._finish(_Stage.IDLE, "refused", started)
            raise
        except FilesystemError:
            self._abort_unspawned()
            self._finish(_Stage.FAILED, "refused", started)
            raise delivery_error("command_not_started") from None
        except OSError as error:
            self._abort_unspawned()
            if space_error(error):
                self._finish(_Stage.FENCED, "journal_unavailable", started)
                raise delivery_error("journal_unavailable") from None
            self._finish(_Stage.IDLE, "refused", started)
            raise
        owner = self._owner
        if owner is None:
            self._finish(_Stage.FAILED, "refused", started)
            raise delivery_error("command_not_started")
        durable = False
        try:
            try:
                self._spawn(owner, argv)
            except OSError:
                self._finish(_Stage.FAILED, "refused", started)
                raise delivery_error("command_not_started") from None
            self._ensure_reaper(owner)
            if not self._acknowledged():
                self._stop_leader(owner)
                self._finish(_Stage.FAILED, "refused", started)
                raise delivery_error("command_not_started")
            if not self._journal.append("runner.candidate_started", {"leader": True}):
                self._stop_leader(owner)
                self._finish(_Stage.FENCED, "journal_unavailable", started)
                raise delivery_error("journal_unavailable")
            with self._lock:
                self._running_ns = started
            self._finish(_Stage.RUNNING, "started", started)
            durable = True
        finally:
            if not durable:
                self._close_pipes()

    def _abandon(self) -> None:
        with self._lock:
            if self._stage is not _Stage.LAUNCHING:
                return
            self._stage = _Stage.FAILED
            self._lock.notify_all()

    def _finish(self, stage: _Stage, outcome: str, started: int) -> None:
        with self._lock:
            self._stage = stage
            try:
                self._emit(outcome, started)
            finally:
                self._lock.notify_all()

    def _prepare_files(self) -> None:
        if not self._candidate_ready:
            try:
                os.mkdir("candidate", mode=0o700, dir_fd=self._attempt.descriptor)
            except FileExistsError:
                pass
            opened = open_private_directory(self._attempt, "candidate")
            opened.close()
            synchronize(self._attempt.descriptor)
            self._candidate_ready = True
        if self._token is None:
            self._token = self._token_file()

    def _token_file(self) -> SecretValue:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            descriptor = os.open(
                "attempt.token", flags, 0o600, dir_fd=self._attempt.descriptor
            )
        except FileExistsError:
            return self._scope.admit(self._existing_token())
        token = self._scope.admit(secrets.token_hex(16))
        try:
            os.fchmod(descriptor, 0o600)
            verify_private(descriptor, directory=False)
            os.write(descriptor, token.reveal().encode())
            synchronize(descriptor)
        except (OSError, FilesystemError):
            os.close(descriptor)
            self._unlink_token()
            raise
        os.close(descriptor)
        synchronize(self._attempt.descriptor)
        return token

    def _existing_token(self) -> str:
        descriptor = os.open(
            "attempt.token",
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=self._attempt.descriptor,
        )
        try:
            verify_private(descriptor, directory=False)
            raw = os.read(descriptor, TOKEN_BYTES + 1)
        finally:
            os.close(descriptor)
        try:
            text = raw.decode("ascii")
        except UnicodeDecodeError as error:
            raise delivery_error("command_not_started") from error
        if len(text) != TOKEN_BYTES:
            raise delivery_error("command_not_started")
        return text

    def _unlink_token(self) -> None:
        try:
            os.unlink("attempt.token", dir_fd=self._attempt.descriptor)
        except FileNotFoundError:
            return

    def _spawn(self, owner: ChildOwner, argv: tuple[str, ...]) -> None:
        reads: list[int] = []
        writes: list[int] = []
        try:
            for _ in range(3):
                reading, writing = os.pipe()
                reads.append(reading)
                writes.append(parked(writing))
            token = self._token
            if token is None:
                raise delivery_error("command_not_started")
            environment = candidate_environment(
                cwd=Path(self._candidate),
                attempt_token=token,
                workload={},
                secrets={},
                harness_credentials={},
                adapter={},
            )
            try:
                owner.start(lambda: self._posix_spawn(argv, environment, writes))
            finally:
                environment.clear()
            self._stdout, self._stderr, self._error = reads
            reads = []
        finally:
            for descriptor in (*writes, *reads):
                os.close(descriptor)

    def _posix_spawn(
        self,
        argv: tuple[str, ...],
        environment: dict[str, str],
        writes: list[int],
    ) -> int:
        return os.posix_spawn(
            sys.executable,
            [sys.executable, "-I", "-c", PREAMBLE, self._candidate, *argv],
            environment,
            file_actions=[
                (os.POSIX_SPAWN_OPEN, 0, os.devnull, os.O_RDONLY, 0),
                (os.POSIX_SPAWN_DUP2, writes[0], 1),
                (os.POSIX_SPAWN_DUP2, writes[1], 2),
                (os.POSIX_SPAWN_DUP2, writes[2], 3),
            ],
            setsigmask=(),
            setsigdef=DEFAULT_SIGNALS,
        )

    def _acknowledged(self) -> bool:
        descriptor = self._error
        if descriptor is None:
            return False
        self._error = None
        deadline = self._clock.monotonic_ns() + SYNC_DEADLINE_NS
        try:
            remaining_ns = deadline - self._clock.monotonic_ns()
            remaining = max(0.0, remaining_ns / 1_000_000_000)
            ready, _, _ = select.select([descriptor], [], [], remaining)
            if not ready:
                return False
            return os.read(descriptor, 1) == b""
        finally:
            os.close(descriptor)

    def _stop_leader(self, owner: ChildOwner) -> None:
        owner.signal(signal.SIGKILL)

    def _close_pipes(self) -> None:
        descriptors = (self._stdout, self._stderr, self._error)
        self._stdout = None
        self._stderr = None
        self._error = None
        for descriptor in descriptors:
            if descriptor is not None:
                os.close(descriptor)

    def _abort_unspawned(self) -> None:
        owner = self._owner
        if owner is not None and owner.pid is None:
            owner.abort()

    def _ensure_reaper(self, owner: ChildOwner) -> None:
        if self._reaper is not None or sys.platform != "linux":
            return
        self._reaper = threading.Thread(
            target=self._reap, args=(owner,), name="kora-adopted", daemon=True
        )
        self._reaper.start()

    def _reap(self, owner: ChildOwner) -> None:
        seen: dict[int, int] = {}
        while not self._stop.wait(1.0):
            listed = adopted_pids()
            if listed is None:
                continue
            now = time.monotonic_ns()
            for pid in listed:
                if pid != owner.pid:
                    seen.setdefault(pid, now)
            for pid, first in tuple(seen.items()):
                if now - first < GRACE_NS:
                    continue
                try:
                    reaped, _status = os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    seen.pop(pid, None)
                    continue
                if reaped == pid:
                    seen.pop(pid, None)

    def _emit(self, outcome: str, started: int) -> None:
        if outcome == "journal_unavailable":
            if self._warned:
                return
            self._warned = True
        waited = max(0, (self._clock.monotonic_ns() - started) // 1_000_000)
        level = Level.INFO if outcome in {"started", "exited"} else Level.WARNING
        self._log.emit_at(level, COMMAND, outcome=outcome, waited_ms=waited)
