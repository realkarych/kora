import pytest

from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database
from tests.api.deployment_cases import evaluator
from tests.engine.heartbeat_binding_cases import (
    relayed_process_exit_must_match_complete_retained_grant,
)

__all__ = ["evaluator"]


@pytest.mark.asyncio
async def test_relayed_process_exit_must_match_complete_retained_grant(
    database: Database, evaluator: Evaluator
) -> None:
    await relayed_process_exit_must_match_complete_retained_grant(
        database, evaluator, "saved_nonleader"
    )
