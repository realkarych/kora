import asyncio
import hashlib
import json
import os
import subprocess
import sys
import threading
from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

import pytest
from pydantic import TypeAdapter

from kora.conformance.capture import Capture
from kora.conformance.common import context_for, kora_capture
from kora.executor.digests import object_digest
from kora.ir.json import JsonObject
from kora.plugins.runtime.local.agent import (
    SCRIPT_LIMIT,
    AgentSession,
    AgentSources,
    ProductionResolver,
    RefusingPolicies,
    agent_admission,
)
from kora.plugins.runtime.local.command import COMPONENT
from kora.plugins.runtime.local.completed import recorded_exited
from kora.plugins.runtime.local.installation import InstallationConfiguration
from kora.plugins.runtime.local.launch import ExpectedBinary
from kora.plugins.runtime.local.reaper import AdoptedReaper
from kora.plugins.runtime.local.runtime import LocalRuntime
from kora.plugins.runtime.local.workload_session import Completion
from kora.plugins.runtime.local.workspace import Admitted
from kora.protocols.common import ProtocolContext
from kora.protocols.deadlines import ExecutionDeadline
from kora.protocols.errors import KoraProtocolError
from kora.protocols.harness import AgentExecution, LaunchInputs
from kora.protocols.identity import Handle
from kora.protocols.runtime import (
    SessionObservation,
    SessionResultObservation,
    Workload,
)
from kora.protocols.secret_value import SecretRevealRefused, SecretScope
from kora.protocols.time import Clock, SystemClock
from kora.protocols.vcs import WorkspaceRef
from kora.runner.agent import INTERRUPT_GRACE_NS, STOP_DEADLINE_NS, ResultPreparation
from kora.runner.channels import ChannelError, ChannelFailure
from kora.runner.filesystem import open_local_directory
from kora.runner.identity import NativeIdentityReader
from kora.runner.registry import Completed, ControlFileRef
from kora.runner.session_result import (
    RESULT_DIRECTORY,
    RESULT_NAME,
    prepare_observation,
)
from kora.testing.local import script_chunks, script_item
from kora.testing.local import testing_runner_command as runner_command
from kora.testing.runner import AllowDenyPolicies
from kora.testing.runner import testing_sources as compose_sources
from kora.testing.script import FakeScript
from tests.plugins.harness.fake.fake_support import turn
from tests.plugins.runtime.local.test_command import attempt_directory, journal_events
from tests.plugins.runtime.local.test_command_observation import (
    SUBMITTER,
    unless_busy,
)
from tests.plugins.runtime.local.test_command_workspace import (
    FAKE_COMMIT,
    OTHER_COMMIT,
    kinds,
    request,
    step_plan,
)
from tests.plugins.runtime.local.test_completion import completed_entry
from tests.plugins.runtime.local.test_release import with_runner
from tests.plugins.runtime.local.testing_support import completed_script
from tests.protocols.runtime_documents import publish_plan, resource_ref, workload
from tests.runner.agent_support import (
    FAKE_SPEC,
    PAUSE,
    FakeResolver,
    GatedClock,
    execution,
    journal_threads_exited,
    script,
)
from tests.support.local_delivery import ReapedSession
from tests.support.local_peers import CONDITION_SECONDS, until
from tests.support.local_runners import (
    open_spawning,
    reap_runners,
    short_directory,
    submitted,
)
from tests.support.local_runtime import (
    private_directory,
    protocol_error,
    runtime_context,
    state_root,
)

__all__ = ["kora_capture"]

AGENT_CONFIG: Final[JsonObject] = {
    "harness": "claude",
    "prompt": "fix the build",
    "workspace": {"repo": "local"},
}
COMMAND_CONFIG: Final[JsonObject] = {
    "cmd": [sys.executable, "-I", "-c", "pass"],
    "workspace": {"repo": "local"},
}
DEADLINE: Final[ExecutionDeadline] = ExecutionDeadline(
    at=datetime(2030, 1, 1, tzinfo=UTC),
    scope_kind="attempt",
    scope_id="attempt-1",
    clock_uncertainty=timedelta(0),
    grace=timedelta(seconds=30),
)
ARTIFACT_INPUTS: Final[JsonObject] = {
    "plan": {
        "artifact_version_id": "version-1",
        "payload_digest": "sha256:" + "0" * 64,
        "type_name": "Plan",
    }
}


@dataclass(frozen=True)
class Refusal:
    agent: dict[str, object]
    command: dict[str, object] | None
    code: str


def agent_only(changes: dict[str, object]) -> Refusal:
    return Refusal(changes, None, "workload_kind_unavailable")


def config(field: str, value: object) -> Refusal:
    return Refusal(
        {"resolved_config": {**AGENT_CONFIG, field: value}},
        {"resolved_config": {**COMMAND_CONFIG, field: value}},
        "workload_kind_unavailable",
    )


def same(
    changes: dict[str, object], code: str = "workload_kind_unavailable"
) -> Refusal:
    return Refusal(changes, changes, code)


