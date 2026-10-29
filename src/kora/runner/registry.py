import asyncio
import fcntl
import hashlib
import json
import os
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Annotated, Final, Literal, Self, TypeVar, cast

from pydantic import (
    AfterValidator,
    Discriminator,
    Field,
    StrictBool,
    StringConstraints,
    model_validator,
)

from kora.ir.json import JsonObjectField, parse_json_object
from kora.ir.scalars import Digest
from kora.ir.vcs_ref_json import canonical_bytes
from kora.observability import CATALOG, EventSpec, FieldKind, Level
from kora.protocols.boundary import BoundaryModel
from kora.protocols.common import ProtocolContext
from kora.protocols.deadlines import ExecutionDeadline
from kora.protocols.errors import KoraProtocolError
from kora.protocols.evidence import (
    EvidenceScope,
    NormalCompletionQuiescence,
    TerminationReceipt,
)
from kora.protocols.identity import Handle, Identifier, Instant, ProcessIdentity
from kora.protocols.runtime import RuntimePhase, RuntimeStatus
from kora.runner.filesystem import (
    DirectoryAnchor,
    FilesystemError,
    create_directory,
    open_private_directory,
    open_private_file,
    read_private_file,
    rename_no_replace,
)
from kora.runner.identity import IdentityReader

HEAD_LIMIT: Final[int] = 65536
PREFIX_ARGUMENTS: Final[int] = 64
PREFIX_BYTES: Final[int] = 16384
SOCKET_PATH_LENGTH: Final[int] = 103
SOCKET_NAME_LENGTH: Final[int] = 16
READINESS_DESCRIPTOR: Final[int] = 3
READY: Final[bytes] = b"R"
NAME_LENGTH: Final[int] = 32
RUNTIME_NAME: Final[str] = "local"
HANDLE_FORMAT: Final[str] = "local/1"
F_FULLFSYNC: Final[int] = 51
LOCK_DEADLINE_NS: Final[int] = 2_000_000_000
LOCK_RETRY_NS: Final[int] = 10_000_000
WORKERS: Final[int] = 4
ADMITTED_JOBS: Final[int] = 64
Result = TypeVar("Result")
COMPONENT: Final[str] = CATALOG.register_component("runtime.local")
LOCK_TIMEOUT: Final[EventSpec] = CATALOG.register(
    EventSpec(
        "runtime.local.lock_timeout",
        COMPONENT,
        Level.WARNING,
        "a local runtime lock was not acquired within its deadline",
        {"operation": FieldKind.STRING, "waited_ms": FieldKind.FLOAT},
    )
)

Nonce = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{32}$")]
SocketPath = Annotated[
    str, StringConstraints(strict=True, min_length=1, max_length=SOCKET_PATH_LENGTH)
]


def bounded_prefix(prefix: tuple[str, ...]) -> tuple[str, ...]:
    encoded = sum(len(os.fsencode(argument)) + 1 for argument in prefix)
    if (
        encoded > PREFIX_BYTES
        or not os.path.isabs(prefix[-1])
        or any("\0" in argument for argument in prefix)
    ):
        raise ValueError("the runner prefix is not a bounded command and root")
    return prefix


RunnerPrefix = Annotated[
    tuple[Annotated[str, StringConstraints(strict=True, min_length=1)], ...],
    Field(min_length=1, max_length=PREFIX_ARGUMENTS),
    AfterValidator(bounded_prefix),
]

MESSAGES: Final[dict[str, str]] = {
    "registry_busy": "the attempt registry lock was not acquired within its deadline",
    "registry_unavailable": "the attempt registry could not be written",
    "runtime_closed": "the local runtime is closed",
    "lineage_unavailable": "the execution-domain registry lineage is unavailable",
    "retirement_unavailable": "retirement of a started runner is not available yet",
    "runner_stop_unconfirmed": "a runner of the attempt was not confirmed stopped",
    "runner_starting": "the attempt runner has not registered yet",
    "runner_unreachable": "the attempt runner cannot be observed yet",
    "attempt_not_submitted": "the attempt has no submitted handle",
    "workload_conflict": "the request conflicts with the attempt's recorded binding",
    "attempt_retired": "the attempt is retired",
    "deadline_expired": "the attempt's inherited deadline has expired",
}
RETRYABLE: Final[frozenset[str]] = frozenset(
    {
        "registry_busy",
        "registry_unavailable",
        "lineage_unavailable",
        "retirement_unavailable",
        "runner_stop_unconfirmed",
        "runner_starting",
        "runner_unreachable",
    }
)


