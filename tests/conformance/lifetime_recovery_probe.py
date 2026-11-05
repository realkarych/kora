import asyncio
import os
import sys
from pathlib import Path
from time import monotonic_ns
from typing import Literal, Self

from pydantic import Field, model_validator

from kora.conformance.controllers import duration_ns
from kora.conformance.io_helper import InstalledIOHelperLauncher
from kora.conformance.lifetime import LifetimeError, ParentLifetime
from kora.protocols.boundary import BoundaryModel
from kora.protocols.lifetime import (
    CleanupOutcome,
    InvocationId,
    LifetimeLimits,
    LifetimeReason,
    ProcessDomain,
    ProcessLaunch,
    ProcessRegistration,
    Reservation,
    ReservationRequest,
    ResourceKind,
    TemporaryTreeRequest,
    new_operation_id,
)
from kora.protocols.time import SystemClock
from kora.runner.channels import DescriptorReadiness, wait_future
from kora.runner.identity import IdentityState, NativeIdentityReader, complete_identity


class ControlledLifetimeClock(SystemClock):
    def __init__(self) -> None:
        self.instant = super().monotonic_ns()
        self.waiters: dict[asyncio.Future[None], int] = {}

    def monotonic_ns(self) -> int:
        return self.instant

    async def sleep_until(self, deadline_ns: int) -> None:
        if self.instant >= deadline_ns:
            return
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.waiters[waiter] = deadline_ns
        try:
            await waiter
        finally:
            self.waiters.pop(waiter)

    def advance(self, nanoseconds: int) -> None:
        self.instant += nanoseconds
        for waiter, deadline in tuple(self.waiters.items()):
            if self.instant >= deadline and not waiter.done():
                waiter.set_result(None)


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


def report_stage(stage: str) -> None:
    record = f"recovery_probe stage={stage} monotonic_ns={monotonic_ns()}\n".encode()
    assert len(record) <= 128
    assert os.write(2, record) == len(record)


class PairPreparationRefused(Exception):
    def __init__(self) -> None:
        super().__init__("expected pair acquisition refusal")


async def prepare_process(
    owner: ParentLifetime,
    tree: Reservation,
    mode: str,
    registrations: list[ProcessRegistration],
    *,
    argv: tuple[str, ...] | None = None,
    acquired: asyncio.Event | None = None,
) -> None:
    report_stage("process_reserve")
    process = await owner.reserve(
        ReservationRequest(
            operation_id=new_operation_id(),
            kind=ResourceKind.PROCESS_DOMAIN,
            parent_resource_id=tree.token.resource_id,
        )
    )
    report_stage("process_acquire")
    acquisition = await owner.acquire(
        process.token,
        new_operation_id(),
        ProcessLaunch(
            executable=sys.executable,
            argv=argv
            if argv is not None
            else (
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
    report_stage("process_acquired")
    if acquired is not None:
        acquired.set()
        waiting: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        await waiting
    await owner.acknowledge(
        process.token, acquisition.operation_id, acquisition.revision
    )
    if mode == "gone":
        handle = owner.records[process.token.resource_id].process
        assert handle is not None
        await handle.settle(owner.deadline())
        await handle.drain(owner.deadline())
        handle.release(owner.deadline())


async def refuse_after_acquisition(
    owner: ParentLifetime,
    tree: Reservation,
    registrations: list[ProcessRegistration],
    acquired: asyncio.Event,
) -> None:
    await acquired.wait()
    try:
        await prepare_process(owner, tree, "pair", registrations, argv=())
    except LifetimeError as error:
        if error.reason is not LifetimeReason.ACQUISITION_FAILED:
            raise
        raise PairPreparationRefused() from None
    raise AssertionError("unexpected successful acquisition")


async def retain(home: Path, mode: str, phase: str = "complete") -> None:
    report_stage("entered")
    clock = ControlledLifetimeClock() if mode in ("live", "pair") else None
    owner = ParentLifetime(
        clock=clock,
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
        report_stage("tree_reserve")
        tree = await owner.reserve(
            ReservationRequest(
                operation_id=new_operation_id(), kind=ResourceKind.TEMPORARY_TREE
            )
        )
        report_stage("tree_acquire")
        await owner.acquire(tree.token, new_operation_id(), TemporaryTreeRequest())
        report_stage("tree_acquired")
        if mode == "pair":
            preparation_refused = False
            try:
                async with asyncio.TaskGroup() as tasks:
                    if phase == "acquire_refused":
                        acquired = asyncio.Event()
                        tasks.create_task(
                            prepare_process(
                                owner, tree, mode, registrations, acquired=acquired
                            )
                        )
                        tasks.create_task(
                            refuse_after_acquisition(
                                owner, tree, registrations, acquired
                            )
                        )
                    else:
                        for _ in range(2):
                            tasks.create_task(
                                prepare_process(owner, tree, mode, registrations)
                            )
            except* PairPreparationRefused:
                assert phase == "acquire_refused"
                preparation_refused = True
            if preparation_refused:
                report_stage("pair_preparation_refused")
                return
        elif mode != "tree":
            await prepare_process(owner, tree, mode, registrations)
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
        report_stage("report_ready")
        assert len(encoded) <= 65536
        os.write(1, len(encoded).to_bytes(4, "big"))
        if phase != "before_report":
            os.write(1, encoded[: len(encoded) // 2] if phase == "partial" else encoded)
        report_stage("awaiting_ack")
        transferred = await acknowledgment(owner.deadline()) == b"a"
        report_stage("ack_received")
    finally:
        if not transferred:
            assert clock is not None
            registered = {
                registration.process.pid: registration for registration in registrations
            }
            for record in owner.records.values():
                handle = record.process
                registration = handle.registration if handle is not None else None
                if registration is not None:
                    registered[registration.process.pid] = registration
            closing = owner.request_close(None)
            clock.advance(duration_ns(owner.limits.cooperative_seconds) + 1)
            assert (await asyncio.shield(closing)).state is CleanupOutcome.CLOSED
            reader = NativeIdentityReader()
            for registration in registered.values():
                assert (
                    reader.read(registration.process.pid).state is IdentityState.ABSENT
                )
                group = reader.group(registration.process_group_id)
                assert group.state is IdentityState.PRESENT
                assert group.members == ()
            assert owner.inventory().retained_processes == 0
            if phase == "acquire_refused":
                assert len(registered) == 2


if __name__ == "__main__":
    asyncio.run(
        retain(
            Path(sys.argv[1]),
            sys.argv[2],
            sys.argv[3] if len(sys.argv) > 3 else "complete",
        )
    )
