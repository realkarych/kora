import asyncio
import os
import signal
import sys
from contextlib import AsyncExitStack
from pathlib import Path

import pytest

from kora.conformance.lifetime import LifetimeError, ParentLifetime
from kora.protocols.lifetime import (
    CleanupOutcome,
    ProcessDomain,
    ProcessLaunch,
    ProcessRegistration,
    ReservationRequest,
    ResourceKind,
    TemporaryTreeRequest,
    new_operation_id,
)
from kora.protocols.time import SystemClock
from kora.runner.identity import NativeIdentityReader
from kora.runner.processes import RecoveredProcess
from tests.conformance.lifetime_support import (
    ControlledLifetimeClock,
    close_recovered_test_owner,
    dispose_test_owner,
    installed_launcher,
    limits,
    retained_invocation,
    retained_journal,
    retained_live_invocation,
)

__all__ = ["limits"]


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
    clock = ControlledLifetimeClock()
    lifetime = ParentLifetime(
        clock=clock,
        limits=limits(),
        launcher=installed_launcher(tmp_path),
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
    clock = ControlledLifetimeClock()
    lifetime = ParentLifetime(
        clock=clock,
        limits=limits().model_copy(update={"max_resource_records": 2}),
        launcher=installed_launcher(tmp_path),
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
    launcher = installed_launcher(tmp_path)
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
    async with AsyncExitStack() as owned:
        retained = (
            await owned.enter_async_context(retained_live_invocation(tmp_path, "live"))
            if mode == "live"
            else None
        )
        reference = (
            retained.reference
            if retained is not None
            else retained_invocation(tmp_path, mode)
        )
        clock = ControlledLifetimeClock()
        lifetime = ParentLifetime(
            clock=clock,
            limits=limits(),
            recovery=reference,
            launcher=installed_launcher(tmp_path),
        )

        observers: list[RecoveredProcess] = []
        actual = SystemClock()
        try:
            await lifetime.ready()
            observers = (
                list(retained.processes)
                if retained is not None
                else [
                    RecoveredProcess(
                        record.entry.registration, actual, NativeIdentityReader()
                    )
                    for record in lifetime.records.values()
                    if isinstance(record.entry.registration, ProcessRegistration)
                ]
            )

            report = await close_recovered_test_owner(lifetime, clock, observers)

            assert lifetime.generation == 2
            assert report.state is CleanupOutcome.CLOSED
            assert lifetime.inventory().helpers.claim_descriptors == 0
            assert not tuple(tmp_path.rglob(reference.invocation_id))
        finally:
            deadline = (
                retained.cleanup()
                if retained is not None
                else actual.monotonic_ns() + 5_000_000_000
            )
            try:
                for observer in observers:
                    observer.signal(signal.SIGKILL, deadline)
                    await observer.settle(deadline)
                    observer.release(deadline)
            finally:
                await dispose_test_owner(
                    lifetime, clock, retained.cleanup if retained is not None else None
                )


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["owner", "helper"])
@pytest.mark.parametrize("field", ["host_id", "boot_id", "pid_namespace", "incomplete"])
async def test_recovery_refuses_absent_writer_from_unproved_scope(
    tmp_path: Path, role: str, field: str
) -> None:
    reference = retained_invocation(tmp_path, "tree")
    path, journal = retained_journal(tmp_path, reference.invocation_id)
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
        launcher=installed_launcher(tmp_path),
    )

    try:
        with pytest.raises(LifetimeError):
            await owner.ready()
        assert path.read_bytes() == retained
    finally:
        await owner.close(None)
