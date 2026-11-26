import asyncio
import hashlib
import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Final

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import context_for, kora_capture
from kora.ir.json import JsonObject
from kora.plugins.runtime.local.agent import AgentSources, HarnessCredentials
from kora.plugins.runtime.local.installation import InstallationConfiguration
from kora.plugins.runtime.local.launch import ExpectedBinary
from kora.plugins.runtime.local.production import production_sources
from kora.protocols.common import ProtocolContext
from kora.protocols.errors import KoraProtocolError
from kora.protocols.harness import AgentExecution, LaunchInputs
from kora.protocols.secret_value import SecretRevealRefused, SecretValue
from kora.protocols.time import SystemClock
from kora.runner.session_result import RESULT_DIRECTORY
from kora.runner.sink import ADAPTER_KINDS
from tests.plugins.harness.fake.fake_support import result, tool_call, turn
from tests.plugins.runtime.local.test_agent_session import (
    agent_session,
    detached_workload,
)
from tests.plugins.runtime.local.test_command_observation import SUBMITTER, unless_busy
from tests.plugins.runtime.local.test_production_sources import CLAUDE, file_sections
from tests.runner.agent_support import (
    PAUSE,
    FakeResolver,
    GatedScript,
    ObservedFakeHarness,
    journal_threads_exited,
    script,
)
from tests.support.local_peers import CONDITION_SECONDS, until
from tests.support.local_runtime import protocol_error

__all__ = ["kora_capture"]

KEY: Final[str] = "configured-claude-credential-1696"


def configured_sources(root: Path, capture: Capture) -> AgentSources:
    installation = InstallationConfiguration(
        {"claude": CLAUDE}, {"allow_deny": {}}, file_sections(root)
    )
    return production_sources(capture.diagnostics, installation)


def key_file(root: Path, value: str = KEY) -> Path:
    directory = root / "credentials"
    directory.mkdir(mode=0o700)
    key = directory / "ANTHROPIC_API_KEY"
    key.write_text(value, encoding="utf-8")
    key.chmod(0o600)
    return key


@pytest.mark.parametrize(
    ("kind", "reason"),
    [
        ("missing", "secret_missing"),
        ("readable", "secret_unreadable"),
        ("symlink", "secret_unreadable"),
        ("prefix", "secret_reference_not_allowed"),
        ("admission", "secret_admission_refused"),
    ],
)
@pytest.mark.asyncio
async def test_unresolvable_credential_refuses_before_launch(
    tmp_path: Path, kora_capture: Capture, kind: str, reason: str
) -> None:
    key = key_file(tmp_path, "x" if kind == "admission" else KEY)
    if kind == "missing":
        key.unlink()
    elif kind == "readable":
        key.chmod(0o640)
    elif kind == "symlink":
        target = key.with_name("target")
        key.rename(target)
        key.symlink_to(target)
    sections = file_sections(tmp_path)
    if kind == "prefix":
        sections["file"]["namespace_prefixes"] = {"local-runner": ["ALLOWED_"]}
    sources = production_sources(
        kora_capture.diagnostics,
        InstallationConfiguration({"claude": CLAUDE}, {"allow_deny": {}}, sections),
    )
    work = detached_workload({})
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

        assert first.code == repeated.code == "workload_kind_unavailable"
        assert first.retryable is False
        assert first.details == {}
        assert session.launches() is None
        assert not (attempt / "control" / "launch").exists()
        assert not (attempt / "harness").exists()
        assert kora_capture.diagnostics.registry.mask(KEY) == KEY
    events = [
        event
        for event in kora_capture.finish()
        if event.get("event") == "secrets.file.refused"
    ]
    assert len(events) == 1
    assert events[0]["reason"] == reason
    assert events[0]["attempt_id"] == work.attempt_id
    assert KEY not in str(events)


