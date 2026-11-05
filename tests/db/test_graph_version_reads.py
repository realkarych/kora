import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Literal

import pytest
from fastapi.encoders import jsonable_encoder
from sqlalchemy import LargeBinary, Text, literal, update
from sqlalchemy import cast as sql_cast

from kora.db.connection import Database
from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected
from kora.db.graph_version_reads import (
    AcquiredGraphVersion,
    acquire_graph_version,
    graph_version_transport_statement,
)
from kora.db.migrations import upgrade_database
from kora.db.reads import graph_version_by_number
from kora.db.tables import graph_version
from kora.engine.graph_version_identity import (
    GraphVersionIdentityLimits,
    GraphVersionPin,
    prepare_graph_version_identity,
    read_graph_version_identity,
)
from tests.db.closing import ConnectionLedger
from tests.db.seed import Seeder
from tests.engine.test_graph_version_identity import LIMITS, insert_version, sample
from tests.support.databases import DatabaseKind, sqlite_settings

MARKER = "bounded-graph-transport"
SQLITE_ENCODINGS = ("UTF-16le", "UTF-16be")


def outer_json(text: str) -> bytes:
    return json.dumps(text, ensure_ascii=False, separators=(",", ":")).encode("ascii")


@dataclass(frozen=True)
class InternalTransport:
    value: AcquiredGraphVersion


@pytest.mark.asyncio
async def test_bounded_graph_version_acquisition_keeps_exact_pair_and_scope(
    database: Database,
) -> None:
    prepared = sample()
    first = outer_json(prepared.values.ir)
    second = outer_json(prepared.values.execution_contract)
    inner_size = len(prepared.values.ir) + len(prepared.values.execution_contract)
    below_outer = (len(first) + len(second) - 5) // 2
    oversized = prepare_graph_version_identity(
        {"large": "x" * 65_536}, {"fact": MARKER}, limits=LIMITS
    )
    ledger = ConnectionLedger(database)

    async with database.transaction() as connection:
        seeder = Seeder(connection)
        namespace = await seeder.namespace()
        foreign = await seeder.namespace("foreign")
        graph = await seeder.graph(namespace)
        other = await seeder.graph(namespace, "other")
        version = await insert_version(seeder, graph, 1, prepared)
        other_version = await insert_version(seeder, graph, 2, prepared)
        large_version = await insert_version(seeder, graph, 3, oversized)
        missing = seeder.new_id()
    async with database.read_transaction() as connection:
        selected = await acquire_graph_version(
            connection, namespace, graph, version, max_encoded_bytes=inner_size
        )
        hidden = await acquire_graph_version(
            connection, foreign, graph, version, max_encoded_bytes=inner_size
        )
        wrong_graph = await acquire_graph_version(
            connection, namespace, other, version, max_encoded_bytes=inner_size
        )
        missing_version = await acquire_graph_version(
            connection, namespace, graph, missing, max_encoded_bytes=inner_size
        )
        denied = (
            await connection.execute(
                graph_version_transport_statement(
                    connection, namespace, graph, version, max_encoded_bytes=below_outer
                )
            )
        ).one()
        oversized_raw = (
            await connection.execute(
                graph_version_transport_statement(
                    connection,
                    namespace,
                    graph,
                    large_version,
                    max_encoded_bytes=inner_size,
                )
            )
        ).one()
        exact_outer = await acquire_graph_version(
            connection, namespace, graph, version, max_encoded_bytes=below_outer + 1
        )
        detached_below_inner = await acquire_graph_version(
            connection, namespace, graph, version, max_encoded_bytes=inner_size - 1
        )
        large_cap = await acquire_graph_version(
            connection, namespace, graph, version, max_encoded_bytes=1 << 80
        )
    assert selected is not None
    assert detached_below_inner is not None
    assert large_cap is not None
    assert exact_outer is not None
    assert ledger.outstanding == database.units_in_flight() == 0
    pin = GraphVersionPin(graph, version, prepared.digests.execution_contract_digest)
    checked = read_graph_version_identity(
        selected, pin, limits=replace(LIMITS, max_encoded_bytes=inner_size)
    )
    with pytest.raises(GraphValueRejected) as below:
        read_graph_version_identity(
            detached_below_inner,
            pin,
            limits=replace(LIMITS, max_encoded_bytes=inner_size - 1),
        )
    with pytest.raises(GraphValueRejected) as wrong_version:
        read_graph_version_identity(
            selected, replace(pin, version_id=other_version), limits=LIMITS
        )
    with pytest.raises(GraphValueRejected) as wrong_pin:
        read_graph_version_identity(
            selected,
            replace(pin, execution_contract_digest=prepared.digests.ir_digest),
            limits=LIMITS,
        )
    await database.close()

    assert (
        selected.payloads
        == large_cap.payloads
        == exact_outer.payloads
        == (first, second)
    )
    assert hidden is wrong_graph is missing_version is None
    assert tuple(denied) == (
        GraphValueFailure.BUDGET.value,
        selected.encoding,
        None,
        None,
        None,
        None,
        None,
    )
    assert tuple(oversized_raw) == tuple(denied)
    assert checked.pin == pin
    assert checked.canonical_ir == prepared.values.ir
    assert checked.values.execution_contract["fact"] == "protected-graph-version"
    assert below.value.reason is GraphValueFailure.BUDGET
    assert wrong_version.value.reason is GraphValueFailure.INVALID
    assert wrong_pin.value.reason is GraphValueFailure.INVALID
    assert ledger.opened == ledger.closed > 0
    assert ledger.outstanding == ledger.closed_while_checked_out == 0


