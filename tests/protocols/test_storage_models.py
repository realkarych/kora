import hashlib
import json

import pytest
from pydantic import ValidationError

from kora.protocols.storage_models import (
    ContentAddress,
    ManifestPart,
    PartSpec,
    StageManifest,
    StageRef,
    StageSpec,
    StorageIdentity,
    StorageLimits,
    StorageModel,
    StorageOperationRef,
    StoragePage,
    StorageScanEntry,
)

IDENTITY: dict[str, object] = {"version": 1, "store_id": "store-1"}
STAGE: dict[str, object] = {
    "version": 1,
    "storage_identity": IDENTITY,
    "stage_id": "stage-1",
}
DIGEST: str = "sha256:" + hashlib.sha256(b"").hexdigest()
SPEC: dict[str, object] = {
    "version": 1,
    "stage": STAGE,
    "payload_digest": DIGEST,
    "total_size": 0,
    "content_type": "application/octet-stream",
}


@pytest.mark.parametrize(
    ("model", "document"),
    [
        (StorageIdentity, IDENTITY),
        (StageRef, STAGE),
        (StageSpec, SPEC),
        (
            ContentAddress,
            {"version": 1, "storage_identity": IDENTITY, "payload_digest": DIGEST},
        ),
    ],
)
def test_storage_records_are_closed_versioned_immutable_json(
    model: type[StorageModel], document: dict[str, object]
) -> None:
    record = model.model_validate(document)

    restored = model.model_validate_json(record.model_dump_json())

    assert restored == record
    assert record.model_dump(mode="json") == document
    with pytest.raises(ValidationError):
        model.model_validate({**document, "unknown": 1})
    with pytest.raises(ValidationError):
        model.model_validate(
            {key: value for key, value in document.items() if key != "version"}
        )
    with pytest.raises(ValidationError):
        model.model_validate({**document, "version": True})
    with pytest.raises(ValidationError):
        record.__setattr__("version", 2)


@pytest.mark.parametrize("value", [-1, True, "1", 2**63])
def test_storage_counts_are_strict_and_finite(value: object) -> None:
    document = {**SPEC, "total_size": value}

    with pytest.raises(ValidationError):
        StageSpec.model_validate(document)


@pytest.mark.parametrize(
    "document",
    [
        {**STAGE, "stage_id": "a" * 257},
        {**STAGE, "stage_id": "a\n"},
        {**STAGE, "stage_id": "has space"},
        {**STAGE, "storage_identity": {**IDENTITY, "unknown": "private"}},
    ],
)
def test_storage_identifiers_and_nested_records_are_closed(
    document: dict[str, object],
) -> None:
    with pytest.raises(ValidationError) as raised:
        StageRef.model_validate(document)

    assert "private" not in str(raised.value)


def test_storage_metadata_rejects_raw_oversize_duplicate_and_deep_json() -> None:
    invalid = (
        '{"version":1,"store_id":"a","store_id":"b"}',
        " " * 16384 + '{"version":1,"store_id":"a"}',
        '{"version":1,"store_id":' + "[" * 16 + '"a"' + "]" * 16 + "}",
    )

    for payload in invalid:
        with pytest.raises(ValueError, match="storage"):
            StorageIdentity.model_validate_json(payload)


def test_storage_manifest_is_compact_and_fits_at_maximum_identifiers() -> None:
    identity = StorageIdentity(version=1, store_id="r" * 256)
    stage = StageRef(version=1, storage_identity=identity, stage_id="s" * 256)
    parts = tuple(
        ManifestPart(
            version=1,
            spec=PartSpec(
                version=1,
                index=index,
                byte_offset=index * 16777216,
                size=16777216,
                digest=DIGEST,
                final=index == 15,
            ),
            part_identity="p" * 256,
        )
        for index in range(16)
    )

    manifest = StageManifest(version=1, stage=stage, parts=parts, prefix=None)

    assert len(manifest.model_dump_json().encode()) <= 16384
    assert manifest.model_dump_json().count('"stage_id"') == 1
    with pytest.raises(ValidationError):
        StageManifest(version=1, stage=stage, parts=parts + parts, prefix=None)