PROTECTED: Final[JsonObject] = request(
    "/repository", FAKE_COMMIT, protected=("secrets/**",)
)
REFUSALS: Final[dict[str, Refusal]] = {
    "missing_stall": agent_only({"limits": {}}),
    "noncanonical_stall": agent_only({"limits": {"stall_after_us": "01"}}),
    "negative_stall": agent_only({"limits": {"stall_after_us": "-1"}}),
    "fractional_stall": agent_only({"limits": {"stall_after_us": "1.5"}}),
    "env": same({"env": {"LANG": "C.UTF-8"}}),
    "secret_refs": same({"secret_refs": {"API_TOKEN": "secret://env/API_TOKEN"}}),
    "harness_credentials": same(
        {"harness_credentials": {"ANTHROPIC_API_KEY": "secret://env/ANTHROPIC"}}
    ),
    "resources": same({"resources": [resource_ref()]}),
    "artifact_inputs": same({"artifact_inputs": ARTIFACT_INPUTS}),
    "publish": same({"publish": publish_plan()}),
    "protected_paths": same(
        {"checkout": step_plan("pass"), "checkout_request": PROTECTED},
        "protected_paths_unsupported",
    ),
    "limits": Refusal(
        {"limits": {"max_events": 10, "stall_after_us": "900000000", "max_turns": 3}},
        {"limits": {"max_turns": 3}},
        "workload_kind_unavailable",
    ),
    "plan_executor": same({"plan_executor": "worker_host"}),
    "config_field": config("unknown", True),
    "config_extension": config("x-extension", True),
    "config_env": config("env", {"LANG": "C.UTF-8"}),
    "config_secret_env": config("secret_env", {"TOKEN": "TOKEN"}),
    "config_network_policy": config("network_policy", {"egress": []}),
    "config_runtime": config("runtime", "local"),
    "config_vcs": config("vcs", "git"),
    "config_sandbox": config("sandbox", "read_only"),
    "config_permission_mode": config("permission_mode", "plan"),
    "config_trust_repo_config": config("trust_repo_config", False),
    "config_output_schema": config("output_schema", {"type": "object"}),
    "config_hooks": config("hooks", {}),
    "config_mcp_servers": config("mcp_servers", {}),
    "config_skills": config("skills", ["review"]),
    "config_subagents": config("subagents", []),
    "config_tool_policy": config("tool_policy", {"allow": [], "deny": []}),
    "workspace_ref_checkout": same(
        {
            "checkout": {
                "attempt_id": "attempt-1",
                "root": "/workspace",
                "repo": "/repository",
                "commit": FAKE_COMMIT,
                "base_commit": FAKE_COMMIT,
                "branch": None,
            },
            "checkout_request": request("/repository", FAKE_COMMIT),
        }
    ),
    "no_checkout_request": agent_only({"checkout_request": None}),
    "null_session_spec": agent_only({"session_spec": None}),
    "execution_request": agent_only({"execution_request": {"node": "fix"}}),
}


def agent_fields(harness: str = "claude") -> dict[str, object]:
    document = execution(harness=harness)
    return {
        "kind": "agent",
        "plan_executor": "runner",
        "publish": None,
        "env": {},
        "secret_refs": {},
        "harness_credentials": {},
        "artifact_inputs": {},
        "resources": [],
        "limits": {"stall_after_us": "900000000"},
        "deadline": None,
        "resolved_config": AGENT_CONFIG,
        "execution_request": document,
        "execution_request_digest": object_digest(document),
        "session_spec": FAKE_SPEC,
        "checkout": step_plan(f"print({FAKE_COMMIT!r})"),
        "checkout_request": request(
            "/repository", FAKE_COMMIT, (FAKE_COMMIT, OTHER_COMMIT)
        ),
    }


def agent_workload(runtime: LocalRuntime, harness: str = "claude") -> Workload:
    return submitted(runtime, "attempt-1", **agent_fields(harness))


def detached_workload(changes: dict[str, object]) -> Workload:
    base = Workload.model_validate_json(json.dumps(workload(**agent_fields())))
    return changed(base, changes)


def changed(base: Workload, changes: dict[str, object]) -> Workload:
    document = {**base.model_dump(mode="json"), **changes}
    return Workload.model_validate_json(json.dumps(document))


def command_document(changes: dict[str, object]) -> Workload:
    fields: dict[str, object] = {
        **agent_fields(),
        "kind": "command",
        "limits": {},
        "session_spec": None,
        "resolved_config": COMMAND_CONFIG,
        "checkout": workload()["checkout"],
        "checkout_request": None,
    }
    base = Workload.model_validate_json(json.dumps(workload(**fields)))
    return changed(base, changes)


@asynccontextmanager
async def agent_runtime(
    tmp_path: Path, capture: Capture, runner: Sequence[str] | None
) -> AsyncGenerator[tuple[LocalRuntime, ProtocolContext, Path]]:
    root = state_root(tmp_path)
    ctx = runtime_context(capture)
    with short_directory() as base:
        runtime = await open_spawning(root, ctx, base / "sockets", runner=runner)
        try:
            yield runtime, ctx, root
        finally:
            await runtime.close()
            reap_runners(root)


