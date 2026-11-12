import asyncio
import os
import signal
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Mapping
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from kora.conformance.capture import Capture
from kora.conformance.common import kora_capture
from kora.ir.json import JsonObject
from kora.plugins.runtime.local.command import COMPONENT
from kora.plugins.runtime.local.reaper import AdoptedReaper
from kora.plugins.runtime.local.workload_session import Completion
from kora.plugins.vcs.git.plans import CommitIdentity, PlanSettings, checkout_plan
from kora.plugins.vcs.git.tools import GitTools
from kora.protocols.runtime import MAX_CHECKOUT_REQUEST_BYTES, CheckoutSource
from kora.protocols.secret_value import SecretScope
from kora.protocols.time import SystemClock
from kora.protocols.vcs import CheckoutPlan, PlanStep
from kora.runner.filesystem import open_local_directory
from kora.runner.registry import Submitter
from tests.plugins.runtime.local.test_command import (
    attempt_directory,
    binary,
    command_workload,
    journal_events,
    launched,
    runtime_outcomes,
)
from tests.support.local_delivery import ReapedSession, delivery_server
from tests.support.local_peers import identity_of, until
from tests.support.local_runtime import (
    open_descriptors,
    private_directory,
    protocol_error,
)

__all__ = ["kora_capture"]

FAKE_COMMIT = "a" * 40
OTHER_COMMIT = "b" * 40
CAPTURE_EVENT = "runtime.local.capture"
CHECKED_README = (
    "import pathlib, sys\n"
    "sys.exit(0 if pathlib.Path('README').read_text() == 'second\\n' else 7)"
)


