from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    UniqueConstraint,
    func,
    literal_column,
    text,
)
from sqlalchemy.types import TypeEngine

from kora.db.admission_discovery_v1 import CANDIDATE_ROWS
from kora.db.admission_discovery_v1 import INDEX_NAME as ADMISSION_DISCOVERY_INDEX
from kora.db.admission_wait_v1 import INDEXES as WAIT_INDEXES
from kora.db.admission_wait_v1 import wait_index_dialect
from kora.db.attempt_capacity_v1 import INDEXES as CAPACITY_INDEXES
from kora.db.attempt_capacity_v1 import capacity_index_dialect
from kora.db.bootstrap_initialization_v1 import (
    TABLE_NAME as BOOTSTRAP_INITIALIZATION_TABLE,
)
from kora.db.bootstrap_initialization_v1 import (
    bootstrap_initialization_elements_v1,
)
from kora.db.claim_sources_v1 import LOCATORS, SQLITE_KEY, claim_source_elements
from kora.db.claim_sources_v1 import TABLE_NAME as CLAIM_SOURCE_TABLE
from kora.db.deadline_discovery_v1 import INDEXES as DEADLINE_INDEXES
from kora.db.grant_history_v1 import (
    attempt_history_columns,
    dispatcher_history_columns,
    dispatcher_history_indexes,
    lease_epoch_type,
)
from kora.db.graph_deployment_stage_v1 import (
    OCCURRENCE_TABLE,
    STAGE_TABLE,
    graph_deployment_stage_elements_v1,
    graph_deployment_stage_occurrence_elements_v1,
)
from kora.db.graph_preparation_capacity_v1 import (
    TABLE_NAME as PREPARATION_CAPACITY_TABLE,
)
from kora.db.graph_preparation_capacity_v1 import graph_preparation_capacity_elements_v1
from kora.db.graph_preparation_v1 import TABLE_NAME as GRAPH_PREPARATION_TABLE
from kora.db.graph_preparation_v1 import graph_preparation_elements_v1
from kora.db.graph_publication_digest_v1 import (
    DIGEST_INDEX,
    INVALID_ID_INDEX,
    SQLITE_INVALID,
    SQLITE_VALID,
)
from kora.db.graph_publication_ordinal_v1 import (
    ORDINAL_INVALID_INDEX,
    ORDINAL_VALID_INDEX,
    SQLITE_ORDINAL_INVALID,
    SQLITE_ORDINAL_VALID,
)
from kora.db.graph_stage_promotion_custody_v1 import (
    INTENT_TABLE as PROMOTION_INTENT_TABLE,
)
from kora.db.graph_stage_promotion_custody_v1 import (
    SETTLEMENT_TABLE as PROMOTION_SETTLEMENT_TABLE,
)
from kora.db.graph_stage_promotion_custody_v1 import (
    graph_stage_promotion_intent_elements_v1,
    graph_stage_promotion_settlement_elements_v1,
)
from kora.db.graph_version_resource_owner_v1 import (
    TABLE_NAME as GRAPH_VERSION_RESOURCE_OWNER_TABLE,
)
from kora.db.graph_version_resource_owner_v1 import (
    graph_version_resource_owner_elements_v1,
)
from kora.db.promotion_admission_v1 import TABLE_NAME as PROMOTION_ADMISSION_TABLE
from kora.db.promotion_admission_v1 import promotion_admission_elements
from kora.db.run_input_locator_v1 import (
    SQLITE_RUN_INPUT_KEY,
    SQLITE_RUN_INPUT_LOCATOR_INDEX,
    SQLITE_RUN_KEY,
    SQLITE_RUN_LOCATOR_INDEX,
)
from kora.db.run_inputs_v1 import TABLE_NAME as RUN_INPUTS_TABLE
from kora.db.run_inputs_v1 import run_input_elements_v1
from kora.db.run_queue_index_v1 import (
    ACTIVE_QUEUE_ROWS,
    POSTGRES_QUEUE_INDEX,
    POSTGRES_QUEUE_KEY,
    SQLITE_QUEUE_INDEX,
    SQLITE_QUEUE_KEY,
    SQLITE_QUEUE_ROWS,
)
from kora.db.storage_address_fence_v1 import TABLE_NAME as STORAGE_ADDRESS_FENCE_TABLE
from kora.db.storage_address_fence_v1 import storage_address_fence_elements_v1
from kora.db.types import (
    DIGEST_LENGTH,
    IDENTIFIER_LENGTH,
    NAME_LENGTH,
    PATH_LENGTH,
    JsonDocument,
    OutcomePayloadText,
    UsdAmount,
    UtcTimestamp,
    collated_text,
)
from kora.db.usage_source import UsageSourceText
from kora.db.worker_registration_v1 import (
    EVENT_NAMESPACE_SCOPE,
    domain_elements,
    incarnation_elements,
)

NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata: MetaData = MetaData(naming_convention=NAMING_CONVENTION)

OUTCOME_PRESENCE_CHECK: str = (
    "(outcome IS NULL AND outcome_payload IS NULL "
    "AND outcome_payload_state IS NULL) OR "
    "(outcome IS NOT NULL AND outcome_payload_state IS NOT NULL AND "
    "((outcome_payload_state = 'not_applicable' AND outcome_payload IS NULL) OR "
    "(outcome_payload_state IN ('available', 'expiring') "
    "AND outcome_payload IS NOT NULL) OR "
    "(outcome_payload_state IN ('expired', 'missing') "
    "AND outcome_payload IS NULL)))"
)


DEADLINE_BINDING_CHECK: str = (
    "deadline_binding IS NULL OR "
    "substr(ltrim(CAST(deadline_binding AS TEXT), ' \t\r\n'), 1, 1) = '{'"
)


NON_TERMINAL_ATTEMPT_STATUSES: tuple[str, ...] = (
    "created",
    "claimed",
    "submitted",
    "running",
    "finishing",
)
FINISHED_NODE_STATUSES: tuple[str, ...] = (
    "succeeded",
    "skipped",
    "upstream_failed",
    "cancelled",
    "failed",
)
OUTSTANDING_RUN_STATUSES: tuple[str, ...] = ("pending", "running")


def identifier() -> TypeEngine[str]:
    return collated_text(IDENTIFIER_LENGTH)


def digest() -> TypeEngine[str]:
    return collated_text(DIGEST_LENGTH)


def name() -> TypeEngine[str]:
    return collated_text(NAME_LENGTH)


def path() -> TypeEngine[str]:
    return collated_text(PATH_LENGTH)


def primary_identifier() -> Column[str]:
    return Column("id", identifier(), primary_key=True)


def timestamp(column_name: str, nullable: bool = False) -> Column[object]:
    return Column(column_name, UtcTimestamp(), nullable=nullable)


def document(column_name: str, nullable: bool = False) -> Column[object]:
    return Column(column_name, JsonDocument(), nullable=nullable)


def flag(column_name: str) -> Column[bool]:
    return Column(
        column_name,
        Boolean(create_constraint=True, name=f"{column_name}_is_boolean"),
        nullable=False,
    )


def reference(column_name: str, target: str, nullable: bool = False) -> Column[str]:
    return Column(column_name, identifier(), ForeignKey(target), nullable=nullable)


def audit_columns() -> list[Column[object]]:
    return [timestamp("created_at"), timestamp("updated_at")]


namespace: Table = Table(
    "namespace",
    metadata,
    primary_identifier(),
    Column("name", name(), nullable=False),
    Column("policy_revision", Integer, nullable=False),
    document("policy"),
    *audit_columns(),
    UniqueConstraint("name"),
)

graph: Table = Table(
    "graph",
    metadata,
    primary_identifier(),
    reference("namespace_id", "namespace.id"),
    Column("name", name(), nullable=False),
    *audit_columns(),
    UniqueConstraint("namespace_id", "name"),
)

graph_version: Table = Table(
    "graph_version",
    metadata,
    primary_identifier(),
    reference("graph_id", "graph.id"),
    Column("number", Integer, nullable=False),
    document("ir"),
    Column("ir_digest", digest(), nullable=False),
    Column("source_digest", digest(), nullable=False),
    document("execution_contract"),
    Column("value_encoding_version", Integer),
    CheckConstraint(
        "value_encoding_version IS NULL OR value_encoding_version = 1",
        name="value_encoding_version",
    ),
    Column("execution_contract_digest", digest(), nullable=False),
    Column("deployment_digest", digest(), nullable=False),
    document("resolved_digests"),
    document("resources"),
    Column("deployed_by", name(), nullable=False),
    timestamp("deployed_at"),
    Column("status", name(), nullable=False),
    *audit_columns(),
    UniqueConstraint("graph_id", "number"),
)

