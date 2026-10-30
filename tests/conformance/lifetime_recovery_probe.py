import asyncio
import os
import sys
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
    ProcessDomain,
    ProcessLaunch,
    ProcessRegistration,
    ReservationRequest,
    ResourceKind,
    TemporaryTreeRequest,
    new_operation_id,
)
from kora.protocols.time import SystemClock
from kora.runner.channels import DescriptorReadiness, wait_future
from kora.runner.identity import IdentityState, NativeIdentityReader, complete_identity


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


async def retain(home: Path, mode: str, phase: str = "complete") -> None:
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
        transferred = await acknowledgment(owner.deadline()) == b"a"
    finally:
        if not transferred:
            assert (await owner.close(None)).state is CleanupOutcome.CLOSED
            reader = NativeIdentityReader()
            for registration in registrations:
                assert (
                    reader.read(registration.process.pid).state is IdentityState.ABSENT
                )
                group = reader.group(registration.process_group_id)
                assert group.state is IdentityState.PRESENT
                assert group.members == ()
            assert owner.inventory().retained_processes == 0


if __name__ == "__main__":
    asyncio.run(
        retain(
            Path(sys.argv[1]),
            sys.argv[2],
            sys.argv[3] if len(sys.argv) > 3 else "complete",
        )
    )
