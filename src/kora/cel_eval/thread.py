import asyncio
import contextvars
import math
import threading
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from functools import partial

from kora.cel import CheckedExpression
from kora.cel.types import CelType
from kora.cel_eval import (
    CEL_COMPONENT,
    CEL_LOGGER_NAME,
    EVALUATOR_CLOSE_TIMED_OUT,
    EVALUATOR_FAILED,
    EVALUATOR_STARTED,
    EVALUATOR_STOPPED,
)
from kora.cel_eval.binding import Binding, load_binding
from kora.cel_eval.cache import DEFAULT_CACHE_CHARACTERS, DEFAULT_CACHE_ENTRIES
from kora.cel_eval.convert import bind
from kora.cel_eval.errors import (
    Clock,
    ErrorContext,
    ErrorKind,
    EvaluationError,
    UnavailableReason,
    raise_safe,
    refuse,
)
from kora.cel_eval.evaluate import Compilation, Machine, Prepared, Result, ResultType
from kora.observability import current_diagnostics

STACK_BYTES: int = 1_048_576
MAX_RENDERING: int = 65_536
MAX_FIELD: int = 1_024
MAX_EVENT_ID: int = 128
REAP_SECONDS: float = 0.05
STACK_LOCK: threading.Lock = threading.Lock()


@dataclass(frozen=True)
class Settings:
    evaluation_jobs: int = 32
    compile_jobs: int = 32
    outstanding_bytes: int = 8_388_608
    batch_size: int = 16
    handles: int = 128
    cache_entries: int = DEFAULT_CACHE_ENTRIES
    cache_characters: int = DEFAULT_CACHE_CHARACTERS
    close_timeout: float = 1.0

    def __post_init__(self) -> None:
        counts = (
            self.evaluation_jobs,
            self.compile_jobs,
            self.outstanding_bytes,
            self.batch_size,
            self.handles,
            self.cache_entries,
            self.cache_characters,
        )
        if any(type(value) is not int or value < 1 for value in counts):
            raise_safe(ValueError("evaluator limits must be positive integers"))
        if not math.isfinite(self.close_timeout) or self.close_timeout <= 0:
            raise_safe(ValueError("close deadline must be finite and positive"))


@dataclass(frozen=True)
class EvaluationInput:
    checked: CheckedExpression
    values: Mapping[str, object]
    types: Mapping[str, CelType]
    context: ErrorContext
    result_type: ResultType = ResultType.BOOL


@dataclass(frozen=True)
class ValidationWork:
    checked: CheckedExpression


@dataclass(frozen=True)
class EvaluationWork:
    requests: tuple[Prepared | EvaluationError, ...]


Work = ValidationWork | EvaluationWork
Reply = Compilation | tuple[Result, ...] | UnavailableReason


