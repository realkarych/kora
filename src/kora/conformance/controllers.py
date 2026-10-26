import asyncio
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from kora.conformance.io_helper import (
    BootstrapMutation,
    HelperError,
    IOHelper,
    IOHelperLauncher,
    Mutation,
    MutationResult,
    PublishMutation,
    RecoverMutation,
)
from kora.conformance.journal import JournalSnapshot
from kora.protocols.identity import ProcessIdentity
from kora.protocols.lifetime import (
    CleanupReference,
    LifetimeLimits,
    LifetimeReason,
    ResourceId,
)
from kora.protocols.time import Clock
from kora.runner.channels import ChannelError
from kora.runner.filesystem import DirectoryAnchor, DirectoryIdentity
from kora.runner.processes import ProcessError

HELPER_ORIGIN_BYTES: int = 2048


def duration_ns(seconds: float) -> int:
    numerator, denominator = seconds.as_integer_ratio()
    return numerator * 1_000_000_000 // denominator


@dataclass
class CleanupPermit:
    resource_id: ResourceId | None
    final: int


@dataclass
class HelperOrigin:
    task: asyncio.Task[object] | None
    birth: int
    cleanup: CleanupPermit | None
    term: int | None = None
    kill: int | None = None
    final: int | None = None
    signalled: int = 0

    def tighten(
        self, cooperative: int, term: int, final: int, limits: LifetimeLimits
    ) -> None:
        if self.cleanup is None:
            stop, kill = cooperative, term
        else:
            remaining = final - self.birth
            stop = min(
                final - 2,
                max(
                    cooperative,
                    self.birth
                    + min(duration_ns(limits.cooperative_seconds), remaining // 4),
                ),
            )
            kill = min(
                final - 1,
                max(
                    stop + 1,
                    term,
                    stop + min(duration_ns(limits.term_seconds), (final - stop) // 3),
                ),
            )
            self.cleanup.final = min(self.cleanup.final, final)
        self.term = stop if self.term is None else min(self.term, stop)
        self.kill = kill if self.kill is None else min(self.kill, kill)
        self.final = final if self.final is None else min(self.final, final)


@dataclass(frozen=True)
class HelperInventory:
    live_helpers: int
    retained_helpers: int
    pending_mutations: int
    directory_descriptors: int
    claim_descriptors: int
    journal_bytes: int
    retained_origins: int
    cleanup_permits: int


class MutationOwner:
    def __init__(
        self,
        launcher: IOHelperLauncher,
        clock: Clock,
        limits: LifetimeLimits,
        changed: asyncio.Event | None = None,
    ) -> None:
        self.launcher = launcher
        self.clock = clock
        self.limits = limits
        self.root: DirectoryAnchor | None = None
        self.expected_root: DirectoryIdentity | None = None
        self.directory: DirectoryAnchor | None = None
        self.claim: int | None = None
        self.snapshot: JournalSnapshot | None = None
        self.helpers: list[IOHelper] = []
        self.origins: dict[IOHelper, HelperOrigin] = {}
        self.cleanup_task: asyncio.Task[None] | None = None
        self.cleanup_permit: CleanupPermit | None = None
        self.changed = changed
        self.pending = 0
        self.failed = False
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def _mutation_slot(self, deadline: int) -> AsyncGenerator[None]:
        if self.clock.monotonic_ns() >= deadline:
            raise HelperError(LifetimeReason.JOURNAL_UNAVAILABLE)
        acquisition = asyncio.create_task(self._lock.acquire())
        timeout = asyncio.create_task(self.clock.sleep_until(deadline))
        try:
            await asyncio.wait(
                (acquisition, timeout), return_when=asyncio.FIRST_COMPLETED
            )
            if not acquisition.done() or self.clock.monotonic_ns() >= deadline:
                raise HelperError(LifetimeReason.JOURNAL_UNAVAILABLE)
            acquisition.result()
            yield
        finally:
            acquisition.cancel()
            timeout.cancel()
            try:
                granted = await acquisition
            except asyncio.CancelledError:
                if not acquisition.cancelled():
                    raise
            else:
                if granted:
                    self._lock.release()
            try:
                await timeout
            except asyncio.CancelledError:
                if not timeout.cancelled():
                    raise

    async def bootstrap(
        self, snapshot: JournalSnapshot, deadline: int
    ) -> JournalSnapshot:
        result = await self.perform(
            BootstrapMutation(
                invocation_id=snapshot.invocation_id,
                journal=snapshot,
                capacity=self.limits.max_state_root_invocations,
            ),
            None,
            deadline,
        )
        helper = self.helpers[-1]
        if result.root is None or result.directory is None or result.journal is None:
            raise HelperError(LifetimeReason.JOURNAL_UNAVAILABLE)
        root, directory, claim = helper.take_descriptors()
        self.root = DirectoryAnchor(root, result.root)
        self.directory = DirectoryAnchor(directory, result.directory)
        self.claim = claim
        self.snapshot = result.journal
        self.prune()
        return result.journal

    async def recover(
        self, reference: CleanupReference, owner: ProcessIdentity, deadline: int
    ) -> JournalSnapshot:
        result = await self.perform(
            RecoverMutation(
                invocation_id=reference.invocation_id,
                owner=owner,
                expected_root=self.expected_root,
            ),
            None,
            deadline,
        )
        helper = self.helpers[-1]
        if result.root is None or result.directory is None or result.journal is None:
            raise HelperError(LifetimeReason.JOURNAL_UNAVAILABLE)
        root, directory, claim = helper.take_descriptors()
        self.root = DirectoryAnchor(root, result.root)
        self.directory = DirectoryAnchor(directory, result.directory)
        self.claim = claim
        self.snapshot = result.journal
        self.prune()
        return result.journal

    async def publish(self, snapshot: JournalSnapshot, deadline: int) -> None:
        directory = self.directory
        if directory is None:
            raise HelperError(LifetimeReason.JOURNAL_UNAVAILABLE)
        if (
            len(snapshot.model_dump_json().encode())
            > self.limits.max_control_recovery_bytes
        ):
            raise HelperError(LifetimeReason.CAPACITY_EXCEEDED)
        result = await self.perform(
            PublishMutation(
                directory=directory.identity,
                revision=snapshot.revision,
                journal=snapshot,
            ),
            directory,
            deadline,
        )
        if result.journal is None:
            raise HelperError(LifetimeReason.JOURNAL_UNAVAILABLE)
        self.snapshot = result.journal
        self.prune()

    def take_directory(self, result: MutationResult) -> DirectoryAnchor:
        helper = next(
            (helper for helper in self.helpers if helper.result is result), None
        )
        if (
            result.directory is None
            or result.descriptor_count != 1
            or helper is None
            or not helper.closed
            or len(helper.descriptors) != 1
        ):
            raise HelperError(LifetimeReason.JOURNAL_UNAVAILABLE)
        (descriptor,) = helper.take_descriptors()
        directory = DirectoryAnchor(descriptor, result.directory)
        self.prune()
        return directory

    async def perform(
        self, mutation: Mutation, directory: DirectoryAnchor | None, deadline: int
    ) -> MutationResult:
        if self.failed:
            raise HelperError(LifetimeReason.JOURNAL_UNAVAILABLE)
        if self.pending >= self.limits.max_outstanding_operations:
            raise HelperError(LifetimeReason.CAPACITY_EXCEEDED)
        task = asyncio.current_task()
        if task is None:
            raise HelperError(LifetimeReason.EFFECT_UNSETTLED)
        permit = self.cleanup_permit if task is self.cleanup_task else None
        self.pending += 1
        try:
            async with self._mutation_slot(deadline):
                deadline = deadline if permit is None else min(deadline, permit.final)
                if self.failed or self.clock.monotonic_ns() >= deadline:
                    raise HelperError(LifetimeReason.JOURNAL_UNAVAILABLE)
                if permit is not None and deadline - self.clock.monotonic_ns() < 4:
                    raise HelperError(LifetimeReason.EFFECT_UNSETTLED)
                if len(self.helpers) >= self.limits.max_outstanding_operations + 1:
                    raise HelperError(LifetimeReason.CAPACITY_EXCEEDED)
                descriptors: tuple[int, ...] = ()
                if directory is not None:
                    if self.claim is None:
                        raise HelperError(LifetimeReason.JOURNAL_UNAVAILABLE)
                    descriptors = (directory.descriptor, self.claim)
                helper = self.launcher.start(descriptors, self.clock)
                self.helpers.append(helper)
                self.origins[helper] = HelperOrigin(
                    task if permit is None else None, self.clock.monotonic_ns(), permit
                )
                if self.changed is not None:
                    self.changed.set()
                try:
                    return await helper.execute(mutation, deadline)
                except (ChannelError, HelperError, ProcessError, OSError):
                    self.failed = True
                    raise
        finally:
            self.pending -= 1

    def prune(self) -> None:
        self.helpers = [
            helper for helper in self.helpers if not helper.closed or helper.descriptors
        ]
        self.origins = {
            helper: origin
            for helper, origin in self.origins.items()
            if helper in self.helpers
        }

    async def settle(self, deadline: int) -> bool:
        for helper in self.helpers:
            if not helper.closed:
                await helper.settle(deadline)
        self.prune()
        return not self.helpers and self.pending == 0

    def inventory(self) -> HelperInventory:
        return HelperInventory(
            live_helpers=sum(not helper.anchor.exited for helper in self.helpers),
            retained_helpers=len(self.helpers),
            pending_mutations=self.pending,
            directory_descriptors=sum(
                anchor is not None and not anchor.closed
                for anchor in (self.root, self.directory)
            ),
            claim_descriptors=int(self.claim is not None),
            journal_bytes=0
            if self.snapshot is None
            else len(self.snapshot.model_dump_json().encode()),
            retained_origins=len(self.origins),
            cleanup_permits=len(
                {
                    id(origin.cleanup)
                    for origin in self.origins.values()
                    if origin.cleanup is not None
                }
                | (
                    {id(self.cleanup_permit)}
                    if self.cleanup_permit is not None
                    else set[int]()
                )
            ),
        )

    def release(self) -> None:
        self.prune()
        if self.helpers or self.pending:
            raise HelperError(LifetimeReason.EFFECT_UNSETTLED)
        if self.claim is not None:
            os.close(self.claim)
            self.claim = None
        if self.directory is not None:
            self.directory.close()
        if self.root is not None:
            self.root.close()

    def dispose_settled(self) -> bool:
        if self.pending or any(not helper.closed for helper in self.helpers):
            return False
        for helper in self.helpers:
            for descriptor in helper.take_descriptors():
                os.close(descriptor)
        self.release()
        return True
