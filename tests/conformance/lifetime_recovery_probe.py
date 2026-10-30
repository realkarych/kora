import asyncio
import errno
import os
import sys
from enum import IntEnum
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator

from kora.conformance.io_helper import InstalledIOHelperLauncher
from kora.conformance.lifetime import ParentLifetime
from kora.protocols.boundary import BoundaryModel
from kora.protocols.lifetime import (
    CleanupOutcome,
    InvocationId,
    LifetimeLimits,
    LifetimeReason,
    ProcessDomain,
    ProcessLaunch,
    ProcessRegistration,
    ReservationRequest,
    ResourceKind,
    ResourceState,
    TemporaryTreeRequest,
    new_operation_id,
)
from kora.protocols.time import SystemClock
from kora.runner.channels import (
    ChannelError,
    ChannelFailure,
    DescriptorReadiness,
    wait_future,
)
from kora.runner.identity import IdentityState, NativeIdentityReader, complete_identity


class ProbeStage(IntEnum):
    SETUP = 0
    REPORT = 1
    ACK_WAIT = 2
    OWNER_CLOSE = 3
    OWNER_ASSERT = 4
    NATIVE_READ = 5
    NATIVE_ASSERT = 6
    GROUP_READ = 7
    GROUP_STATE = 8
    GROUP_EMPTY = 9
    RETAINED_ASSERT = 10
    COMPLETE = 11


def probe_refusal(error: BaseException | None) -> int:
    if error is None:
        return 0
    if isinstance(error, ChannelError):
        return 1 + tuple(ChannelFailure).index(error.reason)
    if isinstance(error, AssertionError):
        return 6
    if isinstance(error, asyncio.CancelledError):
        return 7
    if isinstance(error, OSError):
        return {errno.EBADF: 8, errno.EAGAIN: 9, errno.EPERM: 10}.get(
            error.errno or 0, 11
        )
    return 12


