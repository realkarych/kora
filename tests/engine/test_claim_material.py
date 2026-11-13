import math
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from typing import cast

import pytest
from sqlalchemy import update

from kora.db.connection import Database
from kora.db.graph_version_reads import acquire_graph_version
from kora.db.run_input_reads import RunInputField
from kora.db.tables import graph_version
from kora.engine.claim_material import (
    ClaimRuntimeSelection,
    prepare_claim_material,
)
from kora.engine.deadline_records import duration_us
from kora.engine.graph_execution_contract import (
    CheckedExecutionContract,
    check_detached_execution_contract,
)
from kora.engine.graph_resource_references import GraphResourceScope
from kora.engine.run_input_reads import ReadRunInput, read_run_input
from kora.engine.run_input_values import encode_run_input_value
from kora.engine.start_records import MAX_PARAMS_BYTES, MAX_PARAMS_DEPTH
from kora.ir.cost import PriceRevision
from kora.ir.graph_json import ObjectReader
from kora.ir.json import JsonObject, JsonValue
from kora.protocols.worker_claim_wire import ClaimWireRejected
from tests.db.closing import ConnectionLedger
from tests.engine.graph_output_cases import child
from tests.engine.test_graph_execution_contract import (
    LIMITS,
    complete_inputs,
    prepared_case,
)
from tests.engine.test_run_input_reads import LIMITS as INPUT_LIMITS
from tests.engine.test_run_input_reads import seed

PRICE = PriceRevision.model_validate(
    {
        "id": "pinned-price",
        "namespace_id": None,
        "effective_from": "2026-01-01T00:00:00Z",
        "version": 1,
        "normalization_version": 1,
        "prices": {},
    }
)


async def source(
    database: Database,
    *,
    node_limits: JsonObject | None = None,
    runtime_settings: JsonObject | None = None,
    params: JsonObject | None = None,
    agent_material: bool = False,
) -> tuple[CheckedExecutionContract, ReadRunInput, JsonObject, JsonObject]:
    values: JsonObject = (
        {"enabled": True, "integer": 1, "float": 1.0, "zero": -0.0}
        if params is None
        else params
    )
    binding = await seed(
        database, params=encode_run_input_value(values, max_bytes=MAX_PARAMS_BYTES)
    )
    scope = GraphResourceScope(
        binding.namespace_id, binding.graph_id, binding.version_id
    )
    raw, saved, refs, owners = complete_inputs(scope, runtime_settings=runtime_settings)
    node = child(raw, "graph", "nodes", "producer")
    node["protected_paths"] = ["node/**"]
    if node_limits is not None:
        node["limits"] = node_limits
    child(node, "config").update(
        {
            "cmd": ["check", "{{ params.integer }}"],
            "runtime": "selected-runtime",
            "vcs": "selected-vcs",
            "x-numbers": [10**800, 1, 1.0, -0.0],
        }
    )
    if agent_material:
        agent = child(raw, "graph", "nodes", "agent")
        agent["inputs"] = {"data": {"type": "StructuredOutput", "from": "$params"}}
        child(agent, "config").update(
            {
                "prompt": "Use {{ inputs.data }}",
                "tool_policy": {
                    "allow": ["Read"],
                    "deny": ["Bash(*)"],
                    "x-engine": {"mode": "restricted"},
                },
                "secret_env": {"CREDENTIAL": "SERVICE"},
            }
        )
        raw["secrets"] = {"aliases": {"SERVICE": "secret://store/service"}}
    prepared, pin = prepared_case(raw, saved, scope)
    async with database.transaction() as writer:
        await writer.execute(
            update(graph_version)
            .where(graph_version.c.id == scope.version_id)
            .values(
                ir=prepared.values.ir,
                execution_contract=prepared.values.execution_contract,
                value_encoding_version=prepared.values.value_encoding_version,
                ir_digest=prepared.digests.ir_digest,
                execution_contract_digest=prepared.digests.execution_contract_digest,
                deployment_digest=prepared.digests.deployment_digest,
                resources=refs,
            )
        )
    async with database.read_snapshot() as reader:
        acquired = await acquire_graph_version(
            reader,
            scope.namespace_id,
            scope.graph_id,
            scope.version_id,
            max_encoded_bytes=LIMITS.identity.max_encoded_bytes,
        )
    read_params = await read_run_input(
        database, binding, RunInputField.PARAMS, INPUT_LIMITS
    )
    assert acquired is not None
    assert read_params is not None
    checked = check_detached_execution_contract(
        acquired, pin, scope, refs, owners, limits=LIMITS
    )
    return checked, read_params, raw, saved


