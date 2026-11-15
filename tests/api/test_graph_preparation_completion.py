import asyncio
import sys
import threading
import weakref
from collections.abc import Coroutine
from dataclasses import dataclass
from types import FrameType
from typing import Literal

import pytest

from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.api.graph_preparation_completion import (
    PreparationCompletion,
    PreparationDelivery,
    PreparationOutcome,
    clear_preparation_error,
)
from kora.cel_eval.binding import load_binding
from kora.cel_eval.errors import EvaluatorUnavailable, UnavailableReason
from kora.cel_eval.thread import EvaluationThread, Evaluator
from kora.protocols.effective_artifact_type import EffectiveArtifactType
from tests.api.test_graph_deployment_worker import preparation_threads
from tests.cel_eval.pattern_validation_scenarios import assert_settled
from tests.db.closing import until
from tests.plugins.authentication.support import AuthorityClock
from tests.protocols.effective_artifact_cases import prepared
from tests.support.output_bindings import publication_binding
from tests.support.parameter_bindings import parameter_binding

Mode = Literal["value", "fault", "group", "cancelled", "unavailable", "invocation"]


@dataclass
class PreparedSource:
    holder: EffectiveArtifactType


@dataclass
class PreparedWork:
    mode: Mode
    release: threading.Event
    started: threading.Event
    sources: list[weakref.ReferenceType[PreparedSource]]
    tasks: list[weakref.ReferenceType[asyncio.Task[object]]]

    def __call__(self, evaluator: Evaluator) -> Coroutine[None, None, PreparedSource]:
        if self.mode == "invocation":
            source = PreparedSource(prepared())
            self.sources.append(weakref.ref(source))
            raise ValueError("private invocation source")
        return self.run(evaluator)

    async def run(self, _: Evaluator) -> PreparedSource:
        source = PreparedSource(prepared())
        self.sources.append(weakref.ref(source))
        task = asyncio.current_task()
        assert task is not None
        self.tasks.append(weakref.ref(task))
        del task
        self.started.set()
        await asyncio.to_thread(self.release.wait)
        if self.mode == "fault":
            error = ValueError("private schema source")
            error.__cause__ = error
            raise error
        if self.mode == "group":
            try:
                raise ValueError("private nested schema source")
            except ValueError as inner:
                grouped = ExceptionGroup(
                    "private schema group",
                    [inner, inner, ExceptionGroup("private repeated", [inner])],
                )
                inner.__cause__ = grouped
                grouped.__context__ = grouped
                raise grouped from inner
        if self.mode == "cancelled":
            raise asyncio.CancelledError("private cancellation")
        if self.mode == "unavailable":
            raise EvaluatorUnavailable(UnavailableReason.BUSY)
        return source


async def invoke(preparer: DeploymentPreparer, work: PreparedWork) -> PreparedSource:
    async with preparer.reserve() as reservation:
        assert reservation is not None
        operation = reservation.run(work)
        del work
        return await operation


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["fault", "group", "cancelled", "unavailable", "invocation"]
)
async def test_preparation_failure_disposes_sources_before_next_slot(
    mode: Mode,
) -> None:
    owner = EvaluationThread(load_binding())
    sources: list[weakref.ReferenceType[PreparedSource]] = []
    tasks: list[weakref.ReferenceType[asyncio.Task[object]]] = []
    release = threading.Event()
    release.set()
    work = PreparedWork(mode, release, threading.Event(), sources, tasks)
    work_reference = weakref.ref(work)

    async with DeploymentPreparer(
        AuthorityClock(),
        owner,
        parameter_binding=parameter_binding(),
        binding=publication_binding(),
    ) as preparer:
        operation = invoke(preparer, work)
        del work
        expected = asyncio.CancelledError if mode == "cancelled" else RuntimeError
        with pytest.raises(expected) as failure:
            await operation
        async with preparer.reserve() as next_reservation:
            assert next_reservation is not None
            released_sources = all(reference() is None for reference in sources)
            released_tasks = all(reference() is None for reference in tasks)
            released_callable = work_reference() is None

    assert sources
    assert released_sources
    assert released_tasks
    assert released_callable
    assert "private" not in str(failure.value)
    assert failure.value.__cause__ is None
    assert failure.value.__context__ is None
    if mode == "unavailable":
        assert type(failure.value) is EvaluatorUnavailable
        assert failure.value.reason is UnavailableReason.BUSY
    assert preparation_threads() == []
    assert_settled(owner)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["value", "fault", "group", "cancelled", "unavailable"]
)
async def test_abandoned_preparation_disposes_success_and_failure(mode: Mode) -> None:
    owner = EvaluationThread(load_binding())
    sources: list[weakref.ReferenceType[PreparedSource]] = []
    tasks: list[weakref.ReferenceType[asyncio.Task[object]]] = []
    release, started = threading.Event(), threading.Event()
    work = PreparedWork(mode, release, started, sources, tasks)
    work_reference = weakref.ref(work)

    async with DeploymentPreparer(
        AuthorityClock(),
        owner,
        parameter_binding=parameter_binding(),
        binding=publication_binding(),
    ) as preparer:
        caller = asyncio.create_task(invoke(preparer, work))
        del work
        try:
            await until(started.is_set)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            held = preparer.busy
            async with preparer.reserve() as denied:
                refused = denied is None
        finally:
            release.set()
        async with preparer.hold():
            released_sources = all(reference() is None for reference in sources)
            released_tasks = all(reference() is None for reference in tasks)
            released_callable = work_reference() is None

    assert held
    assert refused
    assert sources
    assert released_sources
    assert released_tasks
    assert released_callable
    assert preparation_threads() == []
    assert_settled(owner)


