import os
import select
import signal
import sys
import time
from collections.abc import Callable
from enum import StrEnum
from typing import Final, Protocol

from kora.protocols.identity_records import ProcessIdentity
from kora.protocols.time import Clock
from kora.runner.identity import (
    PROCESS_SCAN_ENTRIES,
    SCAN_NS,
    CensusCondition,
    CommandRead,
    IdentityState,
    NativeIdentityReader,
)

GRACE_NS: Final[int] = 5_000_000_000
POLL_SECONDS: Final[float] = 0.05
CENSUS_RETRY_NS: Final[int] = 250_000_000
STOP_DETAIL_FIELDS: Final[tuple[str, ...]] = (
    *(condition.value for condition in CensusCondition),
    "last_census_available",
    "last_census_complete",
    "members_retained",
    "group_ambiguous",
    "member_capacity",
    "stop_cutoff",
)
STOP_STATUS_FIELDS: Final[tuple[str, ...]] = (
    "stop_details_available",
    "native_run_unresolved",
    "native_close_unresolved",
)


class StopOutcome(StrEnum):
    CONFIRMED = "confirmed"
    UNRESOLVED = "unresolved"


class DomainStop(Protocol):
    def stop(self, started_ns: int) -> StopOutcome: ...


DomainStopFactory = Callable[
    [int, bytes, Clock, Callable[[], None], dict[int, int] | None], DomainStop
]


class CommandDomainStop:
    def __init__(
        self,
        group: int,
        marker: bytes,
        clock: Clock,
        wake_reaper: Callable[[], None],
        prelaunch: dict[int, int] | None = None,
    ) -> None:
        self.group = group
        self.marker = marker
        self.clock = clock
        self.wake_reaper = wake_reaper
        self.reader = NativeIdentityReader(clock=clock)
        self.prelaunch = prelaunch
        self.members: set[ProcessIdentity] = set()
        self.ambiguous_group = False
        self.next_reap_wake = 0
        self.observed: set[CensusCondition] = set()
        self.last_census_complete: bool | None = None
        self.member_capacity = False
        self.stop_cutoff = False

    def diagnostic_snapshot(self) -> tuple[tuple[str, bool], ...]:
        return (
            *(
                (condition.value, condition in self.observed)
                for condition in CensusCondition
            ),
            ("last_census_available", self.last_census_complete is not None),
            ("last_census_complete", self.last_census_complete is True),
            ("members_retained", bool(self.members)),
            ("group_ambiguous", self.ambiguous_group),
            ("member_capacity", self.member_capacity),
            ("stop_cutoff", self.stop_cutoff),
        )

    def stop(self, started_ns: int) -> StopOutcome:
        escalate = started_ns + GRACE_NS
        if self._phase(signal.SIGTERM, escalate):
            return StopOutcome.CONFIRMED
        if self._phase(signal.SIGKILL, escalate + SCAN_NS):
            return StopOutcome.CONFIRMED
        return StopOutcome.UNRESOLVED

    def _phase(self, requested: signal.Signals, cutoff: int) -> bool:
        signalled: set[ProcessIdentity] = set()
        next_census = self.clock.monotonic_ns()
        if requested is signal.SIGKILL:
            self._signal_members(requested, cutoff, signalled)
        while self.clock.monotonic_ns() < cutoff:
            had_members = bool(self.members)
            self._forget_stopped(cutoff)
            if self.clock.monotonic_ns() >= cutoff:
                self.stop_cutoff = True
                return False
            signalled.intersection_update(self.members)
            if self.clock.monotonic_ns() >= next_census or (
                had_members and not self.members
            ):
                census = self.reader.command_census(
                    self.group,
                    self.marker,
                    cutoff,
                    prelaunch=self.prelaunch,
                    known_pids=(
                        frozenset(identity.pid for identity in self.members)
                        if self.prelaunch
                        else frozenset()
                    ),
                )
                self.ambiguous_group = self.ambiguous_group or census.ambiguous_group
                self.observed.update(census.observed)
                self.last_census_complete = census.complete
                members = {member.process.identity for member in census.members}
                if len(self.members | members) > PROCESS_SCAN_ENTRIES:
                    self.member_capacity = True
                    self._signal_members(requested, cutoff, signalled)
                    return False
                self.members.update(members)
                if census.complete and not self.members and not self.ambiguous_group:
                    in_time = self.clock.monotonic_ns() < cutoff
                    self.stop_cutoff = self.stop_cutoff or not in_time
                    return in_time
                self._signal_members(requested, cutoff, signalled)
                next_census = self.clock.monotonic_ns() + CENSUS_RETRY_NS
            remaining = (cutoff - self.clock.monotonic_ns()) / 1_000_000_000
            if remaining > 0:
                time.sleep(min(remaining, POLL_SECONDS))
        self.stop_cutoff = True
        return False

    def _forget_stopped(self, cutoff: int) -> None:
        needs_reap = False
        for identity in tuple(self.members):
            if self.clock.monotonic_ns() >= cutoff:
                break
            observed = self.reader.read(identity.pid)
            current = observed.process
            if observed.state is IdentityState.ABSENT or (
                current is not None and current.identity != identity
            ):
                self.members.remove(identity)
            elif current is not None and current.dead:
                if sys.platform == "linux" and current.parent_pid == os.getpid():
                    needs_reap = True
                else:
                    self.members.remove(identity)
        now = self.clock.monotonic_ns()
        if needs_reap and self.next_reap_wake <= now < cutoff:
            self.wake_reaper()
            self.next_reap_wake = now + CENSUS_RETRY_NS

    def _signal_members(
        self, requested: signal.Signals, cutoff: int, signalled: set[ProcessIdentity]
    ) -> None:
        delivered = False
        for identity in self.members - signalled:
            if self.clock.monotonic_ns() >= cutoff:
                break
            if self._signal_member(identity, requested, cutoff):
                signalled.add(identity)
                delivered = True
        if delivered:
            self.wake_reaper()

    def _signal_member(
        self, identity: ProcessIdentity, requested: signal.Signals, cutoff: int
    ) -> bool:
        try:
            if sys.platform != "linux":
                if not self._may_signal(identity, cutoff):
                    return False
                os.kill(identity.pid, requested)
                return True
            descriptor = os.pidfd_open(identity.pid)
            try:
                readiness = select.poll()
                readiness.register(descriptor, select.POLLIN)
                if (
                    readiness.poll(0)
                    or not self._may_signal(identity, cutoff)
                    or readiness.poll(0)
                ):
                    return False
                signal.pidfd_send_signal(descriptor, requested)
                return True
            finally:
                os.close(descriptor)
        except OSError:
            return False

    def _may_signal(self, identity: ProcessIdentity, cutoff: int) -> bool:
        member, observation = self.reader.command_member(
            identity.pid, self.group, self.marker
        )
        return (
            observation is CommandRead.VERIFIED
            and member is not None
            and member.owned
            and member.process.identity == identity
            and self.clock.monotonic_ns() < cutoff
        )
