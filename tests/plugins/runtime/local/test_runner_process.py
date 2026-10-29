import json
import os
import socket
import stat
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import kora_capture
from kora.plugins.runtime.local.spawn import (
    RUNNER_MODULE,
    runner_environment,
)
from kora.plugins.runtime.local.supervisor import EXIT_REFUSED
from kora.runner.registry import Fence, Running
from tests.support.local_runners import (
    open_spawning,
    own_identity,
    reap_runners,
    runner_argv,
    short_directory,
    starting_entry,
    submitted,
    supervised,
    written,
)
from tests.support.local_runtime import (
    PATH_MARKER,
    recorded,
    runtime_context,
    state_root,
)

__all__ = ["kora_capture"]

NONCE: Final[str] = "c" * 32
SYSTEM_SUPPLIED: Final[frozenset[str]] = frozenset({"__CF_USER_TEXT_ENCODING"})
MARKER_ATTEMPT: Final[str] = "attempt-diagnostics-marker-7d1f"
PROBE: Final[str] = "\n".join(
    [
        "import json, os, stat, sys",
        "found = {}",
        "for descriptor in range(256):",
        "    try:",
        "        metadata = os.fstat(descriptor)",
        "    except OSError:",
        "        continue",
        "    found[descriptor] = [",
        "        stat.S_IFMT(metadata.st_mode),",
        "        metadata.st_dev,",
        "        metadata.st_ino,",
        "        metadata.st_rdev,",
        "    ]",
        "document = {'environment': dict(os.environ), 'descriptors': found}",
        "with open(os.path.join(sys.argv[1], 'probe.json'), 'w') as probe:",
        "    json.dump(document, probe)",
        f"module = {RUNNER_MODULE!r}",
        "os.execv(sys.executable, [sys.executable, '-I', '-m', module, *sys.argv[1:]])",
    ]
)


@pytest.mark.asyncio
async def test_runner_environment_excludes_worker_credentials(
    tmp_path: Path, kora_capture: Capture
) -> None:
    root = state_root(tmp_path)
    ctx = runtime_context(kora_capture)
    held, peer = socket.socketpair()
    held.set_inheritable(True)
    lock = os.open(root, os.O_RDONLY)
    os.set_inheritable(lock, True)
    try:
        with short_directory() as base:
            runtime = await open_spawning(
                root, ctx, base / "sockets", runner=(sys.executable, "-I", "-c", PROBE)
            )
            try:
                await runtime.submit(submitted(runtime, "attempt-1"), ctx)
                entry = await recorded(runtime, "attempt-1")
            finally:
                await runtime.close()
                reap_runners(root)
    finally:
        held.close()
        peer.close()
        os.close(lock)
    probe = json.loads((root / "probe.json").read_text())

    null = os.stat(os.devnull)
    null_key = [stat.S_IFCHR, null.st_dev, null.st_ino, null.st_rdev]
    assert isinstance(entry, Running)
    assert {
        name: value
        for name, value in probe["environment"].items()
        if name not in SYSTEM_SUPPLIED
    } == runner_environment(sys.platform, Path.home())
    assert sorted(probe["descriptors"]) == ["0", "1", "2", "3"]
    assert [probe["descriptors"][str(index)] for index in range(3)] == [null_key] * 3
    assert probe["descriptors"]["3"][0] == stat.S_IFIFO


@pytest.mark.asyncio
async def test_logging_redacts_content_and_safe_exception_chains(
    tmp_path: Path, kora_capture: Capture
) -> None:
    root = state_root(tmp_path)
    ctx = runtime_context(kora_capture)
    with short_directory() as base:
        sockets = base / "sockets"
        runtime = await open_spawning(root, ctx, sockets)
        fence = Fence(retirement_id="retirement-1", fenced_at=ctx.clock.now())
        domain = runtime.execution_domain_id
        try:
            await written(
                runtime, MARKER_ATTEMPT, starting_entry(sockets, NONCE, fence), ctx
            )
            name = runtime.registry.key(MARKER_ATTEMPT).name
            argv = runner_argv(runtime, root, MARKER_ATTEMPT, NONCE)
            outcome = await supervised(argv)
        finally:
            await runtime.close()
    log = (root / "attempts" / name / "runner.log").read_text()
    records = [json.loads(line) for line in log.splitlines()]

    identity = own_identity()
    events = [record["event"] for record in records]
    assert outcome.code == EXIT_REFUSED
    assert MARKER_ATTEMPT in log
    assert events == ["runtime.local.runner.started", "runtime.local.runner.refused"]
    for withheld in (
        str(root),
        PATH_MARKER,
        NONCE,
        domain,
        str(identity.host_id),
        str(Path.home()),
        *runner_environment(sys.platform, Path.home()).values(),
    ):
        assert withheld not in log


