import asyncio
import errno
import os
import threading
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Final, Literal
from weakref import ReferenceType, ref

import pytest
from pydantic import TypeAdapter

from kora.conformance.capture import Capture, captured
from kora.conformance.common import context_for, kora_capture
from kora.ir.json import JsonObject, JsonValue
from kora.ir.vcs_ref_json import canonical_bytes
from kora.observability import Level, LoggingSettings
from kora.observability.context import bound_scope
from kora.plugins.harness.fake.adapter import NAME, SESSION_ID, VERSION
from kora.plugins.runtime.local.agent import AgentSources
from kora.plugins.runtime.local.channel import PRE_ATTACH_BYTES, FrameType, read_request
from kora.plugins.runtime.local.completed import recorded_exited
from kora.plugins.runtime.local.runtime import LocalRuntime
from kora.plugins.runtime.local.serving import COMPONENT, ControlServer, rebound
from kora.protocols.deadlines import ExecutionDeadline
from kora.protocols.harness import AgentExecution
from kora.protocols.runtime import SessionObservation, Workload
from kora.protocols.vcs import WorkspaceRef
from kora.runner.agent import STOP_DEADLINE_NS, HostedHarness, NativeResultWork
from kora.runner.channels import BoundedChannel, ChannelError, ChannelFailure
from kora.runner.filesystem import open_local_directory
from kora.runner.identity import NativeIdentityReader
from kora.runner.journal import BatchAppend, JournalAppendOutcome, RunningJournal
from kora.runner.native_io import NativeIOBusy
from kora.runner.peers import kernel_source
from kora.runner.registry import (
    AttemptKey,
    CompletedResult,
    ControlFileRef,
    Registry,
    Submitter,
)
from kora.runner.session_result import (
    FINALIZATION_NAME,
    LEGACY_RESULT_NAME,
    RESULT_DIRECTORY,
    RESULT_NAME,
    FinalizationImplementation,
    FinalizationOrigin,
    FinalizationReferences,
    PreparedObservation,
    digest_of,
    prepare_observation,
    read_finalization,
)
from kora.runner.session_result_encoding import ResultEncoder
from kora.testing.local import (
    TESTING_PRICE_TABLE,
    script_chunks,
    script_item,
    unless_busy,
)
from kora.testing.runner import AllowDenyPolicies
from kora.testing.script import FakeScript
from tests.plugins.harness.fake.fake_support import result
from tests.plugins.runtime.local.test_agent_session import (
    agent_fields,
    agent_session,
    code_of_call,
    detached_workload,
)
from tests.plugins.runtime.local.test_command import journal_events
from tests.plugins.runtime.local.test_command_observation import SUBMITTER
from tests.plugins.runtime.local.test_completion import (
    answered,
    answered_code,
    completed_entry,
    started_on,
)
from tests.plugins.runtime.local.testing_support import attempt, composition
from tests.runner.agent_support import (
    FAKE_SPEC,
    FIXED_NOW,
    FakeResolver,
    GatedScript,
    ObservedFakeHarness,
    SteppedClock,
    make_world,
    script,
)
from tests.runner.test_runner_outcome_covers_every_stop_deadline_escalation import (
    SECOND,
    deadline,
)
from tests.runner.test_session_result_protection import observation
from tests.support.local_delivery import delivery_server
from tests.support.local_peers import CONDITION_SECONDS, identity_of, until
from tests.support.local_runners import short_directory, submitted, written
from tests.support.local_runtime import Identifiers, private_directory, protocol_error

__all__ = ["kora_capture"]

DEADLINE_NS: Final[int] = 120 * SECOND
CHECKOUT_SCRIPT: Final[JsonObject] = result()


def scripted_output(value: JsonValue) -> FakeScript:
    return FakeScript.model_validate(
        {
            "end": "completed",
            "on_interrupt": "acknowledge",
            "steps": [
                {"kind": "output", "source": "structured_output", "value": value},
                CHECKOUT_SCRIPT,
            ],
        }
    )