def close_probe_diagnostic(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError as failure:
        if failure.errno not in (errno.EPIPE, errno.EAGAIN, errno.EINTR, errno.EBADF):
            raise


def emit_probe_diagnostic(descriptor: int | None, values: list[int]) -> None:
    if descriptor is None:
        return
    try:
        try:
            os.write(descriptor, bytes(values))
        except OSError as failure:
            if failure.errno not in (
                errno.EPIPE,
                errno.EAGAIN,
                errno.EINTR,
                errno.EBADF,
            ):
                raise
    finally:
        close_probe_diagnostic(descriptor)


def decode_probe_diagnostic(value: bytes) -> str:
    refusals = (
        "none",
        "channel_closed",
        "channel_deadline",
        "channel_capacity",
        "channel_malformed",
        "channel_busy",
        "assertion",
        "cancelled",
        "bad_fd",
        "again",
        "permission",
        "other_os",
        "unexpected",
    )
    kinds = ("none", "proc", "runtime", "tree", "borrowed")
    states = ("none", *(item.value for item in ResourceState))
    reasons = ("none", *(item.value for item in LifetimeReason))
    native = ("none", *(item.value for item in IdentityState))
    closure = ("none", "closed", "unknown")
    if len(value) != 21 or value[0] != 1 or value[10] > 2:
        return "probe_diagnostic_unavailable"
    try:
        samples = ",".join(
            f"{kinds[value[index]]}/{states[value[index + 1]]}/"
            f"{reasons[value[index + 2]]}"
            for index in (12, 15, 18)
            if value[index]
        )
        result = (
            f"ack={ProbeStage(value[1]).name}/{refusals[value[2]]};"
            f"end={ProbeStage(value[3]).name}/{refusals[value[4]]};"
            f"close={closure[value[5]]}:{value[6]};"
            f"samples={samples};pid={native[value[7]]};"
            f"group={native[value[8]]}:{value[9]};item={value[10]};keep={value[11]}"
        )
    except (ValueError, IndexError):
        return "probe_diagnostic_invalid"
    return result[:256]


class RecoveryProbeReport(BoundaryModel):
    invocation_id: InvocationId
    mode: Literal["live", "pair"]
    registrations: tuple[ProcessRegistration, ...] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def complete_authority(self) -> Self:
        if len(self.registrations) != (2 if self.mode == "pair" else 1):
            raise ValueError("incomplete process handoff")
        if any(not complete_identity(item.process) for item in self.registrations):
            raise ValueError("incomplete process identity")
        if len({item.process for item in self.registrations}) != len(
            self.registrations
        ):
            raise ValueError("duplicate process identity")
        if len({item.acquisition_operation_id for item in self.registrations}) != len(
            self.registrations
        ):
            raise ValueError("duplicate acquisition identity")
        return self


async def acknowledgment(deadline: int) -> bytes:
    clock = SystemClock()
    os.set_blocking(0, False)
    readiness = DescriptorReadiness(0, writing=False)
    try:
        await wait_future(readiness.ready, deadline, clock)
        return os.read(0, 2)
    finally:
        readiness.close()


async def retain(
    home: Path, mode: str, phase: str = "complete", diagnostic: int | None = None
) -> None:
    status = [0] * 21
    status[0] = 1
    owner = ParentLifetime(
        launcher=InstalledIOHelperLauncher(home=home, state_home=home / "state"),
        limits=LifetimeLimits(
            cooperative_seconds=0.1,
            term_seconds=0.1,
            kill_observation_seconds=2.0,
            drain_seconds=1.0,
        ),
    )
    registrations: list[ProcessRegistration] = []
    transferred = mode not in ("live", "pair")
    try:
        tree = await owner.reserve(
            ReservationRequest(
                operation_id=new_operation_id(), kind=ResourceKind.TEMPORARY_TREE
            )
        )
        await owner.acquire(tree.token, new_operation_id(), TemporaryTreeRequest())
        for _ in range(0 if mode == "tree" else 2 if mode == "pair" else 1):
            process = await owner.reserve(
                ReservationRequest(
                    operation_id=new_operation_id(),
                    kind=ResourceKind.PROCESS_DOMAIN,
                    parent_resource_id=tree.token.resource_id,
                )
            )
            acquisition = await owner.acquire(
                process.token,
                new_operation_id(),
                ProcessLaunch(
                    executable=sys.executable,
                    argv=(
                        sys.executable,
                        "-I",
                        "-c",
                        "pass" if mode == "gone" else "import signal; signal.pause()",
                    ),
                    environment=(),
                    working_directory_resource_id=tree.token.resource_id,
                    stdin=None,
                    stdout=None,
                    stderr=None,
                    domain=ProcessDomain.INVOCATION,
                ),
            )
            assert isinstance(acquisition.registration, ProcessRegistration)
            registrations.append(acquisition.registration)
            await owner.acknowledge(
                process.token, acquisition.operation_id, acquisition.revision
            )
            if mode == "gone":
                handle = owner.records[process.token.resource_id].process
                assert handle is not None
                await handle.settle(owner.deadline())
                await handle.drain(owner.deadline())
                handle.release(owner.deadline())
        if mode not in ("live", "pair"):
            os.write(1, owner.invocation_id.encode("ascii"))
            return
        status[3] = ProbeStage.REPORT
        report = RecoveryProbeReport(
            invocation_id=owner.invocation_id,
            mode=mode,
            registrations=tuple(registrations),
        )
        encoded = report.model_dump_json().encode()
        if phase == "refused":
            encoded = (
                report.model_copy(update={"registrations": ()})
                .model_dump_json()
                .encode()
            )
        assert len(encoded) <= 65536
        os.write(1, len(encoded).to_bytes(4, "big"))
        if phase != "before_report":
            os.write(1, encoded[: len(encoded) // 2] if phase == "partial" else encoded)
        status[3] = ProbeStage.ACK_WAIT
        transferred = await acknowledgment(owner.deadline()) == b"a"
    finally:
        original = sys.exception()
        status[1] = status[3]
        status[2] = probe_refusal(original)
        try:
            if not transferred:
                status[3] = ProbeStage.OWNER_CLOSE
                closed = await owner.close(None)
                status[5] = 1 if closed.state is CleanupOutcome.CLOSED else 2
                status[6] = min(255, closed.unresolved_count)
                for index, sample in enumerate(closed.samples[:3]):
                    status[12 + 3 * index] = 1 + tuple(ResourceKind).index(sample.kind)
                    status[13 + 3 * index] = 1 + tuple(ResourceState).index(
                        sample.state
                    )
                    status[14 + 3 * index] = 1 + tuple(LifetimeReason).index(
                        sample.reason
                    )
                status[3] = ProbeStage.OWNER_ASSERT
                assert closed.state is CleanupOutcome.CLOSED
                reader = NativeIdentityReader()
                for index, registration in enumerate(registrations):
                    status[10] = index + 1
                    status[3] = ProbeStage.NATIVE_READ
                    observed = reader.read(registration.process.pid)
                    status[7] = 1 + tuple(IdentityState).index(observed.state)
                    status[3] = ProbeStage.NATIVE_ASSERT
                    assert observed.state is IdentityState.ABSENT
                    status[3] = ProbeStage.GROUP_READ
                    group = reader.group(registration.process_group_id)
                    status[8] = 1 + tuple(IdentityState).index(group.state)
                    status[9] = min(255, len(group.members))
                    status[3] = ProbeStage.GROUP_STATE
                    assert group.state is IdentityState.PRESENT
                    status[3] = ProbeStage.GROUP_EMPTY
                    assert group.members == ()
                status[3] = ProbeStage.RETAINED_ASSERT
                retained = owner.inventory().retained_processes
                status[11] = min(255, retained)
                assert retained == 0
                status[3] = ProbeStage.COMPLETE
        finally:
            cleanup_error = sys.exception()
            status[4] = probe_refusal(
                cleanup_error if cleanup_error is not original else None
            )
            emit_probe_diagnostic(diagnostic, status)


if __name__ == "__main__":
    asyncio.run(
        retain(
            Path(sys.argv[1]),
            sys.argv[2],
            sys.argv[3] if len(sys.argv) > 3 else "complete",
            int(sys.argv[4]) if len(sys.argv) > 4 else None,
        )
    )
