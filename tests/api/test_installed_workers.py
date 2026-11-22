import asyncio
import json
import os
import signal
import socket
import subprocess
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select, update

from kora.api.graph_services import deployment_selection
from kora.api.installed_worker import installed_worker_profile
from kora.client.errors import ApiError
from kora.client.transport import KoraClient
from kora.db.connection import Database
from kora.db.identifiers import IdentifierGenerator
from kora.db.tables import (
    api_token,
    attempt,
    attempt_dispatcher,
    price_table,
    run,
    run_inputs,
)
from kora.db.tokens import REVOKED
from kora.engine.admission_processing import AdmissionProcessor
from kora.engine.attempt_admission import AttemptAdmissionState
from kora.engine.expiry import record_run_expiry
from kora.engine.run_input_values import encode_run_input_value
from kora.engine.run_promotion import PromotionOutcome
from kora.engine.start_records import MAX_PARAMS_BYTES
from kora.ir.json import parse_json_object
from kora.models.errors import ApiErrorEnvelope
from kora.models.graphs import DeployResponse
from kora.models.runs import AcceptedRun
from kora.models.worker_claims import WorkerClaimRequest, WorkerClaimResponse
from kora.plugins.runtime.local.configuration import LocalRuntimeConfiguration
from kora.protocols.time import SystemClock
from tests.api.deployment_cases import GRAPH, NAMESPACE
from tests.api.scheduler_claim_support import (
    PARAMS,
    node_id,
    scheduler_claim_ir,
    service_identity,
)
from tests.api.serving_lifecycle_child import serving_event
from tests.api.test_packaging import (
    DOMAIN_HARNESS,
    DRAIN_RECEIPT_TIMEOUT,
    LOOPBACK,
    SCHEMA,
    SERVE_TIMEOUT,
    api_command,
    api_environment,
    diagnostics,
    installed_cli,
    read_bound_port,
    refusal_reason,
    serving_installation,
    socket_port,
    terminate_installed_api,
)
from tests.api.test_worker_registration import changed, registry_state
from tests.api.worker_support import bearer, registration
from tests.db.seed import START, FakeClock
from tests.db.test_first_claim_basis_migration import upgrade_to_predecessor
from tests.engine.admission_processing_support import LIMITS, process
from tests.engine.admission_wait_support import INTERVAL
from tests.engine.attempt_admission_support import SELECTION
from tests.engine.claim_support import state
from tests.engine.expiry_support import ExecutionCase, execution_case, expiry_context
from tests.engine.graph_output_cases import child as ir_child
from tests.engine.test_claim_expiry_handoff import expiry_wait_state, open_expiry_wait
from tests.engine.test_http_trigger_admission import KEY, promotion_from_http
from tests.plugins.authentication.support import AuthenticationCase, seed_case
from tests.support.databases import copy_sqlite_template, sqlite_settings

__all__ = ["serving_installation"]

WORKERS = "/api/v1/workers"
WORKER_SETTINGS = "KORA_API_WORKER_CLOCK_UNCERTAINTY_US"
ENABLED = {DOMAIN_HARNESS: "claude", WORKER_SETTINGS: "2000"}


