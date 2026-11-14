import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import cast

from sqlalchemy import insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection

from kora.cel_eval.thread import Evaluator
from kora.db.claim_source_values import prepare_claim_sources
from kora.db.claim_sources import ClaimSourceBinding
from kora.db.claim_sources_v1 import SOURCE_FIELDS
from kora.db.connection import Database, Dialect
from kora.db.grant_history import HistoryScope
from kora.db.identifiers import IdentifierGenerator
from kora.db.run_input_parent import INPUT_SENTINEL
from kora.db.tables import (
    api_token,
    attempt,
    attempt_claim_read_admission,
    attempt_dispatcher,
    event,
    node_instance,
    run,
    run_inputs,
    worker,
)
from kora.engine.attempt_claims import ClaimDecision, claim_attempt
from kora.engine.attempt_inputs import prepare_attempt_inputs
from kora.engine.claim_context import ClaimQuery
from kora.engine.claim_selection import SelectedClaim, prepare_selected_claim
from kora.engine.claim_snapshot import prepare_claim_snapshot
from kora.engine.claim_sources import insert_claim_attempt
from kora.engine.expiry_context import ExpiryContext
from kora.engine.grant_history import acquire_history, prepare_history
from kora.engine.graph_resource_references import GraphResourceScope
from kora.engine.graph_version_identity import GraphVersionPin
from kora.engine.locks import TransitionLocks
from kora.engine.worker_registration import Registration, register_worker
from kora.ir.json import JsonObject
from kora.protocols.authentication import NamespaceGrant, Principal
from kora.protocols.identity import ProcessIdentity
from tests.db.closing import until
from tests.db.seed import START, FakeClock, Seeder
from tests.engine.promotion_support import READ_LIMITS
from tests.engine.test_claim_snapshot import LIMITS as SNAPSHOT_LIMITS
from tests.engine.test_claim_snapshot import SELECTION, acquire, seed
from tests.engine.test_claim_sources import ALLOWANCE, source_values
from tests.engine.test_claim_sources import LIMITS as SOURCE_LIMITS
from tests.engine.test_grant_history import LIMITS as HISTORY_LIMITS

WORKER_BYTES = 65_536
UNCERTAINTY = timedelta(microseconds=17)


@dataclass(frozen=True)
class ClaimCase:
    binding: ClaimSourceBinding
    pin: GraphVersionPin
    query: ClaimQuery
    principal: Principal
    registration: Registration
    clock: FakeClock
    identifiers: IdentifierGenerator

    async def context(self, writer: AsyncConnection) -> ExpiryContext:
        locks = TransitionLocks(writer)
        await locks.acquire_run(self.binding.run.run_id)
        return ExpiryContext(
            writer,
            locks,
            GraphResourceScope(
                self.binding.run.namespace_id,
                self.binding.run.graph_id,
                self.binding.run.version_id,
            ),
            self.binding.run.run_id,
            self.clock.now,
            self.identifiers,
            READ_LIMITS,
            SOURCE_LIMITS.attempt_row,
        )

    async def prepare(self, database: Database) -> SelectedClaim:
        snapshot = prepare_claim_snapshot(
            await acquire(database, self.binding), self.pin, SELECTION, SNAPSHOT_LIMITS
        )
        async with database.read_transaction() as reader:
            row = (
                await reader.execute(
                    select(attempt.c.execution_domain_id, attempt.c.deadline).where(
                        attempt.c.id == self.binding.attempt_id
                    )
                )
            ).one()
        domain, deadline = cast(tuple[str | None, datetime | None], tuple(row))
        history = None
        if domain is not None:
            history = prepare_history(
                await acquire_history(
                    database,
                    HistoryScope(
                        self.binding.run.namespace_id,
                        self.binding.run.run_id,
                        self.binding.node_id,
                        self.binding.attempt_id,
                        domain,
                    ),
                    HISTORY_LIMITS,
                    deadline=asyncio.get_running_loop().time() + 10,
                )
            )
        return prepare_selected_claim(snapshot, history, deadline)

    async def apply(
        self, writer: AsyncConnection, prepared: SelectedClaim | None
    ) -> ClaimDecision:
        return await claim_attempt(
            await self.context(writer),
            self.binding.attempt_id,
            self.query,
            self.principal,
            prepared,
            SOURCE_LIMITS,
            HISTORY_LIMITS,
            WORKER_BYTES,
            UNCERTAINTY,
        )

    async def claim(self, database: Database) -> ClaimDecision:
        prepared = await self.prepare(database)
        async with database.transaction() as writer:
            result = await self.apply(writer, prepared)
        return result


async def wait_for_claim_waiter(database: Database) -> None:
    if database.dialect is Dialect.SQLITE:
        await until(lambda: database.units_in_flight() == 2)
        return
    async with asyncio.timeout(10):
        while True:
            async with database.read_transaction() as reader:
                if await reader.scalar(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_locks "
                        "JOIN pg_stat_activity USING (pid) "
                        "WHERE NOT granted AND datname = current_database())"
                    )
                ):
                    return


