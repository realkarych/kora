import asyncio
import base64
import hashlib
import json
import os
import signal
import sys
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TypeVar

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import kora_capture
from kora.ir.command_result import (
    CommandResultValidationError,
    verify_command_result_captures,
)
from kora.ir.json import JsonObject
from kora.ir.vcs_ref_json import canonical_bytes
from kora.plugins.runtime.local.channel import (
    OBSERVATION_BYTES,
    observation_envelope,
    read_observation,
)
from kora.plugins.runtime.local.command import COMPONENT
from kora.plugins.runtime.local.control import observe_command
from kora.plugins.runtime.local.reaper import adopted_pids
from kora.plugins.runtime.local.runtime import LocalRuntime
from kora.plugins.runtime.local.workload_session import Completion
from kora.protocols.common import ProtocolContext
from kora.protocols.errors import KoraProtocolError
from kora.protocols.identity import Handle, instant_wire
from kora.protocols.identity_records import ProcessIdentity
from kora.protocols.runtime import (
    MAX_CAPTURE_BYTES,
    MAX_TAIL_BYTES,
    CaptureFacts,
    CommandObservation,
    Workload,
)
from kora.protocols.secret_value import SecretScope
from kora.protocols.time import Clock, SystemClock
from kora.runner.filesystem import open_local_directory
from kora.runner.identity import SCAN_NS, NativeIdentityReader
from kora.runner.journal import RECORD_LIMIT
from kora.runner.peers import kernel_source
from kora.runner.registry import Submitter, TransitionExecutor
from tests.plugins.runtime.local.test_command import (
    attempt_directory,
    command_workload,
    journal_events,
    launched,
)
from tests.plugins.runtime.local.test_command_capture import (
    HeldIO,
    OffsetClock,
    log_bytes,
    observational_refusal,
)
from tests.plugins.runtime.local.test_command_workspace import (
    FAKE_COMMIT,
    OTHER_COMMIT,
    request,
    settled,
    step_plan,
)
from tests.support.local_delivery import ReapedSession
from tests.support.local_peers import (
    CONDITION_SECONDS,
    POLL_SECONDS,
    identity_of,
    until,
)
from tests.support.local_runtime import private_directory

__all__ = ["kora_capture"]

T = TypeVar("T")
OBSERVED = "runner.command_observed"
SUBMITTER = Submitter(dispatcher_id="test", process=identity_of(os.getpid()))
GATED = "\n".join(
    (
        "import os, time",
        "open('leader.pid.tmp', 'w').write(str(os.getpid()))",
        "os.replace('leader.pid.tmp', 'leader.pid')",
        "while not os.path.exists('go'): time.sleep(0.01)",
    )
)
ENDINGS = {
    "zero": ("pass", 0),
    "nonzero": ("import sys; sys.exit(3)", 3),
    "signal": ("import os, signal; os.kill(os.getpid(), signal.SIGTERM)", -15),
}


def checked_out(
    runtime: LocalRuntime, script: str, *, commit: str = FAKE_COMMIT
) -> Workload:
    return command_workload(
        runtime,
        "attempt-1",
        (sys.executable, "-I", "-c", script),
        checkout=step_plan(f"print({commit!r})"),
        checkout_request=request(
            "/repository", FAKE_COMMIT, (FAKE_COMMIT, OTHER_COMMIT)
        ),
    )


async def unless_busy(call: Callable[[], Awaitable[T]]) -> T:
    deadline = time.monotonic() + CONDITION_SECONDS
    while True:
        try:
            return await call()
        except KoraProtocolError as error:
            if error.code != "workload_busy" or time.monotonic() >= deadline:
                raise
        await asyncio.sleep(POLL_SECONDS)


async def runtime_answer(
    runtime: LocalRuntime, handle: Handle, ctx: ProtocolContext
) -> CommandObservation:
    return await unless_busy(lambda: runtime.command_observation(handle, ctx))


def session_answer(
    session: ReapedSession, submitter: Submitter = SUBMITTER
) -> CommandObservation | str:
    try:
        return session.command_observation(submitter)
    except KoraProtocolError as error:
        return error.code


async def observe_as_observer(
    path: str, runner: ProcessIdentity, executor: TransitionExecutor, base: Path
) -> object:
    return await observe_command(
        path,
        runner,
        "dispatcher-2",
        kernel_source(NativeIdentityReader()),
        executor,
        SystemClock(),
        "observe-1",
    )