@asynccontextmanager
async def installed_server(
    python: Path, directory: Path, url: str, settings: dict[str, str]
) -> AsyncGenerator[tuple[str, bytes]]:
    child = await asyncio.create_subprocess_exec(
        *api_command(python, 0),
        cwd=directory,
        env=api_environment(url) | settings,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        port, early = await read_bound_port(child)
        yield f"http://{LOOPBACK}:{port}", early
    finally:
        output, trailing = await terminate_installed_api(child)
    assert child.returncode == 0, early + trailing
    assert output == b""


def available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind((LOOPBACK, 0))
        return socket_port(listener)


async def assert_port_closed(port: int) -> None:
    with pytest.raises(ConnectionRefusedError):
        await asyncio.open_connection(LOOPBACK, port)


def assert_api_error(response: httpx.Response, status: int, code: str) -> None:
    assert response.status_code == status, response.content
    assert ApiErrorEnvelope.model_validate_json(response.content).error.code == code


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["bare", "graph", "worker"])
async def test_installed_sqlite_worker_routes_are_explicit_and_service_only(
    serving_installation: Path, tmp_path: Path, sqlite_template: Path, mode: str
) -> None:
    settings = copy_sqlite_template(sqlite_template, tmp_path / "metadata.sqlite")
    database = Database(settings)
    try:
        personal = await seed_case(database, NAMESPACE)
        worker = await service_identity(
            database, personal.principal.namespaces[0].namespace_id
        )
        revoked = await seed_case(database, "revoked-service", "service")
        async with database.transaction() as writer:
            await writer.execute(
                update(api_token)
                .where(api_token.c.id == revoked.principal.token_id)
                .values(expires_at=REVOKED)
            )
    finally:
        await database.close()
    setting = {"bare": {}, "graph": {DOMAIN_HARNESS: "claude"}, "worker": ENABLED}[mode]
    body = registration(worker).model_dump(mode="json")
    claim_path = f"{WORKERS}/{worker.principal.owner}/claims"

    worker_errors: list[tuple[httpx.Response, int, str]] = []
    async with (
        installed_server(
            serving_installation, tmp_path, str(settings.url), setting
        ) as (origin, early),
        httpx.AsyncClient(base_url=origin, trust_env=False, timeout=5) as client,
    ):
        schema = await client.get(
            SCHEMA, headers={"Authorization": str(personal.request.authorization)}
        )
        paths = parse_json_object(schema.json())["paths"]
        assert isinstance(paths, dict)
        visible = (WORKERS in paths, f"{WORKERS}/{{worker_id}}/claims" in paths)
        missing = await client.post(WORKERS, json=body)
        missing_claim = await client.post(claim_path, json={})
        if mode == "worker":
            wrong_kind = await client.post(
                WORKERS,
                json=body,
                headers={"Authorization": str(personal.request.authorization)},
            )
            revoked_claim = await client.post(
                claim_path,
                json={},
                headers={"Authorization": str(revoked.request.authorization)},
            )
            foreign = await client.post(
                WORKERS,
                json=body | {"name": "another-worker"},
                headers={"Authorization": str(worker.request.authorization)},
            )
            worker_errors = [
                (wrong_kind, 403, "API005"),
                (revoked_claim, 401, "API009"),
                (foreign, 403, "API005"),
            ]

    readback = Database(settings)
    try:
        effects = await registry_state(readback)
    finally:
        await readback.close()

    assert schema.status_code == 200
    assert all(not rows for rows in effects.values())
    enabled = mode == "worker"
    assert visible == (enabled, enabled)
    events = [record["event"] for record in diagnostics(early)]
    assert ("engine.admission_initialization.finished" in events) is enabled
    assert ("engine.claim_expiry_recovery.finished" in events) is enabled
    if enabled:
        assert_api_error(missing, 401, "API009")
        assert_api_error(missing_claim, 401, "API009")
        for response, status, code in worker_errors:
            assert_api_error(response, status, code)
    else:
        assert_api_error(missing, 404, "API001")
        assert_api_error(missing_claim, 404, "API001")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("harness", "uncertainty"),
    [
        (None, "2000"),
        ("claude", "0"),
        ("claude", "020"),
        ("claude", "86400000000000000000"),
        ("claude", "private-clock"),
    ],
)
async def test_installed_worker_rejects_invalid_selection_before_open(
    serving_installation: Path, tmp_path: Path, harness: str | None, uncertainty: str
) -> None:
    settings = sqlite_settings(tmp_path / "must-not-exist.sqlite")
    port = available_port()
    environment = api_environment(str(settings.url)) | {WORKER_SETTINGS: uncertainty}
    if harness is not None:
        environment[DOMAIN_HARNESS] = harness

    result = await asyncio.to_thread(
        subprocess.run,
        api_command(serving_installation, port),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        check=False,
        timeout=SERVE_TIMEOUT,
    )
    await assert_port_closed(port)

    assert result.returncode == 78
    assert refusal_reason(result) == "worker_configuration"
    assert not settings.sqlite_path.exists()
    assert b"private-clock" not in result.stderr
    assert all(
        record["event"] != "protocols.loading.provider_loaded"
        for record in diagnostics(result.stderr)
    )


