import asyncio
import errno
import inspect
import json
import os
import shutil
import stat
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kora.conformance.capture import Capture
from kora.conformance.common import kora_capture
from kora.ir.json import JsonObjectField
from kora.plugins.runtime.local.runtime import LocalRuntime
from kora.protocols.common import ProtocolContext
from kora.protocols.deadlines import DEADLINE_GRACE, ExecutionDeadline
from kora.protocols.errors import KoraProtocolError
from kora.protocols.evidence import EvidenceScope, NormalCompletionQuiescence
from kora.protocols.identity import ProcessIdentity
from kora.protocols.runtime import Runtime
from kora.runner.registry import (
    Completed,
    Entry,
    Fence,
    Retiring,
    Running,
    Starting,
    Submitter,
    with_entry,
    write_atomically,
)
from tests.plugins.runtime.local.frames_support import StepClock
from tests.support.local_runners import (
    open_spawning,
    own_identity,
    reap_runners,
    runner_pids,
    short_directory,
    submitted,
)
from tests.support.local_runtime import (
    PATH_MARKER,
    ConstructedIdentityReader,
    fill_boot_log,
    open_runtime,
    prepared,
    private_directory,
    protocol_error,
    recorded,
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
) -> LocalRuntime:
    return await open_runtime(root, runtime_context(capture), reader)


def code(error: KoraProtocolError) -> tuple[str, bool]:
    return error.code, error.retryable


async def outcome(operation: Awaitable[object]) -> str:
    try:
        await operation
    except KoraProtocolError as error:
        return error.code
    return "returned"


