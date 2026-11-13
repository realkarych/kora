from typing import Annotated, Literal, cast

from pydantic import Field, StrictInt, StrictStr

from kora.ir.cost import PriceRevision
from kora.ir.graph_json import ObjectReader
from kora.ir.json import JsonObject, JsonValue, parse_json_object
from kora.models.errors import WireRecord
from kora.models.graphs import OpaqueObject, Sha256Hex
from kora.models.workers import WorkerName
from kora.protocols.identity import Identifier
from kora.protocols.worker_claim_policy import prepare_claim_policy, prepare_node_policy
from kora.protocols.worker_claim_system import ClaimSystem, prepare_claim_system
from kora.protocols.worker_claim_wire import (
    ClaimPartition,
    ClaimWireRejected,
    claim_response,
    prepare_claim_part,
)

IMMUTABLE_FIELDS: tuple[str, ...] = (
    "params",
    "resolved_config",
    "execution_context",
    "input_bindings",
    "output_contract",
    "limits",
    "secret_aliases",
    "resources",
    "price_table_id",
    "price_table",
)


class WorkerClaimRequest(WireRecord):
    runtime: WorkerName
    incarnation_id: Identifier
    execution_domain_id: Identifier
    max: Annotated[StrictInt, Field(ge=1, le=1)]
    claim_id: Identifier


class ClaimExecutionContext(WireRecord):
    version: Annotated[StrictInt, Field(ge=1, le=1)]
    kind: Literal["agent", "command"]
    graph_version_id: Identifier
    execution_contract_digest: Sha256Hex
    execution_semantics: StrictStr
    runtime: WorkerName
    vcs: WorkerName
    protected_paths: tuple[StrictStr, ...]
    retry_policy: OpaqueObject


class WorkerClaim(ClaimSystem):
    params: OpaqueObject
    resolved_config: OpaqueObject
    execution_context: ClaimExecutionContext
    input_bindings: OpaqueObject
    output_contract: tuple[OpaqueObject, ...]
    limits: OpaqueObject
    secret_aliases: OpaqueObject
    resources: tuple[OpaqueObject, ...]
    price_table_id: Identifier
    price_table: PriceRevision
    execution_policy: OpaqueObject


class WorkerClaimResponse(WireRecord):
    claim: WorkerClaim | None


def decoded_claim(value: JsonObject) -> WorkerClaimResponse:
    result = WorkerClaimResponse.model_validate(value)
    raw = cast(JsonObject | None, value["claim"])
    if raw is None:
        return result
    if (
        result.claim is None
        or result.claim.price_table.id != result.claim.price_table_id
    ):
        raise ClaimWireRejected()
    params = parse_json_object(raw["params"], max_depth=33, max_bytes=4_194_304)
    pending: list[JsonValue] = [params]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
        elif type(item) is int and not -(2**63) <= item < 2**63:
            raise ClaimWireRejected()
    immutable = prepare_claim_part(
        ClaimPartition.IMMUTABLE, {name: raw[name] for name in IMMUTABLE_FIELDS}
    )
    policy = cast(JsonObject, raw["execution_policy"])
    values = cast(JsonObject, policy["values"])
    node = cast(JsonObject, values["node"])
    prepared_policy = prepare_claim_policy(
        cast(JsonObject, values["namespace"]),
        cast(int, policy["namespace_revision"]),
        prepare_node_policy(node["tool_policy"]),
    )
    if (
        ObjectReader(prepared_policy.payload.decode("utf-8")).object()[
            "execution_policy"
        ]
        != policy
    ):
        raise ClaimWireRejected()
    system = ClaimSystem.model_validate(
        {name: raw[name] for name in ClaimSystem.model_fields}
    )
    claim_response(immutable, prepared_policy, prepare_claim_system(system))
    return result
