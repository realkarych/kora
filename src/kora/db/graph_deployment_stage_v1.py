from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
    UniqueConstraint,
)
from sqlalchemy.schema import Constraint, conv

from kora.db.graph_preparation_v1 import TABLE_NAME as PREPARATION_TABLE
from kora.db.graph_preparation_v1 import ULID_ALPHABET, ascii_predicate, exact_text
from kora.db.storage_address_fence_v1 import (
    ADDRESS_TEXT,
    POSTGRESQL_ADDRESS_CHECKS,
    SQLITE_ADDRESS_CHECKS,
)
from kora.db.types import POSTGRESQL, SQLITE

STAGE_TABLE: str = "graph_deployment_stage"
OCCURRENCE_TABLE: str = "graph_deployment_stage_occurrence"
SCOPE_COLUMNS: tuple[str, ...] = ("namespace_id", "preparation_id", "stage_id")
MAX_STAGE_OCCURRENCES: int = 64
MAX_STAGE_FILE_BYTES: int = 262144
SchemaElement = Column[str] | Column[int] | Constraint | Index


def _check(table: str, name: str, predicate: str) -> CheckConstraint:
    return CheckConstraint(predicate, name=conv(f"ck_{table}_{name}"))


def _backend_checks(
    table: str,
    dialect: str | None,
    sqlite_checks: tuple[tuple[str, str], ...],
    postgres_checks: tuple[tuple[str, str], ...] = (),
) -> tuple[CheckConstraint, ...]:
    if dialect not in (None, SQLITE, POSTGRESQL):
        raise RuntimeError("unsupported graph deployment stage schema backend")
    return tuple(
        _check(table, name, predicate).ddl_if(dialect=backend)
        for backend, extra_checks in (
            (SQLITE, sqlite_checks),
            (POSTGRESQL, postgres_checks),
        )
        if dialect in (None, backend)
        for name, predicate in (
            *(
                (
                    f"{column}_domain",
                    ascii_predicate(backend, column, ULID_ALPHABET, "= 26", "0-7"),
                )
                for column in SCOPE_COLUMNS
            ),
            *extra_checks,
        )
    )


def stage_checks(dialect: str | None) -> tuple[CheckConstraint, ...]:
    return (
        _check(STAGE_TABLE, "incarnation_positive", "fence_incarnation > 0"),
        _check(
            STAGE_TABLE,
            "size_range",
            f"total_size BETWEEN 0 AND {MAX_STAGE_FILE_BYTES}",
        ),
        *_backend_checks(
            STAGE_TABLE,
            dialect,
            (
                *SQLITE_ADDRESS_CHECKS,
                (
                    "integer_storage",
                    "typeof(fence_incarnation) = 'integer' "
                    "AND typeof(total_size) = 'integer'",
                ),
            ),
            POSTGRESQL_ADDRESS_CHECKS,
        ),
    )


def occurrence_checks(dialect: str | None) -> tuple[CheckConstraint, ...]:
    return (
        _check(
            OCCURRENCE_TABLE,
            "ordinal_range",
            f"ordinal BETWEEN 0 AND {MAX_STAGE_OCCURRENCES - 1}",
        ),
        *_backend_checks(
            OCCURRENCE_TABLE,
            dialect,
            (("integer_storage", "typeof(ordinal) = 'integer'"),),
        ),
    )


def graph_deployment_stage_elements_v1(
    dialect: str | None = None,
) -> tuple[SchemaElement, ...]:
    return (
        Column("stage_id", exact_text(), nullable=False),
        Column("namespace_id", exact_text(), nullable=False),
        Column("preparation_id", exact_text(), nullable=False),
        Column("store_id", ADDRESS_TEXT, nullable=False),
        Column("payload_digest", ADDRESS_TEXT, nullable=False),
        Column("fence_incarnation", BigInteger(), nullable=False),
        Column("total_size", Integer(), nullable=False),
        PrimaryKeyConstraint("stage_id", name=f"pk_{STAGE_TABLE}"),
        UniqueConstraint(
            "store_id",
            "payload_digest",
            "preparation_id",
            name=f"uq_{STAGE_TABLE}_address_preparation",
        ),
        Index(f"ix_{STAGE_TABLE}_preparation", "preparation_id", "stage_id"),
        ForeignKeyConstraint(
            ("preparation_id",),
            (f"{PREPARATION_TABLE}.id",),
            name=f"fk_{STAGE_TABLE}_preparation",
        ),
        *stage_checks(dialect),
    )


def graph_deployment_stage_occurrence_elements_v1(
    dialect: str | None = None,
) -> tuple[SchemaElement, ...]:
    return (
        Column("namespace_id", exact_text(), nullable=False),
        Column("preparation_id", exact_text(), nullable=False),
        Column("ordinal", Integer(), nullable=False),
        Column("stage_id", exact_text(), nullable=False),
        PrimaryKeyConstraint(
            "preparation_id", "ordinal", name=f"pk_{OCCURRENCE_TABLE}"
        ),
        Index(
            f"ix_{OCCURRENCE_TABLE}_stage",
            "stage_id",
            "preparation_id",
            "ordinal",
        ),
        ForeignKeyConstraint(
            ("stage_id",),
            (f"{STAGE_TABLE}.stage_id",),
            name=f"fk_{OCCURRENCE_TABLE}_stage",
        ),
        *occurrence_checks(dialect),
    )
