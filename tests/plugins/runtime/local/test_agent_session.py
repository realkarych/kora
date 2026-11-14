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
    AgentSession,
    AgentSources,
    ProductionResolver,
    RefusingPolicies,
    agent_admission,
)
from kora.plugins.runtime.local.command import COMPONENT
from kora.plugins.runtime.local.completed import recorded_exited
from kora.plugins.runtime.local.reaper import AdoptedReaper
from kora.plugins.runtime.local.runtime import LocalRuntime
from kora.plugins.runtime.local.workload_session import Completion
from kora.plugins.runtime.local.workspace import Admitted
from kora.protocols.common import ProtocolContext
from kora.protocols.deadlines import ExecutionDeadline
from kora.protocols.errors import KoraProtocolError
from kora.protocols.harness import AgentExecution
from kora.protocols.identity import Handle
from kora.protocols.runtime import (
    SessionObservation,
    SessionResultObservation,
    Workload,
)
from kora.protocols.secret_value import SecretScope
from kora.protocols.time import Clock, SystemClock
from kora.protocols.vcs import WorkspaceRef
from kora.runner.agent import INTERRUPT_GRACE_NS
from kora.runner.filesystem import open_local_directory
from kora.runner.identity import NativeIdentityReader
from kora.runner.registry import Completed, ControlFileRef
from kora.runner.session_result import RESULT_DIRECTORY, RESULT_NAME
from tests.plugins.harness.fake.fake_support import turn
from tests.plugins.runtime.local.test_command import attempt_directory
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
from tests.support.agent_runner import AllowDenyPolicies, agent_runner_command
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
    "deadline": same(
        {"deadline": TypeAdapter(ExecutionDeadline).dump_python(DEADLINE, mode="json")}
    ),
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
        {"limits": {"max_events": 10, "max_turns": 3}},
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
        "limits": {},
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
    tmp_path: Path, capture: Capture, sources: AgentSources, clock: Clock
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
    )
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
    async with agent_runtime(
        tmp_path, kora_capture, agent_runner_command("result")
    ) as (
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
    assert recorded == [
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
    async with agent_runtime(
        tmp_path, kora_capture, agent_runner_command("result")
    ) as (
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
    async with agent_runtime(
        tmp_path, kora_capture, agent_runner_command("result")
    ) as (
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


def test_agent_admission_accepts_the_supported_shape() -> None:
    work = detached_workload({})

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

        refused = await protocol_error(unless_busy(lambda: runtime.start(handle, ctx)))
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
    assert policy == "workload_kind_unavailable"
    assert refused.code == "workload_kind_unavailable"
    assert observation.code == "session_unobserved"
    assert recorded == ["runner.workspace_prepared"]
    assert ("runtime.local.agent", "refused") in runner_events
    assert all(event != "runner.agent.session_started" for event, _ in runner_events)
    assert not result.exists()
    assert os.path.exists(result.parent.parent)
