import asyncio
import errno
import os
import threading
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Final

import pytest
from pydantic import TypeAdapter

from kora.conformance.capture import Capture
from kora.conformance.common import context_for, kora_capture
from kora.ir.json import JsonObject, JsonValue
from kora.ir.vcs_ref_json import canonical_bytes
from kora.plugins.harness.fake.adapter import NAME, SESSION_ID, VERSION
from kora.plugins.runtime.local.agent import AgentSources
from kora.plugins.runtime.local.completed import recorded_exited
from kora.plugins.runtime.local.serving import COMPONENT, ControlServer, rebound
from kora.protocols.deadlines import ExecutionDeadline
from kora.protocols.runtime import SessionObservation
from kora.runner.agent import STOP_DEADLINE_NS
from kora.runner.filesystem import open_local_directory
from kora.runner.identity import NativeIdentityReader
from kora.runner.journal import BatchAppend, JournalAppendOutcome, RunningJournal
from kora.runner.peers import kernel_source
from kora.runner.registry import CompletedResult, ControlFileRef, Registry
from kora.runner.session_result import (
    LEGACY_RESULT_NAME,
    RESULT_DIRECTORY,
    RESULT_NAME,
    PreparedObservation,
    digest_of,
    prepare_observation,
)
from kora.testing.local import unless_busy
from kora.testing.runner import AllowDenyPolicies
from kora.testing.script import FakeScript
from tests.plugins.harness.fake.fake_support import result
from tests.plugins.runtime.local.test_agent_session import (
    agent_session,
    code_of_call,
    detached_workload,
)
from tests.plugins.runtime.local.test_command_observation import SUBMITTER
from tests.plugins.runtime.local.test_completion import completed_entry
from tests.plugins.runtime.local.testing_support import attempt, composition
from tests.runner.agent_support import (
    FakeResolver,
    SteppedClock,
    make_world,
    script,
)
from tests.runner.test_runner_outcome_covers_every_stop_deadline_escalation import (
    SECOND,
    deadline,
)
from tests.runner.test_session_result_protection import observation
from tests.support.local_peers import CONDITION_SECONDS, identity_of, until
from tests.support.local_runners import short_directory, written
from tests.support.local_runtime import protocol_error

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
    assert legacy.code == unowned.code == "session_unobserved"
    assert files == []
    assert retained_quiescence == quiescence
    assert not control.exists()


class HeldPreparation:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.gate = threading.Event()
        self.thread: threading.Thread | None = None
        self.stop: threading.Event | None = None
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
        self.entered.set()
        if not self.gate.wait(CONDITION_SECONDS):
            raise TimeoutError("result preparation did not receive its test release")
        return prepare_observation(result, forms, stop)

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
@pytest.mark.parametrize("expires", [False, True])
async def test_session_result_scan_settles_on_cancel_and_shutdown(
    tmp_path: Path, kora_capture: Capture, expires: bool
) -> None:
    clock = SteppedClock()
    binding = TypeAdapter(ExecutionDeadline).dump_python(deadline(120), mode="json")
    work = detached_workload({"deadline": binding} if expires else {})
    resolver = FakeResolver(script([CHECKOUT_SCRIPT]), context_for(kora_capture))
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
                tmp_path, kora_capture, sources, clock, prepare_result=held
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
                await until(held.entered.is_set)
                control = attempt_path / "control"
                original_control = descriptors_for(control)
                assert original_control
                while_held = (registry.count(), registry.retained_bytes())
                if expires:
                    await clock.requested(DEADLINE_NS, count=2)
                    await clock.advance_to(DEADLINE_NS)
                    await until(held.fenced)
                closing = asyncio.create_task(server.close())
                try:
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
                    held.gate.set()
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
        finally:
            bound.close()
            registry_owner.directory.close()


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
        world.resolver(script([CHECKOUT_SCRIPT])), journal=journal, clock=clock
    )
    try:
        host.start(world.workload(deadline=deadline(120)), world.workspace())

        await host.finished()
        await host.stop()
    finally:
        world.close()

    assert "runner.session_result" in world.kinds()
    assert (world.root / "control" / RESULT_DIRECTORY / RESULT_NAME).is_file()
    assert clock.monotonic_ns() == DEADLINE_NS
    assert host.result_reference() is None
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