async def collected(stream: AsyncIterator[bytes], values: list[bytes]) -> None:
    async for chunk in stream:
        values.append(chunk)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [{"nested": [True, -0.0, "é\\\n"]}, None])
async def test_session_observation_replays_checked_result_after_runner_exit(
    tmp_path: Path, kora_capture: Capture, value: JsonValue
) -> None:
    expected = observation(
        value, harness=NAME, session_id=SESSION_ID, installed_version=VERSION
    )
    original = canonical_bytes(expected.model_dump(mode="json"))
    digest = digest_of(original)
    files: list[bytes] = []
    async with composition(tmp_path, kora_capture) as local:
        handle = await local.run_agent("attempt-1", scripted_output(value))
        control = attempt(local) / "control" / RESULT_DIRECTORY

        live = await unless_busy(
            lambda: local.runtime.session_observation(handle, local.ctx)
        )
        live_bytes = (control / RESULT_NAME).read_bytes()
        await protocol_error(
            collected(local.runtime.transfer_out(handle, RESULT_NAME, local.ctx), files)
        )
        await protocol_error(
            collected(local.runtime.read_log(handle, "stdout", 0, local.ctx), files)
        )
        quiescence = await unless_busy(
            lambda: local.runtime.quiescence(handle, local.ctx)
        )
        entry = completed_entry(local.runtime)
        await until(lambda: recorded_exited(NativeIdentityReader(), entry.runner))
        completed = await local.runtime.session_observation(handle, local.ctx)
        completed_bytes = (control / RESULT_NAME).read_bytes()
        before_live_proof = await protocol_error(
            local.runtime.session_finalization(handle, local.ctx)
        )
        await protocol_error(
            collected(
                local.runtime.transfer_out(handle, FINALIZATION_NAME, local.ctx), files
            )
        )
        await protocol_error(
            collected(local.runtime.transfer_out(handle, RESULT_NAME, local.ctx), files)
        )
        await protocol_error(
            collected(local.runtime.read_log(handle, "stdout", 0, local.ctx), files)
        )

        (control / LEGACY_RESULT_NAME).write_bytes(original)
        legacy_ref = ControlFileRef(
            name=LEGACY_RESULT_NAME, size=len(original), digest=digest
        )
        await written(
            local.runtime,
            handle.attempt_id,
            entry.model_copy(
                update={"result": CompletedResult(kind="agent", ref=legacy_ref)}
            ),
            local.ctx,
        )
        legacy = await protocol_error(
            local.runtime.session_observation(handle, local.ctx)
        )
        await written(
            local.runtime,
            handle.attempt_id,
            entry.model_copy(update={"result": None}),
            local.ctx,
        )
        unowned = await protocol_error(
            local.runtime.session_observation(handle, local.ctx)
        )
        retained_quiescence = await local.runtime.quiescence(handle, local.ctx)
        await local.runtime.release(handle, local.ctx)

    assert live == completed == expected
    assert live_bytes == completed_bytes == original
    assert entry.result == CompletedResult(
        kind="agent",
        ref=ControlFileRef(name=RESULT_NAME, size=len(original), digest=digest),
    )
    assert expected.result.output is not None
    assert expected.result.output.source == "structured_output"
    assert expected.result.output.value == value
    assert legacy.code == unowned.code == before_live_proof.code == "session_unobserved"
    assert files == []
    assert retained_quiescence == quiescence
    assert not control.exists()


