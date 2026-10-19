import collections
import os
import select
import threading
import time
from dataclasses import dataclass, field

from kora.observability.catalog import LossReason
from kora.observability.settings import Level

WRITE_WAIT_SECONDS: float = 0.1
BATCH_BYTES: int = 65_536

LossKey = tuple[str, str, str]


def new_counter() -> collections.Counter[LossKey]:
    return collections.Counter()


@dataclass(frozen=True)
class DiagnosticsSnapshot:
    rejected: dict[LossKey, int]
    dropped: dict[LossKey, int]
    suppressed: dict[LossKey, int]
    secret_admission_failures: int
    sink_failures: int
    sink_recoveries: int
    queue_records: int
    queue_bytes: int
    sink_failed: bool

    def rejected_total(self) -> int:
        return sum(self.rejected.values())

    def dropped_total(self) -> int:
        return sum(self.dropped.values())

    def suppressed_total(self) -> int:
        return sum(self.suppressed.values())


@dataclass
class DiagnosticsState:
    rejected: collections.Counter[LossKey] = field(default_factory=new_counter)
    dropped: collections.Counter[LossKey] = field(default_factory=new_counter)
    suppressed: collections.Counter[LossKey] = field(default_factory=new_counter)
    secret_admission_failures: int = 0
    sink_failures: int = 0
    sink_recoveries: int = 0
    queue_records: int = 0
    queue_bytes: int = 0
    sink_failed: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)

    def count_rejected(self, component: str, level: Level, reason: LossReason) -> None:
        with self.lock:
            self.rejected[(component, level.name, reason.value)] += 1

    def count_dropped(self, component: str, level: Level, reason: LossReason) -> None:
        with self.lock:
            self.dropped[(component, level.name, reason.value)] += 1

    def count_suppressed(self, component: str, level: Level, event: str) -> None:
        with self.lock:
            self.suppressed[(component, level.name, event)] += 1

    def count_secret_admission_failure(self) -> None:
        with self.lock:
            self.secret_admission_failures += 1

    def mark_sink_failed(self) -> None:
        with self.lock:
            self.sink_failed = True
            self.sink_failures += 1

    def mark_sink_recovered(self) -> None:
        with self.lock:
            self.sink_recoveries += 1

    def set_queue(self, records: int, size: int) -> None:
        with self.lock:
            self.queue_records = records
            self.queue_bytes = size

    def snapshot(self) -> DiagnosticsSnapshot:
        with self.lock:
            return DiagnosticsSnapshot(
                rejected=dict(self.rejected),
                dropped=dict(self.dropped),
                suppressed=dict(self.suppressed),
                secret_admission_failures=self.secret_admission_failures,
                sink_failures=self.sink_failures,
                sink_recoveries=self.sink_recoveries,
                queue_records=self.queue_records,
                queue_bytes=self.queue_bytes,
                sink_failed=self.sink_failed,
            )


class BoundedQueue:
    def __init__(
        self, max_records: int, max_bytes: int, state: DiagnosticsState
    ) -> None:
        self._max_records = max_records
        self._max_bytes = max_bytes
        self._state = state
        self._items: collections.deque[bytes] = collections.deque()
        self._size = 0
        self._closed = False
        self._sealed = False
        self._condition = threading.Condition()
        self._stopping = False

    def put(self, item: bytes) -> LossReason | None:
        with self._condition:
            if self._sealed:
                return LossReason.SHUTDOWN
            if self._closed:
                return LossReason.SINK_FAILED
            if len(self._items) >= self._max_records:
                return LossReason.QUEUE_RECORDS
            if self._size + len(item) > self._max_bytes:
                return LossReason.QUEUE_BYTES
            self._items.append(item)
            self._size += len(item)
            self._state.set_queue(len(self._items), self._size)
            self._condition.notify()
            return None

    def get_batch(self, timeout: float, max_bytes: int) -> bytes | None:
        with self._condition:
            if not self._items and not self._stopping:
                self._condition.wait(timeout)
            if not self._items:
                return None
            batch: list[bytes] = [self._items.popleft()]
            total = len(batch[0])
            while self._items and total + len(self._items[0]) <= max_bytes:
                item = self._items.popleft()
                batch.append(item)
                total += len(item)
            self._size -= total
            self._state.set_queue(len(self._items), self._size)
            if not self._items:
                self._condition.notify_all()
            return b"".join(batch)

    def is_empty(self) -> bool:
        with self._condition:
            return not self._items

    def seal(self) -> None:
        with self._condition:
            self._sealed = True

    def request_stop(self) -> None:
        with self._condition:
            self._stopping = True
            self._condition.notify_all()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._items.clear()
            self._size = 0
            self._state.set_queue(0, 0)
            self._condition.notify_all()

    def wait_empty(self, deadline: float) -> bool:
        with self._condition:
            while self._items:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True


class StreamWriter:
    def __init__(
        self,
        descriptor: int,
        queue: BoundedQueue,
        state: DiagnosticsState,
    ) -> None:
        self._descriptor = descriptor
        self._queue = queue
        self._state = state
        self._stop = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._thread = threading.Thread(
            target=self._run, name="kora-diagnostics-writer", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        try:
            while True:
                item = self._queue.get_batch(WRITE_WAIT_SECONDS, BATCH_BYTES)
                if item is None:
                    if self._stop.is_set() and self._queue.is_empty():
                        return
                    continue
                self._idle.clear()
                try:
                    if not self._write_all(item):
                        return
                finally:
                    self._idle.set()
        except BaseException:
            self._fail()

    def _write_all(self, item: bytes) -> bool:
        view = memoryview(item)
        while view:
            try:
                written = os.write(self._descriptor, view)
            except BlockingIOError:
                if self._stop.is_set():
                    return False
                select.select([], [self._descriptor], [], WRITE_WAIT_SECONDS)
                continue
            except OSError:
                self._fail()
                return False
            view = view[written:]
        return True

    def _fail(self) -> None:
        self._state.mark_sink_failed()
        self._queue.close()

    def stop(self, deadline_seconds: float) -> bool:
        deadline = time.monotonic() + deadline_seconds
        drained = self._queue.wait_empty(deadline)
        self._stop.set()
        self._queue.request_stop()
        self._thread.join(max(0.0, deadline - time.monotonic()))
        return drained and not self._thread.is_alive()

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def is_idle(self) -> bool:
        return self._idle.is_set() and self._queue.is_empty()
