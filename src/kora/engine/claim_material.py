from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
from typing import cast

from kora.cel_eval.errors import raise_safe
from kora.db.run_input_reads import RunInputBinding, RunInputField
from kora.engine.deadline_records import duration_us
from kora.engine.graph_execution_contract import CheckedExecutionContract
from kora.engine.locks import COMPONENT
from kora.engine.run_input_reads import ReadRunInput
from kora.engine.run_input_values import encode_run_input_value
from kora.engine.start_records import MAX_PARAMS_BYTES
from kora.ir.cost import PriceRevision
from kora.ir.graph_json import (
    ObjectReader,
    TextBudget,
    integer_value,
    render_object,
    scalar_text,
)
from kora.ir.json import JsonObject, JsonValue
from kora.observability import CATALOG, EventSpec, FieldKind, Level, current_diagnostics
from kora.protocols.worker_claim_policy import prepare_node_policy
from kora.protocols.worker_claim_wire import (
    MAX_IMMUTABLE_BYTES,
    ClaimPartition,
    ClaimWireRejected,
    PreparedClaimPart,
    prepare_claim_part,
)

PREPARED: EventSpec = CATALOG.register(
    EventSpec(
        "engine.claim_material.prepared",
        COMPONENT,
        Level.DEBUG,
        "claim material prepared",
        {"prepared": FieldKind.BOOLEAN},
    )
)


@dataclass(frozen=True, slots=True, repr=False)
class ClaimRuntimeSelection:
    runtime: str
    vcs: str
    default_stall_after: timedelta


@dataclass(frozen=True, slots=True, repr=False)
class ClaimMaterialIdentity:
    binding: RunInputBinding
    node_name: str
    contract_digest: str
    params_digest: str
    inputs_digest: str
    price_digest: str


@dataclass(frozen=True, slots=True, repr=False)
class PreparedClaimMaterial:
    identity: ClaimMaterialIdentity
    immutable: PreparedClaimPart
    node_policy: bytes


@dataclass(frozen=True, slots=True, repr=False)
class SelectedClaimNode:
    context: JsonObject
    config: JsonObject
    limits: JsonObject
    runtime: JsonObject
    input_names: tuple[str, ...]
    outputs: list[JsonObject]
    resources: list[JsonValue]
    secret_aliases: JsonObject


def select_claim_node(
    checked: CheckedExecutionContract, node_name: str
) -> SelectedClaimNode:
    raw = ObjectReader(checked.canonical_ir).object()
    saved = ObjectReader(checked.canonical_contract).object()
    graph = cast(JsonObject, raw["graph"])
    nodes = cast(JsonObject, graph["nodes"])
    selected = nodes.get(node_name)
    if not isinstance(selected, dict) or selected["kind"] not in ("agent", "command"):
        raise ClaimWireRejected()
    config = cast(JsonObject, selected["config"])
    runtime = cast(JsonObject, raw["runtime"])
    path = ["graph", "nodes", node_name]
    outputs = [
        entry
        for entry in cast(list[JsonObject], saved["outputs"])
        if cast(list[str], entry["path"])[:3] == path
        and len(cast(list[str], entry["path"])) == 5
    ]
    declared_outputs = cast(JsonObject, selected["outputs"])
    by_name = {cast(list[str], entry["path"])[4]: entry for entry in outputs}
    if by_name.keys() != declared_outputs.keys():
        raise ClaimWireRejected()
    commands = cast(JsonObject, saved["command_resources"])
    resources: list[JsonValue] = []
    if selected["kind"] == "command":
        manifests = [
            entry
            for entry in cast(list[JsonObject], commands["commands"])
            if entry["path"] == path
        ]
        if len(manifests) != 1:
            raise ClaimWireRejected()
        resources = cast(list[JsonValue], manifests[0]["resources"])
    retry = cast(JsonObject, saved["retry_policies"])
    policies = cast(JsonObject, retry["nodes"])
    selected_retry = cast(JsonObject, policies[node_name])
    context: JsonObject = {
        "version": 1,
        "kind": selected["kind"],
        "graph_version_id": checked.scope.version_id,
        "execution_contract_digest": checked.pin.execution_contract_digest,
        "execution_semantics": saved["semantics"],
        "runtime": config.get("runtime", runtime["runtime"]),
        "vcs": config.get("vcs", runtime["vcs"]),
        "protected_paths": [
            *cast(list[JsonValue], runtime.get("protected_paths", [])),
            *cast(list[JsonValue], selected.get("protected_paths", [])),
        ],
        "retry_policy": {
            "interpretation": retry["interpretation"],
            **selected_retry,
        },
    }
    return SelectedClaimNode(
        context,
        config,
        cast(JsonObject, selected.get("limits", {})),
        runtime,
        tuple(cast(JsonObject, selected.get("inputs", {}))),
        [by_name[name] for name in declared_outputs],
        resources,
        cast(JsonObject, cast(JsonObject, raw.get("secrets", {})).get("aliases", {})),
    )


