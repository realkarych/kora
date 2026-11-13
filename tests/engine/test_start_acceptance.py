import asyncio
from typing import cast

import pytest
from pydantic import MISSING
from sqlalchemy import insert, select, update

from kora.api.graph_deployment_limits import IDENTITY
from kora.cel_eval.thread import Evaluator
from kora.db.connection import Database, Dialect
from kora.db.graph_value_json import GraphValueRejected
from kora.db.graph_version_reads import acquire_graph_version
from kora.db.identifiers import IdentifierGenerator
from kora.db.retention import resolve_retention_snapshot
from kora.db.tables import event, graph_version, inbound_event, namespace, run
from kora.engine.graph_execution_contract import check_detached_execution_contract
from kora.engine.graph_resource_references import GraphResourceScope
from kora.engine.locks import Scope, ScopeKind, TransitionLocks
from kora.engine.promotion_admission import record_bound
from kora.engine.run_materialization import prepare_run_materialization
from kora.engine.run_promotion import (
    PromotionOutcome,
    promote_pending_run,
    promotion_scopes,
)
from kora.engine.run_promotion_read import INPUT_SENTINEL
from kora.engine.start_acceptance import (
    AcceptanceOutcome,
    StartAcceptance,
    StartAcceptanceFailed,
    StartCandidate,
    accept_manual_start,
    acquire_manual_start,
    select_manual_start,
)
from kora.engine.start_facts import (
    InstallationRefusal,
    ManualStartFacts,
    SelectedStartVersion,
    StartFactsResult,
    StartRefusal,
    declares_agent_secrets,
    read_manual_start_facts,
)
from kora.engine.start_records import (
    AcceptedStart,
    SkippedStart,
    StartRecordRejected,
    decode_start_receipt,
)
from kora.ir.deployment_checks import DeploymentSelection
from kora.ir.document import GraphDocument
from kora.ir.json import JsonObject
from kora.protocols.authentication import Principal
from tests.api.deployment_cases import AT, OWNER, SELECTION, evaluator, skeleton_ir
from tests.db.closing import ConnectionLedger, until
from tests.db.seed import FakeClock, Seeder, fixed_randomness
from tests.engine.graph_output_cases import child
from tests.engine.lock_support import (
    DEADLINE,
    held_transaction,
    lock_inventory,
    release_and_join,
)
from tests.engine.promotion_support import READ_LIMITS
from tests.engine.start_cases import (
    KEY,
    DeployedGraph,
    agent_config,
    commit,
    deploy,
    facts_for,
    publish_version,
    redeploy,
    request_for,
    start,
    start_rows,
)
from tests.engine.test_graph_execution_contract import LIMITS

__all__ = ["evaluator"]

EMPTY_RESOURCES: JsonObject = {
    "interpretation": "kora.graph-resource-refs.v1",
    "commands": [],
}


def with_runtime(**settings: object) -> JsonObject:
    ir = skeleton_ir()
    child(ir, "runtime").update(cast("JsonObject", settings))
    return ir


def with_graph(**settings: object) -> JsonObject:
    ir = skeleton_ir()
    child(ir, "graph").update(cast("JsonObject", settings))
    return ir


def with_agent_secret() -> JsonObject:
    ir = skeleton_ir()
    ir["secrets"] = {"aliases": {"API_TOKEN": "secret://env/API_TOKEN"}}
    agent_config(ir)["secret_env"] = {"API_TOKEN": "API_TOKEN"}
    return ir


def accepted(result: object) -> AcceptedStart:
    assert isinstance(result, StartAcceptance)
    assert result.outcome is AcceptanceOutcome.ACCEPTED
    assert isinstance(result.response, AcceptedStart)
    return result.response


async def limit_backlog(database: Database, namespace_id: str, limit: int) -> None:
    async with database.transaction() as writer:
        await writer.execute(
            update(namespace)
            .where(namespace.c.id == namespace_id)
            .values(policy={"concurrency": {"max_pending_runs": limit}})
        )