@pytest.mark.asyncio
async def test_installed_worker_requires_current_schema_before_open(
    serving_installation: Path, tmp_path: Path
) -> None:
    settings = sqlite_settings(tmp_path / "predecessor.sqlite")
    database = Database(settings)
    try:
        await upgrade_to_predecessor(database)
    finally:
        await database.close()
    port = available_port()

    result = await asyncio.to_thread(
        subprocess.run,
        api_command(serving_installation, port),
        cwd=tmp_path,
        env=api_environment(str(settings.url)) | ENABLED,
        capture_output=True,
        check=False,
        timeout=SERVE_TIMEOUT,
    )
    await assert_port_closed(port)

    assert result.returncode == 78
    assert refusal_reason(result) == "sqlite_unprepared"
    assert all(
        record["event"] != "engine.admission_initialization.finished"
        for record in diagnostics(result.stderr)
    )


async def stage_root(
    python: Path,
    directory: Path,
    origin: str,
    database: Database,
    token: str,
    namespace_id: str,
    *,
    extended_params: str | None = None,
) -> AcceptedRun:
    ir = scheduler_claim_ir()
    ir_child(ir, "graph", "nodes", "agent", "config")["harness"] = "claude"
    if extended_params is not None:
        ir_child(ir, "graph", "params", "properties")["text"] = {"type": "string"}
    file = directory / "installed-worker.ir.json"
    file.write_text(json.dumps(ir))
    environment = {"KORA_URL": origin, "KORA_TOKEN": token, "KORA_NAMESPACE": NAMESPACE}
    deployed = await installed_cli(
        python, directory, environment, ["deploy", str(file), "--json"]
    )
    assert deployed.code == 0, deployed.stderr
    saved = DeployResponse.model_validate_json(deployed.stdout)
    started = await installed_cli(
        python,
        directory,
        environment,
        [
            "trigger",
            GRAPH,
            "--json",
            "--graph-version",
            saved.graph_version_id,
            "--idempotency-key",
            KEY,
            "--param",
            f"quantity={PARAMS['quantity']}",
        ],
    )
    assert started.code == 0, started.stderr
    accepted = AcceptedRun.model_validate_json(started.stdout)
    limits = LIMITS
    if extended_params is not None:
        profile = installed_worker_profile("2000")
        assert profile is not None
        inputs = profile.limits.snapshot.inputs
        payload = encode_run_input_value(
            PARAMS | {"text": extended_params}, max_bytes=MAX_PARAMS_BYTES
        )
        assert len(payload) == inputs.params == inputs.projection
        async with database.transaction() as writer:
            await writer.execute(
                update(run_inputs)
                .where(run_inputs.c.run_id == accepted.run_id.removeprefix("run_"))
                .values(cel_projection=payload, params=payload)
            )
        limits = replace(
            LIMITS,
            preparation=replace(
                LIMITS.preparation,
                snapshot=replace(LIMITS.preparation.snapshot, inputs=inputs),
            ),
        )
    clock = FakeClock(SystemClock().now())
    identifiers = IdentifierGenerator(clock.now)
    promotion, _ = await promotion_from_http(
        database, namespace_id, accepted, saved, clock, identifiers
    )
    assert await promotion.promote(database) is PromotionOutcome.APPLIED
    root = await node_id(database, promotion.run_id, "agent")
    runtime = LocalRuntimeConfiguration(state_root=None).capabilities()
    processor = AdmissionProcessor(
        database,
        limits,
        replace(SELECTION, default_stall_after=runtime.default_stall_after),
        deployment_selection("claude", runtime),
        clock.now,
        identifiers,
        1,
        1,
        INTERVAL,
    )
    assert (
        await process(processor, root, periodic=False) is AttemptAdmissionState.QUEUED
    )
    return accepted


