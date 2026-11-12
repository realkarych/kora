from kora.api.graph_deployment_attachment import GraphDeploymentAttachmentLimits
from kora.api.graph_execution_preparation import (
    ExecutionPreparationLimits,
    GraphDeploymentPreparationLimits,
)
from kora.api.graph_publication_values import GraphPublicationBindLimits
from kora.db.types import DIGEST_LENGTH, IDENTIFIER_LENGTH
from kora.engine.graph_cel_facts import GraphCelReadLimits
from kora.engine.graph_command_manifests import CommandManifestLimits
from kora.engine.graph_execution_contract import ExecutionContractReadLimits
from kora.engine.graph_projection_paths import GraphProjectionReadLimits
from kora.engine.graph_resource_references import GraphResourceReferenceLimits
from kora.engine.graph_version_identity import (
    GraphVersionIdentityLimits,
    graph_version_hash_bytes,
)

MAX_REQUEST_BYTES: int = 4 * 1024 * 1024
IR_BYTES: int = MAX_REQUEST_BYTES
PAIR_BYTES: int = 4 * IR_BYTES
SECTION_BYTES: int = IR_BYTES
RESOURCE_RECORD_BYTES: int = 4096
SCOPE_UNITS: int = 4 * 3 * IDENTIFIER_LENGTH
BIND_SLOTS: int = 32
ATTACHMENT_SLOTS: int = 36

IDENTITY = GraphVersionIdentityLimits(
    PAIR_BYTES, graph_version_hash_bytes(PAIR_BYTES, 0)
)
CEL = GraphCelReadLimits(
    ir_bytes=IR_BYTES,
    output_context_bytes=SECTION_BYTES,
    parameter_context_bytes=SECTION_BYTES,
    comparison_bytes=SECTION_BYTES,
    declarations=1024,
    triggers=64,
    typed_work=SECTION_BYTES,
    typed_bytes=SECTION_BYTES,
)
PROJECTION = GraphProjectionReadLimits(
    cel=CEL,
    retry_bytes=SECTION_BYTES,
    source_bytes=20 * SECTION_BYTES,
    section_bytes=SECTION_BYTES,
    work=SECTION_BYTES,
    scratch_bytes=10 * SECTION_BYTES,
)
LOGICAL = CommandManifestLimits(IR_BYTES, 10 * IR_BYTES, 20 * IR_BYTES)
RESOURCES = GraphResourceReferenceLimits(
    LOGICAL, 20 * IR_BYTES, 40 * IR_BYTES, 80 * IR_BYTES
)
READ = ExecutionContractReadLimits(
    IDENTITY,
    PROJECTION,
    RESOURCES,
    RESOURCE_RECORD_BYTES,
    1 << 30,
    1 << 52,
)
EXECUTION = ExecutionPreparationLimits(
    READ, SECTION_BYTES, SECTION_BYTES, SECTION_BYTES, 1 << 31, 1 << 55
)
CONTENTS = CommandManifestLimits(IR_BYTES, 10 * IR_BYTES, 40 * IR_BYTES)
DEPLOYMENT = GraphDeploymentPreparationLimits(
    EXECUTION,
    CONTENTS,
    EXECUTION.max_retained_bytes + CONTENTS.ir_bytes + CONTENTS.scratch + 32,
    EXECUTION.max_work + CONTENTS.work + 32,
)
ATTACHMENT = GraphDeploymentAttachmentLimits(
    RESOURCES,
    RESOURCE_RECORD_BYTES,
    ATTACHMENT_SLOTS
    + PAIR_BYTES
    + 3 * DIGEST_LENGTH
    + RESOURCES.source_bytes
    + RESOURCES.scratch
    + LOGICAL.ir_bytes
    + LOGICAL.scratch,
    ATTACHMENT_SLOTS + RESOURCES.work + LOGICAL.work,
)
BINDS = GraphPublicationBindLimits(
    2 * PAIR_BYTES + 4 + RESOURCE_RECORD_BYTES,
    3 * PAIR_BYTES + 4 + RESOURCE_RECORD_BYTES + BIND_SLOTS + SCOPE_UNITS,
    12 * PAIR_BYTES + 8 + 2 * RESOURCE_RECORD_BYTES + BIND_SLOTS + SCOPE_UNITS,
)