def refusal(code: str) -> KoraProtocolError:
    return KoraProtocolError(code, MESSAGES[code], code in RETRYABLE, {})


def socket_path(directory: str, name: str) -> str:
    return f"{directory}/{name[:SOCKET_NAME_LENGTH]}"


def attempt_name(execution_domain_id: str, attempt_id: str) -> str:
    encoded = json.dumps(
        [execution_domain_id, attempt_id], ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:NAME_LENGTH]


class Fence(BoundaryModel):
    retirement_id: Identifier
    fenced_at: Instant


class Submitter(BoundaryModel):
    dispatcher_id: Identifier
    process: ProcessIdentity


class Unsubmitted(BoundaryModel):
    state: Literal["unsubmitted"]
    deadline: ExecutionDeadline | None
    gate_closed: StrictBool


class Starting(BoundaryModel):
    state: Literal["starting"]
    deadline: ExecutionDeadline | None
    gate_closed: StrictBool
    request_digest: Digest
    submitter: Submitter
    nonce: Nonce
    started_at: Instant
    socket_directory: SocketPath
    runner_prefix: RunnerPrefix
    fence: Fence | None


class Running(BoundaryModel):
    state: Literal["running"]
    deadline: ExecutionDeadline | None
    gate_closed: StrictBool
    request_digest: Digest
    submitter: Submitter
    runner: ProcessIdentity
    socket_path: SocketPath


class Completed(BoundaryModel):
    state: Literal["completed"]
    deadline: ExecutionDeadline | None
    gate_closed: StrictBool
    request_digest: Digest
    runner: ProcessIdentity
    quiescence: NormalCompletionQuiescence
    fence: Fence
    released_at: Instant | None


class Retiring(BoundaryModel):
    state: Literal["retiring"]
    runner: ProcessIdentity
    fence: Fence


class Retired(BoundaryModel):
    state: Literal["retired"]
    runner: ProcessIdentity | None
    receipt: TerminationReceipt
    phase: RuntimePhase
    normal_completion: NormalCompletionQuiescence | None
    released_at: Instant | None


Entry = Annotated[
    Unsubmitted | Starting | Running | Completed | Retiring | Retired,
    Discriminator("state"),
]
Bound = Unsubmitted | Starting | Running | Completed


class RegistryHead(BoundaryModel):
    format: Literal["local/1"]
    execution_domain_id: Identifier
    attempt_id: Identifier
    entry: Entry

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if len(model_bytes(self)) > HEAD_LIMIT:
            raise ValueError("the registry head exceeds its bound")
        return self


def model_bytes(model: BoundaryModel) -> bytes:
    return canonical_bytes(parse_json_object(model.model_dump(mode="json")))


def with_entry(key: "AttemptKey", entry: Entry) -> bytes:
    try:
        head = RegistryHead(
            format="local/1",
            execution_domain_id=key.execution_domain_id,
            attempt_id=key.attempt_id,
            entry=entry,
        )
    except ValueError:
        raise refusal("lineage_unavailable") from None
    return model_bytes(head)


@dataclass(frozen=True)
class AttemptKey:
    execution_domain_id: str
    attempt_id: str

    @property
    def name(self) -> str:
        return attempt_name(self.execution_domain_id, self.attempt_id)

    def handle(self) -> Handle:
        return Handle(
            runtime=RUNTIME_NAME,
            attempt_id=self.attempt_id,
            execution_domain_id=self.execution_domain_id,
            host_hint=None,
            data=JsonObjectField({"format": HANDLE_FORMAT}),
        )


