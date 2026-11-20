import pytest

from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database
from tests.api.deployment_cases import evaluator
from tests.engine.heartbeat_binding_cases import (
    runtime_evidence_must_bind_original_attempt_and_domain,
)

__all__ = ["evaluator"]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["attempt_id", "execution_domain_id"])
async def test_runtime_evidence_must_bind_original_attempt_and_domain(
    database: Database, evaluator: Evaluator, field: str
) -> None:
    await runtime_evidence_must_bind_original_attempt_and_domain(
        database, evaluator, field
    )