@pytest.mark.asyncio
async def test_serving_process_refuses_fake_providers(
    tmp_path: Path, kora_capture: Capture
) -> None:
    root = state_root(tmp_path)
    ctx = runtime_context(kora_capture)
    errors = tmp_path / "imports.txt"
    with short_directory() as base:
        sockets = base / "sockets"
        runtime = await open_spawning(root, ctx, sockets)
        fence = Fence(retirement_id="retirement-1", fenced_at=ctx.clock.now())
        domain = runtime.execution_domain_id
        try:
            await written(
                runtime, "attempt-1", starting_entry(sockets, NONCE, fence), ctx
            )
            command = (sys.executable, "-I", "-X", "importtime", "-m", RUNNER_MODULE)
            outcome = await supervised(
                [*command, str(root), "attempt-1", domain, NONCE], errors
            )
        finally:
            await runtime.close()
    imported = [
        line.rsplit("|", 1)[-1].strip()
        for line in errors.read_text().splitlines()
        if line.startswith("import time:") and "|" in line
    ]

    assert outcome.code == EXIT_REFUSED
    assert "kora.runner.registry" in imported
    assert not [
        module
        for module in imported
        if module.split(".")[0] == "tests" or module.startswith("kora.testing")
    ]


def test_dispatcher_becomes_its_group_leader() -> None:
    probe = "\n".join(
        [
            "import os",
            "from kora.plugins.runtime.local.spawn import ensure_dispatch_group_leader",
            "before = os.getpgid(0) == os.getpid()",
            "ensure_dispatch_group_leader()",
            "ensure_dispatch_group_leader()",
            "print(before, os.getpgid(0) == os.getpid())",
        ]
    )

    completed = subprocess.run(
        [sys.executable, "-I", "-c", probe],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )

    assert completed.stdout.split() == ["False", "True"]


def linux_fields(pid: int) -> dict[str, str]:
    status = Path(f"/proc/{pid}/status").read_text().splitlines()
    fields = dict(line.split(":", 1) for line in status if ":" in line)
    limits = Path(f"/proc/{pid}/limits").read_text().splitlines()
    core = next(line for line in limits if line.startswith("Max core file size"))
    return {
        "umask": fields["Umask"].strip(),
        "blocked": fields["SigBlk"].strip(),
        "core": " ".join(core.split()[4:6]),
        "owner": str(os.stat(f"/proc/{pid}/status").st_uid),
    }


@pytest.mark.asyncio
async def test_registered_runner_is_hardened(
    tmp_path: Path, kora_capture: Capture
) -> None:
    root = state_root(tmp_path)
    ctx = runtime_context(kora_capture)
    with short_directory() as base:
        runtime = await open_spawning(root, ctx, base / "sockets")
        try:
            await runtime.submit(submitted(runtime, "attempt-1"), ctx)
            entry = await recorded(runtime, "attempt-1")
            assert isinstance(entry, Running)
            pid = entry.runner.pid
            group, session = os.getpgid(pid), os.getsid(pid)
            fields = linux_fields(pid) if sys.platform == "linux" else {}
        finally:
            await runtime.close()
            reap_runners(root)

    assert entry.runner.leads_group is False
    assert pid not in (group, session)
    assert session != os.getsid(0)
    if sys.platform == "linux":
        assert fields == {
            "umask": "0077",
            "blocked": "0000000000000000",
            "core": "0 0",
            "owner": "0",
        }
