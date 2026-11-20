import asyncio
import threading
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final, Protocol, TypeVar

from pydantic import ValidationError

from kora.ir.json import CanonicalBudget, JsonObject, JsonShapeError, spend_string
from kora.ir.vcs_ref_json import canonical_bytes
from kora.observability.redaction import SecretRegistry
from kora.protocols.deadlines import duration_ns
from kora.protocols.errors import KoraProtocolError
from kora.protocols.harness import (
    EVENT_ADAPTER,
    MAX_TEXT_BYTES,
    Enforcement,
    PermissionDeniedEvent,
    ProviderLimitedEvent,
    RunnerCause,
    ToolCallEvent,
    ToolResultEvent,
)
from kora.protocols.time import Clock
from kora.runner.calls import TRACKING_CEILING, EventCount
from kora.runner.journal import BatchAppend, JournalAppendOutcome

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
JOURNAL_CAP_BYTES: Final[int] = 268_435_456
MAX_RECORD_BYTES: Final[int] = 65_536
QUEUE_BYTES: Final[int] = 4 * 1_048_576
QUEUE_RECORDS: Final[int] = 4_096
BATCH_RECORDS: Final[int] = 256
BATCH_WINDOW_NS: Final[int] = 50_000_000
MIN_BATCH_WAIT_NS: Final[int] = 1_000_000
MAX_ERROR_EVENTS: Final[int] = 100
T = TypeVar("T")


class JournalWriter(Protocol):
    def append_batch(
        self, records: Sequence[tuple[str, JsonObject]], *, cap_bytes: int
    ) -> BatchAppend: ...

    def append_outcome(self, kind: str, data: JsonObject) -> JournalAppendOutcome: ...


@dataclass(frozen=True)
class Liveness:
    last_event_ns: int
    calls_open: bool
    full_since_ns: int | None


