import asyncio
import os
import secrets
import select
import signal
import stat
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, Literal

from pydantic import MISSING, ValidationError

from kora.ir.config import CommandConfig
from kora.ir.json import JsonObject, JsonShapeError, JsonValue
from kora.observability import CATALOG, ComponentLogger, EventSpec, FieldKind, Level
from kora.plugins.runtime.local.channel import delivery_error
from kora.plugins.runtime.local.child_owner import ChildOwner
from kora.plugins.runtime.local.command_stop import CommandDomainStop, StopOutcome
from kora.plugins.runtime.local.environment import candidate_environment
from kora.plugins.runtime.local.spawn import DEFAULT_SIGNALS
from kora.protocols.errors import KoraProtocolError
from kora.protocols.runtime import TransferItem, Workload
from kora.protocols.secret_value import SecretScope, SecretValue
from kora.protocols.time import Clock
from kora.runner.channels import CaptureDrain, cancel_task
from kora.runner.filesystem import (
    MAX_SEAL_ENTRIES,
    DirectoryAnchor,
    FilesystemError,
    SealCapacityError,
    SealedTree,
    create_directory,
    freeze_private_tree,
    open_private_directory,
    remove_directory,
    rename_no_replace,
    snapshot_regular_tree,
    verify_private,
)
from kora.runner.identity import PROCESS_SCAN_ENTRIES, SCAN_NS, NativeIdentityReader
from kora.runner.journal import (
    SYNC_DEADLINE_NS,
    JournalAppendOutcome,
    JournalCapacityError,
    RunningJournal,
    space_error,
)
from kora.runner.native_io import NativeExecutor, NativeIOUnresolved, observed
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
INCOMING: Final[str] = ".incoming\x7f"
TRANSFER: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "runtime.local.transfer",
        COMPONENT,
        Level.INFO,
        "the attempt runner handled an input transfer",
        {"outcome": FieldKind.STRING, "waited_ms": FieldKind.INTEGER},
    )
)
COMMAND_STOP: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "runtime.local.command_stop",
        COMPONENT,
        Level.INFO,
        "the attempt runner settled command-domain stop",
        {"outcome": FieldKind.STRING, "waited_ms": FieldKind.INTEGER},
    )
)
SEAL: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "runtime.local.seal",
        COMPONENT,
        Level.INFO,
        "the attempt runner sealed a command output snapshot",
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
SNAPSHOT_NAME: Final[str] = "output-snapshot"
GRACE_NS: Final[int] = 1_000_000_000
ADOPTED_LIST_BYTES: Final[int] = PROCESS_SCAN_ENTRIES * 11 + 1
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
    found: set[int] = set()
    try:
        with os.scandir("/proc/self/task") as tasks:
            for index, task in enumerate(tasks):
                if index >= PROCESS_SCAN_ENTRIES:
                    return None
                with open(f"/proc/self/task/{task.name}/children", "rb") as source:
                    children = source.read(ADOPTED_LIST_BYTES)
                if len(children) >= ADOPTED_LIST_BYTES:
                    return None
                found.update(int(item) for item in children.split())
                if len(found) > PROCESS_SCAN_ENTRIES:
                    return None
    except (OSError, ValueError):
        return None
    return tuple(found)


