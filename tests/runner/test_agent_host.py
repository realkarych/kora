import asyncio
import os
from pathlib import Path
from typing import Final

import pytest

from kora.conformance.capture import Capture
from kora.ir import ErrorClass
from kora.ir.json import JsonObject
from kora.ir.vcs_ref_json import canonical_bytes
from kora.protocols.errors import KoraProtocolError
from kora.protocols.harness import LaunchInputs
from kora.protocols.runtime import (
    SessionFailedObservation,
    SessionObservation,
    SessionResultObservation,
)
from kora.runner.agent import (
    INTERRUPT_GRACE_NS,
    KILL_SETTLE_NS,
    STOP_DEADLINE_NS,
    AgentHost,
    RefusingLaunches,
    Released,
)
from kora.runner.journal import SYNC_DEADLINE_NS
from kora.runner.session_result import (
    RESULT_DIRECTORY,
    RESULT_NAME,
    digest_of,
    read_observation,
)
from tests.plugins.harness.fake.fake_support import result, tool_call, turn
from tests.runner.agent_support import (
    FAKE_SPEC,
    PAUSE,
    JournalProxy,
    RaisingPolicy,
    SignalledScript,
    World,
    execution,
    hosted,
    journal_threads_exited,
    make_world,
    run,
    script,
)

DENIED_COMMAND: Final[str] = "rm -rf build"
SETTLED: Final[Released] = Released(session=True, writer=True)


def shell(tool_use_id: str, command: str) -> JsonObject:
    return tool_call("Bash", tool_use_id, arguments={"command": command})


def observed(world: World, host: AgentHost) -> SessionObservation | None:
    reference = host.result_reference()
    if reference is None:
        return None
    return read_observation(world.control, reference)


def result_file(world: World) -> Path:
    return world.root / "control" / RESULT_DIRECTORY / RESULT_NAME


def terminations(capture: Capture) -> list[object]:
    return [
        record.get("termination")
        for record in capture.finish()
        if record.get("event") == "harness.fake.session_finished"
    ]


async def stopped(
    world: World,
    source: SignalledScript,
    *,
    cancel: bool,
    releases: tuple[int, ...] = (),
    deaf: bool = False,
    journal: JournalProxy | None = None,
    **changes: object,
) -> tuple[AgentHost, Released]:
    host = world.host(world.resolver(source, deaf), journal=journal)
    host.start(world.workload(**changes), world.workspace())
    await source.paused.wait()
    abandoned = asyncio.ensure_future(host.finished())
    asyncio.get_running_loop().call_soon(abandoned.cancel)
    await asyncio.wait({abandoned})
    cancelled = asyncio.ensure_future(host.cancel() if cancel else host.finished())
    for deadline in releases:
        await world.clock.requested(deadline)
        await world.clock.release(deadline)
    await cancelled
    await host.finished()
    return host, await host.stop()


