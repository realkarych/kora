from sqlalchemy import update

from kora.db.connection import Database
from kora.db.tables import node_instance
from kora.engine.run_materialization import prepare_run_materialization
from tests.db.seed import FakeClock, Seeder
from tests.engine.attempt_admission_support import (
    AttemptAdmissionCase,
    attempt_admission_case,
)
from tests.engine.promotion_support import PromotionCase, insert_pending
from tests.engine.test_graph_execution_contract import LIMITS
from tests.engine.test_readiness import readiness_source


async def optional_admission_case(
    database: Database,
    state: str = "failed",
    *,
    rule: str = "all_done",
    optional: bool = True,
) -> AttemptAdmissionCase:
    checked = await readiness_source(
        database, command_optional=optional, rules={"producer": rule}
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
