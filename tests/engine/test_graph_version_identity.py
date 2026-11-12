import hashlib
from dataclasses import asdict, dataclass, replace

import pytest
from fastapi.encoders import jsonable_encoder

from kora.db.connection import Database
from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected
from kora.db.graph_values import EncodedGraphValues
from kora.db.reads import GraphVersionRow, graph_version_by_number
from kora.engine.graph_version_identity import (
    CheckedGraphVersionIdentity,
    GraphVersionDigests,
    GraphVersionIdentityLimits,
    GraphVersionPin,
    PreparedGraphVersionIdentity,
    prepare_graph_version_identity,
    read_graph_version_identity,
)
from kora.ir.json import JsonObject
from tests.db.closing import ConnectionLedger
from tests.db.seed import DIGEST, START, Seeder

LIMITS = GraphVersionIdentityLimits(100_000, 200_029)
VECTOR = GraphVersionDigests(
    "30d61e17510359a836fcd9dbb5d11b0e101a7c152fc9eb761c0bbfab53607478",
    "e6f28c3c198a6e186af48698dd59221b64186ada51dd88edd128eaf35ae69343",
    "abecbcca81e8d2c1175b43998dcb20c86cb39431868f392691ad454686298b10",
)
MARKER = "protected-graph-version"


def sample() -> PreparedGraphVersionIdentity:
    return prepare_graph_version_identity(
        {"z": MARKER + chr(0) + "é", "a": -0.0},
        {"semantics": "kora.execution.v1", "fact": MARKER},
        limits=LIMITS,
    )


def detached_row(prepared: PreparedGraphVersionIdentity) -> GraphVersionRow:
    pair = prepared.values
    digests = prepared.digests
    return GraphVersionRow(
        id="version",
        graph_id="graph",
        number=1,
        ir=pair.ir,
        ir_digest=digests.ir_digest,
        source_digest=DIGEST,
        execution_contract=pair.execution_contract,
        value_encoding_version=pair.value_encoding_version,
        execution_contract_digest=digests.execution_contract_digest,
        deployment_digest=digests.deployment_digest,
        resolved_digests={},
        resources={},
        deployed_by="owner",
        deployed_at=START,
        status="active",
    )


def pin_for(row: GraphVersionRow) -> GraphVersionPin:
    return GraphVersionPin(row.graph_id, row.id, row.execution_contract_digest)


def expected_digests(pair: EncodedGraphValues) -> GraphVersionDigests:
    ir = pair.ir.encode("ascii")
    contract = pair.execution_contract.encode("ascii")
    envelope = b'{"ir":' + ir + b',"execution_contract":' + contract + b"}"
    return GraphVersionDigests(
        hashlib.sha256(ir).hexdigest(),
        hashlib.sha256(contract).hexdigest(),
        hashlib.sha256(envelope).hexdigest(),
    )


def test_graph_version_digests_match_the_approved_exact_inputs() -> None:
    ir: JsonObject = {"m": "A", "n": -0.0}
    contract: JsonObject = {"semantics": "kora.execution.v1"}
    exact = GraphVersionIdentityLimits(51, 131)

    prepared = prepare_graph_version_identity(ir, contract, limits=exact)
    with pytest.raises(GraphValueRejected) as short_hash:
        prepare_graph_version_identity(
            ir, contract, limits=replace(exact, max_hash_bytes=130)
        )
    with pytest.raises(GraphValueRejected) as short_pair:
        prepare_graph_version_identity(
            ir, contract, limits=replace(exact, max_encoded_bytes=50)
        )
    row = detached_row(prepared)
    checked = read_graph_version_identity(row, pin_for(row), limits=exact)
    with pytest.raises(GraphValueRejected) as short_read:
        read_graph_version_identity(
            row, pin_for(row), limits=replace(exact, max_hash_bytes=130)
        )
    with pytest.raises(GraphValueRejected) as short_read_pair:
        read_graph_version_identity(
            row, pin_for(row), limits=replace(exact, max_encoded_bytes=50)
        )

    assert prepared.values.ir == '{"m":"A","n":-0.0}'
    assert prepared.values.execution_contract == '{"semantics":"kora.execution.v1"}'
    assert prepared.digests == VECTOR
    assert checked.canonical_ir == prepared.values.ir
    assert short_hash.value.reason is GraphValueFailure.BUDGET
    assert short_pair.value.reason is GraphValueFailure.BUDGET
    assert short_read.value.reason is GraphValueFailure.BUDGET
    assert short_read_pair.value.reason is GraphValueFailure.BUDGET