@asynccontextmanager
async def agent_session(
    tmp_path: Path,
    capture: Capture,
    sources: AgentSources,
    clock: Clock,
    *,
    bind_socket: bool = True,
    prepare_result: ResultPreparation = prepare_observation,
) -> AsyncGenerator[tuple[AgentSession, Path]]:
    attempt = private_directory(tmp_path.resolve(), "attempt")
    directory = open_local_directory(attempt)
    reaper = AdoptedReaper()
    session = AgentSession(
        directory,
        str(attempt / "candidate"),
        capture.diagnostics,
        clock,
        reaper,
        sources,
        NativeIdentityReader(clock=clock),
        prepare_result=prepare_result,
    )
    if bind_socket:
        session.bind_launch_socket(attempt / "unused-socket")
    try:
        yield session, attempt
    finally:
        session.fence()
        session.close_journal()
        await session.stop()
        await reaper.stop()
        directory.close()


async def started(runtime: LocalRuntime, ctx: ProtocolContext) -> Handle:
    handle = await runtime.submit(agent_workload(runtime), ctx)
    source = FakeScript.model_validate(
        {
            "end": "completed",
            "on_interrupt": "acknowledge",
            "steps": [turn("m1", "hello"), {"kind": "result", "status": "success"}],
        }
    )
    buffer = source.serialise()
    await runtime.transfer_in(handle, script_item(buffer), script_chunks(buffer), ctx)
    await unless_busy(lambda: runtime.start(handle, ctx))
    return handle


async def sealed(
    runtime: LocalRuntime, handle: Handle, ctx: ProtocolContext
) -> SessionObservation:
    return await unless_busy(lambda: runtime.session_observation(handle, ctx))


def result_file(root: Path, runtime: LocalRuntime) -> Path:
    attempt = attempt_directory(root, runtime, "attempt-1")
    return attempt / "control" / RESULT_DIRECTORY / RESULT_NAME