GRAPH_VERSION_SCOPE_INDEX: str = "ix_graph_version_graph_id_id"
Index(GRAPH_VERSION_SCOPE_INDEX, graph_version.c.graph_id, graph_version.c.id).ddl_if(
    dialect="sqlite"
)
Index(
    DIGEST_INDEX,
    graph_version.c.graph_id,
    graph_version.c.deployment_digest,
    graph_version.c.id,
    sqlite_where=literal_column(SQLITE_VALID),
)
Index(
    INVALID_ID_INDEX,
    graph_version.c.graph_id,
    graph_version.c.deployment_digest,
    sqlite_where=literal_column(SQLITE_INVALID),
).ddl_if(dialect="sqlite")

Index(
    ORDINAL_VALID_INDEX,
    graph_version.c.graph_id,
    graph_version.c.number,
    sqlite_where=literal_column(SQLITE_ORDINAL_VALID),
).ddl_if(dialect="sqlite")
Index(
    ORDINAL_INVALID_INDEX,
    graph_version.c.graph_id,
    sqlite_where=literal_column(SQLITE_ORDINAL_INVALID),
).ddl_if(dialect="sqlite")

graph_version_resource_owner: Table = Table(
    GRAPH_VERSION_RESOURCE_OWNER_TABLE,
    metadata,
    *graph_version_resource_owner_elements_v1(),
)

graph_version_read_admission: Table = Table(
    "graph_version_read_admission",
    metadata,
    Column(
        "graph_version_id",
        identifier(),
        ForeignKey("graph_version.id"),
        primary_key=True,
    ),
    Column("format_version", Integer, nullable=False),
    Column("db_input_bytes", BigInteger, nullable=False),
    Column("resource_transport_bytes", BigInteger, nullable=False),
    CheckConstraint("format_version = 1", name="format_version"),
    CheckConstraint("db_input_bytes > 0", name="db_input_bytes"),
    CheckConstraint("resource_transport_bytes > 0", name="resource_transport_bytes"),
    CheckConstraint(
        "typeof(format_version) = 'integer' AND typeof(db_input_bytes) = 'integer' "
        "AND typeof(resource_transport_bytes) = 'integer'",
        name="integer_storage",
    ).ddl_if(dialect="sqlite"),
)

graph_deployment_preparation: Table = Table(
    GRAPH_PREPARATION_TABLE, metadata, *graph_preparation_elements_v1()
)

graph_deployment_preparation_capacity: Table = Table(
    PREPARATION_CAPACITY_TABLE, metadata, *graph_preparation_capacity_elements_v1()
)

storage_address_fence: Table = Table(
    STORAGE_ADDRESS_FENCE_TABLE,
    metadata,
    *storage_address_fence_elements_v1(),
    sqlite_autoincrement=True,
)

graph_deployment_stage: Table = Table(
    STAGE_TABLE, metadata, *graph_deployment_stage_elements_v1()
)

graph_deployment_stage_occurrence: Table = Table(
    OCCURRENCE_TABLE, metadata, *graph_deployment_stage_occurrence_elements_v1()
)

graph_deployment_stage_promotion_intent: Table = Table(
    PROMOTION_INTENT_TABLE, metadata, *graph_stage_promotion_intent_elements_v1()
)

graph_deployment_stage_promotion_settlement: Table = Table(
    PROMOTION_SETTLEMENT_TABLE,
    metadata,
    *graph_stage_promotion_settlement_elements_v1(),
)

preset: Table = Table(
    "preset",
    metadata,
    primary_identifier(),
    reference("namespace_id", "namespace.id"),
    Column("name", name(), nullable=False),
    Column("version", name(), nullable=False),
    document("body"),
    *audit_columns(),
    UniqueConstraint("namespace_id", "name", "version"),
)

artifact_type: Table = Table(
    "artifact_type",
    metadata,
    primary_identifier(),
    reference("namespace_id", "namespace.id", nullable=True),
    Column("name", name(), nullable=False),
    document("schema"),
    Column("parent", name(), nullable=True),
    document("behavior"),
    *audit_columns(),
    Index(
        "ix_artifact_type_namespace_name",
        func.coalesce(text("namespace_id"), ""),
        "name",
        unique=True,
    ),
)

api_token: Table = Table(
    "api_token",
    metadata,
    primary_identifier(),
    Column("kind", name(), nullable=False),
    Column("digest", digest(), nullable=False),
    Column("owner", name(), nullable=False),
    document("namespaces"),
    flag("approve"),
    flag("platform"),
    Column("runtime", name(), nullable=True),
    timestamp("expires_at", nullable=True),
    *audit_columns(),
    UniqueConstraint("digest"),
)