@pytest.mark.asyncio
async def test_bounded_graph_version_omits_unusable_transport_and_preserves_history(
    database: Database,
    database_kind: DatabaseKind,
) -> None:
    prepared = sample()

    async with database.transaction() as connection:
        seeder = Seeder(connection)
        namespace = await seeder.namespace()
        graph = await seeder.graph(namespace)
        legacy = await insert_version(seeder, graph, 1, prepared)
        invalid = await insert_version(seeder, graph, 2, prepared)
        await connection.execute(
            update(graph_version)
            .where(graph_version.c.id == legacy)
            .values(value_encoding_version=None, ir={"historical": True})
        )
        await connection.execute(
            update(graph_version)
            .where(graph_version.c.id == invalid)
            .values(
                ir_digest="a" * 65 if database_kind is DatabaseKind.SQLITE else "é" * 64
            )
        )
    async with database.read_transaction() as connection:
        history = await graph_version_by_number(connection, namespace, graph, 1)
        rejected: list[tuple[object, ...]] = []
        for version in (legacy, invalid):
            statement = graph_version_transport_statement(
                connection,
                namespace,
                graph,
                version,
                max_encoded_bytes=LIMITS.max_encoded_bytes,
            )
            rejected.append(tuple((await connection.execute(statement)).one()))
        try:
            raise LookupError(MARKER)
        except LookupError:
            with pytest.raises(GraphValueRejected) as invalid_read:
                await acquire_graph_version(
                    connection,
                    namespace,
                    graph,
                    invalid,
                    max_encoded_bytes=LIMITS.max_encoded_bytes,
                )
    error = invalid_read.value

    assert history is not None
    assert history.ir == {"historical": True}
    assert history.value_encoding_version is None
    assert rejected[0][0] == GraphValueFailure.VERSION.value
    assert rejected[1][0] == GraphValueFailure.INVALID.value
    assert all(record[2:] == (None,) * 5 for record in rejected)
    assert MARKER not in str(error)
    assert error.__context__ is error.__cause__ is None


def malformed_outer_values() -> tuple[bytes, ...]:
    escape = bytes([92])
    return (
        b'"{}"' + bytes([0]) + b"suffix",
        b'"{}' + escape + b'u0000suffix"',
        b'"' + escape + b'ud800"',
        b'"{}' + bytes([255]) + b'"',
        b'"{}"' + bytes([11]),
    )


