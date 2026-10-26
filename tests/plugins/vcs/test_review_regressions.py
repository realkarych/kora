import asyncio
import os
import shutil
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.vcs import Kept, pinned, publish_request, run
from kora.executor.failures import ExecutorError, Phase
from kora.executor.guard import PhaseGuard
from kora.executor.launcher import (
    Ending,
    LaunchSpec,
    ProcessLauncher,
    StepLauncher,
    StepOutcome,
)
from kora.executor.protected import FuelBounds, check_protected, compiled_globs
from kora.ir import VcsRef
from kora.ir.node import ErrorClass
from kora.plugins.vcs.git import GitVcs
from kora.plugins.vcs.git.plans import DECODER_HELPER, TREE_HELPER
from kora.protocols.common import ProtocolContext
from kora.protocols.deadlines import LocalThresholds
from kora.protocols.time import SystemClock
from kora.protocols.vcs import (
    CheckoutRequest,
    PlanFile,
    PlanStep,
    PublishPlan,
    PublishRequest,
)
from tests.plugins.vcs.git_support import REPO, git, make_world, write
from tests.plugins.vcs.launchers import is_push

TOKEN_NAME: str = "ghp_" + "a" * 36
FAST: ProcessLauncher = ProcessLauncher(
    kill_grace_ns=500_000_000, eof_bound_ns=500_000_000
)


