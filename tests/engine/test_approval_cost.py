from datetime import timedelta
from decimal import Decimal, localcontext

import pytest
from sqlalchemy import select, update

from kora.db.connection import Database
from kora.db.tables import attempt, node_instance, usage_record
from kora.engine.approval_cost import ApprovalCostUnavailable, calculate_approval_cost
from kora.engine.approval_cost_totals import ApprovalCostOverflow
from kora.engine.locks import TransitionLocks
from tests.db.seed import START, Seeder
from tests.engine.approval_cost_support import (
    cost_attempt,
    cost_case,
    cost_usage,
    coverage,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["agent", "command"])
async def test_approval_cost_empty_history_has_known_zero(
    database: Database, kind: str
) -> None:
    case = await cost_case(database)
    async with database.transaction() as writer:
        await cost_attempt(writer, case, "never", started=False, kind=kind)

    result = await case.calculate(database)

    assert result.completeness == "complete"
    assert result.usd_estimate == result.known_usd_estimate == "0"
    assert result.pricing_status == "estimated"
    assert result.models == ()
    assert result.observed_at is None
    assert result.missing_report_attempts == 0
    assert database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
async def test_approval_cost_includes_all_attempt_history(database: Database) -> None:
    case = await cost_case(database)
    foreign = await cost_case(database, "foreign")
    async with database.transaction() as writer:
        node, first = await cost_attempt(
            writer, case, "agent", status="failed", saved_coverage=coverage()
        )
        _, second = await cost_attempt(
            writer,
            case,
            "agent",
            status="cancelled",
            saved_coverage=coverage(),
            number=2,
            node_id=node,
        )
        _, third = await cost_attempt(
            writer,
            case,
            "agent",
            saved_coverage=coverage(),
            number=3,
            node_id=node,
        )
        await writer.execute(
            update(node_instance)
            .where(node_instance.c.id == node)
            .values(current_attempt_id=third)
        )
        for identity in (first, second, third):
            await cost_usage(writer, identity, "zebra", amount="0.1")
            await cost_usage(writer, identity, "alpha", amount="0.2")
        _, other = await cost_attempt(
            writer, foreign, "other", saved_coverage=coverage()
        )
        await cost_usage(writer, other, amount="100")

    with localcontext() as context:
        context.prec = 2
        result = await case.calculate(database)

    assert result.usd_estimate == result.known_usd_estimate == "0.9"
    assert result.completeness == "complete"
    assert result.observed_at == START
    assert [model.model for model in result.models] == ["alpha", "zebra"]
    assert [model.usd_estimate for model in result.models] == ["0.6", "0.3"]
    assert [model.input_tokens for model in result.models] == [9, 9]
    assert [model.reasoning_tokens for model in result.models] == [12, 12]
    assert result.missing_report_attempts == 0


@pytest.mark.asyncio
async def test_approval_cost_preserves_missing_and_gap_coverage(
    database: Database,
) -> None:
    case = await cost_case(database)
    older = START - timedelta(seconds=5)
    async with database.transaction() as writer:
        _, reported = await cost_attempt(
            writer,
            case,
            "reported",
            saved_coverage=coverage(
                at=older,
                completeness="partial",
                attribution_gap=True,
                observation_gap=True,
            ),
        )
        await cost_usage(writer, reported, "first", at=older)
        await cost_usage(writer, reported, "second", at=older)
        await cost_attempt(writer, case, "missing")

    result = await case.calculate(database)

    assert result.completeness == "unavailable"
    assert result.usd_estimate is None
    assert result.known_usd_estimate == "0.2"
    assert result.pricing_status == "partially_priced"
    assert result.missing_report_attempts == 1
    assert result.attribution_gap_attempts == result.observation_gap_attempts == 1
    assert result.observed_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["running", "unknown", "recovery"])
async def test_approval_cost_unsettled_execution_caps_model_completeness(
    database: Database, state: str
) -> None:
    case = await cost_case(database)
    async with database.transaction() as writer:
        _, identity = await cost_attempt(
            writer, case, "agent", saved_coverage=coverage()
        )
        await cost_usage(writer, identity)
        values: dict[str, object] = {
            "status": "running" if state == "running" else "succeeded",
            "execution_state": "unknown" if state == "unknown" else "stopped",
            "recovery_required": state == "recovery",
        }
        await writer.execute(
            update(attempt).where(attempt.c.id == identity).values(**values)
        )

    result = await case.calculate(database)

    assert result.completeness == "partial"
    assert result.models[0].completeness == "partial"
    assert result.usd_estimate == "0.1"


