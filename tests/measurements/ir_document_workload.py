import json
from collections.abc import Callable
from copy import deepcopy
from typing import cast

from pydantic import BaseModel

from kora.ir import (
    GraphDocument,
    Provenance,
    export_ir,
    inspect_ir_root,
    render_ir_json,
)
from kora.ir.record import IrRecord
from tests.ir.declaration_cases import BEHAVIOR, DIALECT
from tests.ir.document_cases import INVALID, MINIMAL, NODE_CASES, POPULATED, document
from tests.measurements.ir_config_workload import validate_config, validation_action
from tests.measurements.ir_declaration_workload import fixture_row
from tests.measurements.ir_remaining_workload import Workload, data_inventory

Root = IrRecord | Provenance


def parsed_root(model: type[BaseModel], value: dict[str, object]) -> Root:
    parsed = model.model_validate(value)
    assert isinstance(parsed, IrRecord | Provenance)
    return parsed


def round_trip(
    model: type[BaseModel], value: dict[str, object]
) -> Callable[[], object]:
    return lambda: export_ir(parsed_root(model, value))


def materialized_nodes(
    count: int,
    prefix: str,
    compositions: bool = False,
    body: str = "body_a",
    wide: bool = False,
) -> dict[str, object]:
    kinds = tuple(NODE_CASES) if compositions else tuple(NODE_CASES)[:6]
    nodes: dict[str, object] = {}
    for index in range(count):
        kind = kinds[index % len(kinds)]
        node = deepcopy({"kind": kind, **NODE_CASES[kind]})
        if index and (not wide or kinds[(index - 1) % len(kinds)] == "branch"):
            node["depends_on"] = [f"{prefix}{index - 1:06d}"]
        if wide and index == count - 1:
            node["depends_on"] = [
                f"{prefix}{previous:06d}" for previous in range(index)
            ]
        if kind == "branch":
            target = (
                f"{prefix}{index + 1:06d}" if index + 1 < count else "$fail_attempt"
            )
            node["config"] = {"cases": [{"when": "true", "to": [target]}]}
        if kind in ("fan_out", "subgraph"):
            node["config"] = {
                "body": body,
                **({"over": "items"} if kind == "fan_out" else {}),
            }
            node["inputs"] = {
                "items": {
                    "type": "List[StructuredOutput]",
                    "from": "$inputs",
                    "path": "items",
                }
            }
        nodes[f"{prefix}{index:06d}"] = node
    return nodes


def custom_contract() -> dict[str, object]:
    return {
        "schema": {
            "$schema": DIALECT,
            "type": "object",
            "properties": {"value": {"type": "string"}},
        },
        "parent": None,
        "behavior": deepcopy(BEHAVIOR),
    }


def materialized_document(
    size: int, nested: bool = False, wide: bool = False
) -> dict[str, object]:
    value = document()
    graph = cast("dict[str, object]", value["graph"])
    graph["nodes"] = materialized_nodes(
        800 if nested and size == 1000 else 8 if nested else size,
        "n",
        nested,
        wide=wide,
    )
    value["type_contracts"] = {
        name: {"builtin_revision": "1"}
        for name in ("AgentSession", "CommandResult", "StructuredOutput")
    }
    if nested:
        graph["subgraphs"] = {
            "body_a": {
                "nodes": materialized_nodes(
                    100 if size == 1000 else 8, "a", True, "body_b"
                )
            },
            "body_b": {"nodes": materialized_nodes(100 if size == 1000 else 6, "b")},
        }
        contracts = cast("dict[str, object]", value["type_contracts"])
        contracts["custom/First"] = custom_contract()
        contracts["custom/Second"] = custom_contract()
    return value


def refused_document(position: str, size: int = 1000) -> dict[str, object]:
    value = materialized_document(size)
    nodes = cast(
        "dict[str, dict[str, object]]",
        cast("dict[str, object]", value["graph"])["nodes"],
    )
    names = (
        list(nodes)
        if position == "all"
        else [next(iter(nodes)) if position == "first" else next(reversed(nodes))]
    )
    for name in names:
        nodes[name]["config"] = False
    return value


def ignored_root(cyclic: bool) -> dict[str, object]:
    ignored: object = "a" * 3145728
    if cyclic:
        cycle: dict[str, object] = {}
        cycle["self"] = cycle
        ignored = cycle
    return {**document(), "ignored": ignored}


def contract_document(size: int) -> dict[str, object]:
    return {
        **document(),
        "type_contracts": {
            f"custom/Type{index}": custom_contract() for index in range(size)
        },
    }


def inspected_validation(value: dict[str, object]) -> GraphDocument:
    assert inspect_ir_root(value) == 1
    return GraphDocument.model_validate(value)