@pytest.mark.asyncio
async def test_agent_session_finalization_keeps_ordinary_result_on_optional_failure(
    tmp_path: Path, kora_capture: Capture
) -> None:
    source = scripted_output({"value": "ordinary checked output"})
    async with composition(tmp_path, kora_capture) as local:
        work = await local.workload("attempt-1", source)
        runtime, ctx = local.runtime, local.ctx
        handle = await runtime.submit(work, ctx)
        buffer = source.serialise()
        await runtime.transfer_in(
            handle, script_item(buffer), script_chunks(buffer), ctx
        )
        control = attempt(local) / "control" / RESULT_DIRECTORY
        control.mkdir(mode=0o700, parents=True, exist_ok=True)
        sibling = control / FINALIZATION_NAME
        planted = b'{"candidate":"unowned"}'
        sibling.write_bytes(planted)

        await unless_busy(lambda: runtime.start(handle, ctx))
        source_refused = await protocol_error(
            unless_busy(lambda: runtime.session_finalization(handle, ctx))
        )
        live = await runtime.session_observation(handle, ctx)
        payload = (control / RESULT_NAME).read_bytes()
        remaining = sibling.read_bytes()
        await unless_busy(lambda: runtime.quiescence(handle, ctx))
        entry = completed_entry(runtime)
        await until(lambda: recorded_exited(NativeIdentityReader(), entry.runner))
        retained = await runtime.session_observation(handle, ctx)
        await runtime.release(handle, ctx)

    assert source_refused.code == "session_unobserved"
    assert (
        retained
        == live
        == observation(
            {"value": "ordinary checked output"},
            harness=NAME,
            session_id=SESSION_ID,
            installed_version=VERSION,
        )
    )
    assert entry.result is not None
    assert entry.result.ref == ControlFileRef(
        name=RESULT_NAME, size=len(payload), digest=digest_of(payload)
    )
    assert remaining == planted
    assert not control.exists()


class HeldEncoder(ResultEncoder):
    def __init__(
        self, forms: tuple[str, ...], stop: threading.Event, held: "HeldPreparation"
    ) -> None:
        super().__init__(forms, stop)
        self.held = held

    def string(self, value: str, content: bool) -> None:
        super().string(value, content)
        if content and self.matcher.entries and not self.held.entered.is_set():
            self.held.pause(self)


class HeldPreparation:
    def __init__(self, hold_completed: bool = False) -> None:
        self.entered = threading.Event()
        self.gate = threading.Event()
        self.completed = threading.Event()
        self.return_gate = threading.Event()
        self.returning = threading.Event()
        self.hold_completed = hold_completed
        self.thread: threading.Thread | None = None
        self.stop: threading.Event | None = None
        self.encoder: ReferenceType[ResultEncoder] | None = None
        self.prepared: ReferenceType[PreparedObservation] | None = None
        self.completed_clean = False
        self.forms_size = 0
        self.calls = 0

    def __call__(
        self,
        result: SessionObservation,
        forms: tuple[str, ...],
        stop: threading.Event,
    ) -> PreparedObservation:
        self.calls += 1
        self.thread = threading.current_thread()
        self.stop = stop
        self.forms_size = sum(len(form.encode()) for form in forms)
        prepared = prepare_observation(result, forms, stop, encoder_factory=self.build)
        if self.hold_completed:
            self.prepared = ref(prepared)
            self.completed_clean = prepared.failure is None
            self.completed.set()
            if not self.return_gate.wait(CONDITION_SECONDS):
                raise TimeoutError("prepared result did not receive its test release")
        self.returning.set()
        return prepared

    def build(self, forms: tuple[str, ...], stop: threading.Event) -> ResultEncoder:
        return HeldEncoder(forms, stop, self)

    def pause(self, encoder: ResultEncoder) -> None:
        self.encoder = ref(encoder)
        self.entered.set()
        if not self.gate.wait(CONDITION_SECONDS):
            raise TimeoutError("result preparation did not receive its test release")

    def retained_work(self) -> tuple[int, int, int]:
        encoder = None if self.encoder is None else self.encoder()
        if encoder is None:
            return (0, 0, 0)
        entries = encoder.matcher.entries
        return (
            len(encoder.buffer),
            len(entries),
            sum(len(entry.raw) for entry in entries),
        )

    def retained_prepared(self) -> int:
        prepared = None if self.prepared is None else self.prepared()
        return 0 if prepared is None else len(prepared.payload)

    def release(self) -> None:
        self.gate.set()
        self.return_gate.set()

    def fenced(self) -> bool:
        return self.stop is not None and self.stop.is_set()


