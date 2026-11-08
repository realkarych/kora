from dataclasses import replace
from typing import Never, Self

from sqlalchemy.ext.asyncio import AsyncConnection

from kora.api.graph_execution_preparation import (
    PreparedGraphDeploymentInputs,
    PreparedLogicalExecutionContract,
)
from kora.db.graph_deployment_stage_reads import (
    GraphStageInventory,
    GraphStageInventoryRejected,
    GraphStageScope,
    read_graph_stage_inventory,
)
from kora.db.graph_preparation_reads import (
    GraphPreparationBinding,
    require_graph_preparation_binding,
)
from kora.db.transactions import raise_private_metadata_error
from kora.engine.graph_command_manifests import (
    MAX_CONTENT_BYTES,
    MAX_FILES,
    PreparedCommandStageGroup,
)
from kora.engine.graph_version_identity import GraphVersionDigests
from kora.engine.locks import TransitionLocks
from kora.ir.config import COMMAND_RESOURCE_MAX_BYTES

_Address = tuple[str, str]
_ExpectedStages = dict[_Address, tuple[int, tuple[int, ...]]]


class MatchedGraphStage:
    __slots__ = (
        "stage_id",
        "store_id",
        "payload_digest",
        "fence_incarnation",
        "total_size",
        "ordinals",
    )
    stage_id: str
    store_id: str
    payload_digest: str
    fence_incarnation: int
    total_size: int
    ordinals: tuple[int, ...]

    def __new__(
        cls,
        stage_id: str,
        store_id: str,
        payload_digest: str,
        fence_incarnation: int,
        total_size: int,
        ordinals: tuple[int, ...],
    ) -> Self:
        stage = super().__new__(cls)
        object.__setattr__(stage, "stage_id", stage_id)
        object.__setattr__(stage, "store_id", store_id)
        object.__setattr__(stage, "payload_digest", payload_digest)
        object.__setattr__(stage, "fence_incarnation", fence_incarnation)
        object.__setattr__(stage, "total_size", total_size)
        object.__setattr__(stage, "ordinals", ordinals)
        return stage

    def __setattr__(self, name: str, value: object) -> Never:
        raise AttributeError("matched graph stage is immutable")

    def __delattr__(self, name: str) -> Never:
        raise AttributeError("matched graph stage is immutable")

    def __getstate__(self) -> Never:
        raise TypeError("matched graph stage requires explicit access")


def _expected_stages(
    prepared: PreparedGraphDeploymentInputs,
    deployment_digest: str,
    connection: AsyncConnection,
    locks: TransitionLocks,
) -> _ExpectedStages:
    if type(prepared) is not PreparedGraphDeploymentInputs:
        raise_private_metadata_error(GraphStageInventoryRejected())
    logical, groups = prepared.logical, prepared.groups
    if (
        type(logical) is not PreparedLogicalExecutionContract
        or type(logical.digests) is not GraphVersionDigests
        or type(logical.digests.deployment_digest) is not str
        or logical.digests.deployment_digest != deployment_digest
        or type(groups) is not tuple
        or len(groups) > MAX_FILES
    ):
        raise_private_metadata_error(GraphStageInventoryRejected())
    expected: _ExpectedStages = {}
    store_id: str | None = None
    previous_digest = ""
    occupied = 0
    count = 0
    total_size = 0
    for group in groups:
        if type(group) is not PreparedCommandStageGroup:
            raise_private_metadata_error(GraphStageInventoryRejected())
        store, digest, ordinals = group.store_id, group.payload_digest, group.ordinals
        locks.require_existing_storage_address(connection, store, digest)
        if (
            (store_id is not None and store != store_id)
            or digest <= previous_digest
            or type(group.data) is not bytes
            or len(group.data) > COMMAND_RESOURCE_MAX_BYTES
            or type(ordinals) is not tuple
            or not 1 <= len(ordinals) <= MAX_FILES - count
        ):
            raise_private_metadata_error(GraphStageInventoryRejected())
        store_id, previous_digest = store, digest
        size = len(group.data)
        total_size += size * len(ordinals)
        count += len(ordinals)
        previous_ordinal = -1
        for ordinal in ordinals:
            if type(ordinal) is not int or not previous_ordinal < ordinal < MAX_FILES:
                raise_private_metadata_error(GraphStageInventoryRejected())
            position = 1 << ordinal
            if occupied & position:
                raise_private_metadata_error(GraphStageInventoryRejected())
            occupied |= position
            previous_ordinal = ordinal
        expected[(store, digest)] = size, ordinals
    if occupied != (1 << count) - 1 or total_size > MAX_CONTENT_BYTES:
        raise_private_metadata_error(GraphStageInventoryRejected())
    return expected


def _match_stages(
    expected: _ExpectedStages, inventory: GraphStageInventory
) -> tuple[MatchedGraphStage, ...]:
    if len(expected) != len(inventory.stages):
        raise_private_metadata_error(GraphStageInventoryRejected())
    stages = {
        (stage.store_id, stage.payload_digest): stage for stage in inventory.stages
    }
    ordinals: dict[str, list[int]] = {stage.stage_id: [] for stage in inventory.stages}
    for occurrence in inventory.occurrences:
        ordinals[occurrence.stage_id].append(occurrence.ordinal)
    matched: list[MatchedGraphStage] = []
    for address, (size, expected_ordinals) in expected.items():
        stage = stages.get(address)
        if (
            stage is None
            or stage.total_size != size
            or tuple(ordinals[stage.stage_id]) != expected_ordinals
        ):
            raise_private_metadata_error(GraphStageInventoryRejected())
        matched.append(
            MatchedGraphStage(
                stage.stage_id,
                *address,
                stage.fence_incarnation,
                size,
                expected_ordinals,
            )
        )
    return tuple(matched)


async def match_graph_publication_stages(
    connection: AsyncConnection,
    locks: TransitionLocks,
    binding: GraphPreparationBinding,
    selected_graph_id: str,
    prepared: PreparedGraphDeploymentInputs,
) -> tuple[MatchedGraphStage, ...]:
    require_graph_preparation_binding(binding)
    original = replace(binding)
    locks.require_publication_graph_preparation(connection, original, selected_graph_id)
    expected = _expected_stages(prepared, original.deployment_digest, connection, locks)
    del prepared, binding
    inventory = await read_graph_stage_inventory(
        connection, GraphStageScope(original.preparation_id, original.namespace_id)
    )
    locks.require_publication_graph_preparation(connection, original, selected_graph_id)
    return _match_stages(expected, inventory)
