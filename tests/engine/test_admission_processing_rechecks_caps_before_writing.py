import pytest

from kora.api.graph_deployment_worker import DeploymentPreparer
from kora.conformance.capture import Capture
from kora.db.connection import Database
from tests.api.deployment_cases import preparer
from tests.api.support import api_capture
from tests.engine.admission_processing_freshness import recheck_current_sources

__all__ = ["api_capture", "preparer"]


@pytest.mark.asyncio
async def test_admission_processing_rechecks_caps_before_writing(
    database: Database,
    preparer: DeploymentPreparer,
    api_capture: Capture,
) -> None:
    await recheck_current_sources(database, preparer, api_capture, "caps")
