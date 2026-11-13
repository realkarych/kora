from dataclasses import dataclass

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.db.connection import Database
from kora.db.graph_version_reads import acquire_graph_version
from kora.db.identifiers import IdentifierGenerator
from kora.db.tables import event, inbound_event, node_instance, run, run_inputs
from kora.engine.graph_execution_contract import check_detached_execution_contract
from kora.engine.graph_resource_references import GraphResourceScope
from kora.engine.graph_version_identity import GraphVersionPin
from kora.engine.locks import TransitionLocks
from kora.engine.promotion_admission import record_manual_promotion_admission
from kora.engine.run_materialization import (
    PreparedRunMaterialization,
    prepare_run_materialization,
)
from kora.engine.run_promotion import (
    PromotionOutcome,
    promote_pending_run,
    promotion_scopes,
)
from kora.engine.run_promotion_read import INPUT_SENTINEL, PromotionReadLimits
from kora.engine.start_capacity import Overlap
from kora.engine.start_records import (
    AcceptedStart,
    StartReceipt,
    prepare_manual_start,
    start_receipt_document,
)
from kora.ir.json import JsonObject
from tests.db.seed import FakeClock, Seeder
from tests.engine.test_graph_execution_contract import (
    LIMITS,
    complete_inputs,
    insert_case,
    prepared_case,
)

READ_LIMITS = PromotionReadLimits(1_000_000, 1_000_000)
PRIVATE = "private-promotion-key"


@dataclass(frozen=True)
class PromotionCase:
    scope: GraphResourceScope
    pin: GraphVersionPin
    materialization: PreparedRunMaterialization
    run_id: str
    receipt_id: str
    clock: FakeClock
    identifiers: IdentifierGenerator

    async def locks(self, writer: AsyncConnection) -> TransitionLocks:
        locks = TransitionLocks(writer)
        await locks.acquire_run(self.run_id)
        await locks.acquire_scopes(promotion_scopes(self.scope))
        return locks

    async def apply(
        self, writer: AsyncConnection, locks: TransitionLocks
    ) -> PromotionOutcome:
        return await promote_pending_run(
            writer,
            locks,
            self.scope,
            self.pin,
            self.materialization,
            self.run_id,
            self.clock.now,
            self.identifiers,
            READ_LIMITS,
        )

    async def promote(self, database: Database) -> PromotionOutcome:
        async with database.transaction() as writer:
            return await self.apply(writer, await self.locks(writer))

    async def snapshot(
        self, database: Database
    ) -> tuple[dict[str, object], list[dict[str, object]], list[dict[str, object]]]:
        async with database.read_transaction() as reader:
            with await reader.execute(
                select(run).where(run.c.id == self.run_id)
            ) as result:
                saved = dict(result.mappings().one())
            with await reader.execute(
                select(node_instance)
                .where(node_instance.c.run_id == self.run_id)
                .order_by(node_instance.c.id)
            ) as result:
                nodes = [dict(row) for row in result.mappings()]
            with await reader.execute(
                select(event).where(event.c.run_id == self.run_id)
            ) as result:
                events = [dict(row) for row in result.mappings()]
        return saved, nodes, events