bootstrap_initialization: Table = Table(
    BOOTSTRAP_INITIALIZATION_TABLE,
    metadata,
    *bootstrap_initialization_elements_v1(),
)

worker: Table = Table(
    "worker",
    metadata,
    Column("id", name(), primary_key=True),
    Column("incarnation_id", collated_text(26, postgresql_length=256), nullable=False),
    Column(
        "execution_domain_id", collated_text(26, postgresql_length=256), nullable=False
    ),
    Column("runtime", name(), nullable=False),
    Column("version", name(), nullable=False),
    Column("capacity", Integer, nullable=False),
    timestamp("last_seen_at"),
    reference("token_id", "api_token.id"),
    *audit_columns(),
)

worker_incarnation: Table = Table(
    "worker_incarnation", metadata, *incarnation_elements()
)
worker_execution_domain: Table = Table(
    "worker_execution_domain", metadata, *domain_elements()
)

idempotency_record: Table = Table(
    "idempotency_record",
    metadata,
    reference("token_id", "api_token.id"),
    Column("key", name(), nullable=False),
    Column("request_digest", digest(), nullable=False),
    document("response"),
    timestamp("expires_at"),
    *audit_columns(),
    PrimaryKeyConstraint("token_id", "key"),
)

trigger_binding: Table = Table(
    "trigger_binding",
    metadata,
    primary_identifier(),
    reference("graph_id", "graph.id"),
    Column("kind", name(), nullable=False),
    document("spec"),
    timestamp("next_fire_at", nullable=True),
    Column("last_fired_key", name(), nullable=True),
    Column("binding_revision", Integer, nullable=False),
    document("observation", nullable=True),
    document("evaluation_error", nullable=True),
    *audit_columns(),
    Index("ix_trigger_binding_next_fire_at", "next_fire_at"),
)

run: Table = Table(
    "run",
    metadata,
    primary_identifier(),
    reference("namespace_id", "namespace.id"),
    reference("graph_id", "graph.id"),
    reference("graph_version_id", "graph_version.id"),
    reference("rerun_of_run_id", "run.id", nullable=True),
    Column("idempotency_key", name(), nullable=False),
    Column(
        "target_key", collated_text(NAME_LENGTH, postgresql_length=256), nullable=True
    ),
    timestamp("target_held_since", nullable=True),
    document("params"),
    document("cel_params_projection"),
    flag("allow_unenforced_secrets"),
    Column("status", name(), nullable=False),
    flag("needs_scheduling"),
    document("trigger"),
    document("limits"),
    document("retention"),
    Column("retention_version", Integer, nullable=True),
    timestamp("started_at", nullable=True),
    timestamp("deadline_expired_at", nullable=True),
    timestamp("finished_at", nullable=True),
    *audit_columns(),
    Column("input_encoding_version", Integer, nullable=True),
    UniqueConstraint("graph_id", "idempotency_key"),
    CheckConstraint(
        "retention_version IS NULL OR retention_version = 1", name="retention_version"
    ),
    Index("ix_run_namespace_target", "namespace_id", "target_key"),
    Index("ix_run_status_id", "status", "id"),
    Index(
        "ix_run_needs_scheduling",
        "id",
        postgresql_where=text("needs_scheduling"),
        sqlite_where=text("needs_scheduling = 1"),
    ),
)

Index(
    POSTGRES_QUEUE_INDEX,
    run.c.namespace_id,
    run.c.graph_id,
    run.c.status,
    literal_column(f"(\n{POSTGRES_QUEUE_KEY})", String()),
    postgresql_where=literal_column(ACTIVE_QUEUE_ROWS),
).ddl_if(dialect="postgresql")
Index(
    SQLITE_QUEUE_INDEX,
    run.c.namespace_id,
    run.c.graph_id,
    run.c.status,
    literal_column(f"({SQLITE_QUEUE_KEY})", String()),
    sqlite_where=literal_column(SQLITE_QUEUE_ROWS),
).ddl_if(dialect="sqlite")

run_inputs: Table = Table(RUN_INPUTS_TABLE, metadata, *run_input_elements_v1())

Index(
    SQLITE_RUN_LOCATOR_INDEX,
    run.c.id,
    sqlite_where=literal_column(SQLITE_RUN_KEY),
).ddl_if(dialect="sqlite")
Index(
    SQLITE_RUN_INPUT_LOCATOR_INDEX,
    run_inputs.c.run_id,
    sqlite_where=literal_column(SQLITE_RUN_INPUT_KEY),
).ddl_if(dialect="sqlite")