@pytest.mark.parametrize("value", [KEY, KEY + '"\\é'])
@pytest.mark.parametrize("ending", ["success", "failure", "cancellation"])
@pytest.mark.asyncio
async def test_credential_scope_released_at_every_attempt_end(
    tmp_path: Path, kora_capture: Capture, ending: str, value: str
) -> None:
    key_file(tmp_path, value)
    registry = kora_capture.diagnostics.registry
    baseline_count = registry.count()
    baseline_bytes = registry.retained_bytes()
    source = script(
        [PAUSE]
        if ending == "cancellation"
        else ([{"kind": "result", "status": "success"}] if ending == "success" else []),
        end="cli_exit_without_result" if ending == "failure" else "completed",
    )
    resolver = FakeResolver(source, context_for(kora_capture))
    configured = configured_sources(tmp_path, kora_capture)
    held = HeldCredentials(configured.credentials)
    sources = replace(configured, resolver=resolver, credentials=held)
    work = detached_workload({})
    async with agent_session(tmp_path, kora_capture, sources, SystemClock()) as (
        session,
        attempt,
    ):
        session.accept(SUBMITTER, None, work)
        starting = asyncio.create_task(
            unless_busy(
                lambda: asyncio.to_thread(
                    session.supervise, SUBMITTER, False, work, lambda: None
                )
            )
        )
        try:
            await asyncio.wait_for(held.entered.wait(), CONDITION_SECONDS)
            credential = held.values["ANTHROPIC_API_KEY"]
            assert credential.reveal() == value
            assert registry.mask(value) == "[redacted]"

            held.release.set()
            await asyncio.wait_for(starting, CONDITION_SECONDS)
            if ending == "cancellation":
                await asyncio.wait_for(source.paused.wait(), CONDITION_SECONDS)
                await session.stop()
            else:
                await until(lambda: session.status()[0] == "exited")

            with pytest.raises(SecretRevealRefused):
                credential.reveal()
            assert registry.mask(value) == value
            assert registry.count() == baseline_count
            assert registry.retained_bytes() == baseline_bytes
            assert not (attempt / "control" / "launch").exists()
            assert not (attempt / "harness").exists()
            assert session.status()[0] == "exited"
        finally:
            held.release.set()
            await asyncio.gather(starting, return_exceptions=True)


@pytest.mark.asyncio
async def test_second_attempt_resolves_credential_afresh(
    tmp_path: Path, kora_capture: Capture
) -> None:
    key = key_file(tmp_path)
    target = tmp_path / "binary"
    target.write_bytes(b"#!/bin/sh\nexit 0\n")
    target.chmod(0o500)
    binary = ExpectedBinary(
        target, hashlib.sha256(target.read_bytes()).hexdigest(), target.stat().st_size
    )
    configured = configured_sources(tmp_path, kora_capture)
    registry = kora_capture.diagnostics.registry
    environments: list[dict[str, str]] = []
    for index, value in enumerate((KEY, KEY + "-rotated")):
        baseline_count = registry.count()
        baseline_bytes = registry.retained_bytes()
        key.write_text(value, encoding="utf-8")
        root = tmp_path / str(index)
        root.mkdir(mode=0o700)
        source = script([PAUSE])
        sources = replace(
            configured,
            resolver=FakeResolver(source, context_for(kora_capture)),
            binary=binary,
        )
        work = detached_workload({"attempt_id": f"attempt-{index + 1}"})
        async with agent_session(root, kora_capture, sources, SystemClock()) as (
            session,
            attempt,
        ):
            session.accept(SUBMITTER, None, work)

            await unless_busy(
                lambda work=work: asyncio.to_thread(
                    session.supervise, SUBMITTER, False, work, lambda: None
                )
            )
            await asyncio.wait_for(source.paused.wait(), CONDITION_SECONDS)
            launches = session.launches()
            assert launches is not None
            with pytest.raises(KoraProtocolError) as collision:
                await launches.allocate(
                    LaunchInputs({"ANTHROPIC_API_KEY": value}, attempt / "candidate")
                )
            assert collision.value.code == "launch_rejected"
            assert not launches.pending
            assert tuple(launches.path.iterdir()) == ()
            launch = await launches.allocate(LaunchInputs({}, attempt / "candidate"))
            assert launch.spec is not None
            environments.append(
                {entry.name: entry.value for entry in launch.spec.environment}
            )
            credential = launches.harness_credentials["ANTHROPIC_API_KEY"]
            await session.stop()

            with pytest.raises(SecretRevealRefused):
                credential.reveal()
            assert registry.mask(value) == value
            assert registry.count() == baseline_count
            assert registry.retained_bytes() == baseline_bytes
    assert [env["ANTHROPIC_API_KEY"] for env in environments] == [KEY, KEY + "-rotated"]


