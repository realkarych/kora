import gc
import json
import sys
import threading
import time
import tracemalloc
import weakref
from typing import cast

from pydantic import BaseModel

from kora.ir import (
    BranchCase,
    BranchConfig,
    CommandConfig,
    CommandResource,
    FanOutConfig,
    McpServer,
    NetworkPolicy,
    NodeLimits,
    Provenance,
    Retry,
    SubgraphNodeConfig,
    Workspace,
    export_ir,
)
from kora.ir.record import IrRecord
from tests.ir.config_cases import MINIMAL, POPULATED, RESOURCE
from tests.ir.control_cases import POPULATED as CONTROL_POPULATED
from tests.ir.declaration_cases import INVALID as DECLARATION_INVALID
from tests.ir.declaration_cases import POPULATED as DECLARATION_POPULATED
from tests.ir.document_cases import INVALID as DOCUMENT_INVALID
from tests.ir.document_cases import POPULATED as DOCUMENT_POPULATED
from tests.ir.remaining_config_cases import INVALID as REMAINING_INVALID
from tests.ir.remaining_config_cases import POPULATED as REMAINING_POPULATED
from tests.measurements.harness import (
    open_descriptors,
    os_threads,
    peak_rss_bytes,
    rss_bytes,
)
from tests.measurements.ir_config_workload import validate_config
from tests.measurements.ir_declaration_workload import (
    TRIGGER_ARRAY,
    trigger_lifetime_cycle,
)
from tests.measurements.ir_document_workload import (
    document_lifetime_cycle,
    warm_document_lifetime,
)
from tests.measurements.ir_remaining_workload import supplemental_cycle

INVALID: dict[type[BaseModel], dict[str, object]] = {
    BranchCase: {"when": "true", "to": [False]},
    BranchConfig: {"cases": [{"when": "true", "to": ["a"]}], "else": [False]},
    FanOutConfig: {"body": "body", "over": "items", "max_parallel": 0},
    SubgraphNodeConfig: {"body": "body\n"},
    Retry: {"retry_on": ["timeout", "timeout"]},
    NodeLimits: {"max_events": 0},
    Workspace: {"repo": False},
    NetworkPolicy: {"egress": [False]},
    McpServer: {"type": "invalid"},
    CommandResource: {**RESOURCE, "size": -1},
    CommandConfig: {**MINIMAL[CommandConfig], "cmd": [False]},
}
CONFIGURATIONS: dict[type[BaseModel], dict[str, object]] = {
    **POPULATED,
    **CONTROL_POPULATED,
    **REMAINING_POPULATED,
    **DECLARATION_POPULATED,
    **DOCUMENT_POPULATED,
}

INVALID.update((model, document) for model, document in REMAINING_INVALID.items())
INVALID.update((model, document) for model, document in DECLARATION_INVALID.items())
INVALID.update((model, document) for model, document in DOCUMENT_INVALID.items())


def model_references(value: object) -> list[weakref.ReferenceType[BaseModel]]:
    references: list[weakref.ReferenceType[BaseModel]] = []
    pending: list[object] = [value]
    seen: set[int] = set()
    while pending:
        item = pending.pop()
        identity = id(item)
        if identity in seen:
            continue
        seen.add(identity)
        if isinstance(item, BaseModel):
            references.append(weakref.ref(item))
            pending.extend(getattr(item, name) for name in type(item).model_fields)
        elif isinstance(item, dict):
            pending.extend(cast("dict[object, object]", item).values())
        elif isinstance(item, list | tuple):
            pending.extend(cast("list[object] | tuple[object, ...]", item))
    return references