async def promote(database: Database, case: DeployedGraph, run_id: str) -> str:
    scope = GraphResourceScope(case.namespace_id, case.graph_id, case.version_id)
    request = request_for(case)
    facts = await facts_for(database, case, request, "promotion-facts")
    assert isinstance(facts, ManualStartFacts)
    async with database.read_transaction() as reader:
        acquired = await acquire_graph_version(
            reader,
            case.namespace_id,
            case.graph_id,
            case.version_id,
            max_encoded_bytes=LIMITS.identity.max_encoded_bytes,
        )
    assert acquired is not None
    checked = check_detached_execution_contract(
        acquired, facts.pin, scope, EMPTY_RESOURCES, (), limits=LIMITS
    )
    materialization = prepare_run_materialization(
        checked, max_ir_bytes=len(checked.canonical_ir), max_top_level_nodes=3
    )
    async with database.transaction() as writer:
        locks = TransitionLocks(writer)
        await locks.acquire_run(run_id)
        await locks.acquire_scopes(promotion_scopes(scope))
        outcome = await promote_pending_run(
            writer,
            locks,
            scope,
            facts.pin,
            materialization,
            run_id,
            FakeClock(AT).now,
            IdentifierGenerator(FakeClock(AT).now),
            READ_LIMITS,
        )
    assert outcome is PromotionOutcome.APPLIED
    rows = await start_rows(database)
    return str(next(row for row in rows["run"] if row["id"] == run_id)["status"])


@pytest.mark.asyncio
async def test_manual_start_accepts_a_pending_run(
    database: Database, evaluator: Evaluator
) -> None:
    case = await deploy(
        database,
        evaluator,
        with_runtime(limits={"timeout": "2h"}, retention={"transcripts": "7d"}),
    )

    response = accepted(await start(database, case, allow_unenforced_secrets=True))
    rows = await start_rows(database)

    assert response.graph_version_id == case.version_id
    [saved] = rows["run"]
    [inputs] = rows["run_inputs"]
    [receipt] = rows["receipt"]
    [created] = rows["event"]
    [admission] = rows["admission"]
    trigger = {"kind": "manual", "overlap": "allow", "occurrence_key": receipt["id"]}
    assert {
        name: saved[name]
        for name in (
            "id",
            "graph_version_id",
            "idempotency_key",
            "status",
            "needs_scheduling",
            "trigger",
            "limits",
            "params",
            "cel_params_projection",
            "input_encoding_version",
            "allow_unenforced_secrets",
            "retention",
            "retention_version",
            "started_at",
            "target_key",
        )
    } == {
        "id": response.run_id,
        "graph_version_id": case.version_id,
        "idempotency_key": KEY,
        "status": "pending",
        "needs_scheduling": True,
        "trigger": trigger,
        "limits": {"timeout": "2h"},
        "params": INPUT_SENTINEL,
        "cel_params_projection": INPUT_SENTINEL,
        "input_encoding_version": 1,
        "allow_unenforced_secrets": True,
        "retention": resolve_retention_snapshot(
            {"transcripts": "7d"}, MISSING
        ).to_json(),
        "retention_version": 1,
        "started_at": None,
        "target_key": None,
    }
    assert (inputs["params"], inputs["cel_projection"]) == (b"{}", b"{}")
    assert receipt["name"] == f"kora.start:{case.graph_id}"
    assert receipt["idempotency_key"] == KEY
    assert decode_start_receipt(receipt["start_outcome"]).response == response
    assert (created["kind"], created["run_id"]) == ("run.created", response.run_id)
    assert created["actor"] == {
        "kind": "user",
        "owner": OWNER,
        "token_id": case.user.token_id,
    }
    assert created["payload"] == {
        "trigger": trigger,
        "graph_version": case.version_id,
        "rerun_of": None,
    }
    assert (
        admission["run_id"],
        admission["receipt_id"],
        admission["run_bytes"],
        admission["receipt_bytes"],
    ) == (
        response.run_id,
        receipt["id"],
        record_bound(saved, tuple(run.c.keys())),
        record_bound(receipt, tuple(inbound_event.c.keys())),
    )


@pytest.mark.asyncio
async def test_accepted_manual_start_is_promoted(
    database: Database, evaluator: Evaluator
) -> None:
    case = await deploy(database, evaluator)
    response = accepted(await start(database, case))

    status = await promote(database, case, response.run_id)

    assert status == "running"


