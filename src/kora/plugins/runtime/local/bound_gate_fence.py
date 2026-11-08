from enum import StrEnum

from kora.plugins.runtime.local.bound_checkout import CheckoutFailure
from kora.plugins.runtime.local.bound_role_configuration import RoleConfigurationFailure
from kora.plugins.runtime.local.bound_slots import BoundRoleSlots
from kora.runner.channels import ChannelFailure
from kora.runner.processes import GatedProcess, ProcessCondition


class GatePhaseFailure(StrEnum):
    CANCELLED = "cancelled"
    UNEXPECTED = "unexpected"


class GateAcquisitionFailure(StrEnum):
    REJECTED = "acquisition_rejected"
    GRANT_UNAVAILABLE = "acquisition_grant_unavailable"
    OS_ERROR = "acquisition_os_error"


BoundGateFailure = (
    GatePhaseFailure
    | GateAcquisitionFailure
    | CheckoutFailure
    | RoleConfigurationFailure
    | ChannelFailure
    | ProcessCondition
)


class BoundGateFence:
    def __init__(self, slots: BoundRoleSlots) -> None:
        self._slots = slots
        self._gates: list[GatedProcess] = []
        self._failure: BoundGateFailure | None = None

    @property
    def failure(self) -> BoundGateFailure | None:
        return self._failure

    def record_failure(self, cause: BoundGateFailure) -> None:
        if not self.stopped:
            self._failure = cause

    def fail(self, cause: BoundGateFailure) -> None:
        if not self.stopped:
            self.record_failure(cause)
            self.stop()

    @property
    def stopped(self) -> bool:
        return self._slots.stopped

    @property
    def gates(self) -> tuple[GatedProcess, ...]:
        return tuple(self._gates)

    @property
    def close_failed(self) -> bool:
        return any(gate.gate_close_failed for gate in self._gates)

    def register(self, gate: GatedProcess) -> None:
        self._gates.append(gate)
        if self._slots.stopped:
            _close_for_stop(gate)

    def stop(self) -> None:
        self._slots.stop()
        for gate in self._gates:
            _close_for_stop(gate)


def _close_for_stop(gate: GatedProcess) -> None:
    try:
        gate.close_gate()
    except OSError:
        return
