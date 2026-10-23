import hashlib
from collections.abc import Callable
from typing import cast

from kora.ir import AgentConfig, export_ir, render_ir_json
from kora.ir.json import JsonObject
from kora.ir.record import IrRecord
from tests.ir.remaining_config_cases import INVALID, MINIMAL, POPULATED
from tests.ir.support import agent_document, deep_data
from tests.measurements.ir_config_workload import validate_config, validation_action

Workload = tuple[Callable[[], object], int, bool, int]


def values_data() -> JsonObject:
    return {"values": [{"v": index} for index in range(10000)]}


def open_fixtures() -> dict[str, object]:
    return {
        **{f"depth_{depth}": deep_data(depth) for depth in (17, 64, 255, 256, 1024)},
        "ascii_1mib": {"payload": "a" * 1048576},
        "objects_10000": values_data(),
        "integer_5000": {"n": 10**5000 - 1},
    }


def refused_open_fixtures() -> dict[str, object]:
    cycle: dict[str, object] = {}
    cycle["self"] = cycle
    alias: dict[str, object] = {}
    for _ in range(64):
        alias = {"a": alias, "b": alias}
    late: dict[str, object] = {
        "values": [{"v": index} for index in range(9999)] + [{"v": object()}]
    }
    return {
        "cycle": cycle,
        "alias_64": alias,
        "key": {1: False},
        "nan": {"v": float("nan")},
        "late_runtime": late,
        "array_root": [],
    }


OPEN: dict[str, object] = open_fixtures()
REFUSED_OPEN: dict[str, object] = refused_open_fixtures()


def canonical_round_trip(
    model: type[IrRecord], document: dict[str, object]
) -> Callable[[], object]:
    return lambda: export_ir(model.model_validate(document))


def canonical_export(
    model: type[IrRecord], document: dict[str, object]
) -> Callable[[], object]:
    parsed = model.model_validate(document)
    return lambda: export_ir(parsed)


def agent_scale(size: int) -> dict[str, object]:
    return {
        **MINIMAL[AgentConfig],
        "skills": [f"skill_{index}" for index in range(size)],
        "subagents": [
            {"name": f"helper_{index}", "description": "", "prompt": ""}
            for index in range(size)
        ],
        "mcp_servers": {f"server_{index}": {"type": "http"} for index in range(size)},
    }


def agent_refusals(document: dict[str, object]) -> dict[str, dict[str, object]]:
    servers = cast("dict[str, object]", document["mcp_servers"])
    subagents = cast("list[dict[str, object]]", document["subagents"])
    skills = cast("list[str]", document["skills"])
    return {
        "last_mcp": {
            **document,
            "mcp_servers": {**servers, "server_9999": {"type": False}},
        },
        "last_tools": {
            **document,
            "subagents": [*subagents[:-1], {**subagents[-1], "tools": [False]}],
        },
        "last_skill_duplicate": {
            **document,
            "skills": [*skills[:-1], skills[0]],
        },
        "dense_description": {
            **document,
            "subagents": [{**item, "description": False} for item in subagents],
        },
    }


def remaining_workloads() -> dict[str, Workload]:
    workloads: dict[str, Workload] = {}
    for shape, configurations in (("minimal", MINIMAL), ("populated", POPULATED)):
        for model, document in configurations.items():
            workloads[f"remaining_{model.__name__}_{shape}_round_trip"] = (
                canonical_round_trip(model, document),
                1,
                True,
                30,
            )
    for model, document in INVALID.items():
        workloads[f"remaining_{model.__name__}_refused"] = (
            validation_action(model, document, False),
            1,
            True,
            10,
        )
    for size in (1, 1000, 10000):
        document = agent_scale(size)
        workloads[f"remaining_agent_{size}_validate"] = (
            validation_action(AgentConfig, document),
            size,
            True,
            10,
        )
        workloads[f"remaining_agent_{size}_export"] = (
            canonical_export(AgentConfig, document),
            size,
            True,
            10,
        )
        if size == 10000:
            invalids = agent_refusals(document)
            for name, invalid in invalids.items():
                workloads[f"remaining_agent_10000_refused_{name}"] = (
                    validation_action(AgentConfig, invalid, False),
                    size,
                    True,
                    10,
                )
    for name, value in OPEN.items():
        document = agent_document(value)
        parsed = AgentConfig.model_validate(document)
        workloads[f"remaining_open_{name}_validate"] = (
            validation_action(AgentConfig, document),
            1,
            True,
            10,
        )
        workloads[f"remaining_open_{name}_export"] = (
            lambda parsed=parsed: export_ir(parsed),
            1,
            True,
            10,
        )
        workloads[f"remaining_open_{name}_render"] = (
            lambda parsed=parsed: render_ir_json(parsed),
            1,
            True,
            10,
        )
    for name, value in REFUSED_OPEN.items():
        workloads[f"remaining_open_{name}_refused"] = (
            validation_action(AgentConfig, agent_document(value), False),
            1,
            True,
            10,
        )
    return workloads


