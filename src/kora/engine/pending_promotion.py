import asyncio
from collections.abc import Callable
from dataclasses import dataclass

from kora.db.connection import (
    Database,
    DatabaseSnapshotFailure,
    DatabaseSnapshotRejected,
    DatabaseWriterFailure,
    DatabaseWriterRejected,
)
from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected
from kora.db.identifiers import Clock, IdentifierGenerator, is_identifier
from kora.engine.graph_execution_contract import (
    check_detached_execution_contract,
    execution_contract_read_reservation,
)
from kora.engine.graph_snapshot import GraphSnapshotLimits, acquire_graph_snapshot
from kora.engine.graph_version_identity import GraphVersionPin
from kora.engine.locks import ScopeDeadlineExpired, TransitionLocks
from kora.engine.run_materialization import (
    MaterializationRejected,
    prepare_run_materialization,
)
from kora.engine.run_promotion import (
    PromotionOutcome,
    promote_pending_run,
    promotion_scopes,
)
from kora.engine.run_promotion_read import (
    PromotionReadLimits,
    read_pending_promotion_scope,
)

MAX_PROMOTION_NODES = 3


@dataclass(frozen=True, slots=True)
class PendingPromotion:
    database: Database
    graph: GraphSnapshotLimits
    limits: PromotionReadLimits
    clock: Clock
    identifiers: IdentifierGenerator

    def __post_init__(self) -> None:
        if min(self.graph.native_bytes, self.graph.owner_text_bytes) <= 0:
            raise GraphValueRejected(GraphValueFailure.BUDGET)
        execution_contract_read_reservation(self.graph.graph)

    async def process(
        self, run_id: str, *, deadline: float, stopped: Callable[[], bool]
    ) -> PromotionOutcome:
        if not is_identifier(run_id) or stopped():
            return PromotionOutcome.REFUSED
        timer = asyncio.timeout_at(deadline)
        try:
            async with self.database.read_snapshot(deadline=deadline) as reader:
                scope = await read_pending_promotion_scope(reader, run_id, self.limits)
                if scope is None or stopped():
                    return PromotionOutcome.REFUSED
                acquired = await acquire_graph_snapshot(reader, scope, self.graph)
            graph = acquired.graph
            pin = GraphVersionPin(
                scope.graph_id,
                scope.version_id,
                graph.digests[1].decode(graph.encoding),
            )
            checked = check_detached_execution_contract(
                graph,
                pin,
                scope,
                acquired.physical,
                acquired.owners,
                limits=self.graph.graph,
            )
            materialization = prepare_run_materialization(
                checked,
                max_ir_bytes=self.graph.graph.resources.logical.ir_bytes,
                max_top_level_nodes=MAX_PROMOTION_NODES,
            )
            del acquired, graph, checked
            if stopped():
                return PromotionOutcome.REFUSED
            async with self.database.transaction(admission_deadline=deadline) as writer:
                async with timer:
                    locks = TransitionLocks(writer)
                    if stopped() or not await locks.acquire_run(run_id, wait=False):
                        return PromotionOutcome.REFUSED
                    await locks.acquire_scopes(
                        promotion_scopes(scope), deadline=deadline
                    )
                    if stopped():
                        return PromotionOutcome.REFUSED
                    return await promote_pending_run(
                        writer,
                        locks,
                        scope,
                        pin,
                        materialization,
                        run_id,
                        self.clock,
                        self.identifiers,
                        self.limits,
                    )
        except (
            GraphValueRejected,
            MaterializationRejected,
            UnicodeDecodeError,
            ScopeDeadlineExpired,
        ):
            return PromotionOutcome.REFUSED
        except (DatabaseSnapshotRejected, DatabaseWriterRejected) as error:
            if error.reason not in (
                DatabaseSnapshotFailure.DEADLINE,
                DatabaseWriterFailure.DEADLINE,
            ):
                raise
            return PromotionOutcome.REFUSED
        except TimeoutError:
            if not timer.expired():
                raise
            return PromotionOutcome.REFUSED