def runner_argv(
    prefix: tuple[str, ...], key: AttemptKey, nonce: str
) -> tuple[str, ...]:
    return (*prefix, key.attempt_id, key.execution_domain_id, nonce)


@dataclass(frozen=True)
class Transition:
    entry: Entry | None = None
    refusal: str | None = None


def released(entry: Entry) -> bool:
    return isinstance(entry, Completed) and entry.released_at is not None


def closed_gate(entry: Bound) -> Bound:
    return type(entry).model_validate({**entry.model_dump(), "gate_closed": True})


def bind_workspace(
    entry: Entry | None, deadline: ExecutionDeadline | None, expired: bool
) -> Transition:
    if entry is None:
        written = Unsubmitted(
            state="unsubmitted", deadline=deadline, gate_closed=expired
        )
        return Transition(written, "deadline_expired" if expired else None)
    if isinstance(entry, Retiring | Retired) or released(entry):
        return Transition(refusal="attempt_retired")
    if entry.deadline != deadline:
        return Transition(refusal="workload_conflict")
    if entry.gate_closed:
        return Transition(refusal="deadline_expired")
    if expired:
        return Transition(closed_gate(entry), "deadline_expired")
    return Transition()


def submit_cell(
    entry: Entry | None,
    request_digest: str,
    deadline: ExecutionDeadline | None,
    expired: bool,
    starting: Callable[[], Starting],
) -> Transition:
    if entry is None:
        if expired:
            return bind_workspace(entry, deadline, expired)
        return Transition(starting())
    if isinstance(entry, Retiring | Retired) or released(entry):
        return Transition(refusal="attempt_retired")
    if entry.deadline != deadline:
        return Transition(refusal="workload_conflict")
    if entry.gate_closed:
        return Transition(refusal="deadline_expired")
    if not isinstance(entry, Unsubmitted) and entry.request_digest != request_digest:
        return Transition(refusal="workload_conflict")
    if expired and isinstance(entry, Unsubmitted | Starting):
        return Transition(closed_gate(entry), "deadline_expired")
    if isinstance(entry, Unsubmitted):
        return Transition(starting())
    if isinstance(entry, Starting):
        return Transition(refusal="runner_starting")
    return Transition()


def register_cell(
    entry: Entry | None, nonce: str, runner: ProcessIdentity, socket_path: str
) -> Transition:
    if not isinstance(entry, Starting) or entry.nonce != nonce or entry.fence:
        return Transition(refusal="registration_refused")
    running = Running(
        state="running",
        deadline=entry.deadline,
        gate_closed=entry.gate_closed,
        request_digest=entry.request_digest,
        submitter=entry.submitter,
        runner=runner,
        socket_path=socket_path,
    )
    return Transition(running)


def lookup_cell(entry: Entry | None, key: AttemptKey) -> Handle | None:
    if entry is None or isinstance(entry, Unsubmitted):
        return None
    if isinstance(entry, Starting):
        raise refusal("runner_starting")
    return key.handle()


def status_cell(entry: Entry | None, observed_at: datetime) -> RuntimeStatus:
    if entry is None or isinstance(entry, Unsubmitted):
        raise refusal("attempt_not_submitted")
    if isinstance(entry, Starting):
        raise refusal("runner_starting")
    if isinstance(entry, Running | Retiring):
        raise refusal("runner_unreachable")
    phase: RuntimePhase = entry.phase if isinstance(entry, Retired) else "complete"
    return RuntimeStatus(
        state="exited", exit_code=None, phase=phase, observed_at=observed_at
    )


