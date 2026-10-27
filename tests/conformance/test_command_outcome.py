import asyncio
import os
import socket
import sys
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from kora.conformance.controllers import duration_ns
from kora.conformance.io_helper import InstalledIOHelperLauncher, read_journal
from kora.conformance.lifetime import ParentLifetime
from kora.conformance.transport import LifetimeEndpoint
from kora.protocols.lifetime import (
    CleanupOutcome,
    OriginalOutcome,
    ProcessDomain,
    ProcessLaunch,
    ReservationRequest,
    ResourceKind,
    TemporaryTreeRequest,
    new_operation_id,
)
from kora.protocols.time import SystemClock
from kora.runner.filesystem import DirectoryAnchor
from tests.conformance.lifetime_helpers import ProbeIOHelperLauncher
from tests.conformance.test_lifetime_cleanup_controls import dispose_test_owner
from tests.conformance.test_lifetime_io_helper import entered_fifo
from tests.conformance.test_lifetime_pending_helpers import ControlledLifetimeClock
from tests.measurements.lifetime_workload import turn


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", list(OriginalOutcome))
async def test_command_outcome_precedes_terminal_report_and_journal_removal(
    tmp_path: Path, outcome: OriginalOutcome
) -> None:
    clock = ControlledLifetimeClock()
    owner = ParentLifetime(
        clock=clock,
        launcher=InstalledIOHelperLauncher(
            home=tmp_path, state_home=tmp_path / "state"
        ),
        await_command_outcome=True,
    )
    try:
        async with asyncio.timeout(5):
            tree = await owner.reserve(
                ReservationRequest(
                    operation_id=new_operation_id(),
                    kind=ResourceKind.TEMPORARY_TREE,
                )
            )
            acquired = await owner.acquire(
                tree.token, new_operation_id(), TemporaryTreeRequest()
            )
            await owner.acknowledge(
                tree.token, acquired.operation_id, acquired.revision
            )
            directory = owner.records[tree.token.resource_id].directory
            invocation = owner.mutations.directory
            assert directory is not None
            assert invocation is not None

            closing = owner.request_close(None)
            loop = asyncio.get_running_loop()
            while not directory.closed or owner.cleanup_effect is not None:
                turn: asyncio.Future[None] = loop.create_future()
                loop.call_soon(turn.set_result, None)
                await turn
            assert not closing.done()
            assert not invocation.closed
            owner.finalize_command_outcome(outcome)
            report = await asyncio.shield(closing)

        assert report.original_outcome is outcome
        assert report.state is CleanupOutcome.CLOSED
        assert invocation.closed
        assert await owner.close(None) is report
        assert owner.inventory().helpers.retained_helpers == 0
    finally:
        await dispose_test_owner(owner, clock)


@pytest.mark.asyncio
async def test_missing_command_outcome_uses_original_close_deadline(
    tmp_path: Path,
) -> None:
    clock = ControlledLifetimeClock()
    owner = ParentLifetime(
        clock=clock,
        launcher=InstalledIOHelperLauncher(
            home=tmp_path, state_home=tmp_path / "state"
        ),
        await_command_outcome=True,
    )
    try:
        async with asyncio.timeout(5):
            await owner.ready()
            invocation = owner.mutations.directory
            assert invocation is not None
            closing = owner.request_close(None)
            final = owner.invocation_cleanup.final

            clock.advance(final - clock.monotonic_ns())
            report = await asyncio.shield(closing)

        assert report.state is CleanupOutcome.UNKNOWN
        assert report.original_outcome is OriginalOutcome.FAILED
        assert report.cleanup_reference is not None
        assert not invocation.closed
        assert owner.invocation_cleanup.final == final
        assert final - owner.invocation_cleanup.cooperative == duration_ns(9.0)
        assert await owner.close(None) is report
    finally:
        await dispose_test_owner(owner, clock)