node_instance: Table = Table(
    "node_instance",
    metadata,
    primary_identifier(),
    reference("run_id", "run.id"),
    Column("node_path", path(), nullable=False),
    Column("kind", name(), nullable=False),
    Column("status", name(), nullable=False),
    Column("trigger_rule", name(), nullable=False),
    Column("subgraph_attempt", Integer, nullable=False),
    flag("top_level"),
    document("consumed_versions"),
    Column("current_attempt_id", identifier(), nullable=True),
    document("branch_selection", nullable=True),
    Column("attempt_count", Integer, nullable=False),
    timestamp("started_at", nullable=True),
    timestamp("deadline_expired_at", nullable=True),
    timestamp("provider_wait_deadline", nullable=True),
    *audit_columns(),
    UniqueConstraint("run_id", "node_path"),
    Index("ix_node_instance_run_top_level", "run_id", "top_level"),
    Index(
        ADMISSION_DISCOVERY_INDEX,
        "id",
        sqlite_where=literal_column(CANDIDATE_ROWS),
        postgresql_where=literal_column(CANDIDATE_ROWS),
    ),
)

attempt: Table = Table(
    "attempt",
    metadata,
    primary_identifier(),
    reference("node_instance_id", "node_instance.id"),
    Column("number", Integer, nullable=False),
    Column("runtime", name(), nullable=False),
    Column("worker_id", name(), ForeignKey("worker.id"), nullable=True),
    Column(
        "execution_domain_id", collated_text(26, postgresql_length=256), nullable=True
    ),
    Column("execution_state", name(), nullable=False),
    Column("recovery_version", Integer, nullable=False),
    flag("recovery_required"),
    document("recovery", nullable=True),
    document("handle", nullable=True),
    Column("status", name(), nullable=False),
    Column("desired_state", name(), nullable=False),
    Column("cancel_cause", name(), nullable=True),
    document("outcome", nullable=True),
    Column("outcome_payload", OutcomePayloadText(), nullable=True),
    Column("outcome_payload_state", name(), nullable=True),
    CheckConstraint(OUTCOME_PRESENCE_CHECK, name="outcome_payload_presence"),
    Column("record_version", Integer, nullable=False),
    document("input_bindings"),
    document("execution_request", nullable=True),
    document("execution_policy", nullable=True),
    Column("error_class", name(), nullable=True),
    document("error", nullable=True),
    Column("strategy", name(), nullable=True),
    Column("strategy_fallback", name(), nullable=True),
    Column("infrastructure_retries", Integer, nullable=False),
    Column("lease_owner", name(), nullable=True),
    Column("lease_epoch", lease_epoch_type(), nullable=False),
    timestamp("lease_until", nullable=True),
    timestamp("last_heartbeat_at", nullable=True),
    document("counters"),
    document("scratch"),
    Column("resolved_config_digest", digest(), nullable=True),
    Column("price_table_id", identifier(), nullable=True),
    document("usage_coverage", nullable=True),
    Column("image_digest", name(), nullable=True),
    Column("harness_version", name(), nullable=True),
    timestamp("started_at", nullable=True),
    timestamp("deadline", nullable=True),
    document("deadline_binding", nullable=True),
    CheckConstraint(
        DEADLINE_BINDING_CHECK,
        name="deadline_binding_object",
    ),
    timestamp("deadline_expired_at", nullable=True),
    timestamp("finished_at", nullable=True),
    *attempt_history_columns(),
    *audit_columns(),
    UniqueConstraint("node_instance_id", "number"),
    CheckConstraint(
        "cancel_cause IS NULL OR cancel_cause IN "
        "('cancel', 'clear', 'supersede', 'timeout', 'stop')",
        name="cancel_cause",
    ),
    Index(
        "ix_attempt_lease_until",
        "lease_until",
        postgresql_where=text(
            "status IN ('claimed', 'submitted', 'running', 'finishing')"
        ),
        sqlite_where=text("status IN ('claimed', 'submitted', 'running', 'finishing')"),
    ),
)

attempt.append_column(Column("acknowledged_segment_offset", BigInteger, nullable=True))
attempt_claim_read_admission: Table = Table(
    CLAIM_SOURCE_TABLE, metadata, *claim_source_elements()
)
for owner, key, locator in LOCATORS:
    Index(
        locator,
        metadata.tables[owner].c[key],
        sqlite_where=literal_column(SQLITE_KEY.replace("id", key)),
    ).ddl_if(dialect="sqlite")

