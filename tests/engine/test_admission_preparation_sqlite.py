from pathlib import Path

import pytest

from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.conformance.capture import Capture
from kora.engine.namespace_policy import NamespacePolicyConfiguration
from kora.engine.price_initialization import PriceConfiguration
from tests.api.deployment_cases import preparer
from tests.api.support import api_capture
from tests.db.test_run_input_parent import open_sqlite
from tests.engine.admission_preparation_support import prepare
from tests.engine.admission_snapshot_support import snapshot_case
from tests.engine.test_admission_initialization import initializer, rates

__all__ = ["api_capture", "preparer"]


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["UTF-16le", "UTF-16be"])
async def test_admission_preparation_preserves_sqlite_unicode_sources(
    tmp_path: Path, preparer: DeploymentPreparer, api_capture: Capture, encoding: str
) -> None:
    database = await open_sqlite(tmp_path, encoding)
    try:
        case = await snapshot_case(database, preparer, api_capture)
        policy = NamespacePolicyConfiguration.prepare(
            case.promotion.scope.namespace_id,
            2,
            {"metadata": {"text": "\u4ef7\u683c\U0001f600"}},
        )
        configuration = initializer(
            database,
            (policy,),
            (
                PriceConfiguration(
                    case.promotion.scope.namespace_id,
                    case.promotion.clock.now(),
                    {"\u6a21\u578b\U0001f600": rates("2")},
                ),
            ),
        )
        await configuration.initialize(api_capture.diagnostics)

        prepared = await prepare(database, case)

        assert prepared.policy == policy
        assert set(prepared.price.prices) == {"\u6a21\u578b\U0001f600"}
        assert prepared.checked.pin == case.promotion.pin
        assert prepared.acquired.graph.physical.encoding == encoding.lower()
        assert database.units_in_flight() == 0
    finally:
        await database.close()