@pytest.mark.asyncio
@pytest.mark.parametrize("publication_failed", [False, True])
async def test_late_failed_command_outcome_distinguishes_durable_publication(
    tmp_path: Path, publication_failed: bool
) -> None:
    clock = ControlledLifetimeClock()
    owner = ParentLifetime(
        clock=clock,
        launcher=InstalledIOHelperLauncher(
            home=tmp_path, state_home=tmp_path / "state"
        ),
        await_command_outcome=True,
    )
    try:
        async with asyncio.timeout(5):
            tree = await owner.reserve(
                ReservationRequest(
                    operation_id=new_operation_id(), kind=ResourceKind.TEMPORARY_TREE
                )
            )
            acquired = await owner.acquire(
                tree.token, new_operation_id(), TemporaryTreeRequest()
            )
            await owner.acknowledge(
                tree.token, acquired.operation_id, acquired.revision
            )
            directory = owner.records[tree.token.resource_id].directory
            invocation = owner.mutations.directory
            assert directory is not None
            assert invocation is not None
            if publication_failed:
                os.mkfifo("retained", 0o600, dir_fd=directory.descriptor)
            else:
                owner.retain_unverified_resources()

            closing = owner.request_close(None)
            loop = asyncio.get_running_loop()
            while publication_failed and not owner.mutations.failed:
                turn: asyncio.Future[None] = loop.create_future()
                loop.call_soon(turn.set_result, None)
                await turn
            assert not closing.done()
            owner.finalize_command_outcome(OriginalOutcome.FAILED)
            owner.set_original_outcome(OriginalOutcome.CANCELLED)
            report = await asyncio.shield(closing)
            retained = read_journal(invocation)

        assert report.state is CleanupOutcome.UNKNOWN
        assert report.original_outcome is OriginalOutcome.FAILED
        assert retained.original_outcome is (
            OriginalOutcome.COMPLETE if publication_failed else OriginalOutcome.FAILED
        )
        assert owner.mutations.failed is publication_failed
        assert not invocation.closed
        assert await owner.close(None) is report
    finally:
        await dispose_test_owner(owner, clock)


@pytest.mark.asyncio
async def test_scoped_cleanup_does_not_wait_for_command_outcome(tmp_path: Path) -> None:
    clock = ControlledLifetimeClock()
    owner = ParentLifetime(
        clock=clock,
        launcher=InstalledIOHelperLauncher(
            home=tmp_path, state_home=tmp_path / "state"
        ),
        await_command_outcome=True,
    )
    try:
        async with asyncio.timeout(5):
            tree = await owner.reserve(
                ReservationRequest(
                    operation_id=new_operation_id(), kind=ResourceKind.TEMPORARY_TREE
                )
            )
            acquired = await owner.acquire(
                tree.token, new_operation_id(), TemporaryTreeRequest()
            )
            await owner.acknowledge(
                tree.token, acquired.operation_id, acquired.revision
            )

            scoped = await owner.close(tree.token.resource_id)
            assert scoped.state is CleanupOutcome.CLOSED
            assert not owner.invocation_cleanup.result.done()
            owner.finalize_command_outcome(OriginalOutcome.COMPLETE)
            report = await owner.close(None)

        assert report.state is CleanupOutcome.CLOSED
        assert owner.inventory().retained_trees == 0
    finally:
        await dispose_test_owner(owner, clock)


@pytest.mark.asyncio
async def test_blocked_command_outcome_publication_keeps_original_deadline(
    tmp_path: Path,
) -> None:
    clock = ControlledLifetimeClock()
    with ExitStack() as resources:
        barrier = DirectoryAnchor.open_path(tmp_path)
        resources.callback(barrier.close)
        os.mkfifo("stall", 0o600, dir_fd=barrier.descriptor)
        reading, writing = os.pipe()
        resources.callback(os.close, reading)
        resources.callback(os.close, writing)
        owner = ParentLifetime(
            clock=clock,
            launcher=ProbeIOHelperLauncher(
                tmp_path, writing, barrier.descriptor, pause_at=2
            ),
            await_command_outcome=True,
        )
        try:
            async with asyncio.timeout(5):
                await owner.ready()
                owner.retain_unverified_resources()
                closing = owner.request_close(None)
                final = owner.invocation_cleanup.final
                owner.finalize_command_outcome(OriginalOutcome.FAILED)
                actual = SystemClock()
                with await entered_fifo(
                    barrier.descriptor,
                    reading,
                    actual.monotonic_ns() + 5_000_000_000,
                    actual,
                ):
                    clock.advance(final - clock.monotonic_ns())
                    report = await asyncio.shield(closing)

                assert report.state is CleanupOutcome.UNKNOWN
                assert report.original_outcome is OriginalOutcome.FAILED
                assert owner.invocation_cleanup.final == final
                assert owner.mutations.snapshot is not None
                assert (
                    owner.mutations.snapshot.original_outcome
                    is OriginalOutcome.COMPLETE
                )
        finally:
            await dispose_test_owner(owner, clock)