attempt_dispatcher: Table = Table(
    "attempt_dispatcher",
    metadata,
    primary_identifier(),
    reference("attempt_id", "attempt.id"),
    Column(
        "execution_domain_id", collated_text(26, postgresql_length=256), nullable=False
    ),
    Column("worker_id", name(), ForeignKey("worker.id"), nullable=False),
    Column("incarnation_id", collated_text(26, postgresql_length=256), nullable=False),
    Column("lease_epoch", lease_epoch_type(), nullable=False),
    Column("mode", name(), nullable=False),
    timestamp("granted_at"),
    timestamp("closed_at", nullable=True),
    document("evidence", nullable=True),
    *audit_columns(),
    *dispatcher_history_columns(),
    UniqueConstraint("attempt_id", "lease_epoch"),
    *dispatcher_history_indexes(),
)

wait: Table = Table(
    "wait",
    metadata,
    primary_identifier(),
    reference("node_instance_id", "node_instance.id"),
    reference("attempt_id", "attempt.id", nullable=True),
    Column("kind", name(), nullable=False),
    timestamp("wake_at", nullable=True),
    document("subscription", nullable=True),
    document("reason", nullable=True),
    Column("status", name(), nullable=False),
    document("observation", nullable=True),
    document("evaluation_error", nullable=True),
    *audit_columns(),
    Index("ix_wait_status_wake_at", "status", "wake_at"),
)

approval_request: Table = Table(
    "approval_request",
    metadata,
    primary_identifier(),
    reference("node_instance_id", "node_instance.id"),
    reference("wait_id", "wait.id"),
    Column("channel", name(), nullable=False),
    Column("channel_request_id", name(), nullable=True),
    document("delivery", nullable=True),
    document("form"),
    Column("context_artifact_version_id", identifier(), nullable=True),
    Column("bound_commit_sha", name(), nullable=True),
    document("bound_digests", nullable=True),
    Column("observed_head_sha", name(), nullable=True),
    document("run_cost"),
    Column("run_cost_version", Integer, nullable=True),
    CheckConstraint(
        "run_cost_version IS NULL OR run_cost_version = 1", name="run_cost_version"
    ),
    Column("status", name(), nullable=False),
    document("response", nullable=True),
    document("approver", nullable=True),
    document("evaluation_error", nullable=True),
    *audit_columns(),
)

for index_name, owner_table, index_columns, predicate, _ in DEADLINE_INDEXES:
    Index(
        index_name,
        *(metadata.tables[owner_table].c[column] for column in index_columns),
        postgresql_where=None if predicate is None else literal_column(predicate),
        sqlite_where=None if predicate is None else literal_column(predicate),
    )

for (
    index_name,
    owner_table,
    index_columns,
    sqlite_filter,
    pg_filter,
    _,
) in (*CAPACITY_INDEXES, *WAIT_INDEXES):
    Index(
        index_name,
        *(metadata.tables[owner_table].c[column] for column in index_columns),
        postgresql_where=None if pg_filter is None else literal_column(pg_filter),
        sqlite_where=None if sqlite_filter is None else literal_column(sqlite_filter),
    ).ddl_if(
        dialect=capacity_index_dialect(index_name) or wait_index_dialect(index_name)
    )

artifact_version: Table = Table(
    "artifact_version",
    metadata,
    primary_identifier(),
    reference("run_id", "run.id"),
    reference("node_instance_id", "node_instance.id"),
    reference("attempt_id", "attempt.id", nullable=True),
    Column("output_name", name(), nullable=False),
    Column("type_name", name(), nullable=False),
    Column("contract_digest", digest(), nullable=False),
    document("validation_result"),
    document("digest_selection"),
    document("derived_payload", nullable=True),
    document("cel_projection", nullable=True),
    Column("cel_projection_set", digest(), nullable=True),
    Column("digest", digest(), nullable=False),
    Column("storage_ref", path(), nullable=True),
    Column("size", BigInteger, nullable=False),
    document("inline", nullable=True),
    Column("payload_digest", digest(), nullable=False),
    Column("status", name(), nullable=False),
    Column("payload_state", name(), nullable=False),
    Column("worker_id", name(), nullable=True),
    Column("image_digest", name(), nullable=True),
    *audit_columns(),
    Index("ix_artifact_version_node_output", "node_instance_id", "output_name"),
)