def descriptors_for(path: Path) -> tuple[int, ...]:
    info = path.stat()
    descriptors: list[int] = []
    for name in os.listdir("/dev/fd"):
        if not name.isdecimal():
            continue
        descriptor = int(name)
        try:
            current = os.fstat(descriptor)
        except OSError as error:
            if error.errno != errno.EBADF:
                raise
            continue
        if (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino):
            descriptors.append(descriptor)
    return tuple(sorted(descriptors))


@pytest.mark.asyncio
@pytest.mark.parametrize("expires", [False, True], ids=["shutdown", "deadline"])
async def test_session_result_scan_settles_on_cancel_and_shutdown(
    tmp_path: Path, kora_capture: Capture, expires: bool
) -> None:
    clock = SteppedClock()
    binding = TypeAdapter(ExecutionDeadline).dump_python(deadline(120), mode="json")
    work = detached_workload(
        {"deadline": binding if expires else None, "session_spec": FINALIZATION_SPEC}
    )
    resolver = FinalizationResolver(
        script([CHECKOUT_SCRIPT]), context_for(kora_capture)
    )
    sources = AgentSources(resolver, AllowDenyPolicies())
    held = HeldPreparation()
    registry = kora_capture.diagnostics.registry
    old_scope = (registry.count(), registry.retained_bytes())
    with short_directory() as sockets:
        bound = rebound(sockets / "sockets", "scan")
        registry_owner = Registry(
            open_local_directory(sockets / "sockets"), "test-domain", lambda: "a" * 32
        )
        try:
            async with agent_session(
                tmp_path,
                kora_capture,
                sources,
                clock,
                prepare_result=held,
                registration=(
                    AttemptKey(work.execution_domain_id, work.attempt_id),
                    identity_of(os.getpid()),
                ),
            ) as (session, attempt_path):
                reader = NativeIdentityReader(clock=clock)
                server = ControlServer(
                    bound,
                    sockets / "sockets",
                    "scan",
                    clock,
                    kernel_source(reader),
                    kora_capture.diagnostics.logger(COMPONENT),
                    registry_owner,
                    registry_owner.key("attempt-1"),
                    identity_of(os.getpid()),
                    reader,
                    agent=session,
                )
                session.accept(SUBMITTER, None, work)
                await unless_busy(
                    lambda: asyncio.to_thread(
                        session.supervise, SUBMITTER, False, work, lambda: None
                    )
                )
                closing: asyncio.Task[None] | None = None
                try:
                    await until(held.entered.is_set)
                    control = attempt_path / "control"
                    original_control = descriptors_for(control)
                    assert resolver.adapters
                    while_held = (registry.count(), registry.retained_bytes())
                    retained = held.retained_work()
                    before_stop = code_of_call(
                        lambda: session.session_observation(SUBMITTER)
                    )
                    if expires:
                        await clock.requested(DEADLINE_NS, count=2)
                        await clock.advance_to(DEADLINE_NS)
                        await until(held.fenced)
                        assert (
                            code_of_call(lambda: session.session_observation(SUBMITTER))
                            == "session_unobserved"
                        )
                    closing = asyncio.create_task(server.close())
                    await until(held.fenced)
                    stop_at = clock.monotonic_ns() + STOP_DEADLINE_NS
                    await clock.requested(stop_at)
                    await clock.advance_to(stop_at)
                    await until(
                        lambda: any(
                            record.get("event") == "runner.agent.stop_unsettled"
                            for record in kora_capture.reader.records()
                        )
                    )
                    if not expires:
                        closing.cancel()

                    assert before_stop == "workload_busy"
                    assert original_control
                    assert retained[0] > 0
                    assert retained[1] == while_held[0] - old_scope[0] > 0
                    assert retained[2] == while_held[1] - old_scope[1] > 0
                    assert held.retained_work() == retained
                    assert not closing.done()
                    assert not server.resources_settled
                    assert not session.result_work_settled()
                    assert session.result_reference() is None
                    assert descriptors_for(control) == original_control
                    assert (registry.count(), registry.retained_bytes()) == while_held
                    assert held.calls == 1
                    assert held.forms_size == while_held[1] - old_scope[1]
                    assert not (control / RESULT_DIRECTORY / RESULT_NAME).exists()
                finally:
                    held.release()
                    if closing is not None:
                        await asyncio.gather(closing, return_exceptions=True)
                    await server.close()
                assert server.resources_settled
                assert session.result_work_settled()
                assert descriptors_for(control) == ()
                assert (registry.count(), registry.retained_bytes()) == old_scope
                assert session.result_reference() is None
                assert (
                    code_of_call(lambda: session.session_observation(SUBMITTER))
                    == "session_unobserved"
                )
                assert held.thread is not None
                assert not held.thread.is_alive()
                assert held.retained_work() == (0, 0, 0)
                assert held.retained_prepared() == 0
                assert not (control / RESULT_DIRECTORY / RESULT_NAME).exists()
                after_drain = {
                    event.kind
                    for event in journal_events(attempt_path / "journal.jsonl")
                }
                assert "runner.session_spec" in after_drain
                assert not (control / RESULT_DIRECTORY / FINALIZATION_NAME).exists()
                assert "runner.session_result" not in after_drain
                assert "runner.session_failed" not in after_drain
        finally:
            bound.close()
            registry_owner.directory.close()


