import pytest
from sqlalchemy import select, update

from kora.db.claim_sources import acquire_claim_sources
from kora.db.connection import Database
from kora.db.tables import attempt, node_instance, wait
from kora.engine.attempt_admission import AttemptAdmissionState, admit_attempt
from kora.engine.attempt_capacity import PreparedGraphCaps
from kora.engine.attempt_inputs import read_attempt_inputs
from kora.engine.claim_sources import decode_claim_sources
from kora.protocols.worker_claim_wire import ClaimWireRejected
from tests.db.seed import Seeder
from tests.engine.admission_wait_support import INTERVAL, LIMITS
from tests.engine.expiry_support import expiry_context
from tests.engine.optional_input_support import (
    admit_optional_candidate,
    optional_admission_case,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["skipped", "failed", "upstream_failed", "cancelled"])
async def test_optional_input_absence_uses_current_unsuccessful_producer(
    database: Database, state: str
) -> None:
    case = await optional_admission_case(database, state)
    before = await case.promotion.snapshot(database)
    prepared = case.prepare()
    caps = PreparedGraphCaps(case.promotion.scope, None, None)

    async with database.transaction() as writer:
        result = await admit_attempt(
            await expiry_context(writer, case.promotion),
            prepared,
            LIMITS,
            caps,
            global_limit=1,
            runtime_limit=1,
            interval=INTERVAL,
        )
    async with database.read_transaction() as reader:
        acquired = await acquire_claim_sources(reader, case.binding, LIMITS)
    after = await case.promotion.snapshot(database)

    assert result is AttemptAdmissionState.QUEUED
    assert acquired is not None
    storage = decode_claim_sources(acquired).values["input_bindings"]
    node = case.readiness.nodes[1]
    inputs = read_attempt_inputs(storage, node.declared_inputs())
    assert inputs.claim_map() == {
        "candidate": {
            "source": {"kind": "absent"},
            "path": "commit_sha",
            "relation": "consumed",
        }
    }
    assert next(row for row in after[1] if row["node_path"] == "agent") == next(
        row for row in before[1] if row["node_path"] == "agent"
    )
    assert after[0]["graph_version_id"] == before[0]["graph_version_id"]
    assert after[0]["started_at"] == before[0]["started_at"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "rule"),
    [
        ("succeeded", "all_done"),
        ("pending", "all_done"),
        ("running", "all_done"),
        ("failed", "all_success"),
        ("skipped", "one_success"),
    ],
)
async def test_optional_input_cannot_replace_output_authority_or_trigger_rule(
    database: Database, state: str, rule: str
) -> None:
    case = await optional_admission_case(database, state, rule=rule)
    prepared = case.prepare()
    before = await case.promotion.snapshot(database)

    async with database.transaction() as writer:
        result = await admit_attempt(
            await expiry_context(writer, case.promotion),
            prepared,
            LIMITS,
            PreparedGraphCaps(case.promotion.scope, None, None),
            global_limit=1,
            runtime_limit=1,
            interval=INTERVAL,
        )

    assert result is AttemptAdmissionState.REFUSED
    assert await case.promotion.snapshot(database) == before
    async with database.read_transaction() as reader:
        assert await reader.scalar(select(attempt.c.id)) is None
        assert await reader.scalar(select(wait.c.id)) is None


@pytest.mark.asyncio
async def test_required_input_cannot_prepare_absent_binding(database: Database) -> None:
    case = await optional_admission_case(database, optional=False)
    before = await case.promotion.snapshot(database)

    with pytest.raises(ClaimWireRejected):
        case.prepare()

    assert await case.promotion.snapshot(database) == before


@pytest.mark.asyncio
async def test_optional_input_absence_preserves_historical_recovery_occupancy(
    database: Database,
) -> None:
    case = await optional_admission_case(database)
    prepared = case.prepare()
    async with database.transaction() as writer:
        seed = Seeder(writer)
        historical_node = await seed.node(case.promotion.run_id, "history", "cancelled")
        blocker = await seed.attempt(historical_node, "finishing")
        await writer.execute(
            update(attempt)
            .where(attempt.c.id == blocker)
            .values(recovery_required=True)
        )
    async with database.read_transaction() as reader:
        with await reader.execute(
            select(attempt).where(attempt.c.id == blocker)
        ) as rows:
            before = dict(rows.mappings().one())

    async with database.transaction() as writer:
        result = await admit_attempt(
            await expiry_context(writer, case.promotion),
            prepared,
            LIMITS,
            PreparedGraphCaps(case.promotion.scope, None, None),
            global_limit=1,
            runtime_limit=1,
            interval=INTERVAL,
        )
    async with database.read_transaction() as reader:
        with await reader.execute(select(attempt)) as rows:
            after = [dict(row) for row in rows.mappings()]
        with await reader.execute(select(wait)) as rows:
            waiting = dict(rows.mappings().one())
        target = await reader.scalar(
            select(node_instance.c.current_attempt_id).where(
                node_instance.c.id == case.binding.node_id
            )
        )

    assert result is AttemptAdmissionState.WAITING
    assert after == [before]
    assert target is None
    assert waiting["kind"] == "admission"
    reason = waiting["reason"]
    assert isinstance(reason, dict)
    assert reason["kind"] == "execution_recovery"
    assert reason["blockers"] == {
        "count": 1,
        "ids": [blocker],
        "truncated": False,
    }
    assert waiting["attempt_id"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["succeeded", "failed", "running"])
async def test_control_only_dependencies_need_no_artifact_binding(
    database: Database, state: str
) -> None:
    case = await optional_admission_case(database, state, inputless_command=True)
    prepared = case.prepare()

    async with database.transaction() as writer:
        result = await admit_optional_candidate(writer, case, prepared)
    async with database.read_transaction() as reader:
        acquired = await acquire_claim_sources(reader, case.binding, LIMITS)

    if state == "running":
        assert result is AttemptAdmissionState.REFUSED
        assert acquired is None
    else:
        assert result is AttemptAdmissionState.QUEUED
        assert acquired is not None
        assert decode_claim_sources(acquired).values["input_bindings"] == (
            '{"version":1,"inputs":[]}'
        )
