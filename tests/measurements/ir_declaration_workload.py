import hashlib
import json
from collections.abc import Callable
from copy import deepcopy

from pydantic import TypeAdapter, ValidationError

from kora.ir import Trigger, export_ir, render_ir_json
from kora.ir.json import JsonObject
from tests.ir.declaration_cases import INVALID, MINIMAL, POPULATED
from tests.ir.support import configuration_errors
from tests.measurements.ir_config_workload import validation_action
from tests.measurements.ir_remaining_workload import (
    Workload,
    canonical_round_trip,
    data_inventory,
)

TRIGGER_ARRAY = TypeAdapter(list[Trigger])
TRIGGER_CASES: tuple[dict[str, object], ...] = (
    {"kind": "manual"},
    {"kind": "cron", "expr": ""},
    {"kind": "webhook", "name": "", "auth": {"kind": "hmac", "secret": "TOKEN"}},
    {"kind": "sensor", "poll": {"sensor": "http", "until": "true"}, "interval": "0s"},
    {"kind": "graph", "graph": "", "on": "run_finished"},
)


def trigger_array(size: int) -> list[dict[str, object]]:
    return [deepcopy(TRIGGER_CASES[index % 5]) for index in range(size)]


def invalid_trigger_array(position: str, size: int = 1000) -> list[dict[str, object]]:
    documents = trigger_array(size)
    if position == "all":
        return [{**document, "kind": "wrong"} for document in documents]
    index = 0 if position == "first" else size - 1
    documents[index] = {**documents[index], "kind": "wrong"}
    return documents


def validate_triggers(documents: list[dict[str, object]], valid: bool = True) -> object:
    try:
        parsed = TRIGGER_ARRAY.validate_python(documents)
    except ValidationError as error:
        assert not valid
        errors = configuration_errors(error)
        assert errors
        return errors
    assert valid
    return parsed


def trigger_export(documents: list[dict[str, object]]) -> Callable[[], object]:
    records = TRIGGER_ARRAY.validate_python(documents)
    return lambda: [export_ir(record) for record in records]


def declaration_workloads() -> dict[str, Workload]:
    workloads: dict[str, Workload] = {}
    for shape, configurations in (("minimal", MINIMAL), ("populated", POPULATED)):
        for model, document in configurations.items():
            workloads[f"declaration_{model.__name__}_{shape}_round_trip"] = (
                canonical_round_trip(model, document),
                1,
                True,
                30,
            )
    for model, document in INVALID.items():
        workloads[f"declaration_{model.__name__}_refused"] = (
            validation_action(model, document, False),
            1,
            True,
            10,
        )
    for size in (1, 1000, 10000):
        documents = trigger_array(size)
        workloads[f"declaration_triggers_{size}_validate"] = (
            lambda documents=documents: validate_triggers(documents),
            size,
            True,
            10,
        )
        workloads[f"declaration_triggers_{size}_export"] = (
            trigger_export(documents),
            size,
            True,
            10,
        )
    for position in ("first", "last", "all"):
        documents = invalid_trigger_array(position)
        workloads[f"declaration_triggers_1000_refused_{position}"] = (
            lambda documents=documents: validate_triggers(documents, False),
            1000,
            True,
            10,
        )
    return workloads


def fixture_row(encoded: bytes, document: object) -> dict[str, object]:
    containers, depth = data_inventory(document)
    return {
        "bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "containers": containers,
        "container_depth": depth,
    }


def declaration_fixture_manifest() -> str:
    fixtures: dict[str, object] = {}
    for shape, configurations in (("minimal", MINIMAL), ("populated", POPULATED)):
        for model, document in configurations.items():
            encoded = render_ir_json(model.model_validate(document)).encode("ascii")
            fixtures[f"{model.__name__}_{shape}"] = fixture_row(encoded, document)
    for model, document in INVALID.items():
        encoded = json.dumps(
            document, ensure_ascii=True, allow_nan=False, separators=(",", ":")
        ).encode("ascii")
        fixtures[f"{model.__name__}_refused"] = fixture_row(encoded, document)
    for size in (1, 1000, 10000):
        documents = trigger_array(size)
        records = TRIGGER_ARRAY.validate_python(documents)
        encoded = (
            "[" + ",".join(render_ir_json(record) for record in records) + "]"
        ).encode("ascii")
        fixtures[f"triggers_{size}"] = {
            **fixture_row(encoded, documents),
            "triggers": size,
        }
    for position in ("first", "last", "all"):
        documents = invalid_trigger_array(position)
        encoded = json.dumps(
            documents, ensure_ascii=True, separators=(",", ":")
        ).encode("ascii")
        fixtures[f"triggers_1000_refused_{position}"] = {
            **fixture_row(encoded, documents),
            "triggers": 1000,
        }
    return json.dumps(fixtures, sort_keys=True, separators=(",", ":"))


LIFETIME_TRIGGERS: list[dict[str, object]] = trigger_array(5)
LIFETIME_INVALID: list[dict[str, object]] = invalid_trigger_array("last", 5)


def trigger_lifetime_cycle() -> list[Trigger]:
    records = TRIGGER_ARRAY.validate_python(LIFETIME_TRIGGERS)
    exported: list[JsonObject] = [export_ir(record) for record in records]
    assert len(exported) == 5
    validate_triggers(LIFETIME_INVALID, False)
    return records
