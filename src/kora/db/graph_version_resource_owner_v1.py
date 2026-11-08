from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
)
from sqlalchemy.schema import Constraint, conv

from kora.db.graph_preparation_v1 import ULID_ALPHABET, ascii_predicate, exact_text
from kora.db.storage_address_fence_v1 import (
    ADDRESS_TEXT,
    POSTGRESQL_ADDRESS_CHECKS,
    SQLITE_ADDRESS_CHECKS,
)
from kora.db.types import POSTGRESQL, SQLITE, CollatedText

TABLE_NAME: str = "graph_version_resource_owner"
REVERSE_INDEX: str = "ix_graph_version_resource_owner_address"
SchemaElement = Column[str] | Column[int] | Constraint | Index


def _check(name: str, predicate: str) -> CheckConstraint:
    return CheckConstraint(predicate, name=conv(f"ck_{TABLE_NAME}_{name}"))


def _backend_checks(dialect: str | None) -> tuple[CheckConstraint, ...]:
    if dialect not in (None, SQLITE, POSTGRESQL):
        raise RuntimeError("unsupported graph version resource owner schema backend")
    return tuple(
        _check(name, predicate).ddl_if(dialect=backend)
        for backend, native_checks in (
            (
                SQLITE,
                (
                    *SQLITE_ADDRESS_CHECKS,
                    (
                        "integer_storage",
                        "typeof(ordinal) = 'integer' AND "
                        "typeof(total_size) = 'integer'",
                    ),
                    (
                        "file_name_storage",
                        "typeof(file_name) = 'text' AND instr(file_name, char(0)) = 0",
                    ),
                ),
            ),
            (POSTGRESQL, POSTGRESQL_ADDRESS_CHECKS),
        )
        if dialect in (None, backend)
        for name, predicate in (
            *native_checks,
            (
                "version_domain",
                ascii_predicate(
                    backend, "graph_version_id", ULID_ALPHABET, "= 26", "0-7"
                ),
            ),
            (
                "node_domain",
                ascii_predicate(
                    backend, "node_name", "a-z0-9_", "BETWEEN 1 AND 64", "a-z"
                ),
            ),
            (
                "subgraph_domain",
                ascii_predicate(
                    backend,
                    "subgraph_name",
                    "a-z0-9_",
                    "BETWEEN 1 AND 64",
                    "a-z",
                    nullable=True,
                ),
            ),
            (
                "incarnation_domain",
                ascii_predicate(
                    backend, "object_incarnation", "!-~", "BETWEEN 1 AND 256"
                ),
            ),
        )
    )


def graph_version_resource_owner_elements_v1(
    dialect: str | None = None,
) -> tuple[SchemaElement, ...]:
    return (
        Column("graph_version_id", CollatedText(26), nullable=False),
        Column("ordinal", Integer(), nullable=False),
        Column("subgraph_name", exact_text()),
        Column("node_name", exact_text(), nullable=False),
        Column("file_name", exact_text(), nullable=False),
        Column("store_id", ADDRESS_TEXT, nullable=False),
        Column("payload_digest", ADDRESS_TEXT, nullable=False),
        Column("object_incarnation", ADDRESS_TEXT, nullable=False),
        Column("total_size", Integer(), nullable=False),
        Column("payload_state", exact_text(), nullable=False),
        PrimaryKeyConstraint("graph_version_id", "ordinal", name=f"pk_{TABLE_NAME}"),
        ForeignKeyConstraint(
            ("graph_version_id",),
            ("graph_version.id",),
            name=f"fk_{TABLE_NAME}_version",
        ),
        Index(
            REVERSE_INDEX,
            "store_id",
            "payload_digest",
            "payload_state",
            "graph_version_id",
            "ordinal",
        ),
        _check("ordinal_range", "ordinal BETWEEN 0 AND 63"),
        _check("size_range", "total_size BETWEEN 0 AND 262144"),
        _check("file_name_nonempty", "file_name <> ''"),
        _check(
            "payload_state",
            "payload_state IN ('present', 'expiring', 'expired', 'missing')",
        ),
        *_backend_checks(dialect),
    )