@pytest.mark.asyncio
async def test_installed_sqlite_first_worker_claim_replays_original_grant(
    serving_installation: Path, tmp_path: Path, sqlite_template: Path
) -> None:
    settings = copy_sqlite_template(sqlite_template, tmp_path / "metadata.sqlite")
    database = Database(settings)
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
            claim_id="installed-claim",
        )

        async with (
            installed_server(
                serving_installation, tmp_path, str(settings.url), ENABLED
            ) as (origin, _),
            KoraClient(origin, bearer(service)) as worker,
        ):
            accepted = await stage_root(
                serving_installation,
                tmp_path,
                origin,
                database,
                bearer(personal),
                namespace_id,
            )
            registered = await worker.register_worker(identity)
            first = await worker.claim_worker(registered.worker_id, request)
            before = await state(database)
            registration_replay = await worker.register_worker(identity)
            claim_replay = await worker.claim_worker(registered.worker_id, request)
            replayed_state = await state(database)
            async with database.transaction() as writer:
                await writer.execute(
                    update(run_inputs)
                    .where(run_inputs.c.run_id == accepted.run_id.removeprefix("run_"))
                    .values(cel_projection=b"x" * (MAX_PARAMS_BYTES + 1))
                )
            with pytest.raises(ApiError) as rejected_source:
                await worker.claim_worker(registered.worker_id, request)
        after = await state(database)
    finally:
        await database.close()

    assert registration_replay == registered
    assert claim_replay == first
    assert replayed_state == before == after
    assert rejected_source.value.status == 500
    assert rejected_source.value.code == "API010"
    assert first.claim is not None
    [saved] = after["attempt"]
    [receipt] = after["attempt_dispatcher"]
    [basis] = after["attempt_first_claim_basis"]
    assert first.claim.attempt_id == saved["id"] == receipt["attempt_id"]
    assert first.claim.dispatcher_id == receipt["id"] == basis["dispatcher_id"]
    assert first.claim.run_id == accepted.run_id.removeprefix("run_")
    assert (
        first.claim.execution_context.graph_version_id
        == accepted.graph_version_id.removeprefix("gv_")
    )
    assert first.claim.params == PARAMS
    assert first.claim.input_bindings == {}
    assert first.claim.deadline.value is not None
    assert first.claim.deadline.value.deadline().clock_uncertainty == timedelta(
        microseconds=2000
    )
    assert basis["configured_stall_us"] == "900000000"
    assert sum(record["kind"] == "worker.registered" for record in after["event"]) == 1


async def latched_expiry(
    database: Database, *, corrupt: bool = False
) -> tuple[ExecutionCase, str, datetime]:
    case = await execution_case(database, timeout="1h", granted=False)
    wait_id = await open_expiry_wait(database, case.promotion.run_id)
    cutoff = case.promotion.clock.now() + timedelta(hours=1)
    async with database.transaction() as writer:
        await record_run_expiry(await expiry_context(writer, case.promotion), cutoff)
        if corrupt:
            await writer.execute(
                update(run)
                .where(run.c.id == case.promotion.run_id)
                .values(retention={"private-latch-width": "x" * 1_010_000})
            )
    return case, wait_id, cutoff


@pytest.mark.asyncio
async def test_installed_sqlite_recovers_latched_expiry_before_bind(
    serving_installation: Path, tmp_path: Path, sqlite_template: Path
) -> None:
    settings = copy_sqlite_template(sqlite_template, tmp_path / "metadata.sqlite")
    database = Database(settings)
    try:
        case, wait_id, cutoff = await latched_expiry(database)

        async with installed_server(
            serving_installation, tmp_path, str(settings.url), ENABLED
        ) as (_, early):
            observed = await expiry_wait_state(database, wait_id)
            cancelled = await case.snapshot(database)
            async with database.read_transaction() as reader:
                baseline = (await reader.scalars(select(price_table.c.id))).all()
        async with installed_server(
            serving_installation, tmp_path, str(settings.url), ENABLED
        ):
            replayed = await expiry_wait_state(database, wait_id)
            replayed_cancellation = await case.snapshot(database)
            async with database.read_transaction() as reader:
                replayed_baseline = (
                    await reader.scalars(select(price_table.c.id))
                ).all()
    finally:
        await database.close()

    assert observed == replayed == ("cancelled", cutoff)
    assert cancelled == replayed_cancellation
    assert cancelled["desired_state"] == "cancel"
    assert cancelled["cancel_cause"] == "timeout"
    assert cancelled["updated_at"] == cutoff
    assert len(baseline) == 1
    assert replayed_baseline == baseline
    assert (
        early.index(b"engine.admission_initialization.finished")
        < early.index(b"engine.claim_expiry_recovery.finished")
        < early.index(b"api.serving_bound")
    )


@pytest.mark.asyncio
async def test_installed_sqlite_refuses_unreadable_expiry_before_bind(
    serving_installation: Path, tmp_path: Path, sqlite_template: Path
) -> None:
    settings = copy_sqlite_template(sqlite_template, tmp_path / "metadata.sqlite")
    database = Database(settings)
    try:
        _, wait_id, _ = await latched_expiry(database, corrupt=True)
        port = available_port()

        result = await asyncio.to_thread(
            subprocess.run,
            api_command(serving_installation, port),
            cwd=tmp_path,
            env=api_environment(str(settings.url)) | ENABLED,
            capture_output=True,
            check=False,
            timeout=SERVE_TIMEOUT,
        )
        await assert_port_closed(port)
        observed = await expiry_wait_state(database, wait_id)
    finally:
        await database.close()

    assert result.returncode == 70
    assert refusal_reason(result) == "startup_failed"
    assert b"engine.claim_expiry_recovery.refused" in result.stderr
    assert b"private-latch-width" not in result.stderr
    assert observed[0] == "open"


