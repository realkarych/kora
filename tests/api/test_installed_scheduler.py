import asyncio
import json
import os
import signal
import subprocess
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest

from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.api.installed_scheduler import (
    initial_admission_limits,
    installed_root_admission,
)
from kora.api.installed_worker import installed_worker_profile
from kora.client.transport import KoraClient
from kora.conformance.capture import Capture
from kora.db.connection import Database
from kora.engine.admission_preparation import (
    AdmissionPreparationRejected,
    prepare_admission_snapshot,
)
from kora.engine.admission_snapshot import AdmissionSnapshotRejected
from kora.engine.attempt_admission import AttemptAdmissionState
from kora.engine.attempt_preparation_budget import (
    AttemptPreparationLimits,
    attempt_preparation_reservation,
)
from kora.models.graphs import DeployResponse
from kora.models.runs import AcceptedRun
from kora.models.worker_claims import WorkerClaim, WorkerClaimRequest
from kora.observability.settings import COMPONENT_LEVELS_VARIABLE, LEVEL_VARIABLE
from tests.api.deployment_cases import GRAPH, NAMESPACE, preparer, skeleton_ir
from tests.api.deployment_cases import SELECTION as INSTALLATION
from tests.api.scheduler_claim_support import (
    PARAMS,
    scheduler_claim_ir,
    service_identity,
)
from tests.api.serving_lifecycle_child import serving_event
from tests.api.support import api_capture
from tests.api.test_installed_workers import (
    ENABLED,
    WORKER_SETTINGS,
    WORKERS,
    assert_api_error,
    assert_port_closed,
    available_port,
    installed_server,
    observed_worker_process,
)
from tests.api.test_packaging import (
    DOMAIN_HARNESS,
    DRAIN_RECEIPT_TIMEOUT,
    LOOPBACK,
    SERVE_TIMEOUT,
    api_command,
    api_environment,
    diagnostics,
    installed_cli,
    read_bound_port,
    refusal_reason,
    serving_installation,
    terminate_installed_api,
)
from tests.api.worker_support import CAPABILITIES, bearer, registration
from tests.db.closing import ConnectionLedger
from tests.engine.admission_processing_support import process
from tests.engine.admission_snapshot_support import acquire, snapshot_case
from tests.engine.claim_support import state
from tests.engine.graph_output_cases import child as ir_child
from tests.engine.start_cases import start_rows
from tests.plugins.authentication.support import seed_case
from tests.support.databases import copy_sqlite_template, sqlite_settings
from tests.support.observability import ProcessResult

__all__ = ["api_capture", "preparer", "serving_installation"]

SCHEDULER_SETTING = "KORA_API_SCHEDULER"
FIRST_CLAIM = {**ENABLED, SCHEDULER_SETTING: "first-claim"}
FIRST_CLAIM_DEBUG = FIRST_CLAIM | {
    LEVEL_VARIABLE: "INFO",
    COMPONENT_LEVELS_VARIABLE: "scheduler=DEBUG",
}
TRIGGER_KEY = "installed-scheduler-start"
SCHEDULER_LIFECYCLE = "scheduler.lifecycle"
SCHEDULER_TRANSITION = "scheduler.transition"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("selection", "postgres"),
    [
        pytest.param(ENABLED | {SCHEDULER_SETTING: ""}, False, id="empty"),
        pytest.param(
            ENABLED | {SCHEDULER_SETTING: "private-scheduler-value"},
            False,
            id="invalid",
        ),
        pytest.param({**FIRST_CLAIM, WORKER_SETTINGS: "0"}, False, id="worker-bound"),
        pytest.param(
            {DOMAIN_HARNESS: "claude", SCHEDULER_SETTING: "first-claim"},
            False,
            id="missing-worker",
        ),
        pytest.param(
            {WORKER_SETTINGS: "2000", SCHEDULER_SETTING: "first-claim"},
            False,
            id="missing-harness",
        ),
        pytest.param(FIRST_CLAIM, True, id="postgresql"),
    ],
)
async def test_installed_scheduler_selection_refuses_before_database_open(
    serving_installation: Path,
    tmp_path: Path,
    selection: dict[str, str],
    postgres: bool,
) -> None:
    settings = sqlite_settings(tmp_path / "must-not-exist.sqlite")
    database_url = (
        "postgresql+asyncpg://test:private@127.0.0.1:1/staging"
        if postgres
        else str(settings.url)
    )
    port = available_port()

    result = await asyncio.to_thread(
        subprocess.run,
        api_command(serving_installation, port),
        cwd=tmp_path,
        env=api_environment(database_url) | selection,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=SERVE_TIMEOUT,
    )
    await assert_port_closed(port)

    assert result.returncode == 78
    assert refusal_reason(result) == "scheduler_configuration"
    assert not settings.sqlite_path.exists()
    assert b"private-scheduler-value" not in result.stderr
    assert database_url.encode() not in result.stderr
    assert not any(
        row["event"]
        in (
            "protocols.loading.provider_loaded",
            "engine.admission_initialization.finished",
            "engine.claim_expiry_recovery.finished",
        )
        for row in diagnostics(result.stderr)
    )