def tampered(path: Path) -> None:
    altered = bytearray(path.read_bytes())
    altered[len(altered) // 2] ^= 1
    path.chmod(0o600)
    path.write_bytes(bytes(altered))
    path.chmod(0o400)


def journal(root: Path, runtime: LocalRuntime) -> list[str]:
    return kinds(attempt_directory(root, runtime, "attempt-1") / "journal.jsonl")


def writer_threads_exited() -> bool:
    for thread in threading.enumerate():
        if thread.name == "kora-journal":
            thread.join(CONDITION_SECONDS)
    return all(thread.name != "kora-journal" for thread in threading.enumerate())


def agent_events(capture: Capture) -> list[dict[str, object]]:
    return [
        {key: record[key] for key in sorted(record) if key in {"outcome", "level"}}
        for record in capture.finish()
        if record.get("event") == "runtime.local.agent"
    ]


def refusal_of(call: Callable[[], object]) -> tuple[str, bool]:
    try:
        call()
    except KoraProtocolError as error:
        return error.code, error.retryable
    pytest.fail("the call did not refuse", pytrace=False)


def code_of_call(call: Callable[[], object]) -> str:
    try:
        call()
    except KoraProtocolError as error:
        return error.code
    pytest.fail("the call did not refuse", pytrace=False)


async def refused_start(session: AgentSession | ReapedSession, work: Workload) -> str:
    session.accept(SUBMITTER, None, work)
    try:
        await asyncio.to_thread(session.supervise, SUBMITTER, False, work, lambda: None)
    except KoraProtocolError as error:
        return error.code
    pytest.fail("the workload was admitted", pytrace=False)


@pytest.mark.asyncio
async def test_local_runner_hosts_agent_workload(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async with agent_runtime(tmp_path, kora_capture, runner_command()) as (
        runtime,
        ctx,
        root,
    ):
        handle = await started(runtime, ctx)

        observation = await sealed(runtime, handle, ctx)
        status = await runtime.status(handle, ctx)
        command_only = await protocol_error(runtime.command_observation(handle, ctx))
        recorded = journal(root, runtime)

    assert isinstance(observation, SessionResultObservation)
    assert observation.result.stop_reason == "completed"
    assert observation.cause is None
    assert observation.journal_complete
    assert (status.state, status.phase, status.exit_code) == ("exited", "complete", 0)
    assert command_only.code == "workload_kind_unavailable"
    assert recorded.count("transfer.materialised") == 1
    assert [kind for kind in recorded if kind != "transfer.materialised"] == [
        "runner.workspace_prepared",
        "runner.session_spec",
        "turn",
        "result",
        "runner.session_result",
    ]


@pytest.mark.asyncio
async def test_agent_attempt_completes_and_releases(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async with agent_runtime(tmp_path, kora_capture, runner_command()) as (
        runtime,
        ctx,
        root,
    ):
        handle = await started(runtime, ctx)
        live = await sealed(runtime, handle, ctx)
        running = await protocol_error(runtime.release(handle, ctx))

        record = await unless_busy(lambda: runtime.quiescence(handle, ctx))
        entry = completed_entry(runtime)
        await until(lambda: recorded_exited(NativeIdentityReader(), entry.runner))
        after_exit = await runtime.session_observation(handle, ctx)
        status = await runtime.status(handle, ctx)
        later_start = await protocol_error(runtime.start(handle, ctx))
        path = result_file(root, runtime)
        original = path.read_bytes()
        tampered(path)
        corrupted = await protocol_error(runtime.session_observation(handle, ctx))
        recorded = journal(root, runtime)
        attempt = attempt_directory(root, runtime, "attempt-1")
        before = frozenset(os.listdir(attempt))
        holder = subprocess.Popen(
            (sys.executable, "-I", "-c", "import time; time.sleep(60)")
        )
        try:
            with_runner(runtime, holder.pid)
            alive = await protocol_error(runtime.release(handle, ctx))
            kept = path.exists()
        finally:
            holder.kill()
            holder.wait()

        await runtime.release(handle, ctx)
        released = runtime.registry.entry(runtime.registry.key("attempt-1"))
        await runtime.release(handle, ctx)
        repeated = runtime.registry.entry(runtime.registry.key("attempt-1"))
        after_release = await protocol_error(runtime.session_observation(handle, ctx))
        released_status = await runtime.status(handle, ctx)
        name = runtime.registry.key("attempt-1").name
        registry = frozenset(os.listdir(root / "registry"))

    digest = "sha256:" + hashlib.sha256(original).hexdigest()
    assert entry.quiescence == record
    assert entry.exit_code == 0
    assert entry.result is not None
    assert entry.result.kind == "agent"
    assert entry.result.ref == ControlFileRef(
        name=RESULT_NAME, size=len(original), digest=digest
    )
    assert after_exit == live
    assert (status.state, status.phase, status.exit_code) == ("exited", "complete", 0)
    assert later_start.code == "attempt_completed"
    assert corrupted.code == "integrity_error"
    assert recorded[-2:] == ["runner.session_result", "runner.quiescence"]
    assert (running.code, running.retryable) == ("attempt_running", True)
    assert (alive.code, alive.retryable) == ("runner_unreachable", True)
    assert kept
    assert before >= {"journal.jsonl", "candidate", "control"}
    assert not attempt.exists()
    assert isinstance(released, Completed)
    assert released.released_at is not None
    assert released.result == entry.result
    assert repeated == released
    assert after_release.code == "attempt_retired"
    assert (released_status.state, released_status.exit_code) == ("exited", 0)
    assert {f"{name}.json", f"{name}.lock"} <= registry


@pytest.mark.asyncio
async def test_session_result_is_runner_owned(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async with agent_runtime(tmp_path, kora_capture, runner_command()) as (
        runtime,
        ctx,
        root,
    ):
        handle = await started(runtime, ctx)
        observation = await sealed(runtime, handle, ctx)
        attempt = attempt_directory(root, runtime, "attempt-1")
        path = result_file(root, runtime)
        planted = attempt / "candidate" / RESULT_NAME
        planted.write_bytes(path.read_bytes())
        mode = path.stat().st_mode & 0o777
        events = [
            json.loads(line)
            for line in (attempt / "journal.jsonl").read_text().splitlines()
        ]
        recorded = events[-1]["data"]
        digest = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        unplanted = await runtime.session_observation(handle, ctx)
        tampered(path)
        corrupted = await protocol_error(runtime.session_observation(handle, ctx))

    assert attempt / "candidate" not in path.parents
    assert mode == 0o400
    assert events[-1]["kind"] == "runner.session_result"
    assert recorded == {"digest": digest, "size": len(path.read_bytes())}
    assert unplanted == observation
    assert corrupted.code == "integrity_error"


@pytest.mark.parametrize("with_deadline", [False, True])
def test_agent_admission_accepts_the_supported_shape(with_deadline: bool) -> None:
    work = detached_workload(
        {"deadline": TypeAdapter(ExecutionDeadline).dump_python(DEADLINE, mode="json")}
        if with_deadline
        else {}
    )

    admitted = agent_admission(work)

    assert isinstance(admitted, Admitted)


@pytest.mark.parametrize("field", sorted(REFUSALS))
@pytest.mark.asyncio
async def test_agent_admission_refuses_unsupported_fields(
    field: str, tmp_path: Path, kora_capture: Capture
) -> None:
    case = REFUSALS[field]
    sources = AgentSources(ProductionResolver(), RefusingPolicies())
    attempt = private_directory(tmp_path.resolve(), "command")
    directory = open_local_directory(attempt)
    command = ReapedSession(
        directory,
        str(attempt / "candidate"),
        SecretScope(kora_capture.diagnostics),
        SystemClock(),
        kora_capture.diagnostics.logger(COMPONENT),
    )
    try:
        command_code = (
            case.code
            if case.command is None
            else await refused_start(command, command_document(case.command))
        )
    finally:
        await command.stop()
        directory.close()

    async with agent_session(tmp_path, kora_capture, sources, SystemClock()) as (
        session,
        agent_attempt,
    ):
        agent_code = await refused_start(session, detached_workload(case.agent))
        repeated = await refused_start(session, detached_workload(case.agent))
        status = session.status()
        unobserved = refusal_of(lambda: session.session_observation(SUBMITTER))
    events = agent_events(kora_capture)

    assert agent_code == case.code
    assert repeated == case.code
    assert command_code == case.code
    assert status == ("starting", "preparation", None)
    assert unobserved == ("session_unobserved", False)
    assert not (agent_attempt / "journal.jsonl").exists()
    assert events == [{"level": "WARNING", "outcome": "refused"}] * 2


def test_attempt_token_refuses_fifo_without_waiting(tmp_path: Path) -> None:
    attempt = private_directory(tmp_path.resolve(), "attempt")
    token = attempt / "attempt.token"
    os.mkfifo(token, 0o600)
    program = (
        "import sys\n"
        "from pathlib import Path\n"
        "from kora.plugins.runtime.local.command import attempt_token\n"
        "from kora.protocols.secret_value import SecretScope\n"
        "from kora.runner.filesystem import DirectoryAnchor, FilesystemError\n"
        "anchor = DirectoryAnchor.open_path(Path(sys.argv[1]))\n"
        "scope = SecretScope(None)\n"
        "try:\n"
        "    try:\n"
        "        attempt_token(anchor, scope, 'launch_rejected')\n"
        "    except FilesystemError:\n"
        "        pass\n"
        "    else:\n"
        "        raise AssertionError('FIFO token was admitted')\n"
        "finally:\n"
        "    scope.release()\n"
        "    anchor.close()\n"
    )

    result = subprocess.run(
        [sys.executable, "-I", "-c", program, str(attempt)],
        check=True,
        capture_output=True,
        timeout=5,
    )

    assert result.stdout == b""
    assert result.stderr == b""
    assert token.is_fifo()


@pytest.mark.parametrize("refusal", ["socket", "token", "placement"])
@pytest.mark.asyncio
async def test_agent_session_refuses_unpreparable_launches(
    refusal: str, tmp_path: Path, kora_capture: Capture
) -> None:
    sources = AgentSources(ProductionResolver(), AllowDenyPolicies())
    work = detached_workload({})
    async with agent_session(
        tmp_path, kora_capture, sources, SystemClock(), bind_socket=refusal != "socket"
    ) as (session, attempt):
        if refusal == "token":
            token = attempt / "attempt.token"
            token.write_text("malformed")
            token.chmod(0o600)
        if refusal == "placement":
            (attempt / "harness").mkdir(mode=0o700)
        session.accept(SUBMITTER, None, work)

        first = await protocol_error(
            unless_busy(
                lambda: asyncio.to_thread(
                    session.supervise, SUBMITTER, False, work, lambda: None
                )
            )
        )
        repeated = await protocol_error(
            asyncio.to_thread(session.supervise, SUBMITTER, False, work, lambda: None)
        )
        unobserved = refusal_of(lambda: session.session_observation(SUBMITTER))

        assert (first.code, first.retryable) == ("launch_rejected", False)
        assert (repeated.code, repeated.retryable) == ("launch_rejected", False)
        assert unobserved == ("session_unobserved", False)
        assert not (attempt / "control" / "launch").exists()
        if refusal == "placement":
            assert tuple((attempt / "harness").iterdir()) == ()
        else:
            assert not (attempt / "harness").exists()
        if refusal == "token":
            assert (attempt / "attempt.token").read_text() == "malformed"
        else:
            assert not (attempt / "attempt.token").exists()


@pytest.mark.parametrize("refusal", ["workspace", "policy"])
@pytest.mark.asyncio
async def test_agent_final_refusals_settle_the_observation(
    refusal: str, tmp_path: Path, kora_capture: Capture
) -> None:
    sources = AgentSources(ProductionResolver(), RefusingPolicies())
    changes: dict[str, object] = (
        {"checkout": step_plan("import sys; sys.exit(3)")}
        if refusal == "workspace"
        else {}
    )
    work = detached_workload(changes)
    async with agent_session(tmp_path, kora_capture, sources, SystemClock()) as (
        session,
        attempt,
    ):
        session.accept(SUBMITTER, None, work)
        first = await protocol_error(
            unless_busy(
                lambda: asyncio.to_thread(
                    session.supervise, SUBMITTER, False, work, lambda: None
                )
            )
        )
        repeated = await protocol_error(
            asyncio.to_thread(session.supervise, SUBMITTER, False, work, lambda: None)
        )
        unobserved = refusal_of(lambda: session.session_observation(SUBMITTER))
    recorded = kinds(attempt / "journal.jsonl")

    expected = (
        "workspace_failed" if refusal == "workspace" else "workload_kind_unavailable"
    )
    assert (first.code, first.retryable) == (expected, False)
    assert repeated.code == expected
    assert unobserved == ("session_unobserved", False)
    assert "runner.session_spec" not in recorded


@pytest.mark.asyncio
async def test_agent_session_stop_is_immediate(
    tmp_path: Path, kora_capture: Capture
) -> None:
    source = script([turn("m1"), PAUSE], on_interrupt="ignore")
    resolver = FakeResolver(source, context_for(kora_capture))
    clock = GatedClock()
    sources = AgentSources(resolver, AllowDenyPolicies())
    work = detached_workload({})
    async with agent_session(tmp_path, kora_capture, sources, clock) as (
        session,
        attempt,
    ):
        session.accept(SUBMITTER, None, work)
        await unless_busy(
            lambda: asyncio.to_thread(
                session.supervise, SUBMITTER, False, work, lambda: None
            )
        )
        await source.paused.wait()
        running = session.status()

        session.fence()
        session.close_journal()
        await session.stop()

        status = session.status()
        completion = session.completion()
        reference = session.result_reference()
        unobserved = code_of_call(lambda: session.session_observation(SUBMITTER))
    recorded = kinds(attempt / "journal.jsonl")
    events = agent_events(kora_capture)

    assert running == ("running", "execution", None)
    assert resolver.adapters[0].killed.is_set()
    assert INTERRUPT_GRACE_NS not in clock.deadlines
    assert status == ("exited", "failed", 1)
    assert completion is Completion.COMPLETE
    assert reference is None
    assert unobserved == "session_unobserved"
    assert recorded[:2] == ["runner.workspace_prepared", "runner.session_spec"]
    assert "runner.session_result" not in recorded
    assert "runner.session_failed" not in recorded
    assert journal_threads_exited()
    assert writer_threads_exited()
    assert events[-1] == {"level": "INFO", "outcome": "started"}


@pytest.mark.asyncio
async def test_agent_session_revokes_pending_launch_before_waiting_for_its_host(
    tmp_path: Path, kora_capture: Capture
) -> None:
    source = script([turn("m1"), PAUSE], on_interrupt="ignore")
    resolver = FakeResolver(source, context_for(kora_capture), deaf=True)
    clock = GatedClock()
    target = tmp_path.resolve() / "sdk-target"
    executable = b"#!/bin/sh\nexit 0\n"
    target.write_bytes(executable)
    target.chmod(0o500)
    binary = ExpectedBinary(
        target, hashlib.sha256(executable).hexdigest(), len(executable)
    )
    sources = AgentSources(resolver, AllowDenyPolicies(), binary=binary)
    work = detached_workload({})
    async with agent_session(tmp_path, kora_capture, sources, clock) as (
        session,
        attempt,
    ):
        stopping: asyncio.Task[None] | None = None
        try:
            session.accept(SUBMITTER, None, work)
            await unless_busy(
                lambda: asyncio.to_thread(
                    session.supervise, SUBMITTER, False, work, lambda: None
                )
            )
            await asyncio.wait_for(source.paused.wait(), CONDITION_SECONDS)
            launches = session.launches()
            assert launches is not None
            launch = await launches.allocate(LaunchInputs({}, attempt / "candidate"))
            await asyncio.wait_for(clock.requested(launch.deadline), CONDITION_SECONDS)
            await clock.advance_to(1)

            stopping = asyncio.create_task(session.stop())
            await asyncio.wait_for(
                clock.requested(clock.monotonic_ns() + STOP_DEADLINE_NS),
                CONDITION_SECONDS,
            )
            host_waiting = not stopping.done()
            table_closed = launches.directory.closed
            launch_revoked = (
                launch.capability is None and not launch.cli_path.parent.exists()
            )
            launch_ended = launch.result.done()
            placements_retained = all(
                path.is_dir() for path in launches.placements.values()
            )
            scope_retained = bool(launches.token.reveal())
        finally:
            for adapter in resolver.adapters:
                adapter.killed.set()
            if stopping is not None:
                await asyncio.wait_for(stopping, CONDITION_SECONDS)
        outcome = await launch.ended()

    assert host_waiting
    assert table_closed
    assert launch_revoked
    assert launch_ended
    assert placements_retained
    assert scope_retained
    assert outcome.cause == "launch_failed"
    assert launches.pending == {}
    assert launch.timer.cancelled()
    assert not launches.path.exists()
    assert not (attempt / "harness").exists()
    with pytest.raises(SecretRevealRefused):
        launches.token.reveal()
    assert session.completion() is Completion.COMPLETE
    assert session.result_reference() is None
    assert journal_threads_exited()
    assert writer_threads_exited()


@pytest.mark.asyncio
async def test_production_runner_refuses_fake(
    tmp_path: Path, kora_capture: Capture
) -> None:
    resolver = ProductionResolver()
    workspace = WorkspaceRef(
        attempt_id="attempt-1",
        root=str(tmp_path),
        repo="/repository",
        commit=FAKE_COMMIT,
        base_commit=FAKE_COMMIT,
        branch=None,
    )
    resolved = {
        name: code_of_call(
            lambda name=name: resolver.resolve(
                AgentExecution.model_validate(execution(harness=name)), workspace
            )
        )
        for name in ("fake", "claude", "codex")
    }
    policy = code_of_call(
        lambda: RefusingPolicies().policy(
            AgentExecution.model_validate(execution(harness="fake"))
        )
    )
    async with agent_runtime(tmp_path, kora_capture, None) as (runtime, ctx, root):
        handle = await runtime.submit(agent_workload(runtime, "fake"), ctx)

        buffer = b"{}"
        before = await protocol_error(
            runtime.transfer_in(handle, script_item(buffer), script_chunks(buffer), ctx)
        )
        refused = await protocol_error(unless_busy(lambda: runtime.start(handle, ctx)))
        after = await protocol_error(
            runtime.transfer_in(handle, script_item(buffer), script_chunks(buffer), ctx)
        )
        observation = await protocol_error(runtime.session_observation(handle, ctx))
        recorded = journal(root, runtime)
        result = result_file(root, runtime)
        log = attempt_directory(root, runtime, "attempt-1") / "runner.log"
        runner_events = [
            (record.get("event"), record.get("outcome"))
            for record in map(json.loads, log.read_text().splitlines())
        ]

    assert dict(resolver.hosted) == {}
    assert resolved == {
        "fake": "workload_kind_unavailable",
        "claude": "workload_kind_unavailable",
        "codex": "workload_kind_unavailable",
    }
    assert before.code == after.code == "workload_kind_unavailable"
    assert not (result.parent.parent / "script").exists()
    assert policy == "workload_kind_unavailable"
    assert refused.code == "workload_kind_unavailable"
    assert observation.code == "session_unobserved"
    assert recorded == ["runner.workspace_prepared"]
    assert ("runtime.local.agent", "refused") in runner_events
    assert all(event != "runner.agent.session_started" for event, _ in runner_events)
    assert all(event != "runtime.local.transfer" for event, _ in runner_events)
    assert not result.exists()
    assert os.path.exists(result.parent.parent)


@pytest.mark.asyncio
async def test_fake_harness_reads_only_its_script_oversize_declared(
    tmp_path: Path, kora_capture: Capture
) -> None:
    sources = compose_sources(
        kora_capture.diagnostics, InstallationConfiguration.empty()
    )
    item = script_item(b"{}").model_copy(update={"size": SCRIPT_LIMIT + 1})
    async with agent_session(tmp_path, kora_capture, sources, SystemClock()) as (
        session,
        attempt,
    ):
        session.accept(SUBMITTER, None, detached_workload({}))

        refusal = refusal_of(lambda: session.decide_transfer(SUBMITTER, False, item))

        assert refusal == ("transfer_refused", False)
        assert not (attempt / "control/script").exists()
        assert not (attempt / "journal.jsonl").exists()


@pytest.mark.parametrize("phase", ["before", "after", "fenced"])
@pytest.mark.asyncio
async def test_production_runner_refuses_fake_transfer(
    phase: str, tmp_path: Path, kora_capture: Capture
) -> None:
    sources = AgentSources(ProductionResolver(), RefusingPolicies())
    work = detached_workload({})
    async with agent_session(tmp_path, kora_capture, sources, SystemClock()) as (
        session,
        attempt,
    ):
        session.accept(SUBMITTER, None, work)
        if phase == "after":
            await protocol_error(
                unless_busy(
                    lambda: asyncio.to_thread(
                        session.supervise, SUBMITTER, False, work, lambda: None
                    )
                )
            )
        elif phase == "fenced":
            session.fence_transfer()
        item = script_item(b"{}")

        refusal = refusal_of(lambda: session.decide_transfer(SUBMITTER, False, item))

        assert refusal == ("workload_kind_unavailable", False)
        assert not (attempt / "control/script").exists()


@pytest.mark.asyncio
async def test_fake_harness_reads_only_its_script_during_checkout(
    tmp_path: Path, kora_capture: Capture
) -> None:
    gate = tmp_path / "checkout-gate"
    os.mkfifo(gate, 0o600)
    async with agent_runtime(tmp_path, kora_capture, runner_command()) as (
        runtime,
        ctx,
        root,
    ):
        work = changed(
            agent_workload(runtime),
            {
                "checkout": step_plan(
                    f"from pathlib import Path; Path({str(gate)!r}).read_bytes(); "
                    f"print({FAKE_COMMIT!r})"
                )
            },
        )
        handle = await runtime.submit(work, ctx)
        buffer = FakeScript.model_validate(
            {
                "end": "completed",
                "on_interrupt": "acknowledge",
                "steps": [{"kind": "result", "status": "success"}],
            }
        ).serialise()

        await runtime.transfer_in(
            handle, script_item(buffer), script_chunks(buffer), ctx
        )
        before = journal(root, runtime)
        await asyncio.to_thread(gate.write_bytes, b"go")
        await unless_busy(lambda: runtime.start(handle, ctx))
        observation = await sealed(runtime, handle, ctx)

        assert before == ["transfer.materialised"]
        assert isinstance(observation, SessionResultObservation)
        assert observation.result.stop_reason == "completed"


@pytest.mark.asyncio
async def test_script_transfer_expiry_before_commit_allows_retry(
    tmp_path: Path, kora_capture: Capture
) -> None:
    sources = compose_sources(
        kora_capture.diagnostics, InstallationConfiguration.empty()
    )
    buffer = completed_script().serialise()
    item = script_item(buffer)
    async with agent_session(tmp_path, kora_capture, sources, SystemClock()) as (
        session,
        attempt,
    ):
        session.accept(SUBMITTER, None, detached_workload({}))
        placed = attempt / "control/script/script.json"
        incoming = session.open_incoming()
        os.write(incoming.descriptor, buffer)

        def authorize() -> None:
            if placed.exists():
                raise ChannelError(ChannelFailure.DEADLINE)

        try:
            with pytest.raises(ChannelError) as expired:
                session.finish_incoming(incoming, item, item.sha256, authorize)
        finally:
            session.discard_incoming(incoming)
            session.end_transfer()
        retained = placed.exists()
        before = (
            journal_events(attempt / "journal.jsonl")
            if (attempt / "journal.jsonl").exists()
            else ()
        )
        decision = session.decide_transfer(SUBMITTER, False, item)
        retry = session.open_incoming()
        try:
            os.write(retry.descriptor, buffer)
            session.finish_incoming(retry, item, item.sha256, lambda: None)
        finally:
            session.discard_incoming(retry)
            session.end_transfer()
        records = journal_events(attempt / "journal.jsonl")

        assert expired.value.reason is ChannelFailure.DEADLINE
        assert not retained
        assert all(record.kind != "transfer.materialised" for record in before)
        assert decision == "write"
        assert placed.read_bytes() == buffer
        assert [
            record.data for record in records if record.kind == "transfer.materialised"
        ] == [{"name": item.name, "size": item.size, "sha256": item.sha256}]


@pytest.mark.asyncio
async def test_script_transfer_replaces_stale_incoming(
    tmp_path: Path, kora_capture: Capture
) -> None:
    sources = compose_sources(
        kora_capture.diagnostics, InstallationConfiguration.empty()
    )
    async with agent_session(tmp_path, kora_capture, sources, SystemClock()) as (
        session,
        attempt,
    ):
        directory = attempt / "control/script"
        directory.parent.mkdir(mode=0o700)
        directory.mkdir(mode=0o700)
        (directory / ".incoming").write_bytes(b"stale")

        incoming = session.open_incoming()
        try:
            content = os.pread(incoming.descriptor, 6, 0)
        finally:
            session.discard_incoming(incoming)

        assert content == b""
        assert not (directory / ".incoming").exists()


@pytest.mark.parametrize("configured_policy", [False, True])
@pytest.mark.asyncio
async def test_production_runner_selects_installation_policy_before_resolving(
    configured_policy: bool, tmp_path: Path, kora_capture: Capture
) -> None:
    async with agent_runtime(tmp_path, kora_capture, None) as (runtime, ctx, root):
        installation = root / "runner.json"
        installation.write_text(
            json.dumps(
                {
                    "format": "kora.runner/1",
                    "harness": {},
                    "policy": {"allow_deny": {}} if configured_policy else {},
                }
            )
        )
        installation.chmod(0o600)

        handle = await runtime.submit(agent_workload(runtime), ctx)
        refused = await protocol_error(unless_busy(lambda: runtime.start(handle, ctx)))
        recorded = journal(root, runtime)

    assert refused.code == "workload_kind_unavailable"
    assert refused.retryable is False
    assert recorded == ["runner.workspace_prepared"] + (
        ["runner.session_spec"] if configured_policy else []
    )


@pytest.mark.asyncio
async def test_production_runner_refuses_claude_without_allocating_launch(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async with agent_runtime(tmp_path, kora_capture, None) as (runtime, ctx, root):
        installation = root / "runner.json"
        installation.write_text(
            json.dumps(
                {
                    "format": "kora.runner/1",
                    "harness": {
                        "claude": {
                            "credentials": {
                                "ANTHROPIC_API_KEY": "secret://env/ANTHROPIC_API_KEY"
                            }
                        }
                    },
                    "policy": {"allow_deny": {}},
                }
            )
        )
        installation.chmod(0o600)

        handle = await runtime.submit(agent_workload(runtime), ctx)
        refused = await protocol_error(unless_busy(lambda: runtime.start(handle, ctx)))
        repeated = await protocol_error(runtime.start(handle, ctx))
        observation = await protocol_error(runtime.session_observation(handle, ctx))
        recorded = journal(root, runtime)
        attempt = attempt_directory(root, runtime, "attempt-1")
        launch_allocated = (attempt / "control" / "launch").exists()
        placements_retained = (attempt / "harness").exists()
        runner_events = [
            record.get("event")
            for record in map(
                json.loads, (attempt / "runner.log").read_text().splitlines()
            )
        ]

    assert refused.code == repeated.code == "workload_kind_unavailable"
    assert refused.retryable is False
    assert observation.code == "session_unobserved"
    assert recorded == ["runner.workspace_prepared", "runner.session_spec"]
    assert not launch_allocated
    assert not placements_retained
    assert "runner.agent.session_started" not in runner_events
    assert "runtime.local.launcher.ended" not in runner_events
