from dataclasses import replace

import pytest
from sqlalchemy import event

from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database
from kora.engine.claim_context import ClaimAccessRejected
from tests.api.deployment_cases import evaluator
from tests.engine.claim_support import (
    claim_case,
    state,
)

__all__ = ["evaluator"]


@pytest.mark.parametrize("change", ["incarnation", "domain", "token"])
@pytest.mark.parametrize("replay", [False, True], ids=["fresh", "retained"])
@pytest.mark.asyncio
async def test_claim_refuses_foreign_worker_bindings_before_sources(
    database: Database, evaluator: Evaluator, change: str, replay: bool
) -> None:
    case = await claim_case(database, evaluator)
    if replay:
        granted = await case.claim(database)
        assert granted.delivery is not None
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
    source_reads: list[str] = []
    engines = {database.reader_engine.sync_engine, database.writer_engine.sync_engine}

    def observe_source_reads(
        connection: object, cursor: object, statement: str, *arguments: object
    ) -> None:
        if any(
            source in statement
            for source in ("attempt_first_claim_basis", "attempt_claim_read_admission")
        ):
            source_reads.append(statement)

    async def invoke() -> None:
        if replay:
            await denied.replay(database)
            return
        async with database.transaction() as writer:
            await denied.apply(writer, None)

    for engine in engines:
        event.listen(engine, "before_cursor_execute", observe_source_reads)
    try:
        with pytest.raises(ClaimAccessRejected) as caught:
            await invoke()
    finally:
        for engine in engines:
            event.remove(engine, "before_cursor_execute", observe_source_reads)
    after = await state(database)

    assert caught.value.reason == "forbidden"
    assert source_reads == []
    assert after == before
