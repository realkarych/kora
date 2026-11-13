import asyncio
from dataclasses import replace
from datetime import timedelta
from typing import cast

import pytest
from sqlalchemy import update

from kora.cel_eval.thread import Evaluator
from kora.db.claim_source_values import ClaimSourcesRejected
from kora.db.connection import Database
from kora.db.grant_history_v1 import ACK_FIELDS, TIP_FIELDS
from kora.db.tables import (
    attempt,
    attempt_dispatcher,
)
from kora.engine.attempt_claims import ClaimOutcome
from kora.engine.claim_context import ClaimAccessRejected
from kora.engine.claim_selection import prepare_selected_claim
from kora.ir.graph_json import ObjectReader
from kora.ir.json import JsonObject
from kora.protocols.worker_claim_wire import (
    MAX_CLAIM_BYTES,
    MAX_IMMUTABLE_BYTES,
    ClaimPartition,
    prepare_claim_part,
)
from tests.api.deployment_cases import evaluator
from tests.db.closing import ConnectionLedger
from tests.db.seed import START
from tests.engine.claim_support import (
    another_run,
    claim_case,
    state,
)
from tests.engine.lock_support import DeliberateRollback

__all__ = ["evaluator"]


@pytest.mark.asyncio
async def test_first_grant_and_replay_are_complete_and_replay_is_read_only(
    database: Database, evaluator: Evaluator
) -> None:
    case = await claim_case(database, evaluator)

    first = await case.claim(database)
    before_replay = await state(database)
    case.clock.advance(1)
    replay = await case.claim(database)
    after_replay = await state(database)

    assert first.outcome is ClaimOutcome.GRANTED
    assert replay.outcome is ClaimOutcome.REPLAYED
    assert first.delivery is not None
    assert replay.delivery is not None
    assert first.delivery.encode() == replay.delivery.encode()
    assert before_replay == after_replay
    system = first.delivery.system
    assert system.permitted_actions == ("seal_request",)
    assert system.lease_epoch == 1
    assert system.lease_until == START + timedelta(seconds=120)
    assert system.acknowledged_segment_offset == 0
    assert system.current_grant.process == case.registration.process
    assert system.grant_history.count == 1
    document = ObjectReader(first.delivery.encode().decode()).object()
    claim = cast(JsonObject, document["claim"])
    assert claim["price_table_id"] == case.binding.price_table_id
    assert claim["resolved_config"] is not None
    assert claim["execution_context"] is not None
    assert claim["execution_policy"] is not None
    claimed = [row for row in after_replay["event"] if row["kind"] == "attempt.claimed"]
    assert len(claimed) == 1
    assert claimed[0]["actor"] == {
        "kind": "service",
        "owner": case.principal.owner,
        "token_id": case.principal.token_id,
    }
    assert after_replay["attempt"][0]["started_at"] == START


@pytest.mark.asyncio
async def test_claim_rollback_preserves_attempt_sources_receipts_and_events(
    database: Database, evaluator: Evaluator
) -> None:
    case = await claim_case(database, evaluator)
    prepared = await case.prepare(database)
    before = await state(database)

    async def abort() -> None:
        async with database.transaction() as writer:
            tentative = await case.apply(writer, prepared)
            assert tentative.outcome is ClaimOutcome.GRANTED
            raise DeliberateRollback()

    with pytest.raises(DeliberateRollback):
        await abort()
    after = await state(database)
    retried = await case.claim(database)

    assert before == after
    assert retried.outcome is ClaimOutcome.GRANTED
    assert retried.delivery is not None
    assert retried.delivery.system.lease_epoch == 1


@pytest.mark.asyncio
async def test_unknown_segment_offset_rolls_back_first_claim(
    database: Database, evaluator: Evaluator
) -> None:
    case = await claim_case(database, evaluator)
    async with database.transaction() as writer:
        await writer.execute(update(attempt).values(acknowledged_segment_offset=None))
    before = await state(database)

    with pytest.raises(ClaimSourcesRejected):
        await case.claim(database)
    after = await state(database)

    assert before == after


@pytest.mark.asyncio
async def test_cancellation_before_commit_rolls_back_tentative_grant(
    database: Database, evaluator: Evaluator
) -> None:
    case = await claim_case(database, evaluator)
    prepared = await case.prepare(database)
    before = await state(database)
    tentative_ready, never_released = asyncio.Event(), asyncio.Event()
    ledger = ConnectionLedger(database)

    async def cancel_before_commit() -> None:
        async with database.transaction() as writer:
            result = await case.apply(writer, prepared)
            assert result.outcome is ClaimOutcome.GRANTED
            tentative_ready.set()
            await never_released.wait()

    async with asyncio.timeout(10):
        task = asyncio.create_task(cancel_before_commit())
        await tentative_ready.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    after = await state(database)
    retry = await case.claim(database)

    assert before == after
    assert retry.outcome is ClaimOutcome.GRANTED
    assert database.units_in_flight() == ledger.outstanding == 0