def test_a_secret_in_a_path_name_is_found_when_its_blob_is_reused(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    checkout = world.checkout()
    candidate = world.candidate(checkout)
    shutil.copyfile(candidate / "a.txt", candidate / TOKEN_NAME)

    with pytest.raises(ExecutorError) as raised:
        world.prepare(checkout)

    assert raised.value.failure_class is ErrorClass.CREDENTIAL_LEAK


def fake_git(root: Path) -> tuple[Path, Path]:
    pids = root / "pids"
    pids.mkdir()
    script = root / "fake-git"
    script.write_text(f"#!/bin/sh\necho $$ > {pids}/$$\nexec sleep 30\n")
    script.chmod(0o755)
    return script, pids


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def started_children(pids: Path) -> list[int]:
    deadline = time.monotonic() + 10
    while not any(pids.iterdir()) and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    return [int(path.name) for path in pids.iterdir()]


@pytest.mark.parametrize(
    "arguments",
    [
        (TREE_HELPER, "import", "--repo", "repo", "--input", ".", "--base", "a" * 40),
        (
            DECODER_HELPER,
            "paths",
            "--repo",
            "repo",
            "--base",
            "a" * 40,
            "--prepared",
            "b" * 40,
        ),
    ],
)
def test_helper_git_children_end_with_the_step(
    tmp_path: Path, arguments: tuple[str, ...]
) -> None:
    script, pids = fake_git(tmp_path)
    (tmp_path / "head.json").write_text('{"head": "' + "a" * 40 + '", "bundle": false}')
    helper, *rest = arguments
    spec = LaunchSpec(
        argv=(sys.executable, "-I", helper, "--git", str(script), *rest),
        cwd=tmp_path,
        env={},
        stdin=None,
        timeout_ns=60_000_000_000,
        stdout_limit=4096,
        stderr_limit=4096,
    )

    async def interrupted() -> list[int]:
        task = asyncio.create_task(FAST.run(spec))
        children = await started_children(pids)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return children

    children = asyncio.run(interrupted())

    assert children
    assert not any(alive(pid) for pid in children)


def test_remote_substitution_before_preparation_is_refused(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    checkout = world.checkout()
    write(world.candidate(checkout), {"moved.txt": "moved\n"})
    context = world.context()
    remote = world.remote_fixture()
    plan = run(world.provider.publish_plan(publish_request(remote, checkout), context))
    workspace = checkout.workspace()
    exported = run(world.executor.export(plan, workspace, checkout.areas, context))
    sealed = run(world.executor.seal(exported, workspace, checkout.areas, context))
    copied = tmp_path / "copied.git"
    shutil.copytree(world.remote, copied, symlinks=True)
    shutil.rmtree(world.remote)
    copied.rename(world.remote)
    with pytest.raises(ExecutorError) as raised:
        run(
            world.executor.prepare_and_inspect(
                plan,
                sealed,
                workspace,
                publish_request(remote, checkout),
                pinned(world.binding()),
                checkout.areas,
                (),
                (),
                Kept(),
                context,
            )
        )

    assert raised.value.code == "destination_changed"


class ControlFileVcs(GitVcs):
    async def publish_plan(
        self, request: PublishRequest, ctx: ProtocolContext
    ) -> PublishPlan:
        plan = await super().publish_plan(request, ctx)
        marker = PlanFile(area="control", path="needed", content=b"x", mode=0o600)
        reader = PlanStep(
            argv=("python", "-I", "-c", "open('{control_root}/needed').read()"),
            cwd="control",
            env={},
            stdin=None,
            timeout=plan.publish[0].timeout,
            capture=None,
            failure_class="infrastructure",
        )
        return PublishPlan.model_validate(
            {
                **plan.model_dump(),
                "files": (marker,),
                "inspect": (*plan.inspect, reader),
                "publish": (reader, *plan.publish),
            }
        )


def test_control_files_reach_inspect_and_publish(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture, provider_type=ControlFileVcs)
    checkout = world.checkout()
    write(world.candidate(checkout), {"files.txt": "files\n"})

    prepared = world.prepare(checkout)
    outcome = world.publish(prepared)

    assert outcome.state == "pushed"


@dataclass
class SignalledPushLauncher(StepLauncher):
    inner: StepLauncher = field(default_factory=ProcessLauncher)

    async def run(self, spec: LaunchSpec) -> StepOutcome:
        outcome = await self.inner.run(spec)
        if is_push(spec):
            return replace(outcome, ending=Ending.EXITED, exit_code=-9)
        return outcome


def test_a_push_killed_by_a_signal_is_unknown(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture, launcher=SignalledPushLauncher())
    git(
        world.home,
        f"--git-dir={world.remote}",
        "update-ref",
        "refs/heads/kora/graph/run-1/agent/1",
        world.base,
    )
    checkout = world.checkout()
    write(world.candidate(checkout), {"killed.txt": "killed\n"})
    prepared = world.prepare(checkout)

    outcome = world.publish(prepared)

    assert outcome.state == "unknown"


def test_a_due_interruption_stops_the_protected_path_check(
    kora_capture: Capture,
) -> None:
    glob = "**/" + "/".join(["*x*x*"] * 13 + ["*y"])
    globs = compiled_globs(
        tuple(f"{glob}/z{index}" for index in range(48)), Phase.INSPECT
    )
    stem = "/".join(["x" * 9] * 39)
    paths = [f"{stem}/xxxxx{index:04d}" for index in range(1100)]
    clock = SystemClock()
    now = clock.monotonic_ns()
    guard = PhaseGuard(clock, LocalThresholds(now + 300_000_000, now + 300_000_000))
    started = time.monotonic()

    with pytest.raises(ExecutorError) as raised:
        asyncio.run(
            check_protected(
                globs,
                paths,
                FuelBounds(),
                Phase.INSPECT,
                kora_capture.diagnostics.logger("conformance"),
                guard,
            )
        )

    assert raised.value.failure_class is ErrorClass.TIMEOUT
    assert time.monotonic() - started < 3


def test_publication_succeeds_after_the_base_leaves_every_remote_ref(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    checkout = world.checkout()
    git(world.home, f"--git-dir={world.remote}", "update-ref", "-d", "refs/heads/main")
    write(world.candidate(checkout), {"orphaned.txt": "orphaned\n"})
    prepared = world.prepare(checkout)

    outcome = world.publish(prepared)

    assert outcome.state == "pushed"


def test_a_vcs_ref_checkout_with_a_missing_base_is_config_error(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    checkout = world.checkout()
    write(world.candidate(checkout), {"based.txt": "based\n"})
    outcome = world.publish(world.prepare(checkout))
    assert outcome.ref is not None
    baseless = VcsRef.model_validate(
        {
            **outcome.ref.model_dump(mode="json", exclude_unset=True),
            "base_commit": "1" * 40,
        }
    )
    areas = world.bench().areas()
    request = CheckoutRequest(
        repo=REPO,
        rev=baseless,
        root=str(areas.candidate),
        attempt_id="gate-attempt",
        branch="gate",
    )
    plan = run(world.provider.checkout_plan(request, world.context()))

    with pytest.raises(ExecutorError) as raised:
        run(
            world.executor.prepare_workspace(
                plan, request, areas, (), False, world.context()
            )
        )

    assert raised.value.failure_class is ErrorClass.CONFIG_ERROR


def test_a_missing_remote_is_retryable_and_reconciles_unreachable(
    tmp_path: Path, kora_capture: Capture
) -> None:
    world = make_world(tmp_path, kora_capture)
    checkout = world.checkout()
    write(world.candidate(checkout), {"missing.txt": "missing\n"})
    prepared = world.prepare(checkout)
    moved = tmp_path / "away.git"
    world.remote.rename(moved)

    try:
        state = run(world.executor.reconcile(prepared.result, world.context()))
        with pytest.raises(ExecutorError) as raised:
            world.publish(prepared)
    finally:
        moved.rename(world.remote)

    assert state.value == "unreachable"
    assert raised.value.failure_class is ErrorClass.INFRASTRUCTURE
