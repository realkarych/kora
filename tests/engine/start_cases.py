import json
from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.api.graph_deployment import PreparedDeployment
from kora.api.graph_deployment_limits import IDENTITY
from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database
from kora.db.graph_deployment_reads import graph_id_by_name
from kora.db.identifiers import IdentifierGenerator
from kora.db.tables import (
    event,
    inbound_event,
    run,
    run_inputs,
    run_promotion_admission,
)
from kora.engine.start_acceptance import (
    StartAcceptance,
    acquire_manual_start,
    commit_manual_start,
    select_manual_start,
)
from kora.engine.start_facts import (
    InstallationRefusal,
    ManualStartFacts,
    StartRefusal,
    read_manual_start_facts,
)
from kora.engine.start_records import (
    PreparedManualStartRequest,
    prepare_manual_start_request,
)
from kora.ir.agent_secrets import NamespaceSecretPolicy
from kora.ir.deployment_checks import DeploymentSelection
from kora.ir.json import JsonObject
from kora.protocols.authentication import Principal
from tests.api.deployment_cases import (
    AT,
    DEFAULT_POLICY,
    GRAPH,
    SELECTION,
    prepared,
    principal,
    publish,
    save_secret_policy,
    seeded_namespace,
    skeleton_ir,
)
from tests.db.seed import FakeClock
from tests.engine.graph_output_cases import child

KEY = "start-request-1"
StartResult = StartAcceptance | StartRefusal | InstallationRefusal


@dataclass(frozen=True)
class DeployedGraph:
    namespace_id: str
    graph_id: str
    version_id: str
    user: Principal


async def _graph_id(database: Database, namespace_id: str) -> str:
    async def unit(connection: AsyncConnection) -> str | None:
        return await graph_id_by_name(connection, namespace_id, GRAPH)

    found = await database.read(unit)
    assert found is not None
    return found


PREPARED: dict[str, PreparedDeployment] = {}


async def prepared_once(
    evaluator: Evaluator,
    ir: JsonObject | None = None,
    policy: NamespaceSecretPolicy = DEFAULT_POLICY,
) -> PreparedDeployment:
    document = skeleton_ir() if ir is None else ir
    key = json.dumps([document, asdict(policy)], sort_keys=True)
    if key not in PREPARED:
        PREPARED[key] = await prepared(evaluator, document, policy)
    return PREPARED[key]


async def deploy(
    database: Database,
    evaluator: Evaluator,
    ir: JsonObject | None = None,
    policy: NamespaceSecretPolicy = DEFAULT_POLICY,
) -> DeployedGraph:
    namespace_id, token_id = await seeded_namespace(database)
    if policy != DEFAULT_POLICY:
        await save_secret_policy(database, namespace_id, policy)
    published = await publish(
        database,
        await prepared_once(evaluator, ir, policy),
        namespace_id,
        token_id,
    )
    return DeployedGraph(
        namespace_id,
        await _graph_id(database, namespace_id),
        published.graph_version_id,
        principal(namespace_id, token_id),
    )


def agent_config(ir: JsonObject) -> dict[str, object]:
    nodes = cast("dict[str, dict[str, dict[str, object]]]", child(ir, "graph", "nodes"))
    return nodes["agent"]["config"]


async def publish_version(
    database: Database, evaluator: Evaluator, case: DeployedGraph, ir: JsonObject
) -> str:
    published = await publish(
        database,
        await prepared_once(evaluator, ir),
        case.namespace_id,
        case.user.token_id,
    )
    return published.graph_version_id


async def redeploy(
    database: Database, evaluator: Evaluator, case: DeployedGraph, prompt: str
) -> str:
    ir = skeleton_ir()
    agent_config(ir)["prompt"] = prompt
    return await publish_version(database, evaluator, case, ir)


def request_for(
    case: DeployedGraph,
    params: JsonObject | None = None,
    *,
    version: str | None = None,
    allow_unenforced_secrets: bool = False,
) -> PreparedManualStartRequest:
    return prepare_manual_start_request(
        case.namespace_id,
        case.graph_id,
        {} if params is None else params,
        graph_version_id=version,
        allow_unenforced_secrets=allow_unenforced_secrets,
    )


async def facts_for(
    database: Database,
    case: DeployedGraph,
    request: PreparedManualStartRequest,
    key: str = KEY,
    selection: DeploymentSelection = SELECTION,
) -> StartResult | ManualStartFacts:
    candidate = await select_manual_start(database, case.user, request, key)
    if isinstance(candidate, StartAcceptance):
        return candidate
    selected = await acquire_manual_start(database, request, candidate, limits=IDENTITY)
    if isinstance(selected, StartAcceptance):
        return selected
    return read_manual_start_facts(selected, request, selection, limits=IDENTITY)


async def commit(
    database: Database,
    case: DeployedGraph,
    request: PreparedManualStartRequest,
    facts: ManualStartFacts,
    key: str = KEY,
    identifiers: IdentifierGenerator | None = None,
    seconds: int = 0,
    principal: Principal | None = None,
) -> StartAcceptance:
    clock = FakeClock(AT + timedelta(seconds=seconds))
    return await commit_manual_start(
        database,
        case.user if principal is None else principal,
        request,
        key,
        facts,
        run_id=IdentifierGenerator(clock.now).new(),
        identifiers=IdentifierGenerator(clock.now)
        if identifiers is None
        else identifiers,
        at=clock.now(),
    )


async def start(
    database: Database,
    case: DeployedGraph,
    params: JsonObject | None = None,
    *,
    key: str = KEY,
    version: str | None = None,
    allow_unenforced_secrets: bool = False,
    selection: DeploymentSelection = SELECTION,
    seconds: int = 0,
) -> StartResult:
    request = request_for(
        case, params, version=version, allow_unenforced_secrets=allow_unenforced_secrets
    )
    facts = await facts_for(database, case, request, key, selection)
    if not isinstance(facts, ManualStartFacts):
        return facts
    return await commit(database, case, request, facts, key, seconds=seconds)


async def start_rows(database: Database) -> dict[str, list[dict[str, object]]]:
    tables = {
        "run": run,
        "run_inputs": run_inputs,
        "receipt": inbound_event,
        "event": event,
        "admission": run_promotion_admission,
    }
    rows: dict[str, list[dict[str, object]]] = {}
    async with database.read_transaction() as reader:
        for name, table in tables.items():
            with await reader.execute(
                select(table).order_by(*table.primary_key.columns)
            ) as result:
                rows[name] = [dict(row) for row in result.mappings()]
    return rows