def test_agent_host_returns_session_result_completed(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    source = script([turn("m1", "hello"), shell("call-1", "ls"), result()])

    host, released = run(hosted(world, source))
    observation = observed(world, host)
    world.close()

    reference = host.result_reference()
    records = world.records()
    assert isinstance(observation, SessionResultObservation)
    assert reference is not None
    assert observation.result.stop_reason == "completed"
    assert observation.cause is None
    assert observation.journal_complete
    assert [record["kind"] for record in records] == [
        "runner.session_spec",
        "turn",
        "tool_call",
        "tool_result",
        "result",
        "runner.session_result",
    ]
    assert records[0]["data"] == {"digest": digest_of(canonical_bytes(FAKE_SPEC))}
    assert records[-1]["data"] == {"digest": reference.digest, "size": reference.size}
    assert reference.digest == digest_of(result_file(world).read_bytes())
    assert os.stat(result_file(world)).st_mode & 0o777 == 0o400
    assert released == SETTLED
    assert journal_threads_exited()


def test_agent_host_returns_session_result_max_events(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    source = script([turn("m1"), turn("m2"), turn("m3"), result()])

    host, _ = run(hosted(world, source, limits={"max_events": 2}))
    observation = observed(world, host)
    world.close()

    assert isinstance(observation, SessionResultObservation)
    assert observation.result.stop_reason == "max_events"
    assert observation.result.error_class is ErrorClass.EVENTS_EXHAUSTED
    assert observation.cause == "max_events"
    assert world.kinds().count("turn") == 2


def test_agent_host_returns_session_result_cancel_acknowledged(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    source = script([turn("m1"), PAUSE, result()])

    host, released = run(stopped(world, source, cancel=True))
    observation = observed(world, host)
    world.close()

    assert isinstance(observation, SessionResultObservation)
    assert observation.result.stop_reason == "cancel"
    assert observation.cause == "cancel"
    assert terminations(kora_capture) == ["interrupted"]
    assert released == SETTLED
    assert journal_threads_exited()


def test_agent_host_returns_session_result_cancel_ignored(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    source = script([turn("m1"), PAUSE, result()], on_interrupt="ignore")

    host, _ = run(stopped(world, source, cancel=True, releases=(INTERRUPT_GRACE_NS,)))
    observation = observed(world, host)
    world.close()

    assert isinstance(observation, SessionResultObservation)
    assert observation.result.stop_reason == "cancel"
    assert observation.result.error_class is ErrorClass.CANCELLED
    assert terminations(kora_capture) == ["killed_after_grace"]
    assert world.clock.deadlines.count(INTERRUPT_GRACE_NS) == 1


@pytest.mark.parametrize("cancel", [True, False])
def test_agent_host_returns_session_result_max_events_ignored(
    tmp_path: Path, kora_capture: Capture, cancel: bool
) -> None:
    world = make_world(tmp_path, kora_capture)
    source = script([turn("m1"), PAUSE, result()], on_interrupt="ignore")

    host, _ = run(
        stopped(
            world,
            source,
            cancel=cancel,
            releases=(INTERRUPT_GRACE_NS,),
            limits={"max_events": 1},
        )
    )
    observation = observed(world, host)
    world.close()

    assert isinstance(observation, SessionResultObservation)
    assert observation.cause == "max_events"
    assert observation.result.stop_reason == "max_events"
    assert terminations(kora_capture) == ["killed_after_grace"]


def test_agent_host_returns_session_result_writer_failure_ignored(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    source = script([turn("m1"), PAUSE, result()], on_interrupt="ignore")
    journal = JournalProxy(world.journal, "turn", "fail")

    host, _ = run(
        stopped(
            world, source, cancel=False, releases=(INTERRUPT_GRACE_NS,), journal=journal
        )
    )
    observation = observed(world, host)
    world.close()

    assert isinstance(observation, SessionResultObservation)
    assert observation.cause == "journal_unavailable"
    assert observation.result.stop_reason == "journal_unavailable"
    assert not observation.journal_complete
    assert terminations(kora_capture) == ["killed_after_grace"]


def test_agent_host_returns_session_result_kill_ignored_on_cancel(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    source = script([turn("m1"), PAUSE, result()], on_interrupt="ignore")

    host, released = run(
        stopped(
            world,
            source,
            cancel=True,
            releases=(INTERRUPT_GRACE_NS, KILL_SETTLE_NS),
            deaf=True,
        )
    )
    observation = observed(world, host)
    world.close()

    assert observation == SessionFailedObservation(
        kind="session_failed", code="kill_unconfirmed"
    )
    assert world.kinds()[-1] == "runner.session_failed"
    assert released == SETTLED


def test_agent_host_returns_session_result_kill_ignored_on_stop(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    source = script([turn("m1"), PAUSE, result()], on_interrupt="ignore")

    async def closed() -> tuple[AgentHost, Released]:
        host = world.host(world.resolver(source, deaf=True))
        host.start(world.workload(), world.workspace())
        await source.paused.wait()
        stopping = asyncio.ensure_future(host.stop())
        await world.clock.requested(KILL_SETTLE_NS)
        await world.clock.release(KILL_SETTLE_NS)
        return host, await stopping

    host, released = run(closed())
    world.close()

    assert host.result_reference() is None
    assert released == SETTLED
    assert INTERRUPT_GRACE_NS not in world.clock.deadlines
    assert journal_threads_exited()


@pytest.mark.parametrize("closing", ["fence", "stop"])
def test_agent_host_returns_session_result_immediate_kill(
    tmp_path: Path, kora_capture: Capture, closing: str
) -> None:
    world = make_world(tmp_path, kora_capture)
    source = script([turn("m1"), PAUSE, result()], on_interrupt="ignore")

    async def closed() -> tuple[AgentHost, Released]:
        host = world.host(world.resolver(source))
        host.start(world.workload(), world.workspace())
        await source.paused.wait()
        if closing == "fence":
            host.fence()
            world.journal.close()
        return host, await host.stop()

    host, released = run(closed())
    world.close()

    assert host.result_reference() is None
    assert not result_file(world).exists()
    assert INTERRUPT_GRACE_NS not in world.clock.deadlines
    assert terminations(kora_capture) == ["killed_after_grace"]
    assert released == SETTLED
    assert journal_threads_exited()


def test_agent_host_returns_session_result_stop_unsettled(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    journal = JournalProxy(world.journal, "turn", "hold")
    source = script([turn("m1"), PAUSE, result()])

    async def held() -> Released:
        host = world.host(world.resolver(source), journal=journal)
        host.start(world.workload(), world.workspace())
        await source.paused.wait()
        stopping = asyncio.ensure_future(host.stop())
        await world.clock.requested(STOP_DEADLINE_NS)
        await world.clock.release(STOP_DEADLINE_NS)
        await world.clock.requested(SYNC_DEADLINE_NS, 2)
        await world.clock.release(SYNC_DEADLINE_NS)
        return await stopping

    released = run(held())
    journal.gate.set()
    exited = journal_threads_exited()
    world.close()

    unsettled = [
        record
        for record in kora_capture.finish()
        if record.get("event") == "runner.agent.stop_unsettled"
    ]
    assert released == Released(session=False, writer=False)
    assert [(record["session"], record["writer"]) for record in unsettled] == [
        (False, False)
    ]
    assert exited


def test_agent_host_returns_session_result_fence_before_seal(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    proxy = JournalProxy(world.journal, "runner.session_result", "fence")
    host = world.host(world.resolver(script([turn("m1"), result()])), journal=proxy)
    proxy.host = host

    async def sealed() -> None:
        host.start(world.workload(), world.workspace())
        await host.finished()
        await host.stop()

    run(sealed())
    world.close()

    assert host.result_reference() is None
    assert not result_file(world).exists()
    assert "runner.session_result" not in world.kinds()


def test_agent_host_returns_session_result_unwritten(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    (tmp_path / "control" / RESULT_DIRECTORY).mkdir(mode=0o700)
    result_file(world).write_bytes(b"stale")

    host, _ = run(hosted(world, script([turn("m1"), result()])))
    world.close()

    failures = [
        record.get("code")
        for record in kora_capture.finish()
        if record.get("event") == "runner.agent.session_failed"
    ]
    assert host.result_reference() is None
    assert failures == ["result_unwritten"]
    assert "runner.session_result" not in world.kinds()
    assert result_file(world).read_bytes() == b"stale"


UNTRUSTED: Final[tuple[tuple[str, str], ...]] = (
    ("session_spec", "session_spec_missing"),
    ("execution_field", "agent_execution_invalid"),
    ("limits_key", "agent_execution_invalid"),
    ("command_kind", "workload_kind_unavailable"),
    ("outside_root", "workspace_untrusted"),
    ("symlinked_alias", "workspace_untrusted"),
    ("attempt_mismatch", "workspace_untrusted"),
    ("spec_unrecorded", "journal_unavailable"),
)


@pytest.mark.parametrize(("case", "code"), UNTRUSTED)
def test_agent_host_refuses_untrusted_workload(
    tmp_path: Path, kora_capture: Capture, case: str, code: str
) -> None:
    world = make_world(tmp_path, kora_capture)
    resolver = world.resolver(script([turn("m1"), result()]))
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path / "candidate")
    incomplete = execution()
    del incomplete["output_format"]
    workloads = {
        "session_spec": world.workload(session_spec=None),
        "execution_field": world.workload(execution_request=incomplete),
        "limits_key": world.workload(limits={"max_events": 2, "stall_after": 5}),
        "command_kind": world.workload(kind="command"),
    }
    workspaces = {
        "outside_root": world.workspace(str(tmp_path / "elsewhere")),
        "symlinked_alias": world.workspace(str(alias)),
        "attempt_mismatch": world.workspace().model_copy(
            update={"attempt_id": "attempt-2"}
        ),
    }
    journal = JournalProxy(world.journal, "runner.session_spec", "close")
    workload = workloads.get(case, world.workload())
    workspace = workspaces.get(case, world.workspace())

    async def started() -> KoraProtocolError:
        host = world.host(
            resolver, journal=journal if case == "spec_unrecorded" else None
        )
        with pytest.raises(KoraProtocolError) as raised:
            host.start(workload, workspace)
        return raised.value

    refusal = run(started())
    world.close()

    assert refusal.code == code
    assert refusal.retryable == (code == "journal_unavailable")
    assert refusal.__cause__ is None
    assert resolver.adapters == []
    assert world.records() == []


def test_agent_host_adapter_raises(tmp_path: Path, kora_capture: Capture) -> None:
    world = make_world(tmp_path, kora_capture)
    source = script([turn("m1")], end="completed")

    host, _ = run(hosted(world, source))
    observation = observed(world, host)
    world.close()

    records = world.records()
    assert observation == SessionFailedObservation(
        kind="session_failed", code="script_end_invalid"
    )
    assert records[-1]["kind"] == "runner.session_failed"
    assert isinstance(records[-1]["data"], dict)
    assert records[-1]["data"]["code"] == "script_end_invalid"
    assert "runner.session_result" not in world.kinds()


def test_policy_failure_denies(tmp_path: Path, kora_capture: Capture) -> None:
    world = make_world(tmp_path, kora_capture)
    source = script([turn("m1"), shell("call-1", "ls"), result()])

    host, _ = run(hosted(world, source, policy=RaisingPolicy()))
    observation = observed(world, host)
    world.close()

    denials = [
        record for record in world.records() if record["kind"] == "permission_denied"
    ]
    assert isinstance(observation, SessionResultObservation)
    assert observation.result.stop_reason == "policy_error"
    assert observation.result.error_class is ErrorClass.CONFIG_ERROR
    assert [record["data"] for record in denials] == [
        {
            "tool": "Bash",
            "tool_use_id": "call-1",
            "source": "harness",
            "pattern": None,
            "code": "policy_error",
        }
    ]
    assert "tool_result" not in world.kinds()


def test_fake_tool_calls_go_through_policy_runner_side(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    documents = [turn("m1"), shell("call-1", "ls"), shell("call-2", DENIED_COMMAND)]
    source = script([*documents, result()])

    host, _ = run(
        hosted(world, source, execution_request=execution(deny=("Bash(rm *)",)))
    )
    observation = observed(world, host)
    world.close()

    records = world.records()
    results = [record["data"] for record in records if record["kind"] == "tool_result"]
    denials = [
        record["data"] for record in records if record["kind"] == "permission_denied"
    ]
    assert isinstance(observation, SessionResultObservation)
    assert [
        result["tool_use_id"] for result in results if isinstance(result, dict)
    ] == ["call-1"]
    assert denials == [
        {
            "tool": "Bash",
            "tool_use_id": "call-2",
            "source": "policy",
            "pattern": "node.deny.0",
            "code": "denied",
        }
    ]
    assert observation.result.denials.total == 1
    assert observation.result.error_class is None


def test_refusing_launches_reject_every_allocation(tmp_path: Path) -> None:
    launches = RefusingLaunches()

    with pytest.raises(KoraProtocolError) as raised:
        run(launches.allocate(LaunchInputs(environment={}, cwd=tmp_path)))

    assert raised.value.code == "launch_rejected"
