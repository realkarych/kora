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
    lifetime = ParentLifetime(
        limits=limits(),
        launcher=InstalledIOHelperLauncher(
            home=tmp_path, state_home=tmp_path / "state"
        ),
    )
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
        argv=("-I", "-c", "pass"),
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
                        update={"argv": ("-I", "-c", "raise SystemExit(9)")}
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
    report = await lifetime.close(None)

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


@pytest.mark.asyncio
async def test_conformance_fixture_resources_are_released_capacity_and_subtree_fence(
    tmp_path: Path,
) -> None:
    lifetime = ParentLifetime(
        limits=limits().model_copy(update={"max_resource_records": 2}),
        launcher=InstalledIOHelperLauncher(
            home=tmp_path, state_home=tmp_path / "state"
        ),
    )
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
    subtree = await lifetime.close(root.token.resource_id)
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
    report = await lifetime.close(None)

    assert subtree.state is CleanupOutcome.CLOSED
    assert report.state is CleanupOutcome.CLOSED
    assert late.result.value == "closing"
    assert acquisition.token == root.token


@pytest.mark.asyncio
async def test_conformance_fixture_resources_are_released_state_root_capacity(
    tmp_path: Path,
) -> None:
    launcher = InstalledIOHelperLauncher(home=tmp_path, state_home=tmp_path / "state")
    bounded = limits().model_copy(update={"max_state_root_invocations": 1})
    owner = ParentLifetime(limits=bounded, launcher=launcher)
    await owner.ready()
    refused = ParentLifetime(limits=bounded, launcher=launcher)

    with pytest.raises(LifetimeError):
        await refused.ready()
    refusal_report = await refused.close(None)
    report = await owner.close(None)

    assert refusal_report.state is CleanupOutcome.UNKNOWN
    assert report.state is CleanupOutcome.CLOSED
    assert refused.inventory().retained_processes == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["tree", "live", "gone"])
async def test_conformance_fixture_resources_are_released_after_dead_owner_recovery(
    tmp_path: Path,
    mode: str,
) -> None:
    import subprocess

    from kora.protocols.lifetime import CleanupReference

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
    lifetime = ParentLifetime(
        limits=limits(),
        recovery=reference,
        launcher=InstalledIOHelperLauncher(
            home=tmp_path, state_home=tmp_path / "state"
        ),
    )

    await lifetime.ready()
    report = await lifetime.close(None)

    assert lifetime.generation == 2
    assert report.state is CleanupOutcome.CLOSED
    assert lifetime.inventory().helpers.claim_descriptors == 0
    assert not tuple(tmp_path.rglob(reference.invocation_id))
