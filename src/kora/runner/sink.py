import asyncio
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Protocol, TypeVar

from pydantic import ValidationError

from kora.ir.json import CanonicalBudget, JsonObject, JsonShapeError, spend_string
from kora.ir.vcs_ref_json import canonical_bytes
from kora.observability.redaction import SecretRegistry
from kora.protocols.errors import KoraProtocolError
from kora.protocols.harness import (
    EVENT_ADAPTER,
    MAX_TEXT_BYTES,
    Enforcement,
    RunnerCause,
)
from kora.protocols.time import Clock
from kora.runner.calls import EventCount
from kora.runner.journal import JournalAppendOutcome

ADAPTER_KINDS: Final[frozenset[str]] = frozenset(
    {
        "session_started",
        "turn",
        "tool_call",
        "tool_result",
        "permission_denied",
        "usage",
        "provider_limited",
        "provider_retrying",
        "result",
        "error",
    }
)
TEXT_FIELDS: Final[dict[str, str]] = {
    "turn": "text",
    "tool_result": "content",
    "error": "text",
}
MAX_RECORD_BYTES: Final[int] = 65_536
QUEUE_BYTES: Final[int] = 4 * 1_048_576
QUEUE_RECORDS: Final[int] = 4_096
MAX_ERROR_EVENTS: Final[int] = 100
T = TypeVar("T")


class JournalWriter(Protocol):
    def append_outcome(self, kind: str, data: JsonObject) -> JournalAppendOutcome: ...


@dataclass(frozen=True)
class Record:
    kind: str
    data: JsonObject
    size: int


@dataclass
class Gap:
    omitted: int


def fits_text(text: str) -> bool:
    try:
        spend_string(CanonicalBudget(MAX_TEXT_BYTES), text, "text", 0)
    except JsonShapeError:
        return False
    return True


def recut(text: str) -> tuple[str, bool]:
    if fits_text(text):
        return text, False
    low, high = 0, len(text)
    while high - low > 1:
        middle = (low + high) // 2
        if fits_text(text[:middle]):
            low = middle
        else:
            high = middle
    return text[:low], True


def adapter_record(item: Record | Gap | asyncio.Event) -> bool:
    return isinstance(item, Record) and item.kind in ADAPTER_KINDS


def refused(code: str) -> KoraProtocolError:
    return KoraProtocolError(code, "the adapter event is refused", False, {})


