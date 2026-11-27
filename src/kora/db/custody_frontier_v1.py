from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Integer,
    LargeBinary,
    MetaData,
    Table,
)
from sqlalchemy.schema import conv

from kora.db.types import POSTGRESQL, SQLITE

TABLE_NAME: str = "custody_frontier"
ZERO_ID_HEX: str = "00000000000000000000000000000000"
COUNTERS: tuple[str, ...] = (
    "generation",
    "commit_sequence",
    "previous_generation",
    "previous_commit",
)
IDENTIFIERS: tuple[str, ...] = ("metadata_id", "journal_id", "token", "previous_token")
Element = Column[int] | Column[bytes] | CheckConstraint


def check(name: str, expression: str) -> CheckConstraint:
    return CheckConstraint(expression, name=conv(f"ck_{TABLE_NAME}_{name}"))


def frontier_identity_check(dialect: str) -> CheckConstraint:
    def hex_value(name: str) -> str:
        return f"hex({name})" if dialect == SQLITE else f"encode({name}, 'hex')"

    required = " AND ".join(
        f"{hex_value(name)} != '{ZERO_ID_HEX}'" for name in IDENTIFIERS[:-1]
    )
    previous = hex_value("previous_token")
    return check(
        "identities",
        f"{required} AND ((previous_generation = 0 AND previous_commit = 0 "
        f"AND {previous} = '{ZERO_ID_HEX}') OR "
        f"(previous_generation > 0 AND previous_commit > 0 "
        f"AND {previous} != '{ZERO_ID_HEX}'))",
    ).ddl_if(dialect=dialect)


def custody_frontier_elements_v1(dialect: str | None = None) -> tuple[Element, ...]:
    if dialect not in (SQLITE, POSTGRESQL, None):
        raise RuntimeError("unsupported custody frontier backend")
    common: tuple[Element, ...] = (
        Column("singleton", Integer, primary_key=True, autoincrement=False),
        Column("format_version", Integer, nullable=False),
        *(Column(name, BigInteger, nullable=False) for name in COUNTERS),
        *(Column(name, LargeBinary(16), nullable=False) for name in IDENTIFIERS),
        check("singleton_version", "singleton = 1 AND format_version = 1"),
        check(
            "ordered_frontiers",
            "generation > 0 AND commit_sequence > 0 "
            "AND previous_generation <= generation "
            "AND previous_commit < commit_sequence AND previous_token != token",
        ),
        check(
            "identifier_widths",
            " AND ".join(f"length({name}) = 16" for name in IDENTIFIERS),
        ),
    )
    if dialect == POSTGRESQL:
        return (*common, frontier_identity_check(POSTGRESQL))
    sqlite_storage = check(
        "sqlite_storage",
        " AND ".join(
            (
                *(
                    f"typeof({name}) = 'integer'"
                    for name in ("singleton", "format_version", *COUNTERS)
                ),
                *(f"typeof({name}) = 'blob'" for name in IDENTIFIERS),
            )
        ),
    ).ddl_if(dialect=SQLITE)
    sqlite = (*common, sqlite_storage, frontier_identity_check(SQLITE))
    if dialect == SQLITE:
        return sqlite
    return (*sqlite, frontier_identity_check(POSTGRESQL))


custody_frontier = Table(TABLE_NAME, MetaData(), *custody_frontier_elements_v1(SQLITE))
