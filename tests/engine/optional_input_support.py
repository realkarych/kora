from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.connection import Database
from kora.db.tables import node_instance
from kora.engine.attempt_admission import (
    AttemptAdmissionState,
    PreparedAttemptAdmission,
    admit_attempt,
)
from kora.engine.attempt_capacity import PreparedGraphCaps
from kora.engine.run_materialization import prepare_run_materialization
from tests.db.seed import FakeClock, Seeder
from tests.engine.admission_wait_support import INTERVAL
from tests.engine.admission_wait_support import LIMITS as SOURCE_LIMITS
from tests.engine.attempt_admission_support import (
    AttemptAdmissionCase,
    attempt_admission_case,
)
from tests.engine.expiry_support import expiry_context
from tests.engine.promotion_support import PromotionCase, insert_pending
from tests.engine.test_graph_execution_contract import LIMITS
from tests.engine.test_readiness import readiness_source


async def optional_admission_case(
    database: Database,
    state: str = "failed",
    *,
    rule: str = "all_done",
    optional: bool = True,
    run_timeout: str | None = None,
    inputless_command: bool = False,
) -> AttemptAdmissionCase:
    checked = await readiness_source(
        database,
        command_optional=optional,
        rules={"producer": rule},
        run_timeout=run_timeout,
        inputless_command=inputless_command,
    )
    materialization = prepare_run_materialization(
        checked, max_ir_bytes=LIMITS.resources.logical.ir_bytes, max_top_level_nodes=3
    )
    async with database.transaction() as writer:
        seed = Seeder(writer)
        source = PromotionCase(
            checked.scope,
            checked.pin,
            materialization,
            seed.new_id(),
            seed.new_id(),
            FakeClock(),
            seed.generator,
        )
        promotion = await insert_pending(writer, source)
    case = await attempt_admission_case(
        database, promotion=promotion, node_path="producer"
    )
    async with database.transaction() as writer:
        await writer.execute(
            update(node_instance)
            .where(
                node_instance.c.run_id == promotion.run_id,
                node_instance.c.node_path == "agent",
            )
            .values(status=state)
        )
    return case


async def admit_optional_candidate(
    writer: AsyncConnection,
    case: AttemptAdmissionCase,
    prepared: PreparedAttemptAdmission,
) -> AttemptAdmissionState:
    return await admit_attempt(
        await expiry_context(writer, case.promotion),
        prepared,
        SOURCE_LIMITS,
        PreparedGraphCaps(case.promotion.scope, None, None),
        global_limit=1,
        runtime_limit=1,
        interval=INTERVAL,
    )


async def set_optional_producer_state(
    writer: AsyncConnection, case: AttemptAdmissionCase, state: str
) -> None:
    await expiry_context(writer, case.promotion)
    await writer.execute(
        update(node_instance)
        .where(
            node_instance.c.run_id == case.promotion.run_id,
            node_instance.c.node_path == "agent",
        )
        .values(status=state)
    )
