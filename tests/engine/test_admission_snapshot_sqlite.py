from dataclasses import replace
from pathlib import Path

import pytest

from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.conformance.capture import Capture
from kora.db.claim_source_values import decode_claim_source
from kora.engine.namespace_policy import NamespacePolicyConfiguration
from tests.api.deployment_cases import preparer
from tests.api.support import api_capture
from tests.db.closing import ConnectionLedger
from tests.db.test_run_input_parent import open_sqlite
from tests.engine.admission_snapshot_support import (
    LIMITS,
    acquire,
    checked,
    snapshot_case,
)

__all__ = ["api_capture", "preparer"]


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["UTF-8", "UTF-16le", "UTF-16be"])
async def test_admission_snapshot_preserves_native_sqlite_encoding(
    tmp_path: Path,
    preparer: DeploymentPreparer,
    api_capture: Capture,
    encoding: str,
) -> None:
    database = await open_sqlite(tmp_path, encoding)
    try:
        case = await snapshot_case(database, preparer, api_capture)
        ledger = ConnectionLedger(database)
        native, raw = LIMITS.reservation()
        limits = replace(LIMITS, native_bytes=native, raw_bytes=raw)

        acquired = await acquire(database, case, limits=limits)
        assert ledger.outstanding == database.units_in_flight() == 0
        graph = checked(acquired.graph, case.promotion.pin, case.promotion.scope)
        policy = NamespacePolicyConfiguration.prepare(
            acquired.candidate.run.namespace_id,
            acquired.policy.revision,
            decode_claim_source(acquired.policy.payload, encoding.lower()),
        )

        assert acquired.graph.physical.encoding == encoding.lower()
        assert graph.scope == case.promotion.scope
        assert policy == case.policy
        assert acquired.params == b"{}"
        assert ledger.checkouts == 1
    finally:
        await database.close()
