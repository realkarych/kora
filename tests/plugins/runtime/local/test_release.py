import asyncio
import json
import os
import shlex
import subprocess
import sys
from collections.abc import AsyncGenerator, Awaitable
from datetime import timedelta
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import context_for, kora_capture
from kora.plugins.runtime.local.completed import recorded_exited
from kora.plugins.runtime.local.runtime import LocalRuntime
from kora.protocols.common import ProtocolContext
from kora.protocols.errors import KoraProtocolError
from kora.protocols.identity import Handle
from kora.protocols.runtime import TransferItem
from kora.runner.filesystem import (
    RELEASE_DEPTH,
    DirectoryAnchor,
    ReleaseProgress,
    ReleaseState,
    open_local_directory,
    release_tree,
)
from kora.runner.identity import NativeIdentityReader
from kora.runner.registry import Completed, Entry, LockOutcome, Running, Transition
from tests.plugins.runtime.local.test_command import (
    attempt_directory,
    command_workload,
    launched,
)
from tests.plugins.runtime.local.test_completion import (
    completed_entry,
    quiesced,
    started_legacy,
)
from tests.support.local_peers import identity_of as process_identity
from tests.support.local_peers import until
from tests.support.local_runners import open_spawning
from tests.support.local_runtime import (
    open_descriptors,
    prepared,
    private_directory,
    protocol_error,
)

__all__ = ["kora_capture"]

WRITER = "\n".join(
    (
        "import os",
        "os.makedirs('tree/deep')",
        "open('tree/deep/file', 'w').write('content')",
        "os.write(1, b'out')",
    )
)
GATED = "\n".join(
    (
        "import os, time",
        "while not os.path.exists('go'): time.sleep(0.01)",
    )
)
RELEASED_NAMES = frozenset(
    {
        "journal.jsonl",
        "stdout.log",
        "stderr.log",
        "runner.log",
        "candidate",
        "output-snapshot",
    }
)
NAMESPACED_RELEASE = "\n".join(
    (
        "import json, os, sys",
        "from kora.runner.filesystem import DirectoryAnchor, identity_of",
        "from kora.runner.filesystem import release_tree",
        "descriptor = os.open(sys.argv[1], os.O_RDONLY | os.O_DIRECTORY)",
        "parent = DirectoryAnchor(descriptor, identity_of(descriptor, 'parent'))",
        "progress = release_tree(parent, 'tree')",
        "kept = open(os.path.join(sys.argv[1], 'tree', 'mnt', 'kept')).read()",
        "print(json.dumps([progress.state.value, progress.foreign, kept]))",
    )
)


async def completed_attempt(
    runtime: LocalRuntime, ctx: ProtocolContext, script: str = WRITER
) -> tuple[Handle, str]:
    handle = await started_legacy(runtime, ctx, script)
    running = runtime.registry.entry(runtime.registry.key("attempt-1"))
    assert isinstance(running, Running)
    await quiesced(runtime, handle, ctx)
    runner = completed_entry(runtime).runner
    await until(lambda: recorded_exited(NativeIdentityReader(), runner))
    return handle, running.socket_path


async def released_after_partials(
    runtime: LocalRuntime, handle: Handle, ctx: ProtocolContext
) -> list[KoraProtocolError]:
    partials: list[KoraProtocolError] = []
    while True:
        try:
            await runtime.release(handle, ctx)
        except KoraProtocolError as error:
            if error.code != "release_partial" or len(partials) > 64:
                raise
            partials.append(error)
        else:
            return partials


async def answer_code(outcome: Awaitable[object]) -> str:
    try:
        await outcome
    except KoraProtocolError as error:
        return error.code
    return "answered"


async def read_code(chunks: AsyncGenerator[bytes]) -> str:
    try:
        async for _chunk in chunks:
            pass
    except KoraProtocolError as error:
        return error.code
    return "answered"


async def no_chunks() -> AsyncGenerator[bytes]:
    for chunk in ():
        yield chunk


def release_outcomes(capture: Capture) -> list[object]:
    return [
        record.get("outcome")
        for record in capture.reader.records()
        if record.get("event") == "runtime.local.release"
    ]


