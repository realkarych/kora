from dataclasses import dataclass

from kora.db.connection import Database
from kora.db.run_input_reads import (
    RunInputBinding,
    RunInputField,
    RunInputReadLimits,
    acquire_run_input,
)
from kora.engine.locks import COMPONENT
from kora.engine.run_input_values import RunInputValueRejected, decode_run_input_value
from kora.engine.start_records import MAX_PARAMS_BYTES
from kora.ir.json import JsonObject
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics

CHECKED: EventSpec = CATALOG.register(
    EventSpec(
        "engine.run_input.checked",
        COMPONENT,
        Level.DEBUG,
        "run input checked",
        {"verified": FieldKind.BOOLEAN},
    )
)


@dataclass(frozen=True, slots=True, repr=False)
class ReadRunInput:
    binding: RunInputBinding
    field: RunInputField
    value: JsonObject


async def _read(
    database: Database,
    binding: RunInputBinding,
    field: RunInputField,
    limits: RunInputReadLimits,
) -> ReadRunInput | None:
    if not all(
        type(limit) is int and 0 < limit <= MAX_PARAMS_BYTES
        for limit in (limits.projection, limits.params)
    ):
        return None
    selection = RunInputBinding(
        binding.namespace_id, binding.graph_id, binding.version_id, binding.run_id
    )
    limit = limits.projection if field is RunInputField.PROJECTION else limits.params
    payload = await acquire_run_input(database, selection, field, limits)
    if payload is None:
        return None
    try:
        value = decode_run_input_value(payload, max_bytes=limit)
    except RunInputValueRejected:
        return None
    return ReadRunInput(selection, field, value)


async def read_run_input(
    database: Database,
    binding: RunInputBinding,
    field: RunInputField,
    limits: RunInputReadLimits,
) -> ReadRunInput | None:
    result = await _read(database, binding, field, limits)
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(CHECKED, verified=result is not None)
    return result