def data_inventory(value: object) -> tuple[int, int]:
    pending: list[tuple[object, int]] = [(value, 1)]
    identities: set[int] = set()
    count = 0
    maximum = 0
    while pending:
        item, depth = pending.pop()
        if isinstance(item, dict | list):
            identity = id(cast("object", item))
            if identity in identities:
                continue
            identities.add(identity)
            count += 1
            maximum = max(maximum, depth)
            children = (
                cast("dict[object, object]", item).values()
                if isinstance(item, dict)
                else cast("list[object]", item)
            )
            pending.extend((child, depth + 1) for child in children)
    return count, maximum


def fixture_manifest() -> str:
    import json

    fixtures: dict[str, object] = {}
    configurations = [
        (f"{model.__name__}_{shape}", model, document)
        for shape, values in (("minimal", MINIMAL), ("populated", POPULATED))
        for model, document in values.items()
    ]
    configurations.extend(
        (f"agent_{size}", AgentConfig, agent_scale(size)) for size in (1, 1000, 10000)
    )
    configurations.extend(
        (f"open_{name}", AgentConfig, agent_document(value))
        for name, value in OPEN.items()
    )
    prefix = '{"harness":"claude","prompt":"","workspace":{"repo":""},"output_schema":'
    for name, model, document in configurations:
        encoded = render_ir_json(model.model_validate(document)).encode("ascii")
        containers, depth = data_inventory(document)
        row: dict[str, object] = {
            "bytes": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "containers": containers,
            "container_depth": depth,
        }
        if name.startswith("open_"):
            rendered = encoded.decode("ascii")
            assert rendered.startswith(prefix)
            raw = rendered[len(prefix) : -1].encode("ascii")
            count, data_depth = data_inventory(document["output_schema"])
            row.update(
                {
                    "data_bytes": len(raw),
                    "wrapper_bytes": len(encoded) - len(raw),
                    "data_sha256": hashlib.sha256(raw).hexdigest(),
                    "data_containers": count,
                    "data_depth": data_depth,
                }
            )
        fixtures[name] = row
    refused_documents = {
        **{model.__name__: document for model, document in INVALID.items()},
        **{
            f"agent_10000_{name}": document
            for name, document in agent_refusals(agent_scale(10000)).items()
        },
    }
    for name, document in refused_documents.items():
        encoded = json.dumps(
            document, ensure_ascii=True, allow_nan=False, separators=(",", ":")
        ).encode("ascii")
        count, depth = data_inventory(document)
        fixtures[f"refused_{name}"] = {
            "bytes": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "containers": count,
            "container_depth": depth,
        }
    for name, value in REFUSED_OPEN.items():
        count, depth = data_inventory(value)
        fixtures[f"refused_{name}"] = {
            "unique_containers": count,
            "container_depth": depth,
            "json_bytes": None,
            "reason": "not an admissible open object field",
        }
    fixtures["refused_array_root"] = {
        "unique_containers": 1,
        "container_depth": 1,
        "json_bytes": 2,
        "sha256": hashlib.sha256(b"[]").hexdigest(),
        "reason": "the root is not an object",
    }
    return json.dumps(fixtures, sort_keys=True, separators=(",", ":"))


def supplemental_cycle(index: int) -> None:
    cases = [
        *((value, True) for value in OPEN.values()),
        *((value, False) for value in REFUSED_OPEN.values()),
    ]
    value, valid = cases[index % len(cases)]
    parsed = validate_config(AgentConfig, agent_document(value), valid)
    if valid:
        assert isinstance(parsed, AgentConfig)
        export_ir(parsed)
        render_ir_json(parsed)
