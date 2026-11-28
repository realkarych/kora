import asyncio
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from kora.client.transport import KoraClient
from kora.db.connection import Database
from kora.db.tables import attempt_dispatcher, attempt_first_claim_basis, event
from kora.models.worker_claims import WorkerClaim, WorkerClaimRequest
from tests.api.deployment_cases import NAMESPACE
from tests.api.scheduler_claim_support import PARAMS, service_identity
from tests.api.test_installed_workers import ENABLED, stage_root
from tests.api.test_packaging import (
    LOOPBACK,
    api_environment,
    read_bound_port,
    terminate_installed_api,
)
from tests.api.worker_support import bearer, registration
from tests.engine.test_run_event_positions import PositionSnapshot, snapshot
from tests.plugins.authentication.support import seed_case
from tests.support.databases import sqlite_settings


@dataclass(frozen=True)
class ClaimRows:
    receipt: dict[str, object]
    basis: dict[str, object]
    claimed: dict[str, object]
    events: tuple[dict[str, object], ...]
    positions: PositionSnapshot | None


@dataclass(frozen=True)
class InstalledClaimReceipt:
    token: str
    worker_id: str
    request: WorkerClaimRequest
    claim: WorkerClaim
    rows: ClaimRows


@asynccontextmanager
async def installed_worker(
    python: Path, uid: int, gid: int, path: Path
) -> AsyncGenerator[str]:
    separate_principal = (uid, gid) != (os.geteuid(), os.getegid())
    child = await asyncio.create_subprocess_exec(
        str(python),
        "-I",
        "-m",
        "kora",
        "api",
        "--port",
        "0",
        env=api_environment(str(sqlite_settings(path).url)) | ENABLED,
        user=uid if separate_principal else None,
        group=gid if separate_principal else None,
        extra_groups=[] if separate_principal else None,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        port, early = await read_bound_port(child)
        yield f"http://{LOOPBACK}:{port}"
    finally:
        output, trailing = await terminate_installed_api(child)
    assert child.returncode == 0, early + trailing
    assert output == b""


async def claim_rows(path: Path, claim: WorkerClaim, *, positioned: bool) -> ClaimRows:
    database = Database(sqlite_settings(path))
    try:
        async with database.read_snapshot() as reader:
            with await reader.execute(
                select(attempt_dispatcher).where(
                    attempt_dispatcher.c.id == claim.dispatcher_id,
                    attempt_dispatcher.c.attempt_id == claim.attempt_id,
                )
            ) as receipts:
                receipt = dict(receipts.mappings().one())
            with await reader.execute(
                select(attempt_first_claim_basis).where(
                    attempt_first_claim_basis.c.dispatcher_id == claim.dispatcher_id
                )
            ) as basis_rows:
                basis = dict(basis_rows.mappings().one())
            with await reader.execute(
                select(event)
                .where(
                    event.c.run_id == claim.run_id,
                    event.c.namespace_id == claim.namespace_id,
                )
                .order_by(event.c.id)
            ) as run_events:
                events = tuple(dict(row) for row in run_events.mappings())
            [claimed] = [
                record for record in events if record["kind"] == "attempt.claimed"
            ]
            assert claimed["attempt_id"] == claim.attempt_id
            positions = (
                await snapshot(reader, claim.namespace_id, claim.run_id)
                if positioned
                else None
            )
    finally:
        await database.close()
    return ClaimRows(receipt, basis, claimed, events, positions)


async def issue_installed_claim(
    python: Path,
    uid: int,
    gid: int,
    path: Path,
    directory: Path,
) -> InstalledClaimReceipt:
    settings = sqlite_settings(path)
    database = Database(settings)
    try:
        user = await seed_case(database, NAMESPACE)
        namespace_id = user.principal.namespaces[0].namespace_id
        service = await service_identity(database, namespace_id)
    finally:
        await database.close()
    identity = registration(service)
    request = WorkerClaimRequest(
        runtime=identity.runtime,
        incarnation_id=identity.incarnation_id,
        execution_domain_id=identity.execution_domain_id,
        max=1,
        claim_id="installed-maintenance-claim",
    )
    token = bearer(service)
    async with (
        installed_worker(python, uid, gid, path) as origin,
        KoraClient(origin, token) as client,
    ):
        staging = Database(settings)
        try:
            accepted = await stage_root(
                python, directory, origin, staging, bearer(user), namespace_id
            )
        finally:
            await staging.close()
        worker = await client.register_worker(identity)
        response = await client.claim_worker(worker.worker_id, request)
    claim = response.claim
    assert claim is not None
    assert claim.run_id == accepted.run_id.removeprefix("run_")
    assert claim.namespace_id == namespace_id
    assert claim.params == PARAMS
    assert claim.deadline.value is not None
    rows = await claim_rows(path, claim, positioned=False)
    return InstalledClaimReceipt(token, worker.worker_id, request, claim, rows)


async def replay_installed_claim(
    python: Path,
    uid: int,
    gid: int,
    path: Path,
    held: InstalledClaimReceipt,
    *,
    positioned: bool = False,
) -> None:
    before = await claim_rows(path, held.claim, positioned=positioned)
    assert before.receipt == held.rows.receipt
    assert before.basis == held.rows.basis
    assert before.claimed == held.rows.claimed
    assert before.events == held.rows.events
    if before.positions is not None:
        assert before.positions.high_water == len(before.positions.positions)
        assert [
            key
            for _, key, kind in before.positions.positions
            if kind == "attempt.claimed"
        ] == [held.rows.claimed["id"]]
    async with (
        installed_worker(python, uid, gid, path) as origin,
        KoraClient(origin, held.token) as client,
    ):
        replay = await client.claim_worker(held.worker_id, held.request)
    assert replay.claim == held.claim
    assert await claim_rows(path, held.claim, positioned=positioned) == before