def write_entry(runtime: LocalRuntime, attempt: str, entry: Entry) -> None:
    key = runtime.registry.key(attempt)
    write_atomically(
        runtime.registry.directory,
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
            submitter=Submitter(dispatcher_id="dispatcher-1", process=RUNNER),
            nonce="e" * 32,
            started_at=MOMENT,
            socket_directory="/run/user/1000/kora",
            runner_prefix=("/usr/bin/python3", "/state"),
            fence=None,
        ),
        "running": Running(
            state="running",
            deadline=None,
            gate_closed=False,
            request_digest=DIGEST,
            submitter=Submitter(dispatcher_id="dispatcher-1", process=RUNNER),
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
    runtime = await opened(state_root(tmp_path), capture)
    ctx = runtime_context(capture)
    handle = runtime.registry.key("attempt-1").handle()
    try:
        absent = await runtime.lookup("attempt-1", ctx)
        tombstone = await runtime.retire("attempt-1", timedelta(0), ctx)
        repeated = await runtime.retire("attempt-1", timedelta(0), ctx)
        found = await runtime.lookup("attempt-1", ctx)
        status = await runtime.status(handle, ctx)
        fenced = await outcome(runtime.workspace_target("attempt-1", ctx))
    finally:
        await runtime.close()

    assert absent is None
    assert tombstone.scope is EvidenceScope.NO_RUNNER_STARTED
    assert tombstone.execution_domain_id == runtime.execution_domain_id
    assert repeated == tombstone
    assert found == handle
    assert (status.state, status.phase, status.exit_code) == ("exited", "failed", None)
    assert fenced == "attempt_retired"


async def unsubmitted_retires_without_runner(tmp_path: Path, capture: Capture) -> None:
    runtime = await opened(state_root(tmp_path), capture)
    ctx = runtime_context(capture)
    handle = runtime.registry.key("attempt-1").handle()
    try:
        await runtime.workspace_target("attempt-1", ctx)
        unsubmitted = await recorded(runtime, "attempt-1")
        found = await runtime.lookup("attempt-1", ctx)
        status = await outcome(runtime.status(handle, ctx))
        tombstone = await runtime.retire("attempt-1", timedelta(0), ctx)
    finally:
        await runtime.close()

    assert unsubmitted is not None
    assert unsubmitted.state == "unsubmitted"
    assert found is None
    assert status == "attempt_not_submitted"
    assert tombstone.scope is EvidenceScope.NO_RUNNER_STARTED


async def started_outcome(
    runtime: LocalRuntime, attempt: str, entry: Entry, ctx: ProtocolContext
) -> tuple[Entry | None, tuple[object, ...]]:
    write_entry(runtime, attempt, entry)
    handle = runtime.registry.key(attempt).handle()
    retired = await outcome(runtime.retire(attempt, timedelta(0), ctx))
    current = await recorded(runtime, attempt)
    return current, (
        retired,
        await outcome(runtime.lookup(attempt, ctx)),
        await outcome(runtime.status(handle, ctx)),
    )


async def started_states_refuse_until_runner_retirement(
    tmp_path: Path, capture: Capture
) -> None:
    runtime = await opened(state_root(tmp_path), capture)
    ctx = runtime_context(capture)
    entries = {
        state: entry
        for state, entry in started_entries(runtime.execution_domain_id).items()
        if state != "starting"
    }
    outcomes: dict[str, tuple[object, ...]] = {}
    current: dict[str, Entry | None] = {}
    try:
        for state, entry in entries.items():
            current[state], outcomes[state] = await started_outcome(
                runtime, f"attempt-{state}", entry, ctx
            )
    finally:
        await runtime.close()

    unavailable = "retirement_unavailable"
    assert current == entries
    assert outcomes == {
        "running": (unavailable, "returned", "runner_unreachable"),
        "completed": (unavailable, "returned", "returned"),
        "retiring": (unavailable, "returned", "runner_unreachable"),
    }


async def submit_cells_follow_the_entry(tmp_path: Path, capture: Capture) -> None:
    ctx = runtime_context(capture)
    with short_directory() as base:
        runtime = await open_spawning(state_root(tmp_path), ctx, base / "sockets")
        work = submitted(runtime, "attempt-x")
        digest = work.execution_request_digest
        entries: dict[str, Entry] = {
            state: entry.model_copy(update={"request_digest": digest})
            if hasattr(entry, "request_digest")
            else entry
            for state, entry in started_entries(runtime.execution_domain_id).items()
        }
        entries["retired"] = await retired_entry(runtime, ctx)
        outcomes: dict[str, str] = {}
        try:
            for state, entry in entries.items():
                attempt = f"attempt-{state}"
                write_entry(runtime, attempt, entry)
                again = work.model_copy(update={"attempt_id": attempt})
                outcomes[state] = await outcome(runtime.submit(again, ctx))
        finally:
            await runtime.close()
    assert outcomes == {
        "starting": "runner_starting",
        "running": "returned",
        "completed": "returned",
        "retiring": "attempt_retired",
        "retired": "attempt_retired",
    }
    assert runner_pids(tmp_path) == ()


async def retired_entry(runtime: LocalRuntime, ctx: ProtocolContext) -> Entry:
    await runtime.retire("attempt-tombstone", timedelta(0), ctx)
    entry = await recorded(runtime, "attempt-tombstone")
    assert entry is not None
    return entry


async def submit_checks_refuse_before_any_effect(
    tmp_path: Path, capture: Capture
) -> None:
    ctx = runtime_context(capture)
    other = own_identity().model_copy(update={"start_time": 1})
    with short_directory() as base:
        runtime = await open_spawning(state_root(tmp_path), ctx, base / "sockets")
        work = submitted(runtime, "attempt-1")
        variants = {
            "domain": work.model_copy(update={"execution_domain_id": "domain-other"}),
            "null_process": work.model_copy(update={"dispatcher_process": None}),
            "other_process": work.model_copy(update={"dispatcher_process": other}),
            "digest": work.model_copy(
                update={"execution_request_digest": "sha256:" + "2" * 64}
            ),
        }
        try:
            refused = {
                name: await protocol_error(runtime.submit(variant, ctx))
                for name, variant in variants.items()
            }
            pinned = replace(ctx, request_digest="sha256-other")
            refused["context_digest"] = await protocol_error(
                runtime.submit(work, pinned)
            )
            bounded = replace(
                ctx,
                deadline=ExecutionDeadline(
                    at=MOMENT,
                    scope_kind="attempt",
                    scope_id="attempt-1",
                    clock_uncertainty=timedelta(0),
                    grace=DEADLINE_GRACE,
                ),
            )
            refused["context_deadline"] = await protocol_error(
                runtime.submit(work, bounded)
            )
            entry = await recorded(runtime, "attempt-1")
        finally:
            await runtime.close()
    assert {name: code(error) for name, error in refused.items()} == {
        "domain": ("execution_domain_mismatch", False),
        "null_process": ("dispatcher_process_mismatch", False),
        "other_process": ("dispatcher_process_mismatch", False),
        "digest": ("workload_conflict", False),
        "context_digest": ("workload_conflict", False),
        "context_deadline": ("workload_conflict", False),
    }
    assert entry is None
    assert runner_pids(tmp_path) == ()


async def submitter_is_kept_by_registration(tmp_path: Path, capture: Capture) -> None:
    ctx = runtime_context(capture)
    root = state_root(tmp_path)
    with short_directory() as base:
        sockets = base / "sockets"
        runtime = await open_spawning(root, ctx, sockets)
        try:
            await runtime.workspace_target("attempt-1", ctx)
            shutil.rmtree(sockets)
            await runtime.submit(submitted(runtime, "attempt-1"), ctx)
            entry = await recorded(runtime, "attempt-1")
            recreated = stat.S_IMODE(sockets.stat().st_mode)
        finally:
            await runtime.close()
            reap_runners(root)
    assert isinstance(entry, Running)
    assert entry.submitter == Submitter(
        dispatcher_id="dispatcher-1", process=own_identity()
    )
    assert entry.socket_path.startswith(f"{sockets}/")
    assert recreated == 0o700


async def oversized_and_foreign_heads_are_refused(
    tmp_path: Path, capture: Capture
) -> None:
    root = state_root(tmp_path)
    runtime = await opened(root, capture)
    ctx = runtime_context(capture)
    try:
        await runtime.workspace_target("attempt-a", ctx)
        names = {
            attempt: runtime.registry.key(attempt).name
            for attempt in ("attempt-a", "attempt-b", "attempt-c")
        }
        directory = root / "registry"
        foreign = directory / f"{names['attempt-b']}.json"
        foreign.write_bytes((directory / f"{names['attempt-a']}.json").read_bytes())
        foreign.chmod(0o600)
        oversized = directory / f"{names['attempt-c']}.json"
        oversized.write_bytes(b" " * 65537)
        oversized.chmod(0o600)
        named_other = await protocol_error(recorded(runtime, "attempt-b"))
        too_large = await protocol_error(recorded(runtime, "attempt-c"))
    finally:
        await runtime.close()

    assert code(named_other) == ("lineage_unavailable", True)
    assert code(too_large) == ("lineage_unavailable", True)
    assert PATH_MARKER not in named_other.message + too_large.message


async def attempt_ids_map_to_fixed_names(tmp_path: Path, capture: Capture) -> None:
    root = state_root(tmp_path)
    runtime = await opened(root, capture)
    ctx = runtime_context(capture)
    attempts = ["../escape", "a/b/c", "..", "x" * 256, "~!@#$%^&*()"]
    try:
        for attempt in attempts:
            await runtime.workspace_target(attempt, ctx)
        heads = [await recorded(runtime, attempt) for attempt in attempts]
    finally:
        await runtime.close()

    entries = sorted(os.listdir(root / "registry"))
    stems = {entry.split(".", 1)[0] for entry in entries}
    assert len(entries) == 2 * len(attempts)
    assert len(stems) == len(attempts)
    assert all(len(stem) == NAME_LENGTH for stem in stems)
    assert sorted(os.listdir(root)) == ["boot.lock", "domain.json", "registry"]
    assert all(head is not None for head in heads)


async def crash_temporaries_are_swept(tmp_path: Path, capture: Capture) -> None:
    root = state_root(tmp_path)
    runtime = await opened(root, capture)
    ctx = runtime_context(capture)
    name = runtime.registry.key("attempt-1").name
    directory = root / "registry"
    leftover = directory / f"{name}.json.tmp-{'0' * 32}"
    leftover.write_bytes(b"torn")
    leftover.chmod(0o600)
    try:
        absent = await recorded(runtime, "attempt-1")
        await runtime.workspace_target("attempt-1", ctx)
    finally:
        await runtime.close()

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
    runtime = await opened(root, capture)
    ctx = runtime_context(capture)
    filler = fill(prepared("small-filesystem"))
    try:
        failures = [
            await protocol_error(runtime.workspace_target("attempt-1", ctx))
            for _ in range(3)
        ]
        remaining = sorted(os.listdir(root / "registry"))
    finally:
        filler.unlink()
        await runtime.close()
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
    fill_boot_log(root)

    full = await protocol_error(opened(root, capture))
    known = await opened(
        root,
        capture,
        ConstructedIdentityReader(boot_id="boot-old-7", pid_namespace="pid:[1]"),
    )
    await known.close()

    assert code(full) == ("boot_log_full", False)
    assert known.execution_domain_id == "f" * 32


THRESHOLD_NS = 3599 * 10**9


def deadline_after(clock: StepClock, seconds: int) -> ExecutionDeadline:
    return ExecutionDeadline(
        at=clock.now() + timedelta(seconds=seconds),
        scope_kind="attempt",
        scope_id="attempt-1",
        clock_uncertainty=timedelta(seconds=1),
        grace=DEADLINE_GRACE,
    )


def bound_to(
    ctx: ProtocolContext, deadline: ExecutionDeadline | None
) -> ProtocolContext:
    return replace(ctx, deadline=deadline)


async def workspace_target_binds_the_deadline(tmp_path: Path, capture: Capture) -> None:
    root = state_root(tmp_path)
    clock = StepClock()
    ctx = runtime_context(capture, clock)
    runtime = await opened(root, capture)
    deadline = deadline_after(clock, 3600)
    name = runtime.registry.key("attempt-1").name
    try:
        target = await runtime.workspace_target("attempt-1", bound_to(ctx, deadline))
        again = await runtime.workspace_target("attempt-1", bound_to(ctx, deadline))
        other = deadline_after(clock, 7200)
        conflict = await outcome(
            runtime.workspace_target("attempt-1", bound_to(ctx, other))
        )
        unbound = await outcome(runtime.workspace_target("attempt-1", ctx))
        await runtime.workspace_target("attempt-2", ctx)
        bound = await recorded(runtime, "attempt-1")
        null = await recorded(runtime, "attempt-2")
    finally:
        await runtime.close()

    expected = str(root / "workspaces" / name)
    assert (target.root, target.host_path) == (expected, expected)
    assert again == target
    assert (conflict, unbound) == ("workload_conflict", "workload_conflict")
    assert bound is not None
    assert bound.state == "unsubmitted"
    assert bound.deadline == deadline
    assert null is not None
    assert null.state == "unsubmitted"
    assert null.deadline is None


async def expiry_closes_a_sticky_gate(tmp_path: Path, capture: Capture) -> None:
    clock = StepClock()
    ctx = runtime_context(capture, clock)
    runtime = await opened(state_root(tmp_path), capture)
    bound = bound_to(ctx, deadline_after(clock, 3600))
    try:
        clock.offset_ns = THRESHOLD_NS - 1000
        early = await outcome(runtime.workspace_target("attempt-1", bound))
        clock.offset_ns = THRESHOLD_NS
        at_threshold = await outcome(runtime.workspace_target("attempt-1", bound))
        fresh = await outcome(runtime.workspace_target("attempt-2", bound))
        clock.offset_ns = 0
        corrected = await outcome(runtime.workspace_target("attempt-1", bound))
        closed = await recorded(runtime, "attempt-1")
        written = await recorded(runtime, "attempt-2")
    finally:
        await runtime.close()

    assert early == "returned"
    assert (at_threshold, fresh, corrected) == ("deadline_expired",) * 3
    assert closed is not None
    assert closed.state == "unsubmitted"
    assert closed.gate_closed
    assert written is not None
    assert written.state == "unsubmitted"
    assert written.gate_closed


async def attempt_ids_are_validated(tmp_path: Path, capture: Capture) -> None:
    runtime = await opened(state_root(tmp_path), capture)
    ctx = runtime_context(capture)
    other = replace(ctx, attempt_id="attempt-2")
    handle = runtime.registry.key("attempt-1").handle()
    try:
        malformed = [
            await protocol_error(runtime.workspace_target(attempt, ctx))
            for attempt in ("", "x" * 257, "attempt 1", "attempt\x7f", "attempt-\u00e9")
        ]
        mismatched = [
            await outcome(runtime.workspace_target("attempt-1", other)),
            await outcome(runtime.lookup("attempt-1", other)),
            await outcome(runtime.retire("attempt-1", timedelta(0), other)),
            await outcome(runtime.status(handle, other)),
        ]
        matching = await outcome(
            runtime.workspace_target("attempt-1", replace(ctx, attempt_id="attempt-1"))
        )
    finally:
        await runtime.close()

    assert {code(error) for error in malformed} == {("attempt_invalid", False)}
    assert mismatched == ["attempt_invalid"] * 4
    assert matching == "returned"


async def foreign_handles_are_refused(tmp_path: Path, capture: Capture) -> None:
    runtime = await opened(state_root(tmp_path), capture)
    ctx = runtime_context(capture)
    handle = runtime.registry.key("attempt-1").handle()
    foreign = [
        handle.model_copy(update={"runtime": "docker"}),
        handle.model_copy(update={"execution_domain_id": "f" * 32}),
        handle.model_copy(update={"data": JsonObjectField({"format": "local/2"})}),
        handle.model_copy(update={"host_hint": "host-1"}),
    ]
    try:
        refused = [await protocol_error(runtime.status(item, ctx)) for item in foreign]
        own = await outcome(runtime.status(handle, ctx))
    finally:
        await runtime.close()

    assert {code(error) for error in refused} == {("handle_invalid", False)}
    assert own == "attempt_not_submitted"


async def capabilities_are_the_local_row(tmp_path: Path, capture: Capture) -> None:
    runtime = await opened(state_root(tmp_path), capture)
    try:
        capabilities = runtime.capabilities()
    finally:
        await runtime.close()

    assert not capabilities.supports_resume
    assert not capabilities.portable_handles
    assert not capabilities.enforces_network_policy
    assert not capabilities.isolates_tool_secrets
    assert not capabilities.credential_proxy
    assert not capabilities.protected_execution
    assert capabilities.plan_executors == frozenset({"runner", "worker_host"})
    assert capabilities.max_concurrent is None
    assert capabilities.default_stall_after == timedelta(minutes=15)


async def missing_socket_while_running_is_retryable(
    tmp_path: Path, capture: Capture
) -> None:
    runtime = await opened(state_root(tmp_path), capture)
    ctx = runtime_context(capture)
    handle = runtime.registry.key("attempt-1").handle()
    try:
        with short_directory() as base:
            running = started_entries(runtime.execution_domain_id)["running"]
            missing = running.model_copy(update={"socket_path": str(base / "gone")})
            write_entry(runtime, "attempt-1", missing)
            refused = await protocol_error(runtime.status(handle, ctx))
    finally:
        await runtime.close()

    assert code(refused) == ("runner_unreachable", True)
    assert not any(
        record.get("event") == "runtime.local.listener_refused"
        for record in capture.finish()
    )


LEGS: dict[str, Callable[[Path, Capture], Awaitable[None]]] = {
    "missing_socket_while_running_is_retryable": (
        missing_socket_while_running_is_retryable
    ),
    "no_entry_retires_without_runner": no_entry_retires_without_runner,
    "unsubmitted_retires_without_runner": unsubmitted_retires_without_runner,
    "started_states_refuse_until_runner_retirement": (
        started_states_refuse_until_runner_retirement
    ),
    "oversized_and_foreign_heads_are_refused": oversized_and_foreign_heads_are_refused,
    "submit_cells_follow_the_entry": submit_cells_follow_the_entry,
    "submit_checks_refuse_before_any_effect": submit_checks_refuse_before_any_effect,
    "submitter_is_kept_by_registration": submitter_is_kept_by_registration,
    "attempt_ids_map_to_fixed_names": attempt_ids_map_to_fixed_names,
    "crash_temporaries_are_swept": crash_temporaries_are_swept,
    "lineage_is_checked_at_open": lineage_is_checked_at_open,
    "registry_without_domain_is_lineage_error": (
        registry_without_domain_is_lineage_error
    ),
    "concurrent_first_open_shares_domain": concurrent_first_open_shares_domain,
    "full_boot_log_is_refused": full_boot_log_is_refused,
    "workspace_target_binds_the_deadline": workspace_target_binds_the_deadline,
    "expiry_closes_a_sticky_gate": expiry_closes_a_sticky_gate,
    "attempt_ids_are_validated": attempt_ids_are_validated,
    "foreign_handles_are_refused": foreign_handles_are_refused,
    "capabilities_are_the_local_row": capabilities_are_the_local_row,
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


@pytest.mark.parametrize(
    "method",
    ["capabilities", "workspace_target", "submit", "lookup", "status", "retire"],
)
def test_local_runtime_signatures_match_runtime(method: str) -> None:
    implemented = inspect.signature(getattr(LocalRuntime, method))

    declared = inspect.signature(getattr(Runtime, method))

    assert implemented == declared
