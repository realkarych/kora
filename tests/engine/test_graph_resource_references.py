import pickle
from dataclasses import replace
from traceback import walk_tb
from typing import Literal, cast

import pytest
from fastapi.encoders import jsonable_encoder
from sqlalchemy import insert

from kora.db.connection import Database
from kora.db.graph_value_json import GraphValueFailure, GraphValueRejected, ObjectReader
from kora.db.reads import graph_version_by_number
from kora.db.tables import graph_version
from kora.engine.graph_command_manifests import (
    CommandManifestLimits,
    build_declared_command_manifests,
)
from kora.engine.graph_resource_references import (
    GraphResourceOwner,
    GraphResourceReferenceLimits,
    GraphResourceScope,
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
from tests.db.seed import DIGEST, Seeder
from tests.engine.graph_output_cases import child, output_document
from tests.engine.test_graph_command_manifests import (
    ZERO,
    authored_manifest,
    canonical,
    command_document,
    resource,
)

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
    scope: GraphResourceScope = SCOPE,
) -> tuple[str, JsonObject, tuple[GraphResourceOwner, ...]]:
    config = authored_manifest([resource("é.sh")])
    text = canonical(command_document(config))
    owners = (GraphResourceOwner(scope, ROOT, "é.sh", content()),)
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


def test_physical_references_charge_saved_comparison_and_repeated_occurrences() -> None:
    text, logical, owners = single_inputs()
    tight = tight_limits(text, owners, "work")
    built = build_graph_resource_references(text, SCOPE, owners, limits=tight)
    saved = ObjectReader(built.text).object()
    config = authored_manifest([resource("é.sh")])
    repeated = canonical(command_document(config, config))
    shared = (*owners, GraphResourceOwner(SCOPE, BODY, "é.sh", owners[0].reference))

    with pytest.raises(GraphValueRejected) as comparison:
        check_graph_resource_references(
            text, logical, saved, SCOPE, owners, limits=tight
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


@pytest.mark.asyncio
async def test_physical_references_match_native_rows_after_release(
    database: Database,
) -> None:
    ledger = ConnectionLedger(database)
    async with database.transaction() as connection:
        seeder = Seeder(connection)
        namespace = await seeder.namespace()
        graph = await seeder.graph(namespace)
        version = seeder.new_id()
    scope = GraphResourceScope(namespace, graph, version)
    text, logical, owners = single_inputs(scope)
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
        seeder = Seeder(connection)
        await connection.execute(
            insert(graph_version).values(
                id=version,
                graph_id=graph,
                number=1,
                ir=encoded.ir,
                execution_contract=encoded.execution_contract,
                value_encoding_version=encoded.value_encoding_version,
                ir_digest=digests.ir_digest,
                source_digest=DIGEST,
                execution_contract_digest=digests.execution_contract_digest,
                deployment_digest=digests.deployment_digest,
                resolved_digests={},
                resources=native,
                deployed_by="owner",
                deployed_at=seeder.clock.now(),
                status="active",
                **seeder.stamps(),
            )
        )
    before_read = ledger.checkouts

    async with database.read_transaction() as connection:
        row = await graph_version_by_number(connection, namespace, graph, 1)
    assert row is not None
    assert ledger.checkouts > before_read
    assert ledger.outstanding == database.units_in_flight() == 0
    after_read = ledger.checkouts
    checked = read_graph_version_identity(row, pin, limits=VERSION_LIMITS)
    verified = check_graph_resource_references(
        checked.canonical_ir,
        checked.values.execution_contract["command_resources"],
        row.resources,
        scope,
        owners,
        limits=LIMITS,
    )
    await database.close()

    assert type(row.resources) is dict
    assert list(row.resources) != list(native)
    assert row.resources == native
    assert row.id == scope.version_id
    assert verified.text == built.text
    assert ledger.checkouts == after_read
    assert ledger.opened == ledger.closed
    assert ledger.outstanding == ledger.closed_while_checked_out == 0
