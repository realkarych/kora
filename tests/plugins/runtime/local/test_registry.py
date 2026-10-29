import asyncio
import errno
import json
import os
import shutil
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import kora_capture
from kora.protocols.common import ProtocolContext
from kora.protocols.errors import KoraProtocolError
from kora.protocols.evidence import EvidenceScope, NormalCompletionQuiescence
from kora.protocols.identity import Handle, ProcessIdentity
from kora.protocols.runtime import RuntimeStatus
from kora.runner.registry import (
    MAX_BOOTS,
    Completed,
    Entry,
    Fence,
    Retired,
    Retiring,
    Running,
    Starting,
    bind_workspace,
    lookup_cell,
    retire_cell,
    status_cell,
    with_entry,
    write_atomically,
)
from tests.support.local_runtime import (
    PATH_MARKER,
    ConstructedIdentityReader,
    OpenedRegistry,
    open_registry,
    prepared,
    private_directory,
    runtime_context,
    state_root,
)

__all__ = ["kora_capture"]

DIGEST = "sha256:" + "1" * 64
MOMENT = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
RUNNER = ProcessIdentity(
    host_id="a" * 32,
    boot_id="boot-1",
    pid_namespace="pid:[4026531836]",
    pid=4242,
    start_time=7,
    leads_group=True,
)
FENCE = Fence(retirement_id="retirement-1", fenced_at=MOMENT)
NAME_LENGTH = 32


async def opened(
    root: Path, capture: Capture, reader: ConstructedIdentityReader | None = None
) -> OpenedRegistry:
    return await open_registry(root, runtime_context(capture), reader)


async def protocol_error(outcome: Awaitable[object]) -> KoraProtocolError:
    try:
        await outcome
    except KoraProtocolError as error:
        return error
    pytest.fail("the operation did not raise a protocol error", pytrace=False)


def code(error: KoraProtocolError) -> tuple[str, bool]:
    return error.code, error.retryable


def refused_code(function: Callable[[], object]) -> str:
    try:
        function()
    except KoraProtocolError as error:
        return error.code
    return "returned"


async def retire(registry: OpenedRegistry, attempt: str, capture: Capture) -> Retired:
    key = registry.key(attempt)
    applied = await registry.transition(
        attempt,
        lambda entry: retire_cell(entry, key, f"retirement-{attempt}", MOMENT),
        runtime_context(capture),
    )
    head = applied.head
    assert applied.transition.refusal is None
    assert head is not None
    assert isinstance(head.entry, Retired)
    return head.entry


def handle_of(
    registry: OpenedRegistry, entry: Entry | None, attempt: str
) -> Handle | None:
    return lookup_cell(entry, registry.key(attempt))


def status_of(entry: Entry | None) -> RuntimeStatus:
    return status_cell(entry, MOMENT)


def write_entry(registry: OpenedRegistry, attempt: str, entry: Entry) -> None:
    key = registry.key(attempt)
    write_atomically(
        registry.registry.directory,
        f"{key.name}.json",
        with_entry(key, entry),
        "f" * 32,
        replace=True,
    )


def started_entries(domain: str) -> dict[str, Entry]:
    quiescence = NormalCompletionQuiescence(
        kind="normal_completion",
        attempt_id="attempt-1",
        execution_domain_id=domain,
        record_ref="record-1",
        observed_at=MOMENT,
        scope=EvidenceScope.LEADER_GROUP_ENV_SWEEP,
    )
    return {
        "starting": Starting(
            state="starting",
            deadline=None,
            gate_closed=False,
            request_digest=DIGEST,
            nonce="e" * 32,
            started_at=MOMENT,
            fence=None,
        ),
        "running": Running(
            state="running",
            deadline=None,
            gate_closed=False,
            request_digest=DIGEST,
            runner=RUNNER,
            socket_path="/run/user/1000/kora/0123456789abcdef",
        ),
        "completed": Completed(
            state="completed",
            deadline=None,
            gate_closed=False,
            request_digest=DIGEST,
            runner=RUNNER,
            quiescence=quiescence,
            fence=FENCE,
            released_at=None,
        ),
        "retiring": Retiring(state="retiring", runner=RUNNER, fence=FENCE),
    }