@pytest.mark.asyncio
async def test_retained_endpoint_reports_late_failure_before_unknown(
    tmp_path: Path,
) -> None:
    clock = ControlledLifetimeClock()
    owner = ParentLifetime(
        clock=clock,
        launcher=InstalledIOHelperLauncher(
            home=tmp_path, state_home=tmp_path / "state"
        ),
        await_command_outcome=True,
    )
    retained: LifetimeEndpoint | None = None
    try:
        with TemporaryDirectory(prefix="kora-endpoint-", dir="/tmp") as temporary:
            address = str(Path(temporary) / "endpoint")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(address)
                listener.listen(1)
                listener.setblocking(False)
                async with asyncio.timeout(10):
                    tree = await owner.reserve(
                        ReservationRequest(
                            operation_id=new_operation_id(),
                            kind=ResourceKind.TEMPORARY_TREE,
                        )
                    )
                    await owner.acquire(
                        tree.token, new_operation_id(), TemporaryTreeRequest()
                    )
                    child = await owner.reserve(
                        ReservationRequest(
                            operation_id=new_operation_id(),
                            kind=ResourceKind.PROCESS_DOMAIN,
                            parent_resource_id=tree.token.resource_id,
                        )
                    )
                    acquired = await owner.acquire(
                        child.token,
                        new_operation_id(),
                        ProcessLaunch(
                            executable=sys.executable,
                            argv=(
                                sys.executable,
                                "-I",
                                str(
                                    Path(__file__).with_name(
                                        "command_endpoint_probe.py"
                                    )
                                ),
                                address,
                            ),
                            environment=(),
                            working_directory_resource_id=tree.token.resource_id,
                            stdin=None,
                            stdout=None,
                            stderr=None,
                            domain=ProcessDomain.INVOCATION,
                        ),
                    )
                    await owner.acknowledge(
                        child.token, acquired.operation_id, acquired.revision
                    )
                    endpoint, _ = await asyncio.get_running_loop().sock_accept(listener)
                    retained = LifetimeEndpoint(
                        owner, child.token.resource_id, endpoint
                    )
                    process = owner.records[child.token.resource_id].process
                    directory = owner.records[tree.token.resource_id].directory
                    invocation = owner.mutations.directory
                    assert process is not None
                    assert directory is not None
                    assert invocation is not None

                    closing = owner.request_close(None)
                    clock.advance(5_000_000_000)
                    while not process.closed or not directory.closed:
                        await turn()
                    assert owner.endpoint_count == 1
                    assert not closing.done()
                    owner.finalize_command_outcome(OriginalOutcome.FAILED)
                    report = await asyncio.shield(closing)
                    repeated = await retained.run()

                assert report.state is CleanupOutcome.UNKNOWN
                assert report.original_outcome is OriginalOutcome.FAILED
                assert (
                    read_journal(invocation).original_outcome is OriginalOutcome.FAILED
                )
                assert not invocation.closed
                assert owner.endpoint_count == 0
                assert repeated is report
                assert await owner.close(None) is report
    finally:
        try:
            await dispose_test_owner(owner, clock)
        finally:
            if retained is not None and owner.endpoint_count:
                async with asyncio.timeout(5):
                    await retained.run()
