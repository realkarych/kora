from dataclasses import replace

import pytest
from sqlalchemy import select, update

from kora.db.connection import Database
from kora.db.cost_reads import CostReadRefused
from kora.db.identifiers import IdentifierGenerator
from kora.db.tables import attempt, usage_record
from kora.engine.attempt_usage import UsageAccess, UsageAccessRejected
from kora.engine.usage_acceptance import UsageRefusal, UsageRejected
from tests.db.seed import START, fixed_randomness
from tests.engine.test_usage_acceptance import usage
from tests.engine.worker_usage_support import submit_usage, usage_case, usage_snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["codex", "fake"])
async def test_usage_mapping_is_selected_from_checked_node(
    database: Database, harness: str
) -> None:
    case = await usage_case(database, harness=harness)

    accepted = await submit_usage(database, case, (usage(),))
    before = await usage_snapshot(database, case)
    with pytest.raises(UsageRejected) as refused:
        await submit_usage(
            database,
            case,
            (usage(observation_sequence=1, basis="harness_list", reported_usd="1"),),
        )
    after = await usage_snapshot(database, case)
    async with database.read_transaction() as connection:
        usage_id = await connection.scalar(select(usage_record.c.id))

    assert len(accepted.changed_rows) == 1
    assert refused.value.reason is UsageRefusal.MAPPING
    assert after == before
    assert (
        usage_id == IdentifierGenerator(lambda: START, fixed_randomness(500_000)).new()
    )


@pytest.mark.asyncio
async def test_command_usage_has_coverage_without_model_rows(
    database: Database,
) -> None:
    case = await usage_case(database, node_path="producer")

    accepted = await submit_usage(database, case, ())
    before = await usage_snapshot(database, case)
    with pytest.raises(UsageRejected) as refused:
        await submit_usage(database, case, (usage(),))
    after = await usage_snapshot(database, case)

    assert accepted.coverage_changed
    assert refused.value.reason is UsageRefusal.MAPPING
    assert before == after


@pytest.mark.asyncio
async def test_unsupported_usage_mapping_is_not_inferred(database: Database) -> None:
    with pytest.raises(UsageRejected) as refused:
        await usage_case(database, harness="unsupported")

    assert refused.value.reason is UsageRefusal.MAPPING


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "principal_change", [{"runtime": "other"}, {"kind": "user", "runtime": None}]
)
async def test_usage_requires_service_runtime_ownership(
    database: Database, principal_change: dict[str, object]
) -> None:
    case = await usage_case(database)
    changed = replace(
        case, principal=case.principal.model_copy(update=principal_change)
    )
    before = await usage_snapshot(database, case)

    with pytest.raises(UsageAccessRejected) as refused:
        await submit_usage(database, changed, (usage(),))
    after = await usage_snapshot(database, case)

    assert refused.value.reason is UsageAccess.FORBIDDEN
    assert before == after


@pytest.mark.asyncio
async def test_usage_corrupt_saved_coverage_is_a_stored_state_refusal(
    database: Database,
) -> None:
    case = await usage_case(database)
    async with database.transaction() as connection:
        await connection.execute(
            update(attempt).values(usage_coverage={"private-marker": "invalid"})
        )
    before = await usage_snapshot(database, case)

    with pytest.raises(CostReadRefused):
        await submit_usage(database, case, (usage(),))
    after = await usage_snapshot(database, case)

    assert before == after