@pytest.mark.asyncio
async def test_result_prepared_before_deadline_and_returned_late_stays_unpublished(
    tmp_path: Path, kora_capture: Capture
) -> None:
    clock = SteppedClock()
    binding = TypeAdapter(ExecutionDeadline).dump_python(deadline(120), mode="json")
    fields = {
        **agent_fields(),
        "deadline": binding,
        "session_spec": FINALIZATION_SPEC,
    }
    expected_domain = Identifiers()()
    resolver = FinalizationResolver(
        script([CHECKOUT_SCRIPT]), context_for(kora_capture)
    )
    sources = AgentSources(resolver, AllowDenyPolicies())
    held = HeldPreparation(hold_completed=True)

    def work_for(runtime: LocalRuntime) -> Workload:
        return submitted(runtime, "attempt-1", **fields)

    async with (
        agent_session(
            tmp_path,
            kora_capture,
            sources,
            clock,
            prepare_result=held,
            registration=(
                AttemptKey(expected_domain, "attempt-1"),
                identity_of(os.getpid()),
            ),
        ) as (
            session,
            path,
        ),
        delivery_server(
            private_directory(tmp_path, "server"),
            kora_capture,
            clock=clock,
            agent=session,
            workload=work_for,
        ) as live,
    ):
        assert live.work.execution_domain_id == expected_domain
        journal = path / "journal.jsonl"
        result_file = path / "control" / RESULT_DIRECTORY / RESULT_NAME
        try:
            channel = await live.attach()
            try:
                await live.deliver(channel)
            finally:
                channel.close()
            await unless_busy(lambda: started_on(live))
            await until(held.entered.is_set)
            held.gate.set()
            await until(held.completed.is_set)
            await clock.requested(DEADLINE_NS, count=2)
            await clock.advance_to(DEADLINE_NS)
            await until(held.fenced)
            pending = await answered_code(live)
            retained = held.retained_prepared()
            observed = code_of_call(
                lambda: session.session_observation(live.entry.submitter)
            )
            assert not live.server.resources_settled
            assert not session.result_work_settled()

            held.return_gate.set()
            await until(session.result_work_settled)
            file_after_return = result_file.exists()
            sibling_after_return = result_file.with_name(FINALIZATION_NAME).exists()
            reference_after_return = session.result_reference()
            after_return = tuple(event.kind for event in journal_events(journal))
            quiescence = await unless_busy(lambda: answered(live, clock))
            saved = completed_entry(live.runtime)
            completed_event = journal_events(journal)[-1]
            assert not live.server.resources_settled
        finally:
            held.release()

    assert pending == "workload_busy"
    assert observed == "session_unobserved"
    assert retained > 0
    assert held.completed_clean
    assert held.returning.is_set()
    assert held.calls == 1
    assert held.retained_prepared() == 0
    assert held.thread is not None
    assert not held.thread.is_alive()
    assert not file_after_return
    assert not sibling_after_return
    assert reference_after_return is None
    assert "runner.session_spec" in after_return
    assert "runner.session_result" not in after_return
    assert "runner.session_failed" not in after_return
    assert saved.result is None
    assert saved.quiescence == quiescence
    assert completed_event.kind == "runner.quiescence"
    assert completed_event.data == quiescence.model_dump(mode="json")


