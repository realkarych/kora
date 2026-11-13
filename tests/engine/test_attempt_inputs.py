import pytest
from sqlalchemy import select, update

from kora.db.connection import Database
from kora.db.tables import attempt, node_instance
from kora.engine.attempt_inputs import (
    AttemptInputsRejected,
    admit_input_document,
    prepare_attempt_inputs,
    read_attempt_inputs,
)
from kora.ir.graph_json import TextBudget, render_object
from kora.ir.json import JsonObject
from kora.protocols.worker_claim_wire import MAX_IMMUTABLE_BYTES
from tests.db.seed import Seeder

VERSION = "00000000000000000000000001"
DECLARED: JsonObject = {
    "candidate": {"path": '.["世界\\u0000"]', "optional": False},
    "feedback": {"optional": True},
}


@pytest.mark.asyncio
@pytest.mark.parametrize("selector", [None, "", '.["世界😀"]', "nul\0surrogate\ud800"])
async def test_attempt_input_storage_roundtrips_exact_selector_text(
    database: Database, selector: str | None
) -> None:
    declaration: JsonObject = {
        "candidate": {} if selector is None else {"path": selector}
    }
    prepared = prepare_attempt_inputs(declaration, {"candidate": VERSION})
    async with database.transaction() as writer:
        seed = Seeder(writer)
        namespace_id = await seed.namespace()
        graph_id = await seed.graph(namespace_id)
        version_id = await seed.graph_version(graph_id)
        run_id = await seed.run(namespace_id, graph_id, version_id)
        node_id = await seed.node(run_id, "consumer")
        attempt_id = await seed.attempt(node_id, "created")

        await writer.execute(
            update(attempt)
            .where(attempt.c.id == attempt_id)
            .values(input_bindings=prepared.storage)
        )
        await writer.execute(
            update(node_instance)
            .where(node_instance.c.id == node_id)
            .values(consumed_versions={})
        )
    async with database.read_transaction() as reader:
        stored: object = await reader.scalar(
            select(attempt.c.input_bindings).where(attempt.c.id == attempt_id)
        )
    decoded = read_attempt_inputs(stored, declaration)

    assert decoded == prepared
    assert decoded.entries[0].path == selector
    assert decoded.entries[0].artifact_version_id == VERSION
    assert stored == prepared.storage


def test_attempt_input_snapshot_orders_names_and_preserves_optional_absence() -> None:
    sources = {"feedback": None, "candidate": VERSION}

    prepared = prepare_attempt_inputs(DECLARED, sources)
    logical = prepared.claim_map()
    logical_bytes = render_object(
        logical, TextBudget(MAX_IMMUTABLE_BYTES, ascii_only=False)
    ).encode()
    decoded = read_attempt_inputs(prepared.storage, DECLARED)
    logical["candidate"] = None

    assert decoded == prepared
    assert tuple(prepared.claim_map()) == ("candidate", "feedback")
    assert prepared.claim_map()["candidate"] is not None
    assert prepared.claim_map()["feedback"] == {
        "source": {"kind": "absent"},
        "path": None,
        "relation": "consumed",
    }
    assert len(prepared.storage.encode()) == len(logical_bytes) + 23 + 7 * 2


@pytest.mark.parametrize(
    "stored",
    [
        {},
        '{"version":true,"inputs":[]}',
        '{"version":2,"inputs":[]}',
        '{"inputs":[],"version":1}',
        '{"version":1,"inputs":[],"extra":null}',
        '{"version":1,"inputs":[]} ',
        '{"version":1,"inputs":[],"inputs":[]}',
    ],
)
def test_attempt_input_reader_refuses_legacy_and_noncanonical_envelopes(
    stored: object,
) -> None:
    with pytest.raises(
        AttemptInputsRejected, match="^immutable attempt inputs are unavailable$"
    ):
        read_attempt_inputs(stored, {})


@pytest.mark.parametrize(
    "replacement",
    [
        ('"candidate"', '"feedback"'),
        ('"kind":"artifact"', '"kind":"parameter"'),
        ('"relation":"consumed"', '"relation":"reused_invariant"'),
        (VERSION, "invalid"),
        ('"path":null', '"path":"changed"'),
        ('"name":"candidate"', '"name":"candidate","extra":null'),
        ('"version":1', '"version":1.0'),
    ],
)
def test_attempt_input_reader_refuses_changed_inventory_and_source_shape(
    replacement: tuple[str, str],
) -> None:
    declared: JsonObject = {"candidate": {}}
    prepared = prepare_attempt_inputs(declared, {"candidate": VERSION})
    changed = prepared.storage.replace(*replacement)

    with pytest.raises(AttemptInputsRejected):
        read_attempt_inputs(changed, declared)

    assert changed != prepared.storage


def test_attempt_input_reader_refuses_absence_for_required_source() -> None:
    optional: JsonObject = {"candidate": {"optional": True}}
    prepared = prepare_attempt_inputs(optional, {"candidate": None})

    with pytest.raises(AttemptInputsRejected):
        read_attempt_inputs(prepared.storage, {"candidate": {}})

    assert read_attempt_inputs(prepared.storage, optional) == prepared


@pytest.mark.parametrize("character", ["a", "é", "世", "😀"])
def test_input_document_admission_checks_utf8_bytes_across_chunks(
    character: str,
) -> None:
    document = character * 4097
    exact_bytes = len(document.encode("utf-8"))

    admitted = admit_input_document(document, exact_bytes)
    with pytest.raises(AttemptInputsRejected):
        admit_input_document(document, exact_bytes - 1)

    assert admitted is None


def test_input_document_admission_rejects_raw_surrogates_without_private_context() -> (
    None
):
    document = "private-input-content\ud800"

    with pytest.raises(AttemptInputsRejected) as refusal:
        admit_input_document(document, 4096)

    assert refusal.value.__context__ is None
    assert "private-input-content" not in str(refusal.value)


def test_attempt_input_map_accepts_exact_partition_and_refuses_one_byte_more() -> None:
    empty = prepare_attempt_inputs({"candidate": {"path": ""}}, {"candidate": VERSION})
    overhead = len(
        render_object(
            empty.claim_map(), TextBudget(MAX_IMMUTABLE_BYTES, ascii_only=False)
        ).encode()
    )
    size = MAX_IMMUTABLE_BYTES - overhead
    selector = "é" * (size // 2) + "a" * (size % 2)
    declared: JsonObject = {"candidate": {"path": selector}}

    prepared = prepare_attempt_inputs(declared, {"candidate": VERSION})
    decoded = read_attempt_inputs(prepared.storage, declared)
    with pytest.raises(AttemptInputsRejected):
        prepare_attempt_inputs(
            {"candidate": {"path": selector + "a"}}, {"candidate": VERSION}
        )

    assert decoded == prepared
    assert len(prepared.storage.encode()) == MAX_IMMUTABLE_BYTES + 30