async def trigger_installed(
    python: Path,
    directory: Path,
    environment: dict[str, str],
    graph_version_id: str,
) -> ProcessResult:
    trigger = await installed_cli(
        python,
        directory,
        environment,
        [
            "trigger",
            GRAPH,
            "--json",
            "--graph-version",
            graph_version_id,
            "--idempotency-key",
            TRIGGER_KEY,
            "--param",
            f"quantity={PARAMS['quantity']}",
        ],
    )
    assert trigger.code == 0, trigger.stderr
    return trigger


async def deploy_then_trigger(
    python: Path, directory: Path, origin: str, token: str
) -> tuple[DeployResponse, ProcessResult]:
    ir = scheduler_claim_ir()
    ir_child(ir, "graph", "nodes", "agent", "config")["harness"] = "claude"
    graph_file = directory / "installed-scheduler.ir.json"
    graph_file.write_text(json.dumps(ir))
    environment = {"KORA_URL": origin, "KORA_TOKEN": token, "KORA_NAMESPACE": NAMESPACE}
    deployment = await installed_cli(
        python, directory, environment, ["deploy", str(graph_file), "--json"]
    )
    assert deployment.code == 0, deployment.stderr
    deployed = DeployResponse.model_validate_json(deployment.stdout)
    trigger = await trigger_installed(
        python, directory, environment, deployed.graph_version_id
    )
    return deployed, trigger


