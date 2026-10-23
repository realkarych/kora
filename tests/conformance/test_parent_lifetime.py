import asyncio
import os
import sys
from pathlib import Path

import pytest

from kora.conformance.io_helper import InstalledIOHelperLauncher
from kora.conformance.lifetime import LifetimeError, ParentLifetime
from kora.protocols.lifetime import (
    CleanupOutcome,
    LifetimeLimits,
    ProcessDomain,
    ProcessLaunch,
    ReservationRequest,
    ResourceKind,
    TemporaryTreeRequest,
    new_operation_id,
)


def limits() -> LifetimeLimits:
    return LifetimeLimits(
        cooperative_seconds=0.1,
        term_seconds=0.1,
        kill_observation_seconds=2.0,
        drain_seconds=1.0,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "normal",
        "lost_acquire_reply",
        "duplicate_conflict",
        "close_before_ack",
        "cancel_before_dispatch",
        "detached",
    ],
)
async def test_conformance_fixture_resources_are_released(
    tmp_path: Path, case: str
) -> None:
    from tests.conformance.test_lifetime_cleanup_controls import dispose_test_owner
    from tests.conformance.test_lifetime_pending_helpers import ControlledLifetimeClock

    clock = ControlledLifetimeClock()
    lifetime = ParentLifetime(
        clock=clock,
        limits=limits(),
        launcher=InstalledIOHelperLauncher(
            home=tmp_path, state_home=tmp_path / "state"
        ),
    )
    try:
        tree = await lifetime.reserve(
            ReservationRequest(
                operation_id=new_operation_id(), kind=ResourceKind.TEMPORARY_TREE
            )
        )
        tree_acquisition = await lifetime.acquire(
            tree.token, new_operation_id(), TemporaryTreeRequest()
        )
        await lifetime.acknowledge(
            tree.token, tree_acquisition.operation_id, tree_acquisition.revision
        )
        request = ReservationRequest(
            operation_id=new_operation_id(),
            kind=ResourceKind.PROCESS_DOMAIN,
            parent_resource_id=tree.token.resource_id,
        )
        reservation = await lifetime.reserve(request)
        operation_id = new_operation_id()
        launch = ProcessLaunch(
            executable=sys.executable,
            argv=(sys.executable, "-I", "-c", "pass"),
            environment=(),
            working_directory_resource_id=tree.token.resource_id,
            stdin=None,
            stdout=None,
            stderr=None,
            domain=ProcessDomain.DETACHED
            if case == "detached"
            else ProcessDomain.INVOCATION,
        )

        acquire = asyncio.create_task(
            lifetime.acquire(reservation.token, operation_id, launch)
        )
        if case == "cancel_before_dispatch":
            acquire.cancel()
            with pytest.raises(asyncio.CancelledError):
                await acquire
        else:
            acquisition = await acquire
            if case == "lost_acquire_reply":
                assert (
                    await lifetime.acquire(reservation.token, operation_id, launch)
                    == acquisition
                )
            if case == "duplicate_conflict":
                with pytest.raises(LifetimeError):
                    await lifetime.acquire(
                        reservation.token,
                        operation_id,
                        launch.model_copy(
                            update={
                                "argv": (
                                    sys.executable,
                                    "-I",
                                    "-c",
                                    "raise SystemExit(9)",
                                )
                            }
                        ),
                    )
            if case != "close_before_ack":
                activation = await lifetime.acknowledge(
                    reservation.token, operation_id, acquisition.revision
                )
                assert (
                    await lifetime.acknowledge(
                        reservation.token, operation_id, acquisition.revision
                    )
                    == activation
                )
        report = await asyncio.wait_for(lifetime.close(None), 5)

        assert report.state is CleanupOutcome.CLOSED
        assert await lifetime.close(None) == report
        assert lifetime.inventory().retained_processes == 0
        assert lifetime.inventory().helpers.retained_helpers == 0
        assert lifetime.inventory().helpers.claim_descriptors == 0
        assert not any(
            path.name == report.invocation_id
            for path in tmp_path.rglob(report.invocation_id)
        )
        assert reservation.token.owner.pid == os.getpid()
    finally:
        await dispose_test_owner(lifetime, clock)


@pytest.mark.asyncio
async def test_conformance_fixture_resources_are_released_capacity_and_subtree_fence(
    tmp_path: Path,
) -> None:
    from tests.conformance.test_lifetime_cleanup_controls import dispose_test_owner
    from tests.conformance.test_lifetime_pending_helpers import ControlledLifetimeClock

    clock = ControlledLifetimeClock()
    lifetime = ParentLifetime(
        clock=clock,
        limits=limits().model_copy(update={"max_resource_records": 2}),
        launcher=InstalledIOHelperLauncher(
            home=tmp_path, state_home=tmp_path / "state"
        ),
    )
    try:
        root_request = ReservationRequest(
            operation_id=new_operation_id(), kind=ResourceKind.TEMPORARY_TREE
        )
        root = await lifetime.reserve(root_request)
        acquisition = await lifetime.acquire(
            root.token, new_operation_id(), TemporaryTreeRequest()
        )
        child = await lifetime.reserve(
            ReservationRequest(
                operation_id=new_operation_id(),
                kind=ResourceKind.TEMPORARY_TREE,
                parent_resource_id=root.token.resource_id,
            )
        )
        child_acquisition = await lifetime.acquire(
            child.token,
            new_operation_id(),
            TemporaryTreeRequest(parent_resource_id=root.token.resource_id),
        )

        with pytest.raises(LifetimeError):
            await lifetime.reserve(
                ReservationRequest(
                    operation_id=new_operation_id(), kind=ResourceKind.TEMPORARY_TREE
                )
            )
        subtree = await asyncio.wait_for(lifetime.close(root.token.resource_id), 5)
        with pytest.raises(LifetimeError):
            await lifetime.reserve(
                ReservationRequest(
                    operation_id=new_operation_id(),
                    kind=ResourceKind.TEMPORARY_TREE,
                    parent_resource_id=root.token.resource_id,
                )
            )
        late = await lifetime.acknowledge(
            child.token, child_acquisition.operation_id, child_acquisition.revision
        )
        report = await asyncio.wait_for(lifetime.close(None), 5)

        assert subtree.state is CleanupOutcome.CLOSED
        assert report.state is CleanupOutcome.CLOSED
        assert late.result.value == "closing"
        assert acquisition.token == root.token
    finally:
        await dispose_test_owner(lifetime, clock)