async def settled_answer(session: ReapedSession) -> CommandObservation | str:
    await until(lambda: session_answer(session) != "workload_busy")
    return session_answer(session)


async def supervised(session: ReapedSession, workload: Workload) -> None:
    await unless_busy(
        lambda: asyncio.to_thread(
            session.supervise, SUBMITTER, False, workload, lambda: None
        )
    )


async def accepted(
    session: ReapedSession, workload: Workload, clock: Clock | None = None
) -> None:
    prelaunch: dict[int, int] = {}
    if sys.platform == "darwin":
        await asyncio.to_thread(
            session.capture_prelaunch,
            prelaunch,
            workload,
            (clock or SystemClock()).monotonic_ns() + SCAN_NS,
        )
    session.accept(SUBMITTER, prelaunch, workload)


def workspace_settled(attempt: Path) -> bool:
    kinds = {event.kind for event in journal_events(attempt / "journal.jsonl")}
    return bool(kinds & {"runner.workspace_prepared", "runner.workspace_failed"})


def observed_records(attempt: Path) -> list[JsonObject]:
    found: list[JsonObject] = []
    for line in (attempt / "journal.jsonl").read_bytes().splitlines():
        entry: JsonObject = json.loads(line)
        data = entry["data"]
        if entry["kind"] == OBSERVED and isinstance(data, dict):
            found.append(data)
    return found


def workspace_prepared(capture: Capture) -> bool:
    return any(
        record.get("event") == "runtime.local.workspace"
        and record.get("outcome") == "prepared"
        for record in capture.reader.records()
    )


def leader_pid(attempt: Path) -> int:
    return int((attempt / "candidate" / "leader.pid").read_text())


def described_child(pid: int) -> str:
    try:
        argv = Path(f"/proc/{pid}/cmdline").read_bytes()
        status = Path(f"/proc/{pid}/status").read_text().splitlines()
    except (FileNotFoundError, ProcessLookupError):
        return f"{pid} gone"
    state = next(line for line in status if line.startswith("State:"))
    shown = argv.replace(b"\0", b" ").decode(errors="replace").strip()
    return f"{pid} {state} {shown[:200]}"


def stray_children() -> tuple[str, ...]:
    if sys.platform != "linux":
        return ()
    listed = adopted_pids()
    if listed is None:
        return ("children unreadable",)
    return tuple(described_child(pid) for pid in sorted(listed))


async def without_stray_children() -> None:
    deadline = time.monotonic() + CONDITION_SECONDS
    while found := stray_children():
        assert time.monotonic() < deadline, f"the test process has children: {found}"
        await asyncio.sleep(POLL_SECONDS)


@asynccontextmanager
async def observed_session(
    tmp_path: Path,
    capture: Capture,
    *,
    clock: Clock | None = None,
    io: HeldIO | None = None,
    seed: bytes = b"",
) -> AsyncGenerator[tuple[ReapedSession, Path]]:
    attempt = private_directory(tmp_path.resolve(), "attempt")
    if seed:
        (attempt / "journal.jsonl").write_bytes(seed)
        (attempt / "journal.jsonl").chmod(0o600)
    directory = open_local_directory(attempt)
    session = ReapedSession(
        directory,
        str(attempt / "candidate"),
        SecretScope(capture.diagnostics),
        clock or SystemClock(),
        capture.diagnostics.logger(COMPONENT),
        capture_io=io,
    )
    try:
        await without_stray_children()
        yield session, attempt
    finally:
        if io is not None:
            io.release.set()
        await session.stop()
        directory.close()


def padding_record(size: int) -> bytes:
    at = instant_wire(SystemClock().now())
    empty = canonical_bytes(
        {"i": 0, "at": at, "kind": "runner.started", "data": {"pad": ""}}
    )
    pad = "x" * (size - len(empty) - 1)
    return (
        canonical_bytes(
            {"i": 0, "at": at, "kind": "runner.started", "data": {"pad": pad}}
        )
        + b"\n"
    )


