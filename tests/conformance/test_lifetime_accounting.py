import asyncio
from pathlib import Path

import pytest

from kora.conformance.io_helper import InstalledIOHelperLauncher
from kora.conformance.lifetime import (
    CLOSURE_CONTROL_BYTES,
    CLOSURE_REPORT_BYTES,
    MEMBER_CONTROL_BYTES,
    LifetimeError,
    ParentLifetime,
)
from kora.conformance.transport import (
    ENVELOPE,
    HEADER,
    HEADER_BYTES,
    CloseReply,
    OrdinaryHeader,
    OrdinaryReply,
    SuccessReply,
    reply_reservation,
)
from kora.protocols.identity import MAX_COUNT, MAX_PID, ProcessIdentity
from kora.protocols.lifetime import (
    CleanupOutcome,
    CleanupReference,
    ClosureReport,
    LifetimeLimits,
    LifetimeReason,
    OriginalOutcome,
    Reservation,
    ReservationRequest,
    ResourceKind,
    ResourceObservation,
    ResourceState,
    new_operation_id,
)
from tests.conformance.test_lifetime_cleanup_controls import dispose_test_owner
from tests.conformance.test_lifetime_pending_helpers import ControlledLifetimeClock
from tests.conformance.test_parent_lifetime import limits


def test_control_reservations_cover_maximum_closed_wire_values() -> None:
    identity = ProcessIdentity(
        host_id='"' * 256,
        boot_id='"' * 256,
        pid_namespace='"' * 256,
        pid=MAX_PID,
        start_time=MAX_COUNT,
        leads_group=True,
    )
    observation = ResourceObservation(
        resource_id="f" * 32,
        kind=max(ResourceKind, key=len),
        revision=2**64 - 1,
        state=max(ResourceState, key=len),
        pending_acquisition_count=MAX_COUNT,
        live_owned_process_count=MAX_COUNT,
        remaining_pipe_count=MAX_COUNT,
        remaining_tree_count=MAX_COUNT,
        reason=max(LifetimeReason, key=len),
    )
    report = ClosureReport(
        invocation_id="f" * 32,
        resource_id="e" * 32,
        revision=2**64 - 1,
        state=CleanupOutcome.UNKNOWN,
        original_outcome=OriginalOutcome.CANCELLED,
        cleanup_outcome=CleanupOutcome.UNKNOWN,
        unresolved_count=MAX_COUNT,
        samples=(observation,) * 32,
        cleanup_reference=CleanupReference(invocation_id="f" * 32),
    )
    header = OrdinaryHeader(
        request_id=2**64 - 1,
        verb="acknowledge",
        control_bytes=131072,
        execution_bytes=1048576,
    )

    close = ENVELOPE.dump_json(CloseReply(resource_id="e" * 32, report=report))
    observed = ENVELOPE.dump_json(
        OrdinaryReply(
            request_id=2**64 - 1,
            reply=SuccessReply(value=(observation,) * 256),
        )
    )

    assert len(close) <= CLOSURE_REPORT_BYTES
    assert len(observed) <= reply_reservation("observe", LifetimeLimits())
    assert len(HEADER.dump_json(header)) <= HEADER_BYTES
    assert len(identity.model_dump_json().encode()) + 256 <= MEMBER_CONTROL_BYTES


@pytest.mark.asyncio
async def test_prepaid_control_capacity_refuses_before_effect_and_keeps_close_usable(
    tmp_path: Path,
) -> None:
    clock = ControlledLifetimeClock()
    owner = ParentLifetime(
        clock=clock,
        limits=limits().model_copy(update={"max_control_recovery_bytes": 100000}),
        launcher=InstalledIOHelperLauncher(
            home=tmp_path, state_home=tmp_path / "state"
        ),
    )
    admitted: list[Reservation] = []
    refusal = None

    try:
        await owner.ready()
        while refusal is None:
            try:
                admitted.append(
                    await owner.reserve(
                        ReservationRequest(
                            operation_id=new_operation_id(),
                            kind=ResourceKind.TEMPORARY_TREE,
                        )
                    )
                )
            except LifetimeError as error:
                refusal = error.reason
        before = tuple(owner.records)
        report = await asyncio.wait_for(owner.close(None), 5)

        assert refusal is LifetimeReason.CAPACITY_EXCEEDED
        assert 0 < len(admitted) < owner.limits.max_resource_records
        assert len(before) == len(admitted)
        assert (
            owner.control_bytes + owner.mutations.inventory().journal_bytes
            <= owner.limits.max_control_recovery_bytes
        )
        assert owner.control_bytes >= (len(admitted) + 1) * CLOSURE_CONTROL_BYTES
        assert report.state is CleanupOutcome.CLOSED
        assert not owner.ordinary_slots
    finally:
        await dispose_test_owner(owner, clock)