def document_workloads() -> dict[str, Workload]:
    workloads: dict[str, Workload] = {}
    for shape, fixtures in (("minimal", MINIMAL), ("populated", POPULATED)):
        for model, value in fixtures.items():
            workloads[f"document_{model.__name__}_{shape}_round_trip"] = (
                round_trip(model, value),
                1,
                True,
                30,
            )
    for model, value in INVALID.items():
        workloads[f"document_{model.__name__}_refused"] = (
            validation_action(model, value, False),
            1,
            True,
            10,
        )
    scales = {f"flat_{size}": materialized_document(size) for size in (1, 100, 1000)}
    scales["wide_1000"] = materialized_document(1000, wide=True)
    scales["nested_1000"] = materialized_document(1000, nested=True)
    scales.update(
        (f"contracts_{size}", contract_document(size)) for size in (1, 100, 1000)
    )
    for name, value in scales.items():
        parsed = GraphDocument.model_validate(value)
        workloads[f"document_{name}_validate"] = (
            validation_action(GraphDocument, value),
            1000 if name.endswith("1000") else 100 if name.endswith("100") else 1,
            True,
            10,
        )
        workloads[f"document_{name}_export"] = (
            lambda parsed=parsed: export_ir(parsed),
            1000 if name.endswith("1000") else 100 if name.endswith("100") else 1,
            True,
            10,
        )
        if name.startswith("flat") or name.startswith("nested"):
            workloads[f"document_{name}_render"] = (
                lambda parsed=parsed: render_ir_json(parsed),
                1000 if name.endswith("1000") else 100 if name.endswith("100") else 1,
                True,
                10,
            )
    for position in ("first", "last", "all"):
        workloads[f"document_flat_1000_refused_{position}"] = (
            validation_action(GraphDocument, refused_document(position), False),
            1000,
            True,
            10,
        )
    for cyclic in (False, True):
        value = ignored_root(cyclic)
        workloads[
            f"document_ignored_{'cycle' if cyclic else '3mib'}_inspect_validate"
        ] = (lambda value=value: inspected_validation(value), 1, True, 10)
    assert len(workloads) == 64
    assert sum(row[3] for row in workloads.values()) == 1160
    return workloads


def graph_inventory(value: dict[str, object]) -> dict[str, object]:
    graph = cast("dict[str, object]", value["graph"])
    bodies = cast("dict[str, dict[str, object]]", graph.get("subgraphs", {}))
    scopes = [graph, *bodies.values()]
    kinds: dict[str, int] = {}
    controls = 0
    producers = 0
    compositions = 0
    for scope in scopes:
        nodes = cast("dict[str, dict[str, object]]", scope["nodes"])
        for node in nodes.values():
            kind = cast("str", node["kind"])
            kinds[kind] = kinds.get(kind, 0) + 1
            controls += len(cast("list[str]", node.get("depends_on", [])))
            compositions += int(kind in ("fan_out", "subgraph"))
            inputs = cast("dict[str, dict[str, object]]", node.get("inputs", {}))
            producers += sum(
                not cast("str", reference.get("from", "$inputs")).startswith("$")
                for reference in inputs.values()
            )
    return {
        "nodes": sum(kinds.values()),
        "kinds": kinds,
        "control_references": controls,
        "producer_references": producers,
        "composition_references": compositions,
        "body_definitions": len(bodies),
    }


def document_fixture_manifest() -> str:
    fixtures: dict[str, object] = {}
    for shape, values in (("minimal", MINIMAL), ("populated", POPULATED)):
        for model, value in values.items():
            parsed = parsed_root(model, value)
            encoded = json.dumps(
                value, ensure_ascii=True, separators=(",", ":")
            ).encode("ascii")
            canonical = render_ir_json(parsed).encode("ascii")
            fixtures[f"{model.__name__}_{shape}"] = {
                **fixture_row(encoded, value),
                "canonical_projection": fixture_row(canonical, export_ir(parsed)),
            }
    for model, value in INVALID.items():
        encoded = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode(
            "ascii"
        )
        fixtures[f"{model.__name__}_refused"] = fixture_row(encoded, value)
    documents = {f"flat_{size}": materialized_document(size) for size in (1, 100, 1000)}
    documents.update(
        {
            "wide_1000": materialized_document(1000, wide=True),
            "nested_1000": materialized_document(1000, nested=True),
            "nested_miniature": materialized_document(22, nested=True),
        }
    )
    documents.update(
        (f"flat_1000_refused_{position}", refused_document(position))
        for position in ("first", "last", "all")
    )
    documents.update(
        (f"contracts_{size}", contract_document(size)) for size in (1, 100, 1000)
    )
    for name, value in documents.items():
        encoded = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode(
            "ascii"
        )
        fixtures[name] = {
            **fixture_row(encoded, value),
            **graph_inventory(value),
            "custom_contracts": int(name.removeprefix("contracts_"))
            if name.startswith("contracts_")
            else 2
            if name.startswith("nested_")
            else 0,
        }
    value = ignored_root(False)
    encoded = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode(
        "ascii"
    )
    fixtures["ignored_3mib"] = fixture_row(encoded, value)
    cycle = ignored_root(True)
    encoded = render_ir_json(GraphDocument.model_validate(cycle)).encode("ascii")
    count, depth = data_inventory(cycle)
    fixtures["ignored_cycle"] = {
        "bytes": None,
        "reason": "materialized cyclic value has no JSON encoding",
        "containers": count,
        "container_depth": depth,
        "canonical_projection": fixture_row(encoded, document()),
    }
    return json.dumps(fixtures, sort_keys=True, separators=(",", ":"))


LIFETIME_DOCUMENT: dict[str, object] = materialized_document(22, nested=True)
LIFETIME_ROTATION: tuple[tuple[dict[str, object], bool], ...] = (
    (materialized_document(1000), True),
    (refused_document("last"), False),
    (refused_document("all"), False),
    (materialized_document(1000, nested=True), True),
)


def document_lifetime_cycle(index: int) -> BaseModel | None:
    value, valid = (
        LIFETIME_ROTATION[((index + 1) // 100 - 1) % 4]
        if (index + 1) % 100 == 0
        else (LIFETIME_DOCUMENT, True)
    )
    result = validate_config(GraphDocument, value, valid)
    if valid:
        assert isinstance(result, GraphDocument)
        export_ir(result)
        return result
    return None


def warm_document_lifetime() -> None:
    for value, valid in LIFETIME_ROTATION:
        for _ in range(3):
            result = validate_config(GraphDocument, value, valid)
            if isinstance(result, GraphDocument):
                export_ir(result)