async def no_entry_retires_without_runner(tmp_path: Path, capture: Capture) -> None:
    registry = await opened(state_root(tmp_path), capture)
    ctx = runtime_context(capture)
    try:
        absent = await registry.entry("attempt-1")
        tombstone = await retire(registry, "attempt-1", capture)
        repeated = await retire(registry, "attempt-1", capture)
        recorded = await registry.entry("attempt-1")
        fenced = await registry.transition(
            "attempt-1", lambda entry: bind_workspace(entry, None, False), ctx
        )
    finally:
        await registry.close()

    status = status_of(recorded)
    handle = registry.key("attempt-1").handle()
    assert handle_of(registry, absent, "attempt-1") is None
    assert tombstone.receipt.scope is EvidenceScope.NO_RUNNER_STARTED
    assert tombstone.receipt.execution_domain_id == registry.execution_domain_id
    assert repeated.receipt == tombstone.receipt
    assert handle_of(registry, recorded, "attempt-1") == handle
    assert (status.state, status.phase, status.exit_code) == ("exited", "failed", None)
    assert fenced.transition.refusal == "attempt_retired"


async def unsubmitted_retires_without_runner(tmp_path: Path, capture: Capture) -> None:
    registry = await opened(state_root(tmp_path), capture)
    ctx = runtime_context(capture)
    try:
        bound = await registry.transition(
            "attempt-1", lambda entry: bind_workspace(entry, None, False), ctx
        )
        unsubmitted = await registry.entry("attempt-1")
        tombstone = await retire(registry, "attempt-1", capture)
    finally:
        await registry.close()

    assert bound.head is not None
    assert bound.head.entry.state == "unsubmitted"
    assert handle_of(registry, unsubmitted, "attempt-1") is None
    assert refused_code(lambda: status_of(unsubmitted)) == "attempt_not_submitted"
    assert tombstone.receipt.scope is EvidenceScope.NO_RUNNER_STARTED


async def started_outcome(
    registry: OpenedRegistry, attempt: str, entry: Entry, ctx: ProtocolContext
) -> tuple[Entry | None, tuple[object, ...]]:
    write_entry(registry, attempt, entry)
    key = registry.key(attempt)
    applied = await registry.transition(
        attempt, lambda current: retire_cell(current, key, "r", MOMENT), ctx
    )
    current = await registry.entry(attempt)
    return current, (
        applied.transition.refusal,
        refused_code(lambda: handle_of(registry, current, attempt)),
        refused_code(lambda: status_of(current)),
    )


async def started_states_refuse_until_runner_retirement(
    tmp_path: Path, capture: Capture
) -> None:
    registry = await opened(state_root(tmp_path), capture)
    ctx = runtime_context(capture)
    entries = started_entries(registry.execution_domain_id)
    outcomes: dict[str, tuple[object, ...]] = {}
    recorded: dict[str, Entry | None] = {}
    try:
        for state, entry in entries.items():
            recorded[state], outcomes[state] = await started_outcome(
                registry, f"attempt-{state}", entry, ctx
            )
    finally:
        await registry.close()

    unavailable = "retirement_unavailable"
    assert recorded == entries
    assert outcomes == {
        "starting": (unavailable, "runner_starting", "runner_starting"),
        "running": (unavailable, "returned", "runner_unreachable"),
        "completed": (unavailable, "returned", "returned"),
        "retiring": (unavailable, "returned", "runner_unreachable"),
    }


async def oversized_and_foreign_heads_are_refused(
    tmp_path: Path, capture: Capture
) -> None:
    root = state_root(tmp_path)
    registry = await opened(root, capture)
    ctx = runtime_context(capture)
    try:
        await registry.transition(
            "attempt-a", lambda entry: bind_workspace(entry, None, False), ctx
        )
        names = {
            attempt: registry.key(attempt).name
            for attempt in ("attempt-a", "attempt-b", "attempt-c")
        }
        directory = root / "registry"
        foreign = directory / f"{names['attempt-b']}.json"
        foreign.write_bytes((directory / f"{names['attempt-a']}.json").read_bytes())
        foreign.chmod(0o600)
        oversized = directory / f"{names['attempt-c']}.json"
        oversized.write_bytes(b" " * 65537)
        oversized.chmod(0o600)
        named_other = await protocol_error(registry.entry("attempt-b"))
        too_large = await protocol_error(registry.entry("attempt-c"))
    finally:
        await registry.close()

    assert code(named_other) == ("lineage_unavailable", True)
    assert code(too_large) == ("lineage_unavailable", True)
    assert PATH_MARKER not in named_other.message + too_large.message