class HeldCredentials:
    def __init__(self, delegate: HarnessCredentials) -> None:
        self.delegate = delegate
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.values: Mapping[str, SecretValue] = {}

    async def resolve(
        self, execution: AgentExecution, ctx: ProtocolContext
    ) -> Mapping[str, SecretValue]:
        self.values = await self.delegate.resolve(execution, ctx)
        self.entered.set()
        await self.release.wait()
        return self.values


class HeldBeforeAdmissionCredentials:
    def __init__(self, delegate: HarnessCredentials) -> None:
        self.delegate = delegate
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def resolve(
        self, execution: AgentExecution, ctx: ProtocolContext
    ) -> Mapping[str, SecretValue]:
        self.entered.set()
        await self.release.wait()
        return await self.delegate.resolve(execution, ctx)


@pytest.mark.parametrize("admission", ["before", "after"])
@pytest.mark.asyncio
async def test_stop_during_credential_resolution_prevents_launch(
    tmp_path: Path, kora_capture: Capture, admission: str
) -> None:
    key_file(tmp_path)
    configured = configured_sources(tmp_path, kora_capture)
    held = (
        HeldBeforeAdmissionCredentials(configured.credentials)
        if admission == "before"
        else HeldCredentials(configured.credentials)
    )
    sources = replace(configured, credentials=held)
    work = detached_workload({})
    async with agent_session(tmp_path, kora_capture, sources, SystemClock()) as (
        session,
        attempt,
    ):
        session.accept(SUBMITTER, None, work)
        starting = asyncio.create_task(
            unless_busy(
                lambda: asyncio.to_thread(
                    session.supervise, SUBMITTER, False, work, lambda: None
                )
            )
        )
        try:
            await asyncio.wait_for(held.entered.wait(), CONDITION_SECONDS)

            await session.stop()
            held.release.set()
            refused = await protocol_error(
                asyncio.wait_for(starting, CONDITION_SECONDS)
            )

            assert refused.code == "journal_unavailable"
            assert session.launches() is None
            assert not (attempt / "control" / "launch").exists()
            assert not (attempt / "harness").exists()
            assert kora_capture.diagnostics.registry.mask(KEY) == KEY
            if isinstance(held, HeldCredentials):
                with pytest.raises(SecretRevealRefused):
                    held.values["ANTHROPIC_API_KEY"].reveal()
        finally:
            held.release.set()
            await asyncio.gather(starting, return_exceptions=True)

    if admission == "before":
        events = [
            event
            for event in kora_capture.finish()
            if event.get("event") == "secrets.file.refused"
        ]
        assert len(events) == 1
        assert events[0]["reason"] == "secret_admission_refused"