def cycle(index: int = 0) -> int:
    references: list[weakref.ReferenceType[BaseModel]] = []
    for model, document in CONFIGURATIONS.items():
        parsed = model.model_validate(document)
        references.extend(model_references(parsed))
        if (
            isinstance(parsed, Provenance)
            or isinstance(parsed, IrRecord)
            and (
                type(parsed) in REMAINING_POPULATED
                or type(parsed) in DECLARATION_POPULATED
                or type(parsed) in DOCUMENT_POPULATED
            )
        ):
            export_ir(parsed)
        else:
            parsed.model_dump(mode="json", by_alias=True, exclude_unset=True)
        validate_config(model, INVALID[model], False)
        del parsed
    triggers = trigger_lifetime_cycle()
    references.extend(model_references(triggers))
    del triggers
    supplemental_cycle(index)
    graph = document_lifetime_cycle(index)
    references.extend(model_references(graph))
    del graph
    return sum(reference() is not None for reference in references)


def main() -> None:
    for _ in range(3):
        assert cycle() == 0
    for index in range(14):
        for _ in range(3):
            supplemental_cycle(index)
    warm_document_lifetime()
    checkpoints: list[dict[str, object]] = [{} for _ in range(4)]
    baseline_rss = rss_bytes()
    baseline_peak_rss = peak_rss_bytes()
    baseline_threads = threading.active_count()
    baseline_os_threads = os_threads()
    baseline_descriptors = open_descriptors()
    baseline_schemas = {
        model.__name__: id(model.__pydantic_core_schema__) for model in CONFIGURATIONS
    }
    trigger_schema = id(TRIGGER_ARRAY.core_schema)
    gc.collect()
    tracemalloc.start()
    baseline_snapshot = tracemalloc.take_snapshot()
    baseline = tracemalloc.get_traced_memory()[0]
    started = time.perf_counter()
    live_models = 0

    for cycles in range(1, 1001):
        live_models += cycle(cycles - 1)
        if cycles % 250 == 0:
            gc.collect()
            current, peak = tracemalloc.get_traced_memory()
            snapshot = tracemalloc.take_snapshot()
            differences = snapshot.compare_to(baseline_snapshot, "lineno")
            checkpoints[cycles // 250 - 1] = {
                "cycles": cycles,
                "seconds": time.perf_counter() - started,
                "retained_bytes": current - baseline,
                "peak_traced_bytes": peak,
                "current_rss_bytes": rss_bytes(),
                "peak_rss_bytes": peak_rss_bytes(),
                "live_models": live_models,
                "threads": threading.active_count(),
                "os_threads": os_threads(),
                "tracer_bytes": tracemalloc.get_tracemalloc_memory(),
                "descriptors": open_descriptors(),
                "allocation_differences": [
                    {
                        "location": str(item.traceback),
                        "bytes": item.size_diff,
                        "count": item.count_diff,
                    }
                    for item in differences[:10]
                ],
            }
            del snapshot, differences
    tracemalloc.stop()
    assert {
        model.__name__: id(model.__pydantic_core_schema__) for model in CONFIGURATIONS
    } == baseline_schemas
    assert id(TRIGGER_ARRAY.core_schema) == trigger_schema
    result: dict[str, object] = {
        "baseline_rss_bytes": baseline_rss,
        "baseline_peak_rss_bytes": baseline_peak_rss,
        "baseline_threads": baseline_threads,
        "baseline_os_threads": baseline_os_threads,
        "baseline_descriptors": baseline_descriptors,
        "checkpoints": checkpoints,
        "cycles": 1000,
        "models_per_cycle": len(CONFIGURATIONS),
        "class_schema_inventory": sorted(baseline_schemas),
        "class_schemas_unchanged": True,
        "supplemental_fixtures": 14,
        "trigger_union_array_size": 5,
        "trigger_union_schema_unchanged": True,
        "document_miniature_nodes": 22,
        "document_full_size_slots": 10,
        "document_full_size_rotation": {
            "flat_valid": 3,
            "flat_final_invalid": 3,
            "flat_all_invalid": 2,
            "nested_valid": 2,
        },
        "warm_up": 3,
        "rss_source": "/bin/ps rss KiB converted to bytes"
        if sys.platform == "darwin"
        else "/proc/self/statm resident pages converted to bytes",
    }
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    main()