def effective_claim_limits(
    node: SelectedClaimNode, selection: ClaimRuntimeSelection
) -> JsonObject:
    if (
        node.context["runtime"] != selection.runtime
        or node.context["vcs"] != selection.vcs
        or selection.default_stall_after <= timedelta(0)
    ):
        raise ClaimWireRejected()
    duration = node.limits.get("stall_after", node.runtime.get("stall_after"))
    if duration is None:
        microseconds = duration_us(selection.default_stall_after)
    else:
        duration = cast(str, duration)
        unit = "ms" if duration.endswith("ms") else duration[-1]
        scale = {
            "ms": 1_000,
            "s": 1_000_000,
            "m": 60_000_000,
            "h": 3_600_000_000,
            "d": 86_400_000_000,
        }[unit]
        microseconds = integer_value(duration[: -len(unit)]) * scale
    return {
        **{name: value for name, value in node.limits.items() if name != "stall_after"},
        "stall_after_us": scalar_text(microseconds, TextBudget(MAX_IMMUTABLE_BYTES)),
    }


def _prepare(
    checked: CheckedExecutionContract,
    params: ReadRunInput,
    node_name: str,
    input_bindings: JsonObject,
    price: PriceRevision,
    selection: ClaimRuntimeSelection,
) -> PreparedClaimMaterial:
    scope = checked.scope
    binding = params.binding
    if (
        params.field is not RunInputField.PARAMS
        or (binding.namespace_id, binding.graph_id, binding.version_id)
        != (scope.namespace_id, scope.graph_id, scope.version_id)
        or price.namespace_id not in (None, scope.namespace_id)
    ):
        raise ClaimWireRejected()
    node = select_claim_node(checked, node_name)
    if input_bindings.keys() != set(node.input_names):
        raise ClaimWireRejected()
    params_bytes = encode_run_input_value(params.value, max_bytes=MAX_PARAMS_BYTES)
    inputs_text = render_object(
        input_bindings, TextBudget(MAX_IMMUTABLE_BYTES, ascii_only=False)
    )
    price_value = price.model_dump(mode="json")
    price_text = render_object(
        price_value, TextBudget(PriceRevision.byte_limit, ascii_only=False)
    )
    immutable = prepare_claim_part(
        ClaimPartition.IMMUTABLE,
        {
            "params": params.value,
            "resolved_config": node.config,
            "execution_context": node.context,
            "input_bindings": input_bindings,
            "output_contract": node.outputs,
            "limits": effective_claim_limits(node, selection),
            "secret_aliases": node.secret_aliases,
            "resources": node.resources,
            "price_table_id": price.id,
            "price_table": price_value,
        },
    )
    policy = prepare_node_policy(node.config.get("tool_policy", {}))
    return PreparedClaimMaterial(
        ClaimMaterialIdentity(
            RunInputBinding(
                binding.namespace_id,
                binding.graph_id,
                binding.version_id,
                binding.run_id,
            ),
            node_name,
            checked.pin.execution_contract_digest,
            sha256(params_bytes).hexdigest(),
            sha256(inputs_text.encode("utf-8")).hexdigest(),
            sha256(price_text.encode("utf-8")).hexdigest(),
        ),
        immutable,
        policy,
    )


def prepare_claim_material(
    checked: CheckedExecutionContract,
    params: ReadRunInput,
    node_name: str,
    input_bindings: JsonObject,
    price: PriceRevision,
    selection: ClaimRuntimeSelection,
) -> PreparedClaimMaterial:
    try:
        result = _prepare(checked, params, node_name, input_bindings, price, selection)
    except ValueError:
        del checked, params, node_name, input_bindings, price, selection
        raise_safe(ClaimWireRejected())
    diagnostics = current_diagnostics()
    if diagnostics is not None:
        diagnostics.logger(COMPONENT).emit(PREPARED, prepared=True)
    return result
