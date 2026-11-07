from collections.abc import Sequence

from alembic import op
from sqlalchemy import BigInteger, CheckConstraint, Column, ForeignKey, Integer

from kora.db.types import CollatedText

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    sqlite = op.get_bind().dialect.name == "sqlite"
    constraints = [
        CheckConstraint(
            "format_version = 1",
            name=op.f("ck_graph_version_read_admission_format_version"),
        ),
        CheckConstraint(
            "db_input_bytes > 0",
            name=op.f("ck_graph_version_read_admission_db_input_bytes"),
        ),
        CheckConstraint(
            "resource_transport_bytes > 0",
            name=op.f("ck_graph_version_read_admission_resource_transport_bytes"),
        ),
    ]
    if sqlite:
        constraints.append(
            CheckConstraint(
                "typeof(format_version) = 'integer' "
                "AND typeof(db_input_bytes) = 'integer' "
                "AND typeof(resource_transport_bytes) = 'integer'",
                name=op.f("ck_graph_version_read_admission_integer_storage"),
            )
        )
    op.create_table(
        "graph_version_read_admission",
        Column(
            "graph_version_id",
            CollatedText(26),
            ForeignKey("graph_version.id"),
            primary_key=True,
        ),
        Column("format_version", Integer, nullable=False),
        Column("db_input_bytes", BigInteger, nullable=False),
        Column("resource_transport_bytes", BigInteger, nullable=False),
        *constraints,
    )
    if sqlite:
        op.create_index(
            "ix_graph_version_graph_id_id", "graph_version", ["graph_id", "id"]
        )


def downgrade() -> None:
    raise RuntimeError(
        "graph read admission history cannot be downgraded; restore a compatible backup"
    )
