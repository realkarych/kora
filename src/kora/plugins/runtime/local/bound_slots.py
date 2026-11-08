from collections.abc import Callable
from typing import TypeVar

from kora.plugins.runtime.local.environment import BoundCandidateEnvironments
from kora.protocols.errors import KoraProtocolError
from kora.protocols.time import Clock

T = TypeVar("T")


class RoleClaim:
    __slots__ = ()


class BoundRoleSlots:
    def __init__(
        self, binding: BoundCandidateEnvironments, clock: Clock, cutoff_ns: int
    ) -> None:
        self._binding = binding
        self._roles = binding.roles
        self._clock = clock
        self._cutoff_ns = cutoff_ns
        self._claims: dict[str, RoleClaim | None] = {}
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    def _require_open(self) -> None:
        if self._stopped or self._clock.monotonic_ns() >= self._cutoff_ns:
            raise _slot_refused()

    def claim(self, role: str) -> RoleClaim:
        self._require_open()
        if role not in self._roles or role in self._claims:
            raise _slot_refused()
        claim = RoleClaim()
        self._claims[role] = claim
        return claim

    def start(
        self, role: str, claim: RoleClaim, acquire: Callable[[dict[str, str]], T]
    ) -> T:
        self._require_open()
        issued = self._claims.get(role)
        if issued is None or issued is not claim:
            raise _slot_refused()
        self._claims[role] = None
        started = False
        try:
            environment = self._binding.materialize(role)
            self._require_open()
            resource = acquire(environment)
            started = True
            return resource
        finally:
            if not started:
                self.stop()


def _slot_refused() -> KoraProtocolError:
    return KoraProtocolError(
        "launch_rejected", "the bound native role is unavailable", False, {}
    )
