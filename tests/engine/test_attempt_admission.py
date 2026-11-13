from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select

from kora.db.claim_sources import acquire_claim_sources
from kora.db.connection import Database
from kora.db.tables import attempt, wait
from kora.engine.attempt_admission import AttemptAdmissionState, admit_attempt
from kora.engine.attempt_capacity import PreparedGraphCaps
from kora.engine.attempt_preparation_budget import attempt_preparation_reservation
from kora.engine.claim_sources import decode_claim_sources
from kora.engine.locks import TransitionLocks
from kora.engine.namespace_policy import (
    NamespacePolicyConfiguration,
    write_namespace_policy,
)
from kora.engine.price_initialization import (
    PriceConfiguration,
    import_price_revision,
    price_scopes,
)
from kora.ir.deployment_checks import DeploymentSelection
from kora.protocols.worker_claim_wire import ClaimWireRejected
from tests.db.closing import ConnectionLedger
from tests.db.seed import Seeder
from tests.engine.admission_wait_support import INTERVAL, LIMITS
from tests.engine.attempt_admission_support import (
    INSTALLATION,
    PREPARATION_LIMITS,
    attempt_admission_case,
)
from tests.engine.expiry_support import expiry_context


@pytest.mark.asyncio
async def test_attempt_admission_prepares_real_claim_and_commits_once(
    database: Database,
) -> None:
    case = await attempt_admission_case(database)
    ledger = ConnectionLedger(database)
    prepared = case.prepare()
    caps = PreparedGraphCaps(case.promotion.scope, None, None)
    assert ledger.checkouts == 0

    async with database.transaction() as writer:
        context = await expiry_context(writer, case.promotion)
        outcome = await admit_attempt(
            context,
            prepared,
            LIMITS,
            caps,
            global_limit=1,
            runtime_limit=1,
            interval=INTERVAL,
        )
    async with database.transaction() as writer:
        context = await expiry_context(writer, case.promotion)
        replay = await admit_attempt(
            context,
            prepared,
            LIMITS,
            caps,
            global_limit=1,
            runtime_limit=1,
            interval=INTERVAL,
        )
    async with database.read_transaction() as reader:
        with await reader.execute(select(attempt)) as rows:
            saved = rows.mappings().one()
        acquired = await acquire_claim_sources(reader, case.binding, LIMITS)
    run, nodes, events = await case.promotion.snapshot(database)

    assert outcome is AttemptAdmissionState.QUEUED
    assert replay is AttemptAdmissionState.REFUSED
    assert saved["status"] == "created"
    assert saved["execution_state"] == "not_started"
    assert saved["acknowledged_segment_offset"] == saved["lease_epoch"] == 0
    assert all(
        saved[name] is None
        for name in (
            "started_at",
            "deadline",
            "deadline_binding",
            "worker_id",
            "lease_owner",
            "execution_domain_id",
            "execution_policy",
            "execution_request",
            "handle",
        )
    )
    assert acquired is not None
    assert (
        decode_claim_sources(acquired).values["input_bindings"]
        == '{"version":1,"inputs":[]}'
    )
    node = next(node for node in nodes if node["id"] == case.binding.node_id)
    assert node["status"] == "queued"
    assert node["current_attempt_id"] == case.binding.attempt_id
    assert node["attempt_count"] == 1
    assert node["started_at"] is None
    assert run["needs_scheduling"] is True
    assert [
        event["kind"] for event in sorted(events, key=lambda event: str(event["id"]))
    ] == [
        "run.started",
        "node.ready",
        "node.queued",
    ]
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["retained", "work", "owned", "source"])
async def test_attempt_preparation_refuses_incomplete_fresh_allowance(
    database: Database, boundary: str
) -> None:
    case = await attempt_admission_case(database)
    reservation = attempt_preparation_reservation(case.checked, PREPARATION_LIMITS)
    admitted = replace(
        PREPARATION_LIMITS,
        retained_units=reservation.retained_units,
        work_units=reservation.work_units,
    )
    if boundary == "source":
        graph = replace(
            admitted.graph,
            identity=replace(
                admitted.graph.identity,
                max_encoded_bytes=1,
            ),
        )
        refused = replace(admitted, graph=graph)
    elif boundary == "owned":
        refused = replace(admitted, owned_units=admitted.owned_units + 1)
    elif boundary == "retained":
        refused = replace(admitted, retained_units=admitted.retained_units - 1)
    else:
        refused = replace(admitted, work_units=admitted.work_units - 1)
    ledger = ConnectionLedger(database)

    with pytest.raises(ClaimWireRejected):
        case.prepare(limits=refused)
    prepared = case.prepare(limits=admitted)

    assert prepared.binding == case.binding
    assert ledger.checkouts == ledger.outstanding == database.units_in_flight() == 0
    async with database.read_transaction() as reader:
        assert await reader.scalar(select(attempt.c.id)) is None


@pytest.mark.asyncio
async def test_attempt_admission_rejects_current_installation_change(
    database: Database,
) -> None:
    case = await attempt_admission_case(database)
    changed = DeploymentSelection("codex", INSTALLATION.runtime)

    with pytest.raises(ClaimWireRejected):
        case.prepare(changed)

    async with database.read_transaction() as reader:
        assert await reader.scalar(select(attempt.c.id)) is None


