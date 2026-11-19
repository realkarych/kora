from dataclasses import replace

import pytest

from kora.api.run_trigger_limits import START_PREPARATION
from kora.db.graph_value_json import GraphValueRejected
from tests.api.deployment_cases import evaluator

__all__ = ["evaluator"]


@pytest.mark.parametrize("field", ["native_bytes", "owner_text_bytes"])
def test_trigger_reservation_cannot_omit_acquisition_owners(field: str) -> None:
    larger = replace(
        START_PREPARATION.snapshot, **{field: START_PREPARATION.retained * 2}
    )
    limits = replace(START_PREPARATION, snapshot=larger)

    with pytest.raises(GraphValueRejected):
        limits.admit()