def git(arguments: list[str], cwd: Path) -> str:
    environment = {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "HOME": str(cwd),
        "PATH": "/usr/bin:/bin",
    }
    completed = subprocess.run(
        [binary("git"), "-c", "user.name=kora", "-c", "user.email=kora@example.invalid"]
        + arguments,
        cwd=cwd,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def repository(tmp_path: Path) -> tuple[Path, str, str]:
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    work.mkdir()
    git(["init", "--quiet", "--bare", str(remote)], tmp_path)
    git(["init", "--quiet"], work)
    (work / "README").write_text("first\n")
    git(["add", "README"], work)
    git(["commit", "--quiet", "-m", "base"], work)
    base = git(["rev-parse", "HEAD"], work)
    (work / "README").write_text("second\n")
    git(["commit", "--quiet", "-am", "head"], work)
    head = git(["rev-parse", "HEAD"], work)
    git(["push", "--quiet", str(remote), "HEAD:refs/heads/main"], work)
    return remote, base, head


def git_plan(remote: Path, head: str) -> JsonObject:
    settings = PlanSettings(
        GitTools(binary("git"), sys.executable, None),
        str(remote),
        CommitIdentity("kora", "kora@example.invalid"),
    )
    return checkout_plan(settings, head, "kora-attempt").model_dump(mode="json")


def step_plan(
    script: str,
    credentials: dict[str, str] | None = None,
    env: dict[str, str] | None = None,
) -> JsonObject:
    plan = CheckoutPlan(
        required_tools=("python",),
        credentials=credentials or {},
        files=(),
        steps=(
            PlanStep(
                argv=("python", "-I", "-c", script),
                cwd="candidate",
                env=env or {},
                stdin=None,
                timeout=timedelta(seconds=60),
                capture="commit",
            ),
        ),
    )
    return plan.model_dump(mode="json")


def gated_script(gate: Path, commit: str, files: int = 0, hold: float = 0) -> str:
    return "\n".join(
        (
            "import os, time",
            "open('step.pid', 'w').write(str(os.getpid()))",
            f"while not os.path.exists({str(gate)!r}): time.sleep(0.01)",
            f"time.sleep({hold})",
            f"[open(f'file-{{index}}', 'w').close() for index in range({files})]",
            f"print({commit!r})",
        )
    )


def request(
    repo: str,
    rev: str,
    pinned: tuple[str, str] | None = None,
    protected: tuple[str, ...] = (),
) -> JsonObject:
    return {
        "repo": repo,
        "rev": rev,
        "pinned": None
        if pinned is None
        else {"commit": pinned[0], "base_commit": pinned[1]},
        "branch": "kora-attempt",
        "protected_paths": list(protected),
    }


def kinds(journal: Path) -> list[str]:
    return [event.kind for event in journal_events(journal)]


def record(journal: Path, kind: str) -> JsonObject | None:
    for event in journal_events(journal):
        if event.kind == kind:
            return event.data
    return None


def settled(journal: Path) -> list[str]:
    return runtime_outcomes(journal.parent / "runner.log", "runtime.local.workspace")


def pid_gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


@pytest.mark.asyncio
async def test_command_workspace_is_prepared_before_start(
    tmp_path: Path, kora_capture: Capture
) -> None:
    remote, base, head = repository(tmp_path)
    async with launched(tmp_path, kora_capture) as (runtime, ctx, root):
        workload = command_workload(
            runtime,
            "attempt-1",
            (sys.executable, "-I", "-c", CHECKED_README),
            checkout=git_plan(remote, head),
            checkout_request=request(str(remote), head, (head, base)),
        )
        journal = attempt_directory(root, runtime, "attempt-1") / "journal.jsonl"
        handle = await runtime.submit(workload, ctx)
        await until(lambda: settled(journal) == ["prepared"])

        await runtime.start(handle, ctx)
        await until(lambda: record(journal, "command_exited") is not None)

    assert record(journal, "runner.workspace_prepared") == {
        "repo": str(remote),
        "commit": head,
        "base_commit": base,
        "branch": "kora-attempt",
    }
    assert record(journal, "command_exited") == {"exit_code": 0}
    assert kinds(journal).index("runner.workspace_prepared") < kinds(journal).index(
        "runner.candidate_started"
    )


@pytest.mark.asyncio
async def test_command_start_waits_for_workspace(
    tmp_path: Path, kora_capture: Capture
) -> None:
    gate = tmp_path / "gate"
    async with launched(tmp_path, kora_capture) as (runtime, ctx, root):
        workload = command_workload(
            runtime,
            "attempt-1",
            (sys.executable, "-I", "-c", "pass"),
            checkout=step_plan(gated_script(gate, FAKE_COMMIT)),
            checkout_request=request("/repository", FAKE_COMMIT),
        )
        journal = attempt_directory(root, runtime, "attempt-1") / "journal.jsonl"
        handle = await runtime.submit(workload, ctx)

        busy = await protocol_error(runtime.start(handle, ctx))
        gate.touch()
        await until(lambda: settled(journal) == ["prepared"])
        await runtime.start(handle, ctx)
        await until(lambda: record(journal, "command_exited") is not None)

    assert (busy.code, busy.retryable) == ("workload_busy", True)
    assert record(journal, "runner.workspace_prepared") == {
        "repo": "/repository",
        "commit": FAKE_COMMIT,
        "base_commit": FAKE_COMMIT,
        "branch": None,
    }
    assert record(journal, "command_exited") == {"exit_code": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["pinned_mismatch", "credentials", "missing"])
async def test_command_workspace_failure_never_starts(
    tmp_path: Path, kora_capture: Capture, case: str
) -> None:
    gate = tmp_path / "gate"
    gate.touch()
    remote, base, _head = repository(tmp_path)
    plans: dict[str, tuple[JsonObject, JsonObject, str]] = {
        "pinned_mismatch": (
            step_plan(gated_script(gate, FAKE_COMMIT)),
            request("/repository", FAKE_COMMIT, (OTHER_COMMIT, FAKE_COMMIT)),
            "pinned_commit_mismatch",
        ),
        "credentials": (
            step_plan(gated_script(gate, FAKE_COMMIT), {"TOKEN": "credential-1"}),
            request("/repository", FAKE_COMMIT),
            "checkout_credentials_refused",
        ),
        "missing": (
            git_plan(remote, OTHER_COMMIT),
            request(str(remote), OTHER_COMMIT, (OTHER_COMMIT, base)),
            "step_failed",
        ),
    }
    plan, source, expected = plans[case]
    async with launched(tmp_path, kora_capture) as (runtime, ctx, root):
        workload = command_workload(
            runtime,
            "attempt-1",
            (sys.executable, "-I", "-c", "pass"),
            checkout=plan,
            checkout_request=source,
        )
        journal = attempt_directory(root, runtime, "attempt-1") / "journal.jsonl"
        handle = await runtime.submit(workload, ctx)
        await until(lambda: settled(journal) == ["failed"])

        refused = await protocol_error(runtime.start(handle, ctx))

    failed = record(journal, "runner.workspace_failed")
    assert refused.code == "workspace_failed"
    assert failed is not None
    assert failed == {"code": expected}
    assert "runner.candidate_started" not in kinds(journal)
    assert "runner.workspace_prepared" not in kinds(journal)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["protected_paths", "workspace_ref"])
