from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.claim_source_values import SourceAllowance
from kora.db.claim_sources import ClaimSourceBinding, ClaimSourceReadLimits
from kora.db.connection import Database
from kora.db.run_input_reads import RunInputBinding
from kora.db.tables import node_instance, wait
from kora.engine.admission_wait import AdmissionWaitState, save_admission_wait
from kora.engine.attempt_capacity import PreparedGraphCaps, check_attempt_capacity
from kora.engine.expiry_context import ExpiryContext
from tests.db.seed import Seeder
from tests.engine.expiry_support import expiry_context
from tests.engine.promotion_support import PromotionCase, promotion_case

LIMITS = ClaimSourceReadLimits(
    1_000_000, 4096, 1_000_000, (SourceAllowance(1_000_000, 1_000_000),) * 5
)
INTERVAL = timedelta(seconds=1)


@dataclass(frozen=True)
class AdmissionWaitCase:
    promotion: PromotionCase
    binding: ClaimSourceBinding
    blocker: str

    async def apply(
        self, context: ExpiryContext, expected_wait_id: str | None = None
    ) -> AdmissionWaitState:
        observation = await check_attempt_capacity(
            context,
            "local",
            PreparedGraphCaps(self.promotion.scope, None, None),
            global_limit=1,
            runtime_limit=None,
        )
        assert observation is not None
        return await save_admission_wait(
            context,
            self.binding,
            LIMITS,
            "local",
            observation,
            interval=INTERVAL,
            expected_wait_id=expected_wait_id,
        )

    async def context(self, writer: AsyncConnection) -> ExpiryContext:
        return await expiry_context(writer, self.promotion)

    async def saved_waits(self, database: Database) -> list[dict[str, object]]:
        async with database.read_transaction() as reader:
            with await reader.execute(
                select(wait).where(wait.c.node_instance_id == self.binding.node_id)
            ) as rows:
                return [dict(row) for row in rows.mappings()]


async def admission_case(database: Database) -> AdmissionWaitCase:
    promotion = await promotion_case(database, timeout="10s")
    await promotion.promote(database)
    async with database.transaction() as writer:
        seed = Seeder(writer)
        with await writer.execute(
            select(node_instance.c.id, node_instance.c.node_path)
            .where(node_instance.c.run_id == promotion.run_id)
            .order_by(node_instance.c.id)
            .limit(1)
        ) as rows:
            node = rows.one()
        await writer.execute(
            update(node_instance)
            .where(node_instance.c.id == node.id)
            .values(status="ready")
        )
        blocker = await seed.attempt(
            await seed.node(promotion.run_id, "historical", "cancelled"), "finishing"
        )
        binding = ClaimSourceBinding(
            RunInputBinding(
                promotion.scope.namespace_id,
                promotion.scope.graph_id,
                promotion.scope.version_id,
                promotion.run_id,
            ),
            node.id,
            seed.new_id(),
            seed.new_id(),
            node.node_path,
        )
    return AdmissionWaitCase(promotion, binding, blocker)