async def attempt_ids_map_to_fixed_names(tmp_path: Path, capture: Capture) -> None:
    root = state_root(tmp_path)
    registry = await opened(root, capture)
    ctx = runtime_context(capture)
    attempts = ["../escape", "a/b/c", "..", "x" * 256, "~!@#$%^&*()"]
    try:
        for attempt in attempts:
            await registry.transition(
                attempt, lambda entry: bind_workspace(entry, None, False), ctx
            )
        recorded = [await registry.entry(attempt) for attempt in attempts]
    finally:
        await registry.close()

    entries = sorted(os.listdir(root / "registry"))
    stems = {entry.split(".", 1)[0] for entry in entries}
    assert len(entries) == 2 * len(attempts)
    assert len(stems) == len(attempts)
    assert all(len(stem) == NAME_LENGTH for stem in stems)
    assert sorted(os.listdir(root)) == ["boot.lock", "domain.json", "registry"]
    assert all(entry is not None for entry in recorded)


async def crash_temporaries_are_swept(tmp_path: Path, capture: Capture) -> None:
    root = state_root(tmp_path)
    registry = await opened(root, capture)
    ctx = runtime_context(capture)
    name = registry.key("attempt-1").name
    directory = root / "registry"
    leftover = directory / f"{name}.json.tmp-{'0' * 32}"
    leftover.write_bytes(b"torn")
    leftover.chmod(0o600)
    try:
        absent = await registry.entry("attempt-1")
        await registry.transition(
            "attempt-1", lambda entry: bind_workspace(entry, None, False), ctx
        )
    finally:
        await registry.close()

    assert absent is None
    assert sorted(os.listdir(directory)) == [f"{name}.json", f"{name}.lock"]


def write_until_full(descriptor: int, size: int) -> int | None:
    chunk = b"\0" * size
    while True:
        try:
            os.write(descriptor, chunk)
        except OSError as error:
            return error.errno