@dataclass(frozen=True)
class Record:
    kind: str
    data: JsonObject
    size: int
    queued_ns: int


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
        stall_after_ns: int,
        *,
        journal_cap: int = JOURNAL_CAP_BYTES,
    ) -> None:
        self._journal = journal
        self._journal_cap = journal_cap
        self._capped = False
        self._capped_records = 0
        self._capped_bytes = 0
        self._cap_reported = False
        self._registry = registry
        self._clock = clock
        self._stall_after_ns = stall_after_ns
        self._in_flight = 0
        self._last_event_ns = clock.monotonic_ns()
        self._open_calls: set[str] = set()
        self._full_since_ns: int | None = None
        self._changed = asyncio.Event()
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
        with self._state:
            self._last_event_ns = self._clock.monotonic_ns()
            if isinstance(event, ToolCallEvent):
                if len(self._open_calls) < TRACKING_CEILING:
                    self._open_calls.add(event.data.tool_use_id)
            elif isinstance(event, (ToolResultEvent, PermissionDeniedEvent)):
                had_calls = bool(self._open_calls)
                self._open_calls.discard(event.data.tool_use_id)
                if had_calls and not self._open_calls:
                    self._changed.set()
        if (
            isinstance(event, ProviderLimitedEvent)
            and event.data.retry_after is not None
            and duration_ns(event.data.retry_after - at) > self._stall_after_ns
        ):
            self.terminate("provider_limit")
        if self._count.count(kind).cause is not None:
            self.terminate("max_events")
        if kind == "error":
            self._errors += 1
            if self._errors > MAX_ERROR_EVENTS:
                return
        self._sequence += 1
        size = len(canonical_bytes(event.model_dump(mode="json")))
        data = event.data.model_dump(mode="json")
        self._enqueue(Record(kind, data, size, self._clock.monotonic_ns()))

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
            if self._capped:
                self._count_capped(record)
                return
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
            if record.size <= MAX_RECORD_BYTES and self._full_since_ns is None:
                self._full_since_ns = self._clock.monotonic_ns()
                self._changed.set()
            if isinstance(last, Gap):
                last.omitted += 1
            else:
                self._queue.append(Gap(1))
                self._state.notify_all()

    def observe_dropped(self, kind: str) -> None:
        with self._state:
            self._last_event_ns = self._clock.monotonic_ns()

    def liveness(self) -> Liveness:
        with self._state:
            return Liveness(
                self._last_event_ns, bool(self._open_calls), self._full_since_ns
            )

    async def changed(self) -> None:
        await self._changed.wait()
        self._changed.clear()

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
                self._queue.append(
                    Record("runner.cap_reached", capped, 0, self._clock.monotonic_ns())
                )
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
                self._halt(self._in_flight)
            self._wake(self._written.set)

    def _drain(self) -> None:
        while True:
            with self._state:
                self._state.wait_for(lambda: bool(self._queue) or self._closing)
                if not self._queue:
                    return
                item = self._queue.popleft()
                if self._full_since_ns is not None:
                    self._full_since_ns = None
                if isinstance(item, Record):
                    self._queued_bytes -= item.size
            if isinstance(item, asyncio.Event):
                if not self._settle_cap():
                    self._halt()
                    self._wake(item.set)
                    return
                self._wake(item.set)
                continue
            if isinstance(item, Gap):
                lost = 0
                durable = self._append("runner.journal_gap", {"omitted": item.omitted})
            elif item.kind in ADAPTER_KINDS:
                batch = self._collect_batch(item)
                lost = len(batch)
                self._in_flight = lost
                durable = self._append_batch(batch)
                self._in_flight = 0
            else:
                lost = 0
                durable = self._append(item.kind, item.data)
            if not durable:
                self._halt(lost)
                return

    def _collect_batch(self, first: Record) -> list[Record]:
        batch = [first]
        deadline = first.queued_ns + BATCH_WINDOW_NS
        with self._state:
            while len(batch) < BATCH_RECORDS:
                if self._queue:
                    item = self._queue[0]
                    if (
                        not isinstance(item, Record)
                        or item.kind not in ADAPTER_KINDS
                        or item.queued_ns >= deadline
                    ):
                        break
                    self._queue.popleft()
                    self._queued_bytes -= item.size
                    self._full_since_ns = None
                    batch.append(item)
                    continue
                remaining = deadline - self._clock.monotonic_ns()
                if self._closing or remaining <= 0:
                    break
                self._state.wait(max(remaining, MIN_BATCH_WAIT_NS) / 1_000_000_000)
        return batch

    def _append_batch(self, batch: Sequence[Record]) -> bool:
        try:
            result = self._journal.append_batch(
                [(item.kind, item.data) for item in batch], cap_bytes=self._journal_cap
            )
        except OSError:
            return False
        if result.outcome not in (None, JournalAppendOutcome.DURABLE):
            return False
        if result.admitted < len(batch):
            with self._state:
                self._capped = True
                self._full_since_ns = None
                for record in batch[result.admitted :]:
                    self._count_capped(record)
                retained: deque[Record | Gap | asyncio.Event] = deque()
                for item in self._queue:
                    if isinstance(item, Gap):
                        self._capped_records += item.omitted
                    elif isinstance(item, Record) and item.kind in ADAPTER_KINDS:
                        self._count_capped(item)
                        self._queued_bytes -= item.size
                    else:
                        retained.append(item)
                self._queue = retained
                self._state.notify_all()
        return True

    def _count_capped(self, record: Record) -> None:
        self._capped_records += 1
        self._capped_bytes += record.size
        self._omitted += 1

    def _settle_cap(self) -> bool:
        with self._state:
            if not self._capped or self._cap_reported:
                return True
            self._cap_reported = True
            records = self._capped_records
            size = self._capped_bytes
        return self._append(
            "runner.journal_gap", {"omitted": records}
        ) and self._append("runner.cap_reached", {"kind": "journal", "omitted": size})

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