@pytest.mark.asyncio
async def test_receipt_redirect_and_stale_refusal_precede_source_preparation(
    database: Database, evaluator: Evaluator
) -> None:
    first = await claim_case(database, evaluator)
    second = await another_run(database, first)
    await first.claim(database)
    replay_from_other_run = replace(second, query=first.query)
    before = await state(database)

    async with database.transaction() as writer:
        redirect = await replay_from_other_run.apply(writer, None)
    first.clock.advance(121)

    async def stale_without_sources() -> None:
        async with database.transaction() as writer:
            await first.apply(writer, None)

    with pytest.raises(ClaimAccessRejected) as caught:
        await stale_without_sources()
    after = await state(database)

    assert redirect.outcome is ClaimOutcome.REDIRECT
    assert redirect.redirect == first.binding.attempt_id
    assert caught.value.reason == "stale"
    assert before == after


@pytest.mark.parametrize("field", ["handle", "execution_request", "deadline_binding"])
@pytest.mark.asyncio
async def test_invalid_selected_recovery_data_has_content_free_error(
    database: Database, evaluator: Evaluator, field: str
) -> None:
    case = await claim_case(database, evaluator)
    prepared = await case.prepare(database)
    marker = "private-recovery-field-and-value"
    sources = replace(
        prepared.snapshot.sources,
        values={**prepared.snapshot.sources.values, field: {marker: marker}},
    )
    snapshot = replace(prepared.snapshot, sources=sources)

    with pytest.raises(ClaimSourcesRejected) as caught:
        prepare_selected_claim(snapshot, None, None)

    assert marker not in str(caught.value)
    assert marker not in repr(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.asyncio
async def test_history_maintenance_commits_without_issuing_a_grant(
    database: Database, evaluator: Evaluator
) -> None:
    case = await claim_case(database, evaluator)
    await case.claim(database)
    async with database.transaction() as writer:
        await writer.execute(
            update(attempt_dispatcher).values(
                prefix_count=None, prefix_digest=None, claim_id=None, claim_digest=None
            )
        )
        await writer.execute(
            update(attempt).values(**dict.fromkeys((*TIP_FIELDS, *ACK_FIELDS)))
        )
    case.clock.advance(121)
    next_case = replace(
        case, query=case.query.model_copy(update={"claim_id": "history"})
    )
    before = await state(database)

    maintained = await next_case.claim(database)
    after = await state(database)
    granted = await next_case.claim(database)

    assert maintained.outcome is ClaimOutcome.MAINTAINED
    assert maintained.delivery is None
    assert after["attempt"][0]["lease_epoch"] == before["attempt"][0]["lease_epoch"]
    assert (
        after["attempt"][0]["recovery_version"]
        == before["attempt"][0]["recovery_version"]
    )
    assert after["event"] == before["event"]
    assert len(after["attempt_dispatcher"]) == 1
    assert after["attempt_dispatcher"][0]["prefix_count"] == 1
    assert after["attempt_dispatcher"][0]["claim_id"] is None
    assert granted.outcome is ClaimOutcome.GRANTED
    assert granted.delivery is not None
    assert granted.delivery.system.grant_history.count == 2


@pytest.mark.asyncio
async def test_selected_claim_accepts_maximum_admitted_immutable_partition(
    database: Database, evaluator: Evaluator
) -> None:
    case = await claim_case(database, evaluator)
    prepared = await case.prepare(database)
    immutable = ObjectReader(
        prepared.snapshot.material.immutable.payload.decode()
    ).object()
    config = cast(JsonObject, immutable["resolved_config"])
    config["prompt"] = ""
    base = prepare_claim_part(ClaimPartition.IMMUTABLE, immutable)
    config["prompt"] = "a" * (MAX_IMMUTABLE_BYTES - len(base.payload))
    largest = prepare_claim_part(ClaimPartition.IMMUTABLE, immutable)
    selected = replace(
        prepared,
        snapshot=replace(
            prepared.snapshot,
            material=replace(prepared.snapshot.material, immutable=largest),
        ),
    )
    ledger = ConnectionLedger(database)

    async with database.transaction() as writer:
        decision = await case.apply(writer, selected)
    assert ledger.outstanding == database.units_in_flight() == 0
    assert decision.delivery is not None
    response = decision.delivery.encode()

    assert decision.outcome is ClaimOutcome.GRANTED
    assert len(largest.payload) == MAX_IMMUTABLE_BYTES
    assert len(response) <= MAX_CLAIM_BYTES
    assert b'"prompt":"' + b"a" * 256 in response