@pytest.mark.asyncio
async def test_manual_start_replays_saved_outcomes(
    database: Database, evaluator: Evaluator
) -> None:
    case = await deploy(database, evaluator)
    first = accepted(await start(database, case))
    await redeploy(database, evaluator, case, "A newer prompt.")
    before = await start_rows(database)

    again = await start(database, case, seconds=5)
    changed = await start(database, case, allow_unenforced_secrets=True, seconds=6)
    after = await start_rows(database)

    assert again == StartAcceptance(AcceptanceOutcome.REPLAYED, first)
    assert changed == StartAcceptance(AcceptanceOutcome.MISMATCHED)
    assert after == before


@pytest.mark.asyncio
async def test_manual_start_skips_on_overlap(
    database: Database, evaluator: Evaluator
) -> None:
    case = await deploy(database, evaluator, with_graph(overlap="skip"))
    accepted(await start(database, case))

    skipped = await start(database, case, key="second", seconds=1)
    replayed = await start(database, case, key="second", seconds=2)
    rows = await start_rows(database)

    skip = SkippedStart(outcome="skipped", reason="overlap", run_id=None)
    assert skipped == StartAcceptance(AcceptanceOutcome.SKIPPED, skip)
    assert replayed == StartAcceptance(AcceptanceOutcome.REPLAYED, skip)
    assert (len(rows["run"]), len(rows["receipt"]), len(rows["admission"])) == (1, 2, 1)
    receipt = next(row for row in rows["receipt"] if row["idempotency_key"] == "second")
    timeline = next(row for row in rows["event"] if row["kind"] == "trigger.skipped")
    assert timeline["run_id"] is None
    assert timeline["payload"] == {
        "graph_id": case.graph_id,
        "occurrence_key": receipt["id"],
        "request_digest": request_for(case).identity.request_digest,
        "reason": "overlap",
    }


@pytest.mark.asyncio
async def test_manual_start_refuses_a_full_backlog(
    database: Database, evaluator: Evaluator
) -> None:
    case = await deploy(database, evaluator)
    await limit_backlog(database, case.namespace_id, 1)
    first = accepted(await start(database, case))
    before = await start_rows(database)

    full = await start(database, case, key="second", seconds=1)
    after_refusal = await start_rows(database)
    async with database.transaction() as writer:
        await writer.execute(
            update(run)
            .where(run.c.id == first.run_id)
            .values(status="succeeded", finished_at=AT)
        )
    retried = await start(database, case, key="second", seconds=2)

    assert full == StartAcceptance(AcceptanceOutcome.FULL, None, 1, 1)
    assert after_refusal == before
    accepted(retried)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ir", "params", "refusal"),
    [
        (None, {"extra": 1}, StartRefusal.PARAMETERS),
        (
            with_graph(params={"type": "object"}),
            None,
            StartRefusal.PARAMETERS_UNAVAILABLE,
        ),
        (
            with_graph(params={"type": "object"}),
            {"extra": 1},
            StartRefusal.PARAMETERS_UNAVAILABLE,
        ),
        (with_agent_secret(), None, StartRefusal.SECRETS),
    ],
    ids=["nonempty", "declared", "declared-nonempty", "agent-secret"],
)
async def test_manual_start_refuses_parameters_and_secrets(
    database: Database,
    evaluator: Evaluator,
    ir: JsonObject | None,
    params: JsonObject | None,
    refusal: StartRefusal,
) -> None:
    case = await deploy(database, evaluator, ir)

    result = await start(database, case, params, allow_unenforced_secrets=True)
    rows = await start_rows(database)

    assert result is refusal
    assert all(not table for table in rows.values())


@pytest.mark.asyncio
async def test_manual_start_rechecks_the_installation(
    database: Database, evaluator: Evaluator
) -> None:
    case = await deploy(database, evaluator)

    result = await start(database, case, selection=DeploymentSelection("claude"))

    assert isinstance(result, InstallationRefusal)
    assert [finding.code for finding in result.diagnostics.errors] == ["IR018"]
    assert all(not table for table in (await start_rows(database)).values())


