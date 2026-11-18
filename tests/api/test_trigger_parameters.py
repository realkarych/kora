import json
import math
from typing import cast

import pytest
from sqlalchemy import select, update

from kora.api.graph_deployment_limits import IDENTITY
from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.cel_eval.thread import Evaluator
from kora.conformance.capture import Capture
from kora.db.connection import Database
from kora.db.run_input_reads import RunInputBinding, RunInputField, RunInputReadLimits
from kora.db.tables import graph_version
from kora.engine.graph_version_identity import prepare_graph_version_identity
from kora.engine.run_input_reads import read_run_input
from kora.ir.json import JsonObject, JsonValue
from kora.ir.vcs_ref_json import canonical_bytes
from tests.api.deployment_cases import HARNESS, evaluator, preparer, skeleton_ir
from tests.api.support import api_capture, application, client_for, deployment_services
from tests.api.test_trigger_route import RUNS, VERSIONS, deployed, error_of, keyed, user
from tests.engine.graph_output_cases import child
from tests.engine.start_cases import start_rows

__all__ = ["api_capture", "evaluator", "preparer"]

PRIVATE = "private-parameter-content"


def parameter_graph(schema: JsonObject, predicate: str | None = None) -> JsonObject:
    ir = skeleton_ir()
    child(ir, "graph")["params"] = schema
    if predicate is not None:
        child(ir, "graph", "nodes", "approve", "config")["approve_when"] = predicate
    return ir


@pytest.mark.asyncio
async def test_trigger_parameters_keep_original_values_and_typed_projection(
    database: Database, preparer: DeploymentPreparer, api_capture: Capture
) -> None:
    headers, _ = await user(database)
    ir = parameter_graph(
        {
            "type": "object",
            "properties": {"enabled": {"type": "integer"}},
            "required": ["enabled"],
        },
        "params.enabled == 0",
    )
    values: JsonObject = {
        "enabled": -0.0,
        "integer": 9_007_199_254_740_993,
        "float": 1.0,
        "é\x00": PRIVATE * 1000,
    }
    app = application(
        database, api_capture, deployment_services(database, preparer, HARNESS)
    )

    async with client_for(app) as client:
        published = await client.post(
            VERSIONS,
            json={"graph": "skeleton", "ir": ir, "resources": []},
            headers=headers,
        )
        accepted = await client.post(
            RUNS, json={"params": values}, headers=keyed(headers, "original")
        )
        replayed = await client.post(
            RUNS, json={"params": values}, headers=keyed(headers, "original")
        )
        changed = await client.post(
            RUNS,
            json={"params": values | {"enabled": 0}},
            headers=keyed(headers, "original"),
        )
    rows = await start_rows(database)
    [saved] = rows["run"]
    [inputs] = rows["run_inputs"]
    binding = RunInputBinding(
        str(saved["namespace_id"]),
        str(saved["graph_id"]),
        str(saved["graph_version_id"]),
        str(saved["id"]),
    )
    loaded = await read_run_input(
        database,
        binding,
        RunInputField.PARAMS,
        RunInputReadLimits(1_000_000, 4096, 4_194_304),
    )

    assert (published.status_code, accepted.status_code, replayed.status_code) == (
        201,
        201,
        200,
    )
    assert replayed.json() == accepted.json()
    assert error_of(changed).error.code == "API004"
    assert inputs["params"] == canonical_bytes(values)
    assert inputs["cel_projection"] == b'{"enabled":-0.0}'
    assert loaded is not None
    assert type(loaded.value["integer"]) is int
    assert type(loaded.value["float"]) is float
    assert math.copysign(1, cast(float, loaded.value["enabled"])) == -1
    assert loaded.value["é\x00"] == values["é\x00"]
    assert len(rows["receipt"]) == len(rows["event"]) == len(rows["admission"]) == 1
    assert PRIVATE not in json.dumps(api_capture.finish())


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["schema", "profile-capacity", "converted-cut"])
async def test_trigger_parameter_refusals_leave_no_durable_effect(
    database: Database,
    evaluator: Evaluator,
    preparer: DeploymentPreparer,
    api_capture: Capture,
    failure: str,
) -> None:
    headers, namespace_id = await user(database)
    predicate = None
    schema: JsonObject = {}
    values: JsonObject = {"unused": cast(list[JsonValue], [0] * 65537)}
    expected = "API010"
    if failure == "schema":
        schema = {"type": "object", "properties": {"enabled": {"type": "integer"}}}
        values = {"enabled": True}
        expected = "API003"
    elif failure == "converted-cut":
        schema = {
            "type": "object",
            "properties": {"enabled": {"type": "array", "items": {"type": "number"}}},
            "required": ["enabled"],
        }
        values = {"enabled": cast(list[JsonValue], [9_007_199_254_740_993] * 230)}
        predicate = "params.enabled.size() > 0"
        expected = "API003"
    await deployed(
        database, evaluator, namespace_id, parameter_graph(schema, predicate)
    )
    app = application(
        database, api_capture, deployment_services(database, preparer, HARNESS)
    )

    async with client_for(app) as client:
        refused = await client.post(
            RUNS, json={"params": values}, headers=keyed(headers, "refused")
        )
    rows = await start_rows(database)

    assert error_of(refused).error.code == expected
    assert error_of(refused).error.details.model_dump() == {}
    assert all(not table for table in rows.values())
    assert not preparer.busy


@pytest.mark.asyncio
async def test_trigger_legacy_profile_refuses_fresh_effects_but_keeps_receipt_order(
    database: Database,
    evaluator: Evaluator,
    preparer: DeploymentPreparer,
    api_capture: Capture,
) -> None:
    headers, namespace_id = await user(database)
    version = (await deployed(database, evaluator, namespace_id)).removeprefix("gv_")
    app = application(
        database, api_capture, deployment_services(database, preparer, HARNESS)
    )
    async with client_for(app) as client:
        original = await client.post(
            RUNS, json={"params": {}}, headers=keyed(headers, "saved")
        )
    async with database.transaction() as writer:
        row = (
            await writer.execute(
                select(graph_version.c.ir, graph_version.c.execution_contract).where(
                    graph_version.c.id == version
                )
            )
        ).one()
        ir = cast(JsonObject, json.loads(row.ir))
        contract = cast(JsonObject, json.loads(row.execution_contract))
        del contract["parameter_evaluation"]
        historical = prepare_graph_version_identity(ir, contract, limits=IDENTITY)
        await writer.execute(
            update(graph_version)
            .where(graph_version.c.id == version)
            .values(
                execution_contract=historical.values.execution_contract,
                execution_contract_digest=historical.digests.execution_contract_digest,
                deployment_digest=historical.digests.deployment_digest,
            )
        )
    before = await start_rows(database)

    async with client_for(app) as client:
        fresh = await client.post(
            RUNS, json={"params": {}}, headers=keyed(headers, "new")
        )
        replayed = await client.post(
            RUNS, json={"params": {}}, headers=keyed(headers, "saved")
        )
        mismatch = await client.post(
            RUNS, json={"params": {"changed": 1}}, headers=keyed(headers, "saved")
        )

    assert original.status_code == 201
    assert error_of(fresh).error.code == "API010"
    assert replayed.status_code == 200
    assert replayed.json() == original.json()
    assert error_of(mismatch).error.code == "API004"
    assert await start_rows(database) == before
