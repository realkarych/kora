from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol

from kora.plugins.runtime.local.capture import OpenedLog
from kora.plugins.runtime.local.channel import LogName
from kora.protocols.runtime import TransferItem, Workload
from kora.runner.filesystem import DirectoryAnchor, FileIdentity
from kora.runner.registry import Submitter

PhaseName = Literal["preparation", "execution", "complete", "failed"]
StateName = Literal["starting", "running", "exited"]


@dataclass(frozen=True)
class Incoming:
    directory: DirectoryAnchor
    descriptor: int


@dataclass(frozen=True)
class OpenedOutput:
    descriptor: int
    size: int
    sha256: str
    identity: FileIdentity
    changed_ns: int


class Completion(StrEnum):
    PENDING = "pending"
    COMPLETE = "complete"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class JournalReference:
    offset: int
    length: int
    digest: str


class WorkloadSession(Protocol):
    def capture_prelaunch(
        self, baseline: dict[int, int], workload: Workload, deadline: int
    ) -> None: ...

    def accept(
        self,
        submitter: Submitter,
        prelaunch: dict[int, int] | None,
        workload: Workload,
    ) -> None: ...

    def supervise(
        self,
        submitter: Submitter,
        observational: bool,
        workload: Workload | None,
        authorize: Callable[[], object],
    ) -> None: ...

    def attach_drains(self) -> None: ...

    def status(self) -> tuple[StateName, PhaseName, int | None]: ...

    def decide_transfer(
        self, submitter: Submitter, observational: bool, item: TransferItem
    ) -> Literal["write", "repeat", "conflict"]: ...

    def open_incoming(self) -> Incoming: ...

    def discard_incoming(self, incoming: Incoming) -> None: ...

    def finish_incoming(
        self,
        incoming: Incoming,
        item: TransferItem,
        digest: str,
        authorize: Callable[[], object],
    ) -> None: ...

    def complete_seen(
        self,
        decision: Literal["repeat", "conflict"],
        item: TransferItem,
        digest: str,
    ) -> None: ...

    def end_transfer(self) -> None: ...

    def fence_transfer(self) -> None: ...

    def open_output(self, submitter: Submitter, name: str) -> OpenedOutput: ...

    def open_log(
        self, submitter: Submitter, which: LogName, offset: int
    ) -> OpenedLog: ...

    def confirm_output(
        self, opened: OpenedOutput, active: Callable[[], None] | None = None
    ) -> None: ...

    def read_output(self, opened: OpenedOutput, offset: int, length: int) -> bytes: ...

    def note_output(self, outcome: str, started: int) -> None: ...

    def completion(self) -> Completion: ...

    def result_reference(self) -> JournalReference | None: ...

    def fence(self) -> None: ...

    def close_journal(self) -> None: ...

    async def stop(self) -> None: ...