def test_graph_version_digest_streams_exact_large_canonical_texts() -> None:
    ir: JsonObject = {"z": 1, "a": (chr(0) + "é😀") * 600}
    contract: JsonObject = {"payload": "private" * 2200, "number": 1.0}

    prepared = prepare_graph_version_identity(ir, contract, limits=LIMITS)
    reordered = prepare_graph_version_identity(
        {"a": ir["a"], "z": 1}, contract, limits=LIMITS
    )
    integer = prepare_graph_version_identity(
        ir, {"payload": contract["payload"], "number": 1}, limits=LIMITS
    )

    assert len(prepared.values.ir) > 8192
    assert len(prepared.values.execution_contract) > 8192
    assert prepared.digests == expected_digests(prepared.values)
    assert reordered.digests.ir_digest != prepared.digests.ir_digest
    assert (
        reordered.digests.execution_contract_digest
        == prepared.digests.execution_contract_digest
    )
    assert integer.digests.ir_digest == prepared.digests.ir_digest
    assert (
        integer.digests.execution_contract_digest
        != prepared.digests.execution_contract_digest
    )
    assert (
        len({item.digests.deployment_digest for item in (prepared, reordered, integer)})
        == 3
    )


async def insert_version(
    seeder: Seeder, graph: str, number: int, prepared: PreparedGraphVersionIdentity
) -> str:
    values = prepared.values
    digests = prepared.digests
    return await seeder.graph_version(
        graph,
        number,
        values.ir,
        execution_contract=values.execution_contract,
        value_encoding_version=values.value_encoding_version,
        ir_digest=digests.ir_digest,
        execution_contract_digest=digests.execution_contract_digest,
        deployment_digest=digests.deployment_digest,
    )


@pytest.mark.asyncio
async def test_graph_version_identity_checks_scoped_rows_after_database_release(
    database: Database,
) -> None:
    prepared = sample()
    malformed_pair = replace(
        prepared.values, ir='{"private":"' + MARKER + chr(92) + 'q"}'
    )
    malformed = PreparedGraphVersionIdentity(
        malformed_pair, expected_digests(malformed_pair)
    )
    ledger = ConnectionLedger(database)

    async with database.transaction() as connection:
        seeder = Seeder(connection)
        namespace = await seeder.namespace()
        foreign = await seeder.namespace("foreign")
        graph = await seeder.graph(namespace)
        alternate = await seeder.graph(namespace, "alternate")
        version = await insert_version(seeder, graph, 1, prepared)
        other_version = await insert_version(seeder, graph, 2, prepared)
        other_graph = await insert_version(seeder, alternate, 1, prepared)
        malformed_version = await insert_version(seeder, graph, 3, malformed)
    async with database.read_transaction() as connection:
        selected = await graph_version_by_number(connection, namespace, graph, 1)
        previous = await graph_version_by_number(connection, namespace, graph, 2)
        different = await graph_version_by_number(connection, namespace, alternate, 1)
        bad = await graph_version_by_number(connection, namespace, graph, 3)
        hidden = await graph_version_by_number(connection, foreign, graph, 1)
    assert selected is not None
    assert previous is not None
    assert different is not None
    assert bad is not None
    pin = GraphVersionPin(graph, version, prepared.digests.execution_contract_digest)
    assert ledger.outstanding == database.units_in_flight() == 0
    checked = read_graph_version_identity(selected, pin, limits=LIMITS)
    with pytest.raises(GraphValueRejected) as changed_version:
        read_graph_version_identity(previous, pin, limits=LIMITS)
    with pytest.raises(GraphValueRejected) as changed_graph:
        read_graph_version_identity(
            different, replace(pin, version_id=other_graph), limits=LIMITS
        )
    assert ledger.outstanding == database.units_in_flight() == 0
    with pytest.raises(GraphValueRejected) as malformed_refusal:
        read_graph_version_identity(
            bad, replace(pin, version_id=malformed_version), limits=LIMITS
        )
    await database.close()

    assert selected.id == version
    assert previous.id == other_version
    assert different.id == other_graph
    assert bad.ir == malformed_pair.ir
    assert bad.deployment_digest == malformed.digests.deployment_digest
    assert hidden is None
    assert checked.pin == pin
    assert checked.canonical_ir == prepared.values.ir
    assert checked.values.ir["z"] == MARKER + chr(0) + "é"
    assert checked.values.execution_contract["fact"] == MARKER
    assert changed_version.value.reason is GraphValueFailure.INVALID
    assert changed_graph.value.reason is GraphValueFailure.INVALID
    assert malformed_refusal.value.reason is GraphValueFailure.INVALID
    assert ledger.opened == ledger.closed > 0
    assert ledger.outstanding == ledger.closed_while_checked_out == 0