def with_runner(runtime: LocalRuntime, pid: int) -> None:
    key = runtime.registry.key("attempt-1")
    nameless = process_identity(pid).model_copy(
        update={"host_id": None, "boot_id": None, "pid_namespace": None}
    )

    def rewrite(entry: Entry | None) -> Transition:
        assert isinstance(entry, Completed)
        return Transition(entry.model_copy(update={"runner": nameless}))

    applied = runtime.registry.try_transition(key, rewrite)
    assert not isinstance(applied, LockOutcome)


def walked_parent(tmp_path: Path) -> tuple[DirectoryAnchor, Path, Path]:
    parent = private_directory(tmp_path, "parent")
    tree = private_directory(parent, "tree")
    outside = private_directory(tmp_path, "outside")
    return open_local_directory(parent), tree, outside


def planted(leg: str, tree: Path, outside: Path) -> Path:
    if leg == "symlink":
        target = private_directory(outside, "target")
        (target / "content").write_text("kept")
        (tree / "link").symlink_to(target)
        return target / "content"
    if leg == "hard_link":
        target = outside / "file"
        target.write_text("kept")
        os.link(target, tree / "alias")
        return target
    if leg == "fifo":
        os.mkfifo(tree / "pipe", 0o600)
        return outside
    frozen = private_directory(tree, "snapshot")
    nested = private_directory(frozen, "nested")
    (nested / "file").write_text("frozen")
    (nested / "file").chmod(0o400)
    nested.chmod(0o500)
    frozen.chmod(0o500)
    readonly = private_directory(tree, "readonly")
    (readonly / "file").write_text("readonly")
    readonly.chmod(0o500)
    return outside


