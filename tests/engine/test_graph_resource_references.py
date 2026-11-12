import pickle
from dataclasses import replace
from pathlib import Path
from traceback import walk_tb
from typing import Literal, cast

import pytest
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select

from kora.db.connection import Database, Dialect
from kora.db.graph_resource_owner_reads import (
    AcquiredGraphResourceOwners,
    NativeOwnerRow,
)
from kora.db.graph_resource_transport import graph_resource_binary_projection
from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected, ObjectReader
from kora.db.identifiers import encode
from kora.db.migrations import upgrade_database
from kora.db.reads import graph_version_by_number
from kora.db.tables import graph_version
from kora.engine.graph_command_manifests import (
    CommandManifestLimits,
    build_declared_command_manifests,
)
from kora.engine.graph_resource_references import (
    CheckedNativeGraphResourceReferences,
    GraphResourceOwner,
    GraphResourceReferenceLimits,
    GraphResourceReferences,
    GraphResourceScope,
    NativeGraphResourceTransport,
    NativeOwnerState,
    NativeResourceEncoding,
    PreparedGraphResourceReferences,
    build_graph_resource_references,
    check_graph_resource_references,
)
from kora.engine.graph_version_identity import (
    GraphVersionIdentityLimits,
    GraphVersionPin,
    prepare_graph_version_identity,
    read_graph_version_identity,
)
from kora.ir.json import JsonObject, JsonValue
from kora.protocols.storage_models import ContentAddress, ContentRef, StorageIdentity
from tests.db.closing import ConnectionLedger
from tests.db.seed import Seeder
from tests.engine.graph_output_cases import child, output_document
from tests.engine.test_graph_command_manifests import (
    ZERO,
    authored_manifest,
    canonical,
    command_document,
    resource,
)
from tests.support.databases import encoded_sqlite_settings

LIMITS = GraphResourceReferenceLimits(
    CommandManifestLimits(100_000, 1_000_000, 2_000_000),
    2_000_000,
    4_000_000,
    8_000_000,
)
VERSION_LIMITS = GraphVersionIdentityLimits(100_000, 200_029)
SCOPE = GraphResourceScope("namespace", "graph", "version")
ROOT = ("graph", "nodes", "producer")
BODY = ("graph", "subgraphs", "body", "nodes", "producer")
INTERPRETATION = "kora.graph-resource-refs.v1"
MARKER = "private-content-reference-marker"


def content(
    store: str = "store",
    incarnation: str = "original",
    *,
    digest: str = ZERO,
    size: int = 0,
) -> ContentRef:
    return ContentRef(
        version=1,
        address=ContentAddress(
            version=1,
            storage_identity=StorageIdentity(version=1, store_id=store),
            payload_digest=digest,
        ),
        object_incarnation=incarnation,
        total_size=size,
    )


def expected_ref() -> JsonObject:
    return {
        "version": 1,
        "address": {
            "version": 1,
            "storage_identity": {"version": 1, "store_id": "store"},
            "payload_digest": ZERO,
        },
        "object_incarnation": "original",
        "total_size": 0,
    }


def declared(text: str) -> JsonObject:
    value = build_declared_command_manifests(text, limits=LIMITS.logical)
    assert len(value.text) <= LIMITS.source_bytes
    return ObjectReader(value.text).object()


def physical_file(value: JsonObject, *, command: int = 0, file: int = 0) -> JsonObject:
    entries = cast(list[JsonObject], value["commands"])
    return cast(list[JsonObject], entries[command]["resources"])[file]


