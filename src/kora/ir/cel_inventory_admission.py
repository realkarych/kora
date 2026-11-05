from dataclasses import dataclass

from kora.cel import CheckedExpression
from kora.cel.meter import Meter
from kora.ir.expression_fields import FieldPath


class InventoryAdmissionExhausted(Exception):
    def __init__(self) -> None:
        super().__init__("graph CEL typed inventory allowance exhausted")


@dataclass(slots=True)
class CheckedInventoryAdmission:
    remaining_work: int
    remaining_bytes: int

    def charge_work(self, units: int) -> None:
        if units > self.remaining_work:
            raise InventoryAdmissionExhausted
        self.remaining_work -= units

    def retain(self, path: FieldPath, checked: CheckedExpression) -> None:
        size = (
            4 * len(checked.rendering)
            + checked.bound_bytes
            + checked.projection_bytes
            + len(path)
            + sum(4 * len(segment) for segment in path if isinstance(segment, str))
        )
        if size > self.remaining_bytes:
            raise InventoryAdmissionExhausted
        self.remaining_bytes -= size


class InventoryMeter(Meter):
    def __init__(self, admission: CheckedInventoryAdmission) -> None:
        super().__init__()
        self.admission: CheckedInventoryAdmission = admission

    def tick(self, count: int = 1) -> None:
        self.admission.charge_work(count)
        super().tick(count)


def inventory_meter(admission: CheckedInventoryAdmission | None) -> Meter:
    return Meter() if admission is None else InventoryMeter(admission)


def retain_checked_site(
    admission: CheckedInventoryAdmission | None,
    path: FieldPath,
    checked: CheckedExpression,
) -> None:
    if admission is not None:
        admission.retain(path, checked)