def namespaced_release(tmp_path: Path) -> tuple[str, int, str]:
    prepared("user-namespaces")
    parent = private_directory(tmp_path, "namespaced")
    tree = private_directory(parent, "tree")
    private_directory(tree, "mnt")
    (tree / "file").write_text("removed")
    script = " && ".join(
        (
            f"mount -t tmpfs kora {shlex.quote(str(tree / 'mnt'))}",
            f"echo kept > {shlex.quote(str(tree / 'mnt' / 'kept'))}",
            shlex.join((sys.executable, "-I", "-c", NAMESPACED_RELEASE, str(parent))),
        )
    )
    finished = subprocess.run(
        [
            "/usr/bin/unshare",
            "--user",
            "--map-root-user",
            "--mount",
            "/bin/sh",
            "-c",
            script,
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    state, foreign, kept = json.loads(finished.stdout)
    assert not (tree / "file").exists()
    return state, foreign, kept.strip()


@pytest.mark.asyncio
async def test_completed_command_attempt_is_released(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async with launched(tmp_path, kora_capture) as (runtime, ctx, root):
        attempt = attempt_directory(root, runtime, "attempt-1")
        handle, socket = await completed_attempt(runtime, ctx)
        before = frozenset(os.listdir(attempt))
        socket_left = os.path.lexists(socket)

        await runtime.release(handle, ctx)
        entry = completed_entry(runtime)
        registry = frozenset(os.listdir(root / "registry"))
        name = runtime.registry.key("attempt-1").name
        await until(lambda: "released" in release_outcomes(kora_capture))
        outcomes = release_outcomes(kora_capture)

    assert before >= RELEASED_NAMES
    assert not socket_left
    assert not attempt.exists()
    assert entry.released_at is not None
    assert {f"{name}.json", f"{name}.lock"} <= registry
    assert outcomes == ["released"]


@pytest.mark.asyncio
async def test_release_refuses_unfinished_attempts(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async with launched(private_directory(tmp_path, "running"), kora_capture) as (
        runtime,
        ctx,
        _root,
    ):
        unsubmitted = await protocol_error(
            runtime.release(runtime.registry.key("attempt-1").handle(), ctx)
        )
        await runtime.retire("attempt-2", timedelta(seconds=1), ctx)
        retired = await protocol_error(
            runtime.release(runtime.registry.key("attempt-2").handle(), ctx)
        )
        handle = await started_legacy(runtime, ctx, GATED)
        running = await protocol_error(runtime.release(handle, ctx))
    async with launched(private_directory(tmp_path, "completed"), kora_capture) as (
        runtime,
        ctx,
        root,
    ):
        handle, _socket = await completed_attempt(runtime, ctx)
        holder = subprocess.Popen(
            (sys.executable, "-I", "-c", "import time; time.sleep(60)")
        )
        try:
            with_runner(runtime, holder.pid)
            alive = await protocol_error(runtime.release(handle, ctx))
            attempt_kept = attempt_directory(root, runtime, "attempt-1").exists()
        finally:
            holder.kill()
            holder.wait()
        locked = private_directory(
            attempt_directory(root, runtime, "attempt-1") / "candidate", "locked"
        )
        locked.chmod(0)
        try:
            foreign = await protocol_error(runtime.release(handle, ctx))
            foreign_released_at = completed_entry(runtime).released_at
        finally:
            locked.chmod(0o700)
        await until(lambda: "foreign_entry" in release_outcomes(kora_capture))
        outcomes = release_outcomes(kora_capture)
        await runtime.release(handle, ctx)
        released_at = completed_entry(runtime).released_at
        await runtime.release(handle, ctx)
        repeated_at = completed_entry(runtime).released_at
        workload = command_workload(
            runtime, "attempt-1", (sys.executable, "-I", "-c", WRITER)
        )
        item = TransferItem(
            name="input.txt", kind="input", size=0, sha256="sha256:" + "0" * 64
        )
        after = {
            "command_observation": await answer_code(
                runtime.command_observation(handle, ctx)
            ),
            "read_log": await read_code(runtime.read_log(handle, "stdout", 0, ctx)),
            "transfer_out": await read_code(
                runtime.transfer_out(handle, "result.txt", ctx)
            ),
            "start": await answer_code(runtime.start(handle, ctx)),
            "transfer_in": await answer_code(
                runtime.transfer_in(handle, item, no_chunks(), ctx)
            ),
            "submit": await answer_code(runtime.submit(workload, ctx)),
            "retire": await answer_code(
                runtime.retire("attempt-1", timedelta(seconds=1), ctx)
            ),
        }
        quiescence = await runtime.quiescence(handle, ctx)
        status = await runtime.status(handle, ctx)
        found = await runtime.lookup("attempt-1", ctx)

    assert (unsubmitted.code, unsubmitted.retryable) == ("attempt_not_submitted", False)
    assert (retired.code, retired.retryable) == ("attempt_retired", False)
    assert (running.code, running.retryable) == ("attempt_running", True)
    assert (alive.code, alive.retryable) == ("runner_unreachable", True)
    assert attempt_kept
    assert (foreign.code, foreign.retryable) == ("release_foreign_entry", False)
    assert foreign.details == {"foreign": 1}
    assert foreign_released_at is None
    assert outcomes[-2:] == ["refused", "foreign_entry"]
    assert released_at is not None
    assert repeated_at == released_at
    assert after == dict.fromkeys(after, "attempt_retired")
    assert quiescence.kind == "normal_completion"
    assert (status.state, status.phase) == ("exited", "complete")
    assert found == handle


@pytest.mark.parametrize(
    "leg",
    [
        "symlink",
        "hard_link",
        "fifo",
        "read_only",
        *(
            [pytest.param("foreign_device", marks=pytest.mark.host_fixture)]
            if sys.platform == "linux"
            else []
        ),
    ],
)
def test_release_never_leaves_the_attempt(tmp_path: Path, leg: str) -> None:
    if leg == "foreign_device":
        outcome = namespaced_release(tmp_path)

        assert outcome == (ReleaseState.FOREIGN.value, 1, "kept")
        return
    parent, tree, outside = walked_parent(tmp_path)
    survivor = planted(leg, tree, outside)

    try:
        progress = release_tree(parent, "tree")
    finally:
        parent.close()

    assert progress.state is ReleaseState.COMPLETE
    assert not tree.exists()
    assert survivor.exists()
    assert leg != "hard_link" or os.stat(survivor).st_nlink == 1


@pytest.mark.asyncio
async def test_release_is_resumable_past_its_bound(
    tmp_path: Path, kora_capture: Capture
) -> None:
    async with launched(tmp_path, kora_capture, release_entries=80) as (
        runtime,
        ctx,
        root,
    ):
        handle, _socket = await completed_attempt(runtime, ctx)
        attempt = attempt_directory(root, runtime, "attempt-1")
        for index in range(300):
            (attempt / "candidate" / f"extra-{index}").write_text("x")

        partials = await released_after_partials(runtime, handle, ctx)
        entry = completed_entry(runtime)

    assert len(partials) >= 3
    assert all(error.retryable for error in partials)
    assert all(int(str(error.details["removed"])) > 0 for error in partials)
    assert not attempt.exists()
    assert entry.released_at is not None


def test_release_walk_bounds_each_pass(tmp_path: Path) -> None:
    parent, tree, _outside = walked_parent(tmp_path)
    for index in range(10):
        (tree / f"file-{index}").write_text("x")
    progresses: list[ReleaseProgress] = []

    try:
        while not progresses or progresses[-1].state is ReleaseState.PARTIAL:
            progresses.append(release_tree(parent, "tree", max_entries=4, max_depth=2))
    finally:
        parent.close()

    assert [progress.state for progress in progresses[:-1]] == [
        ReleaseState.PARTIAL
    ] * (len(progresses) - 1)
    assert progresses[-1].state is ReleaseState.COMPLETE
    assert sum(progress.removed for progress in progresses) == 11
    assert all(progress.removed <= 4 for progress in progresses)
    assert not tree.exists()


@pytest.mark.parametrize("shape", ["too_deep", "no_progress"])
def test_release_walk_ends_when_only_left_entries_remain(
    tmp_path: Path, shape: str
) -> None:
    parent, tree, _outside = walked_parent(tmp_path)
    locked: list[Path] = []
    if shape == "too_deep":
        (private_directory(private_directory(tree, "a"), "b") / "file").write_text("x")
    else:
        for index in range(6):
            locked.append(private_directory(tree, f"locked-{index}"))
            locked[-1].chmod(0)

    try:
        progress = release_tree(parent, "tree", max_entries=4, max_depth=2)
    finally:
        parent.close()
        for directory in locked:
            directory.chmod(0o700)

    assert progress.state is ReleaseState.FOREIGN
    assert progress.removed == 0
    assert progress.foreign == (1 if shape == "too_deep" else 3)
    assert tree.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["success", "refusal", "cancellation", "shutdown"])
async def test_release_frees_resources(
    tmp_path: Path, kora_capture: Capture, case: str
) -> None:
    descriptors = open_descriptors()
    pending: list[asyncio.Task[None]] = []
    async with launched(tmp_path, kora_capture) as (runtime, ctx, _root):
        executor = runtime.releases
        if case == "refusal":
            handle = await started_legacy(runtime, ctx, GATED)
            outcome = (await protocol_error(runtime.release(handle, ctx))).code
        else:
            handle, _socket = await completed_attempt(runtime, ctx)
            if case == "success":
                await runtime.release(handle, ctx)
                outcome = "released"
            else:
                pending.append(asyncio.create_task(runtime.release(handle, ctx)))
                if case == "cancellation":
                    pending[0].cancel()
                outcome = case
    settled = await asyncio.gather(*pending, return_exceptions=True)
    await until(lambda: open_descriptors() <= descriptors)

    expected = {
        "success": "released",
        "refusal": "attempt_running",
        "cancellation": "cancellation",
        "shutdown": "shutdown",
    }
    assert outcome == expected[case]
    assert executor.joined
    if case == "cancellation":
        assert isinstance(settled[0], asyncio.CancelledError)
    if case == "shutdown":
        assert settled[0] is None or (
            isinstance(settled[0], KoraProtocolError)
            and settled[0].code == "runtime_closed"
        )
    assert open_descriptors() <= descriptors


@pytest.mark.asyncio
async def test_release_refuses_a_bound_within_its_depth(
    tmp_path: Path, kora_capture: Capture
) -> None:
    parent, tree, _outside = walked_parent(tmp_path)
    root = private_directory(tmp_path, "root")

    try:
        with pytest.raises(ValueError, match="depth bound"):
            release_tree(parent, "tree", max_entries=2, max_depth=2)
    finally:
        parent.close()
    with pytest.raises(ValueError, match="depth bound"):
        await open_spawning(
            root,
            context_for(kora_capture),
            tmp_path / "sockets",
            release_entries=RELEASE_DEPTH,
        )

    assert tree.exists()
    assert list(root.iterdir()) == []
    assert not (tmp_path / "sockets").exists()
