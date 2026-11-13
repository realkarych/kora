from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import text, update

from kora.cel_eval.thread import Evaluator
from kora.db.claim_source_values import ClaimSourcesRejected
from kora.db.connection import Database, Dialect
from kora.db.tables import (
    attempt,
    attempt_dispatcher,
    namespace,
)
from kora.engine.attempt_claims import ClaimOutcome, claim_attempt
from kora.engine.claim_context import ClaimAccessRejected
from kora.engine.worker_registration import register_worker
from kora.engine.writer_mode import WRITABLE_POSTGRES_MODE
from tests.api.deployment_cases import evaluator
from tests.db.seed import START
from tests.db.test_graph_read_admission import executed_queries
from tests.engine.claim_support import (
    HISTORY_LIMITS,
    SOURCE_LIMITS,
    UNCERTAINTY,
    WORKER_BYTES,
    another_run,
    claim_case,
    state,
)

__all__ = ["evaluator"]


@pytest.mark.asyncio
async def test_retained_incarnation_replays_its_original_process_after_registration(
    database: Database, evaluator: Evaluator
) -> None:
    case = await claim_case(database, evaluator)
    first = await case.claim(database)
    next_registration = replace(
        case.registration,
        incarnation_id="new-incarnation",
        process=case.registration.process.model_copy(update={"pid": 124}),
    )
    async with database.transaction() as writer:
        await register_worker(
            writer,
            next_registration,
            case.principal,
            portable=False,
            now=START + timedelta(seconds=1),
            event_id=case.identifiers.new(),
        )
    before = await state(database)

    replay = await case.claim(database)
    after = await state(database)

    assert replay.outcome is ClaimOutcome.REPLAYED
    assert replay.delivery is not None
    assert first.delivery is not None
    assert replay.delivery.encode() == first.delivery.encode()
    assert before == after


@pytest.mark.parametrize("damage", ["digest", "epoch", "lease"])
@pytest.mark.asyncio
async def test_receipt_conflict_and_stale_replay_do_not_mutate(
    database: Database, evaluator: Evaluator, damage: str
) -> None:
    case = await claim_case(database, evaluator)
    await case.claim(database)
    async with database.transaction() as writer:
        if damage == "digest":
            await writer.execute(
                update(attempt_dispatcher).values(claim_digest="f" * 64)
            )
        else:
            changes = (
                {"lease_epoch": 2} if damage == "epoch" else {"lease_until": START}
            )
            await writer.execute(update(attempt).values(**changes))
    before = await state(database)

    with pytest.raises(ClaimAccessRejected) as caught:
        await case.claim(database)
    after = await state(database)

    assert caught.value.reason == ("conflict" if damage == "digest" else "stale")
    assert before == after


@pytest.mark.parametrize("mode", ["repeatable_read", "readonly"])
@pytest.mark.asyncio
async def test_claim_requires_fresh_writable_postgres_transaction(
    database: Database, evaluator: Evaluator, mode: str
) -> None:
    case = await claim_case(database, evaluator)
    prepared = await case.prepare(database)
    before = await state(database)

    if database.dialect is Dialect.POSTGRESQL:
        async with database.transaction() as writer:
            await writer.execute(
                text(
                    "SET TRANSACTION READ ONLY"
                    if mode == "readonly"
                    else "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"
                )
            )
            context = await case.context(writer)
            with executed_queries(writer) as statements:
                with pytest.raises(ClaimSourcesRejected):
                    await claim_attempt(
                        context,
                        case.binding.attempt_id,
                        case.query,
                        case.principal,
                        prepared,
                        SOURCE_LIMITS,
                        HISTORY_LIMITS,
                        WORKER_BYTES,
                        UNCERTAINTY,
                    )
        assert [sql for sql, _ in statements] == [WRITABLE_POSTGRES_MODE.text]
        assert await state(database) == before
    else:
        decision = await case.claim(database)
        assert decision.outcome is ClaimOutcome.GRANTED

    assert database.units_in_flight() == 0
    assert database.writer_is_free()


@pytest.mark.parametrize("replay", [False, True])
@pytest.mark.asyncio
async def test_claim_rechecks_current_namespace_authority_before_sources(
    database: Database, evaluator: Evaluator, replay: bool
) -> None:
    case = await claim_case(database, evaluator)
    if replay:
        await case.claim(database)
    denied = replace(
        case, principal=case.principal.model_copy(update={"namespaces": ()})
    )
    before = await state(database)

    async def invoke() -> ClaimOutcome:
        async with database.transaction() as writer:
            return (await denied.apply(writer, None)).outcome

    if replay:
        with pytest.raises(ClaimAccessRejected) as caught:
            await invoke()
        assert caught.value.reason == "forbidden"
    else:
        assert await invoke() is ClaimOutcome.INELIGIBLE
    after = await state(database)

    assert after == before


@pytest.mark.parametrize("change", ["incarnation", "domain", "token"])
@pytest.mark.asyncio
async def test_claim_refuses_foreign_worker_bindings_before_sources(
    database: Database, evaluator: Evaluator, change: str
) -> None:
    case = await claim_case(database, evaluator)
    denied = (
        replace(
            case,
            principal=case.principal.model_copy(update={"token_id": "foreign-token"}),
        )
        if change == "token"
        else replace(
            case,
            query=case.query.model_copy(
                update={
                    "incarnation_id"
                    if change == "incarnation"
                    else "execution_domain_id": "foreign-identity"
                }
            ),
        )
    )
    before = await state(database)

    async def invoke() -> None:
        async with database.transaction() as writer:
            await denied.apply(writer, None)

    with pytest.raises(ClaimAccessRejected) as caught:
        await invoke()
    after = await state(database)

    assert caught.value.reason == "forbidden"
    assert after == before


@pytest.mark.asyncio
async def test_policy_revision_change_refuses_stale_preparation_before_grant(
    database: Database, evaluator: Evaluator
) -> None:
    case = await claim_case(database, evaluator)
    prepared = await case.prepare(database)
    async with database.transaction() as writer:
        await writer.execute(
            update(namespace).values(policy_revision=namespace.c.policy_revision + 1)
        )
    before = await state(database)

    async def invoke() -> None:
        async with database.transaction() as writer:
            await case.apply(writer, prepared)

    with pytest.raises(ClaimSourcesRejected):
        await invoke()
    after = await state(database)

    assert before == after


@pytest.mark.asyncio
async def test_selected_attempt_lock_has_its_original_run_predicate(
    database: Database, evaluator: Evaluator
) -> None:
    case = await claim_case(database, evaluator)
    other = await another_run(database, case)
    before = await state(database)

    async with database.transaction() as writer:
        context = await case.context(writer)
        with executed_queries(writer) as statements:
            decision = await claim_attempt(
                context,
                other.binding.attempt_id,
                case.query,
                case.principal,
                None,
                SOURCE_LIMITS,
                HISTORY_LIMITS,
                WORKER_BYTES,
                UNCERTAINTY,
            )
    after = await state(database)

    assert decision.outcome is ClaimOutcome.INELIGIBLE
    assert before == after
    [(sql, _)] = [(sql, args) for sql, args in statements if "FROM attempt " in sql]
    assert "EXISTS (SELECT node_instance.id" in sql
    assert "node_instance.id = attempt.node_instance_id" in sql
    assert "node_instance.run_id =" in sql
    if database.dialect is Dialect.POSTGRESQL:
        assert sql.endswith("FOR UPDATE SKIP LOCKED")