def test_storage_operation_digest_has_a_fixed_canonical_preimage() -> None:
    identity = StorageIdentity.model_validate(IDENTITY)
    spec = StageSpec.model_validate(SPEC)
    preimage = {
        "version": 1,
        "kind": "begin_stage",
        "storage_identity": IDENTITY,
        "operation_id": "begin-1",
        "arguments": SPEC,
    }
    expected = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                preimage, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        ).hexdigest()
    )

    operation = StorageOperationRef.for_arguments(
        identity, "begin-1", "begin_stage", spec
    )

    assert operation.request_digest == expected
    assert (
        expected
        == "sha256:d68fa0b129ebb375e4fbb9048643925c32651f4dee9ec7fb7a0b57885019f9dd"
    )
    assert operation.matches("begin_stage", spec)
    assert not operation.matches("seal_stage", spec)
    changed = StageSpec.model_validate({**SPEC, "content_type": "text/plain"})
    assert not operation.matches("begin_stage", changed)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("private_bytes", 1048575),
        ("private_bytes", 17179869185),
        ("stage_slots", 0),
        ("stage_slots", 33),
        ("deletion_slots", 33),
        ("data_work_slots", 9),
        ("reader_slots", 17),
        ("control_work_slots", 3),
        ("stage_slots", True),
    ],
)
def test_storage_limits_refuse_unsupported_settings(field: str, value: object) -> None:
    document = StorageLimits(version=1).model_dump(mode="json")

    with pytest.raises(ValidationError):
        StorageLimits.model_validate({**document, field: value})


def test_storage_complete_page_envelope_is_bounded() -> None:
    identity = StorageIdentity(version=1, store_id="s" * 256)
    entry = StorageScanEntry(version=1, key="k" * 256, state="retired", size=2**63 - 1)

    with pytest.raises(ValidationError):
        StoragePage(
            version=1,
            storage_identity=identity,
            kind="stages",
            entries=(entry,) * 128,
            next_cursor=None,
        )


@pytest.mark.parametrize(
    ("state", "closed"),
    [
        ("settled", False),
        ("settled", True),
        ("pending", True),
        ("evidence_unavailable", True),
    ],
)
def test_storage_observation_never_invents_closure(state: str, closed: bool) -> None:
    from kora.protocols.storage_models import MutationObservation

    identity = StorageIdentity.model_validate(IDENTITY)
    spec = StageSpec.model_validate(SPEC)
    operation = StorageOperationRef.for_arguments(
        identity, "begin-1", "begin_stage", spec
    )

    with pytest.raises(ValidationError):
        MutationObservation.model_validate(
            {
                "version": 1,
                "operation": operation,
                "state": state,
                "result": None,
                "effects_closed": closed,
            }
        )


def test_storage_release_acknowledges_a_compact_exact_mutation_set() -> None:
    from kora.protocols.storage_models import MutationReceipt, StorageMutationSet

    identity = StorageIdentity.model_validate(IDENTITY)
    stage = StageRef.model_validate(STAGE)
    spec = StageSpec.model_validate(SPEC)
    operation = StorageOperationRef.for_arguments(
        identity, "b" * 256, "begin_stage", spec
    )
    receipt = MutationReceipt(
        version=1,
        operation_id=operation.operation_id,
        kind=operation.kind,
        request_digest=operation.request_digest,
        outcome_digest=stage.metadata_digest(),
    )

    mutation_set = StorageMutationSet(version=1, stage=stage, receipts=(receipt,))

    assert len(mutation_set.canonical_bytes()) < 16384
    assert (
        mutation_set.metadata_digest()
        != StorageMutationSet(version=1, stage=stage, receipts=()).metadata_digest()
    )
    with pytest.raises(ValidationError):
        StorageMutationSet(version=1, stage=stage, receipts=(receipt, receipt))


def test_storage_source_declaration_can_exceed_the_retained_payload_cap() -> None:
    document = {**SPEC, "total_size": 400 * 1024 * 1024}

    record = StageSpec.model_validate(document)

    assert record.total_size == 419430400