@pytest.mark.asyncio
async def test_manual_start_detects_a_changed_latest_version(
    database: Database, evaluator: Evaluator
) -> None:
    case = await deploy(database, evaluator)
    request = request_for(case)
    facts = await facts_for(database, case, request)
    assert isinstance(facts, ManualStartFacts)
    newer = await redeploy(database, evaluator, case, "A concurrent deployment.")

    stale = await commit(database, case, request, facts)
    rows = await start_rows(database)
    retried = accepted(await start(database, case, seconds=1))

    assert stale == StartAcceptance(AcceptanceOutcome.STALE)
    assert all(not table for table in rows.values())
    assert retried.graph_version_id == newer


@pytest.mark.asyncio
async def test_manual_start_rolls_back_and_releases(
    database: Database, evaluator: Evaluator
) -> None:
    case = await deploy(database, evaluator)
    ledger = ConnectionLedger(database)
    request = request_for(case)
    facts = await facts_for(database, case, request)
    assert isinstance(facts, ManualStartFacts)
    clock = FakeClock(AT)
    probe = IdentifierGenerator(clock.now, fixed_randomness(7))
    probe.new()
    collision = probe.new()
    async with database.transaction() as writer:
        await writer.execute(
            insert(event).values(
                id=collision,
                namespace_id=case.namespace_id,
                run_id=None,
                kind="trigger.skipped",
                actor={},
                payload={},
                at=AT,
                **Seeder(writer).stamps(),
            )
        )
    before = await start_rows(database)

    with pytest.raises(StartAcceptanceFailed):
        await commit(
            database,
            case,
            request,
            facts,
            identifiers=IdentifierGenerator(clock.now, fixed_randomness(7)),
        )
    after = await start_rows(database)
    retried = accepted(await start(database, case, seconds=1))

    assert after == before
    assert retried.graph_version_id == case.version_id
    assert await lock_inventory(database) == []
    assert database.units_in_flight() == ledger.outstanding == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["service", "ungranted"])
async def test_manual_start_refuses_unauthorized_principals(
    database: Database, evaluator: Evaluator, variant: str
) -> None:
    case = await deploy(database, evaluator)
    grants = tuple(
        grant.model_copy(update={"transcripts": False})
        for grant in case.user.namespaces
    )
    change: dict[str, object] = (
        {
            "kind": "service",
            "approve": False,
            "platform": False,
            "runtime": "local",
            "namespaces": grants,
        }
        if variant == "service"
        else {"namespaces": ()}
    )
    intruder = Principal.model_validate(case.user.model_dump() | change)
    request = request_for(case)
    facts = await facts_for(database, case, request)
    assert isinstance(facts, ManualStartFacts)

    selected = await select_manual_start(database, intruder, request, KEY)
    committed = await commit(database, case, request, facts, principal=intruder)
    rows = await start_rows(database)

    assert selected == StartAcceptance(AcceptanceOutcome.REFUSED)
    assert committed == StartAcceptance(AcceptanceOutcome.REFUSED)
    assert all(not table for table in rows.values())


@pytest.mark.asyncio
async def test_manual_start_facts_bind_their_request(
    database: Database, evaluator: Evaluator
) -> None:
    case = await deploy(database, evaluator)
    facts = await facts_for(database, case, request_for(case))
    assert isinstance(facts, ManualStartFacts)

    newer = await redeploy(database, evaluator, case, "A newer prompt.")
    explicit = request_for(case, version=newer)
    candidate = await select_manual_start(database, case.user, explicit, KEY)
    assert isinstance(candidate, StartCandidate)
    selected = await acquire_manual_start(
        database, explicit, candidate, limits=IDENTITY
    )
    assert isinstance(selected, SelectedStartVersion)

    with pytest.raises(StartAcceptanceFailed):
        await commit(database, case, request_for(case, {"extra": 1}), facts)
    with pytest.raises(StartRecordRejected):
        await acquire_manual_start(
            database, request_for(case), candidate, limits=IDENTITY
        )
    with pytest.raises(TypeError):
        StartCandidate(object(), explicit, newer)
    with pytest.raises(GraphValueRejected):
        read_manual_start_facts(
            selected,
            request_for(case, version=case.version_id),
            SELECTION,
            limits=IDENTITY,
        )
    with pytest.raises(TypeError):
        ManualStartFacts(
            object(),
            request_for(case),
            facts.pin,
            GraphDocument.model_validate(skeleton_ir()),
        )
    rows = await start_rows(database)

    assert all(not table for table in rows.values())


