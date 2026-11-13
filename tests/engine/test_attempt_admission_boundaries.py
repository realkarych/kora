from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import literal, select

from kora.db.claim_sources import acquire_claim_sources, verify_claim_source_binding
from kora.db.connection import Database, Dialect
from kora.db.run_input_parent import sqlite_storage_width
from kora.db.tables import attempt, node_instance
from kora.engine.attempt_admission import (
    AttemptAdmissionFailed,
    AttemptAdmissionState,
    admit_attempt,
)
from kora.engine.attempt_capacity import PreparedGraphCaps
from kora.engine.locks import TransitionLocks
from kora.engine.namespace_policy import (
    NamespacePolicyConfiguration,
    namespace_policy_scopes,
    write_namespace_policy,
)
from kora.engine.price_initialization import (
    PriceConfiguration,
    import_price_revision,
    price_scopes,
)
from kora.ir.json import JsonObject
from kora.protocols.worker_claim_wire import ClaimWireRejected
from tests.db.closing import ConnectionLedger
from tests.engine.admission_wait_support import INTERVAL, LIMITS
from tests.engine.attempt_admission_support import (
    append_admission_case,
    attempt_admission_case,
)
from tests.engine.expiry_support import expiry_context
from tests.engine.lock_support import lock_inventory


@pytest.mark.asyncio
@pytest.mark.parametrize("allowance", ["initial", "one_below_final", "exact_final"])
async def test_attempt_admission_keeps_final_node_within_claim_read_allowance(
    database: Database, allowance: str
) -> None:
    case = await attempt_admission_case(database)
    prepared = case.prepare()
    caps = PreparedGraphCaps(case.promotion.scope, None, None)
    limits = LIMITS
    if database.dialect is Dialect.SQLITE:
        width = sum(
            (sqlite_storage_width(field) for field in node_instance.c),
            literal(9 * (len(node_instance.c) + 1)),
        )
        async with database.read_transaction() as reader:
            original = await reader.scalar(
                select(width).where(node_instance.c.id == case.binding.node_id)
            )
        assert isinstance(original, int)
        growth = len(case.binding.attempt_id) + len("queued") - len("pending")
        added = {"initial": 0, "one_below_final": growth - 1, "exact_final": growth}
        limits = replace(limits, node_row=original + added[allowance])
    async with database.read_transaction() as reader:
        assert await verify_claim_source_binding(
            reader, case.binding, limits, include_attempt=False
        )
    before = await case.promotion.snapshot(database)
    ledger = ConnectionLedger(database)

    async def apply() -> AttemptAdmissionState:
        async with database.transaction() as writer:
            return await admit_attempt(
                await expiry_context(writer, case.promotion),
                prepared,
                limits,
                caps,
                global_limit=1,
                runtime_limit=1,
                interval=INTERVAL,
            )

    refused = database.dialect is Dialect.SQLITE and allowance != "exact_final"
    if refused:
        with pytest.raises(AttemptAdmissionFailed) as caught:
            await apply()
        assert caught.value.__cause__ is caught.value.__context__ is None
    else:
        assert await apply() is AttemptAdmissionState.QUEUED
    async with database.read_transaction() as reader:
        sources = await acquire_claim_sources(reader, case.binding, limits)
        saved_id = await reader.scalar(select(attempt.c.id))
    after = await case.promotion.snapshot(database)

    if refused:
        assert sources is saved_id is None
        assert after == before
    else:
        assert sources is not None
        assert saved_id == case.binding.attempt_id
    assert await lock_inventory(database) == []
    assert ledger.outstanding == database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.asyncio