@pytest.mark.parametrize("field", ["ir", "contract", "deployment", "prior-pin"])
def test_graph_version_identity_checks_each_digest(field: str) -> None:
    row = detached_row(sample())
    pin = pin_for(row)
    if field == "ir":
        row = replace(row, ir_digest=DIGEST)
    elif field == "contract":
        row = replace(row, execution_contract_digest=DIGEST)
        pin = pin_for(row)
    elif field == "deployment":
        row = replace(row, deployment_digest=DIGEST)
    else:
        pin = replace(pin, execution_contract_digest=DIGEST)

    with pytest.raises(GraphValueRejected) as refusal:
        read_graph_version_identity(row, pin, limits=LIMITS)

    assert refusal.value.reason is GraphValueFailure.INVALID


@pytest.mark.parametrize("version", [None, 2])
def test_graph_version_identity_refuses_incompatible_encoding(
    version: int | None,
) -> None:
    row = replace(detached_row(sample()), value_encoding_version=version)
    pin = pin_for(row)

    with pytest.raises(GraphValueRejected) as refusal:
        read_graph_version_identity(row, pin, limits=LIMITS)

    assert refusal.value.reason is GraphValueFailure.VERSION


@dataclass(frozen=True)
class InternalValues:
    value: PreparedGraphVersionIdentity | CheckedGraphVersionIdentity


def test_graph_version_identity_discards_private_failure_and_serialization() -> None:
    prepared = sample()
    row = detached_row(prepared)
    checked = read_graph_version_identity(row, pin_for(row), limits=LIMITS)
    malformed = replace(row, ir='{"private":"' + MARKER + chr(92) + 'q"}')
    internal = (InternalValues(prepared), InternalValues(checked))

    for value in internal:
        with pytest.raises(
            TypeError, match="graph version identity requires explicit access"
        ):
            asdict(value)
        with pytest.raises(
            TypeError, match="graph version identity requires explicit access"
        ):
            jsonable_encoder(value)
        with pytest.raises(ValueError, match="not iterable"):
            jsonable_encoder(value.value)
    with pytest.raises(GraphValueRejected) as malformed_value:
        read_graph_version_identity(malformed, pin_for(row), limits=LIMITS)
    try:
        raise LookupError(MARKER)
    except LookupError:
        with pytest.raises(GraphValueRejected) as short_prepare:
            prepare_graph_version_identity(
                {"private": MARKER},
                {"fact": MARKER},
                limits=replace(LIMITS, max_hash_bytes=1),
            )
        with pytest.raises(GraphValueRejected) as refusal:
            read_graph_version_identity(malformed, pin_for(row), limits=LIMITS)
    private_by_entry = {
        "read_graph_version_identity": {"row", "pin"},
        "prepare_graph_version_identity": {"ir", "execution_contract", "pair"},
    }

    assert MARKER not in repr(internal)
    assert malformed_value.value.reason is GraphValueFailure.INVALID
    assert malformed_value.value.__context__ is malformed_value.value.__cause__ is None
    assert refusal.value.reason is GraphValueFailure.INVALID
    assert short_prepare.value.reason is GraphValueFailure.BUDGET
    for error in (refusal.value, short_prepare.value):
        assert MARKER not in str(error)
        assert error.__context__ is error.__cause__ is None
        traceback = error.__traceback__
        while traceback is not None:
            name = traceback.tb_frame.f_code.co_name
            assert name not in {"_checked_graph_version_identity", "_canonical_digests"}
            if name in private_by_entry:
                assert private_by_entry[name].isdisjoint(traceback.tb_frame.f_locals)
            traceback = traceback.tb_next
