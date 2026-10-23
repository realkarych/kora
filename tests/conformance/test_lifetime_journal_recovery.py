import signal
import subprocess
import sys
from pathlib import Path

import pytest

from kora.conformance.io_helper import InstalledIOHelperLauncher
from kora.conformance.journal import JournalSnapshot
from kora.conformance.lifetime import LifetimeError, ParentLifetime
from kora.protocols.lifetime import CleanupReference, ProcessRegistration, ResourceKind
from kora.protocols.time import SystemClock
from kora.runner.identity import NativeIdentityReader
from kora.runner.processes import RecoveredProcess, SignalResult
from tests.conformance.test_parent_lifetime import limits


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption", ["duplicate", "parent", "kind", "operation", "revision", "pending"]
)
async def test_corrupt_journal_cannot_lose_live_process_ownership(
    tmp_path: Path, corruption: str
) -> None:
    probe = subprocess.run(
        [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("lifetime_recovery_probe.py")),
            str(tmp_path),
            "pair",
        ],
        check=True,
        capture_output=True,
    )
    reference = CleanupReference(invocation_id=probe.stdout.decode("ascii"))
    path = next(tmp_path.rglob(reference.invocation_id)) / "journal"
    journal = JournalSnapshot.model_validate_json(path.read_bytes())
    clock = SystemClock()
    processes = [
        RecoveredProcess(entry.registration, clock, NativeIdentityReader())
        for entry in journal.entries
        if isinstance(entry.registration, ProcessRegistration)
    ]
    assert len(processes) == 2
    original = journal.entries[-1]
    if corruption == "duplicate":
        changed = original.model_copy(
            update={"resource_id": journal.entries[-2].resource_id}
        )
    elif corruption == "parent":
        changed = original.model_copy(
            update={"parent_resource_id": original.resource_id}
        )
    elif corruption == "kind":
        changed = original.model_copy(update={"kind": ResourceKind.TEMPORARY_TREE})
    elif corruption == "operation":
        changed = original.model_copy(update={"acquisition_id": "e" * 32})
    elif corruption == "revision":
        changed = original.model_copy(update={"revision": journal.revision + 1})
    else:
        changed = original
    corrupt = journal.model_copy(
        update={
            "entries": (*journal.entries[:-1], changed),
            "pending_effects": ("f" * 32,)
            if corruption == "pending"
            else journal.pending_effects,
        }
    )
    retained = corrupt.model_dump_json().encode()
    path.write_bytes(retained)
    owner = ParentLifetime(
        limits=limits(),
        recovery=reference,
        launcher=InstalledIOHelperLauncher(
            home=tmp_path, state_home=tmp_path / "state"
        ),
    )

    try:
        with pytest.raises(LifetimeError):
            await owner.ready()
        assert path.read_bytes() == retained
        assert all(
            process.observe(clock.monotonic_ns() + 5_000_000_000).live_members
            for process in processes
        )
    finally:
        await owner.close(None)
        for process in processes:
            deadline = clock.monotonic_ns() + 5_000_000_000
            assert process.signal(signal.SIGKILL, deadline) in (
                SignalResult.SENT,
                SignalResult.SETTLED,
            )
            await process.settle(deadline)
            process.release(deadline)
