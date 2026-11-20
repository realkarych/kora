import pytest

from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database
from tests.api.deployment_cases import evaluator
from tests.engine.heartbeat_deadline_cases import (
    deadline_authority_ignores_scheduler_tick,
)

__all__ = ["evaluator"]


@pytest.mark.asyncio
@pytest.mark.parametrize("tick_first", [False, True])
async def test_heartbeat_deadline_authority_does_not_depend_on_scheduler_tick(
    database: Database, evaluator: Evaluator, tick_first: bool
) -> None:
    await deadline_authority_ignores_scheduler_tick(
        database, evaluator, "attempt", tick_first
    )