@pytest.mark.asyncio
async def test_native_result_work_rejects_a_second_admission_and_unwinds_on_stop() -> (
    None
):
    work = NativeResultWork()
    held = HeldPreparation()
    candidate = observation({"content": "safe value"})
    forms = ("registered-value",)

    try:
        pending = asyncio.create_task(
            work.prepare(lambda: held(candidate, forms, work.stop))
        )
        await until(held.entered.is_set)
        retained = held.retained_work()
        with pytest.raises(NativeIOBusy):
            await work.prepare(lambda: held(candidate, forms, work.stop))
        work.stop.set()
        held.release()
        outcome = await pending
    finally:
        held.release()
        await work.close()

    assert retained[0] > 0
    assert retained[1:] == (1, len(forms[0]))
    assert outcome == "stopped"
    assert held.calls == 1
    assert work.settled
    assert held.encoder is not None
    assert held.encoder() is None
    assert held.thread is not None
    assert not held.thread.is_alive()


class FinalizationResolver(FakeResolver):
    def resolve(
        self, execution: AgentExecution, workspace: WorkspaceRef
    ) -> HostedHarness:
        return replace(
            super().resolve(execution, workspace),
            no_capture=FinalizationImplementation(name="fake", version="fake-1"),
        )


def finalization_origin(work: Workload) -> FinalizationOrigin:
    return FinalizationOrigin(
        attempt_id=work.attempt_id,
        execution_domain_id=work.execution_domain_id,
        submitter=SUBMITTER,
        supervisor=identity_of(os.getpid()),
        request_digest=work.execution_request_digest,
    )


FINALIZATION_SPEC: Final[JsonObject] = {
    **FAKE_SPEC,
    "price_table_id": TESTING_PRICE_TABLE.id,
    "price_table": TESTING_PRICE_TABLE.model_dump(mode="json"),
}


@pytest.mark.asyncio
async def test_agent_session_finalization_measures_the_adapter_and_logs_fixed_outcome(
    tmp_path: Path,
) -> None:
    clock = SteppedClock()
    gated = GatedScript(script([CHECKOUT_SCRIPT]))
    with captured(
        "runner.agent",
        LoggingSettings(level=Level.DEBUG),
        "finalization-tests",
        lambda: FIXED_NOW,
    ) as capture:
        world = make_world(tmp_path, capture)
        resolver = FinalizationResolver(gated, context_for(capture))
        host = world.host(resolver, clock=clock)
        work = world.workload(session_spec=FINALIZATION_SPEC)
        try:
            with bound_scope(attempt_id=work.attempt_id):
                host.start(work, world.workspace(), origin=finalization_origin(work))
                await asyncio.wait_for(gated.entered.wait(), CONDITION_SECONDS)

                await clock.advance_to(17_999_999)
                gated.release.set()
                await host.finished()
                result_ref = host.result_reference()
                sibling = host.finalization_reference()
                assert result_ref is not None
                assert sibling is not None
                facts = read_finalization(
                    world.control,
                    FinalizationReferences(
                        checked_result=ControlFileRef(
                            name=result_ref.name,
                            size=result_ref.size,
                            digest=result_ref.digest,
                        ),
                        finalization=ControlFileRef(
                            name=sibling.name,
                            size=sibling.size,
                            digest=sibling.digest,
                        ),
                    ),
                )
        finally:
            gated.release.set()
            await host.stop()
            world.close()
        records = capture.finish()

    (event,) = (
        record
        for record in records
        if record.get("event") == "runner.agent.finalization"
    )
    assert facts.duration_ms == 17
    assert event["level"] == "DEBUG"
    assert event["attempt_id"] == work.attempt_id
    assert event["outcome"] == "sealed"
    assert (
        not {
            "digest",
            "capture",
            "price_table_id",
            "session_id",
            "transcript",
            "process",
        }
        & event.keys()
    )


