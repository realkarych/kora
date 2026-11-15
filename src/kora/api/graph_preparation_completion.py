import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from enum import Enum, auto
from inspect import currentframe
from types import FrameType
from typing import Generic, TypeVar, cast

from kora.cel_eval.errors import EvaluatorUnavailable, UnavailableReason, raise_safe
from kora.cel_eval.thread import Evaluator

T = TypeVar("T")


class PreparationOutcome(Enum):
    VALUE = auto()
    CANCELLED = auto()
    FAULT = auto()
    UNAVAILABLE = auto()


@dataclass(slots=True)
class PreparationCompletion(Generic[T]):
    outcome: PreparationOutcome
    value: T | None = None
    unavailable: UnavailableReason | None = None
    cleaned: bool = True


def clear_preparation_error(error: BaseException, active: FrameType | None) -> bool:
    pending = [error]
    seen = {id(error)}
    cleaned = active is not None
    while pending:
        current = pending.pop()
        children = (
            cast(BaseExceptionGroup[BaseException], current).exceptions
            if isinstance(current, BaseExceptionGroup)
            else ()
        )
        for child in (current.__cause__, current.__context__):
            if child is not None and id(child) not in seen:
                seen.add(id(child))
                pending.append(child)
        for child in children:
            if id(child) not in seen:
                seen.add(id(child))
                pending.append(child)
        traceback = current.__traceback__
        current.__traceback__ = None
        current.__cause__ = None
        current.__context__ = None
        while traceback is not None:
            frame = traceback.tb_frame
            if frame is not active:
                try:
                    frame.clear()
                except RuntimeError:
                    cleaned = False
            traceback = traceback.tb_next
    return cleaned


class PreparationDelivery(Generic[T]):
    def __init__(self, settled: Callable[[], None]) -> None:
        self.caller = asyncio.get_running_loop()
        self.ready: asyncio.Future[None] = self.caller.create_future()
        self._settled = settled
        self._completion: PreparationCompletion[T] | None = None
        self._abandoned = False

    def _consume(self) -> PreparationCompletion[T]:
        completion, self._completion = self._completion, None
        if completion is None:
            raise RuntimeError("Preparation has no completion.")
        if completion.cleaned:
            self._settled()
        return completion

    def abandon(self) -> None:
        self._abandoned = True
        if self._completion is not None:
            self._completion.value = None
            self._consume()

    def take(self) -> T:
        completion = self._consume()
        value, completion.value = completion.value, None
        if completion.outcome is PreparationOutcome.VALUE:
            return cast(T, value)
        if completion.outcome is PreparationOutcome.CANCELLED:
            raise_safe(asyncio.CancelledError())
        if completion.outcome is PreparationOutcome.UNAVAILABLE:
            raise_safe(
                EvaluatorUnavailable(cast(UnavailableReason, completion.unavailable))
            )
        raise_safe(RuntimeError("Graph preparation failed."))

    def receive(self, completion: PreparationCompletion[T]) -> None:
        self._completion = completion
        if self._abandoned:
            self.abandon()
        self.ready.set_result(None)

    def forward(self, completion: PreparationCompletion[T]) -> None:
        self.caller.call_soon_threadsafe(self.receive, completion)

    def finished(self, task: asyncio.Task[T]) -> None:
        error: BaseException | None
        try:
            error = task.exception()
        except asyncio.CancelledError as cancelled:
            error = cancelled
        if error is None:
            completion = PreparationCompletion(PreparationOutcome.VALUE, task.result())
        else:
            outcome = PreparationOutcome.FAULT
            unavailable: UnavailableReason | None = None
            if type(error) is asyncio.CancelledError:
                outcome = PreparationOutcome.CANCELLED
            elif (
                type(error) is EvaluatorUnavailable
                and type(error.reason) is UnavailableReason
            ):
                outcome = PreparationOutcome.UNAVAILABLE
                unavailable = error.reason
            cleaned = clear_preparation_error(error, currentframe())
            completion = PreparationCompletion[T](
                outcome if cleaned else PreparationOutcome.FAULT,
                unavailable=unavailable,
                cleaned=cleaned,
            )
        del task, error
        asyncio.get_running_loop().call_soon(self.forward, completion)


async def invoke_preparation(
    work: Callable[[Evaluator], Coroutine[None, None, T]], evaluator: Evaluator
) -> T:
    operation = work(evaluator)
    del work, evaluator
    return await operation


def start_preparation(
    work: Callable[[Evaluator], Coroutine[None, None, T]],
    evaluator: Evaluator,
    delivery: PreparationDelivery[T],
) -> None:
    task = asyncio.get_running_loop().create_task(invoke_preparation(work, evaluator))
    task.add_done_callback(delivery.finished)