def fill(directory: Path) -> Path:
    filler = directory / "filler"
    descriptor = os.open(filler, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        failures = [write_until_full(descriptor, size) for size in (65536, 512, 1)]
    finally:
        os.close(descriptor)
    assert all(failure in (errno.ENOSPC, errno.EDQUOT) for failure in failures)
    return filler


async def failed_writes_leave_no_temporaries(tmp_path: Path, capture: Capture) -> None:
    root = private_directory(prepared("small-filesystem"), tmp_path.name)
    registry = await opened(root, capture)
    ctx = runtime_context(capture)
    filler = fill(prepared("small-filesystem"))
    try:
        failures = [
            await protocol_error(
                registry.transition(
                    "attempt-1", lambda entry: bind_workspace(entry, None, False), ctx
                )
            )
            for _ in range(3)
        ]
        remaining = sorted(os.listdir(root / "registry"))
    finally:
        filler.unlink()
        await registry.close()
        shutil.rmtree(root)

    assert {code(failure) for failure in failures} == {("registry_unavailable", True)}
    assert not any(".tmp-" in entry for entry in remaining)


async def lineage_is_checked_at_open(tmp_path: Path, capture: Capture) -> None:
    root = state_root(tmp_path)
    first = await opened(root, capture)
    await first.close()
    moved_host = await protocol_error(
        opened(root, capture, ConstructedIdentityReader(host_id="b" * 32))
    )
    moved_namespace = await protocol_error(
        opened(root, capture, ConstructedIdentityReader(pid_namespace="pid:[1]"))
    )
    unknown_namespace = await protocol_error(
        opened(root, capture, ConstructedIdentityReader(pid_namespace=None))
    )
    rebooted = await opened(
        root,
        capture,
        ConstructedIdentityReader(boot_id="boot-2", pid_namespace="pid:[2]"),
    )
    await rebooted.close()
    again = await opened(root, capture)
    await again.close()

    record = json.loads((root / "domain.json").read_bytes())
    assert code(moved_host) == ("lineage_unavailable", True)
    assert code(moved_namespace) == ("lineage_unavailable", True)
    assert code(unknown_namespace) == ("lineage_unavailable", True)
    assert rebooted.execution_domain_id == again.execution_domain_id
    assert again.execution_domain_id == first.execution_domain_id
    assert record["boots"] == [
        {"boot_id": "boot-1", "pid_namespace": "pid:[4026531836]"},
        {"boot_id": "boot-2", "pid_namespace": "pid:[2]"},
    ]
    assert PATH_MARKER not in moved_host.message + moved_namespace.message


async def registry_without_domain_is_lineage_error(
    tmp_path: Path, capture: Capture
) -> None:
    root = state_root(tmp_path)
    (root / "registry").mkdir(mode=0o700)
    leftover = root / f"domain.json.tmp-{'0' * 32}"
    leftover.write_bytes(b"torn")
    leftover.chmod(0o600)

    missing = await protocol_error(opened(root, capture))

    assert code(missing) == ("lineage_unavailable", True)
    assert sorted(os.listdir(root)) == ["boot.lock", "registry"]


async def concurrent_first_open_shares_domain(tmp_path: Path, capture: Capture) -> None:
    root = state_root(tmp_path)

    registries = await asyncio.gather(*(opened(root, capture) for _ in range(4)))
    for registry in registries:
        await registry.close()

    assert len({registry.execution_domain_id for registry in registries}) == 1
    assert sorted(os.listdir(root)) == ["boot.lock", "domain.json", "registry"]


async def full_boot_log_is_refused(tmp_path: Path, capture: Capture) -> None:
    root = state_root(tmp_path)
    record = {
        "format": "local/1",
        "execution_domain_id": "f" * 32,
        "host_id": "a" * 32,
        "boots": [
            {"boot_id": f"boot-old-{index}", "pid_namespace": "pid:[1]"}
            for index in range(MAX_BOOTS)
        ],
    }
    (root / "domain.json").write_text(json.dumps(record))
    (root / "domain.json").chmod(0o600)
    (root / "registry").mkdir(mode=0o700)

    full = await protocol_error(opened(root, capture))
    known = await opened(
        root,
        capture,
        ConstructedIdentityReader(boot_id="boot-old-7", pid_namespace="pid:[1]"),
    )
    await known.close()

    assert code(full) == ("boot_log_full", False)
    assert known.execution_domain_id == "f" * 32


LEGS: dict[str, Callable[[Path, Capture], Awaitable[None]]] = {
    "no_entry_retires_without_runner": no_entry_retires_without_runner,
    "unsubmitted_retires_without_runner": unsubmitted_retires_without_runner,
    "started_states_refuse_until_runner_retirement": (
        started_states_refuse_until_runner_retirement
    ),
    "oversized_and_foreign_heads_are_refused": oversized_and_foreign_heads_are_refused,
    "attempt_ids_map_to_fixed_names": attempt_ids_map_to_fixed_names,
    "crash_temporaries_are_swept": crash_temporaries_are_swept,
    "lineage_is_checked_at_open": lineage_is_checked_at_open,
    "registry_without_domain_is_lineage_error": (
        registry_without_domain_is_lineage_error
    ),
    "concurrent_first_open_shares_domain": concurrent_first_open_shares_domain,
    "full_boot_log_is_refused": full_boot_log_is_refused,
}
PREPARED_LEGS: dict[str, Callable[[Path, Capture], Awaitable[None]]] = {
    "failed_writes_leave_no_temporaries": failed_writes_leave_no_temporaries,
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "leg",
    [
        *sorted(LEGS),
        *(
            pytest.param(name, marks=pytest.mark.host_fixture)
            for name in sorted(PREPARED_LEGS)
        ),
    ],
)
async def test_registry_states_and_release(
    tmp_path: Path, kora_capture: Capture, leg: str
) -> None:
    await {**LEGS, **PREPARED_LEGS}[leg](tmp_path, kora_capture)

    records = kora_capture.finish()
    assert all(PATH_MARKER not in str(record) for record in records)