def retire_cell(
    entry: Entry | None, key: AttemptKey, retirement_id: str, stopped_at: datetime
) -> Transition:
    if isinstance(entry, Retired):
        return Transition()
    if entry is not None and released(entry):
        return Transition(refusal="attempt_retired")
    if isinstance(entry, Starting):
        if entry.fence is not None:
            return Transition()
        fence = Fence(retirement_id=retirement_id, fenced_at=stopped_at)
        return Transition(
            Starting.model_validate({**entry.model_dump(), "fence": fence})
        )
    if entry is not None and not isinstance(entry, Unsubmitted):
        return Transition(refusal="retirement_unavailable")
    return Transition(no_runner_started(key, retirement_id, stopped_at))


def tombstone_cell(
    entry: Entry | None, key: AttemptKey, fence: Fence, stopped_at: datetime
) -> Transition:
    if isinstance(entry, Retired):
        return Transition()
    if not isinstance(entry, Starting) or entry.fence != fence:
        return Transition(refusal="retirement_unavailable")
    return Transition(no_runner_started(key, fence.retirement_id, stopped_at))


def no_runner_started(
    key: AttemptKey, retirement_id: str, stopped_at: datetime
) -> Retired:
    receipt = TerminationReceipt(
        kind="termination",
        attempt_id=key.attempt_id,
        execution_domain_id=key.execution_domain_id,
        retirement_id=retirement_id,
        stopped_at=stopped_at,
        evidence_ref=f"registry:{key.name}",
        scope=EvidenceScope.NO_RUNNER_STARTED,
    )
    return Retired(
        state="retired",
        runner=None,
        receipt=receipt,
        phase="failed",
        normal_completion=None,
        released_at=None,
    )


def synchronize(descriptor: int) -> None:
    if sys.platform == "darwin":
        fcntl.fcntl(descriptor, F_FULLFSYNC)
    else:
        os.fsync(descriptor)


def unlink_if_present(directory: DirectoryAnchor, name: str) -> None:
    try:
        os.unlink(name, dir_fd=directory.descriptor)
    except FileNotFoundError:
        pass


def write_atomically(
    directory: DirectoryAnchor,
    name: str,
    encoded: bytes,
    nonce: str,
    *,
    replace: bool,
) -> bool:
    temporary = f"{name}.tmp-{nonce}"
    descriptor = open_private_file(
        directory, temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
    )
    try:
        try:
            remaining = memoryview(encoded)
            while remaining:
                remaining = remaining[os.write(descriptor, remaining) :]
            synchronize(descriptor)
        finally:
            os.close(descriptor)
        if replace:
            os.replace(
                temporary,
                name,
                src_dir_fd=directory.descriptor,
                dst_dir_fd=directory.descriptor,
            )
        else:
            try:
                rename_no_replace(directory, temporary, name)
            except FileExistsError:
                unlink_if_present(directory, temporary)
                return False
    except BaseException:
        unlink_if_present(directory, temporary)
        raise
    synchronize(directory.descriptor)
    return True


def sweep_temporaries(directory: DirectoryAnchor, name: str) -> None:
    prefix = f"{name}.tmp-"
    with os.scandir(directory.descriptor) as entries:
        leftovers = [entry.name for entry in entries if entry.name.startswith(prefix)]
    for leftover in leftovers:
        unlink_if_present(directory, leftover)


class LockOutcome(Enum):
    BUSY = "busy"


@dataclass(frozen=True)
class Applied:
    head: RegistryHead | None
    transition: Transition


