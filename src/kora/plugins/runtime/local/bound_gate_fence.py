from kora.plugins.runtime.local.bound_slots import BoundRoleSlots
from kora.runner.processes import GatedProcess


class BoundGateFence:
    def __init__(self, slots: BoundRoleSlots) -> None:
        self._slots = slots
        self._gates: list[GatedProcess] = []

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
