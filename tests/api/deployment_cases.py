from collections.abc import AsyncGenerator
from copy import deepcopy
from datetime import UTC, datetime
from typing import cast

import pytest_asyncio

from kora.api.graph_deployment import PreparedDeployment, prepare_graph_deployment
from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.api.graph_deployment_writer import (
    GraphDeploymentTarget,
    PublishedGraphVersion,
    publish_graph_deployment,
)
from kora.cel_eval.binding import load_binding
from kora.cel_eval.thread import EvaluationThread, Evaluator
from kora.db.connection import Database
from kora.db.identifiers import IdentifierGenerator
from kora.engine.graph_command_manifests import GraphDeploymentSelectors
from kora.ir.deployment_checks import DeploymentSelection
from kora.ir.json import JsonObject
from kora.protocols.authentication import NamespaceGrant, Principal
from tests.cel_eval.pattern_validation_scenarios import assert_settled
from tests.db.seed import FakeClock, Seeder
from tests.plugins.authentication.support import AuthorityClock

HARNESS = "fake"
NAMESPACE = "example"
GRAPH = "skeleton"
OWNER = "person"
AT = datetime(2026, 11, 12, 9, 30, tzinfo=UTC)
BUILTIN_TYPES = ("AgentSession", "CommandResult", "StructuredOutput", "VcsRef")
SELECTORS = GraphDeploymentSelectors(
    authorized_namespace_name=NAMESPACE, route_graph=GRAPH, request_graph=GRAPH
)
SELECTION = DeploymentSelection(HARNESS)


def evaluation_clock() -> datetime:
    return AT


def skeleton_ir(
    *, harness: str = HARNESS, graph: str = GRAPH, namespace: str = NAMESPACE
) -> JsonObject:
    return deepcopy(
        {
            "ir_version": "1.0",
            "type_contracts": {
                name: {"builtin_revision": "1"} for name in BUILTIN_TYPES
            },
            "graph": {
                "id": graph,
                "namespace": namespace,
                "nodes": {
                    "agent": {
                        "kind": "agent",
                        "config": {
                            "harness": harness,
                            "prompt": "Summarize the repository.",
                            "workspace": {"repo": ""},
                        },
                        "outputs": {
                            "session": {"type": "AgentSession"},
                            "summary": {"type": "StructuredOutput"},
                        },
                    },
                    "gate": {
                        "kind": "command",
                        "depends_on": ["agent"],
                        "config": {"cmd": ["true"], "workspace": {"repo": ""}},
                        "outputs": {"result": {"type": "CommandResult"}},
                    },
                    "approve": {
                        "kind": "human_input",
                        "inputs": {
                            "gate": {
                                "type": "CommandResult",
                                "from": "gate",
                                "output": "result",
                            }
                        },
                        "config": {
                            "channel": "cli",
                            "timeout": "1h",
                            "on_expiry": "fail",
                        },
                        "outputs": {"response": {"type": "StructuredOutput"}},
                    },
                },
            },
            "runtime": {"runtime": "local", "vcs": "git"},
        }
    )


def unresolved_approval() -> JsonObject:
    ir = skeleton_ir()
    nodes = cast(
        "dict[str, dict[str, dict[str, object]]]",
        cast("JsonObject", ir["graph"])["nodes"],
    )
    approve = nodes["approve"]
    approve["inputs"]["summary"] = {
        "type": "StructuredOutput",
        "from": "agent",
        "output": "summary",
        "path": "x",
    }
    approve["config"]["approve_when"] = "inputs.summary == 1"
    return ir


def principal(namespace_id: str, token_id: str, owner: str = OWNER) -> Principal:
    return Principal(
        token_id=token_id,
        kind="user",
        owner=owner,
        namespaces=(NamespaceGrant(namespace_id=namespace_id, transcripts=True),),
        approve=True,
        platform=True,
        runtime=None,
    )


async def seeded_namespace(
    database: Database, name: str = NAMESPACE
) -> tuple[str, str]:
    async with database.transaction() as writer:
        seeder = Seeder(writer)
        namespace_id = await seeder.namespace(name)
        token_id = seeder.new_id()
    return namespace_id, token_id


@pytest_asyncio.fixture
async def evaluator() -> AsyncGenerator[Evaluator]:
    owner = EvaluationThread(load_binding())
    opened = Evaluator(evaluation_clock, owner)
    try:
        yield opened
    finally:
        await opened.close()
    assert_settled(owner)


async def prepared(
    evaluator: Evaluator, ir: JsonObject | None = None
) -> PreparedDeployment:
    result = await prepare_graph_deployment(
        skeleton_ir() if ir is None else ir, SELECTORS, SELECTION, evaluator
    )
    assert isinstance(result, PreparedDeployment)
    return result


async def publish(
    database: Database,
    deployment: PreparedDeployment,
    namespace_id: str,
    token_id: str,
    owner: str = OWNER,
) -> PublishedGraphVersion:
    return await publish_graph_deployment(
        database,
        deployment,
        GraphDeploymentTarget(namespace_id, principal(namespace_id, token_id, owner)),
        identifiers=IdentifierGenerator(FakeClock(AT).now),
        at=AT,
    )


@pytest_asyncio.fixture
async def preparer() -> AsyncGenerator[DeploymentPreparer]:
    owner = EvaluationThread(load_binding())
    async with DeploymentPreparer(AuthorityClock(), owner) as opened:
        yield opened
    assert_settled(owner)
