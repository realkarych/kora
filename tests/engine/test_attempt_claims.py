import asyncio
from dataclasses import replace
from datetime import timedelta
from typing import cast

import pytest
from sqlalchemy import select, text, update

from kora.cel_eval.thread import Evaluator
from kora.db.claim_source_values import ClaimSourcesRejected
from kora.db.connection import Database, Dialect
from kora.db.grant_history_v1 import ACK_FIELDS, TIP_FIELDS
from kora.db.tables import (
    attempt,
    attempt_dispatcher,
    namespace,
    node_instance,
    run,
    worker,
)
from kora.engine.attempt_claims import ClaimOutcome, claim_attempt
from kora.engine.claim_context import ClaimAccessRejected
from kora.engine.claim_selection import prepare_selected_claim
from kora.engine.worker_registration import register_worker
from kora.engine.writer_mode import WRITABLE_POSTGRES_MODE
from kora.ir.graph_json import ObjectReader
from kora.ir.json import JsonObject
from kora.protocols.worker_claim_wire import (
    MAX_CLAIM_BYTES,
    MAX_IMMUTABLE_BYTES,
    ClaimPartition,
    prepare_claim_part,
)
from tests.api.deployment_cases import evaluator, skeleton_ir
from tests.db.closing import ConnectionLedger
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
    wait_for_claim_waiter,
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
async def test_zero_timeout_returns_learnable_expired_first_grant(
    database: Database, evaluator: Evaluator
) -> None:
    ir = skeleton_ir()
    graph = cast(JsonObject, ir["graph"])
    node = cast(JsonObject, cast(JsonObject, graph["nodes"])["agent"])
    node["limits"] = {"timeout": "0ms"}
    case = await claim_case(database, evaluator, ir)

    first = await case.claim(database)
    saved = await state(database)
    replay = await case.claim(database)

    assert first.outcome is ClaimOutcome.GRANTED
    assert first.delivery is not None
    assert replay.delivery is not None
    assert replay.delivery.encode() == first.delivery.encode()
    assert first.delivery.system.permitted_actions == ("seal_request",)
    deadline = first.delivery.system.deadline.value
    assert deadline is not None
    assert deadline.at == "2026-10-20T12:00:00.000000Z"
    assert deadline.clock_uncertainty_us == str(
        UNCERTAINTY // timedelta(microseconds=1)
    )
    assert saved["attempt"][0]["deadline_expired_at"] == START
    assert saved["attempt"][0]["recovery_required"] is True
    assert saved["attempt"][0]["desired_state"] == "cancel"
    assert saved["run"][0]["needs_scheduling"] is True
    assert saved["run"][0]["deadline_expired_at"] is None


@pytest.mark.asyncio
async def test_two_replicas_share_worker_capacity_across_runs(
    database: Database, evaluator: Evaluator
) -> None:
    first = await claim_case(database, evaluator)
    second = await another_run(database, first)
    prepared_first, prepared_second = (
        await first.prepare(database),
        await second.prepare(database),
    )
    decision_ready, release, contender_started = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    ledger = ConnectionLedger(database)

    async def holder() -> ClaimOutcome:
        async with database.transaction() as writer:
            result = await first.apply(writer, prepared_first)
            decision_ready.set()
            await release.wait()
        return result.outcome

    async def contender() -> ClaimOutcome:
        contender_started.set()
        async with database.transaction() as writer:
            return (await second.apply(writer, prepared_second)).outcome

    async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:
        holding = tasks.create_task(holder())
        await decision_ready.wait()
        competing = tasks.create_task(contender())
        await contender_started.wait()
        await wait_for_claim_waiter(database)
        release.set()
        results = await asyncio.gather(holding, competing)
    saved = await state(database)

    assert results == [ClaimOutcome.GRANTED, ClaimOutcome.INELIGIBLE]
    assert len(saved["attempt_dispatcher"]) == 1
    assert sum(row["current_dispatcher_open"] is True for row in saved["attempt"]) == 1
    assert database.units_in_flight() == ledger.outstanding == 0


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


