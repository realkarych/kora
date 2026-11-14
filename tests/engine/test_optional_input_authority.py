from datetime import timedelta

import pytest
from sqlalchemy import delete, insert, select, update

from kora.db.connection import Database
from kora.db.tables import attempt, node_instance, run, wait
from kora.engine.attempt_admission import AttemptAdmissionState
from tests.engine.attempt_admission_support import append_admission_case
from tests.engine.optional_input_support import (
    admit_optional_candidate,
    optional_admission_case,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed",
    [
        "missing",
        "foreign",
        "kind",
        "rule",
        "compatible_replacement",
        "target_replacement",
    ],
)
async def test_optional_admission_preserves_original_target_and_current_producer_scope(
    database: Database, changed: str
) -> None:
    case = await optional_admission_case(database)
    prepared = case.prepare()
    peer = await append_admission_case(database, case) if changed == "foreign" else None
    path = "producer" if changed == "target_replacement" else "agent"
    async with database.transaction() as writer:
        with await writer.execute(
            select(node_instance).where(
                node_instance.c.run_id == case.promotion.run_id,
                node_instance.c.node_path == path,
            )
        ) as rows:
            original = dict(rows.mappings().one())
        predicate = node_instance.c.id == original["id"]
        if changed in ("kind", "rule"):
            values: dict[str, object] = (
                {"kind": "command"}
                if changed == "kind"
                else {"trigger_rule": "all_done"}
            )
            await writer.execute(
                update(node_instance).where(predicate).values(**values)
            )
        else:
            await writer.execute(delete(node_instance).where(predicate))
            if changed.endswith("replacement"):
                await writer.execute(
                    insert(node_instance).values(
                        **{**original, "id": case.promotion.identifiers.new()}
                    )
                )
            if peer is not None:
                await writer.execute(
                    update(node_instance)
                    .where(
                        node_instance.c.run_id == peer.promotion.run_id,
                        node_instance.c.node_path == "agent",
                    )
                    .values(status="failed")
                )
    before = await case.promotion.snapshot(database)

    async with database.transaction() as writer:
        result = await admit_optional_candidate(writer, case, prepared)
    after = await case.promotion.snapshot(database)
    async with database.read_transaction() as reader:
        with await reader.execute(select(attempt.c.id)) as rows:
            attempts = rows.scalars().all()
        waiting = await reader.scalar(select(wait.c.id))

    assert waiting is None
    if changed == "compatible_replacement":
        assert result is AttemptAdmissionState.QUEUED
        assert attempts == [case.binding.attempt_id]
    else:
        assert result is AttemptAdmissionState.REFUSED
        assert attempts == []
        assert after == before


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["cancel", "deadline", "trigger_outcome"])
async def test_optional_candidate_rechecks_enclosing_authority_and_trigger(
    database: Database, changed: str
) -> None:
    case = await optional_admission_case(
        database, "skipped", rule="all_skipped", run_timeout="1h"
    )
    prepared = case.prepare()
    async with database.transaction() as writer:
        if changed == "cancel":
            await writer.execute(
                update(run)
                .where(run.c.id == case.promotion.run_id)
                .values(status="cancelled")
            )
        elif changed == "trigger_outcome":
            await writer.execute(
                update(node_instance)
                .where(
                    node_instance.c.run_id == case.promotion.run_id,
                    node_instance.c.node_path == "agent",
                )
                .values(status="failed")
            )
    if changed == "deadline":
        case.promotion.clock.advance(timedelta(hours=1).total_seconds())

    async with database.transaction() as writer:
        result = await admit_optional_candidate(writer, case, prepared)

    assert result is (
        AttemptAdmissionState.EXPIRED
        if changed == "deadline"
        else AttemptAdmissionState.REFUSED
    )
    async with database.read_transaction() as reader:
        assert await reader.scalar(select(attempt.c.id)) is None
        assert await reader.scalar(select(wait.c.id)) is None