async def test_command_checkout_request_is_validated(
    tmp_path: Path, kora_capture: Capture, case: str
) -> None:
    gate = tmp_path / "gate"
    gate.touch()
    workspace_ref: JsonObject = {
        "attempt_id": "attempt-1",
        "root": str(tmp_path / "root"),
        "repo": "/repository",
        "commit": FAKE_COMMIT,
        "base_commit": FAKE_COMMIT,
        "branch": None,
    }
    changes: dict[str, tuple[JsonObject, JsonObject, str]] = {
        "protected_paths": (
            step_plan(gated_script(gate, FAKE_COMMIT)),
            request("/repository", FAKE_COMMIT, protected=("secret/**",)),
            "protected_paths_unsupported",
        ),
        "workspace_ref": (
            workspace_ref,
            request("/repository", FAKE_COMMIT),
            "workload_kind_unavailable",
        ),
    }
    checkout, source, expected = changes[case]
    async with launched(tmp_path, kora_capture) as (runtime, ctx, root):
        workload = command_workload(
            runtime,
            "attempt-1",
            (sys.executable, "-I", "-c", "pass"),
            checkout=checkout,
            checkout_request=source,
        )
        attempt = attempt_directory(root, runtime, "attempt-1")
        journal = attempt / "journal.jsonl"
        handle = await runtime.submit(workload, ctx)

        refused = await protocol_error(runtime.start(handle, ctx))

    assert refused.code == expected
    assert not (attempt / "control").exists()
    assert kinds(journal) == []


def test_checkout_request_fields_are_required_and_bounded() -> None:
    paths = ["p" * 4000] * 17
    bounded: dict[str, object] = {
        "repo": "/repository",
        "rev": FAKE_COMMIT,
        "pinned": None,
        "branch": "main",
        "protected_paths": [],
    }
    oversized: dict[str, object] = {**bounded, "protected_paths": paths}
    missing = {key: value for key, value in bounded.items() if key != "pinned"}

    accepted = CheckoutSource.model_validate(bounded)
    rejected = [
        document for document in (oversized, missing) if not validates(document)
    ]

    assert accepted.pinned is None
    assert sum(len(path) for path in paths) > MAX_CHECKOUT_REQUEST_BYTES
    assert rejected == [oversized, missing]


def validates(document: Mapping[str, object]) -> bool:
    try:
        CheckoutSource.model_validate(document)
    except ValidationError:
        return False
    return True