@pytest.mark.asyncio
async def test_claim_material_uses_exact_saved_sources(database: Database) -> None:
    ledger = ConnectionLedger(database)
    checked, params, raw, saved = await source(
        database, runtime_settings={"protected_paths": ["runtime/**"]}
    )
    selection = ClaimRuntimeSelection(
        "selected-runtime", "selected-vcs", timedelta(minutes=7)
    )
    before = ledger.checkouts

    material = prepare_claim_material(checked, params, "producer", {}, PRICE, selection)
    value = ObjectReader(material.immutable.payload.decode("utf-8")).object()

    assert ledger.checkouts == before
    assert ledger.outstanding == database.units_in_flight() == 0
    assert material.identity.binding == params.binding
    assert material.identity.contract_digest == checked.pin.execution_contract_digest
    context = child(value, "execution_context")
    assert context == {
        "version": 1,
        "kind": "command",
        "graph_version_id": checked.scope.version_id,
        "execution_contract_digest": checked.pin.execution_contract_digest,
        "execution_semantics": saved["semantics"],
        "runtime": "selected-runtime",
        "vcs": "selected-vcs",
        "protected_paths": ["runtime/**", "node/**"],
        "retry_policy": {
            "interpretation": child(saved, "retry_policies")["interpretation"],
            **child(saved, "retry_policies", "nodes", "producer"),
        },
    }
    assert value["output_contract"] == [
        entry
        for entry in cast(list[JsonObject], saved["outputs"])
        if cast(list[str], entry["path"])[:3] == ["graph", "nodes", "producer"]
    ]
    [command] = cast(list[JsonObject], child(saved, "command_resources")["commands"])
    assert value["resources"] == command["resources"]
    assert value["price_table_id"] == PRICE.id
    assert value["price_table"] == PRICE.model_dump(mode="json")
    assert value["resolved_config"] == child(
        raw, "graph", "nodes", "producer", "config"
    )
    assert child(value, "params")["integer"] == 1
    assert type(child(value, "params")["float"]) is float
    assert math.copysign(1, cast(float, child(value, "params")["zero"])) == -1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("node", "runtime", "default", "expected"),
    [
        ({"stall_after": "003ms"}, {"stall_after": "9s"}, timedelta(seconds=4), "3000"),
        ({}, {"stall_after": "9s"}, timedelta(seconds=4), "9000000"),
        ({}, {}, timedelta(microseconds=1), "1"),
        ({}, {}, timedelta(seconds=3, microseconds=7), "3000007"),
        ({}, {}, timedelta.max, str(duration_us(timedelta.max))),
        ({"stall_after": "0s"}, {}, timedelta(seconds=4), "0"),
    ],
)
async def test_claim_limits_keep_exact_stall_precedence(
    database: Database,
    node: JsonObject,
    runtime: JsonObject,
    default: timedelta,
    expected: str,
) -> None:
    checked, params, _, _ = await source(
        database,
        node_limits=node,
        runtime_settings={**runtime, "limits": {"timeout": "1h", "max_events": 100}},
    )

    material = prepare_claim_material(
        checked,
        params,
        "producer",
        {},
        PRICE,
        ClaimRuntimeSelection("selected-runtime", "selected-vcs", default),
    )
    value = ObjectReader(material.immutable.payload.decode("utf-8")).object()

    assert value["limits"] == {"stall_after_us": expected}