@asynccontextmanager
async def observed_worker_process(
    python: Path, directory: Path, url: str, mode: str, port: int
) -> AsyncGenerator[tuple[asyncio.subprocess.Process, int]]:
    fixture = Path(__file__).with_name("serving_lifecycle_child.py")
    release_read, release_write = os.pipe()
    try:
        try:
            child = await asyncio.create_subprocess_exec(
                str(python),
                "-I",
                str(fixture),
                mode,
                str(port),
                str(release_read),
                pass_fds=(release_read,),
                cwd=directory,
                env=api_environment(url),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        finally:
            os.close(release_read)
        try:
            async with asyncio.timeout(SERVE_TIMEOUT):
                yield child, release_write
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()
    finally:
        os.close(release_write)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stop", "expired", "exit_code"),
    [
        (signal.SIGTERM, False, 0),
        (signal.SIGINT, False, 130),
        (signal.SIGTERM, True, 70),
    ],
    ids=["settled-term", "settled-int", "expired-term"],
)
async def test_installed_worker_stop_during_recovery_never_binds(
    serving_installation: Path,
    tmp_path: Path,
    sqlite_template: Path,
    stop: signal.Signals,
    expired: bool,
    exit_code: int,
) -> None:
    settings = copy_sqlite_template(sqlite_template, tmp_path / "metadata.sqlite")
    database = Database(settings)
    try:
        _, wait_id, cutoff = await latched_expiry(database)
        port = available_port()
        async with observed_worker_process(
            serving_installation,
            tmp_path,
            str(settings.url),
            "hold_worker_recovery",
            port,
        ) as (child, release):
            assert child.stdout is not None
            assert child.stderr is not None
            held = await child.stdout.readline()
            await assert_port_closed(port)

            child.send_signal(stop)
            await serving_event(child.stderr, "api.serving_draining")
            refused = None
            if expired:
                async with asyncio.timeout(DRAIN_RECEIPT_TIMEOUT):
                    refused = await serving_event(child.stderr, "api.serving_refused")
            retained = child.returncode is None
            pending = await expiry_wait_state(database, wait_id)
            await assert_port_closed(port)
            os.write(release, b"r")
            output, trailing = await child.communicate()
            await assert_port_closed(port)
        observed = await expiry_wait_state(database, wait_id)
    finally:
        await database.close()

    assert held == b"native_sqlite_held\n"
    assert retained
    assert pending[0] == "open"
    assert observed == ("cancelled", cutoff)
    assert child.returncode == exit_code, trailing
    if expired:
        assert refused is not None
        assert refused["reason"] == "drain_unfinished"
    assert b"api.serving_refused" not in trailing
    assert b"api.serving_bound" not in trailing
    assert output.splitlines() == [
        b"native_sqlite_returned",
        b"native_sqlite_result_closed",
        b"lifespan_shutdown",
        b"domain_owners_settled",
    ]


async def same_owner_service(
    database: Database, service: AuthenticationCase, name: str, *, revoked: bool
) -> str:
    other = await seed_case(database, name, "service")
    async with database.transaction() as writer:
        await writer.execute(
            update(api_token)
            .where(api_token.c.id == other.principal.token_id)
            .values(
                owner=service.principal.owner,
                namespaces=[
                    grant.model_dump() for grant in service.principal.namespaces
                ],
                expires_at=REVOKED if revoked else None,
            )
        )
    authorization = other.request.authorization
    assert authorization is not None
    return authorization


