import pytest

from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database
from tests.api.deployment_cases import evaluator
from tests.engine.heartbeat_deadline_cases import own_deadline_keeps_prior_revocation

__all__ = ["evaluator"]


@pytest.mark.asyncio
@pytest.mark.parametrize("prior", ["cancel", "superseded"])
async def test_own_deadline_does_not_override_prior_result_revocation(
    database: Database, evaluator: Evaluator, prior: str
) -> None:
    await own_deadline_keeps_prior_revocation(database, evaluator, prior)