@pytest.mark.asyncio
async def test_bounded_graph_version_refuses_corrupt_selected_values_after_release(
    database: Database, database_kind: DatabaseKind
) -> None:
    prepared = prepare_graph_version_identity({}, {"fact": MARKER}, limits=LIMITS)
    nonascii_inner = "é" * 64
    apparent_width = (
        len(nonascii_inner) + 2 + len(outer_json(prepared.values.execution_contract))
    )
    nonascii_bound = (apparent_width - 3) // 2
    ledger = ConnectionLedger(database)

    async with database.transaction() as connection:
        seeder = Seeder(connection)
        namespace = await seeder.namespace()
        graph = await seeder.graph(namespace)
        malformed: list[str] = []
        if database_kind is DatabaseKind.SQLITE:
            for index, payload in enumerate(malformed_outer_values(), start=1):
                version = await insert_version(seeder, graph, index, prepared)
                await connection.execute(
                    update(graph_version)
                    .where(graph_version.c.id == version)
                    .values(ir=sql_cast(literal(payload, type_=LargeBinary()), Text()))
                )
                malformed.append(version)
        else:
            for index, inner in enumerate(("{}suffix", "é"), start=1):
                version = await insert_version(seeder, graph, index, prepared)
                await connection.execute(
                    update(graph_version)
                    .where(graph_version.c.id == version)
                    .values(ir=inner)
                )
                malformed.append(version)
        wrong_type = await insert_version(seeder, graph, 10, prepared)
        wrong_ir: object = (
            literal(b'"{}"', type_=LargeBinary())
            if database_kind is DatabaseKind.SQLITE
            else {}
        )
        await connection.execute(
            update(graph_version)
            .where(graph_version.c.id == wrong_type)
            .values(ir=wrong_ir)
        )
        equivalent = await insert_version(seeder, graph, 11, prepared)
        if database_kind is DatabaseKind.SQLITE:
            escape = bytes([92])
            alternate_outer = b' \t"' + escape + b"u007b" + escape + b'u007d"\r\n'
            await connection.execute(
                update(graph_version)
                .where(graph_version.c.id == equivalent)
                .values(
                    ir=sql_cast(literal(alternate_outer, type_=LargeBinary()), Text())
                )
            )
        nonascii = await insert_version(seeder, graph, 12, prepared)
        await connection.execute(
            update(graph_version)
            .where(graph_version.c.id == nonascii)
            .values(ir=nonascii_inner)
        )
    async with database.read_transaction() as connection:
        selected = [
            await acquire_graph_version(
                connection,
                namespace,
                graph,
                version,
                max_encoded_bytes=LIMITS.max_encoded_bytes,
            )
            for version in malformed
        ]
        invalid_shape = (
            await connection.execute(
                graph_version_transport_statement(
                    connection,
                    namespace,
                    graph,
                    wrong_type,
                    max_encoded_bytes=LIMITS.max_encoded_bytes,
                )
            )
        ).one()
        same = await acquire_graph_version(
            connection,
            namespace,
            graph,
            equivalent,
            max_encoded_bytes=LIMITS.max_encoded_bytes,
        )
        denied_nonascii = (
            await connection.execute(
                graph_version_transport_statement(
                    connection,
                    namespace,
                    graph,
                    nonascii,
                    max_encoded_bytes=nonascii_bound,
                )
            )
        ).one()
    assert ledger.outstanding == database.units_in_flight() == 0
    assert same is not None
    checked = read_graph_version_identity(
        same,
        GraphVersionPin(graph, equivalent, prepared.digests.execution_contract_digest),
        limits=LIMITS,
    )
    failures: list[GraphValueRejected] = []
    for version, candidate in zip(malformed, selected, strict=True):
        assert candidate is not None
        pin = GraphVersionPin(
            graph, version, prepared.digests.execution_contract_digest
        )
        with pytest.raises(GraphValueRejected) as refusal:
            read_graph_version_identity(candidate, pin, limits=LIMITS)
        failures.append(refusal.value)
    last = selected[-1]
    assert last is not None
    opaque = InternalTransport(last)
    with pytest.raises(
        TypeError, match="graph version transport requires explicit access"
    ):
        asdict(opaque)
    with pytest.raises(ValueError, match="not iterable"):
        jsonable_encoder(last)
    await database.close()

    assert invalid_shape[0] == GraphValueFailure.INVALID.value
    assert tuple(invalid_shape)[2:] == (None,) * 5
    assert denied_nonascii[0] == GraphValueFailure.BUDGET.value
    assert tuple(denied_nonascii)[2:] == (None,) * 5
    assert checked.canonical_ir == "{}"
    assert all(error.reason is GraphValueFailure.INVALID for error in failures)
    assert all(error.__context__ is error.__cause__ is None for error in failures)
    assert MARKER not in repr(opaque)
    assert ledger.opened == ledger.closed > 0
    assert ledger.outstanding == ledger.closed_while_checked_out == 0


async def verify_transport_encoding(
    database: Database, expected: Literal["ascii", "utf-16le", "utf-16be"]
) -> None:
    prepared = sample()
    ledger = ConnectionLedger(database)
    inner_size = len(prepared.values.ir) + len(prepared.values.execution_contract)

    async with database.transaction() as connection:
        seeder = Seeder(connection)
        namespace = await seeder.namespace()
        graph = await seeder.graph(namespace)
        version = await insert_version(seeder, graph, 1, prepared)
    async with database.read_transaction() as connection:
        result = await acquire_graph_version(
            connection, namespace, graph, version, max_encoded_bytes=inner_size
        )
    assert result is not None
    assert ledger.outstanding == database.units_in_flight() == 0
    checked = read_graph_version_identity(
        result,
        GraphVersionPin(graph, version, prepared.digests.execution_contract_digest),
        limits=GraphVersionIdentityLimits(inner_size, 2 * inner_size + 29),
    )
    await database.close()

    assert result.encoding == expected
    assert result.payloads[0].decode(expected) == outer_json(prepared.values.ir).decode(
        "ascii"
    )
    assert checked.canonical_ir == prepared.values.ir
    assert ledger.opened == ledger.closed > 0


@pytest.mark.asyncio
async def test_bounded_graph_version_acquisition_preserves_native_database_encodings(
    database: Database, database_kind: DatabaseKind, tmp_path: Path
) -> None:
    if database_kind is DatabaseKind.POSTGRESQL:
        await verify_transport_encoding(database, "ascii")
        return
    for encoding in SQLITE_ENCODINGS:
        path = tmp_path / (encoding + ".db")
        with closing(sqlite3.connect(path)) as connection:
            connection.execute(
                "PRAGMA encoding = 'UTF-16le'"
                if encoding == "UTF-16le"
                else "PRAGMA encoding = 'UTF-16be'"
            )
            connection.execute("CREATE TABLE encoding_seed (value INTEGER)")
            connection.execute("DROP TABLE encoding_seed")
            connection.commit()
        settings = sqlite_settings(path)
        await upgrade_database(settings)
        encoded = Database(settings)
        try:
            await verify_transport_encoding(
                encoded, "utf-16le" if encoding == "UTF-16le" else "utf-16be"
            )
        finally:
            await encoded.close()