async def claim_error_precedence(
    database: Database,
    client: httpx.AsyncClient,
    path: str,
    request: WorkerClaimRequest,
    authorization: str,
) -> tuple[httpx.Response, httpx.Response]:
    async with database.transaction() as writer:
        saved_digest = await writer.scalar(select(attempt_dispatcher.c.claim_digest))
        saved_lease = await writer.scalar(select(attempt.c.lease_until))
        assert isinstance(saved_digest, str)
        assert isinstance(saved_lease, datetime)
        await writer.execute(update(attempt_dispatcher).values(claim_digest="f" * 64))
    before = await state(database)

    digest_conflict = await client.post(
        path,
        json=request.model_dump(mode="json"),
        headers={"Authorization": authorization},
    )
    assert await state(database) == before

    async with database.transaction() as writer:
        await writer.execute(
            update(attempt_dispatcher).values(claim_digest=saved_digest)
        )
        await writer.execute(update(attempt).values(lease_until=START))
    before = await state(database)

    stale_grant = await client.post(
        path,
        json=request.model_dump(mode="json"),
        headers={"Authorization": authorization},
    )
    assert await state(database) == before

    async with database.transaction() as writer:
        await writer.execute(update(attempt).values(lease_until=saved_lease))
    return digest_conflict, stale_grant


