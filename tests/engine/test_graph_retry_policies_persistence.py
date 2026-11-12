from copy import deepcopy

import pytest

from kora.db.connection import Database
from kora.db.graph_value_json import (
    GraphValueFailure,
    GraphValueRejected,
    ObjectReader,
    TextBudget,
    render_object,
)
from kora.db.graph_values import (
    DecodedGraphValues,
    prepare_graph_values,
    read_graph_values,
)
from kora.engine.graph_retry_policies import (
    build_graph_retry_policies,
    read_graph_retry_policies,
)
from tests.db.closing import ConnectionLedger
from tests.db.seed import stored_graph_version
from tests.engine.graph_output_cases import LIMIT, child
from tests.engine.graph_retry_cases import retry_document


@pytest.mark.asyncio
async def test_retry_policies_round_trip_outside_metadata_read(
    database: Database,
) -> None:
    raw = retry_document()
    source = render_object(raw, TextBudget(LIMIT))
    expected = build_graph_retry_policies(source, max_encoded_bytes=LIMIT)
    contract = {"retry_policies": ObjectReader(expected).object()}
    encoded = prepare_graph_values(raw, contract, max_encoded_bytes=LIMIT)
    changed = deepcopy(raw)
    child(changed, "graph", "nodes", "producer", "retry")["backoff_coefficient"] = 2
    changed_source = render_object(changed, TextBudget(LIMIT))
    ledger = ConnectionLedger(database)
    row, foreign = await stored_graph_version(database, encoded)
    assert ledger.outstanding == database.units_in_flight() == 0
    assert row is not None
    assert isinstance(row.ir, str)
    decoded = read_graph_values(
        row.value_encoding_version,
        row.ir,
        row.execution_contract,
        max_encoded_bytes=LIMIT,
    )
    assert isinstance(decoded, DecodedGraphValues)
    saved = decoded.execution_contract["retry_policies"]
    restored = read_graph_retry_policies(row.ir, saved, max_encoded_bytes=LIMIT)
    with pytest.raises(GraphValueRejected) as failure:
        read_graph_retry_policies(changed_source, saved, max_encoded_bytes=LIMIT)
    await database.close()

    assert restored == expected
    assert row.ir == encoded.ir == source
    assert row.execution_contract == encoded.execution_contract
    assert decoded.execution_contract == contract
    assert failure.value.reason is GraphValueFailure.INVALID
    assert foreign is None
    assert ledger.opened > 0
    assert ledger.opened == ledger.closed
    assert (
        ledger.outstanding
        == ledger.closed_while_checked_out
        == database.units_in_flight()
        == 0
    )
