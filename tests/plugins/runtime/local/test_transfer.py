import asyncio
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Literal

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import kora_capture
from kora.observability import LoggingSettings, configure_process
from kora.plugins.runtime.local.control import transfer_input
from kora.plugins.runtime.local.runtime import LocalRuntime
from kora.plugins.runtime.local.serving import COMPONENT
from kora.protocols.activity import NoActivity
from kora.protocols.common import ProtocolContext
from kora.protocols.identity import Handle
from kora.protocols.runtime import TransferItem
from kora.protocols.time import SystemClock
from kora.runner.filesystem import open_local_directory
from kora.runner.identity import NativeIdentityReader
from kora.runner.journal import read_records
from kora.runner.peers import kernel_source
from kora.runner.registry import Running, TransitionExecutor
from tests.plugins.runtime.local.test_command import (
    attempt_directory,
    binary,
    command_workload,
    fill,
    launched,
    reap_runners,
    release_commands,
    running_pid,
)
from tests.support.local_peers import identity_of, until
from tests.support.local_runners import (
    exited_within,
    open_spawning,
    runner_pids,
    short_directory,
)
from tests.support.local_runtime import (
    PATH_MARKER,
    prepared,
    private_directory,
    protocol_error,
    recorded,
    runtime_context,
    state_root,
)

__all__ = ["kora_capture"]


