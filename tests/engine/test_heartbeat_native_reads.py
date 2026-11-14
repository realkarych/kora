import json

import pytest
from sqlalchemy import insert, text

from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database, Dialect
from kora.db.tables import attempt_dispatcher
from kora.db.worker_heartbeats import heartbeat_dispatchers
from kora.engine.heartbeat_evidence import HeartbeatGrant
from tests.api.deployment_cases import evaluator
from tests.db.test_graph_read_admission import executed_queries
from tests.engine.claim_support import claim_case
from tests.engine.heartbeat_support import HEARTBEAT_LIMITS

__all__ = ["evaluator"]


@pytest.mark.asyncio
async def test_submitted_dispatcher_batch_uses_indexed_lookup_independent_of_history(
    database: Database, evaluator: Evaluator
) -> None:
    case = await claim_case(database, evaluator)
    decision = await case.claim(database)
    assert decision.delivery is not None
    granted = decision.delivery.system
    records = [
        dict(
            id=f"{number:026d}",
            attempt_id=granted.attempt_id,
            execution_domain_id=granted.execution_domain_id,
            worker_id=case.principal.owner,
            incarnation_id=case.query.incarnation_id,
            lease_epoch=number,
            mode="recover",
            granted_at=case.clock.now(),
            closed_at=case.clock.now(),
            evidence=None,
            created_at=case.clock.now(),
            updated_at=case.clock.now(),
        )
        for number in range(2, 10_002)
    ]
    async with database.transaction() as writer:
        await writer.execute(insert(attempt_dispatcher), records)
        if database.dialect is Dialect.POSTGRESQL:
            await writer.execute(text("ANALYZE attempt_dispatcher"))
    keys = tuple((f"{number:026d}", number) for number in range(5000, 5128))

    async with database.read_transaction() as reader:
        with executed_queries(reader) as statements:
            found = await heartbeat_dispatchers(
                reader,
                granted.attempt_id,
                keys,
                tuple(HeartbeatGrant.model_fields),
                HEARTBEAT_LIMITS,
            )
        assert len(statements) == 1
        sql, parameters = statements[0]
        prefix = (
            "EXPLAIN QUERY PLAN "
            if database.dialect is Dialect.SQLITE
            else "EXPLAIN (FORMAT JSON) "
        )
        plan = (await reader.exec_driver_sql(prefix + sql, parameters)).all()
        encoded = json.dumps([list(row) for row in plan])

    assert len(found) == 128
    assert {(row["id"], row["lease_epoch"]) for row in found} == set(keys)
    if database.dialect is Dialect.SQLITE:
        assert "SEARCH attempt_dispatcher USING INDEX" in encoded
        assert "SCAN attempt_dispatcher" not in encoded
    else:
        assert '"Node Type": "Seq Scan"' not in encoded, encoded
        assert any(
            f'"Index Name": "{name}"' in encoded
            for name in (
                "pk_attempt_dispatcher",
                "uq_attempt_dispatcher_attempt_id_lease_epoch",
            )
        ), encoded