async def test_attempt_admission_uses_price_effective_at_final_creation_time(
    database: Database,
) -> None:
    case = await attempt_admission_case(database)
    effective_at = case.promotion.clock.now() + timedelta(seconds=1)
    price = PriceConfiguration(case.policy.namespace_id, effective_at, {}).prepare(
        case.promotion.identifiers.new()
    )
    async with database.transaction() as writer:
        locks = TransitionLocks(writer)
        await locks.acquire_scopes(price_scopes(case.policy.namespace_id))
        await import_price_revision(writer, locks, price, case.promotion.clock.now())
    prepared = case.prepare()
    caps = PreparedGraphCaps(case.promotion.scope, None, None)
    case.promotion.clock.advance(1)
    before = await case.promotion.snapshot(database)

    async with database.transaction() as writer:
        stale = await admit_attempt(
            await expiry_context(writer, case.promotion),
            prepared,
            LIMITS,
            caps,
            global_limit=1,
            runtime_limit=1,
            interval=INTERVAL,
        )
    refused = await case.promotion.snapshot(database)
    async with database.read_transaction() as reader:
        absent = await reader.scalar(select(attempt.c.id))
    refreshed = replace(
        case, price=price, binding=replace(case.binding, price_table_id=price.id)
    ).prepare()
    async with database.transaction() as writer:
        admitted = await admit_attempt(
            await expiry_context(writer, case.promotion),
            refreshed,
            LIMITS,
            caps,
            global_limit=1,
            runtime_limit=1,
            interval=INTERVAL,
        )
    async with database.read_transaction() as reader:
        with await reader.execute(select(attempt)) as rows:
            saved = rows.mappings().one()

    assert stale is AttemptAdmissionState.REFUSED
    assert refused == before
    assert absent is None
    assert admitted is AttemptAdmissionState.QUEUED
    assert saved["price_table_id"] == price.id
    assert saved["created_at"] == effective_at
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "required",
    [
        {"require_protected_execution": True},
        {"require_protected_resources": True},
        {"daily_budget_usd": "0"},
        {"tool_policy": {"allow": ["Read"]}},
        {"tool_policy": {"deny": ["Bash(*)"]}},
    ],
)
async def test_attempt_preparation_refuses_unavailable_current_namespace_policy(
    database: Database, required: JsonObject
) -> None:
    case = await attempt_admission_case(database)
    policy = NamespacePolicyConfiguration.prepare(
        case.policy.namespace_id, 2, {**case.policy.value(), **required}
    )
    async with database.transaction() as writer:
        locks = TransitionLocks(writer)
        await locks.acquire_scopes(namespace_policy_scopes(policy.namespace_id))
        await write_namespace_policy(writer, locks, policy, case.promotion.clock.now())
    current = replace(case, policy=policy)
    before = await case.promotion.snapshot(database)
    ledger = ConnectionLedger(database)

    with pytest.raises(ClaimWireRejected) as caught:
        current.prepare()

    assert caught.value.__cause__ is caught.value.__context__ is None
    assert ledger.checkouts == 0
    assert await case.promotion.snapshot(database) == before
    async with database.read_transaction() as reader:
        assert await reader.scalar(select(attempt.c.id)) is None
    assert ledger.outstanding == database.units_in_flight() == 0


@pytest.mark.asyncio
async def test_attempt_admission_refuses_node_from_another_run(
    database: Database,
) -> None:
    first = await attempt_admission_case(database)
    second = await append_admission_case(database, first)
    mixed = replace(
        first,
        params=second.params,
        binding=replace(first.binding, run=second.binding.run),
    ).prepare()
    caps = PreparedGraphCaps(second.promotion.scope, None, None)
    before_first = await first.promotion.snapshot(database)
    before_second = await second.promotion.snapshot(database)

    async with database.transaction() as writer:
        result = await admit_attempt(
            await expiry_context(writer, second.promotion),
            mixed,
            LIMITS,
            caps,
            global_limit=1,
            runtime_limit=1,
            interval=INTERVAL,
        )

    assert result is AttemptAdmissionState.REFUSED
    assert await first.promotion.snapshot(database) == before_first
    assert await second.promotion.snapshot(database) == before_second
    async with database.read_transaction() as reader:
        assert await reader.scalar(select(attempt.c.id)) is None
    assert database.units_in_flight() == 0
