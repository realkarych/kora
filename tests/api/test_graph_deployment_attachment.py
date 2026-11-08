import pickle
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from traceback import walk_tb

import pytest
from fastapi.encoders import jsonable_encoder

from kora.api.graph_deployment_attachment import (
    AttachedGraphDeployment,
    GraphDeploymentAttachmentLimits,
    attach_graph_deployment_references,
    is_checked_graph_attachment,
)
from kora.api.graph_execution_preparation import (
    PreparedExecutionContract,
    PreparedGraphDeploymentInputs,
    prepare_execution_contract,
    prepare_graph_deployment_inputs,
)
from kora.db.graph_value_json import (
    GraphValueFailure,
    GraphValueRejected,
    ObjectReader,
    TextBudget,
    render_object,
)
from kora.engine.graph_command_manifests import (
    CommandResourceContent,
    PreparedCommandStageGroup,
)
from kora.engine.graph_resource_references import (
    GraphResourceOwner,
    GraphResourceScope,
    PreparedGraphResourceReferences,
)
from kora.protocols.storage_models import MAX_STORAGE_METADATA, ContentRef
from tests.api.test_graph_execution_preparation import (
    DEPLOYMENT_LIMITS,
    DEPLOYMENT_STORE,
    LIMITS,
    MARKER,
    PRIVATE_STORE,
    READ_LIMITS,
    borrowed,
    deployment_source,
    source,
)
from tests.engine.graph_output_cases import child
from tests.engine.test_graph_command_manifests import ZERO, authored_manifest, resource
from tests.engine.test_graph_resource_references import SCOPE, assert_refused, content

ROOT_PATH = ("graph", "nodes", "producer")
SECOND_PATH = ("graph", "nodes", "second")
PRIVATE_OBJECT = "private-attachment-object-453"
ATTACH_LIMITS = GraphDeploymentAttachmentLimits(
    READ_LIMITS.resources,
    READ_LIMITS.physical_input_bytes,
    100_000_000,
    100_000_000,
)


class ForeignReferenceFields(dict[str, object]):
    def __len__(self) -> int:
        raise AssertionError("foreign reference fields were inspected")


def attachment_inputs(
    scope: GraphResourceScope,
) -> tuple[str, tuple[CommandResourceContent, ...], tuple[GraphResourceOwner, ...]]:
    payload = MARKER.encode("ascii")
    digest = "sha256:" + sha256(payload).hexdigest()
    ir_text, _ = source()
    raw = ObjectReader(ir_text).object()
    nodes = child(raw, "graph", "nodes")
    nodes["second"] = deepcopy(child(nodes, "producer"))
    child(nodes, "producer", "config").update(
        authored_manifest([resource("é.sh"), resource("a.sh", len(payload), digest)])
    )
    child(nodes, "second", "config").update(
        authored_manifest([resource("a.sh", len(payload), digest)])
    )
    files = (
        CommandResourceContent(ROOT_PATH, "a.sh", payload),
        CommandResourceContent(ROOT_PATH, "é.sh", b""),
        CommandResourceContent(SECOND_PATH, "a.sh", payload),
    )
    shared = content(
        store=PRIVATE_STORE,
        incarnation=PRIVATE_OBJECT,
        digest=digest,
        size=len(payload),
    )
    empty = content(store=PRIVATE_STORE, incarnation="empty-object")
    owners = (
        GraphResourceOwner(scope, ROOT_PATH, "a.sh", shared),
        GraphResourceOwner(scope, ROOT_PATH, "é.sh", empty),
        GraphResourceOwner(scope, SECOND_PATH, "a.sh", shared),
    )
    return (
        render_object(raw, TextBudget(READ_LIMITS.identity.max_encoded_bytes)),
        tuple(reversed(files)),
        owners,
    )


def prepare_inputs(
    ir_text: str, files: tuple[CommandResourceContent, ...]
) -> PreparedGraphDeploymentInputs:
    with borrowed() as (runner, evaluator):
        prepared = runner.run(
            prepare_graph_deployment_inputs(
                ir_text,
                files,
                evaluator,
                storage_identity=DEPLOYMENT_STORE if files else None,
                limits=DEPLOYMENT_LIMITS,
            )
        )
    assert isinstance(prepared, PreparedGraphDeploymentInputs)
    return prepared