class Registry:
    def __init__(
        self,
        directory: DirectoryAnchor,
        execution_domain_id: str,
        new_nonce: Callable[[], str],
    ) -> None:
        self.directory = directory
        self.execution_domain_id = execution_domain_id
        self.new_nonce = new_nonce

    def key(self, attempt_id: str) -> AttemptKey:
        return AttemptKey(self.execution_domain_id, attempt_id)

    def read(self, key: AttemptKey) -> RegistryHead | None:
        try:
            encoded = read_private_file(self.directory, f"{key.name}.json", HEAD_LIMIT)
        except FileNotFoundError:
            return None
        except (OSError, FilesystemError):
            raise refusal("lineage_unavailable") from None
        try:
            head = RegistryHead.model_validate_json(encoded)
        except ValueError:
            raise refusal("lineage_unavailable") from None
        if (head.execution_domain_id, head.attempt_id) != (
            key.execution_domain_id,
            key.attempt_id,
        ):
            raise refusal("lineage_unavailable")
        return head

    def entry(self, key: AttemptKey) -> Entry | None:
        head = self.read(key)
        return None if head is None else head.entry

    def try_transition(
        self, key: AttemptKey, decide: Callable[[Entry | None], Transition]
    ) -> Applied | LockOutcome:
        try:
            lock = open_private_file(
                self.directory, f"{key.name}.lock", os.O_RDWR | os.O_CREAT
            )
        except (OSError, FilesystemError):
            raise refusal("registry_unavailable") from None
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return LockOutcome.BUSY
            return self.locked_transition(key, decide)
        finally:
            os.close(lock)

    def locked_transition(
        self, key: AttemptKey, decide: Callable[[Entry | None], Transition]
    ) -> Applied:
        try:
            sweep_temporaries(self.directory, f"{key.name}.json")
        except OSError:
            raise refusal("registry_unavailable") from None
        head = self.read(key)
        transition = decide(None if head is None else head.entry)
        if transition.entry is None:
            return Applied(head, transition)
        encoded = with_entry(key, transition.entry)
        try:
            write_atomically(
                self.directory,
                f"{key.name}.json",
                encoded,
                self.new_nonce(),
                replace=True,
            )
        except (OSError, FilesystemError):
            raise refusal("registry_unavailable") from None
        return Applied(RegistryHead.model_validate_json(encoded), transition)


DOMAIN_LIMIT: Final[int] = 1048576
MAX_BOOTS: Final[int] = 4096


class BootRecord(BoundaryModel):
    boot_id: Identifier
    pid_namespace: Identifier


class DomainRecord(BoundaryModel):
    format: Literal["local/1"]
    execution_domain_id: Identifier
    host_id: Identifier
    boots: Annotated[tuple[BootRecord, ...], Field(min_length=1, max_length=MAX_BOOTS)]

    @model_validator(mode="after")
    def distinct_boots(self) -> Self:
        if len({boot.boot_id for boot in self.boots}) != len(self.boots):
            raise ValueError("a boot is recorded once")
        return self


@dataclass(frozen=True)
class Lineage:
    host_id: str
    boot_id: str
    pid_namespace: str


def read_domain(root: DirectoryAnchor) -> DomainRecord | None:
    try:
        encoded = read_private_file(root, "domain.json", DOMAIN_LIMIT)
    except FileNotFoundError:
        return None
    except (OSError, FilesystemError):
        raise refusal("lineage_unavailable") from None
    try:
        return DomainRecord.model_validate_json(encoded)
    except ValueError:
        raise refusal("lineage_unavailable") from None


def recorded_domain(
    root: DirectoryAnchor,
    lineage: Lineage,
    new_identifier: Callable[[], str],
    new_nonce: Callable[[], str],
) -> DomainRecord:
    sweep_temporaries(root, "domain.json")
    record = read_domain(root)
    boot = BootRecord(boot_id=lineage.boot_id, pid_namespace=lineage.pid_namespace)
    if record is None:
        if registry_exists(root):
            raise refusal("lineage_unavailable")
        created = DomainRecord(
            format="local/1",
            execution_domain_id=new_identifier(),
            host_id=lineage.host_id,
            boots=(boot,),
        )
        encoded = model_bytes(created)
        if write_atomically(root, "domain.json", encoded, new_nonce(), replace=False):
            return created
        return recorded_domain(root, lineage, new_identifier, new_nonce)
    if record.host_id != lineage.host_id:
        raise refusal("lineage_unavailable")
    known = {entry.boot_id: entry.pid_namespace for entry in record.boots}
    if lineage.boot_id in known:
        if known[lineage.boot_id] != lineage.pid_namespace:
            raise refusal("lineage_unavailable")
        return record
    if len(record.boots) >= MAX_BOOTS:
        raise KoraProtocolError(
            "boot_log_full", "the state root's boot log is full", False, {}
        )
    appended = DomainRecord(
        format="local/1",
        execution_domain_id=record.execution_domain_id,
        host_id=record.host_id,
        boots=(*record.boots, boot),
    )
    write_atomically(
        root, "domain.json", model_bytes(appended), new_nonce(), replace=True
    )
    return appended