@pytest.mark.asyncio
async def test_claim_material_preserves_compiler_resolved_configuration(
    database: Database,
) -> None:
    checked, params, _, _ = await source(database, node_limits={"timeout": "2m"})
    selection = ClaimRuntimeSelection(
        "selected-runtime", "selected-vcs", timedelta(seconds=3)
    )

    material = prepare_claim_material(checked, params, "producer", {}, PRICE, selection)
    params.value["integer"] = 99
    value = ObjectReader(material.immutable.payload.decode("utf-8")).object()

    assert child(value, "resolved_config")["cmd"] == ["check", "{{ params.integer }}"]
    assert child(value, "resolved_config")["x-numbers"] == [10**800, 1, 1.0, -0.0]
    assert child(value, "params")["integer"] == 1
    assert value["limits"] == {"timeout": "2m", "stall_after_us": "3000000"}
    with pytest.raises(FrozenInstanceError):
        material.__setattr__("node_policy", b"changed")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mismatch",
    [
        "namespace",
        "graph",
        "version",
        "projection",
        "node",
        "runtime",
        "vcs",
        "inputs",
        "price",
    ],
)
async def test_claim_material_refuses_wrong_source_binding(
    database: Database, mismatch: str
) -> None:
    checked, params, _, _ = await source(database)
    selection = ClaimRuntimeSelection(
        "selected-runtime", "selected-vcs", timedelta(seconds=3)
    )
    binding = params.binding
    if mismatch in ("namespace", "graph", "version"):
        fields = {
            "namespace": "namespace_id",
            "graph": "graph_id",
            "version": "version_id",
        }
        params = replace(
            params, binding=replace(binding, **{fields[mismatch]: "foreign"})
        )
    if mismatch == "projection":
        params = replace(params, field=RunInputField.PROJECTION)
    if mismatch == "runtime":
        selection = replace(selection, runtime="wrong")
    if mismatch == "vcs":
        selection = replace(selection, vcs="wrong")
    price = (
        PriceRevision.model_validate(
            {**PRICE.model_dump(mode="json"), "namespace_id": "foreign"}
        )
        if mismatch == "price"
        else PRICE
    )

    with pytest.raises(ClaimWireRejected) as error:
        prepare_claim_material(
            checked,
            params,
            "approval" if mismatch == "node" else "producer",
            {"unexpected": {}} if mismatch == "inputs" else {},
            price,
            selection,
        )

    assert str(error.value) == "the Claim representation is invalid"
    assert error.value.__context__ is None
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_claim_material_keeps_maximum_parameter_bytes_and_depth(
    database: Database,
) -> None:
    nested: JsonValue = -0.0
    for _ in range(MAX_PARAMS_DEPTH):
        nested = [nested]
    values: JsonObject = {"nested": nested, "padding": ""}
    fixed = len(encode_run_input_value(values, max_bytes=MAX_PARAMS_BYTES))
    values["padding"] = "a" * (MAX_PARAMS_BYTES - fixed)
    checked, params, _, _ = await source(database, params=values)

    material = prepare_claim_material(
        checked,
        params,
        "producer",
        {},
        PRICE,
        ClaimRuntimeSelection(
            "selected-runtime", "selected-vcs", timedelta(microseconds=1)
        ),
    )
    value = ObjectReader(material.immutable.payload.decode("utf-8")).object()
    encoded = encode_run_input_value(value["params"], max_bytes=MAX_PARAMS_BYTES)

    assert len(encoded) == MAX_PARAMS_BYTES
    assert encoded == encode_run_input_value(values, max_bytes=MAX_PARAMS_BYTES)
    assert b"-0.0" in encoded
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_claim_material_accepts_complete_unicode_price_revision(
    database: Database,
) -> None:
    checked, params, _, _ = await source(database)
    rates = dict.fromkeys(
        (
            "input",
            "cached_input",
            "cache_creation_5m",
            "cache_creation_1h",
            "output",
            "reasoning",
        )
    )
    price = PriceRevision.model_validate(
        {
            **PRICE.model_dump(mode="json"),
            "prices": {str(index) + "界" * 253: rates for index in range(50)},
        }
    )

    material = prepare_claim_material(
        checked,
        params,
        "producer",
        {},
        price,
        ClaimRuntimeSelection("selected-runtime", "selected-vcs", timedelta(seconds=1)),
    )
    value = ObjectReader(material.immutable.payload.decode("utf-8")).object()

    assert value["price_table"] == price.model_dump(mode="json")
    assert len(child(value, "price_table", "prices")) == 50


@pytest.mark.asyncio
async def test_claim_agent_material_keeps_policy_aliases_and_exact_inputs(
    database: Database,
) -> None:
    checked, params, raw, _ = await source(database, agent_material=True)
    inputs: JsonObject = {
        "data": {
            "source": "run_parameter",
            "run_id": params.binding.run_id,
            "path": [],
            "inline": {"enabled": True},
        }
    }

    material = prepare_claim_material(
        checked,
        params,
        "agent",
        inputs,
        PRICE,
        ClaimRuntimeSelection("local", "git", timedelta(seconds=1)),
    )
    value = ObjectReader(material.immutable.payload.decode("utf-8")).object()
    inputs["data"] = {"absent": True}

    assert child(value, "execution_context")["kind"] == "agent"
    assert value["resources"] == []
    assert value["secret_aliases"] == {"SERVICE": "secret://store/service"}
    assert child(value, "resolved_config")["secret_env"] == {"CREDENTIAL": "SERVICE"}
    assert ObjectReader(material.node_policy.decode("utf-8")).object() == child(
        raw, "graph", "nodes", "agent", "config", "tool_policy"
    )
    assert child(value, "input_bindings")["data"] == {
        "source": "run_parameter",
        "run_id": params.binding.run_id,
        "path": [],
        "inline": {"enabled": True},
    }
    assert child(value, "resolved_config")["prompt"] == "Use {{ inputs.data }}"