def facts(
    payload: bytes, *, observed: int | None = None, eof: bool = True
) -> CaptureFacts:
    seen = len(payload) if observed is None else observed
    return CaptureFacts(
        payload_digest="sha256:" + hashlib.sha256(payload).hexdigest(),
        size=len(payload),
        observed_bytes=seen,
        eof=eof,
        truncated=seen > len(payload) or not eof,
        tail=payload[-4096:],
    )


def observation(
    exit_code: int, signal_number: int | None, stdout: CaptureFacts
) -> CommandObservation:
    return CommandObservation(
        exit_code=exit_code,
        signal=signal_number,
        termination="none",
        stop_reason="completed",
        duration_ms=12,
        repo="/repository",
        commit=FAKE_COMMIT,
        base_commit=OTHER_COMMIT,
        protected_paths=(),
        protection="unprotected",
        resources=(),
        resource_set_digest=None,
        resource_protection="none",
        stdout=stdout,
        stderr=facts(b""),
    )


def test_observation_result_maps_every_field() -> None:
    payload = b"y" * 5000
    observed = observation(-9, 9, facts(payload, observed=6000))

    result = observed.result("stdout-ref", "stderr-ref")

    value = result.value
    assert value["exit_code"] == -9
    assert value["signal"] == 9
    assert value["duration_ms"] == 12
    assert (value["repo"], value["commit"], value["base_commit"]) == (
        "/repository",
        FAKE_COMMIT,
        OTHER_COMMIT,
    )
    assert value["stdout"] == {
        "storage_ref": "stdout-ref",
        "payload_digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
        "size": 5000,
        "observed_bytes": 6000,
        "eof": True,
        "truncated": True,
        "tail_base64": base64.b64encode(payload[-4096:]).decode("ascii"),
    }
    verify_command_result_captures(result, stdout=payload, stderr=b"")


@pytest.mark.parametrize(
    ("exit_code", "signal_number"),
    [(0, 9), (-9, None), (-9, 15)],
    ids=["signal_with_exit", "missing_signal", "mismatched_signal"],
)
def test_observation_result_refuses_contradictions(
    exit_code: int, signal_number: int | None
) -> None:
    observed = observation(exit_code, signal_number, facts(b"out"))

    with pytest.raises(CommandResultValidationError):
        observed.result("stdout-ref", "stderr-ref")


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", sorted(ENDINGS))
async def test_command_result_preserves_observed_termination(
    tmp_path: Path, kora_capture: Capture, ending: str
) -> None:
    script, expected = ENDINGS[ending]
    async with launched(tmp_path, kora_capture) as (runtime, ctx, root):
        workload = checked_out(runtime, script)
        attempt = attempt_directory(root, runtime, "attempt-1")
        handle = await runtime.submit(workload, ctx)
        await until(lambda: settled(attempt / "journal.jsonl") == ["prepared"])
        await runtime.start(handle, ctx)

        observed = await runtime_answer(runtime, handle, ctx)
        records = observed_records(attempt)

    result = observed.result("stdout-ref", "stderr-ref").value
    assert observed.exit_code == expected
    assert observed.signal == (-expected if expected < 0 else None)
    assert (result["termination"], result["stop_reason"]) == ("none", "completed")
    assert len(records) == 1
    assert records[0]["exit_code"] == expected


@pytest.mark.asyncio
async def test_command_observation_builds_a_valid_command_result(
    tmp_path: Path, kora_capture: Capture
) -> None:
    script = "import os; os.write(1, b'out' * 3000); os.write(2, b'err')"
    async with launched(tmp_path, kora_capture) as (runtime, ctx, root):
        workload = checked_out(runtime, script)
        attempt = attempt_directory(root, runtime, "attempt-1")
        handle = await runtime.submit(workload, ctx)
        await until(lambda: settled(attempt / "journal.jsonl") == ["prepared"])
        await runtime.start(handle, ctx)
        observed = await runtime_answer(runtime, handle, ctx)

        _, stdout = await log_bytes(runtime, handle, ctx, "stdout", 0)
        _, stderr = await log_bytes(runtime, handle, ctx, "stderr", 0)
        record = observed_records(attempt)[0]

    result = observed.result("stdout-ref", "stderr-ref")
    verify_command_result_captures(result, stdout=stdout, stderr=stderr)
    value = result.value
    assert (stdout, stderr) == (b"out" * 3000, b"err")
    assert (value["repo"], value["commit"], value["base_commit"]) == (
        "/repository",
        FAKE_COMMIT,
        OTHER_COMMIT,
    )
    assert (value["protected_paths"], value["protection"]) == ([], "unprotected")
    assert (value["resources"], value["resource_set_digest"]) == ([], None)
    assert value["resource_protection"] == "none"
    assert record["stdout"] == {
        "payload_digest": "sha256:" + hashlib.sha256(stdout).hexdigest(),
        "size": len(stdout),
        "observed_bytes": len(stdout),
        "eof": True,
        "truncated": False,
    }