@pytest.mark.parametrize(
    "condition", ["running", "cancelled", "finishing", "terminal_run", "retired_node"]
)
@pytest.mark.asyncio
async def test_regrant_preserves_original_identity_deadline_and_resource_ownership(
    database: Database, evaluator: Evaluator, condition: str
) -> None:
    ir = skeleton_ir()
    graph = cast(JsonObject, ir["graph"])
    node = cast(JsonObject, cast(JsonObject, graph["nodes"])["agent"])
    node["limits"] = {"timeout": "1h"}
    case = await claim_case(database, evaluator, ir)
    first = await case.claim(database)
    async with database.transaction() as writer:
        await writer.execute(
            update(attempt).values(
                execution_state="live",
                desired_state="cancel" if condition == "cancelled" else "run",
                status="finishing" if condition == "finishing" else "claimed",
            )
        )
        await writer.execute(
            update(run).values(
                target_key="held",
                target_held_since=START,
                status="failed" if condition == "terminal_run" else "running",
            )
        )
        if condition == "retired_node":
            await writer.execute(
                update(node_instance).values(current_attempt_id=None, status="failed")
            )
    original = await state(database)
    case.clock.advance(121)
    next_case = replace(
        case, query=case.query.model_copy(update={"claim_id": "next-claim"})
    )

    second = await next_case.claim(database)
    saved = await state(database)
    replay = await next_case.claim(database)

    assert second.outcome is ClaimOutcome.GRANTED
    assert first.delivery is not None
    assert second.delivery is not None
    assert replay.delivery is not None
    assert second.delivery.system.mode == (
        "execute" if condition == "running" else "recover"
    )
    assert second.delivery.system.permitted_actions == ("observe", "retire")
    assert second.delivery.system.lease_epoch == 2
    assert second.delivery.system.grant_history.count == 2
    assert second.delivery.system.deadline == first.delivery.system.deadline
    assert second.delivery.encode() == replay.delivery.encode()
    assert saved["attempt"][0]["started_at"] == original["attempt"][0]["started_at"]
    assert (
        saved["attempt"][0]["execution_domain_id"]
        == original["attempt"][0]["execution_domain_id"]
    )
    assert saved["attempt"][0]["recovery_required"] is True
    assert (
        saved["run"][0]["target_held_since"] == original["run"][0]["target_held_since"]
    )


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


@pytest.mark.asyncio
async def test_live_legacy_projection_reconciliation_is_one_terminal_transition(
    database: Database, evaluator: Evaluator
) -> None:
    case = await claim_case(database, evaluator)
    second = await another_run(database, case)
    await case.claim(database)
    async with database.transaction() as writer:
        await writer.execute(
            update(attempt)
            .where(attempt.c.id == case.binding.attempt_id)
            .values(current_dispatcher_open=None)
        )
    next_case = replace(
        case, query=case.query.model_copy(update={"claim_id": "reconcile"})
    )
    before = await state(database)

    blocked = await second.claim(database)
    async with database.transaction() as writer:
        reconciled = await next_case.apply(writer, None)
    after = await state(database)

    assert blocked.outcome is ClaimOutcome.INELIGIBLE
    assert reconciled.outcome is ClaimOutcome.MAINTAINED
    assert reconciled.delivery is None
    assert after["attempt_dispatcher"] == before["attempt_dispatcher"]
    assert after["event"] == before["event"]
    assert (
        after["attempt_claim_read_admission"] == before["attempt_claim_read_admission"]
    )
    changed = [row for row in after["attempt"] if row["id"] == case.binding.attempt_id]
    assert len(changed) == 1
    assert changed[0]["current_dispatcher_open"] is True
    assert changed[0]["lease_epoch"] == 1


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


@pytest.mark.asyncio
async def test_parent_expiry_uses_time_after_worker_guard_wait(
    database: Database, evaluator: Evaluator
) -> None:
    ir = skeleton_ir()
    runtime = cast(JsonObject, ir["runtime"])
    runtime["limits"] = {"timeout": "1s"}
    case = await claim_case(database, evaluator, ir)
    other = await another_run(database, case)
    prepared = await other.prepare(database)
    before = await state(database)
    guarded, release = asyncio.Event(), asyncio.Event()

    async def holder() -> None:
        async with database.transaction() as writer:
            await case.context(writer)
            await writer.execute(
                select(worker.c.id)
                .where(worker.c.id == case.query.worker_id)
                .with_for_update()
            )
            guarded.set()
            await release.wait()

    async def contender() -> ClaimOutcome:
        async with database.transaction() as writer:
            return (await other.apply(writer, prepared)).outcome

    async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:
        held = tasks.create_task(holder())
        await guarded.wait()
        pending = tasks.create_task(contender())
        await wait_for_claim_waiter(database)
        case.clock.advance(1)
        release.set()
        await held
        outcome = await pending
    after = await state(database)

    assert outcome is ClaimOutcome.EXPIRE
    assert before == after
    assert database.units_in_flight() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["known", "expired", "receipt"])
async def test_unprepared_legacy_selection_rechecks_final_grant_state(
    database: Database, evaluator: Evaluator, changed: str
) -> None:
    case = await claim_case(database, evaluator)
    await case.claim(database)
    async with database.transaction() as writer:
        await writer.execute(
            update(attempt).values(
                current_dispatcher_open=True if changed == "known" else None
            )
        )
    if changed == "expired":
        case.clock.advance(121)
    selected = (
        case
        if changed == "receipt"
        else replace(case, query=case.query.model_copy(update={"claim_id": "legacy"}))
    )
    before = await state(database)

    async with database.transaction() as writer:
        result = await selected.apply(writer, None)
    after = await state(database)

    assert result.outcome is (
        ClaimOutcome.REDIRECT if changed == "receipt" else ClaimOutcome.INELIGIBLE
    )
    assert result.redirect == (
        case.binding.attempt_id if changed == "receipt" else None
    )
    assert result.delivery is None
    assert after == before