@pytest.mark.asyncio
async def test_corrupted_stored_graph_never_executes(
    database: Database, evaluator: Evaluator
) -> None:
    case = await deploy(database, evaluator)
    async with database.transaction() as writer:
        stored = await writer.scalar(
            select(graph_version.c.ir).where(graph_version.c.id == case.version_id)
        )
        assert isinstance(stored, str)
        assert stored.count("repository") == 1
        await writer.execute(
            update(graph_version)
            .where(graph_version.c.id == case.version_id)
            .values(ir=stored.replace("repository", "repositorx"))
        )

    response = accepted(await start(database, case))
    with pytest.raises(GraphValueRejected):
        await promote(database, case, response.run_id)
    rows = await start_rows(database)

    [saved] = rows["run"]
    assert (saved["status"], saved["started_at"]) == ("pending", None)


@pytest.mark.parametrize(
    ("change", "declares"),
    [
        ({}, False),
        ({"secret_env": {"API_TOKEN": "API_TOKEN"}}, True),
        ({"mcp_servers": {"tools": {"type": "stdio", "command": "tools"}}}, False),
        (
            {
                "mcp_servers": {
                    "tools": {
                        "type": "stdio",
                        "command": "tools",
                        "secret_env": {"API_TOKEN": "API_TOKEN"},
                    }
                }
            },
            True,
        ),
    ],
    ids=["none", "agent", "server-without-secrets", "server"],
)
def test_agent_secret_declarations(change: JsonObject, declares: bool) -> None:
    ir = skeleton_ir()
    agent_config(ir).update(change)

    found = declares_agent_secrets(GraphDocument.model_validate(ir))

    assert found is declares


@pytest.mark.asyncio
async def test_manual_start_pins_an_older_explicit_version(
    database: Database, evaluator: Evaluator
) -> None:
    case = await deploy(database, evaluator, with_runtime(limits={"timeout": "2h"}))
    await publish_version(
        database, evaluator, case, with_runtime(limits={"timeout": "3h"})
    )

    response = accepted(await start(database, case, version=case.version_id))
    rows = await start_rows(database)

    [saved] = rows["run"]
    assert response.graph_version_id == case.version_id
    assert (saved["graph_version_id"], saved["limits"]) == (
        case.version_id,
        {"timeout": "2h"},
    )


@pytest.mark.asyncio
async def test_manual_start_refuses_an_inactive_version(
    database: Database, evaluator: Evaluator
) -> None:
    case = await deploy(database, evaluator)
    async with database.transaction() as writer:
        await writer.execute(
            update(graph_version)
            .where(graph_version.c.id == case.version_id)
            .values(status="retired")
        )

    result = await start(database, case)

    assert result == StartAcceptance(AcceptanceOutcome.UNAVAILABLE)
    assert all(not table for table in (await start_rows(database)).values())


async def wait_for_waiters(database: Database, count: int) -> None:
    if database.dialect is Dialect.SQLITE:
        await until(lambda: database.units_in_flight() == count + 1)
        return
    async with asyncio.timeout(DEADLINE):
        while (
            sum(not granted for _, _, granted in await lock_inventory(database)) < count
        ):
            pass