def exact_attachment_limits(
    prepared: PreparedGraphDeploymentInputs, owners: tuple[GraphResourceOwner, ...]
) -> GraphDeploymentAttachmentLimits:
    resources = READ_LIMITS.resources
    logical = resources.logical
    fixed = (
        32
        + len(prepared.groups) * (4 * (256 + 71) + 16)
        + len(owners) * (4 + 8 * MAX_STORAGE_METADATA)
    )
    return replace(
        ATTACH_LIMITS,
        max_work=fixed + resources.work + logical.work,
        max_retained_bytes=(
            fixed
            + len(prepared.logical.canonical_ir)
            + len(prepared.logical.canonical_contract)
            + 192
            + sum(group.total_size for group in prepared.groups)
            + resources.source_bytes
            + resources.scratch
            + logical.ir_bytes
            + logical.scratch
        ),
    )


def assert_attachment_refused(
    error: GraphValueRejected, expected: GraphValueFailure
) -> None:
    frames = {
        frame.f_code.co_name: frame.f_locals
        for frame, _ in walk_tb(error.__traceback__)
    }

    assert_refused(error, expected)
    assert set(frames["attach_graph_deployment_references"]) == {"reason"}
    assert "_attach" not in frames
    assert "_snapshot_reference" not in frames
    assert all(
        marker not in repr(error) for marker in (MARKER, PRIVATE_STORE, PRIVATE_OBJECT)
    )


@pytest.fixture(scope="module")
def nonempty_preparation() -> tuple[
    PreparedGraphDeploymentInputs, PreparedExecutionContract
]:
    scope = replace(SCOPE)
    ir_text, files, owners = attachment_inputs(scope)
    with borrowed() as (runner, evaluator):
        prepared = runner.run(
            prepare_graph_deployment_inputs(
                ir_text,
                files,
                evaluator,
                storage_identity=DEPLOYMENT_STORE,
                limits=DEPLOYMENT_LIMITS,
            )
        )
        legacy = runner.run(
            prepare_execution_contract(ir_text, scope, owners, evaluator, limits=LIMITS)
        )
    assert isinstance(prepared, PreparedGraphDeploymentInputs)
    assert isinstance(legacy, PreparedExecutionContract)
    return prepared, legacy


