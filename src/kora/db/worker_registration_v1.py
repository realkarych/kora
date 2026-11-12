from datetime import datetime

from sqlalchemy import Column, ForeignKey, PrimaryKeyConstraint, UniqueConstraint

from kora.db.types import JsonDocument, JsonObject, UtcTimestamp, collated_text

INCARNATION_TABLE: str = "worker_incarnation"
DOMAIN_TABLE: str = "worker_execution_domain"
EVENT_NAMESPACE_SCOPE: str = (
    "namespace_id IS NOT NULL OR (kind = 'worker.registered' "
    "AND run_id IS NULL AND node_instance_id IS NULL AND attempt_id IS NULL)"
)
Element = (
    Column[str]
    | Column[datetime]
    | Column[JsonObject]
    | PrimaryKeyConstraint
    | UniqueConstraint
)


def incarnation_elements() -> tuple[Element, ...]:
    return (
        Column(
            "worker_id", collated_text(255), ForeignKey("worker.id"), nullable=False
        ),
        Column("incarnation_id", collated_text(256), nullable=False),
        Column("execution_domain_id", collated_text(256), nullable=False),
        Column("process", JsonDocument(), nullable=False),
        Column("process_key", collated_text(64), nullable=False),
        Column("request_digest", collated_text(64), nullable=False),
        Column("registered_at", UtcTimestamp(), nullable=False),
        PrimaryKeyConstraint("worker_id", "incarnation_id"),
        UniqueConstraint("process_key"),
    )


def domain_elements() -> tuple[Element, ...]:
    return (
        Column("execution_domain_id", collated_text(256), primary_key=True),
        Column("runtime", collated_text(255), nullable=False),
        Column("worker_id", collated_text(255), nullable=True),
    )