class Phase(Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DELIVERY = "delivery"


@dataclass
class Handle:
    loop: asyncio.AbstractEventLoop
    finished: asyncio.Future[None]
    jobs: set[int] = field(default_factory=lambda: set[int]())
    timeout_reported: bool = False
    closed: bool = False
    rejected: int = 0


@dataclass
class Job:
    handle: int
    future: asyncio.Future[Reply]
    work: Work | None
    clock: Clock
    charge: int
    evaluation: bool
    phase: Phase = Phase.QUEUED
    cancelled: bool = False
    reply: Reply | None = None


@dataclass(frozen=True)
class Snapshot:
    thread_id: int | None
    failed: bool
    handles: int
    outstanding: int
    bytes: int
    queued: int
    running: int
    deliveries: int
    cache_entries: int
    cache_characters: int
    diagnostics: int


def now() -> datetime:
    return datetime.now(UTC)


def finish_close(future: asyncio.Future[None]) -> None:
    if not future.done():
        future.set_result(None)


def finish_failure(future: asyncio.Future[Reply]) -> None:
    if not future.done():
        future.set_result(UnavailableReason.FAILED)


class EvaluationThread:
    def __init__(self, binding: Binding, settings: Settings | None = None) -> None:
        self.settings = Settings() if settings is None else settings
        self.machine = Machine(
            binding, now, self.settings.cache_entries, self.settings.cache_characters
        )
        self.condition = threading.Condition()
        self._handles: dict[int, Handle] = {}
        self._jobs: dict[int, Job] = {}
        self._evaluations: deque[int] = deque()
        self._compiles: deque[int] = deque()
        self._next = 0
        self._bytes = 0
        self._failed = False
        self._cache_entries = 0
        self._cache_characters = 0
        self._started = False
        self._failure_reported = False
        self._events: deque[tuple[bool, int]] = deque()
        self._reporting = False
        with STACK_LOCK:
            previous = threading.stack_size()
            try:
                threading.stack_size(STACK_BYTES)
                context = contextvars.Context()
                self.worker = threading.Thread(
                    target=context.run,
                    args=(self._run,),
                    name="kora-cel-evaluator",
                    daemon=True,
                )
                self.worker.start()
            finally:
                threading.stack_size(previous)
            if threading.stack_size() != previous:
                raise_safe(RuntimeError("thread stack setting was not restored"))

    def open(self, loop: asyncio.AbstractEventLoop) -> tuple[int, Handle]:
        with self.condition:
            if self._failed:
                refuse(UnavailableReason.FAILED)
            if len(self._handles) + len(self._events) >= self.settings.handles:
                refuse(UnavailableReason.BUSY)
            self._next += 1
            handle = Handle(loop, loop.create_future())
            self._handles[self._next] = handle
            return self._next, handle

    def submit(
        self, handle_id: int, work: Work, clock: Clock, charge: int
    ) -> asyncio.Future[Reply]:
        with self.condition:
            if self._failed:
                refuse(UnavailableReason.FAILED)
            handle = self._handles.get(handle_id)
            if handle is None or handle.closed:
                refuse(UnavailableReason.CLOSED)
            evaluation = isinstance(work, EvaluationWork)
            count = sum(job.evaluation is evaluation for job in self._jobs.values())
            capacity = (
                self.settings.evaluation_jobs
                if evaluation
                else self.settings.compile_jobs
            )
            if (
                count >= capacity
                or self._bytes + charge > self.settings.outstanding_bytes
            ):
                refuse(UnavailableReason.BUSY)
            self._next += 1
            identifier = self._next
            future: asyncio.Future[Reply] = handle.loop.create_future()
            future.add_done_callback(partial(self._future_done, identifier))
            job = Job(handle_id, future, work, clock, charge, evaluation)
            self._jobs[identifier] = job
            handle.jobs.add(identifier)
            self._bytes += charge
            queue = self._evaluations if evaluation else self._compiles
            queue.append(identifier)
            self.condition.notify_all()
            return future

    def _future_done(self, identifier: int, future: asyncio.Future[Reply]) -> None:
        if future.cancelled():
            with self.condition:
                job = self._jobs.get(identifier)
                if job is not None:
                    job.cancelled = True
                    if job.phase is not Phase.RUNNING:
                        self._release(identifier)
                self.condition.notify_all()

    def _release(self, identifier: int) -> None:
        job = self._jobs.pop(identifier, None)
        if job is None:
            return
        self._bytes -= job.charge
        handle = self._handles.get(job.handle)
        if handle is not None:
            handle.jobs.discard(identifier)
        if identifier in self._evaluations:
            self._evaluations.remove(identifier)
        if identifier in self._compiles:
            self._compiles.remove(identifier)
        job.work = None
        job.reply = None
        self.condition.notify_all()

    def close(self, handle: Handle) -> None:
        with self.condition:
            handle.closed = True
            for job_id in tuple(handle.jobs):
                job = self._jobs[job_id]
                if job.phase is Phase.QUEUED:
                    handle.rejected += 1
                    self._complete(job_id, UnavailableReason.CLOSED)
            self._settle_handles()
            self.condition.notify_all()

    def _settle_handles(self) -> None:
        for identifier, handle in tuple(self._handles.items()):
            if not handle.closed or handle.jobs:
                continue
            del self._handles[identifier]
            self._events.append((False, handle.rejected))
            if handle.loop.is_closed():
                continue
            try:
                handle.loop.call_soon_threadsafe(finish_close, handle.finished)
            except RuntimeError:
                if not handle.loop.is_closed():
                    raise

    def _reap_loops(self) -> None:
        for handle in tuple(self._handles.values()):
            if not handle.loop.is_closed():
                continue
            handle.closed = True
            for identifier in tuple(handle.jobs):
                job = self._jobs[identifier]
                job.cancelled = True
                if job.phase is not Phase.RUNNING:
                    self._release(identifier)
        self._settle_handles()

    def _complete(self, identifier: int, reply: Reply) -> None:
        job = self._jobs.get(identifier)
        if job is None:
            return
        handle = self._handles[job.handle]
        if job.cancelled or job.future.cancelled() or handle.loop.is_closed():
            self._release(identifier)
            return
        result_charge = 1024
        if isinstance(reply, tuple):
            result_charge += sum(
                len(item) * 4
                if isinstance(item, str)
                else MAX_FIELD * 4 + MAX_EVENT_ID * 4 + 1024
                for item in reply
            )
        additional = max(0, result_charge - job.charge)
        if self._bytes + additional > self.settings.outstanding_bytes:
            reply = UnavailableReason.BUSY
        else:
            job.charge += additional
            self._bytes += additional
        job.phase = Phase.DELIVERY
        job.work = None
        job.reply = reply
        if identifier in self._evaluations:
            self._evaluations.remove(identifier)
        if identifier in self._compiles:
            self._compiles.remove(identifier)
        try:
            handle.loop.call_soon_threadsafe(self._deliver, identifier)
            self.condition.notify_all()
        except RuntimeError:
            if not handle.loop.is_closed():
                raise
            self._release(identifier)

    def _deliver(self, identifier: int) -> None:
        with self.condition:
            job = self._jobs.get(identifier)
            if job is None:
                return
            reply = job.reply
            if reply is not None and not job.future.done():
                job.future.set_result(reply)
            self._release(identifier)
            self._settle_handles()

    def _fail(self) -> None:
        with self.condition:
            if self._failed:
                return
            self._failed = True
            self._events.clear()
            notifications = [
                (self._handles[job.handle].loop, job.future)
                for job in self._jobs.values()
            ]
            for identifier in tuple(self._jobs):
                self._release(identifier)
            for handle in self._handles.values():
                handle.closed = True
            self._settle_handles()
            self.condition.notify_all()
        for loop, future in notifications:
            if loop.is_closed():
                continue
            try:
                loop.call_soon_threadsafe(
                    finish_failure, future, context=contextvars.Context()
                )
            except RuntimeError:
                if not loop.is_closed():
                    raise

    def timed_out(self, handle: Handle) -> None:
        with self.condition:
            if not self._failed and not handle.timeout_reported:
                handle.timeout_reported = True
                self._events.append((True, 0))
                self.condition.notify_all()

    def _diagnostics(self) -> bool:
        with self.condition:
            if self._failed:
                self.machine.cache.clear()
                self._cache_entries = 0
                self._cache_characters = 0
                self._events.clear()
                if self._failure_reported:
                    return False
                self._failure_reported = True
                event = EVALUATOR_FAILED
                fields: dict[str, str | int] = {"fault": "unmapped"}
            elif not self._started:
                self._started = True
                event, fields = EVALUATOR_STARTED, {}
            elif self._events:
                timed_out, rejected = self._events.popleft()
                event = EVALUATOR_CLOSE_TIMED_OUT if timed_out else EVALUATOR_STOPPED
                fields = {} if timed_out else {"rejected_jobs": rejected}
            else:
                return True
            self._reporting = True
        try:
            diagnostics = current_diagnostics()
            if diagnostics is not None:
                diagnostics.logger(CEL_COMPONENT, name=CEL_LOGGER_NAME).emit(
                    event, **fields
                )
        finally:
            with self.condition:
                self._reporting = False
                self.condition.notify_all()
        return True

    def _advance(self) -> bool:
        if not self._diagnostics():
            return False
        with self.condition:
            self._reap_loops()
            if self._failed or self._events:
                return True
            if not self._evaluations and not self._compiles:
                self.condition.wait(REAP_SECONDS)
                return True
            queue = self._evaluations if self._evaluations else self._compiles
            identifier = queue.popleft()
            job = self._jobs[identifier]
            if job.cancelled or job.future.cancelled():
                self._release(identifier)
                return True
            job.phase = Phase.RUNNING
            work = job.work
        if isinstance(work, ValidationWork):
            reply: Reply = self.machine.validate(work.checked)
        elif isinstance(work, EvaluationWork):
            reply = tuple(
                request
                if isinstance(request, EvaluationError)
                else self.machine.run(request, job.clock)
                for request in work.requests
            )
        else:
            raise RuntimeError("accepted evaluator job has no work")
        with self.condition:
            cache = self.machine.cache.snapshot()
            self._cache_entries, self._cache_characters = (
                cache.entries,
                cache.characters,
            )
            self._complete(identifier, reply)
            self._settle_handles()
        return True

    def _step(self) -> bool:
        try:
            return self._advance()
        except Exception:
            self._fail()
            return True

    def _run(self) -> None:
        try:
            while self._step():
                pass
        finally:
            self._fail()
            with self.condition:
                self.machine.cache.clear()
                self._cache_entries = 0
                self._cache_characters = 0
                self._events.clear()
                self.condition.notify_all()

    def snapshot(self) -> Snapshot:
        with self.condition:
            phases = [job.phase for job in self._jobs.values()]
            return Snapshot(
                self.worker.ident,
                self._failed,
                len(self._handles),
                len(self._jobs),
                self._bytes,
                phases.count(Phase.QUEUED),
                phases.count(Phase.RUNNING),
                phases.count(Phase.DELIVERY),
                self._cache_entries,
                self._cache_characters,
                len(self._events) + int(self._reporting),
            )


class ProcessOwner:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.thread: EvaluationThread | None = None

    def get(self) -> EvaluationThread:
        with self.lock:
            if self.thread is None:
                self.thread = EvaluationThread(contextvars.Context().run(load_binding))
            return self.thread


PROCESS: ProcessOwner = ProcessOwner()


class Evaluator:
    def __init__(self, clock: Clock, owner: EvaluationThread | None = None) -> None:
        self.clock = clock
        self.owner = PROCESS.get() if owner is None else owner
        self.identifier, self.handle = self.owner.open(asyncio.get_running_loop())

    def _loop(self) -> None:
        if asyncio.get_running_loop() is not self.handle.loop:
            refuse(UnavailableReason.WRONG_LOOP)

    async def _reply(self, work: Work, charge: int) -> Reply:
        self._loop()
        result = await self.owner.submit(self.identifier, work, self.clock, charge)
        if isinstance(result, UnavailableReason):
            refuse(result)
        return result

    async def validate(self, checked: CheckedExpression) -> Compilation:
        if len(checked.rendering) > MAX_RENDERING:
            refuse(UnavailableReason.BUSY)
        result = await self._reply(
            ValidationWork(checked), len(checked.rendering) * 4 + checked.bound_bytes
        )
        if not isinstance(result, Compilation):
            raise_safe(RuntimeError("invalid validation reply"))
        return result

    async def evaluate(
        self,
        checked: CheckedExpression,
        values: Mapping[str, object],
        types: Mapping[str, CelType],
        context: ErrorContext,
        result_type: ResultType = ResultType.BOOL,
    ) -> Result:
        results = await self.evaluate_batch(
            [EvaluationInput(checked, values, types, context, result_type)]
        )
        return results[0]

    async def evaluate_batch(
        self, inputs: Sequence[EvaluationInput]
    ) -> tuple[Result, ...]:
        self._loop()
        if not inputs or len(inputs) > self.owner.settings.batch_size:
            refuse(UnavailableReason.BUSY)
        prepared: list[Prepared | EvaluationError] = []
        charge = 0
        for request in inputs:
            context = request.context
            event_id = context.event_id
            if (
                len(request.checked.rendering) > MAX_RENDERING
                or len(context.field) > MAX_FIELD
                or (event_id is not None and len(event_id) > MAX_EVENT_ID)
            ):
                refuse(UnavailableReason.BUSY)
            bound = bind(request.checked, request.values, request.types)
            charge += (
                len(request.checked.rendering) * 4
                + len(context.field) * 4
                + MAX_EVENT_ID * 4
                + 1024
                + request.checked.bound_bytes
            )
            if isinstance(bound, ErrorKind):
                prepared.append(context.record(bound, self.clock))
            else:
                charge += bound.bytes + bound.count * 64
                prepared.append(
                    Prepared(request.checked, bound, context, request.result_type)
                )
            if charge > self.owner.settings.outstanding_bytes:
                refuse(UnavailableReason.BUSY)
        result = await self._reply(EvaluationWork(tuple(prepared)), charge)
        if not isinstance(result, tuple):
            raise_safe(RuntimeError("invalid evaluation reply"))
        return result

    async def close(self) -> None:
        self._loop()
        self.owner.close(self.handle)
        timed_out = False
        try:
            await asyncio.wait_for(
                asyncio.shield(self.handle.finished), self.owner.settings.close_timeout
            )
        except TimeoutError:
            timed_out = True
        if timed_out:
            self.owner.timed_out(self.handle)
            refuse(UnavailableReason.CLOSE_TIMED_OUT)

    def snapshot(self) -> Snapshot:
        return self.owner.snapshot()