async def promotion_case(
    database: Database,
    overlap: Overlap = "allow",
    timeout: str | None = "01h",
    retention: JsonObject | None = None,
) -> PromotionCase:
    clock = FakeClock()
    async with database.transaction() as writer:
        seed = Seeder(writer)
        namespace = await seed.namespace()
        graph_id = await seed.graph(namespace)
        scope = GraphResourceScope(namespace, graph_id, seed.new_id())
        run_id, receipt_id = seed.new_id(), seed.new_id()
        identifiers = seed.generator
    raw, envelope, refs, owners = complete_inputs(
        scope,
        resources=False,
        graph_settings={"overlap": overlap},
        runtime_settings={"limits": {} if timeout is None else {"timeout": timeout}},
    )
    prepared, pin = prepared_case(raw, envelope, scope)
    trigger = {"kind": "manual", "overlap": overlap, "occurrence_key": receipt_id}
    identity = prepare_manual_start(scope.namespace_id, scope.graph_id, {})
    receipt = StartReceipt(
        version=1,
        kind="manual",
        namespace_id=scope.namespace_id,
        graph_id=scope.graph_id,
        request_digest=identity.request_digest,
        response=AcceptedStart(
            outcome="accepted",
            run_id=run_id,
            graph_version_id=scope.version_id,
            status="pending",
        ),
    )
    async with database.transaction() as writer:
        seed = Seeder(writer)
        await insert_case(seed, scope, 1, prepared, refs)
        values = seed.run_values(
            scope.namespace_id,
            scope.graph_id,
            scope.version_id,
            "pending",
            idempotency_key=PRIVATE,
        )
        values.update(
            id=run_id,
            trigger=trigger,
            limits={} if timeout is None else {"timeout": timeout},
            input_encoding_version=1,
            retention={} if retention is None else retention,
            params=INPUT_SENTINEL,
            cel_params_projection=INPUT_SENTINEL,
            started_at=None,
        )
        await writer.execute(insert(run).values(**values))
        await writer.execute(
            insert(run_inputs).values(run_id=run_id, params=b"{}", cel_projection=b"{}")
        )
        receipt_values: dict[str, object] = dict(
            id=receipt_id,
            namespace_id=scope.namespace_id,
            name=f"kora.start:{scope.graph_id}",
            idempotency_key=PRIVATE,
            payload={},
            auth_kind="bearer",
            nonce=None,
            received_at=clock.now(),
            consumed_by=[],
            start_outcome=start_receipt_document(receipt),
            **seed.stamps(),
        )
        await writer.execute(insert(inbound_event).values(**receipt_values))
        locks = TransitionLocks(writer)
        await locks.acquire_run(run_id)
        await locks.acquire_scopes(promotion_scopes(scope))
        await record_manual_promotion_admission(
            writer, locks, scope, values, receipt_values
        )
    async with database.read_transaction() as reader:
        acquired = await acquire_graph_version(
            reader,
            scope.namespace_id,
            scope.graph_id,
            scope.version_id,
            max_encoded_bytes=LIMITS.identity.max_encoded_bytes,
        )
    assert acquired is not None
    checked = check_detached_execution_contract(
        acquired, pin, scope, refs, owners, limits=LIMITS
    )
    materialization = prepare_run_materialization(
        checked, max_ir_bytes=len(checked.canonical_ir), max_top_level_nodes=3
    )
    return PromotionCase(
        scope, pin, materialization, run_id, receipt_id, clock, identifiers
    )


async def append_pending(database: Database, previous: PromotionCase) -> PromotionCase:
    async with database.transaction() as writer:
        return await insert_pending(writer, previous)


async def insert_pending(
    writer: AsyncConnection, previous: PromotionCase
) -> PromotionCase:
    seed = Seeder(writer)
    run_id, receipt_id = seed.new_id(), seed.new_id()
    values = seed.run_values(
        previous.scope.namespace_id,
        previous.scope.graph_id,
        previous.scope.version_id,
        "pending",
    )
    values.update(
        id=run_id,
        idempotency_key=run_id,
        trigger={
            "kind": "manual",
            "overlap": previous.materialization.manual_overlap,
            "occurrence_key": receipt_id,
        },
        limits={}
        if previous.materialization.timeout is None
        else {"timeout": previous.materialization.timeout},
        input_encoding_version=1,
        params=INPUT_SENTINEL,
        cel_params_projection=INPUT_SENTINEL,
        started_at=None,
        needs_scheduling=False,
    )
    await writer.execute(insert(run).values(**values))
    await writer.execute(
        insert(run_inputs).values(run_id=run_id, params=b"{}", cel_projection=b"{}")
    )
    receipt = StartReceipt(
        version=1,
        kind="manual",
        namespace_id=previous.scope.namespace_id,
        graph_id=previous.scope.graph_id,
        request_digest="a" * 64,
        response=AcceptedStart(
            outcome="accepted",
            run_id=run_id,
            graph_version_id=previous.scope.version_id,
            status="pending",
        ),
    )
    receipt_values: dict[str, object] = dict(
        id=receipt_id,
        namespace_id=previous.scope.namespace_id,
        name=f"kora.start:{previous.scope.graph_id}",
        idempotency_key=run_id,
        payload={},
        auth_kind="bearer",
        nonce=None,
        received_at=previous.clock.now(),
        consumed_by=[],
        start_outcome=start_receipt_document(receipt),
        **seed.stamps(),
    )
    await writer.execute(insert(inbound_event).values(**receipt_values))
    locks = TransitionLocks(writer)
    await locks.acquire_run(run_id)
    await locks.acquire_scopes(promotion_scopes(previous.scope))
    await record_manual_promotion_admission(
        writer, locks, previous.scope, values, receipt_values
    )
    return PromotionCase(
        previous.scope,
        previous.pin,
        previous.materialization,
        run_id,
        receipt_id,
        previous.clock,
        previous.identifiers,
    )