def ordered_reverse(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        return {key: ordered_reverse(item) for key, item in reversed(value.items())}
    if isinstance(value, list):
        return [ordered_reverse(item) for item in value]
    return value


def single_inputs(
    scope: GraphResourceScope = SCOPE, *, name: str = "é.sh"
) -> tuple[str, JsonObject, tuple[GraphResourceOwner, ...]]:
    config = authored_manifest([resource(name)])
    text = canonical(command_document(config))
    owners = (GraphResourceOwner(scope, ROOT, name, content()),)
    return text, declared(text), owners


def assert_refused(error: GraphValueRejected, reason: GraphValueFailure) -> None:
    assert error.reason is reason
    assert error.args == (reason.value,)
    assert error.__cause__ is None
    assert error.__context__ is None
    assert MARKER not in repr(error)


def test_physical_references_build_sparse_exact_occurrences() -> None:
    file = resource("é.sh")
    config = authored_manifest([file])
    raw = command_document(config, config)
    empty = output_document("command")
    child(raw, "graph", "nodes")["empty"] = child(empty, "graph", "nodes")["producer"]
    text = canonical(raw)
    reference = content()
    owners = (
        GraphResourceOwner(SCOPE, BODY, "é.sh", reference),
        GraphResourceOwner(SCOPE, ROOT, "é.sh", reference),
    )
    expected: JsonObject = {
        "interpretation": INTERPRETATION,
        "commands": [
            {
                "path": list(path),
                "resource_set_digest": config["resource_set_digest"],
                "resources": [{**file, "storage_ref": expected_ref()}],
            }
            for path in (ROOT, BODY)
        ],
    }

    built = build_graph_resource_references(text, SCOPE, owners, limits=LIMITS)
    checked = check_graph_resource_references(
        text, declared(text), ordered_reverse(expected), SCOPE, owners, limits=LIMITS
    )

    assert built.text == checked.text == canonical(expected)


def test_prepared_resource_owners_follow_canonical_path_and_file_order() -> None:
    config = authored_manifest([resource("é.sh"), resource("z.sh")])
    text = canonical(command_document(config, config))
    repeated = content(incarnation=MARKER)
    distinct = content(store="second-store", incarnation="second-incarnation")
    root_first = GraphResourceOwner(SCOPE, ROOT, "z.sh", repeated)
    root_second = GraphResourceOwner(SCOPE, ROOT, "é.sh", distinct)
    body_first = GraphResourceOwner(SCOPE, BODY, "z.sh", distinct)
    body_second = GraphResourceOwner(SCOPE, BODY, "é.sh", repeated)
    expected = (root_first, root_second, body_first, body_second)
    supplied = tuple(reversed(expected))

    built = build_graph_resource_references(text, SCOPE, supplied, limits=LIMITS)
    saved = ObjectReader(built.text).object()
    checked = check_graph_resource_references(
        text, declared(text), saved, SCOPE, supplied, limits=LIMITS
    )

    assert all(
        actual is original
        for actual, original in zip(built.owners, expected, strict=True)
    )
    assert checked.text == built.text
    assert physical_file(saved, command=0, file=0)["name"] == "z.sh"
    assert physical_file(saved, command=1, file=1)["name"] == "é.sh"


def test_physical_references_require_explicit_empty_inventory() -> None:
    text = canonical(command_document({}))
    logical = declared(text)
    empty: JsonObject = {"interpretation": INTERPRETATION, "commands": []}

    built = build_graph_resource_references(text, SCOPE, (), limits=LIMITS)
    verified = check_graph_resource_references(
        text, logical, empty, SCOPE, (), limits=LIMITS
    )
    with pytest.raises(GraphValueRejected) as historical:
        check_graph_resource_references(text, logical, {}, SCOPE, (), limits=LIMITS)

    assert built.text == verified.text == canonical(empty)
    assert built.owners == ()
    assert_refused(historical.value, GraphValueFailure.INVALID)


def test_physical_references_refuse_swap_between_two_eligible_occurrences() -> None:
    config = authored_manifest([resource("same")])
    text = canonical(command_document(config, config))
    logical = declared(text)
    owners = (
        GraphResourceOwner(SCOPE, ROOT, "same", content(incarnation="root-object")),
        GraphResourceOwner(SCOPE, BODY, "same", content(incarnation="body-object")),
    )
    built = build_graph_resource_references(text, SCOPE, owners, limits=LIMITS)
    saved = ObjectReader(built.text).object()
    root, body = physical_file(saved), physical_file(saved, command=1)
    root["storage_ref"], body["storage_ref"] = body["storage_ref"], root["storage_ref"]

    with pytest.raises(GraphValueRejected) as refusal:
        check_graph_resource_references(
            text, logical, saved, SCOPE, owners, limits=LIMITS
        )

    assert_refused(refusal.value, GraphValueFailure.INVALID)


@pytest.mark.parametrize(
    "field",
    [
        "namespace",
        "graph",
        "version",
        "path",
        "name",
        "digest",
        "size",
        "extra",
        "missing",
    ],
)
def test_physical_references_require_exact_scoped_owner(field: str) -> None:
    text, _, original = single_inputs()
    owner = original[0]
    scope = {
        "namespace": replace(SCOPE, namespace_id="other"),
        "graph": replace(SCOPE, graph_id="other"),
        "version": replace(SCOPE, version_id="other"),
    }.get(field, SCOPE)
    reference = {
        "digest": content(digest="sha256:" + "1" * 64),
        "size": content(size=1),
    }.get(field, owner.reference)
    owners = (
        GraphResourceOwner(
            scope,
            BODY if field == "path" else ROOT,
            "different" if field == "name" else owner.name,
            reference,
        ),
    )
    if field == "extra":
        owners = (*owners, owner)
    if field == "missing":
        owners = ()

    with pytest.raises(GraphValueRejected) as refusal:
        build_graph_resource_references(text, SCOPE, owners, limits=LIMITS)

    assert_refused(refusal.value, GraphValueFailure.INVALID)


def test_physical_references_reject_repeated_owner_occurrence() -> None:
    config = authored_manifest([resource("one"), resource("two")])
    text = canonical(command_document(config))
    owner = GraphResourceOwner(SCOPE, ROOT, "one", content())

    with pytest.raises(GraphValueRejected) as refusal:
        build_graph_resource_references(text, SCOPE, (owner, owner), limits=LIMITS)

    assert_refused(refusal.value, GraphValueFailure.INVALID)


@pytest.mark.parametrize(
    "field",
    [
        "store",
        "incarnation",
        "dto-version",
        "integer",
        "extra",
        "missing",
        "order",
        "logical",
        "path",
        "format",
    ],
)
def test_physical_references_compare_every_native_value(field: str) -> None:
    config = authored_manifest([resource("a"), resource("z")])
    text = canonical(command_document(config))
    owners = tuple(
        GraphResourceOwner(SCOPE, ROOT, name, content()) for name in ("a", "z")
    )
    logical = declared(text)
    built = build_graph_resource_references(text, SCOPE, owners, limits=LIMITS)
    saved = ObjectReader(built.text).object()
    commands = cast(list[JsonObject], saved["commands"])
    storage = child(physical_file(saved), "storage_ref")
    if field == "store":
        child(storage, "address", "storage_identity")["store_id"] = "other"
    elif field == "incarnation":
        storage["object_incarnation"] = "other"
    elif field == "dto-version":
        child(storage, "address")["version"] = True
    elif field == "integer":
        physical_file(saved)["size"] = 0.0
    elif field == "extra":
        commands.append(commands[0].copy())
    elif field == "missing":
        cast(list[JsonObject], commands[0]["resources"]).pop()
    elif field == "order":
        cast(list[JsonObject], commands[0]["resources"]).reverse()
    elif field == "logical":
        physical_file(logical)["executable"] = True
    elif field == "path":
        commands[0]["path"] = list(BODY)
    else:
        saved["interpretation"] = "kora.graph-resource-refs.v2"

    with pytest.raises(GraphValueRejected) as refusal:
        check_graph_resource_references(
            text, logical, saved, SCOPE, owners, limits=LIMITS
        )

    assert_refused(refusal.value, GraphValueFailure.INVALID)


def tight_limits(
    text: str,
    owners: tuple[GraphResourceOwner, ...],
    dimension: Literal["source_bytes", "scratch", "work"],
) -> GraphResourceReferenceLimits:
    minimum, maximum = 1, getattr(LIMITS, dimension)
    while minimum < maximum:
        middle = (minimum + maximum) // 2
        try:
            build_graph_resource_references(
                text, SCOPE, owners, limits=replace(LIMITS, **{dimension: middle})
            )
        except GraphValueRejected as refusal:
            assert_refused(refusal, GraphValueFailure.BUDGET)
            minimum = middle + 1
        else:
            maximum = middle
    return replace(LIMITS, **{dimension: minimum})


@pytest.mark.parametrize("dimension", ["source_bytes", "scratch", "work"])
def test_physical_references_admit_exact_and_one_short_capacity(
    dimension: Literal["source_bytes", "scratch", "work"],
) -> None:
    text, _, owners = single_inputs()
    exact = tight_limits(text, owners, dimension)
    short = replace(exact, **{dimension: getattr(exact, dimension) - 1})

    built = build_graph_resource_references(text, SCOPE, owners, limits=exact)
    with pytest.raises(GraphValueRejected) as refusal:
        build_graph_resource_references(text, SCOPE, owners, limits=short)

    assert len(built.text) > 0
    assert_refused(refusal.value, GraphValueFailure.BUDGET)


@pytest.mark.parametrize(
    ("dimension", "previous_build", "reader_exact"),
    [("scratch", 35_559, 35_559), ("work", 52_717, 54_603)],
)
def test_prepared_owner_retention_fits_exact_limit_with_unchanged_reader(
    dimension: Literal["scratch", "work"], previous_build: int, reader_exact: int
) -> None:
    text, logical, owners = single_inputs()
    prepared_exact = previous_build + 16_579
    exact = replace(LIMITS, **{dimension: prepared_exact})
    short = replace(exact, **{dimension: prepared_exact - 1})
    reader_limits = replace(LIMITS, **{dimension: reader_exact})

    built = build_graph_resource_references(text, SCOPE, owners, limits=exact)
    with pytest.raises(GraphValueRejected) as refused:
        build_graph_resource_references(text, SCOPE, owners, limits=short)
    checked = check_graph_resource_references(
        text,
        logical,
        ObjectReader(built.text).object(),
        SCOPE,
        owners,
        limits=reader_limits,
    )

    frames = {
        frame.f_code.co_name: frame.f_locals
        for frame, _ in walk_tb(refused.value.__traceback__)
    }

    assert built.owners == owners
    assert type(checked) is GraphResourceReferences
    assert checked.text == built.text
    assert_refused(refused.value, GraphValueFailure.BUDGET)
    assert "_references_result" not in frames
    assert set(frames["build_graph_resource_references"]) == {
        "limits",
        "result",
        "reason",
    }


def test_physical_references_charge_saved_comparison_and_repeated_occurrences() -> None:
    text, logical, owners = single_inputs()
    tight = tight_limits(text, owners, "work")
    built = build_graph_resource_references(text, SCOPE, owners, limits=tight)
    saved = ObjectReader(built.text).object()
    config = authored_manifest([resource("é.sh")])
    repeated = canonical(command_document(config, config))
    shared = (*owners, GraphResourceOwner(SCOPE, BODY, "é.sh", owners[0].reference))
    comparison_limits = replace(LIMITS, work=52_717)

    with pytest.raises(GraphValueRejected) as comparison:
        check_graph_resource_references(
            text, logical, saved, SCOPE, owners, limits=comparison_limits
        )
    with pytest.raises(GraphValueRejected) as duplicate_cost:
        build_graph_resource_references(repeated, SCOPE, shared, limits=tight)
    complete = build_graph_resource_references(repeated, SCOPE, shared, limits=LIMITS)

    assert_refused(comparison.value, GraphValueFailure.BUDGET)
    assert_refused(duplicate_cost.value, GraphValueFailure.BUDGET)
    assert (
        len(cast(list[object], ObjectReader(complete.text).object()["commands"])) == 2
    )


def test_physical_references_keep_owner_and_saved_data_opaque() -> None:
    text, logical, _ = single_inputs()
    owner = GraphResourceOwner(SCOPE, ROOT, "é.sh", content(incarnation=MARKER))
    owners = (owner,)
    built = build_graph_resource_references(text, SCOPE, owners, limits=LIMITS)
    saved = ObjectReader(built.text).object()
    child(physical_file(saved), "storage_ref")["object_incarnation"] = (
        MARKER + "-changed"
    )
    protected_name = "reference"

    with pytest.raises(GraphValueRejected) as refused:
        check_graph_resource_references(
            text, logical, saved, SCOPE, owners, limits=LIMITS
        )
    with pytest.raises(AttributeError, match="immutable"):
        setattr(owner, protected_name, content())
    for protected in (owner, built):
        with pytest.raises(TypeError, match="require explicit access"):
            pickle.dumps(protected)
        with pytest.raises(ValueError, match="not iterable") as encoder:
            jsonable_encoder(protected)

        assert MARKER not in repr(protected)
        assert MARKER not in repr(encoder.value)

    assert_refused(refused.value, GraphValueFailure.INVALID)
    frames = dict(
        (frame.f_code.co_name, frame.f_locals)
        for frame, _ in walk_tb(refused.value.__traceback__)
    )
    assert set(frames["check_graph_resource_references"]) == {"limits", "result"}


@pytest.mark.parametrize(
    "case",
    ["duplicate", "depth", "integer", "float", "constant", "unicode", "shape", "codec"],
)
def test_native_resource_refusals_drop_bytes_and_parser_state(case: str) -> None:
    text, logical, owners = single_inputs()
    built = build_graph_resource_references(text, SCOPE, owners, limits=LIMITS)
    wire = built.text.encode()
    encoding: NativeResourceEncoding = "utf-8"
    if case == "duplicate":
        wire = b'{"\\u0069nterpretation":"private-content-reference-marker",' + wire[1:]
    elif case == "depth":
        wire = b'{"extra":' + b"[" * 8 + b"0" + b"]" * 8 + b"}"
    elif case == "integer":
        wire = wire.replace(b'"size":0', b'"size":1234567')
    elif case == "float":
        wire = wire.replace(b'"size":0', b'"size":0.0')
    elif case == "constant":
        wire = wire.replace(b'"size":0', b'"size":NaN')
    elif case == "unicode":
        wire = b'{"extra":"\\ud800"}'
    elif case == "shape":
        wire = b"[]"
    else:
        encoding = cast(NativeResourceEncoding, "utf8")
    wrapped = NativeGraphResourceTransport(wire, encoding)
    limits = LIMITS if case == "duplicate" else replace(LIMITS, source_bytes=len(wire))

    with pytest.raises(GraphValueRejected) as refused:
        check_graph_resource_references(
            text, logical, wrapped, SCOPE, owners, limits=limits
        )
    with pytest.raises(TypeError, match="require explicit access"):
        pickle.dumps(wrapped)
    with pytest.raises(ValueError, match="not iterable") as encoder:
        jsonable_encoder(wrapped)

    assert_refused(refused.value, GraphValueFailure.INVALID)
    assert MARKER not in repr(wrapped)
    assert MARKER not in repr(encoder.value)
    frames = {
        frame.f_code.co_name: frame.f_locals
        for frame, _ in walk_tb(refused.value.__traceback__)
    }
    assert "_decode_native_resources" not in frames
    assert set(frames["check_graph_resource_references"]) == {"limits", "result"}


@pytest.mark.parametrize(
    ("dimension", "utf8_units", "utf16_units"),
    [("source_bytes", 1, 2), ("scratch", 22, 48), ("work", 30, 70)],
)
def test_native_resource_budget_refuses_before_invalid_unicode(
    dimension: str, utf8_units: int, utf16_units: int
) -> None:
    text, logical, owners = single_inputs()
    malformed = (
        (NativeGraphResourceTransport(b"\xff", "utf-8"), utf8_units),
        (NativeGraphResourceTransport(b"\x00\xd8", "utf-16le"), utf16_units),
    )

    for transport, units in malformed:
        exact = replace(LIMITS, **{dimension: units})
        short = replace(LIMITS, **{dimension: units - 1})
        with pytest.raises(GraphValueRejected) as invalid:
            check_graph_resource_references(
                text, logical, transport, SCOPE, owners, limits=exact
            )
        with pytest.raises(GraphValueRejected) as refused:
            check_graph_resource_references(
                text, logical, transport, SCOPE, owners, limits=short
            )

        assert_refused(invalid.value, GraphValueFailure.INVALID)
        assert_refused(refused.value, GraphValueFailure.BUDGET)


@pytest.mark.parametrize(
    ("dimension", "reader_units", "decoder_units_per_byte"),
    [("scratch", 35_559, 24), ("work", 54_603, 35)],
)
def test_native_resources_share_the_exact_reconciliation_budget(
    dimension: str, reader_units: int, decoder_units_per_byte: int
) -> None:
    text, logical, owners = single_inputs()
    built = build_graph_resource_references(text, SCOPE, owners, limits=LIMITS)
    wire = built.text.encode("utf-16le")
    transport = NativeGraphResourceTransport(wire, "utf-16le")
    limit = reader_units + decoder_units_per_byte * len(wire)
    exact = replace(LIMITS, **{dimension: limit})
    short = replace(LIMITS, **{dimension: limit - 1})

    checked = check_graph_resource_references(
        text, logical, transport, SCOPE, owners, limits=exact
    )
    with pytest.raises(GraphValueRejected) as refused:
        check_graph_resource_references(
            text, logical, transport, SCOPE, owners, limits=short
        )

    assert checked.text == built.text
    assert_refused(refused.value, GraphValueFailure.BUDGET)


async def verify_native_references(
    database: Database, encoding: NativeResourceEncoding
) -> None:
    ledger = ConnectionLedger(database)
    async with database.transaction() as connection:
        seeder = Seeder(connection)
        namespace = await seeder.namespace()
        graph = await seeder.graph(namespace)
        version = seeder.new_id()
    scope = GraphResourceScope(namespace, graph, version)
    name = "é𐐀.sh"
    text, logical, owners = single_inputs(scope, name=name)
    built = build_graph_resource_references(text, scope, owners, limits=LIMITS)
    assert len(built.text) <= LIMITS.source_bytes
    native = ObjectReader(built.text).object()
    prepared = prepare_graph_version_identity(
        ObjectReader(text).object(),
        {"command_resources": logical},
        limits=VERSION_LIMITS,
    )
    encoded, digests = prepared.values, prepared.digests
    pin = GraphVersionPin(graph, version, digests.execution_contract_digest)
    async with database.transaction() as connection:
        await Seeder(connection).graph_version(
            graph,
            ir=encoded.ir,
            version_id=version,
            execution_contract=encoded.execution_contract,
            value_encoding_version=encoded.value_encoding_version,
            ir_digest=digests.ir_digest,
            execution_contract_digest=digests.execution_contract_digest,
            deployment_digest=digests.deployment_digest,
            resources=native,
        )
    before_read = ledger.checkouts

    async with database.read_snapshot() as connection:
        row = await graph_version_by_number(connection, namespace, graph, 1)
        field = graph_resource_binary_projection(
            database.dialect, graph_version.c.resources
        )
        wire: bytes = (
            await connection.execute(select(field).where(graph_version.c.id == version))
        ).scalar_one()
        encoding_query = (
            "SHOW server_encoding"
            if database.dialect is Dialect.POSTGRESQL
            else "PRAGMA main.encoding"
        )
        observed: object = (
            await connection.exec_driver_sql(encoding_query)
        ).scalar_one()
    assert row is not None
    assert ledger.checkouts > before_read
    assert ledger.outstanding == database.units_in_flight() == 0
    after_read = ledger.checkouts
    checked = read_graph_version_identity(row, pin, limits=VERSION_LIMITS)
    transport = NativeGraphResourceTransport(wire, encoding)
    saved_logical = checked.values.execution_contract["command_resources"]
    verified = check_graph_resource_references(
        checked.canonical_ir,
        saved_logical,
        transport,
        scope,
        owners,
        limits=LIMITS,
    )
    for reference in (content(store="other"), content(incarnation="other")):
        with pytest.raises(GraphValueRejected) as mismatch:
            check_graph_resource_references(
                checked.canonical_ir,
                saved_logical,
                transport,
                scope,
                (GraphResourceOwner(scope, ROOT, name, reference),),
                limits=LIMITS,
            )
        assert_refused(mismatch.value, GraphValueFailure.INVALID)
    await database.close()

    assert type(row.resources) is dict
    assert type(observed) is str
    assert observed.lower() == (
        "utf8" if database.dialect is Dialect.POSTGRESQL else encoding
    )
    assert name.encode(encoding) in wire
    assert list(row.resources) != list(native)
    assert row.resources == native
    assert row.id == scope.version_id
    assert verified.text == built.text
    assert ledger.checkouts == after_read
    assert ledger.opened == ledger.closed
    assert ledger.outstanding == ledger.closed_while_checked_out == 0


@pytest.mark.asyncio
async def test_physical_references_match_native_rows_after_release(
    database: Database,
) -> None:
    await verify_native_references(database, "utf-8")


@pytest.mark.parametrize("encoding", ["utf-16le", "utf-16be"])
@pytest.mark.asyncio
async def test_native_physical_references_match_sqlite_utf16(
    tmp_path: Path, encoding: NativeResourceEncoding
) -> None:
    settings = encoded_sqlite_settings(tmp_path / "resources.db", encoding)
    await upgrade_database(settings)
    database = Database(settings)

    try:
        await verify_native_references(database, encoding)
    finally:
        await database.close()


NATIVE_SCOPE = GraphResourceScope(encode(1), encode(2), encode(3))


def native_owner_row(
    owner: GraphResourceOwner,
    ordinal: int,
    state: NativeOwnerState,
    encoding: NativeResourceEncoding,
) -> NativeOwnerRow:
    reference = owner.reference
    return (
        ordinal,
        reference.total_size,
        owner.path[2].encode(encoding) if len(owner.path) == 5 else None,
        owner.path[-1].encode(encoding),
        owner.name.encode(encoding),
        reference.address.storage_identity.store_id.encode(encoding),
        reference.address.payload_digest.encode(encoding),
        reference.object_incarnation.encode(encoding),
        state.encode(encoding),
    )


def native_owner_case(
    encoding: NativeResourceEncoding,
) -> tuple[
    str,
    JsonObject,
    PreparedGraphResourceReferences,
    NativeGraphResourceTransport,
    tuple[NativeOwnerRow, NativeOwnerRow],
]:
    name = "é𐐀.sh"
    config = authored_manifest([resource(name)])
    ir = canonical(command_document(config, config))
    owners = (
        GraphResourceOwner(NATIVE_SCOPE, ROOT, name, content(incarnation=MARKER)),
        GraphResourceOwner(NATIVE_SCOPE, BODY, name, content(incarnation="body-owner")),
    )
    built = build_graph_resource_references(ir, NATIVE_SCOPE, owners, limits=LIMITS)
    first, second = built.owners
    return (
        ir,
        declared(ir),
        built,
        NativeGraphResourceTransport(built.text.encode(encoding), encoding),
        (
            native_owner_row(first, 0, "expiring", encoding),
            native_owner_row(second, 1, "missing", encoding),
        ),
    )


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16le", "utf-16be"])
def test_native_owner_reconciliation_keeps_canonical_state_vector(
    encoding: NativeResourceEncoding,
) -> None:
    ir, logical, built, wire, rows = native_owner_case(encoding)
    batch = AcquiredGraphResourceOwners(NATIVE_SCOPE.version_id, rows)

    checked = check_graph_resource_references(
        ir, logical, wire, NATIVE_SCOPE, batch, limits=LIMITS
    )
    legacy = check_graph_resource_references(
        ir,
        logical,
        ObjectReader(built.text).object(),
        NATIVE_SCOPE,
        tuple(reversed(built.owners)),
        limits=LIMITS,
    )

    assert type(checked) is CheckedNativeGraphResourceReferences
    assert checked.owner_states == ("expiring", "missing")
    assert checked.text == legacy.text == built.text
    assert type(legacy) is GraphResourceReferences
    assert MARKER not in repr(checked)
    with pytest.raises(TypeError, match="require explicit access"):
        pickle.dumps(checked)
    with pytest.raises(ValueError, match="not iterable"):
        jsonable_encoder(checked)


@pytest.mark.parametrize(
    "case",
    [
        "version",
        "tuple-order",
        "canonical-order",
        "row-shape",
        "ordinal-type",
        "file-type",
        "batch-type",
        "metadata-width",
        "missing",
        "extra",
        "duplicate",
        "store",
        "incarnation",
        "model",
        "state",
        "codec",
    ],
)
def test_native_owner_reconciliation_refuses_inconsistent_evidence(case: str) -> None:
    ir, logical, _, wire, (first, second) = native_owner_case("utf-8")
    rows: tuple[NativeOwnerRow, ...] = (first, second)
    version = NATIVE_SCOPE.version_id
    limits = LIMITS
    if case == "version":
        version = encode(4)
    elif case == "tuple-order":
        rows = (second, first)
    elif case == "canonical-order":
        rows = ((0, *second[1:]), (1, *first[1:]))
    elif case == "row-shape":
        rows = (cast(NativeOwnerRow, first[:-1]), second)
    elif case == "ordinal-type":
        rows = ((False, *first[1:]), second)
    elif case == "file-type":
        rows = (
            cast(NativeOwnerRow, (*first[:4], bytearray(first[4]), *first[5:])),
            second,
        )
    elif case == "batch-type":
        rows = cast("tuple[NativeOwnerRow, ...]", [first, second])
    elif case == "metadata-width":
        rows = ((*first[:5], b"x" * 257, *first[6:]), second)
        limits = replace(LIMITS, source_bytes=len(wire.payload) + len(rows))
    elif case == "missing":
        rows = (first,)
    elif case == "extra":
        rows = (first, second, (2, *first[1:]))
    elif case == "duplicate":
        rows = (first, (1, *first[1:]))
    elif case == "store":
        rows = ((*first[:5], b"Another-store", *first[6:]), second)
    elif case == "incarnation":
        rows = ((*first[:7], b"Another-incarnation", first[-1]), second)
    elif case == "model":
        rows = ((*first[:5], b"invalid " + MARKER.encode(), *first[6:]), second)
    elif case == "state":
        rows = ((*first[:-1], b"present!"), second)
    else:
        wire = NativeGraphResourceTransport(wire.payload, "utf-16le")
    batch = AcquiredGraphResourceOwners(version, rows)

    with pytest.raises(GraphValueRejected) as refusal:
        check_graph_resource_references(
            ir, logical, wire, NATIVE_SCOPE, batch, limits=limits
        )

    assert_refused(refusal.value, GraphValueFailure.INVALID)
    frames = {
        frame.f_code.co_name: frame.f_locals
        for frame, _ in walk_tb(refusal.value.__traceback__)
    }
    assert set(frames["check_graph_resource_references"]) == {"limits", "result"}
    assert "_decode_native_owner" not in frames


def native_owner_admission_units(
    wire: NativeGraphResourceTransport, rows: tuple[NativeOwnerRow, ...]
) -> dict[str, int]:
    vector_units = 4 + 4 * len(rows)
    source = len(wire.payload) + len(rows)
    scratch = 22 * len(wire.payload) + vector_units
    work = 30 * len(wire.payload) + len(rows) + vector_units
    for row in rows:
        native_bytes = sum(len(cell) for cell in row if type(cell) is bytes)
        metadata = 256 + 2 * (len(row[5]) + len(row[6]) + len(row[7]))
        retained = 16 + 4 * native_bytes + 9 * metadata
        source += native_bytes
        scratch += retained
        work += native_bytes + retained + 4 * native_bytes + 12 * metadata
    return {"source_bytes": source, "scratch": scratch, "work": work}


@pytest.mark.parametrize("dimension", ["source_bytes", "scratch", "work"])
def test_native_owner_admission_charges_prior_wire_and_rows_before_decoding(
    dimension: str,
) -> None:
    ir, logical, _, wire, (first, second) = native_owner_case("utf-8")
    rows = (first, (*second[:4], b"\xff", *second[5:]))
    batch = AcquiredGraphResourceOwners(NATIVE_SCOPE.version_id, rows)
    units = native_owner_admission_units(wire, rows)[dimension]
    exact = replace(LIMITS, **{dimension: units})
    short = replace(LIMITS, **{dimension: units - 1})

    with pytest.raises(GraphValueRejected) as invalid:
        check_graph_resource_references(
            ir, logical, wire, NATIVE_SCOPE, batch, limits=exact
        )
    with pytest.raises(GraphValueRejected) as budget:
        check_graph_resource_references(
            ir, logical, wire, NATIVE_SCOPE, batch, limits=short
        )

    assert_refused(invalid.value, GraphValueFailure.INVALID)
    assert_refused(budget.value, GraphValueFailure.BUDGET)
