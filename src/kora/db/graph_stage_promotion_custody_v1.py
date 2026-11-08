from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    ForeignKeyConstraint,
    LargeBinary,
    PrimaryKeyConstraint,
    UniqueConstraint,
)
from sqlalchemy.schema import Constraint, conv

from kora.db.graph_deployment_stage_v1 import STAGE_TABLE
from kora.db.graph_preparation_v1 import ULID_ALPHABET, ascii_predicate, exact_text
from kora.db.storage_address_fence_v1 import (
    ADDRESS_TEXT,
    POSTGRESQL_ADDRESS_CHECKS,
    SQLITE_ADDRESS_CHECKS,
)
from kora.db.types import POSTGRESQL, SQLITE

INTENT_TABLE: str = "graph_deployment_stage_promotion_intent"
SETTLEMENT_TABLE: str = "graph_deployment_stage_promotion_settlement"
MAX_PROMOTION_DOCUMENT_BYTES_V1: int = 16_384
SchemaElement = Column[str] | Column[int] | Column[bytes] | Constraint
NativeChecks = tuple[tuple[str, str], ...]


def _check(relation: str, name: str, predicate: str) -> CheckConstraint:
    return CheckConstraint(predicate, name=conv(f"ck_promotion_{relation}_{name}"))


def _native_checks(
    dialect: str | None,
    relation: str,
    document: str,
    sqlite_checks: NativeChecks = (),
    postgres_checks: NativeChecks = (),
) -> tuple[CheckConstraint, ...]:
    if dialect not in (None, SQLITE, POSTGRESQL):
        raise RuntimeError("unsupported graph promotion custody schema backend")
    return tuple(
        _check(relation, name, predicate).ddl_if(dialect=backend)
        for backend, length, extra_checks in (
            (
                SQLITE,
                "length",
                ((*sqlite_checks, ("document_type", f"typeof({document}) = 'blob'"))),
            ),
            (POSTGRESQL, "octet_length", postgres_checks),
        )
        if dialect in (None, backend)
        for name, predicate in (
            (
                "stage_id_domain",
                ascii_predicate(backend, "stage_id", ULID_ALPHABET, "= 26", "0-7"),
            ),
            (
                "document_width",
                f"{length}({document}) BETWEEN 1 AND {MAX_PROMOTION_DOCUMENT_BYTES_V1}",
            ),
            *extra_checks,
        )
    )


def _intent_domains(dialect: str) -> NativeChecks:
    address_checks = (
        SQLITE_ADDRESS_CHECKS if dialect == SQLITE else POSTGRESQL_ADDRESS_CHECKS
    )
    payload_digest_check = next(
        predicate
        for name, predicate in address_checks
        if name == "payload_digest_domain"
    )
    return (
        *address_checks,
        (
            "operation_id_domain",
            ascii_predicate(dialect, "operation_id", "!-~", "BETWEEN 1 AND 256"),
        ),
        (
            "request_digest_domain",
            payload_digest_check.replace("payload_digest", "request_digest"),
        ),
    )


def graph_stage_promotion_intent_elements_v1(
    dialect: str | None = None,
) -> tuple[SchemaElement, ...]:
    return (
        Column("stage_id", exact_text(), nullable=False),
        Column("store_id", ADDRESS_TEXT, nullable=False),
        Column("payload_digest", ADDRESS_TEXT, nullable=False),
        Column("fence_incarnation", BigInteger(), nullable=False),
        Column("operation_id", exact_text(), nullable=False),
        Column("request_digest", ADDRESS_TEXT, nullable=False),
        Column("content_generation", BigInteger(), nullable=False),
        Column("request_bytes", LargeBinary(), nullable=False),
        PrimaryKeyConstraint("stage_id", name=f"pk_{INTENT_TABLE}"),
        ForeignKeyConstraint(
            ("stage_id",),
            (f"{STAGE_TABLE}.stage_id",),
            name="fk_promotion_intent_stage",
        ),
        UniqueConstraint(
            "store_id", "operation_id", name="uq_promotion_intent_store_operation"
        ),
        _check("intent", "incarnation_positive", "fence_incarnation > 0"),
        _check("intent", "generation_nonnegative", "content_generation >= 0"),
        *_native_checks(
            dialect,
            "intent",
            "request_bytes",
            (
                *_intent_domains(SQLITE),
                (
                    "integer_storage",
                    "typeof(fence_incarnation) = 'integer' "
                    "AND typeof(content_generation) = 'integer'",
                ),
            ),
            _intent_domains(POSTGRESQL),
        ),
    )


def graph_stage_promotion_settlement_elements_v1(
    dialect: str | None = None,
) -> tuple[SchemaElement, ...]:
    return (
        Column("stage_id", exact_text(), nullable=False),
        Column("observation_bytes", LargeBinary(), nullable=False),
        PrimaryKeyConstraint("stage_id", name=f"pk_{SETTLEMENT_TABLE}"),
        ForeignKeyConstraint(
            ("stage_id",),
            (f"{INTENT_TABLE}.stage_id",),
            name="fk_promotion_settlement_intent",
        ),
        *_native_checks(dialect, "settlement", "observation_bytes"),
    )