@dataclass(frozen=True)
class Incoming:
    directory: DirectoryAnchor
    descriptor: int


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
        self._loop = asyncio.get_running_loop()
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
        self._reap_stop = threading.Event()
        self._reap_wake = threading.Event()
        self._domain_stop: asyncio.Task[StopOutcome] | None = None
        self._prelaunch: dict[int, int] | None = None
        self._closing: asyncio.Task[None] | None = None
        self._reported_exit = False
        self._exit_recorded: bool | None = None
        self._running_ns = 0
        self._warned = False
        self._inputs: dict[str, str] = {}
        self._transferring = False
        self._transfer_ns = 0
        self._sealed = False
        self._output_seal: asyncio.Task[None] | None = None

    def capture_prelaunch(
        self, baseline: dict[int, int], workload: Workload, deadline: int
    ) -> None:
        cutoff = min(deadline, self._clock.monotonic_ns() + SCAN_NS)
        if (
            self._clock.monotonic_ns() < cutoff
            and command_argv(workload) is not None
            and self._clock.monotonic_ns() < cutoff
        ):
            NativeIdentityReader(clock=self._clock).command_prelaunch(baseline, cutoff)

    def note(
        self, submitter: Submitter, prelaunch: dict[int, int] | None = None
    ) -> None:
        with self._lock:
            if self._execution is None:
                self._execution = submitter
                self._prelaunch = prelaunch or None

    def decide_transfer(
        self, submitter: Submitter, observational: bool, item: TransferItem
    ) -> Literal["write", "repeat", "conflict"]:
        started = self._clock.monotonic_ns()
        try:
            return self._transfer_decision(submitter, observational, item, started)
        except KoraProtocolError as error:
            outcome = (
                "journal_unavailable"
                if error.code == "journal_unavailable"
                else "refused"
            )
            self._emit_transfer(outcome, started)
            raise

    def open_incoming(self) -> Incoming:
        try:
            os.mkdir("input", mode=0o700, dir_fd=self._attempt.descriptor)
        except FileExistsError:
            pass
        directory = open_private_directory(self._attempt, "input")
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            synchronize(self._attempt.descriptor)
            self._unlink_input(directory, INCOMING)
            descriptor = os.open(INCOMING, flags, 0o600, dir_fd=directory.descriptor)
        except OSError:
            directory.close()
            raise
        return Incoming(directory, descriptor)

    def discard_incoming(self, incoming: Incoming) -> None:
        try:
            try:
                os.close(incoming.descriptor)
            finally:
                self._unlink_input(incoming.directory, INCOMING)
        finally:
            incoming.directory.close()

    def finish_incoming(
        self,
        incoming: Incoming,
        item: TransferItem,
        digest: str,
        authorize: Callable[[], object],
    ) -> None:
        authorize()
        if digest != item.sha256:
            self._unlink_input(incoming.directory, INCOMING)
            authorize()
            self._reject(item)
            raise delivery_error("integrity_error")
        directory = incoming.directory
        created = False
        committed = False
        try:
            os.fchmod(incoming.descriptor, 0o400)
            synchronize(incoming.descriptor)
            authorize()
            try:
                rename_no_replace(directory, INCOMING, item.name)
            except FileExistsError:
                entry = os.stat(
                    item.name, dir_fd=directory.descriptor, follow_symlinks=False
                )
                code = (
                    "workload_conflict"
                    if stat.S_ISREG(entry.st_mode) and entry.st_nlink == 1
                    else "transfer_refused"
                )
                raise delivery_error(code) from None
            created = True
            synchronize(directory.descriptor)
            authorize()
            self._record_input(item)
            committed = True
        except OSError as error:
            if space_error(error):
                self.fence_transfer()
            raise
        finally:
            if created and not committed:
                self._unlink_input(directory, item.name)
                synchronize(directory.descriptor)

    def complete_seen(
        self,
        decision: Literal["repeat", "conflict"],
        item: TransferItem,
        digest: str,
    ) -> None:
        if decision == "conflict":
            self._emit_transfer("refused")
            raise delivery_error("workload_conflict")
        if digest != item.sha256:
            self._reject(item)
            raise delivery_error("integrity_error")

    def end_transfer(self) -> None:
        with self._lock:
            self._transferring = False

    def fence_transfer(self) -> None:
        with self._lock:
            if self._stage is _Stage.FENCED:
                return
            self._stage = _Stage.FENCED
            self._discard_unlaunched_prelaunch()
            closed = self._journal.closed
        if not closed:
            self._journal.close()
        self._emit_transfer("journal_unavailable")

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

    async def _seal_confirmed(self) -> None:
        started = self._clock.monotonic_ns()
        executor = NativeExecutor(1, 1)
        unresolved = False
        try:
            await executor.run(lambda: self._write_snapshot(started))
        except NativeIOUnresolved:
            unresolved = True
        finally:
            try:
                await executor.close()
            except NativeIOUnresolved:
                unresolved = True
        if unresolved:
            self._finish_seal("refused", started)

    def _snapshot_ready(self) -> bool:
        return (
            self._exit_recorded is not None
            or self._stage not in (_Stage.LAUNCHING, _Stage.RUNNING)
            or self._journal.closed
            or self._stop.is_set()
        )

    def _write_snapshot(self, started: int) -> None:
        with self._lock:
            self._lock.wait_for(self._snapshot_ready)
            ran = self._stage is _Stage.RUNNING
            recorded = self._exit_recorded
        if not ran:
            return
        if not recorded or self._journal.closed:
            self._finish_seal("journal_unavailable", started)
            return
        source: DirectoryAnchor | None = None
        snapshot: DirectoryAnchor | None = None
        retained = False
        outcome = "refused"
        try:
            try:
                source = open_private_directory(self._attempt, "candidate")
                snapshot = create_directory(self._attempt, SNAPSHOT_NAME)
                if snapshot.identity.device != self._attempt.identity.device:
                    raise FilesystemError("snapshot_device_mismatch")
                sealed = snapshot_regular_tree(source, snapshot)
                payload = self._seal_payload(sealed)
                self._finalize_snapshot(snapshot)
            except (SealCapacityError, RecursionError, MemoryError):
                pass
            except (OSError, FilesystemError) as error:
                if isinstance(error, OSError) and space_error(error):
                    self._journal.close()
                    outcome = "journal_unavailable"
            else:
                retained = True
                try:
                    result = self._journal.append_outcome(
                        "runner.outputs_sealed", payload, reader_bounded=True
                    )
                except (JournalCapacityError, JsonShapeError, UnicodeEncodeError):
                    retained = False
                else:
                    retained = result is not JournalAppendOutcome.ABSENT
                    outcome = (
                        "sealed"
                        if result is JournalAppendOutcome.DURABLE
                        else "journal_unavailable"
                    )
        finally:
            cleanup: OSError | FilesystemError | None = None
            if source is not None:
                try:
                    source.close()
                except OSError as error:
                    cleanup = error
            if snapshot is not None:
                try:
                    if not retained:
                        remove_directory(
                            self._attempt,
                            snapshot.identity,
                            max_entries=MAX_SEAL_ENTRIES,
                            restore_owner_write=True,
                        )
                except (OSError, FilesystemError) as error:
                    if cleanup is None:
                        cleanup = error
                finally:
                    try:
                        snapshot.close()
                    except OSError as error:
                        if cleanup is None:
                            cleanup = error
            if cleanup is not None:
                raise cleanup
        self._finish_seal(outcome, started)

    def _finalize_snapshot(self, snapshot: DirectoryAnchor) -> None:
        freeze_private_tree(snapshot)
        synchronize(self._attempt.descriptor)

    def _seal_payload(self, sealed: SealedTree) -> JsonObject:
        files: list[JsonValue] = [
            {"name": item.name, "sha256": item.sha256} for item in sealed.files
        ]
        refused: list[JsonValue] = list(sealed.refused)
        return {"files": files, "refused": refused}

    def _finish_seal(self, outcome: str, started: int) -> None:
        with self._lock:
            if self._sealed:
                return
            self._sealed = True
        waited = max(0, (self._clock.monotonic_ns() - started) // 1_000_000)
        level = Level.INFO if outcome == "sealed" else Level.WARNING
        self._log.emit_at(level, SEAL, outcome=outcome, waited_ms=waited)

    def _record_exit(self, status: int) -> None:
        code = observed_exit(status)
        if code is None:
            return
        try:
            self._loop.call_soon_threadsafe(
                self._begin_domain_stop, self._clock.monotonic_ns()
            )
        except RuntimeError:
            if not self._loop.is_closed():
                raise
        with self._lock:
            self._lock.wait_for(
                lambda: self._stage is not _Stage.LAUNCHING or self._stop.is_set()
            )
            if self._stage is not _Stage.RUNNING or self._stop.is_set():
                return
            started = self._running_ns
        recorded = False
        try:
            recorded = self._journal.append("command_exited", {"exit_code": code})
        finally:
            with self._lock:
                self._exit_recorded = recorded
                self._lock.notify_all()
        if not recorded:
            with self._lock:
                if not self._stop.is_set():
                    self._emit("journal_unavailable", started)

    def _begin_domain_stop(self, started_ns: int) -> None:
        owner = self._owner
        token = self._token
        if (
            self._stop.is_set()
            or self._domain_stop is not None
            or owner is None
            or token is None
        ):
            return
        with owner.lock:
            pid = owner.pid
            status = owner.status
        if pid is None or status is None or observed_exit(status) is None:
            return
        with self._lock:
            prelaunch, self._prelaunch = self._prelaunch, None
        self._domain_stop = self._loop.create_task(
            self._stop_domain(pid, token, started_ns, prelaunch)
        )

    async def _stop_domain(
        self,
        pid: int,
        token: SecretValue,
        started_ns: int,
        prelaunch: dict[int, int] | None,
    ) -> StopOutcome:
        marker = b"KORA_ATTEMPT_TOKEN=" + token.reveal().encode("ascii")
        executor = NativeExecutor(1, 1)
        try:
            outcome = await executor.run(
                lambda: CommandDomainStop(
                    pid, marker, self._clock, self._reap_wake.set, prelaunch
                ).stop(started_ns)
            )
        except NativeIOUnresolved:
            outcome = StopOutcome.UNRESOLVED
        finally:
            try:
                await executor.close()
            except NativeIOUnresolved:
                outcome = StopOutcome.UNRESOLVED
        waited = max(0, (self._clock.monotonic_ns() - started_ns) // 1_000_000)
        level = Level.INFO if outcome is StopOutcome.CONFIRMED else Level.WARNING
        self._log.emit_at(level, COMMAND_STOP, outcome=outcome, waited_ms=waited)
        if outcome is StopOutcome.CONFIRMED:
            self._output_seal = self._loop.create_task(self._seal_confirmed())
        return outcome

    def close_journal(self) -> None:
        self._journal.close()
        with self._lock:
            self._lock.notify_all()

    async def aclose(self) -> None:
        if self._closing is None:
            self._closing = self._loop.create_task(self._release())
        cancelled = await observed(self._closing)
        self._closing.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _release(self) -> None:
        self.close_journal()
        self._begin_domain_stop(self._clock.monotonic_ns())
        with self._lock:
            self._prelaunch = None
            self._stop.set()
            self._lock.notify_all()
        try:
            if self._domain_stop is not None:
                await self._domain_stop
            if self._output_seal is not None:
                await self._output_seal
        finally:
            self._reap_stop.set()
            self._reap_wake.set()
            try:
                if self._reaper is not None:
                    await self._loop.run_in_executor(None, self._reaper.join)
            finally:
                for task in tuple(self._tasks):
                    await cancel_task(task)
                for drain in self._drains:
                    drain.close()
                self._close_pipes()
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
            if self._transferring:
                self._emit("refused", started)
                raise delivery_error("workload_busy")
            if self._journal.closed:
                self._discard_unlaunched_prelaunch()
                self._emit("journal_unavailable", started)
                raise delivery_error("journal_unavailable")
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
        except KoraProtocolError as error:
            if error.code == "deadline_expired":
                with self._lock:
                    self._discard_unlaunched_prelaunch()
            self._finish(_Stage.IDLE, "refused", started)
            raise
        except FilesystemError:
            self._finish(_Stage.FAILED, "refused", started)
            raise delivery_error("command_not_started") from None
        except OSError as error:
            if space_error(error):
                self._finish(_Stage.FENCED, "journal_unavailable", started)
                raise delivery_error("journal_unavailable") from None
            self._finish(_Stage.IDLE, "refused", started)
            raise
        owner = ChildOwner(loop=self._loop, on_exit=self._record_exit)
        with self._lock:
            self._owner = owner
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
                owner.abort()
                self._close_pipes()

    def _abandon(self) -> None:
        with self._lock:
            if self._stage is not _Stage.LAUNCHING:
                return
            self._stage = _Stage.FAILED
            self._discard_unlaunched_prelaunch()
            self._lock.notify_all()

    def _discard_unlaunched_prelaunch(self) -> None:
        if self._owner is None or self._owner.pid is None:
            self._prelaunch = None

    def _finish(self, stage: _Stage, outcome: str, started: int) -> None:
        with self._lock:
            self._stage = stage
            if stage in (_Stage.FAILED, _Stage.FENCED):
                self._discard_unlaunched_prelaunch()
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

    def _ensure_reaper(self, owner: ChildOwner) -> None:
        if self._reaper is not None or sys.platform != "linux":
            return
        self._reaper = threading.Thread(
            target=self._reap, args=(owner,), name="kora-adopted", daemon=True
        )
        self._reaper.start()

    def _reap(self, owner: ChildOwner) -> None:
        seen: dict[int, int] = {}
        while not self._reap_stop.is_set():
            self._reap_wake.wait(1.0)
            self._reap_wake.clear()
            if self._reap_stop.is_set():
                return
            listed = adopted_pids()
            if listed is None:
                continue
            now = time.monotonic_ns()
            with owner.lock:
                leader = None if owner.reaped else owner.pid
            seen = {pid: seen.get(pid, now) for pid in listed if pid != leader}
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

    def _transfer_decision(
        self,
        submitter: Submitter,
        observational: bool,
        item: TransferItem,
        started: int,
    ) -> Literal["write", "repeat", "conflict"]:
        with self._lock:
            if self._stage is _Stage.FENCED:
                raise delivery_error("journal_unavailable")
            if observational or self._execution != submitter:
                raise delivery_error("execution_unauthorized")
            if self._journal.closed:
                raise delivery_error("journal_unavailable")
            if (
                item.kind != "input"
                or self._stage is not _Stage.IDLE
                or self._transferring
            ):
                raise delivery_error("transfer_refused")
            self._transferring = True
            self._transfer_ns = started
            previous = self._inputs.get(item.name)
            if previous == item.sha256:
                return "repeat"
            if previous is not None:
                return "conflict"
            return "write"

    def _append_transfer(self, kind: str, item: TransferItem) -> None:
        try:
            if self._journal.append(kind, {"name": item.name, "sha256": item.sha256}):
                return
        except FilesystemError:
            pass
        self.fence_transfer()
        raise delivery_error("journal_unavailable")

    def _record_input(self, item: TransferItem) -> None:
        self._append_transfer("transfer.materialised", item)
        with self._lock:
            self._inputs[item.name] = item.sha256
        self._emit_transfer("materialised")

    def _reject(self, item: TransferItem) -> None:
        self._append_transfer("transfer.rejected", item)
        self._emit_transfer("rejected")

    def _unlink_input(self, directory: DirectoryAnchor, name: str) -> None:
        try:
            os.unlink(name, dir_fd=directory.descriptor)
        except FileNotFoundError:
            return

    def _emit_transfer(self, outcome: str, started: int | None = None) -> None:
        origin = self._transfer_ns if started is None else started
        waited = max(0, (self._clock.monotonic_ns() - origin) // 1_000_000)
        level = Level.INFO if outcome == "materialised" else Level.WARNING
        self._log.emit_at(level, TRANSFER, outcome=outcome, waited_ms=waited)

    def _emit(self, outcome: str, started: int) -> None:
        if outcome == "journal_unavailable":
            if self._warned:
                return
            self._warned = True
        waited = max(0, (self._clock.monotonic_ns() - started) // 1_000_000)
        level = Level.INFO if outcome in {"started", "exited"} else Level.WARNING
        self._log.emit_at(level, COMMAND, outcome=outcome, waited_ms=waited)