@pytest.mark.asyncio
async def test_command_observation_is_never_fabricated(
    tmp_path: Path, kora_capture: Capture
) -> None:
    answers: dict[str, object] = {}
    records: dict[str, list[JsonObject]] = {}
    async with launched(tmp_path, kora_capture) as (runtime, _ctx, _root):
        for case in (
            "before_exit",
            "failed_checkout",
            "unsealed",
            "fence_kill",
            "supervisor_fence",
            "shutdown",
        ):
            case_path = private_directory(tmp_path, case)
            io = HeldIO(None)
            if case == "unsealed":
                io.sync_error = OSError("held sync failure")
            commit = OTHER_COMMIT if case == "failed_checkout" else FAKE_COMMIT
            workload = checked_out(runtime, GATED, commit=commit)
            async with observed_session(case_path, kora_capture, io=io) as (
                session,
                attempt,
            ):
                await accepted(session, workload)
                await until(lambda attempt=attempt: workspace_settled(attempt))
                if case == "failed_checkout":
                    with pytest.raises(KoraProtocolError, match="prepared"):
                        await supervised(session, workload)
                    answers[case] = session_answer(session)
                    records[case] = observed_records(attempt)
                    continue
                await supervised(session, workload)
                session.attach_drains()
                await until(lambda: (attempt / "candidate" / "leader.pid").exists())
                if case == "before_exit":
                    answers[case] = session_answer(session)
                    (attempt / "candidate" / "go").touch()
                elif case == "unsealed":
                    (attempt / "candidate" / "go").touch()
                    answers[case] = await settled_answer(session)
                elif case == "supervisor_fence":
                    session.fence()
                    os.kill(leader_pid(attempt), signal.SIGKILL)
                    await until(lambda: session.completion() is not Completion.PENDING)
                    answers[case] = await settled_answer(session)
                elif case == "fence_kill":
                    session.fence_transfer()
                    os.kill(leader_pid(attempt), signal.SIGKILL)
                    await until(lambda: session.completion() is not Completion.PENDING)
                    answers[case] = await settled_answer(session)
                else:
                    await session.stop()
                    os.kill(leader_pid(attempt), signal.SIGKILL)
                    answers[case] = session_answer(session)
                records[case] = observed_records(attempt)

    assert answers == {
        "before_exit": "workload_busy",
        "failed_checkout": "command_unobserved",
        "unsealed": "command_unobserved",
        "fence_kill": "command_unobserved",
        "supervisor_fence": "command_unobserved",
        "shutdown": "command_unobserved",
    }
    assert all(found == [] for found in records.values())