artifact_lineage: Table = Table(
    "artifact_lineage",
    metadata,
    reference("artifact_version_id", "artifact_version.id"),
    Column("input_name", name(), nullable=False),
    reference("consumed_artifact_version_id", "artifact_version.id"),
    Column("relation", name(), nullable=False),
    *audit_columns(),
    PrimaryKeyConstraint(
        "artifact_version_id", "input_name", "consumed_artifact_version_id"
    ),
    Index("ix_artifact_lineage_consumed", "consumed_artifact_version_id"),
)

usage_record: Table = Table(
    "usage_record",
    metadata,
    primary_identifier(),
    reference("attempt_id", "attempt.id"),
    Column("model", collated_text(256), nullable=False),
    Column("input_tokens", Integer, nullable=False),
    Column("cached_input_tokens", Integer, nullable=False),
    Column("cache_creation_tokens", Integer, nullable=False),
    Column("output_tokens", Integer, nullable=False),
    Column("reasoning_tokens", Integer, nullable=False),
    Column("completeness", name(), nullable=False),
    Column("basis", name(), nullable=False),
    Column("billing_class", name(), nullable=False),
    Column("usd_estimate", UsdAmount(), nullable=True),
    Column("same_basis_usd_floor", UsdAmount(), nullable=True),
    Column("reported_usd", UsdAmount(), nullable=True),
    document("known_token_fields", nullable=True),
    document("cache_creation_by_ttl", nullable=True),
    Column("observation_id", collated_text(256), nullable=True),
    Column("observation_sequence", BigInteger, nullable=True),
    timestamp("observed_at", nullable=True),
    Column("dispatcher_id", identifier(), nullable=True),
    Column("normalization_version", Integer, nullable=True),
    CheckConstraint(
        "(normalization_version IS NULL AND reported_usd IS NULL AND "
        "known_token_fields IS NULL AND cache_creation_by_ttl IS NULL AND "
        "observation_id IS NULL AND observation_sequence IS NULL AND "
        "observed_at IS NULL AND dispatcher_id IS NULL) OR "
        "(normalization_version IS NOT NULL AND normalization_version = 1 AND "
        "known_token_fields IS NOT NULL AND observation_id IS NOT NULL AND "
        "observation_sequence IS NOT NULL AND observed_at IS NOT NULL AND "
        "observation_sequence >= 0 AND observation_sequence <= 9007199254740991 AND "
        "input_tokens >= 0 AND input_tokens <= 2147483647 AND "
        "cached_input_tokens >= 0 AND cached_input_tokens <= 2147483647 AND "
        "cache_creation_tokens >= 0 AND cache_creation_tokens <= 2147483647 AND "
        "output_tokens >= 0 AND output_tokens <= 2147483647 AND "
        "reasoning_tokens >= 0 AND reasoning_tokens <= 2147483647 AND "
        "reasoning_tokens <= output_tokens AND "
        "input_tokens = CAST(input_tokens AS BIGINT) AND "
        "cached_input_tokens = CAST(cached_input_tokens AS BIGINT) AND "
        "cache_creation_tokens = CAST(cache_creation_tokens AS BIGINT) AND "
        "output_tokens = CAST(output_tokens AS BIGINT) AND "
        "reasoning_tokens = CAST(reasoning_tokens AS BIGINT) AND "
        "observation_sequence = CAST(observation_sequence AS BIGINT))",
        name="normalization",
    ),
    flag("delta_of_session_totals"),
    Column("source", UsageSourceText(), nullable=False),
    Column("worker_id", name(), nullable=True),
    Column("image_digest", name(), nullable=True),
    *audit_columns(),
    UniqueConstraint("attempt_id", "model"),
)

price_table: Table = Table(
    "price_table",
    metadata,
    primary_identifier(),
    reference("namespace_id", "namespace.id", nullable=True),
    Column("model", name(), nullable=True),
    Column("version", Integer, nullable=True),
    Column("normalization_version", Integer, nullable=True),
    timestamp("effective_from"),
    document("prices"),
    CheckConstraint(
        "(version IS NULL AND normalization_version IS NULL AND model IS NOT NULL) OR "
        "(version IS NOT NULL AND normalization_version IS NOT NULL AND "
        "version = 1 AND normalization_version = 1 AND model IS NULL)",
        name="revision",
    ),
    Index(
        "ix_price_table_default_effective",
        "effective_from",
        unique=True,
        postgresql_where=text("version = 1 AND namespace_id IS NULL"),
        sqlite_where=text("version = 1 AND namespace_id IS NULL"),
    ),
    Index(
        "ix_price_table_namespace_effective",
        "namespace_id",
        "effective_from",
        unique=True,
        postgresql_where=text("version = 1 AND namespace_id IS NOT NULL"),
        sqlite_where=text("version = 1 AND namespace_id IS NOT NULL"),
    ),
    *audit_columns(),
)