@pytest.mark.asyncio
async def test_installed_worker_claim_admission_releases_before_socket_drain(
    serving_installation: Path, tmp_path: Path, sqlite_template: Path
) -> None:
    settings = copy_sqlite_template(sqlite_template, tmp_path / "metadata.sqlite")
    database = Database(settings)
    parameter_overhead = len(
        encode_run_input_value(PARAMS | {"text": ""}, max_bytes=MAX_PARAMS_BYTES)
    )
    params = "a" * (MAX_PARAMS_BYTES - parameter_overhead)
    try:
        personal = await seed_case(database, NAMESPACE)
        namespace_id = personal.principal.namespaces[0].namespace_id
        service = await service_identity(database, namespace_id)
        foreign_service = await seed_case(
            database, "foreign-registered-worker", "service"
        )
        wrong_token = await same_owner_service(
            database, service, "wrong-worker-token", revoked=False
        )
        revoked = await same_owner_service(
            database, service, "revoked-worker-token", revoked=True
        )
        authorization = service.request.authorization
        assert authorization is not None
        assert personal.request.authorization is not None
        async with installed_server(
            serving_installation, tmp_path, str(settings.url), ENABLED
        ) as (origin, _):
            accepted = await stage_root(
                serving_installation,
                tmp_path,
                origin,
                database,
                bearer(personal),
                namespace_id,
                extended_params=params,
            )
        identity = registration(service)
        original = WorkerClaimRequest(
            runtime=identity.runtime,
            incarnation_id=identity.incarnation_id,
            execution_domain_id=identity.execution_domain_id,
            max=1,
            claim_id="held-claim",
        )
        contender = original.model_copy(update={"claim_id": "next-poll"})
        async with observed_worker_process(
            serving_installation, tmp_path, str(settings.url), "hold_claim_response", 0
        ) as (child, release):
            assert child.stdout is not None
            assert child.stderr is not None
            port, _ = await read_bound_port(child)
            origin = f"http://{LOOPBACK}:{port}"
            async with KoraClient(origin, bearer(service)) as worker_client:
                worker = await worker_client.register_worker(identity)
            foreign_body = changed(
                registration(foreign_service),
                execution_domain_id="foreign-owned-domain",
                process={**identity.process.model_dump(mode="json"), "pid": 1236},
            )
            async with KoraClient(origin, bearer(foreign_service)) as foreign_client:
                await foreign_client.register_worker(foreign_body)
            before_registration_refusal = await registry_state(database)
            async with httpx.AsyncClient(
                base_url=origin, trust_env=False, timeout=5
            ) as client:
                registration_refusal = await client.post(
                    WORKERS,
                    headers={"Authorization": wrong_token},
                    json=changed(
                        identity, execution_domain_id=foreign_body.execution_domain_id
                    ).model_dump(mode="json"),
                )
            after_registration_refusal = await registry_state(database)
            path = f"{WORKERS}/{worker.worker_id}/claims"
            payload = original.model_dump_json().encode()
            head = (
                f"POST {path} HTTP/1.1\r\nHost: {LOOPBACK}:{port}\r\n"
                f"Authorization: {authorization}\r\nConnection: close\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(payload)}\r\n\r\n"
            ).encode("ascii")
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as slow_socket:
                slow_socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 32768)
                slow_socket.setblocking(False)
                await asyncio.get_running_loop().sock_connect(
                    slow_socket, (LOOPBACK, port)
                )
                reader, writer = await asyncio.open_connection(
                    sock=slow_socket, limit=8192
                )
                try:
                    writer.write(head + payload)
                    await writer.drain()
                    held = await child.stdout.readline()
                    before = await state(database)

                    async with httpx.AsyncClient(
                        base_url=origin, trust_env=False, timeout=5
                    ) as client:
                        refusals = [
                            await client.post(
                                path,
                                json=original.model_dump(mode="json"),
                                headers=headers,
                            )
                            for headers in (
                                {},
                                {"Authorization": personal.request.authorization},
                                {"Authorization": revoked},
                                {"Authorization": wrong_token},
                                {"Authorization": authorization},
                            )
                        ]
                        fresh_refusal = await client.post(
                            path,
                            json=contender.model_dump(mode="json"),
                            headers={"Authorization": authorization},
                        )
                        wrong_domain = await client.post(
                            path,
                            json=original.model_dump(mode="json")
                            | {"execution_domain_id": "wrong-domain"},
                            headers={"Authorization": authorization},
                        )
                        async with database.transaction() as db_writer:
                            await db_writer.execute(
                                update(api_token)
                                .where(api_token.c.id == service.principal.token_id)
                                .values(namespaces=[])
                            )
                        removed_namespace = await client.post(
                            path,
                            json=original.model_dump(mode="json"),
                            headers={"Authorization": authorization},
                        )
                        async with database.transaction() as db_writer:
                            await db_writer.execute(
                                update(api_token)
                                .where(api_token.c.id == service.principal.token_id)
                                .values(
                                    namespaces=[
                                        grant.model_dump()
                                        for grant in service.principal.namespaces
                                    ]
                                )
                            )
                        after_refusals = await state(database)
                        conflict, stale = await claim_error_precedence(
                            database, client, path, original, authorization
                        )
                        restored = await state(database)
                    os.write(release, b"r")
                    native_receipts = [
                        await child.stdout.readline(),
                        await child.stdout.readline(),
                        await child.stdout.readline(),
                    ]
                    async with KoraClient(origin, bearer(service)) as worker_client:
                        admitted_next = await worker_client.claim_worker(
                            worker.worker_id, contender
                        )

                    child.send_signal(signal.SIGTERM)
                    await serving_event(child.stderr, "api.serving_draining")
                    await assert_port_closed(port)
                    original_owner_retained = child.returncode is None
                    slow_socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 262144)
                    wire = await reader.read()
                finally:
                    writer.close()
                    await writer.wait_closed()
            output, trailing = await child.communicate()
    finally:
        await database.close()

    assert held == b"native_sqlite_held\n"
    assert_api_error(registration_refusal, 403, "API005")
    assert after_registration_refusal == before_registration_refusal
    assert after_refusals == restored == before
    expected = [
        (401, "API009"),
        (403, "API005"),
        (401, "API009"),
        (403, "API005"),
        (503, "API011"),
    ]
    for refused, (status, code) in zip(refusals, expected, strict=True):
        assert_api_error(refused, status, code)
    assert_api_error(wrong_domain, 403, "API005")
    assert_api_error(removed_namespace, 403, "API005")
    assert_api_error(conflict, 409, "API002")
    assert_api_error(stale, 409, "API008")
    assert_api_error(fresh_refusal, 503, "API011")
    for busy in (refusals[-1], fresh_refusal):
        error = ApiErrorEnvelope.model_validate_json(busy.content).error
        assert error.retryable
        assert error.details.model_dump() == {}
        assert error.request_id == busy.headers["x-request-id"]
    assert native_receipts == [
        b"native_sqlite_returned\n",
        b"native_sqlite_result_closed\n",
        b"claim_handed_to_socket\n",
    ]
    assert admitted_next.claim is None
    assert original_owner_retained
    headers, separator, body = wire.partition(b"\r\n\r\n")
    assert separator == b"\r\n\r\n"
    assert headers.startswith(b"HTTP/1.1 200 OK\r\n")
    original_claim = WorkerClaimResponse.model_validate_json(body).claim
    assert original_claim is not None
    assert original_claim.params == PARAMS | {"text": params}
    assert original_claim.run_id == accepted.run_id.removeprefix("run_")
    assert output.splitlines() == [b"lifespan_shutdown", b"domain_owners_settled"]
    assert child.returncode == 0, trailing
    assert b"api.serving_stopped" in trailing
    assert b"drain_unfinished" not in trailing