@pytest.mark.asyncio
async def test_manual_start_contending_duplicates_accept_once(
    database: Database, evaluator: Evaluator
) -> None:
    case = await deploy(database, evaluator)
    request = request_for(case)
    prepared = [await facts_for(database, case, request) for _ in range(2)]
    facts = [entry for entry in prepared if isinstance(entry, ManualStartFacts)]
    assert len(facts) == 2
    ready, release = asyncio.Event(), asyncio.Event()
    holder = asyncio.create_task(
        held_transaction(database, ready, release, (Scope(ScopeKind.GLOBAL),))
    )
    await asyncio.wait_for(ready.wait(), DEADLINE)

    starts = [
        asyncio.create_task(commit(database, case, request, entry, seconds=index))
        for index, entry in enumerate(facts)
    ]
    await wait_for_waiters(database, 2)
    await release_and_join(release, holder)
    results = await asyncio.gather(*starts)
    rows = await start_rows(database)

    assert sorted(result.outcome.value for result in results) == [
        "accepted",
        "replayed",
    ]
    assert results[0].response == results[1].response
    assert (len(rows["run"]), len(rows["receipt"]), len(rows["event"])) == (1, 1, 1)


@pytest.mark.asyncio
async def test_cancelled_manual_start_settles_its_unit(
    database: Database, evaluator: Evaluator
) -> None:
    case = await deploy(database, evaluator)
    ledger = ConnectionLedger(database)
    request = request_for(case)
    facts = await facts_for(database, case, request)
    assert isinstance(facts, ManualStartFacts)
    ready, release = asyncio.Event(), asyncio.Event()
    holder = asyncio.create_task(
        held_transaction(database, ready, release, (Scope(ScopeKind.GLOBAL),))
    )
    await asyncio.wait_for(ready.wait(), DEADLINE)
    task = asyncio.create_task(commit(database, case, request, facts))
    await wait_for_waiters(database, 1)

    task.cancel()
    await release_and_join(release, holder)
    with pytest.raises(asyncio.CancelledError):
        await task
    rows = await start_rows(database)
    replayed = await start(database, case, seconds=1)

    assert {name: len(table) for name, table in rows.items()} == {
        "run": 1,
        "run_inputs": 1,
        "receipt": 1,
        "event": 1,
        "admission": 1,
    }
    assert isinstance(replayed, StartAcceptance)
    assert replayed.outcome is AcceptanceOutcome.REPLAYED
    assert await lock_inventory(database) == []
    assert database.units_in_flight() == ledger.outstanding == 0


@pytest.mark.asyncio
async def test_manual_start_refuses_an_unavailable_saved_binding(
    database: Database, evaluator: Evaluator
) -> None:
    case = await deploy(database, evaluator)
    async with database.transaction() as writer:
        stored = await writer.scalar(
            select(graph_version.c.execution_contract).where(
                graph_version.c.id == case.version_id
            )
        )
        assert isinstance(stored, str)
        assert stored.count("kora.graph-parameters.v1") == 1
        await writer.execute(
            update(graph_version)
            .where(graph_version.c.id == case.version_id)
            .values(
                execution_contract=stored.replace(
                    "kora.graph-parameters.v1", "kora.graph-parameters.v9"
                )
            )
        )

    result = await start(database, case)

    assert result is StartRefusal.PARAMETERS_UNAVAILABLE
    assert all(not table for table in (await start_rows(database)).values())


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [1, 2])
async def test_accept_manual_start_prepares_again_once(
    database: Database, evaluator: Evaluator, changes: int
) -> None:
    case = await deploy(database, evaluator)
    request = request_for(case)
    published: list[str] = []

    async def prepare(
        candidate: StartCandidate,
    ) -> StartAcceptance | StartFactsResult | None:
        selected = await acquire_manual_start(
            database, request, candidate, limits=IDENTITY
        )
        assert isinstance(selected, SelectedStartVersion)
        facts = read_manual_start_facts(selected, request, SELECTION, limits=IDENTITY)
        if len(published) < changes:
            published.append(
                await redeploy(database, evaluator, case, f"Change {len(published)}.")
            )
        return facts

    clock = FakeClock(AT)
    result = await accept_manual_start(
        database,
        case.user,
        request,
        KEY,
        prepare,
        identifiers=IdentifierGenerator(clock.now),
        clock=clock.now,
    )
    rows = await start_rows(database)

    if changes == 1:
        assert accepted(result).graph_version_id == published[0]
        assert len(rows["run"]) == 1
    else:
        assert result == StartAcceptance(AcceptanceOutcome.STALE)
        assert all(not table for table in rows.values())
