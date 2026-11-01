from dataclasses import dataclass

from sqlalchemy import insert, select

from kora.db.connection import Database
from kora.db.tables import event, idempotency_record, inbound_event, node_instance, run
from kora.engine.start_records import (
    AcceptedStart,
    ManualStartIdentity,
    SkippedStart,
    StartReceipt,
    prepare_manual_start,
    start_receipt_document,
)
from kora.engine.start_replay import StartReplay, replay_manual_start
from kora.protocols.authentication import NamespaceGrant, Principal
from tests.db.seed import Seeder

PRIVATE: str = "manual-start-private-parameter"
KEY: str = "manual-start-private-key"


@dataclass(frozen=True)
class ReplayScenario:
    database: Database
    identity: ManualStartIdentity
    receipt: StartReceipt
    principal: Principal
    receipt_id: str
    run_id: str | None
    version_id: str

    async def read(self) -> StartReplay:
        async with self.database.read_transaction() as connection:
            return await replay_manual_start(
                connection, self.principal, self.identity, KEY
            )

    async def snapshot(self) -> dict[str, list[dict[str, object]]]:
        snapshot: dict[str, list[dict[str, object]]] = {}
        async with self.database.read_transaction() as connection:
            for table in (inbound_event, run, node_instance, event, idempotency_record):
                result = await connection.execute(select(table))
                try:
                    snapshot[table.name] = [dict(row) for row in result.mappings()]
                finally:
                    result.close()
        return snapshot


async def replay_scenario(database: Database, skipped: bool = False) -> ReplayScenario:
    async with database.transaction() as connection:
        seed = Seeder(connection)
        namespace_id = await seed.namespace()
        graph_id = await seed.graph(namespace_id)
        version_id = await seed.graph_version(graph_id)
        run_id = (
            None
            if skipped
            else await seed.run(namespace_id, graph_id, version_id, "pending")
        )
        token_id = await seed.token("a" * 64, owner=seed.new_id())
        principal = Principal(
            token_id=token_id,
            kind="user",
            owner=seed.new_id(),
            namespaces=(NamespaceGrant(namespace_id=namespace_id, transcripts=False),),
            approve=False,
            platform=False,
            runtime=None,
        )
        identity = prepare_manual_start(namespace_id, graph_id, {"private": PRIVATE})
        response = (
            SkippedStart(outcome="skipped", reason="overlap", run_id=None)
            if run_id is None
            else AcceptedStart(
                outcome="accepted",
                run_id=run_id,
                graph_version_id=version_id,
                status="pending",
            )
        )
        receipt = StartReceipt(
            version=1,
            kind="manual",
            namespace_id=namespace_id,
            graph_id=graph_id,
            request_digest=identity.request_digest,
            response=response,
        )
        receipt_id = seed.new_id()
        await connection.execute(
            insert(inbound_event).values(
                id=receipt_id,
                namespace_id=namespace_id,
                name=f"kora.start:{graph_id}",
                idempotency_key=KEY,
                payload={"private": PRIVATE},
                auth_kind="bearer",
                nonce=None,
                received_at=seed.clock.now(),
                consumed_by=[],
                start_outcome=start_receipt_document(receipt),
                **seed.stamps(),
            )
        )
        await connection.execute(
            insert(idempotency_record).values(
                token_id=token_id,
                key=KEY,
                request_digest=identity.request_digest,
                response=response.model_dump(mode="json"),
                expires_at=seed.clock.now(),
                **seed.stamps(),
            )
        )
    return ReplayScenario(
        database, identity, receipt, principal, receipt_id, run_id, version_id
    )
