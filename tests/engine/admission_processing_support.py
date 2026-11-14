import asyncio

from kora.db.connection import Database
from kora.engine.admission_processing import (
    AdmissionProcessingLimits,
    AdmissionProcessor,
)
from kora.engine.attempt_admission import AttemptAdmissionState
from tests.api.deployment_cases import SELECTION as INSTALLATION
from tests.engine.admission_preparation_support import LIMITS as PREPARATION_LIMITS
from tests.engine.admission_snapshot_support import AdmissionSnapshotCase
from tests.engine.admission_wait_support import INTERVAL
from tests.engine.admission_wait_support import LIMITS as SOURCE_LIMITS
from tests.engine.attempt_admission_support import SELECTION
from tests.engine.promotion_support import READ_LIMITS

LIMITS = AdmissionProcessingLimits(
    PREPARATION_LIMITS, SOURCE_LIMITS, READ_LIMITS, 10**22
)


def processor(database: Database, case: AdmissionSnapshotCase) -> AdmissionProcessor:
    return AdmissionProcessor(
        database,
        LIMITS,
        SELECTION,
        INSTALLATION,
        case.promotion.clock.now,
        case.promotion.identifiers,
        1,
        1,
        INTERVAL,
    )


async def process(
    owner: AdmissionProcessor, node_id: str, *, periodic: bool = True
) -> AttemptAdmissionState:
    return await owner.process(
        node_id,
        periodic_recheck=periodic,
        deadline=asyncio.get_running_loop().time() + 10.0,
    )