async def settled_by(
    waited: "asyncio.Future[T]", clock: Clock, deadline_ns: int
) -> bool:
    timer = asyncio.ensure_future(clock.sleep_until(deadline_ns))
    try:
        await asyncio.wait({waited, timer}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        timer.cancel()
    return waited.done()


class RunnerSink:
    def __init__(
        self,
        journal: JournalWriter,
        registry: SecretRegistry,
        clock: Clock,
        max_events: int | None,
    ) -> None:
        self._journal = journal
        self._registry = registry
        self._clock = clock
        self._count = EventCount(max_events)
        self._loop = asyncio.get_running_loop()
        self._stopped: asyncio.Future[RunnerCause] = self._loop.create_future()
        self._written = asyncio.Event()
        self._state = threading.Condition()
        self._queue: deque[Record | Gap | asyncio.Event] = deque()
        self._queued_bytes = 0
        self._cause: RunnerCause | None = None
        self._omitted = 0
        self._errors = 0
        self._sequence = 0
        self._fenced = False
        self._halted = False
        self._closing = False
        self._thread = threading.Thread(
            target=self._write, name="kora-agent-journal", daemon=True
        )
        self._thread.start()

    def emit(self, kind: str, data: JsonObject) -> None:
        if kind not in ADAPTER_KINDS:
            raise refused("event_kind_refused") from None
        at = self._clock.now()
        try:
            event = EVENT_ADAPTER.validate_python(
                {
                    "i": self._sequence,
                    "at": at,
                    "kind": kind,
                    "data": self._bounded(kind, data),
                }
            )
        except ValidationError:
            raise refused("event_refused") from None
        if self._count.count(kind).cause is not None:
            self.terminate("max_events")
        if kind == "error":
            self._errors += 1
            if self._errors > MAX_ERROR_EVENTS:
                return
        self._sequence += 1
        size = len(canonical_bytes(event.model_dump(mode="json")))
        self._enqueue(Record(kind, event.data.model_dump(mode="json"), size))

    def _bounded(self, kind: str, data: JsonObject) -> JsonObject:
        field = TEXT_FIELDS.get(kind)
        text = None if field is None else data.get(field)
        if field is None or not isinstance(text, str):
            return data
        cut, truncated = recut(self._registry.mask(text, MAX_TEXT_BYTES))
        truncated = truncated or len(text) > MAX_TEXT_BYTES
        bounded: JsonObject = {**data, field: cut}
        if truncated:
            bounded["truncated"] = True
        return bounded

    def _enqueue(self, record: Record) -> None:
        with self._state:
            if (
                not self._halted
                and record.size <= MAX_RECORD_BYTES
                and len(self._queue) < QUEUE_RECORDS
                and self._queued_bytes + record.size <= QUEUE_BYTES
            ):
                self._queue.append(record)
                self._queued_bytes += record.size
                self._state.notify_all()
                return
            self._omitted += 1
            last = self._queue[-1] if self._queue else None
            if self._halted:
                return
            if isinstance(last, Gap):
                last.omitted += 1
            else:
                self._queue.append(Gap(1))
                self._state.notify_all()

    def observe_dropped(self, kind: str) -> None:
        return None

    def enforcement(self) -> Enforcement:
        with self._state:
            cause = self._cause
        if cause is not None:
            return Enforcement("terminate", cause)
        return self._count.enforcement()

    async def termination(self) -> RunnerCause:
        return await asyncio.shield(self._stopped)

    def terminate(self, cause: RunnerCause) -> None:
        with self._state:
            if self._cause is not None:
                return
            self._cause = cause
        self._wake(lambda: self._stopped.set_result(cause))

    def cause(self) -> RunnerCause | None:
        with self._state:
            return self._cause

    def omitted(self) -> int:
        with self._state:
            return self._omitted

    def fence(self) -> None:
        with self._state:
            self._fenced = True

    async def settle(self, deadline_ns: int) -> bool:
        barrier = asyncio.Event()
        with self._state:
            if self._errors > MAX_ERROR_EVENTS:
                omitted = self._errors - MAX_ERROR_EVENTS
                capped: JsonObject = {"kind": "error_events", "omitted": omitted}
                self._queue.append(Record("runner.cap_reached", capped, 0))
            halted = self._halted
            if not halted:
                self._queue.append(barrier)
                self._state.notify_all()
        if halted:
            return False
        waiter = asyncio.ensure_future(barrier.wait())
        try:
            reached = await settled_by(waiter, self._clock, deadline_ns)
        finally:
            waiter.cancel()
        with self._state:
            return reached and self._omitted == 0 and not self._halted

    def close(self) -> None:
        with self._state:
            self._closing = True
            self._state.notify_all()

    async def joined(self, deadline_ns: int) -> bool:
        waiter = asyncio.ensure_future(self._written.wait())
        try:
            return await settled_by(waiter, self._clock, deadline_ns)
        finally:
            waiter.cancel()

    def _wake(self, callback: Callable[[], object]) -> None:
        try:
            self._loop.call_soon_threadsafe(callback)
        except RuntimeError:
            return

    def _write(self) -> None:
        settled = False
        try:
            self._drain()
            settled = True
        finally:
            if not settled:
                self._halt()
            self._wake(self._written.set)

    def _drain(self) -> None:
        while True:
            with self._state:
                self._state.wait_for(lambda: bool(self._queue) or self._closing)
                if not self._queue:
                    return
                item = self._queue.popleft()
                if isinstance(item, Record):
                    self._queued_bytes -= item.size
            if isinstance(item, asyncio.Event):
                self._wake(item.set)
                continue
            if isinstance(item, Gap):
                lost = 0
                durable = self._append("runner.journal_gap", {"omitted": item.omitted})
            else:
                lost = 1 if adapter_record(item) else 0
                durable = self._append(item.kind, item.data)
            if not durable:
                self._halt(lost)
                return

    def _append(self, kind: str, data: JsonObject) -> bool:
        try:
            outcome = self._journal.append_outcome(kind, data)
        except OSError:
            return False
        return outcome is JournalAppendOutcome.DURABLE

    def _halt(self, lost: int = 0) -> None:
        with self._state:
            self._halted = True
            self._omitted += lost
            fenced = self._fenced
            pending = tuple(self._queue)
            self._queue.clear()
            self._queued_bytes = 0
            self._omitted += sum(1 for item in pending if adapter_record(item))
        for item in pending:
            if isinstance(item, asyncio.Event):
                self._wake(item.set)
        if not fenced:
            self.terminate("journal_unavailable")
