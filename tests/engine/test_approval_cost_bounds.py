from decimal import Decimal, localcontext

import pytest
from sqlalchemy import bindparam, func, insert, select, update

from kora.db.connection import Database
from kora.db.tables import attempt, usage_record
from kora.engine.approval_cost import ApprovalCostUnavailable
from kora.engine.approval_cost_totals import ApprovalCostOverflow, add_count, add_money
from kora.ir.cost import MAX_MONEY, MAX_SUM, RunCost
from tests.db.seed import Seeder
from tests.engine.approval_cost_support import (
    cost_attempt,
    cost_case,
    cost_usage,
    coverage,
    usage_values,
)


@pytest.mark.parametrize("overflow", [False, True])
def test_approval_cost_integer_addition_preserves_the_exact_bound(
    overflow: bool,
) -> None:
    left = MAX_SUM - 1
    right = 2 if overflow else 1

    if overflow:
        with pytest.raises(ApprovalCostOverflow):
            add_count(left, right)
        return
    result = add_count(left, right)

    assert result == MAX_SUM


@pytest.mark.parametrize("overflow", [False, True])
def test_approval_cost_money_addition_ignores_ambient_precision(overflow: bool) -> None:
    left = Decimal("99999999999999.9999999998")
    right = Decimal("0.0000000002" if overflow else "0.0000000001")

    with localcontext() as context:
        context.prec = 1
        if overflow:
            with pytest.raises(ApprovalCostOverflow):
                add_money(left, right)
            return
        result = add_money(left, right)

    assert result == MAX_MONEY


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("count", "wide", "overflow"),
    [(1024, False, False), (1025, False, True), (1024, True, True)],
)
async def test_approval_cost_enforces_model_and_serialized_bounds(
    database: Database, count: int, wide: bool, overflow: bool
) -> None:
    case = await cost_case(database)
    async with database.transaction() as writer:
        seed = Seeder(writer)
        for start in range(0, count, 64):
            _, identity = await cost_attempt(
                writer, case, f"node{start}", saved_coverage=coverage()
            )
            values = [
                usage_values(
                    seed,
                    identity,
                    ("\U00010000" * 252 if wide else "") + f"{number:04d}",
                    amount="0",
                )
                for number in range(start, min(start + 64, count))
            ]
            await writer.execute(insert(usage_record), values)

    if overflow:
        with pytest.raises(ApprovalCostOverflow):
            await case.calculate(database)
        result = None
    else:
        result = await case.calculate(database)

    if result is not None:
        assert len(result.models) == 1024
        assert result.usd_estimate == "0"
        assert len(result.model_dump_json().encode()) <= RunCost.byte_limit
    async with database.read_transaction() as reader:
        assert (
            await reader.scalar(select(func.count()).select_from(usage_record)) == count
        )
    assert database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["coverage", "model", "subscription"])
async def test_approval_cost_refuses_invalid_saved_accounting(
    database: Database, field: str
) -> None:
    case = await cost_case(database)
    async with database.transaction() as writer:
        _, identity = await cost_attempt(
            writer, case, "agent", saved_coverage=coverage()
        )
        usage_id = await cost_usage(writer, identity)
        if field == "coverage":
            await writer.execute(
                update(attempt)
                .where(attempt.c.id == identity)
                .values(usage_coverage={"private": "x" * 60_000})
            )
        elif field == "model":
            await writer.execute(
                update(usage_record)
                .where(usage_record.c.id == usage_id)
                .values(model="private\nmodel")
            )
        else:
            await writer.execute(
                update(usage_record)
                .where(usage_record.c.id == usage_id)
                .values(billing_class="subscription")
            )

    with pytest.raises(ApprovalCostUnavailable) as failure:
        await case.calculate(database)

    assert str(failure.value) == "the approval cost is unavailable"
    assert failure.value.__cause__ is failure.value.__context__ is None
    assert database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
@pytest.mark.parametrize("extra_byte", [0, 1])
async def test_approval_cost_exact_serialized_boundary(
    database: Database, extra_byte: int
) -> None:
    case = await cost_case(database)
    identities: list[str] = []
    async with database.transaction() as writer:
        seed = Seeder(writer)
        for start in range(0, 1024, 64):
            _, identity = await cost_attempt(
                writer, case, f"node{start}", saved_coverage=coverage()
            )
            values = [
                usage_values(seed, identity, f"{number:04d}", amount="0")
                for number in range(start, start + 64)
            ]
            await writer.execute(insert(usage_record), values)
            identities.extend(str(value["id"]) for value in values)
    baseline = await case.calculate(database)
    missing_bytes = RunCost.byte_limit - len(baseline.model_dump_json().encode())
    changes: list[dict[str, str]] = []
    for number, identity in enumerate(identities):
        padding_bytes = min(missing_bytes, 996)
        wide_count, ascii_count = divmod(padding_bytes, 4)
        model = f"{number:04d}" + "\U00010000" * wide_count + "a" * ascii_count
        changes.append({"identity": identity, "model_name": model})
        missing_bytes -= padding_bytes
    assert missing_bytes == 0
    changes[-1]["model_name"] += "b" * extra_byte
    async with database.transaction() as writer:
        await writer.execute(
            update(usage_record)
            .where(usage_record.c.id == bindparam("identity"))
            .values(model=bindparam("model_name")),
            changes,
        )
    async with database.read_transaction() as reader:
        before = list((await reader.execute(select(usage_record))).all())

    if extra_byte:
        with pytest.raises(ApprovalCostOverflow):
            await case.calculate(database)
        result = None
    else:
        result = await case.calculate(database)

    if result is not None:
        assert len(result.model_dump_json().encode()) == RunCost.byte_limit
        assert result.usd_estimate == "0"
        assert len(result.models) == 1024
    async with database.read_transaction() as reader:
        after = list((await reader.execute(select(usage_record))).all())
    assert sorted(before) == sorted(after)
    assert database.writer_is_free()
    assert database.units_in_flight() == 0
