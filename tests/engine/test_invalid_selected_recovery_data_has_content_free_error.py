from dataclasses import replace

import pytest

from kora.cel_eval.thread import Evaluator
from kora.db.claim_source_values import ClaimSourcesRejected
from kora.db.connection import Database
from kora.engine.claim_selection import prepare_selected_claim
from tests.api.deployment_cases import evaluator
from tests.engine.claim_support import (
    claim_case,
)

__all__ = ["evaluator"]


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