@pytest.mark.asyncio
async def test_command_observation_refuses_other_kinds(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async with launched(tmp_path, kora_capture) as (runtime, _ctx, _root):
        workload = checked_out(runtime, "pass").model_copy(update={"kind": "agent"})
        async with observed_session(tmp_path, kora_capture) as (session, _attempt):
            await accepted(session, workload)

            answer = session_answer(session)

    assert answer == "workload_kind_unavailable"


@pytest.mark.asyncio
async def test_command_observation_duration_uses_execution_acknowledgement(
    tmp_path: Path, kora_capture: Capture
) -> None:
    clock = OffsetClock()
    async with launched(tmp_path, kora_capture) as (runtime, _ctx, _root):
        workload = checked_out(runtime, GATED)
        async with observed_session(tmp_path, kora_capture, clock=clock) as (
            session,
            attempt,
        ):
            await accepted(session, workload, clock)
            await until(lambda: workspace_prepared(kora_capture))
            clock.advance(100_000_000_000)
            started = time.monotonic_ns()
            await supervised(session, workload)
            session.attach_drains()
            await until(lambda: (attempt / "candidate" / "leader.pid").exists())
            clock.advance(7_000_000_000)
            (attempt / "candidate" / "go").touch()

            observed = await settled_answer(session)
            elapsed_ms = (time.monotonic_ns() - started) // 1_000_000

    assert isinstance(observed, CommandObservation)
    assert 7000 <= observed.duration_ms <= 7000 + elapsed_ms


@pytest.mark.asyncio
@pytest.mark.parametrize("room", [4096, 256], ids=["fits", "overflows"])
async def test_command_observation_record_fits_the_reader_bound(
    tmp_path: Path, kora_capture: Capture, room: int
) -> None:
    seed = padding_record(RECORD_LIMIT - room)
    async with launched(tmp_path, kora_capture) as (runtime, _ctx, _root):
        workload = checked_out(runtime, "pass")
        async with observed_session(tmp_path, kora_capture, seed=seed) as (
            session,
            attempt,
        ):
            await accepted(session, workload)
            await supervised(session, workload)
            session.attach_drains()

            answer = await settled_answer(session)
            size = (attempt / "journal.jsonl").stat().st_size
            records = observed_records(attempt)

    if room == 4096:
        assert isinstance(answer, CommandObservation)
        assert len(records) == 1
        assert RECORD_LIMIT - room < size <= RECORD_LIMIT
    else:
        assert answer == "command_unobserved"
        assert records == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tamper", "expected"),
    [("in_place", "integrity_error"), ("replaced", "transfer_refused")],
)
async def test_command_observation_refuses_tampered_capture(
    tmp_path: Path, kora_capture: Capture, tamper: str, expected: str
) -> None:
    async with launched(tmp_path, kora_capture) as (runtime, _ctx, _root):
        workload = checked_out(runtime, "import os; os.write(1, b'out')")
        async with observed_session(tmp_path, kora_capture) as (session, attempt):
            await accepted(session, workload)
            await supervised(session, workload)
            session.attach_drains()
            await until(lambda: observed_records(attempt) != [])
            log = attempt / "stdout.log"
            if tamper == "in_place":
                with log.open("r+b") as handle:
                    handle.write(b"OUT")
            else:
                (attempt / "other.log").write_bytes(b"out")
                log.unlink()
                log.symlink_to(attempt / "other.log")

            answer = await settled_answer(session)
            stranger = Submitter(dispatcher_id="other", process=SUBMITTER.process)
            foreign = session_answer(session, stranger)

    assert answer == expected
    assert foreign == "execution_unauthorized"


@pytest.mark.asyncio
async def test_command_observation_refuses_an_observational_peer(
    tmp_path: Path,
) -> None:
    refused = await observational_refusal(tmp_path, observe_as_observer)

    assert refused == "execution_unauthorized"


def test_maximal_observation_fits_its_answer_frame() -> None:
    stream = CaptureFacts(
        payload_digest="sha256:" + "f" * 64,
        size=MAX_CAPTURE_BYTES,
        observed_bytes=2**63 - 1,
        eof=False,
        truncated=True,
        tail=b"\xff" * MAX_TAIL_BYTES,
    )
    maximal = CommandObservation(
        exit_code=-255,
        signal=255,
        termination="none",
        stop_reason="completed",
        duration_ms=2**63 - 1,
        repo='"' * 4096,
        commit='"' * 255,
        base_commit='"' * 255,
        protected_paths=(),
        protection="unprotected",
        resources=(),
        resource_set_digest=None,
        resource_protection="none",
        stdout=stream,
        stderr=stream,
    )

    envelope = observation_envelope("r" * 64, maximal)

    assert len(envelope) <= OBSERVATION_BYTES
    assert read_observation(envelope, "r" * 64) == maximal


@pytest.mark.asyncio
async def test_command_observation_survives_late_drain_attachment(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async with launched(tmp_path, kora_capture) as (runtime, _ctx, _root):
        workload = checked_out(runtime, "import os; os.write(1, b'out')")
        async with observed_session(tmp_path, kora_capture) as (session, attempt):
            await accepted(session, workload)
            await supervised(session, workload)
            await until(lambda: session.completion() is not Completion.PENDING)
            stopped = session.completion()

            session.attach_drains()
            answer = await settled_answer(session)
            records = observed_records(attempt)

    assert stopped is Completion.COMPLETE
    assert isinstance(answer, CommandObservation)
    assert answer.stdout.size == 3
    assert len(records) == 1