@pytest.mark.asyncio
async def test_attempt_admission_wait_reserves_nothing_then_closes_exact_wait(
    database: Database,
) -> None:
    case = await attempt_admission_case(database)
    prepared = case.prepare()
    caps = PreparedGraphCaps(case.promotion.scope, None, None)
    async with database.transaction() as writer:
        seed = Seeder(writer)
        blocker = await seed.attempt(
            await seed.node(case.promotion.run_id, "historical"), "created"
        )

    async with database.transaction() as writer:
        state = await admit_attempt(
            await expiry_context(writer, case.promotion),
            prepared,
            LIMITS,
            caps,
            global_limit=1,
            runtime_limit=None,
            interval=INTERVAL,
        )
    async with database.read_transaction() as reader:
        with await reader.execute(select(wait)) as rows:
            saved_wait = dict(rows.mappings().one())
        with await reader.execute(select(attempt.c.id)) as rows:
            attempts = rows.scalars().all()
    async with database.transaction() as writer:
        admitted = await admit_attempt(
            await expiry_context(writer, case.promotion),
            prepared,
            LIMITS,
            caps,
            global_limit=2,
            runtime_limit=None,
            interval=INTERVAL,
            expected_wait_id=str(saved_wait["id"]),
        )
    async with database.read_transaction() as reader:
        with await reader.execute(select(wait)) as rows:
            closed = dict(rows.mappings().one())

    assert state is AttemptAdmissionState.WAITING
    assert attempts == [blocker]
    assert saved_wait["status"] == "open"
    assert admitted is AttemptAdmissionState.QUEUED
    assert closed["id"] == saved_wait["id"]
    assert closed["created_at"] == saved_wait["created_at"]
    assert closed["reason"] == saved_wait["reason"]
    assert closed["status"] == "satisfied"


@pytest.mark.asyncio
async def test_attempt_admission_expiry_before_creation_keeps_attempt_absent(
    database: Database,
) -> None:
    case = await attempt_admission_case(database)
    prepared = case.prepare()
    caps = PreparedGraphCaps(case.promotion.scope, None, None)
    at = case.promotion.clock.now() + timedelta(hours=1)

    async with database.transaction() as writer:
        context = replace(
            await expiry_context(writer, case.promotion), clock=lambda: at
        )
        outcome = await admit_attempt(
            context,
            prepared,
            LIMITS,
            caps,
            global_limit=1,
            runtime_limit=None,
            interval=INTERVAL,
        )
    async with database.read_transaction() as reader:
        found = await reader.scalar(select(attempt.c.id))

    assert outcome is AttemptAdmissionState.EXPIRED
    assert found is None


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["policy", "price"])
async def test_attempt_admission_rechecks_fresh_policy_and_price(
    database: Database, changed: str
) -> None:
    case = await attempt_admission_case(database)
    prepared = case.prepare()
    caps = PreparedGraphCaps(case.promotion.scope, None, None)
    async with database.transaction() as writer:
        locks = TransitionLocks(writer)
        await locks.acquire_scopes(price_scopes(case.promotion.scope.namespace_id))
        if changed == "policy":
            current = NamespacePolicyConfiguration.prepare(
                case.policy.namespace_id, 2, case.policy.value()
            )
            await write_namespace_policy(
                writer, locks, current, case.promotion.clock.now()
            )
        else:
            revision = PriceConfiguration(
                case.promotion.scope.namespace_id, case.promotion.clock.now(), {}
            ).prepare(case.promotion.identifiers.new())
            await import_price_revision(
                writer, locks, revision, case.promotion.clock.now()
            )

    async with database.transaction() as writer:
        result = await admit_attempt(
            await expiry_context(writer, case.promotion),
            prepared,
            LIMITS,
            caps,
            global_limit=1,
            runtime_limit=None,
            interval=INTERVAL,
        )
    saved, nodes, events = await case.promotion.snapshot(database)
    async with database.read_transaction() as reader:
        found = await reader.scalar(select(attempt.c.id))

    assert result is AttemptAdmissionState.REFUSED
    assert found is None
    assert all(node["status"] == "pending" for node in nodes)
    assert [event["kind"] for event in events] == ["run.started"]
    assert saved["status"] == "running"


class AdmissionRolledBack(RuntimeError):
    pass


@pytest.mark.asyncio
async def test_attempt_admission_rollback_removes_sources_pointer_and_events(
    database: Database,
) -> None:
    case = await attempt_admission_case(database)
    prepared = case.prepare()
    caps = PreparedGraphCaps(case.promotion.scope, None, None)
    ledger = ConnectionLedger(database)
    before = await case.promotion.snapshot(database)

    async def rollback() -> None:
        async with database.transaction() as writer:
            state = await admit_attempt(
                await expiry_context(writer, case.promotion),
                prepared,
                LIMITS,
                caps,
                global_limit=1,
                runtime_limit=None,
                interval=INTERVAL,
            )
            assert state is AttemptAdmissionState.QUEUED
            raise AdmissionRolledBack()

    with pytest.raises(AdmissionRolledBack):
        await rollback()
    after = await case.promotion.snapshot(database)
    async with database.read_transaction() as reader:
        found = await acquire_claim_sources(reader, case.binding, LIMITS)
        attempt_id = await reader.scalar(select(attempt.c.id))

    assert before == after
    assert found is None
    assert attempt_id is None
    assert ledger.outstanding == database.units_in_flight() == 0
