from collections.abc import Callable
from typing import Final, TypeVar

from kora.plugins.runtime.local.environment import BoundCandidateEnvironments
from kora.protocols.errors import KoraProtocolError
from kora.protocols.time import Clock

T = TypeVar("T")
SLOT_UNAVAILABLE: Final = "the bound native role is unavailable"


class BoundSlotDeadline(KoraProtocolError):
    pass


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

    @property
    def stopped(self) -> bool:
        return self._stopped

    def stop(self) -> None:
        self._stopped = True

    def require_open(self) -> None:
        if self._stopped:
            raise _slot_refused()
        if self._clock.monotonic_ns() >= self._cutoff_ns:
            raise _slot_refused(BoundSlotDeadline)

    def claim(self, role: str) -> RoleClaim:
        self.require_open()
        if role not in self._roles or role in self._claims:
            raise _slot_refused()
        claim = RoleClaim()
        self._claims[role] = claim
        return claim

    def start(
        self,
        role: str,
        claim: RoleClaim,
        acquire: Callable[[dict[str, str]], T],
        *,
        preflight: Callable[[], None] | None = None,
        on_failure: Callable[[BaseException], None] | None = None,
    ) -> T:
        self.require_open()
        issued = self._claims.get(role)
        if issued is None or issued is not claim:
            raise _slot_refused()
        self._claims[role] = None
        try:
            if preflight is not None:
                preflight()
                self.require_open()
            environment = self._binding.materialize(role)
            self.require_open()
            return acquire(environment)
        except BaseException as error:
            try:
                if on_failure is not None:
                    on_failure(error)
            finally:
                self.stop()
            raise


def _slot_refused(
    error_type: type[KoraProtocolError] = KoraProtocolError,
) -> KoraProtocolError:
    return error_type("launch_rejected", SLOT_UNAVAILABLE, False, {})