@pytest.mark.asyncio
async def test_conflicting_digest_refuses_before_credential_resolution(
    tmp_path: Path, kora_capture: Capture
) -> None:
    key_file(tmp_path)
    sources = configured_sources(tmp_path, kora_capture)
    work = detached_workload({"execution_request_digest": "sha256:" + "0" * 64})
    async with agent_session(tmp_path, kora_capture, sources, SystemClock()) as (
        session,
        attempt,
    ):
        session.accept(SUBMITTER, None, work)

        refused = await protocol_error(
            unless_busy(
                lambda: asyncio.to_thread(
                    session.supervise, SUBMITTER, False, work, lambda: None
                )
            )
        )

        assert refused.code == "workload_conflict"
        assert session.launches() is None
        assert not (attempt / "control" / "launch").exists()
    assert all(
        event.get("event") != "secrets.file.resolved" for event in kora_capture.finish()
    )


@pytest.mark.parametrize("identifier", [False, True], ids=["content", "identifier"])
@pytest.mark.asyncio
async def test_handle_and_events_are_secret_and_capability_free(
    tmp_path: Path, kora_capture: Capture, identifier: bool
) -> None:
    key_file(tmp_path)
    token = "1785" * 8
    content = f"before {KEY} {token} after"
    call = tool_call("Bash", "call-1")
    call["outcome"] = {"is_error": False, "content": content}
    documents: list[JsonObject] = [
        turn("m1", content),
        call,
        {
            "kind": "event",
            "event": "error",
            "data": {"code": "harness_failed", "text": content, "truncated": False},
        },
    ]
    if identifier:
        documents.append(turn(f"id-{token}", "refused"))
    else:
        documents.append(
            {"kind": "output", "source": "structured_output", "value": content}
        )
    documents.append(result())
    source = GatedScript(script(documents))
    resolver = FakeResolver(
        source, context_for(kora_capture), adapter_type=ObservedFakeHarness
    )
    sources = replace(configured_sources(tmp_path, kora_capture), resolver=resolver)
    work = detached_workload({})
    async with agent_session(tmp_path, kora_capture, sources, SystemClock()) as (
        session,
        attempt,
    ):
        token_path = attempt / "attempt.token"
        token_path.write_text(token, encoding="ascii")
        token_path.chmod(0o600)
        session.accept(SUBMITTER, None, work)

        await unless_busy(
            lambda: asyncio.to_thread(
                session.supervise, SUBMITTER, False, work, lambda: None
            )
        )
        await asyncio.wait_for(source.entered.wait(), CONDITION_SECONDS)
        try:
            registry = kora_capture.diagnostics.registry
            masked_key = registry.mask(KEY)
            masked_token = registry.mask(token)
            source.release.set()
            await until(lambda: session.status()[0] == "exited")
            reference = session.session_observation(SUBMITTER)
            payload = (
                attempt / "control" / RESULT_DIRECTORY / reference.name
            ).read_bytes()
            journal = (attempt / "journal.jsonl").read_bytes()
            adapter = resolver.adapters[0]
            assert isinstance(adapter, ObservedFakeHarness)
            sink = adapter.runner_sink
            assert sink is not None
            liveness = sink.liveness()
            await session.stop()
        finally:
            source.release.set()

        assert masked_key == masked_token == "[redacted]"
        assert json.loads(payload) == {
            "kind": "session_failed",
            "code": "event_refused" if identifier else "secret_leak",
        }
        assert reference.size == len(payload)
        assert reference.digest == "sha256:" + hashlib.sha256(payload).hexdigest()
        for value in (KEY, token):
            assert value.encode() not in journal
            assert value.encode() not in payload
            assert value not in str(reference)
        records: list[JsonObject] = [json.loads(line) for line in journal.splitlines()]
        host_started = next(
            index
            for index, record in enumerate(records)
            if record["kind"] == "runner.session_spec"
        )
        assert all(
            record["kind"] in ADAPTER_KINDS or str(record["kind"]).startswith("runner.")
            for record in records[host_started:]
        )
        assert all("kill" not in str(record["kind"]) for record in records)
        assert b"kill_handle" not in journal
        assert b"[redacted] [redacted]" in journal
        assert not liveness.calls_open
        assert journal_threads_exited()