async def run_installed_scheduler_until_queued(
    python: Path, directory: Path, database_url: str
) -> None:
    process = await asyncio.create_subprocess_exec(
        *api_command(python, 0),
        cwd=directory,
        env=api_environment(database_url) | FIRST_CLAIM_DEBUG,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        port, early = await read_bound_port(process)
        assert process.stderr is not None
        async with asyncio.timeout(SERVE_TIMEOUT):
            await serving_event(
                process.stderr, SCHEDULER_TRANSITION, outcome="admitted"
            )
    finally:
        output, trailing = await terminate_installed_api(process)

    assert process.returncode == 0, early + trailing
    assert output == b""
    assert b"api.serving_stopped" in trailing
    await assert_port_closed(port)


def assert_first_claim_is_bound_to_original_run(
    claim: WorkerClaim,
    deployed: DeployResponse,
    accepted: AcceptedRun,
    queued: dict[str, list[dict[str, object]]],
    granted: dict[str, list[dict[str, object]]],
    claim_id: str,
) -> None:
    [unclaimed] = queued["attempt"]
    assert unclaimed["status"] == "created"
    assert unclaimed["worker_id"] is unclaimed["deadline"] is None
    assert unclaimed["lease_epoch"] == 0
    assert queued["attempt_dispatcher"] == queued["attempt_first_claim_basis"] == []
    [row] = granted["attempt"]
    [receipt] = granted["attempt_dispatcher"]
    [basis] = granted["attempt_first_claim_basis"]
    [source] = granted["attempt_claim_read_admission"]
    [run] = granted["run"]
    assert claim.attempt_id == unclaimed["id"] == row["id"] == receipt["attempt_id"]
    assert claim.dispatcher_id == receipt["id"] == basis["dispatcher_id"]
    assert receipt["claim_id"] == claim_id
    assert claim.lease_epoch == row["lease_epoch"] == receipt["lease_epoch"] == 1
    assert claim.current_grant.worker_id == receipt["worker_id"] == row["worker_id"]
    assert claim.mode == "execute"
    assert claim.grant_history.count == 1
    assert row["lease_until"] == claim.lease_until
    assert source["attempt_id"] == claim.attempt_id
    assert source["source_revision"] == basis["source_revision"]
    assert basis["configured_stall_us"] == "900000000"
    assert claim.run_id == run["id"] == accepted.run_id.removeprefix("run_")
    assert (
        claim.execution_context.graph_version_id
        == run["graph_version_id"]
        == deployed.graph_version_id.removeprefix("gv_")
    )
    assert (
        claim.execution_context.execution_contract_digest
        == deployed.execution_contract_digest
    )
    assert claim.execution_context.kind == "agent"
    assert claim.params == PARAMS
    assert claim.input_bindings == {}
    assert (
        row["input_bindings"]
        == unclaimed["input_bindings"]
        == '{"version":1,"inputs":[]}'
    )
    assert claim.deadline.value is not None
    deadline = claim.deadline.value.deadline()
    assert deadline.clock_uncertainty == timedelta(microseconds=2000)
    started_at = run["started_at"]
    assert isinstance(started_at, datetime)
    assert deadline.at == row["deadline"] == started_at + timedelta(hours=1)
    kinds = [event["kind"] for event in granted["event"]]
    assert kinds.count("run.started") == kinds.count("attempt.claimed") == 1
    assert kinds.count("worker.registered") == 1


@pytest.mark.asyncio
async def test_installed_scheduler_restart_reaches_one_first_http_claim(
    serving_installation: Path, tmp_path: Path, sqlite_template: Path
) -> None:
    settings = copy_sqlite_template(sqlite_template, tmp_path / "metadata.sqlite")
    database = Database(settings)
    url = str(settings.url)
    try:
        personal = await seed_case(database, NAMESPACE)
        namespace_id = personal.principal.namespaces[0].namespace_id
        service = await service_identity(database, namespace_id)
        identity = registration(service)
        request = WorkerClaimRequest(
            runtime=identity.runtime,
            incarnation_id=identity.incarnation_id,
            execution_domain_id=identity.execution_domain_id,
            max=1,
            claim_id="installed-scheduler-claim",
        )

        async with installed_server(serving_installation, tmp_path, url, ENABLED) as (
            origin,
            _,
        ):
            deployed, original_trigger = await deploy_then_trigger(
                serving_installation, tmp_path, origin, bearer(personal)
            )
            accepted = AcceptedRun.model_validate_json(original_trigger.stdout)
            pending = await start_rows(database)
            before_scheduler = await state(database)
        await run_installed_scheduler_until_queued(serving_installation, tmp_path, url)
        queued = await state(database)
        async with (
            installed_server(
                serving_installation, tmp_path, url, FIRST_CLAIM_DEBUG
            ) as (origin, _),
            KoraClient(origin, bearer(service)) as worker,
            httpx.AsyncClient(base_url=origin, trust_env=False, timeout=5) as client,
        ):
            trigger_replay = await trigger_installed(
                serving_installation,
                tmp_path,
                {
                    "KORA_URL": origin,
                    "KORA_TOKEN": bearer(personal),
                    "KORA_NAMESPACE": NAMESPACE,
                },
                deployed.graph_version_id,
            )
            after_trigger_replay = await state(database)
            denied = await client.post(
                WORKERS,
                json=identity.model_dump(mode="json"),
                headers={"Authorization": str(personal.request.authorization)},
            )
            registered = await worker.register_worker(identity)
            first = await worker.claim_worker(registered.worker_id, request)
            granted = await state(database)
            replay = await worker.claim_worker(registered.worker_id, request)
            replayed = await state(database)
        settled = await state(database)
        saved_start = await start_rows(database)
    finally:
        await database.close()

    [pending_run] = pending["run"]
    [pending_receipt] = pending["receipt"]
    [pending_admission] = pending["admission"]
    assert pending_run["status"] == "pending"
    assert pending_run["needs_scheduling"] is True
    assert pending_run["started_at"] is None
    assert pending_receipt["idempotency_key"] == TRIGGER_KEY
    assert pending_receipt["id"] == pending_admission["receipt_id"]
    assert before_scheduler["node_instance"] == before_scheduler["attempt"] == []
    assert trigger_replay.stdout == original_trigger.stdout
    assert queued == after_trigger_replay
    assert pending["receipt"] == saved_start["receipt"]
    assert pending["run_inputs"] == saved_start["run_inputs"]
    assert pending["admission"] == saved_start["admission"]
    assert_api_error(denied, 403, "API005")
    assert first == replay
    assert granted == replayed == settled
    assert first.claim is not None
    assert_first_claim_is_bound_to_original_run(
        first.claim, deployed, accepted, queued, granted, request.claim_id
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stop", "expired", "exit_code"),
    [
        pytest.param(signal.SIGTERM, False, 0, id="native-term"),
        pytest.param(signal.SIGINT, False, 130, id="settled-int"),
        pytest.param(signal.SIGTERM, True, 70, id="expired-term"),
    ],
)
async def test_installed_scheduler_stop_waits_for_native_settlement(
    serving_installation: Path,
    tmp_path: Path,
    sqlite_template: Path,
    stop: signal.Signals,
    expired: bool,
    exit_code: int,
) -> None:
    settings = copy_sqlite_template(sqlite_template, tmp_path / "metadata.sqlite")
    database = Database(settings)
    url = str(settings.url)
    try:
        personal = await seed_case(database, NAMESPACE)
        async with installed_server(serving_installation, tmp_path, url, ENABLED) as (
            origin,
            _,
        ):
            await deploy_then_trigger(
                serving_installation, tmp_path, origin, bearer(personal)
            )
        pending = await start_rows(database)
        port = available_port()
        native_sigterm = stop is signal.SIGTERM and not expired
        mode = (
            "hold_scheduler_poll_native_term"
            if native_sigterm
            else "hold_scheduler_poll"
        )

        async with observed_worker_process(
            serving_installation, tmp_path, url, mode, port
        ) as (child, release):
            assert child.stdout is not None
            assert child.stderr is not None
            bound, _ = await read_bound_port(child)
            assert await child.stdout.readline() == b"native_sqlite_held\n"
            if native_sigterm:
                os.write(release, b"s")
                assert await child.stdout.readline() == b"native_sqlite_sigterm_sent\n"
            else:
                child.send_signal(stop)
            await serving_event(child.stderr, "api.serving_draining")
            refused = None
            if expired:
                async with asyncio.timeout(DRAIN_RECEIPT_TIMEOUT):
                    refused = await serving_event(child.stderr, "api.serving_refused")
            retained = child.returncode is None
            await assert_port_closed(port)
            before_release = await start_rows(database)
            os.write(release, b"r")
            output, trailing = await child.communicate()
        stopped = await start_rows(database)
        stopped_attempts = (await state(database))["attempt"]
        await run_installed_scheduler_until_queued(serving_installation, tmp_path, url)
        restarted = await state(database)
    finally:
        await database.close()

    assert bound == port
    assert retained
    assert stopped == before_release == pending
    assert stopped_attempts == []
    assert child.returncode == exit_code, trailing
    if expired:
        assert refused is not None
        assert refused["reason"] == "drain_unfinished"
    markers = output.splitlines()
    assert markers[-1] == b"domain_owners_settled"
    assert markers.count(b"native_sqlite_returned") == 1
    assert b"api.serving_refused" not in trailing
    assert SCHEDULER_TRANSITION.encode() not in trailing
    assert [
        row["outcome"]
        for row in diagnostics(trailing)
        if row["event"] == SCHEDULER_LIFECYCLE
    ] == ["stopped"]
    [created] = restarted["attempt"]
    assert created["status"] == "created"
    assert restarted["attempt_dispatcher"] == []


@pytest.mark.asyncio
async def test_installed_scheduler_refuses_startup_when_recovery_source_is_unreadable(
    serving_installation: Path, tmp_path: Path, sqlite_template: Path
) -> None:
    settings = copy_sqlite_template(sqlite_template, tmp_path / "metadata.sqlite")
    database = Database(settings)
    try:
        personal = await seed_case(database, NAMESPACE)
        async with installed_server(
            serving_installation, tmp_path, str(settings.url), ENABLED
        ) as (origin, _):
            await deploy_then_trigger(
                serving_installation, tmp_path, origin, bearer(personal)
            )
        pending = await start_rows(database)
    finally:
        await database.close()
    damaged = copy_sqlite_template(settings.sqlite_path, tmp_path / "unreadable.sqlite")
    database = Database(damaged)
    try:
        async with database.transaction() as writer:
            await writer.exec_driver_sql(
                "ALTER TABLE attempt RENAME TO private_scheduler_recovery_attempt"
            )
    finally:
        await database.close()
    port = available_port()

    result = await asyncio.to_thread(
        subprocess.run,
        api_command(serving_installation, port),
        cwd=tmp_path,
        env=api_environment(str(damaged.url)) | FIRST_CLAIM,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=SERVE_TIMEOUT,
    )
    await assert_port_closed(port)
    database = Database(damaged)
    try:
        persisted = await start_rows(database)
    finally:
        await database.close()

    assert result.returncode == 70, result.stderr
    assert refusal_reason(result) == "startup_failed"
    assert b"engine.claim_expiry_recovery.finished" in result.stderr
    assert SCHEDULER_LIFECYCLE.encode() not in result.stderr
    assert b"OperationalError" not in result.stderr
    assert b"no such table" not in result.stderr
    assert b"private_scheduler_recovery_attempt" not in result.stderr
    assert persisted == pending


@pytest.mark.asyncio
async def test_installed_scheduler_forced_drain_joins_reader_without_asgi_shutdown(
    serving_installation: Path, tmp_path: Path, sqlite_template: Path
) -> None:
    settings = copy_sqlite_template(sqlite_template, tmp_path / "metadata.sqlite")
    database = Database(settings)
    url = str(settings.url)
    try:
        personal = await seed_case(database, NAMESPACE)
        async with installed_server(serving_installation, tmp_path, url, ENABLED) as (
            origin,
            _,
        ):
            await deploy_then_trigger(
                serving_installation, tmp_path, origin, bearer(personal)
            )
        pending = await start_rows(database)
        port = available_port()

        async with observed_worker_process(
            serving_installation, tmp_path, url, "hold_scheduler_poll_forced", port
        ) as (child, release):
            assert child.stdout is not None
            assert child.stderr is not None
            bound, _ = await read_bound_port(child)
            assert await child.stdout.readline() == b"native_sqlite_held\n"
            reader, writer = await asyncio.open_connection(LOOPBACK, port)
            try:
                writer.write(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
                await writer.drain()
                assert await child.stdout.readline() == b"request_holds_sqlite\n"
                child.send_signal(signal.SIGINT)
                await serving_event(child.stderr, "api.serving_draining")
                child.send_signal(signal.SIGINT)
                async with asyncio.timeout(DRAIN_RECEIPT_TIMEOUT):
                    wire = await reader.read()
                    assert await child.stdout.readline() == b"request_settled\n"
                retained = child.returncode is None
                await assert_port_closed(port)
                before_release = await start_rows(database)
            finally:
                writer.close()
                await writer.wait_closed()
            os.write(release, b"r")
            output, trailing = await child.communicate()
        stopped = await start_rows(database)
        stopped_attempts = (await state(database))["attempt"]
        await run_installed_scheduler_until_queued(serving_installation, tmp_path, url)
        restarted = await state(database)
    finally:
        await database.close()

    assert bound == port
    assert wire == b""
    assert retained
    assert stopped == before_release == pending
    assert stopped_attempts == []
    assert child.returncode == 130, trailing
    assert output == (
        b"native_sqlite_returned\nscheduler_database_settled\ndomain_owners_settled\n"
    )
    assert b"drain_unfinished" not in trailing
    assert SCHEDULER_TRANSITION.encode() not in trailing
    assert [
        row["outcome"]
        for row in diagnostics(trailing)
        if row["event"] == SCHEDULER_LIFECYCLE
    ] == ["stopped"]
    [created] = restarted["attempt"]
    assert created["status"] == "created"
    assert restarted["attempt_dispatcher"] == []


@pytest.mark.asyncio
async def test_installed_scheduler_derives_exact_two_graph_reservations(
    database: Database, preparer: DeploymentPreparer, api_capture: Capture
) -> None:
    pending = await snapshot_case(database, preparer, api_capture)
    newer = skeleton_ir()
    ir_child(newer, "runtime")["concurrency"] = {"max_running_nodes": 2}
    await pending.publish(newer)
    worker = installed_worker_profile("2000")
    assert worker is not None
    installed = initial_admission_limits(worker.limits).preparation.snapshot
    before = ConnectionLedger(database)

    for short in (
        replace(installed, native_bytes=installed.native_bytes - 1),
        replace(installed, raw_bytes=installed.raw_bytes - 1),
    ):
        with pytest.raises(AdmissionSnapshotRejected):
            await acquire(database, pending, limits=short)
    refused_checkouts = before.checkouts
    accepted = await acquire(database, pending, limits=installed)

    assert installed.inputs == worker.limits.snapshot.inputs
    assert (installed.native_bytes, installed.raw_bytes) == (214_847_700, 144_867_812)
    assert refused_checkouts == 0
    assert accepted.graph is not accepted.cap_graph
    assert before.checkouts == 1
    assert before.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_installed_scheduler_bounds_detached_and_claim_preparation(
    database: Database, preparer: DeploymentPreparer, api_capture: Capture
) -> None:
    pending = await snapshot_case(database, preparer, api_capture)
    worker = installed_worker_profile("2000")
    assert worker is not None
    owner = installed_root_admission(
        database,
        worker.limits,
        pending.promotion.clock.now,
        pending.promotion.identifiers,
        INSTALLATION,
        CAPABILITIES,
    )
    preparation = owner.limits.preparation
    deadline = asyncio.get_running_loop().time() + worker.limits.read_seconds
    ledger = ConnectionLedger(database)

    with pytest.raises(AdmissionPreparationRejected):
        await prepare_admission_snapshot(
            database,
            pending.node_id,
            replace(preparation, work_units=preparation.work_units - 1),
            observed_at=owner.clock(),
            periodic_recheck=True,
            deadline=deadline,
        )
    rejected_checkouts = ledger.checkouts
    prepared = await prepare_admission_snapshot(
        database,
        pending.node_id,
        preparation,
        observed_at=owner.clock(),
        periodic_recheck=True,
        deadline=deadline,
    )
    exact = attempt_preparation_reservation(
        prepared.checked,
        AttemptPreparationLimits(
            preparation.snapshot.graph,
            prepared.reservation.retained_units,
            preparation.retained_units,
            owner.limits.claim_work,
        ),
        reconstruct_params=True,
    )
    short_retained = replace(
        owner.limits,
        preparation=replace(preparation, retained_units=exact.retained_units - 1),
    )
    short_work = replace(owner.limits, claim_work=exact.work_units - 1)
    refused = [
        await process(replace(owner, limits=limits), pending.node_id)
        for limits in (short_retained, short_work)
    ]
    intermediate = await state(database)
    admitted = await process(owner, pending.node_id)
    final = await state(database)

    assert rejected_checkouts == 0
    assert refused == [AttemptAdmissionState.REFUSED] * 2
    assert not intermediate["attempt"]
    assert admitted is AttemptAdmissionState.QUEUED
    assert len(final["attempt"]) == 1
    assert not final["attempt_dispatcher"]
    assert ledger.outstanding == database.units_in_flight() == 0