async def claim_case(
    database: Database, evaluator: Evaluator, ir: JsonObject | None = None
) -> ClaimCase:
    binding, pin = await seed(database, evaluator, ir=ir)
    async with database.transaction() as writer:
        seeder = Seeder(writer)
        token_id = await seeder.token("c" * 64, kind="worker", owner="claim-worker")
        principal = Principal(
            token_id=token_id,
            kind="service",
            owner="claim-worker",
            namespaces=(
                NamespaceGrant(
                    namespace_id=binding.run.namespace_id, transcripts=False
                ),
            ),
            approve=False,
            platform=False,
            runtime="local",
        )
        await writer.execute(
            update(api_token)
            .where(api_token.c.id == token_id)
            .values(
                namespaces=[entry.model_dump() for entry in principal.namespaces],
                runtime="local",
                approve=False,
            )
        )
        registration = Registration(
            principal.owner,
            "original-incarnation",
            "local",
            "original-domain",
            "0.1.0",
            1,
            ProcessIdentity(
                host_id="host",
                boot_id="boot",
                pid_namespace=None,
                pid=123,
                start_time=456,
                leads_group=True,
            ),
        )
        await register_worker(
            writer,
            registration,
            principal,
            portable=False,
            now=START,
            event_id=seeder.new_id(),
        )
        await writer.execute(
            update(run)
            .where(run.c.id == binding.run.run_id)
            .values(status="running", started_at=START)
        )
        await writer.execute(
            update(node_instance)
            .where(node_instance.c.id == binding.node_id)
            .values(status="queued", current_attempt_id=binding.attempt_id)
        )
        await writer.execute(
            update(attempt)
            .where(attempt.c.id == binding.attempt_id)
            .values(status="created", execution_state="not_started")
        )
    return ClaimCase(
        binding,
        pin,
        ClaimQuery(
            worker_id=registration.name,
            runtime=registration.runtime,
            incarnation_id=registration.incarnation_id,
            execution_domain_id=registration.execution_domain_id,
            claim_id="claim-1",
        ),
        principal,
        registration,
        FakeClock(),
        seeder.generator,
    )


async def another_run(database: Database, case: ClaimCase) -> ClaimCase:
    async with database.transaction() as writer:
        seeder = Seeder(writer)
        original = dict(
            (
                await writer.execute(
                    select(attempt).where(attempt.c.id == case.binding.attempt_id)
                )
            )
            .mappings()
            .one()
        )
        values = seeder.run_values(
            case.binding.run.namespace_id,
            case.binding.run.graph_id,
            case.binding.run.version_id,
        )
        values.update(
            status="running",
            started_at=START,
            input_encoding_version=1,
            params=INPUT_SENTINEL,
            cel_params_projection=INPUT_SENTINEL,
        )
        await writer.execute(insert(run).values(**values))
        run_id = str(values["id"])
        await writer.execute(
            insert(run_inputs).values(run_id=run_id, params=b"{}", cel_projection=b"{}")
        )
        node_id, attempt_id = (
            await seeder.node(run_id, case.binding.node_path),
            seeder.new_id(),
        )
        binding = replace(
            case.binding,
            run=replace(case.binding.run, run_id=run_id),
            node_id=node_id,
            attempt_id=attempt_id,
        )
        original.update(id=attempt_id, node_instance_id=node_id)
        for field in (*SOURCE_FIELDS, "acknowledged_segment_offset"):
            del original[field]
        locks = TransitionLocks(writer)
        await locks.acquire_run(run_id)
        sources = source_values()
        sources["input_bindings"] = prepare_attempt_inputs({}, {}).storage
        await insert_claim_attempt(
            writer,
            locks,
            binding,
            original,
            prepare_claim_sources(sources, {name: ALLOWANCE for name in sources}),
            SOURCE_LIMITS,
            START,
        )
        await writer.execute(
            update(node_instance)
            .where(node_instance.c.id == node_id)
            .values(kind="agent", status="queued", current_attempt_id=attempt_id)
        )
    return replace(
        case,
        binding=binding,
        query=case.query.model_copy(update={"claim_id": "claim-2"}),
    )


async def state(database: Database) -> dict[str, list[dict[str, object]]]:
    async with database.read_transaction() as reader:
        result: dict[str, list[dict[str, object]]] = {}
        for table in (
            worker,
            run,
            node_instance,
            attempt,
            attempt_claim_read_admission,
            attempt_dispatcher,
            event,
        ):
            with await reader.execute(
                select(table).order_by(*table.primary_key.columns)
            ) as rows:
                result[table.name] = [dict(row) for row in rows.mappings()]
        return result