@pytest.mark.asyncio
async def test_command_without_checkout_request_is_unchanged(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async with launched(tmp_path, kora_capture) as (runtime, ctx, root):
        workload = command_workload(
            runtime, "attempt-1", (sys.executable, "-I", "-c", "pass")
        )
        journal = attempt_directory(root, runtime, "attempt-1") / "journal.jsonl"
        handle = await runtime.submit(workload, ctx)

        await runtime.start(handle, ctx)
        await until(lambda: record(journal, "command_exited") is not None)

    assert kinds(journal)[:2] == ["runner.candidate_started", "command_exited"]
    assert record(journal, "runner.workspace_prepared") is None


@pytest.mark.asyncio
async def test_checked_out_attempt_skips_output_snapshot(
    tmp_path: Path, kora_capture: Capture
) -> None:
    gate = tmp_path / "gate"
    gate.touch()
    async with launched(tmp_path, kora_capture) as (runtime, ctx, root):
        workload = command_workload(
            runtime,
            "attempt-1",
            (sys.executable, "-I", "-c", "pass"),
            checkout=step_plan(gated_script(gate, FAKE_COMMIT, files=1001)),
            checkout_request=request("/repository", FAKE_COMMIT),
        )
        attempt = attempt_directory(root, runtime, "attempt-1")
        journal = attempt / "journal.jsonl"
        log = attempt / "runner.log"
        handle = await runtime.submit(workload, ctx)
        await until(lambda: settled(journal) == ["prepared"])
        await runtime.start(handle, ctx)
        await until(lambda: runtime_outcomes(log, "runtime.local.command_stop") != [])

        refused = await protocol_error(drain(runtime.transfer_out(handle, "a", ctx)))
        stops = runtime_outcomes(log, "runtime.local.command_stop")
        if stops == ["confirmed"]:
            await until(lambda: len(runtime_outcomes(log, CAPTURE_EVENT)) == 2)
        captures = runtime_outcomes(log, CAPTURE_EVENT)

    assert refused.code == "transfer_refused"
    assert record(journal, "command_exited") == {"exit_code": 0}
    assert stops == (["confirmed"] if sys.platform == "linux" else stops)
    assert captures == (["complete"] * 2 if stops == ["confirmed"] else captures)
    assert "runner.outputs_sealed" not in kinds(journal)
    assert runtime_outcomes(log, "runtime.local.seal") == []
    assert not (attempt / "output-snapshot").exists()


async def drain(chunks: AsyncIterator[bytes]) -> None:
    async for _ in chunks:
        pass


@pytest.mark.asyncio
async def test_checkout_step_exit_survives_the_reaper(
    tmp_path: Path, kora_capture: Capture
) -> None:
    gate = tmp_path / "gate"
    gate.touch()
    async with launched(tmp_path, kora_capture) as (runtime, ctx, root):
        workload = command_workload(
            runtime,
            "attempt-1",
            (sys.executable, "-I", "-c", "pass"),
            checkout=step_plan(gated_script(gate, FAKE_COMMIT, hold=3.5)),
            checkout_request=request("/repository", FAKE_COMMIT),
        )
        journal = attempt_directory(root, runtime, "attempt-1") / "journal.jsonl"

        await runtime.submit(workload, ctx)
        await until(
            lambda: (
                record(journal, "runner.workspace_prepared") is not None
                or record(journal, "runner.workspace_failed") is not None
            )
        )

    assert record(journal, "runner.workspace_failed") is None
    prepared = record(journal, "runner.workspace_prepared")
    assert prepared is not None
    assert prepared["commit"] == FAKE_COMMIT


@pytest.mark.asyncio
async def test_workload_session_seam_keeps_close_order(
    tmp_path: Path, kora_capture: Capture
) -> None:
    attempt = private_directory(tmp_path.resolve(), "attempt")
    directory = open_local_directory(attempt)
    marker = tmp_path / "leader.pid"
    session = ReapedSession(
        directory,
        str(attempt / "candidate"),
        SecretScope(kora_capture.diagnostics),
        SystemClock(),
        kora_capture.diagnostics.logger(COMPONENT),
    )
    submitter = Submitter(dispatcher_id="test", process=identity_of(os.getpid()))
    script = (
        f"import os, time\nopen({str(marker)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(60)"
    )
    leader: int | None = None
    try:
        async with launched(tmp_path, kora_capture) as (runtime, _ctx, _root):
            workload = command_workload(
                runtime, "attempt-1", (sys.executable, "-I", "-c", script)
            )
            session.accept(submitter, None, workload)
            await asyncio.to_thread(
                session.supervise, submitter, False, workload, lambda: None
            )
            session.attach_drains()
            await until(lambda: marker.exists() and marker.read_text() != "")
            leader = int(marker.read_text())

            session.fence()
            session.close_journal()
            await session.stop()
            alive_after_stop = not pid_gone(leader)
            recorded = kinds(attempt / "journal.jsonl")
    finally:
        if leader is not None and not pid_gone(leader):
            os.kill(leader, signal.SIGKILL)
        await session.stop()
        directory.close()

    assert alive_after_stop
    assert recorded == ["runner.candidate_started"]


@pytest.mark.asyncio
async def test_command_workspace_releases_resources(
    tmp_path: Path, kora_capture: Capture
) -> None:
    attempt = private_directory(tmp_path.resolve(), "attempt")
    directory = open_local_directory(attempt)
    session = ReapedSession(
        directory,
        str(attempt / "candidate"),
        SecretScope(kora_capture.diagnostics),
        SystemClock(),
        kora_capture.diagnostics.logger(COMPONENT),
    )
    submitter = Submitter(dispatcher_id="test", process=identity_of(os.getpid()))
    step_pid = attempt / "candidate" / "step.pid"
    try:
        async with launched(tmp_path, kora_capture) as (runtime, _ctx, _root):
            workload = command_workload(
                runtime,
                "attempt-1",
                (sys.executable, "-I", "-c", "pass"),
                checkout=step_plan(gated_script(tmp_path / "never", FAKE_COMMIT)),
                checkout_request=request("/repository", FAKE_COMMIT),
            )
            baseline = open_descriptors()
            session.accept(submitter, None, workload)
            await until(lambda: step_pid.exists() and step_pid.read_text() != "")
            step = int(step_pid.read_text())

            session.fence()
            session.close_journal()
            await session.stop()
            recorded = kinds(attempt / "journal.jsonl")
            remaining = open_descriptors()
    finally:
        await session.stop()
        directory.close()

    assert remaining == baseline
    assert pid_gone(step)
    assert "runner.workspace_prepared" not in recorded
    assert list((attempt / "control").iterdir()) == []


ENVIRONMENT_STEP = "open('environment', 'w').write('\\n'.join(sorted(os.environ)))"


@pytest.mark.asyncio
async def test_checkout_step_receives_only_the_plan_environment(
    tmp_path: Path, kora_capture: Capture
) -> None:
    gate = tmp_path / "gate"
    gate.touch()
    script = f"import os\n{ENVIRONMENT_STEP}\n" + gated_script(gate, FAKE_COMMIT)
    async with launched(tmp_path, kora_capture) as (runtime, ctx, root):
        workload = command_workload(
            runtime,
            "attempt-1",
            (sys.executable, "-I", "-c", "pass"),
            checkout=step_plan(script, env={"KORA_PLAN_MARKER": "1"}),
            checkout_request=request("/repository", FAKE_COMMIT),
        )
        attempt = attempt_directory(root, runtime, "attempt-1")
        journal = attempt / "journal.jsonl"

        await runtime.submit(workload, ctx)
        await until(lambda: settled(journal) == ["prepared"])

    names = set((attempt / "candidate" / "environment").read_text().split())
    assert "KORA_PLAN_MARKER" in names
    assert names.isdisjoint({"PATH", "HOME", "KORA_ATTEMPT_TOKEN", "TMPDIR"})


def test_reaper_skips_registered_children_and_reaps_the_rest() -> None:
    reaper = AdoptedReaper(grace_ns=0, interval=60)
    registered = spawn_exiting()
    unregistered = spawn_exiting()
    reaper.register(registered)
    exited = wait_zombies((registered, unregistered))

    reaper.start()
    scans = reaper.scans
    reaper.wake()
    scanned = reaper.wait_scans(scans + 1, 10 if sys.platform == "linux" else 0)
    asyncio.run(reaper.stop())

    collected = os.waitpid(registered, 0)[0] == registered
    stolen = waited_elsewhere(unregistered)
    assert exited
    assert collected
    assert scanned is (sys.platform == "linux")
    assert stolen is (sys.platform == "linux")


def spawn_exiting() -> int:
    return os.posix_spawn(
        sys.executable, [sys.executable, "-I", "-c", "pass"], dict(os.environ)
    )


def wait_zombies(pids: tuple[int, ...]) -> bool:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if all(is_zombie(pid) for pid in pids):
            return True
        time.sleep(0.01)
    return False


def is_zombie(pid: int) -> bool:
    if sys.platform == "linux":
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return fields[0] == "Z"
    listed = subprocess.run(
        ["/bin/ps", "-o", "stat=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    )
    return listed.stdout.strip().startswith("Z")


def waited_elsewhere(pid: int) -> bool:
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        return True
    return False


class OrderProbe(ReapedSession):
    calls: list[str] = []

    def fence(self) -> None:
        self.calls.append("fence")
        super().fence()

    def close_journal(self) -> None:
        self.calls.append("close_journal")
        super().close_journal()

    async def stop(self) -> None:
        self.calls.append("stop")
        await super().stop()


@pytest.mark.asyncio
async def test_control_server_fences_before_closing_the_journal(
    tmp_path: Path, kora_capture: Capture
) -> None:
    attempt = private_directory(tmp_path.resolve(), "attempt")
    directory = open_local_directory(attempt)
    probe = OrderProbe(
        directory,
        str(attempt / "candidate"),
        SecretScope(kora_capture.diagnostics),
        SystemClock(),
        kora_capture.diagnostics.logger(COMPONENT),
    )
    probe.calls = []
    try:
        async with delivery_server(tmp_path, kora_capture, command=probe) as live:
            channel = await live.attach()
            try:
                await live.deliver(channel)
            finally:
                channel.close()
    finally:
        directory.close()

    assert probe.calls[:3] == ["fence", "close_journal", "stop"]


@pytest.mark.asyncio
async def test_leader_exit_after_fence_still_gets_its_domain_stop(
    tmp_path: Path, kora_capture: Capture
) -> None:
    attempt = private_directory(tmp_path.resolve(), "attempt")
    directory = open_local_directory(attempt)
    gate = tmp_path / "exit-gate"
    session = ReapedSession(
        directory,
        str(attempt / "candidate"),
        SecretScope(kora_capture.diagnostics),
        SystemClock(),
        kora_capture.diagnostics.logger(COMPONENT),
    )
    submitter = Submitter(dispatcher_id="test", process=identity_of(os.getpid()))
    script = (
        f"import os, time\nwhile not os.path.exists({str(gate)!r}): time.sleep(0.01)"
    )
    try:
        async with launched(tmp_path, kora_capture) as (runtime, _ctx, _root):
            workload = command_workload(
                runtime, "attempt-1", (sys.executable, "-I", "-c", script)
            )
            session.accept(submitter, None, workload)
            await asyncio.to_thread(
                session.supervise, submitter, False, workload, lambda: None
            )
            session.attach_drains()

            session.fence()
            gate.touch()
            await until(lambda: session.completion() is not Completion.PENDING)
            recorded = kinds(attempt / "journal.jsonl")
    finally:
        await session.stop()
        directory.close()

    assert recorded == ["runner.candidate_started"]