@pytest.mark.asyncio
async def test_conformance_fixture_resources_are_released_state_root_capacity(
    tmp_path: Path,
) -> None:
    from tests.conformance.test_lifetime_cleanup_controls import dispose_test_owner
    from tests.conformance.test_lifetime_pending_helpers import ControlledLifetimeClock

    launcher = InstalledIOHelperLauncher(home=tmp_path, state_home=tmp_path / "state")
    bounded = limits().model_copy(update={"max_state_root_invocations": 1})
    clock = ControlledLifetimeClock()
    owner = ParentLifetime(limits=bounded, launcher=launcher, clock=clock)
    refused: ParentLifetime | None = None
    try:
        await owner.ready()
        refused = ParentLifetime(limits=bounded, launcher=launcher)

        with pytest.raises(LifetimeError):
            await refused.ready()
        refusal_report = await refused.close(None)
        report = await asyncio.wait_for(owner.close(None), 5)

        assert refusal_report.state is CleanupOutcome.UNKNOWN
        assert report.state is CleanupOutcome.CLOSED
        assert refused.inventory().retained_processes == 0
    finally:
        if refused is not None:
            await refused.close(None)
        await dispose_test_owner(owner, clock)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["tree", "live", "gone"])
async def test_conformance_fixture_resources_are_released_after_dead_owner_recovery(
    tmp_path: Path,
    mode: str,
) -> None:
    import subprocess

    from kora.protocols.lifetime import CleanupReference
    from kora.protocols.time import SystemClock
    from kora.runner.identity import NativeIdentityReader
    from kora.runner.processes import RecoveredProcess
    from tests.conformance.test_lifetime_cleanup_controls import (
        close_recovered_test_owner,
        dispose_test_owner,
    )
    from tests.conformance.test_lifetime_pending_helpers import ControlledLifetimeClock

    probe = subprocess.run(
        [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("lifetime_recovery_probe.py")),
            str(tmp_path),
            mode,
        ],
        check=True,
        capture_output=True,
    )
    reference = CleanupReference(invocation_id=probe.stdout.decode("ascii"))
    clock = ControlledLifetimeClock()
    lifetime = ParentLifetime(
        clock=clock,
        limits=limits(),
        recovery=reference,
        launcher=InstalledIOHelperLauncher(
            home=tmp_path, state_home=tmp_path / "state"
        ),
    )

    observers: list[RecoveredProcess] = []
    actual = SystemClock()
    try:
        await lifetime.ready()
        from kora.protocols.lifetime import ProcessRegistration

        observers = [
            RecoveredProcess(record.entry.registration, actual, NativeIdentityReader())
            for record in lifetime.records.values()
            if isinstance(record.entry.registration, ProcessRegistration)
        ]

        report = await close_recovered_test_owner(lifetime, clock, observers)

        assert lifetime.generation == 2
        assert report.state is CleanupOutcome.CLOSED
        assert lifetime.inventory().helpers.claim_descriptors == 0
        assert not tuple(tmp_path.rglob(reference.invocation_id))
    finally:
        import signal

        deadline = actual.monotonic_ns() + 5_000_000_000
        for observer in observers:
            observer.signal(signal.SIGKILL, deadline)
            await observer.settle(deadline)
            observer.release(deadline)
        await dispose_test_owner(lifetime, clock)


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["owner", "helper"])
@pytest.mark.parametrize("field", ["host_id", "boot_id", "pid_namespace", "incomplete"])
async def test_recovery_refuses_absent_writer_from_unproved_scope(
    tmp_path: Path, role: str, field: str
) -> None:
    import subprocess

    from kora.conformance.journal import JournalSnapshot
    from kora.protocols.lifetime import CleanupReference

    probe = subprocess.run(
        [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("lifetime_recovery_probe.py")),
            str(tmp_path),
            "tree",
        ],
        check=True,
        capture_output=True,
    )
    reference = CleanupReference(invocation_id=probe.stdout.decode("ascii"))
    path = next(tmp_path.rglob(reference.invocation_id)) / "journal"
    journal = JournalSnapshot.model_validate_json(path.read_bytes())
    original = journal.owner if role == "owner" else journal.helpers[0]
    changed = original.model_copy(
        update={
            "pid": 2**31 - 1,
            "host_id" if field == "incomplete" else field: None
            if field == "incomplete"
            else "foreign",
        }
    )
    replacement = journal.model_copy(
        update={"owner": changed} if role == "owner" else {"helpers": (changed,)}
    )
    retained = replacement.model_dump_json().encode()
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
    finally:
        await owner.close(None)