class WaitingFinalizationServer(ControlServer):
    request_received: asyncio.Event
    peer_closed: asyncio.Event

    async def _attached(
        self, channel: BoundedChannel, submitter: Submitter, observational: bool
    ) -> None:
        cutoff = self._clock.monotonic_ns() + int(CONDITION_SECONDS * SECOND)
        frames = {FrameType.JSON.value: PRE_ATTACH_BYTES}
        _, payload = await channel.read_typed_frame(cutoff, frames)
        request = read_request(payload)
        assert request is not None
        assert request.op == "session_finalization"
        self.request_received.set()
        try:
            await channel.read_typed_frame(cutoff, frames)
        except ChannelError as error:
            if error.reason is not ChannelFailure.CLOSED:
                raise
            self.peer_closed.set()


@pytest.mark.asyncio
async def test_agent_session_finalization_reader_closes_with_local_runtime(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async with delivery_server(
        tmp_path, kora_capture, server_type=WaitingFinalizationServer
    ) as live:
        server = live.server
        assert isinstance(server, WaitingFinalizationServer)
        server.request_received = asyncio.Event()
        server.peer_closed = asyncio.Event()
        handle = live.runtime.registry.key(live.work.attempt_id).handle()
        reading = asyncio.create_task(
            live.runtime.session_finalization(handle, live.ctx)
        )
        try:
            await asyncio.wait_for(server.request_received.wait(), CONDITION_SECONDS)

            await live.runtime.close()
            read_ended_at_close = reading.done()
            await asyncio.wait_for(server.peer_closed.wait(), CONDITION_SECONDS)
        finally:
            if not reading.done():
                reading.cancel()
            with pytest.raises(asyncio.CancelledError):
                await reading

    assert read_ended_at_close


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "intervention", ["cancel", "earlier_stop_then_cancel", "abandoned_waiter", "fence"]
)
async def test_result_work_survives_protocol_cancel_and_abandoned_waiter(
    tmp_path: Path,
    kora_capture: Capture,
    intervention: Literal[
        "cancel", "earlier_stop_then_cancel", "abandoned_waiter", "fence"
    ],
) -> None:
    world = make_world(tmp_path, kora_capture)
    kora_capture.scope.admit("registered-value")
    resolver = FinalizationResolver(
        script([CHECKOUT_SCRIPT]),
        context_for(kora_capture),
        adapter_type=ObservedFakeHarness,
    )
    held = HeldPreparation()
    host = world.host(resolver, prepare_result=held)

    try:
        work = world.workload(session_spec=FINALIZATION_SPEC)
        host.start(work, world.workspace(), origin=finalization_origin(work))
        await until(held.entered.is_set)
        adapter = resolver.adapters[0]
        assert isinstance(adapter, ObservedFakeHarness)
        sink = adapter.runner_sink
        assert sink is not None
        cancel_waiter: asyncio.Task[None] | None = None
        if intervention in ("cancel", "earlier_stop_then_cancel"):
            expected_cause = "cancel"
            if intervention == "earlier_stop_then_cancel":
                expected_cause = "max_events"
                sink.terminate("max_events")
            cancel_started = asyncio.Event()

            async def cancel() -> None:
                cancel_started.set()
                await host.cancel()

            cancel_waiter = asyncio.create_task(cancel())
            await cancel_started.wait()
            assert sink.cause() == expected_cause
        elif intervention == "abandoned_waiter":
            waiter = asyncio.create_task(host.finished())
            asyncio.get_running_loop().call_soon(waiter.cancel)
            with pytest.raises(asyncio.CancelledError):
                await waiter
        in_flight = not host.result_work_settled() and not held.fenced()
        if intervention == "fence":
            host.fence()
            await until(held.fenced)
            assert not host.result_work_settled()
        cause = sink.cause()
        held.release()
        await host.finished()
        if cancel_waiter is not None:
            await cancel_waiter
        published = host.result_reference()
        sibling = host.finalization_reference()
    finally:
        held.release()
        released = await host.stop()
        world.close()

    assert in_flight
    expected_causes = {
        "cancel": "cancel",
        "fence": "cancel",
        "earlier_stop_then_cancel": "max_events",
    }
    assert cause == expected_causes.get(intervention)
    assert (published is None) == (intervention == "fence")
    assert (sibling is not None) == (intervention == "abandoned_waiter")
    assert world.kinds().count("runner.session_result") == (
        0 if intervention == "fence" else 1
    )
    assert held.calls == 1
    assert host.result_work_settled()
    assert held.encoder is not None
    assert held.encoder() is None
    assert held.thread is not None
    assert not held.thread.is_alive()
    assert released.session
    assert released.writer


