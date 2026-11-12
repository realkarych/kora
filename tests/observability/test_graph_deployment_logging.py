from typing import cast

import pytest
from sqlalchemy.exc import IntegrityError

from kora.api.graph_deployment_writer import (
    GraphDeploymentTarget,
    publish_graph_deployment,
)
from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database
from kora.db.identifiers import IdentifierGenerator
from kora.ir.deployment_checks import DeploymentSelection, check_deployment_document
from kora.observability import Level, LoggingSettings
from tests.api.deployment_cases import (
    AT,
    OWNER,
    evaluator,
    prepared,
    principal,
    seeded_namespace,
)
from tests.db.seed import FakeClock
from tests.ir.first_slice_configuration_cases import chain_document, chain_nodes
from tests.observability.conftest import (
    Configure,
    Pipe,
    configured_with_reader,
    finished_output,
)

__all__ = ["evaluator"]


@pytest.mark.parametrize("invalid", [False, True])
def test_deployment_checks_logging_is_content_free(
    configure: Configure, pipe: Pipe, capsys: pytest.CaptureFixture[str], invalid: bool
) -> None:
    markers = ("private_prompt", "private_repo", "private_harness")
    nodes = chain_nodes()
    config = cast("dict[str, object]", nodes["agent"]["config"])
    config.update(prompt=markers[0], workspace={"repo": markers[1]})
    if invalid:
        config["harness"] = markers[2]
    document = chain_document(nodes)
    diagnostics = configured_with_reader(
        configure, pipe, LoggingSettings(level=Level.DEBUG)
    )

    result = check_deployment_document(document, DeploymentSelection("claude"))
    output = finished_output(diagnostics, pipe)

    records = [
        record
        for record in pipe.reader.records()
        if record["event"] == "ir.deployment_checks.completed"
    ]
    assert result.deployable is not invalid
    assert len(records) == 1
    assert records[0]["level"] == "DEBUG"
    assert records[0]["component"] == "ir.validation"
    assert records[0]["complete"] is True
    assert records[0]["errors"] == int(invalid)
    assert records[0]["warnings"] == 0
    assert all(marker not in output for marker in markers)
    assert capsys.readouterr().out == ""


@pytest.mark.asyncio
async def test_deploy_writer_logging_is_content_free(
    configure: Configure, pipe: Pipe, database: Database, evaluator: Evaluator
) -> None:
    namespace_id, token_id = await seeded_namespace(database)
    deployment = await prepared(evaluator)
    target = GraphDeploymentTarget(namespace_id, principal(namespace_id, token_id))
    missing = GraphDeploymentTarget(token_id, principal(token_id, token_id))
    diagnostics = configured_with_reader(
        configure, pipe, LoggingSettings(level=Level.DEBUG)
    )

    published = [
        await publish_graph_deployment(
            database,
            deployment,
            target,
            identifiers=IdentifierGenerator(FakeClock(AT).now),
            at=AT,
        )
        for _ in range(2)
    ]
    with pytest.raises(IntegrityError):
        await publish_graph_deployment(
            database,
            deployment,
            missing,
            identifiers=IdentifierGenerator(FakeClock(AT).now),
            at=AT,
        )
    output = finished_output(diagnostics, pipe)

    records = [
        record
        for record in pipe.reader.records()
        if record["event"] == "api.graph_deployment.completed"
    ]
    digests = deployment.inputs.logical.digests
    private = (
        namespace_id,
        token_id,
        OWNER,
        published[0].graph_version_id,
        digests.ir_digest,
        digests.execution_contract_digest,
        digests.deployment_digest,
    )
    assert [record["outcome"] for record in records] == ["published", "reused"]
    assert {record["level"] for record in records} == {"DEBUG"}
    assert {record["component"] for record in records} == {"api"}
    assert all(value not in output for value in private)