@pytest.mark.asyncio
@pytest.mark.parametrize("mixed", [False, True])
async def test_approval_cost_preserves_subscription_and_subtotals(
    database: Database, mixed: bool
) -> None:
    case = await cost_case(database)
    async with database.transaction() as writer:
        _, identity = await cost_attempt(
            writer, case, "agent", saved_coverage=coverage()
        )
        await cost_usage(
            writer, identity, "subscription", amount=None, billing="subscription"
        )
        if mixed:
            await cost_usage(writer, identity, "priced", amount="1.25")

    result = await case.calculate(database)

    assert result.usd_estimate is None
    assert result.known_usd_estimate == ("1.25" if mixed else None)
    assert result.pricing_status == ("partially_priced" if mixed else "not_billable")
    assert result.models[-1].usd_estimate is None
    assert result.models[-1].billing_classes == ("subscription",)


@pytest.mark.asyncio
async def test_approval_cost_uses_oldest_covered_observation(
    database: Database,
) -> None:
    case = await cost_case(database)
    older = START - timedelta(seconds=10)
    async with database.transaction() as writer:
        for name, at in (("old", older), ("new", START)):
            _, identity = await cost_attempt(
                writer, case, name, saved_coverage=coverage(at=at)
            )
            await cost_usage(writer, identity, name, at=at)

    result = await case.calculate(database)

    assert result.observed_at == older
    assert result.as_of == START


@pytest.mark.asyncio
async def test_approval_cost_refuses_contradictory_grant_history(
    database: Database,
) -> None:
    case = await cost_case(database)
    async with database.transaction() as writer:
        _, identity = await cost_attempt(writer, case, "agent", started=False)
        await writer.execute(
            update(attempt).where(attempt.c.id == identity).values(lease_epoch=1)
        )

    with pytest.raises(
        ApprovalCostUnavailable, match="^the approval cost is unavailable$"
    ):
        await case.calculate(database)

    assert database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
async def test_approval_cost_preserves_predecessor_distinction(
    database: Database,
) -> None:
    case = await cost_case(database)
    async with database.transaction() as writer:
        _, identity = await cost_attempt(
            writer, case, "agent", saved_coverage=coverage()
        )
        await Seeder(writer).usage(identity, "legacy", Decimal("1"))

    with pytest.raises(ApprovalCostUnavailable) as failure:
        await case.calculate(database)

    assert failure.value.__cause__ is None
    assert failure.value.__context__ is None
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_approval_cost_money_overflow_preserves_ledger(
    database: Database,
) -> None:
    case = await cost_case(database)
    async with database.transaction() as writer:
        _, identity = await cost_attempt(
            writer, case, "agent", saved_coverage=coverage()
        )
        await cost_usage(writer, identity, "first", amount="99999999999999.9999999999")
        await cost_usage(writer, identity, "second", amount="0.0000000001")
    async with database.read_transaction() as reader:
        before = (await reader.execute(select(usage_record))).all()

    with pytest.raises(ApprovalCostOverflow):
        await case.calculate(database)

    async with database.read_transaction() as reader:
        after = (await reader.execute(select(usage_record))).all()
    assert after == before
    assert database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong", ["namespace", "run", "missing_lock"])
async def test_approval_cost_requires_original_run_lock(
    database: Database, wrong: str
) -> None:
    case = await cost_case(database)
    other = await cost_case(database, "other")

    async with database.transaction() as writer:
        locks = TransitionLocks(writer)
        if wrong != "missing_lock":
            await locks.acquire_run(case.run_id)
        with pytest.raises(ApprovalCostUnavailable):
            await calculate_approval_cost(
                writer,
                locks,
                other.namespace_id if wrong == "namespace" else case.namespace_id,
                other.run_id if wrong == "run" else case.run_id,
                START,
            )

    assert database.units_in_flight() == 0
    assert database.writer_is_free()