def registry_exists(root: DirectoryAnchor) -> bool:
    try:
        os.stat("registry", dir_fd=root.descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def open_domain(
    root: DirectoryAnchor,
    lineage: Lineage,
    new_identifier: Callable[[], str],
    new_nonce: Callable[[], str],
) -> tuple[DomainRecord, DirectoryAnchor] | LockOutcome:
    try:
        lock = open_private_file(root, "boot.lock", os.O_RDWR | os.O_CREAT)
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return LockOutcome.BUSY
            record = recorded_domain(root, lineage, new_identifier, new_nonce)
            if not registry_exists(root):
                create_directory(root, "registry").close()
            return record, open_private_directory(root, "registry")
        finally:
            os.close(lock)
    except (OSError, FilesystemError):
        raise refusal("lineage_unavailable") from None


def lineage_of(identities: IdentityReader) -> Lineage:
    observation = identities.read(os.getpid())
    if observation.process is None:
        raise refusal("lineage_unavailable")
    identity = observation.process.identity
    host, boot, namespace = identity.host_id, identity.boot_id, identity.pid_namespace
    if host is None or boot is None or namespace is None:
        raise refusal("lineage_unavailable")
    return Lineage(host_id=host, boot_id=boot, pid_namespace=namespace)


def release(
    value: Result | LockOutcome, on_cancel: Callable[[Result], None] | None
) -> None:
    if on_cancel is not None and not isinstance(value, LockOutcome):
        on_cancel(value)


class TransitionExecutor:
    def __init__(self, workers: int = WORKERS, admitted: int = ADMITTED_JOBS) -> None:
        self.executor = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="kora-runtime-local"
        )
        self.admitted = admitted
        self.jobs: set[asyncio.Future[object]] = set()
        self.closed = False

    async def run(
        self,
        function: Callable[[], Result],
        on_cancel: Callable[[Result], None] | None = None,
    ) -> Result:
        if self.closed:
            raise refusal("runtime_closed")
        if len(self.jobs) >= self.admitted:
            raise refusal("registry_busy")
        future = asyncio.get_running_loop().run_in_executor(self.executor, function)
        owned = cast("asyncio.Future[object]", future)
        self.jobs.add(owned)
        cancelled = False
        try:
            while True:
                try:
                    result = await asyncio.shield(future)
                    break
                except asyncio.CancelledError:
                    if future.cancelled():
                        raise
                    cancelled = True
                except Exception:
                    if cancelled:
                        raise asyncio.CancelledError from None
                    raise
            if cancelled:
                if on_cancel is not None:
                    on_cancel(result)
                raise asyncio.CancelledError
            return result
        finally:
            self.jobs.discard(owned)

    async def locked(
        self,
        attempt: Callable[[], Result | LockOutcome],
        ctx: ProtocolContext,
        operation: str,
        on_cancel: Callable[[Result], None] | None = None,
    ) -> Result:
        started = ctx.clock.monotonic_ns()
        deadline = started + LOCK_DEADLINE_NS
        while True:
            outcome = await self.run(attempt, lambda value: release(value, on_cancel))
            if not isinstance(outcome, LockOutcome):
                return outcome
            now = ctx.clock.monotonic_ns()
            if now >= deadline:
                waited = (now - started) / 1_000_000
                ctx.logger.emit(LOCK_TIMEOUT, operation=operation, waited_ms=waited)
                raise refusal("registry_busy")
            await ctx.clock.sleep_until(min(deadline, now + LOCK_RETRY_NS))

    async def close(self) -> None:
        self.closed = True
        if self.jobs:
            await asyncio.wait(set(self.jobs))
        self.executor.shutdown(wait=True)
