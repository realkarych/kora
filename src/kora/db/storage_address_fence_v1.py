from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Identity,
    Integer,
    PrimaryKeyConstraint,
    UniqueConstraint,
)
from sqlalchemy.schema import Constraint, conv

from kora.db.types import POSTGRESQL, SQLITE, collated_text

TABLE_NAME: str = "storage_address_fence"
STORE_ID_WIDTH: int = 256
PAYLOAD_DIGEST_WIDTH: int = 71
SQLITE_ADDRESS_CHECKS: tuple[tuple[str, str], ...] = (
    (
        "store_id_domain",
        "typeof(store_id) = 'text' AND instr(store_id, char(0)) = 0 "
        "AND length(store_id) BETWEEN 1 AND 256 AND store_id NOT GLOB '*[^!-~]*'",
    ),
    (
        "payload_digest_domain",
        "typeof(payload_digest) = 'text' AND instr(payload_digest, char(0)) = 0 "
        "AND length(payload_digest) = 71 AND substr(payload_digest, 1, 7) = 'sha256:' "
        "AND substr(payload_digest, 8) NOT GLOB '*[^0-9a-f]*'",
    ),
    (
        "generation_storage",
        "typeof(last_deletion_generation) = 'integer' AND "
        "(pending_deletion_generation IS NULL OR "
        "typeof(pending_deletion_generation) = 'integer')",
    ),
)
POSTGRESQL_ADDRESS_CHECKS: tuple[tuple[str, str], ...] = (
    (
        "store_id_domain",
        "octet_length(store_id) BETWEEN 1 AND 256 AND store_id !~ '[^!-~]'",
    ),
    (
        "payload_digest_domain",
        "octet_length(payload_digest) = 71 "
        "AND substr(payload_digest, 1, 7) = 'sha256:' "
        "AND substr(payload_digest, 8) !~ '[^0-9a-f]'",
    ),
)


def _check(name: str, predicate: str) -> CheckConstraint:
    return CheckConstraint(predicate, name=conv(f"ck_{TABLE_NAME}_{name}"))


def _backend_checks(dialect: str | None) -> tuple[CheckConstraint, ...]:
    if dialect not in (None, SQLITE, POSTGRESQL):
        raise RuntimeError("unsupported storage address fence schema backend")
    return tuple(
        _check(name, predicate).ddl_if(dialect=backend)
        for backend, checks in (
            (SQLITE, SQLITE_ADDRESS_CHECKS),
            (POSTGRESQL, POSTGRESQL_ADDRESS_CHECKS),
        )
        if dialect in (None, backend)
        for name, predicate in checks
    )


def storage_address_fence_elements_v1(
    dialect: str | None = None,
) -> tuple[Column[str] | Column[int] | Constraint, ...]:
    return (
        Column(
            "fence_incarnation",
            BigInteger().with_variant(Integer(), SQLITE),
            Identity(always=True, start=1, minvalue=1, cycle=False),
            nullable=False,
        ),
        Column("store_id", collated_text(STORE_ID_WIDTH), nullable=False),
        Column("payload_digest", collated_text(PAYLOAD_DIGEST_WIDTH), nullable=False),
        Column("last_deletion_generation", BigInteger(), nullable=False),
        Column("pending_deletion_generation", BigInteger(), nullable=True),
        PrimaryKeyConstraint("fence_incarnation", name=f"pk_{TABLE_NAME}"),
        UniqueConstraint(
            "store_id",
            "payload_digest",
            name=f"uq_{TABLE_NAME}_store_id_payload_digest",
        ),
        _check("incarnation_positive", "fence_incarnation > 0"),
        _check("last_generation_nonnegative", "last_deletion_generation >= 0"),
        _check(
            "pending_generation_current",
            "pending_deletion_generation IS NULL OR "
            "(pending_deletion_generation > 0 AND "
            "pending_deletion_generation = last_deletion_generation)",
        ),
        *_backend_checks(dialect),
    )