class DeadlineCrossingAppend:
    def __init__(self, journal: RunningJournal, clock: SteppedClock) -> None:
        self.journal = journal
        self.clock = clock

    def append_batch(
        self, records: Sequence[tuple[str, JsonObject]], *, cap_bytes: int
    ) -> BatchAppend:
        return self.journal.append_batch(records, cap_bytes=cap_bytes)

    def append_outcome(self, kind: str, data: JsonObject) -> JournalAppendOutcome:
        outcome = self.journal.append_outcome(kind, data)
        if kind == "runner.session_result":
            self.clock.current = DEADLINE_NS
        return outcome


@pytest.mark.asyncio
async def test_late_durable_result_append_does_not_publish_reference(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    clock = SteppedClock()
    journal = DeadlineCrossingAppend(world.journal, clock)
    host = world.host(
        FinalizationResolver(script([CHECKOUT_SCRIPT]), context_for(kora_capture)),
        journal=journal,
        clock=clock,
    )
    try:
        work = world.workload(deadline=deadline(120), session_spec=FINALIZATION_SPEC)
        host.start(work, world.workspace(), origin=finalization_origin(work))

        await host.finished()
        await host.stop()
    finally:
        world.close()

    assert "runner.session_result" in world.kinds()
    assert (world.root / "control" / RESULT_DIRECTORY / RESULT_NAME).is_file()
    assert clock.monotonic_ns() == DEADLINE_NS
    assert host.result_reference() is None
    assert host.finalization_reference() is None
    assert not (world.root / "control" / RESULT_DIRECTORY / FINALIZATION_NAME).exists()
    assert host.result_work_settled()


@pytest.mark.asyncio
async def test_result_published_before_deadline_remains_readable_after_expiry(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    clock = SteppedClock()
    host = world.host(world.resolver(script([CHECKOUT_SCRIPT])), clock=clock)
    try:
        host.start(world.workload(deadline=deadline(120)), world.workspace())
        await host.finished()
        first = host.result_reference()

        await clock.advance_to(DEADLINE_NS)
        replay = host.observation()
        last = host.result_reference()
        await host.stop()
    finally:
        world.close()

    assert first is not None
    assert replay is not None
    assert first == last
    assert world.kinds().count("runner.session_result") == 1