def digest_of(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def input_item(
    name: str,
    payload: bytes,
    *,
    kind: Literal["resource", "input", "script"] = "input",
    sha256: str | None = None,
) -> TransferItem:
    return TransferItem(
        name=name,
        size=len(payload),
        sha256=digest_of(payload) if sha256 is None else sha256,
        kind=kind,
    )


async def chunks(payload: bytes) -> AsyncIterator[bytes]:
    yield payload


def mode_of(path: Path) -> int:
    return os.stat(path).st_mode & 0o777


def journal_kinds(attempt: Path) -> list[str]:
    anchor = open_local_directory(attempt)
    try:
        return [kind for _, kind in read_records(anchor)]
    finally:
        anchor.close()


def journal_fields(attempt: Path) -> tuple[str, str]:
    for line in (attempt / "journal.jsonl").read_text().splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("kind") != "transfer.materialised":
            continue
        data = record["data"]
        assert set(data) == {"name", "sha256"}
        name = data["name"]
        digest = data["sha256"]
        assert isinstance(name, str)
        assert isinstance(digest, str)
        return name, digest
    pytest.fail("the materialised record is missing", pytrace=False)


def transfer_diagnostics(path: Path) -> list[tuple[str, int]]:
    hidden = {"name", "sha256", "path", "pid", "token", "argv", "attempt_id"}
    found: list[tuple[str, int]] = []
    for line in path.read_text().splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("event") != "runtime.local.transfer":
            continue
        outcome = record.get("outcome")
        waited = record.get("waited_ms")
        assert isinstance(outcome, str)
        assert isinstance(waited, int)
        assert hidden.isdisjoint(record)
        found.append((outcome, waited))
    return found


def stored_text(attempt: Path) -> str:
    journal = attempt / "journal.jsonl"
    if not journal.exists():
        return ""
    return journal.read_text()


async def accepted(
    runtime: LocalRuntime, ctx: ProtocolContext, root: Path, attempt: str
) -> tuple[Handle, Path]:
    workload = command_workload(runtime, attempt, (binary("true"),))
    handle = await runtime.submit(workload, ctx)
    return handle, attempt_directory(root, runtime, attempt)


@pytest.mark.asyncio
async def test_input_transfer_is_durable_before_acknowledgement(
    tmp_path: Path, kora_capture: Capture
) -> None:
    payload = b"payload-bytes"
    async with launched(tmp_path, kora_capture) as (runtime, ctx, root):
        handle, attempt = await accepted(runtime, ctx, root, "attempt-1")
        await runtime.transfer_in(
            handle, input_item("payload", payload), chunks(payload), ctx
        )
        stored = attempt / "input" / "payload"
        file_mode = mode_of(stored)
        directory_mode = mode_of(attempt / "input")
        body = stored.read_bytes()
        kinds = journal_kinds(attempt)
        materialised = journal_fields(attempt)
        log = attempt / "runner.log"
        await until(lambda: '"runtime.local.transfer"' in log.read_text())
        diagnostics = transfer_diagnostics(log)
        entry = await recorded(runtime, "attempt-1")
        assert isinstance(entry, Running)
        spool = list((Path(entry.socket_path).parent / "spool").iterdir())
        pid = await running_pid(runtime, "attempt-1")
        os.kill(pid, signal.SIGKILL)
        assert exited_within(pid, 10)
        after = stored.read_bytes()
        remaining = journal_kinds(attempt)

    assert file_mode == 0o400
    assert directory_mode == 0o700
    assert body == payload == after
    assert kinds.count("transfer.materialised") == 1
    assert remaining.count("transfer.materialised") == 1
    assert materialised == ("payload", digest_of(payload))
    assert [outcome for outcome, _waited in diagnostics] == ["materialised"]
    assert all(isinstance(waited, int) for _outcome, waited in diagnostics)
    assert spool == []


@pytest.mark.asyncio
async def test_transfer_digest_mismatch_is_rejected(
    tmp_path: Path, kora_capture: Capture
) -> None:
    payload = b"payload-bytes"
    async with launched(tmp_path, kora_capture) as (runtime, ctx, root):
        handle, attempt = await accepted(runtime, ctx, root, "attempt-1")
        refused = await protocol_error(
            runtime.transfer_in(
                handle,
                input_item("payload", payload, sha256=digest_of(b"other-bytes")),
                chunks(payload),
                ctx,
            )
        )
        names = sorted(path.name for path in (attempt / "input").iterdir())
        kinds = journal_kinds(attempt)

    assert refused.code == "integrity_error"
    assert refused.retryable is False
    assert names == []
    assert "transfer.materialised" not in kinds
    assert kinds.count("transfer.rejected") == 1


@pytest.mark.asyncio
async def test_transfer_name_conflict_is_refused(
    tmp_path: Path, kora_capture: Capture
) -> None:
    payload = b"payload-bytes"
    other = b"other-bytes!!"
    async with launched(tmp_path, kora_capture) as (runtime, ctx, root):
        handle, attempt = await accepted(runtime, ctx, root, "attempt-1")
        item = input_item("payload", payload)
        await runtime.transfer_in(handle, item, chunks(payload), ctx)
        await runtime.transfer_in(handle, item, chunks(payload), ctx)
        conflict = await protocol_error(
            runtime.transfer_in(
                handle, input_item("payload", other), chunks(other), ctx
            )
        )
        changed = b"X" * len(payload)
        rejected = await protocol_error(
            runtime.transfer_in(handle, item, chunks(changed), ctx)
        )
        note = b"note-bytes"
        missed = await protocol_error(
            runtime.transfer_in(
                handle,
                input_item("note", note, sha256=digest_of(b"not-the-note")),
                chunks(note),
                ctx,
            )
        )
        stored = attempt / "input" / "payload"
        body = stored.read_bytes()
        names = sorted(path.name for path in stored.parent.iterdir())
        text = stored_text(attempt)

    assert conflict.code == "workload_conflict"
    assert rejected.code == "integrity_error"
    assert missed.code == "integrity_error"
    assert names == ["payload"]
    assert body == payload
    assert text.count('"transfer.materialised"') == 1


@pytest.mark.asyncio
async def test_script_transfer_is_refused(
    tmp_path: Path, kora_capture: Capture
) -> None:
    payload = b"script-bytes"
    async with launched(tmp_path, kora_capture) as (runtime, ctx, root):
        handle, attempt = await accepted(runtime, ctx, root, "attempt-1")
        refused = await protocol_error(
            runtime.transfer_in(
                handle,
                input_item("payload", payload, kind="script"),
                chunks(payload),
                ctx,
            )
        )
        resource = await protocol_error(
            runtime.transfer_in(
                handle,
                input_item("payload", payload, kind="resource"),
                chunks(payload),
                ctx,
            )
        )
        created = (attempt / "input").exists()

    assert refused.code == "transfer_refused"
    assert resource.code == "transfer_refused"
    assert refused.retryable is False
    assert created is False


@pytest.mark.asyncio
async def test_transfer_after_start_is_refused(
    tmp_path: Path, kora_capture: Capture
) -> None:
    payload = b"payload-bytes"
    async with launched(tmp_path, kora_capture) as (runtime, ctx, root):
        handle, attempt = await accepted(runtime, ctx, root, "attempt-1")
        await runtime.start(handle, ctx)
        refused = await protocol_error(
            runtime.transfer_in(
                handle, input_item("payload", payload), chunks(payload), ctx
            )
        )
        created = (attempt / "input").exists()

    assert refused.code == "transfer_refused"
    assert created is False


@pytest.mark.asyncio
async def test_accepted_incoming_name_survives_the_next_transfer(
    tmp_path: Path, kora_capture: Capture
) -> None:
    kept = b"kept-bytes"
    nxt = b"next-bytes"
    async with launched(tmp_path, kora_capture) as (runtime, ctx, root):
        handle, attempt = await accepted(runtime, ctx, root, "attempt-1")
        await runtime.transfer_in(
            handle, input_item(".incoming", kept), chunks(kept), ctx
        )
        await runtime.transfer_in(handle, input_item("payload", nxt), chunks(nxt), ctx)
        directory = attempt / "input"
        names = sorted(path.name for path in directory.iterdir())
        kept_body = (directory / ".incoming").read_bytes()
        next_body = (directory / "payload").read_bytes()

    assert names == [".incoming", "payload"]
    assert kept_body == kept
    assert next_body == nxt


@pytest.mark.asyncio
async def test_transfer_does_not_follow_an_input_symlink(
    tmp_path: Path, kora_capture: Capture
) -> None:
    payload = b"payload-bytes"
    async with launched(tmp_path, kora_capture) as (runtime, ctx, root):
        handle, attempt = await accepted(runtime, ctx, root, "attempt-1")
        directory = attempt / "input"
        directory.mkdir(mode=0o700)
        os.chmod(directory, 0o700)
        outside = attempt / "outside"
        outside.write_bytes(b"kept")
        (directory / "payload").symlink_to(outside)
        refused = await protocol_error(
            runtime.transfer_in(
                handle, input_item("payload", payload), chunks(payload), ctx
            )
        )
        followed = outside.read_bytes()
        linked = (directory / "payload").is_symlink()

    assert refused.code == "transfer_refused"
    assert followed == b"kept"
    assert linked is True


async def transfer_worker() -> None:
    root = Path(os.environ["KORA_COMMAND_ROOT"])
    sockets = Path(os.environ["KORA_COMMAND_SOCKETS"])
    clock = SystemClock()
    diagnostics = configure_process(
        COMPONENT,
        LoggingSettings(),
        "transfer-worker",
        clock.now,
        os.open(os.devnull, os.O_WRONLY),
    )
    ctx = ProtocolContext(
        namespace_id="local-runtime",
        run_id=None,
        attempt_id=None,
        logger=diagnostics.logger(COMPONENT),
        secrets=None,
        secret_admission=None,
        request_digest=None,
        deadline=None,
        clock=clock,
        activity=NoActivity(),
    )
    runtime = await open_spawning(root, ctx, sockets)
    await runtime.submit(command_workload(runtime, "attempt-1", (binary("true"),)), ctx)
    entry = await recorded(runtime, "attempt-1")
    assert isinstance(entry, Running)
    sys.stdout.write(f"{entry.socket_path}\n")
    sys.stdout.flush()
    signal.pause()


@pytest.mark.asyncio
async def test_observational_attach_cannot_transfer(tmp_path: Path) -> None:
    payload = b"payload-bytes"
    root = state_root(tmp_path)
    with short_directory() as base:
        sockets = base / "sockets"
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import asyncio\n"
                "from tests.plugins.runtime.local.test_transfer import "
                "transfer_worker\n"
                "asyncio.run(transfer_worker())\n",
            ],
            env={
                **os.environ,
                "KORA_COMMAND_ROOT": str(root),
                "KORA_COMMAND_SOCKETS": str(sockets),
            },
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert process.stdout is not None
            path = await asyncio.to_thread(process.stdout.readline)
            runners = runner_pids(root)
            assert len(runners) == 1
            os.kill(process.pid, signal.SIGKILL)
            process.wait(timeout=10)
            identity = identity_of(runners[0])
            spool = base / "body"
            spool.write_bytes(payload)
            executor = TransitionExecutor()
            try:
                refused = await protocol_error(
                    transfer_input(
                        path.strip(),
                        identity,
                        "dispatcher-2",
                        kernel_source(NativeIdentityReader()),
                        executor,
                        SystemClock(),
                        "transfer-1",
                        spool,
                        input_item("payload", payload),
                    )
                )
            finally:
                await executor.close()
            created = any((root / "attempts").glob("*/input"))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            if process.stdout is not None:
                process.stdout.close()
            release_commands(root)
            reap_runners(root)

    assert path.strip()
    assert refused.code == "execution_unauthorized"
    assert created is False


@pytest.mark.asyncio
@pytest.mark.host_fixture
async def test_fenced_journal_refuses_transfer(
    tmp_path: Path, kora_capture: Capture
) -> None:
    payload = b"x"
    small = private_directory(
        prepared("small-filesystem"), f"{PATH_MARKER}-{os.getpid()}-transfer"
    )
    root = private_directory(small, "state")
    ctx = runtime_context(kora_capture)
    with short_directory() as base:
        runtime = await open_spawning(root, ctx, base / "sockets")
        try:
            handle, attempt = await accepted(runtime, ctx, root, "attempt-1")
            fill(attempt)
            refused = await protocol_error(
                runtime.transfer_in(
                    handle, input_item("payload", payload), chunks(payload), ctx
                )
            )
            present = (attempt / "input").exists() and any(
                (attempt / "input").iterdir()
            )
            text = stored_text(attempt)
        finally:
            release_commands(root)
            await runtime.close()
            reap_runners(root)
            shutil.rmtree(small)

    assert refused.code == "journal_unavailable"
    assert refused.retryable is True
    assert present is False
    assert "transfer.materialised" not in text