def test_attachment_preserves_exact_pair_and_detaches_nested_reference_aliases(
    nonempty_preparation: tuple[
        PreparedGraphDeploymentInputs, PreparedExecutionContract
    ],
) -> None:
    prepared, legacy = nonempty_preparation
    scope = replace(SCOPE)
    _, _, owners = attachment_inputs(scope)
    prior_scope = replace(scope)
    expected_refs = tuple(owner.reference.canonical_bytes() for owner in legacy.owners)
    expected_text = legacy.resources.text
    exact = replace(
        exact_attachment_limits(prepared, owners),
        physical_input_bytes=len(expected_text),
    )

    finished = attach_graph_deployment_references(
        prepared, scope, (owners[2], owners[0], owners[1]), limits=exact
    )
    short_aggregate = (
        replace(exact, max_retained_bytes=exact.max_retained_bytes - 1),
        replace(exact, max_work=exact.max_work - 1),
    )
    for short in (
        *short_aggregate,
        replace(exact, physical_input_bytes=exact.physical_input_bytes - 1),
        replace(exact, resources=replace(exact.resources, scratch=1)),
    ):
        with pytest.raises(GraphValueRejected) as refused:
            attach_graph_deployment_references(prepared, scope, owners, limits=short)
        assert_attachment_refused(refused.value, GraphValueFailure.BUDGET)
    malformed = with_reference(
        owners[0], owners[0].reference.model_copy(update={"total_size": False})
    )
    for short in short_aggregate:
        with pytest.raises(GraphValueRejected) as before_reference:
            attach_graph_deployment_references(
                prepared, scope, (malformed, *owners[1:]), limits=short
            )
        assert_attachment_refused(before_reference.value, GraphValueFailure.BUDGET)
    owners[0].reference.address.storage_identity.__init__(
        version=1, store_id="changed-store"
    )
    owners[1].reference.__init__(
        version=1,
        address=owners[1].reference.address,
        object_incarnation="changed-object",
        total_size=0,
    )
    owners[2].__init__(scope, ROOT_PATH, "substituted.sh", owners[0].reference)
    scope.__init__("changed-namespace", "changed-graph", "changed-version")
    with pytest.raises(
        TypeError, match="requires the checked producer"
    ) as wrong_issuer:
        AttachedGraphDeployment(
            object(),
            prior_scope,
            prepared.logical,
            PreparedGraphResourceReferences(expected_text, finished.owners),
        )
    with pytest.raises(AttributeError, match="immutable") as reassigned_text:
        finished.resources.__init__(MARKER)
    forged = object.__new__(AttachedGraphDeployment)
    PreparedExecutionContract.__init__(
        forged,
        prior_scope,
        finished.canonical_ir,
        finished.canonical_contract,
        finished.digests,
        finished.resources,
        finished.owners,
    )
    with pytest.raises(TypeError, match="explicit access"):
        pickle.dumps(finished)
    with pytest.raises(ValueError, match="not iterable") as encoder:
        jsonable_encoder(finished)

    assert type(finished) is AttachedGraphDeployment
    assert is_checked_graph_attachment(finished)
    assert not is_checked_graph_attachment(legacy)
    assert not is_checked_graph_attachment(forged)
    for refusal, allowed in (
        (wrong_issuer, {"__class__": AttachedGraphDeployment}),
        (reassigned_text, {}),
    ):
        frames = {
            frame.f_code.co_name: frame.f_locals
            for frame, _ in walk_tb(refusal.value.__traceback__)
        }
        assert frames["__init__"] == allowed
    assert finished.scope == prior_scope
    assert finished.scope is not scope
    assert finished.canonical_ir is prepared.logical.canonical_ir
    assert finished.canonical_contract is prepared.logical.canonical_contract
    assert finished.digests is prepared.logical.digests
    assert finished.digests == legacy.digests
    assert finished.resources.text == expected_text
    assert (
        tuple(owner.reference.canonical_bytes() for owner in finished.owners)
        == expected_refs
    )
    assert [(owner.path, owner.name) for owner in finished.owners] == [
        (ROOT_PATH, "a.sh"),
        (ROOT_PATH, "é.sh"),
        (SECOND_PATH, "a.sh"),
    ]
    assert {group.ordinals for group in prepared.groups} == {(0, 2), (1,)}
    assert all(owner.scope == prior_scope for owner in finished.owners)
    assert PRIVATE_STORE not in repr(encoder.value)
    assert PRIVATE_OBJECT not in repr(finished)


def with_reference(
    owner: GraphResourceOwner, reference: ContentRef
) -> GraphResourceOwner:
    return GraphResourceOwner(owner.scope, owner.path, owner.name, reference)


