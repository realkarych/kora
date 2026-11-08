from enum import StrEnum

from kora.cel_eval.bounds import MAX_BYTES, admitted, measure
from kora.cel_eval.convert import convert
from kora.cel_eval.errors import ErrorKind, raise_safe
from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected, TextBudget
from kora.engine.graph_execution_contract import CheckedExecutionContract
from kora.engine.graph_types import read_document
from kora.engine.locks import COMPONENT
from kora.engine.run_input_values import RunInputValueRejected, decode_run_input_value
from kora.engine.start_parameter_cut import PreparedManualStartParameterCut
from kora.ir.cel_inventory_admission import (
    CheckedInventoryAdmission,
    InventoryAdmissionExhausted,
)
from kora.ir.expression_contexts import prepare_parameter_variable
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

CHECKED: EventSpec = CATALOG.register(
    EventSpec(
        "engine.start_parameter_cel.checked",
        COMPONENT,
        Level.DEBUG,
        "manual start CEL parameter projection checked",
        {"outcome": FieldKind.STRING},
    )
)


class ParameterCelFailure(StrEnum):
    BINDING = "binding"
    CAPACITY = "capacity"
    VIEW = "view"
    VALUES = "values"


class ParameterCelRejected(ValueError):
    def __init__(self, reason: ParameterCelFailure) -> None:
        super().__init__(reason.value)
        self.reason: ParameterCelFailure = reason


def _check(
    cut: PreparedManualStartParameterCut,
    checked: CheckedExecutionContract,
    max_ir_bytes: int,
    max_schema_work: int,
) -> ParameterCelFailure | None:
    if (
        type(cut) is not PreparedManualStartParameterCut
        or type(checked) is not CheckedExecutionContract
    ):
        return ParameterCelFailure.BINDING
    scope = checked.scope
    if (cut.namespace_id, cut.graph_id, cut.version_id) != (
        scope.namespace_id,
        scope.graph_id,
        scope.version_id,
    ) or cut.execution_contract_digest != checked.pin.execution_contract_digest:
        return ParameterCelFailure.BINDING
    if any(
        type(limit) is not int or limit <= 0
        for limit in (max_ir_bytes, max_schema_work)
    ):
        return ParameterCelFailure.CAPACITY
    paths = checked.parameter_paths
    payload = cut.projection_bytes()
    if not (paths.whole or paths.presence):
        return None if payload == b"{}" else ParameterCelFailure.VIEW
    try:
        document = read_document(checked.canonical_ir, TextBudget(max_ir_bytes))
        admission = CheckedInventoryAdmission(max_schema_work, 0)
        variable = prepare_parameter_variable(
            document.type_contracts, document.graph.params, admission
        )
        del document
        original = decode_run_input_value(payload, max_bytes=MAX_BYTES)
    except InventoryAdmissionExhausted:
        return ParameterCelFailure.CAPACITY
    except GraphValueRejected as rejection:
        return (
            ParameterCelFailure.CAPACITY
            if rejection.reason is GraphValueFailure.BUDGET
            else ParameterCelFailure.VIEW
        )
    except RunInputValueRejected:
        return ParameterCelFailure.VIEW
    if variable is None:
        return ParameterCelFailure.VIEW
    if isinstance(measure(original), ErrorKind):
        return ParameterCelFailure.VALUES
    converted = convert(original, variable.type)
    if (
        isinstance(converted, ErrorKind)
        or not admitted(converted, variable.bound)
        or isinstance(measure(converted), ErrorKind)
    ):
        return ParameterCelFailure.VALUES
    return None


def check_manual_start_parameter_cel(
    cut: PreparedManualStartParameterCut,
    checked: CheckedExecutionContract,
    *,
    max_ir_bytes: int,
    max_schema_work: int,
) -> PreparedManualStartParameterCut:
    failure = _check(cut, checked, max_ir_bytes, max_schema_work)
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(
            CHECKED, outcome="accepted" if failure is None else failure.value
        )
    if failure is None:
        return cut
    del cut, checked, max_ir_bytes, max_schema_work
    raise_safe(ParameterCelRejected(failure))
