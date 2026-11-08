from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKeyConstraint,
    PrimaryKeyConstraint,
    Text,
)
from sqlalchemy.schema import Constraint, conv

from kora.db.types import POSTGRESQL, SQLITE

TABLE_NAME: str = "graph_deployment_preparation"
ULID_ALPHABET: str = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
ULID_COLUMNS: tuple[tuple[str, bool], ...] = (
    ("id", False),
    ("namespace_id", False),
    ("initial_graph_id", True),
    ("result_graph_id", True),
    ("result_graph_version_id", True),
)
RESULT_SHAPE: str = (
    "(state IN ('published', 'reused') AND result_graph_id IS NOT NULL "
    "AND result_graph_version_id IS NOT NULL) OR "
    "(state IN ('open', 'closed_without_publication') AND result_graph_id IS NULL "
    "AND result_graph_version_id IS NULL)"
)
RESULT_TARGET: str = (
    "initial_graph_id IS NULL OR result_graph_id IS NULL "
    "OR initial_graph_id = result_graph_id"
)


def exact_text() -> Text:
    return Text(collation="BINARY").with_variant(Text(collation="C"), POSTGRESQL)


def named_check(name: str, predicate: str) -> CheckConstraint:
    return CheckConstraint(predicate, name=conv(f"ck_{TABLE_NAME}_{name}"))


def ascii_predicate(
    dialect: str,
    column: str,
    alphabet: str,
    width: str,
    leading: str | None = None,
    *,
    nullable: bool = False,
) -> str:
    if dialect == SQLITE:
        predicates = [
            f"typeof({column}) = 'text'",
            f"instr({column}, char(0)) = 0",
            f"length({column}) {width}",
            f"{column} NOT GLOB '*[^{alphabet}]*'",
        ]
        if leading is not None:
            predicates.append(f"substr({column}, 1, 1) GLOB '[{leading}]'")
    else:
        predicates = [
            f"octet_length({column}) {width}",
            f"{column} !~ '[^{alphabet}]'",
        ]
        if leading is not None:
            predicates.append(f"substr({column}, 1, 1) ~ '[{leading}]'")
    predicate = " AND ".join(predicates)
    if nullable:
        predicate = f"{column} IS NULL OR ({predicate})"
    return predicate


def ascii_check(
    dialect: str,
    column: str,
    alphabet: str,
    width: str,
    leading: str | None = None,
    *,
    nullable: bool = False,
) -> CheckConstraint:
    return named_check(
        f"{column}_domain",
        ascii_predicate(dialect, column, alphabet, width, leading, nullable=nullable),
    )


def domain_checks(dialect: str) -> tuple[CheckConstraint, ...]:
    return (
        *(
            ascii_check(
                dialect,
                column,
                ULID_ALPHABET,
                "= 26",
                "0-7",
                nullable=nullable,
            )
            for column, nullable in ULID_COLUMNS
        ),
        ascii_check(dialect, "graph_name", "a-z0-9_", "BETWEEN 1 AND 64", "a-z"),
        ascii_check(dialect, "deployment_digest", "0-9a-f", "= 64"),
    )


def result_checks() -> tuple[CheckConstraint, CheckConstraint]:
    return (
        named_check("result_shape", RESULT_SHAPE),
        named_check("result_target", RESULT_TARGET),
    )


def graph_preparation_elements_v1(
    dialect: str | None = None,
) -> tuple[Column[str] | Constraint, ...]:
    if dialect is None:
        checks = tuple(
            check.ddl_if(dialect=backend)
            for backend in (SQLITE, POSTGRESQL)
            for check in domain_checks(backend)
        )
    elif dialect in (SQLITE, POSTGRESQL):
        checks = domain_checks(dialect)
    else:
        raise RuntimeError("unsupported graph preparation schema backend")

    return (
        Column("id", exact_text(), nullable=False),
        Column("namespace_id", exact_text(), nullable=False),
        Column("graph_name", exact_text(), nullable=False),
        Column("initial_graph_id", exact_text(), nullable=True),
        Column("deployment_digest", exact_text(), nullable=False),
        Column("state", exact_text(), nullable=False),
        Column("result_graph_id", exact_text(), nullable=True),
        Column("result_graph_version_id", exact_text(), nullable=True),
        PrimaryKeyConstraint("id", name=f"pk_{TABLE_NAME}"),
        ForeignKeyConstraint(
            ("namespace_id",),
            ("namespace.id",),
            name=f"fk_{TABLE_NAME}_namespace_id_namespace",
        ),
        *result_checks(),
        *checks,
    )
