import sqlalchemy as sa
from alembic import op

import kora.db.types

revision: str = "0001"
down_revision: str | None = None
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "api_token",
        sa.Column("id", kora.db.types.CollatedText(26), nullable=False),
        sa.Column("kind", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("digest", kora.db.types.CollatedText(64), nullable=False),
        sa.Column("owner", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("namespaces", kora.db.types.JsonDocument(), nullable=False),
        sa.Column("approve", sa.Boolean(), nullable=False),
        sa.Column("platform", sa.Boolean(), nullable=False),
        sa.Column("runtime", kora.db.types.CollatedText(255), nullable=True),
        sa.Column("expires_at", kora.db.types.UtcTimestamp(), nullable=True),
        sa.Column("created_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.Column("updated_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_token")),
        sa.UniqueConstraint("digest", name=op.f("uq_api_token_digest")),
    )
    op.create_table(
        "automation_principal",
        sa.Column("binding", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("binding_revision", sa.Integer(), nullable=False),
        sa.Column("provider_instance", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("stable_id", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("kind", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("display_name", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("first_used_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.Column("created_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.Column("updated_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.PrimaryKeyConstraint(
            "binding",
            "binding_revision",
            "provider_instance",
            "stable_id",
            name=op.f("pk_automation_principal"),
        ),
    )
    op.create_table(
        "namespace",
        sa.Column("id", kora.db.types.CollatedText(26), nullable=False),
        sa.Column("name", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("policy_revision", sa.Integer(), nullable=False),
        sa.Column("policy", kora.db.types.JsonDocument(), nullable=False),
        sa.Column("created_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.Column("updated_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_namespace")),
        sa.UniqueConstraint("name", name=op.f("uq_namespace_name")),
    )
    op.create_table(
        "provider_pause",
        sa.Column("quota_resource", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("quota_group", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("until", kora.db.types.UtcTimestamp(), nullable=False),
        sa.Column("source_attempt_id", kora.db.types.CollatedText(26), nullable=True),
        sa.Column("reason", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("released_at", kora.db.types.UtcTimestamp(), nullable=True),
        sa.Column("created_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.Column("updated_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.PrimaryKeyConstraint(
            "quota_resource", "quota_group", name=op.f("pk_provider_pause")
        ),
    )
    op.create_table(
        "artifact_type",
        sa.Column("id", kora.db.types.CollatedText(26), nullable=False),
        sa.Column("namespace_id", kora.db.types.CollatedText(26), nullable=True),
        sa.Column("name", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("schema", kora.db.types.JsonDocument(), nullable=False),
        sa.Column("parent", kora.db.types.CollatedText(255), nullable=True),
        sa.Column("behavior", kora.db.types.JsonDocument(), nullable=False),
        sa.Column("created_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.Column("updated_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.ForeignKeyConstraint(
            ["namespace_id"],
            ["namespace.id"],
            name=op.f("fk_artifact_type_namespace_id_namespace"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_artifact_type")),
    )
    op.create_index(
        "ix_artifact_type_namespace_name",
        "artifact_type",
        [sa.text("coalesce(namespace_id, '')"), "name"],
        unique=True,
    )
    op.create_table(
        "graph",
        sa.Column("id", kora.db.types.CollatedText(26), nullable=False),
        sa.Column("namespace_id", kora.db.types.CollatedText(26), nullable=False),
        sa.Column("name", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("created_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.Column("updated_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.ForeignKeyConstraint(
            ["namespace_id"],
            ["namespace.id"],
            name=op.f("fk_graph_namespace_id_namespace"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_graph")),
        sa.UniqueConstraint(
            "namespace_id", "name", name=op.f("uq_graph_namespace_id_name")
        ),
    )
    op.create_table(
        "idempotency_record",
        sa.Column("token_id", kora.db.types.CollatedText(26), nullable=False),
        sa.Column("key", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("request_digest", kora.db.types.CollatedText(64), nullable=False),
        sa.Column("response", kora.db.types.JsonDocument(), nullable=False),
        sa.Column("expires_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.Column("created_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.Column("updated_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.ForeignKeyConstraint(
            ["token_id"],
            ["api_token.id"],
            name=op.f("fk_idempotency_record_token_id_api_token"),
        ),
        sa.PrimaryKeyConstraint("token_id", "key", name=op.f("pk_idempotency_record")),
    )
    op.create_table(
        "inbound_event",
        sa.Column("id", kora.db.types.CollatedText(26), nullable=False),
        sa.Column("namespace_id", kora.db.types.CollatedText(26), nullable=False),
        sa.Column("name", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("idempotency_key", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("payload", kora.db.types.JsonDocument(), nullable=False),
        sa.Column("auth_kind", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("nonce", kora.db.types.CollatedText(255), nullable=True),
        sa.Column("received_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.Column("start_outcome", kora.db.types.JsonDocument(), nullable=True),
        sa.Column("consumed_by", kora.db.types.JsonDocument(), nullable=False),
        sa.Column("created_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.Column("updated_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.ForeignKeyConstraint(
            ["namespace_id"],
            ["namespace.id"],
            name=op.f("fk_inbound_event_namespace_id_namespace"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inbound_event")),
        sa.UniqueConstraint(
            "namespace_id",
            "name",
            "idempotency_key",
            name=op.f("uq_inbound_event_namespace_id_name_idempotency_key"),
        ),
    )
    op.create_table(
        "notification_subscription",
        sa.Column("id", kora.db.types.CollatedText(26), nullable=False),
        sa.Column("namespace_id", kora.db.types.CollatedText(26), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("kinds", kora.db.types.JsonDocument(), nullable=False),
        sa.Column("destination", kora.db.types.JsonDocument(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("state", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("last_error", kora.db.types.CollatedText(255), nullable=True),
        sa.Column("created_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.Column("updated_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.ForeignKeyConstraint(
            ["namespace_id"],
            ["namespace.id"],
            name=op.f("fk_notification_subscription_namespace_id_namespace"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_subscription")),
    )
    op.create_table(
        "preset",
        sa.Column("id", kora.db.types.CollatedText(26), nullable=False),
        sa.Column("namespace_id", kora.db.types.CollatedText(26), nullable=False),
        sa.Column("name", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("version", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("body", kora.db.types.JsonDocument(), nullable=False),
        sa.Column("created_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.Column("updated_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.ForeignKeyConstraint(
            ["namespace_id"],
            ["namespace.id"],
            name=op.f("fk_preset_namespace_id_namespace"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_preset")),
        sa.UniqueConstraint(
            "namespace_id",
            "name",
            "version",
            name=op.f("uq_preset_namespace_id_name_version"),
        ),
    )
    op.create_table(
        "price_table",
        sa.Column("id", kora.db.types.CollatedText(26), nullable=False),
        sa.Column("namespace_id", kora.db.types.CollatedText(26), nullable=True),
        sa.Column("model", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("effective_from", kora.db.types.UtcTimestamp(), nullable=False),
        sa.Column("prices", kora.db.types.JsonDocument(), nullable=False),
        sa.Column("created_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.Column("updated_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.ForeignKeyConstraint(
            ["namespace_id"],
            ["namespace.id"],
            name=op.f("fk_price_table_namespace_id_namespace"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_price_table")),
    )
    op.create_table(
        "worker",
        sa.Column("id", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("incarnation_id", kora.db.types.CollatedText(26), nullable=False),
        sa.Column(
            "execution_domain_id", kora.db.types.CollatedText(26), nullable=False
        ),
        sa.Column("runtime", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("version", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("capacity", sa.Integer(), nullable=False),
        sa.Column("last_seen_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.Column("token_id", kora.db.types.CollatedText(26), nullable=False),
        sa.Column("created_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.Column("updated_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.ForeignKeyConstraint(
            ["token_id"], ["api_token.id"], name=op.f("fk_worker_token_id_api_token")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_worker")),
    )
    op.create_table(
        "graph_version",
        sa.Column("id", kora.db.types.CollatedText(26), nullable=False),
        sa.Column("graph_id", kora.db.types.CollatedText(26), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("ir", kora.db.types.JsonDocument(), nullable=False),
        sa.Column("ir_digest", kora.db.types.CollatedText(64), nullable=False),
        sa.Column("source_digest", kora.db.types.CollatedText(64), nullable=False),
        sa.Column("execution_contract", kora.db.types.JsonDocument(), nullable=False),
        sa.Column(
            "execution_contract_digest", kora.db.types.CollatedText(64), nullable=False
        ),
        sa.Column("deployment_digest", kora.db.types.CollatedText(64), nullable=False),
        sa.Column("resolved_digests", kora.db.types.JsonDocument(), nullable=False),
        sa.Column("resources", kora.db.types.JsonDocument(), nullable=False),
        sa.Column("deployed_by", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("deployed_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.Column("status", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("created_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.Column("updated_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.ForeignKeyConstraint(
            ["graph_id"], ["graph.id"], name=op.f("fk_graph_version_graph_id_graph")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_graph_version")),
        sa.UniqueConstraint(
            "graph_id", "number", name=op.f("uq_graph_version_graph_id_number")
        ),
    )
    op.create_table(
        "trigger_binding",
        sa.Column("id", kora.db.types.CollatedText(26), nullable=False),
        sa.Column("graph_id", kora.db.types.CollatedText(26), nullable=False),
        sa.Column("kind", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("spec", kora.db.types.JsonDocument(), nullable=False),
        sa.Column("next_fire_at", kora.db.types.UtcTimestamp(), nullable=True),
        sa.Column("last_fired_key", kora.db.types.CollatedText(255), nullable=True),
        sa.Column("binding_revision", sa.Integer(), nullable=False),
        sa.Column("observation", kora.db.types.JsonDocument(), nullable=True),
        sa.Column("evaluation_error", kora.db.types.JsonDocument(), nullable=True),
        sa.Column("created_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.Column("updated_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.ForeignKeyConstraint(
            ["graph_id"], ["graph.id"], name=op.f("fk_trigger_binding_graph_id_graph")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_trigger_binding")),
    )
    op.create_index(
        "ix_trigger_binding_next_fire_at",
        "trigger_binding",
        ["next_fire_at"],
        unique=False,
    )
    op.create_table(
        "run",
        sa.Column("id", kora.db.types.CollatedText(26), nullable=False),
        sa.Column("namespace_id", kora.db.types.CollatedText(26), nullable=False),
        sa.Column("graph_id", kora.db.types.CollatedText(26), nullable=False),
        sa.Column("graph_version_id", kora.db.types.CollatedText(26), nullable=False),
        sa.Column("rerun_of_run_id", kora.db.types.CollatedText(26), nullable=True),
        sa.Column("idempotency_key", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("target_key", kora.db.types.CollatedText(255), nullable=True),
        sa.Column("target_held_since", kora.db.types.UtcTimestamp(), nullable=True),
        sa.Column("params", kora.db.types.JsonDocument(), nullable=False),
        sa.Column(
            "cel_params_projection", kora.db.types.JsonDocument(), nullable=False
        ),
        sa.Column("allow_unenforced_secrets", sa.Boolean(), nullable=False),
        sa.Column("status", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("needs_scheduling", sa.Boolean(), nullable=False),
        sa.Column("trigger", kora.db.types.JsonDocument(), nullable=False),
        sa.Column("limits", kora.db.types.JsonDocument(), nullable=False),
        sa.Column("retention", kora.db.types.JsonDocument(), nullable=False),
        sa.Column("started_at", kora.db.types.UtcTimestamp(), nullable=True),
        sa.Column("deadline_expired_at", kora.db.types.UtcTimestamp(), nullable=True),
        sa.Column("finished_at", kora.db.types.UtcTimestamp(), nullable=True),
        sa.Column("created_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.Column("updated_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.ForeignKeyConstraint(
            ["graph_id"], ["graph.id"], name=op.f("fk_run_graph_id_graph")
        ),
        sa.ForeignKeyConstraint(
            ["graph_version_id"],
            ["graph_version.id"],
            name=op.f("fk_run_graph_version_id_graph_version"),
        ),
        sa.ForeignKeyConstraint(
            ["namespace_id"],
            ["namespace.id"],
            name=op.f("fk_run_namespace_id_namespace"),
        ),
        sa.ForeignKeyConstraint(
            ["rerun_of_run_id"], ["run.id"], name=op.f("fk_run_rerun_of_run_id_run")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run")),
        sa.UniqueConstraint(
            "graph_id", "idempotency_key", name=op.f("uq_run_graph_id_idempotency_key")
        ),
    )
    op.create_index(
        "ix_run_namespace_target", "run", ["namespace_id", "target_key"], unique=False
    )
    op.create_index(
        "ix_run_needs_scheduling",
        "run",
        ["id"],
        unique=False,
        postgresql_where=sa.text("needs_scheduling"),
        sqlite_where=sa.text("needs_scheduling = 1"),
    )
    op.create_table(
        "node_instance",
        sa.Column("id", kora.db.types.CollatedText(26), nullable=False),
        sa.Column("run_id", kora.db.types.CollatedText(26), nullable=False),
        sa.Column("node_path", kora.db.types.CollatedText(2048), nullable=False),
        sa.Column("kind", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("status", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("trigger_rule", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("subgraph_attempt", sa.Integer(), nullable=False),
        sa.Column("top_level", sa.Boolean(), nullable=False),
        sa.Column("consumed_versions", kora.db.types.JsonDocument(), nullable=False),
        sa.Column("current_attempt_id", kora.db.types.CollatedText(26), nullable=True),
        sa.Column("branch_selection", kora.db.types.JsonDocument(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("started_at", kora.db.types.UtcTimestamp(), nullable=True),
        sa.Column("deadline_expired_at", kora.db.types.UtcTimestamp(), nullable=True),
        sa.Column(
            "provider_wait_deadline", kora.db.types.UtcTimestamp(), nullable=True
        ),
        sa.Column("created_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.Column("updated_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["run.id"], name=op.f("fk_node_instance_run_id_run")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_node_instance")),
        sa.UniqueConstraint(
            "run_id", "node_path", name=op.f("uq_node_instance_run_id_node_path")
        ),
    )
    op.create_index(
        "ix_node_instance_run_top_level",
        "node_instance",
        ["run_id", "top_level"],
        unique=False,
    )
    op.create_table(
        "attempt",
        sa.Column("id", kora.db.types.CollatedText(26), nullable=False),
        sa.Column("node_instance_id", kora.db.types.CollatedText(26), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("runtime", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("worker_id", kora.db.types.CollatedText(255), nullable=True),
        sa.Column("execution_domain_id", kora.db.types.CollatedText(26), nullable=True),
        sa.Column("execution_state", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("recovery_version", sa.Integer(), nullable=False),
        sa.Column("recovery_required", sa.Boolean(), nullable=False),
        sa.Column("recovery", kora.db.types.JsonDocument(), nullable=True),
        sa.Column("handle", kora.db.types.JsonDocument(), nullable=True),
        sa.Column("status", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("desired_state", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("record_version", sa.Integer(), nullable=False),
        sa.Column("input_bindings", kora.db.types.JsonDocument(), nullable=False),
        sa.Column("execution_request", kora.db.types.JsonDocument(), nullable=True),
        sa.Column("execution_policy", kora.db.types.JsonDocument(), nullable=True),
        sa.Column("error_class", kora.db.types.CollatedText(255), nullable=True),
        sa.Column("error", kora.db.types.JsonDocument(), nullable=True),
        sa.Column("strategy", kora.db.types.CollatedText(255), nullable=True),
        sa.Column("strategy_fallback", kora.db.types.CollatedText(255), nullable=True),
        sa.Column("infrastructure_retries", sa.Integer(), nullable=False),
        sa.Column("lease_owner", kora.db.types.CollatedText(255), nullable=True),
        sa.Column("lease_epoch", sa.Integer(), nullable=False),
        sa.Column("lease_until", kora.db.types.UtcTimestamp(), nullable=True),
        sa.Column("last_heartbeat_at", kora.db.types.UtcTimestamp(), nullable=True),
        sa.Column("counters", kora.db.types.JsonDocument(), nullable=False),
        sa.Column("scratch", kora.db.types.JsonDocument(), nullable=False),
        sa.Column(
            "resolved_config_digest", kora.db.types.CollatedText(64), nullable=True
        ),
        sa.Column("price_table_id", kora.db.types.CollatedText(26), nullable=True),
        sa.Column("image_digest", kora.db.types.CollatedText(255), nullable=True),
        sa.Column("harness_version", kora.db.types.CollatedText(255), nullable=True),
        sa.Column("started_at", kora.db.types.UtcTimestamp(), nullable=True),
        sa.Column("deadline", kora.db.types.UtcTimestamp(), nullable=True),
        sa.Column("deadline_expired_at", kora.db.types.UtcTimestamp(), nullable=True),
        sa.Column("finished_at", kora.db.types.UtcTimestamp(), nullable=True),
        sa.Column("created_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.Column("updated_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.ForeignKeyConstraint(
            ["node_instance_id"],
            ["node_instance.id"],
            name=op.f("fk_attempt_node_instance_id_node_instance"),
        ),
        sa.ForeignKeyConstraint(
            ["worker_id"], ["worker.id"], name=op.f("fk_attempt_worker_id_worker")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_attempt")),
        sa.UniqueConstraint(
            "node_instance_id",
            "number",
            name=op.f("uq_attempt_node_instance_id_number"),
        ),
    )
    op.create_index(
        "ix_attempt_lease_until",
        "attempt",
        ["lease_until"],
        unique=False,
        postgresql_where=sa.text(
            "status IN ('claimed', 'submitted', 'running', 'finishing')"
        ),
        sqlite_where=sa.text(
            "status IN ('claimed', 'submitted', 'running', 'finishing')"
        ),
    )
    op.create_table(
        "artifact_version",
        sa.Column("id", kora.db.types.CollatedText(26), nullable=False),
        sa.Column("run_id", kora.db.types.CollatedText(26), nullable=False),
        sa.Column("node_instance_id", kora.db.types.CollatedText(26), nullable=False),
        sa.Column("attempt_id", kora.db.types.CollatedText(26), nullable=True),
        sa.Column("output_name", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("type_name", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("contract_digest", kora.db.types.CollatedText(64), nullable=False),
        sa.Column("validation_result", kora.db.types.JsonDocument(), nullable=False),
        sa.Column("digest_selection", kora.db.types.JsonDocument(), nullable=False),
        sa.Column("derived_payload", kora.db.types.JsonDocument(), nullable=True),
        sa.Column("cel_projection", kora.db.types.JsonDocument(), nullable=True),
        sa.Column("cel_projection_set", kora.db.types.CollatedText(64), nullable=True),
        sa.Column("digest", kora.db.types.CollatedText(64), nullable=False),
        sa.Column("storage_ref", kora.db.types.CollatedText(2048), nullable=True),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("inline", kora.db.types.JsonDocument(), nullable=True),
        sa.Column("payload_digest", kora.db.types.CollatedText(64), nullable=False),
        sa.Column("status", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("payload_state", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("worker_id", kora.db.types.CollatedText(255), nullable=True),
        sa.Column("image_digest", kora.db.types.CollatedText(255), nullable=True),
        sa.Column("created_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.Column("updated_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["attempt.id"],
            name=op.f("fk_artifact_version_attempt_id_attempt"),
        ),
        sa.ForeignKeyConstraint(
            ["node_instance_id"],
            ["node_instance.id"],
            name=op.f("fk_artifact_version_node_instance_id_node_instance"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["run.id"], name=op.f("fk_artifact_version_run_id_run")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_artifact_version")),
    )
    op.create_index(
        "ix_artifact_version_node_output",
        "artifact_version",
        ["node_instance_id", "output_name"],
        unique=False,
    )
    op.create_table(
        "attempt_dispatcher",
        sa.Column("id", kora.db.types.CollatedText(26), nullable=False),
        sa.Column("attempt_id", kora.db.types.CollatedText(26), nullable=False),
        sa.Column(
            "execution_domain_id", kora.db.types.CollatedText(26), nullable=False
        ),
        sa.Column("worker_id", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("incarnation_id", kora.db.types.CollatedText(26), nullable=False),
        sa.Column("lease_epoch", sa.Integer(), nullable=False),
        sa.Column("mode", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("granted_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.Column("closed_at", kora.db.types.UtcTimestamp(), nullable=True),
        sa.Column("evidence", kora.db.types.JsonDocument(), nullable=True),
        sa.Column("created_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.Column("updated_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["attempt.id"],
            name=op.f("fk_attempt_dispatcher_attempt_id_attempt"),
        ),
        sa.ForeignKeyConstraint(
            ["worker_id"],
            ["worker.id"],
            name=op.f("fk_attempt_dispatcher_worker_id_worker"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_attempt_dispatcher")),
        sa.UniqueConstraint(
            "attempt_id",
            "lease_epoch",
            name=op.f("uq_attempt_dispatcher_attempt_id_lease_epoch"),
        ),
    )
    op.create_table(
        "event",
        sa.Column("id", kora.db.types.CollatedText(26), nullable=False),
        sa.Column("namespace_id", kora.db.types.CollatedText(26), nullable=False),
        sa.Column("run_id", kora.db.types.CollatedText(26), nullable=True),
        sa.Column("node_instance_id", kora.db.types.CollatedText(26), nullable=True),
        sa.Column("attempt_id", kora.db.types.CollatedText(26), nullable=True),
        sa.Column("kind", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("actor", kora.db.types.JsonDocument(), nullable=False),
        sa.Column("payload", kora.db.types.JsonDocument(), nullable=False),
        sa.Column("at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.Column("created_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.Column("updated_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.ForeignKeyConstraint(
            ["attempt_id"], ["attempt.id"], name=op.f("fk_event_attempt_id_attempt")
        ),
        sa.ForeignKeyConstraint(
            ["namespace_id"],
            ["namespace.id"],
            name=op.f("fk_event_namespace_id_namespace"),
        ),
        sa.ForeignKeyConstraint(
            ["node_instance_id"],
            ["node_instance.id"],
            name=op.f("fk_event_node_instance_id_node_instance"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["run.id"], name=op.f("fk_event_run_id_run")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event")),
    )
    op.create_index("ix_event_run_id_id", "event", ["run_id", "id"], unique=False)
    op.create_table(
        "session_segment",
        sa.Column("attempt_id", kora.db.types.CollatedText(26), nullable=False),
        sa.Column("offset", sa.Integer(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("storage_ref", kora.db.types.CollatedText(2048), nullable=False),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.Column("payload_state", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("uploaded_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.Column("created_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.Column("updated_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["attempt.id"],
            name=op.f("fk_session_segment_attempt_id_attempt"),
        ),
        sa.PrimaryKeyConstraint(
            "attempt_id", "offset", name=op.f("pk_session_segment")
        ),
    )
    op.create_table(
        "usage_record",
        sa.Column("id", kora.db.types.CollatedText(26), nullable=False),
        sa.Column("attempt_id", kora.db.types.CollatedText(26), nullable=False),
        sa.Column("model", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("cached_input_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_creation_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("reasoning_tokens", sa.Integer(), nullable=False),
        sa.Column("completeness", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("basis", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("billing_class", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("usd_estimate", kora.db.types.UsdAmount(), nullable=False),
        sa.Column("delta_of_session_totals", sa.Boolean(), nullable=False),
        sa.Column("source", kora.db.types.JsonDocument(), nullable=False),
        sa.Column("worker_id", kora.db.types.CollatedText(255), nullable=True),
        sa.Column("image_digest", kora.db.types.CollatedText(255), nullable=True),
        sa.Column("created_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.Column("updated_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["attempt.id"],
            name=op.f("fk_usage_record_attempt_id_attempt"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_usage_record")),
        sa.UniqueConstraint(
            "attempt_id", "model", name=op.f("uq_usage_record_attempt_id_model")
        ),
    )
    op.create_table(
        "wait",
        sa.Column("id", kora.db.types.CollatedText(26), nullable=False),
        sa.Column("node_instance_id", kora.db.types.CollatedText(26), nullable=False),
        sa.Column("attempt_id", kora.db.types.CollatedText(26), nullable=True),
        sa.Column("kind", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("wake_at", kora.db.types.UtcTimestamp(), nullable=True),
        sa.Column("subscription", kora.db.types.JsonDocument(), nullable=True),
        sa.Column("reason", kora.db.types.JsonDocument(), nullable=True),
        sa.Column("status", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("observation", kora.db.types.JsonDocument(), nullable=True),
        sa.Column("evaluation_error", kora.db.types.JsonDocument(), nullable=True),
        sa.Column("created_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.Column("updated_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.ForeignKeyConstraint(
            ["attempt_id"], ["attempt.id"], name=op.f("fk_wait_attempt_id_attempt")
        ),
        sa.ForeignKeyConstraint(
            ["node_instance_id"],
            ["node_instance.id"],
            name=op.f("fk_wait_node_instance_id_node_instance"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_wait")),
    )
    op.create_index(
        "ix_wait_status_wake_at", "wait", ["status", "wake_at"], unique=False
    )
    op.create_table(
        "approval_request",
        sa.Column("id", kora.db.types.CollatedText(26), nullable=False),
        sa.Column("node_instance_id", kora.db.types.CollatedText(26), nullable=False),
        sa.Column("wait_id", kora.db.types.CollatedText(26), nullable=False),
        sa.Column("channel", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("channel_request_id", kora.db.types.CollatedText(255), nullable=True),
        sa.Column("delivery", kora.db.types.JsonDocument(), nullable=True),
        sa.Column("form", kora.db.types.JsonDocument(), nullable=False),
        sa.Column(
            "context_artifact_version_id", kora.db.types.CollatedText(26), nullable=True
        ),
        sa.Column("bound_commit_sha", kora.db.types.CollatedText(255), nullable=True),
        sa.Column("bound_digests", kora.db.types.JsonDocument(), nullable=True),
        sa.Column("observed_head_sha", kora.db.types.CollatedText(255), nullable=True),
        sa.Column("run_cost", kora.db.types.JsonDocument(), nullable=False),
        sa.Column("status", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("response", kora.db.types.JsonDocument(), nullable=True),
        sa.Column("approver", kora.db.types.JsonDocument(), nullable=True),
        sa.Column("evaluation_error", kora.db.types.JsonDocument(), nullable=True),
        sa.Column("created_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.Column("updated_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.ForeignKeyConstraint(
            ["node_instance_id"],
            ["node_instance.id"],
            name=op.f("fk_approval_request_node_instance_id_node_instance"),
        ),
        sa.ForeignKeyConstraint(
            ["wait_id"], ["wait.id"], name=op.f("fk_approval_request_wait_id_wait")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approval_request")),
    )
    op.create_table(
        "artifact_lineage",
        sa.Column(
            "artifact_version_id", kora.db.types.CollatedText(26), nullable=False
        ),
        sa.Column("input_name", kora.db.types.CollatedText(255), nullable=False),
        sa.Column(
            "consumed_artifact_version_id",
            kora.db.types.CollatedText(26),
            nullable=False,
        ),
        sa.Column("relation", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("created_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.Column("updated_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.ForeignKeyConstraint(
            ["artifact_version_id"],
            ["artifact_version.id"],
            name=op.f("fk_artifact_lineage_artifact_version_id_artifact_version"),
        ),
        sa.ForeignKeyConstraint(
            ["consumed_artifact_version_id"],
            ["artifact_version.id"],
            name=op.f(
                "fk_artifact_lineage_consumed_artifact_version_id_artifact_version"
            ),
        ),
        sa.PrimaryKeyConstraint(
            "artifact_version_id",
            "input_name",
            "consumed_artifact_version_id",
            name=op.f("pk_artifact_lineage"),
        ),
    )
    op.create_index(
        "ix_artifact_lineage_consumed",
        "artifact_lineage",
        ["consumed_artifact_version_id"],
        unique=False,
    )
    op.create_table(
        "event_evaluation",
        sa.Column("event_id", kora.db.types.CollatedText(26), nullable=False),
        sa.Column("wait_id", kora.db.types.CollatedText(26), nullable=False),
        sa.Column("evaluated_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.Column("created_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.Column("updated_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["inbound_event.id"],
            name=op.f("fk_event_evaluation_event_id_inbound_event"),
        ),
        sa.ForeignKeyConstraint(
            ["wait_id"], ["wait.id"], name=op.f("fk_event_evaluation_wait_id_wait")
        ),
        sa.PrimaryKeyConstraint(
            "event_id", "wait_id", name=op.f("pk_event_evaluation")
        ),
    )
    op.create_index(
        "ix_event_evaluation_wait_id", "event_evaluation", ["wait_id"], unique=False
    )
    op.create_table(
        "notification_delivery",
        sa.Column("id", kora.db.types.CollatedText(26), nullable=False),
        sa.Column("subscription_id", kora.db.types.CollatedText(26), nullable=False),
        sa.Column("event_id", kora.db.types.CollatedText(26), nullable=False),
        sa.Column("state", kora.db.types.CollatedText(255), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", kora.db.types.UtcTimestamp(), nullable=True),
        sa.Column("claim_token", kora.db.types.CollatedText(255), nullable=True),
        sa.Column("claim_until", kora.db.types.UtcTimestamp(), nullable=True),
        sa.Column("subscription_revision_used", sa.Integer(), nullable=False),
        sa.Column("last_error", kora.db.types.CollatedText(255), nullable=True),
        sa.Column("created_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.Column("updated_at", kora.db.types.UtcTimestamp(), nullable=False),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["event.id"],
            name=op.f("fk_notification_delivery_event_id_event"),
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["notification_subscription.id"],
            name=op.f(
                "fk_notification_delivery_subscription_id_notification_subscription"
            ),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_delivery")),
        sa.UniqueConstraint(
            "subscription_id",
            "event_id",
            name=op.f("uq_notification_delivery_subscription_id_event_id"),
        ),
    )
    op.create_index(
        "ix_notification_delivery_state_next",
        "notification_delivery",
        ["state", "next_attempt_at"],
        unique=False,
    )