def test_attachment_refuses_malformed_and_inconsistent_references(
    nonempty_preparation: tuple[
        PreparedGraphDeploymentInputs, PreparedExecutionContract
    ],
) -> None:
    prepared, _ = nonempty_preparation
    scope = replace(SCOPE)
    _, _, owners = attachment_inputs(scope)
    first, empty, sibling = owners
    original = first.reference
    address = original.address
    identity = address.storage_identity
    foreign_fields = original.model_copy()
    object.__setattr__(
        foreign_fields, "__dict__", ForeignReferenceFields(vars(original))
    )
    references = (
        foreign_fields,
        original.model_copy(update={"version": True}),
        original.model_copy(update={"total_size": False}),
        original.model_copy(update={"unknown": PRIVATE_OBJECT}),
        ContentRef.model_construct(address=address, total_size=original.total_size),
        original.model_copy(update={"address": {"payload_digest": ZERO}}),
        original.model_copy(
            update={"address": address.model_copy(update={"version": True})}
        ),
        original.model_copy(
            update={
                "address": address.model_copy(update={"payload_digest": "sha256:?"})
            }
        ),
        original.model_copy(
            update={
                "address": address.model_copy(
                    update={
                        "storage_identity": identity.model_copy(
                            update={"version": True}
                        )
                    }
                )
            }
        ),
        original.model_copy(
            update={
                "address": address.model_copy(
                    update={
                        "storage_identity": identity.model_copy(
                            update={"store_id": PRIVATE_STORE * 256}
                        )
                    }
                )
            }
        ),
        original.model_copy(update={"object_incarnation": PRIVATE_OBJECT * 256}),
        content(
            store="substituted", digest=address.payload_digest, size=original.total_size
        ),
        content(
            store=PRIVATE_STORE,
            digest=address.payload_digest,
            size=original.total_size + 1,
        ),
        content(
            store=PRIVATE_STORE,
            digest=ZERO,
            size=original.total_size,
        ),
        content(
            store=PRIVATE_STORE, digest=address.payload_digest, size=original.total_size
        ),
    )
    wrong_scope = GraphResourceOwner(
        replace(scope, graph_id="foreign"), first.path, first.name, original
    )
    wrong_name = GraphResourceOwner(scope, first.path, "substitute.sh", original)
    foreign = (wrong_scope, wrong_name)
    uniform = content(
        store=PRIVATE_STORE, digest=address.payload_digest, size=original.total_size
    )

    for candidates in (
        owners[:-1],
        (*owners, sibling),
        (first, empty, first),
        *((owner, empty, sibling) for owner in foreign),
        *(
            (with_reference(first, reference), empty, sibling)
            for reference in references
        ),
    ):
        with pytest.raises(GraphValueRejected) as refused:
            attach_graph_deployment_references(
                prepared, scope, candidates, limits=ATTACH_LIMITS
            )
        assert_attachment_refused(refused.value, GraphValueFailure.INVALID)
    allowed = attach_graph_deployment_references(
        prepared,
        scope,
        (with_reference(first, uniform), empty, with_reference(sibling, uniform)),
        limits=ATTACH_LIMITS,
    )

    assert allowed.digests is prepared.logical.digests
    assert allowed.owners[0].reference == allowed.owners[2].reference == uniform


def test_attachment_requires_a_complete_single_store_address_partition(
    nonempty_preparation: tuple[
        PreparedGraphDeploymentInputs, PreparedExecutionContract
    ],
) -> None:
    prepared, _ = nonempty_preparation
    scope = replace(SCOPE)
    _, _, owners = attachment_inputs(scope)
    groups = {group.ordinals: group for group in prepared.groups}
    shared, empty = groups[(0, 2)], groups[(1,)]
    narrowed = PreparedCommandStageGroup(
        shared.store_id, shared.payload_digest, shared.data, (0,)
    )
    overlap = PreparedCommandStageGroup(
        shared.store_id, shared.payload_digest, shared.data, (0, 1)
    )
    misplaced = PreparedCommandStageGroup(
        empty.store_id, empty.payload_digest, empty.data, (2,)
    )
    unsorted = PreparedCommandStageGroup(
        shared.store_id, shared.payload_digest, shared.data, (2, 0)
    )
    invalid = PreparedCommandStageGroup(
        shared.store_id, shared.payload_digest, shared.data, (False, 2)
    )
    foreign = PreparedCommandStageGroup(
        "foreign", empty.payload_digest, empty.data, (1,)
    )
    repeated = PreparedCommandStageGroup(
        shared.store_id, shared.payload_digest, shared.data, (2,)
    )
    malformed = (
        (narrowed, empty),
        (overlap, empty),
        (overlap, misplaced),
        (unsorted, empty),
        (invalid, empty),
        (shared, foreign),
        (narrowed, repeated, empty),
    )
    candidates = tuple(
        tuple(sorted(groups, key=lambda group: group.payload_digest))
        for groups in malformed
    ) + (tuple(reversed(prepared.groups)),)

    for ordered in candidates:
        with pytest.raises(GraphValueRejected) as refused:
            attach_graph_deployment_references(
                PreparedGraphDeploymentInputs(prepared.logical, ordered),
                scope,
                owners,
                limits=ATTACH_LIMITS,
            )
        assert_attachment_refused(refused.value, GraphValueFailure.INVALID)


def test_attachment_preserves_explicit_empty_resource_references() -> None:
    prepared = prepare_inputs(deployment_source(()), ())

    finished = attach_graph_deployment_references(
        prepared, SCOPE, (), limits=ATTACH_LIMITS
    )

    assert finished.owners == ()
    assert ObjectReader(finished.resources.text).object() == {
        "interpretation": "kora.graph-resource-refs.v1",
        "commands": [],
    }
    assert finished.digests is prepared.logical.digests