@pytest.mark.asyncio
async def test_preparation_success_transfers_only_the_live_result() -> None:
    owner = EvaluationThread(load_binding())
    sources: list[weakref.ReferenceType[PreparedSource]] = []
    tasks: list[weakref.ReferenceType[asyncio.Task[object]]] = []
    release = threading.Event()
    release.set()
    work = PreparedWork("value", release, threading.Event(), sources, tasks)

    async with DeploymentPreparer(
        AuthorityClock(),
        owner,
        parameter_binding=parameter_binding(),
        binding=publication_binding(),
    ) as preparer:
        operation = invoke(preparer, work)
        del work
        result = await operation
        async with preparer.reserve() as next_reservation:
            assert next_reservation is not None
            assert sources[0]() is result
            assert all(reference() is None for reference in tasks)
        del result
        released = sources[0]() is None

    assert released
    assert preparation_threads() == []
    assert_settled(owner)


@pytest.mark.asyncio
async def test_preparation_unconfirmed_frame_cleanup_cannot_settle() -> None:
    acknowledgements: list[bool] = []
    delivery = PreparationDelivery[None](lambda: acknowledgements.append(True))
    try:
        raise RuntimeError("private still-active frame")
    except RuntimeError as error:
        cleaned = clear_preparation_error(error, None)
    completion = PreparationCompletion[None](PreparationOutcome.FAULT, cleaned=cleaned)

    delivery.receive(completion)
    with pytest.raises(RuntimeError, match="^Graph preparation failed[.]$"):
        delivery.take()
    delivery.abandon()

    assert not cleaned
    assert acknowledgements == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["value", "fault"])
async def test_cancellation_after_delivery_discards_unclaimed_completion(
    mode: Mode,
) -> None:
    owner = EvaluationThread(load_binding())
    sources: list[weakref.ReferenceType[PreparedSource]] = []
    tasks: list[weakref.ReferenceType[asyncio.Task[object]]] = []
    release = threading.Event()
    release.set()
    work = PreparedWork(mode, release, threading.Event(), sources, tasks)
    caller: asyncio.Task[PreparedSource] | None = None
    interruptions: list[bool] = []

    def interrupt(frame: FrameType, event: str, _: object) -> None:
        if (
            event == "return"
            and frame.f_code is PreparationDelivery[PreparedSource].receive.__code__
        ):
            assert caller is not None
            interruptions.append(caller.cancel())

    async with DeploymentPreparer(
        AuthorityClock(),
        owner,
        parameter_binding=parameter_binding(),
        binding=publication_binding(),
    ) as preparer:
        incumbent = sys.getprofile()
        try:
            sys.setprofile(interrupt)
            caller = asyncio.create_task(invoke(preparer, work))
            del work
            with pytest.raises(asyncio.CancelledError):
                await caller
        finally:
            sys.setprofile(incumbent)
        async with preparer.hold():
            released_sources = all(reference() is None for reference in sources)
            released_tasks = all(reference() is None for reference in tasks)

    assert interruptions == [True]
    assert sources
    assert released_sources
    assert released_tasks
    assert preparation_threads() == []
    assert_settled(owner)