session_segment: Table = Table(
    "session_segment",
    metadata,
    reference("attempt_id", "attempt.id"),
    Column("offset", Integer, nullable=False),
    Column("count", Integer, nullable=False),
    Column("storage_ref", path(), nullable=False),
    flag("truncated"),
    Column("payload_state", name(), nullable=False),
    timestamp("uploaded_at"),
    *audit_columns(),
    PrimaryKeyConstraint("attempt_id", "offset"),
)

automation_principal: Table = Table(
    "automation_principal",
    metadata,
    Column("binding", name(), nullable=False),
    Column("binding_revision", Integer, nullable=False),
    Column("provider_instance", name(), nullable=False),
    Column("stable_id", name(), nullable=False),
    Column("kind", name(), nullable=False),
    Column("display_name", name(), nullable=False),
    timestamp("first_used_at"),
    *audit_columns(),
    PrimaryKeyConstraint(
        "binding", "binding_revision", "provider_instance", "stable_id"
    ),
)

event: Table = Table(
    "event",
    metadata,
    primary_identifier(),
    reference("namespace_id", "namespace.id", nullable=True),
    reference("run_id", "run.id", nullable=True),
    reference("node_instance_id", "node_instance.id", nullable=True),
    reference("attempt_id", "attempt.id", nullable=True),
    Column("kind", name(), nullable=False),
    document("actor"),
    document("payload"),
    timestamp("at"),
    *audit_columns(),
    Index("ix_event_run_id_id", "run_id", "id"),
    CheckConstraint(EVENT_NAMESPACE_SCOPE, name="namespace_scope"),
)

provider_pause: Table = Table(
    "provider_pause",
    metadata,
    Column("quota_resource", name(), nullable=False),
    Column("quota_group", name(), nullable=False),
    timestamp("until"),
    Column("source_attempt_id", identifier(), nullable=True),
    Column("reason", name(), nullable=False),
    timestamp("released_at", nullable=True),
    *audit_columns(),
    PrimaryKeyConstraint("quota_resource", "quota_group"),
)

inbound_event: Table = Table(
    "inbound_event",
    metadata,
    primary_identifier(),
    reference("namespace_id", "namespace.id"),
    Column("name", name(), nullable=False),
    Column("idempotency_key", name(), nullable=False),
    document("payload"),
    Column("auth_kind", name(), nullable=False),
    Column("nonce", name(), nullable=True),
    timestamp("received_at"),
    document("start_outcome", nullable=True),
    document("consumed_by"),
    *audit_columns(),
    UniqueConstraint("namespace_id", "name", "idempotency_key"),
)

run_promotion_admission: Table = Table(
    PROMOTION_ADMISSION_TABLE, metadata, *promotion_admission_elements()
)

event_evaluation: Table = Table(
    "event_evaluation",
    metadata,
    reference("event_id", "inbound_event.id"),
    reference("wait_id", "wait.id"),
    timestamp("evaluated_at"),
    *audit_columns(),
    PrimaryKeyConstraint("event_id", "wait_id"),
    Index("ix_event_evaluation_wait_id", "wait_id"),
)

notification_subscription: Table = Table(
    "notification_subscription",
    metadata,
    primary_identifier(),
    reference("namespace_id", "namespace.id"),
    Column("revision", Integer, nullable=False),
    document("kinds"),
    document("destination"),
    flag("enabled"),
    Column("state", name(), nullable=False),
    Column("last_error", name(), nullable=True),
    *audit_columns(),
)

notification_delivery: Table = Table(
    "notification_delivery",
    metadata,
    primary_identifier(),
    reference("subscription_id", "notification_subscription.id"),
    reference("event_id", "event.id"),
    Column("state", name(), nullable=False),
    Column("attempts", Integer, nullable=False),
    timestamp("next_attempt_at", nullable=True),
    Column("claim_token", name(), nullable=True),
    timestamp("claim_until", nullable=True),
    Column("subscription_revision_used", Integer, nullable=False),
    Column("last_error", name(), nullable=True),
    *audit_columns(),
    UniqueConstraint("subscription_id", "event_id"),
    Index("ix_notification_delivery_state_next", "state", "next_attempt_at"),
)
