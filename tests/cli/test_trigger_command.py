import json
from pathlib import Path

import pytest

from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.conformance.capture import Capture
from kora.db.connection import Database
from kora.models.errors import ApiErrorEnvelope
from kora.models.runs import AcceptedRun
from tests.api.deployment_cases import HARNESS, NAMESPACE, preparer, skeleton_ir
from tests.api.support import api_capture, application, deployment_services
from tests.cli.test_deploy_command import bearer, kora, written
from tests.client.support import running_app

__all__ = ["api_capture", "preparer"]


@pytest.mark.asyncio
async def test_cli_trigger(
    database: Database,
    preparer: DeploymentPreparer,
    api_capture: Capture,
    tmp_path: Path,
) -> None:
    token = await bearer(database)
    app = application(
        database, api_capture, deployment_services(database, preparer, HARNESS)
    )
    graph = written(tmp_path, "skeleton.ir.json", skeleton_ir())

    async with running_app(app) as origin:
        environment = {"KORA_URL": origin, "KORA_TOKEN": token}
        deployed = await kora(["deploy", str(graph)], environment)
        machine = await kora(
            ["trigger", "skeleton", "--namespace", NAMESPACE, "--json"]
            + ["--idempotency-key", "cli-start"],
            environment,
        )
        human = await kora(
            ["trigger", "skeleton", "--idempotency-key", "cli-start"],
            environment | {"KORA_NAMESPACE": NAMESPACE, "KORA_LOG_LEVEL": "DEBUG"},
        )
        changed = await kora(
            ["trigger", "skeleton", "--idempotency-key", "cli-start"]
            + ["--allow-unenforced-secrets"],
            environment | {"KORA_NAMESPACE": NAMESPACE},
        )
        preferred = await kora(
            ["trigger", "skeleton", "--namespace", NAMESPACE, "--json"],
            environment | {"KORA_NAMESPACE": "elsewhere"},
        )
        dotted = await kora(
            ["trigger", "..", "--namespace", NAMESPACE, "--json"], environment
        )
        unconfigured = await kora(["trigger", "skeleton"], environment)
        invalid = await kora(
            ["trigger", "skeleton", "--namespace", "Not-A-Namespace"], environment
        )
        bad_version = await kora(
            ["trigger", "skeleton", "--namespace", NAMESPACE]
            + ["--graph-version", "gv_1"],
            environment,
        )

    accepted = AcceptedRun.model_validate_json(machine.stdout)
    assert (deployed.code, machine.code, human.code) == (0, 0, 0)
    assert human.stdout.decode().strip() == (
        f"accepted run {accepted.run_id} (graph version {accepted.graph_version_id})"
    )
    records = {str(record["event"]): record for record in human.records()}
    assert records["cli.trigger.finished"]["outcome"] == "ok"
    assert changed.code == 1
    assert ApiErrorEnvelope.model_validate_json(changed.stdout).error.code == "API004"
    assert preferred.code == 0
    assert AcceptedRun.model_validate_json(preferred.stdout).run_id != accepted.run_id
    assert dotted.code == 1
    details = json.loads(dotted.stdout)["error"]["details"]
    assert (details["reason"], details["outcome"]) == ("destination_policy", "not_sent")
    assert (unconfigured.code, unconfigured.stdout) == (78, b"")
    assert b"KORA_NAMESPACE: missing" in unconfigured.stderr
    assert (invalid.code, invalid.stdout) == (78, b"")
    assert b"--namespace: invalid" in invalid.stderr
    assert (bad_version.code, bad_version.stdout) == (2, b"")
    for private in (token.encode(), b"cli-start"):
        assert private not in human.stderr + machine.stderr + changed.stderr
