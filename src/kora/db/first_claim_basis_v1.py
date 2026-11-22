import sqlalchemy as sa
from sqlalchemy.schema import Constraint, conv

from kora.db.graph_preparation_v1 import ascii_predicate
from kora.db.identifiers import CROCKFORD_ALPHABET
from kora.db.types import BYTE_ORDER_COLLATION, POSTGRESQL, SQLITE

TABLE_NAME: str = "attempt_first_claim_basis"
MAX_CONFIGURED_STALL_US: int = 86_399_999_999_999_999_999
FIRST_CLAIM_BASIS_SQLITE_BYTES: int = 428
SELECTION_ROLES: tuple[str, ...] = ("runtime", "vcs", "harness")
SELECTION_IDS: tuple[str, ...] = tuple(
    f"{role}_selection_id" for role in SELECTION_ROLES
)
SELECTION_REVISIONS: tuple[str, ...] = tuple(
    f"{role}_selection_revision" for role in SELECTION_ROLES
)


def _text(width: int) -> sa.String:
    return sa.String(width, collation="BINARY").with_variant(
        sa.String(width, collation=BYTE_ORDER_COLLATION), POSTGRESQL
    )


def _ascii(dialect: str, field: str, alphabet: str, width: int, leading: str) -> str:
    postgres = dialect == POSTGRESQL
    cast = "::text" if postgres else ""
    parsed = field + cast
    if postgres:
        octets = f"octet_length({parsed})"
        width_check = (
            f"{octets} = 26" if width == 26 else f"{octets} >= 1 AND {octets} <= 20"
        )
        domain = (
            f"{width_check} AND {parsed} !~ '[^{alphabet}]'::text "
            f"AND substr({parsed}, 1, 1) ~ '[{leading}]'::text"
        )
    else:
        length = "= 26" if width == 26 else "BETWEEN 1 AND 20"
        domain = ascii_predicate(dialect, field, alphabet, length, leading)
    if width == 20:
        maximum = f"'{MAX_CONFIGURED_STALL_US}'{cast}"
        domain += f" AND (length({parsed}) < 20 OR {parsed} <= {maximum})"
    if postgres:
        return domain
    return (
        f"CASE WHEN typeof({field}) = 'text' "
        f"AND octet_length({field}) <= {2 * width} THEN {domain} ELSE false END"
    )


def first_claim_basis_domain(dialect: str) -> str:
    sqlite = dialect == SQLITE
    stall = "configured_stall_us"
    digest = "renderer_behavior_sha256"
    pairs = tuple(
        f"{key} IS NOT NULL AND {revision} IS NOT NULL"
        for key, revision in zip(SELECTION_IDS, SELECTION_REVISIONS, strict=True)
    )
    absent = " AND ".join(
        f"{field} IS NULL" for field in (digest, *SELECTION_IDS, *SELECTION_REVISIONS)
    )
    present = (
        f"{digest} IS NOT NULL AND {pairs[0]} AND {pairs[1]} AND "
        f"({pairs[2]} OR harness_selection_id IS NULL AND "
        "harness_selection_revision IS NULL)"
    )
    digest_valid = (
        f"typeof({digest}) = 'blob' AND " if sqlite else ""
    ) + f"octet_length({digest}) = 32"
    checks = (
        "format_version = 1",
        "source_revision >= 2",
        _ascii(dialect, "dispatcher_id", CROCKFORD_ALPHABET, 26, "0-7"),
        f"({stall} IS NULL OR {_ascii(dialect, stall, '0-9', 20, '1-9')})",
        f"({digest} IS NULL OR {digest_valid})",
        *(
            f"({field} IS NULL OR {predicate})"
            for field in SELECTION_IDS
            for predicate in (_ascii(dialect, field, CROCKFORD_ALPHABET, 26, "0-7"),)
        ),
        *(f"({field} IS NULL OR {field} > 0)" for field in SELECTION_REVISIONS),
        f"({absent} OR {present})",
    )
    if sqlite:
        checks += (
            "typeof(format_version) = 'integer'",
            "typeof(source_revision) = 'integer'",
            *(
                f"({field} IS NULL OR typeof({field}) = 'integer')"
                for field in SELECTION_REVISIONS
            ),
        )
    return " AND ".join(checks)


def first_claim_basis_elements(
    dialect: str | None = None,
) -> tuple[sa.Column[str] | sa.Column[int] | sa.Column[bytes] | Constraint, ...]:
    if dialect not in (None, SQLITE, POSTGRESQL):
        raise RuntimeError("unsupported first-Claim basis schema backend")
    return (
        sa.Column("dispatcher_id", _text(26), nullable=False),
        sa.Column("format_version", sa.SmallInteger(), nullable=False),
        sa.Column("source_revision", sa.BigInteger(), nullable=False),
        sa.Column("configured_stall_us", _text(20)),
        sa.Column("renderer_behavior_sha256", sa.LargeBinary()),
        *(
            field
            for key, revision in zip(SELECTION_IDS, SELECTION_REVISIONS, strict=True)
            for field in (
                sa.Column(key, _text(26)),
                sa.Column(revision, sa.BigInteger()),
            )
        ),
        sa.PrimaryKeyConstraint("dispatcher_id", name=f"pk_{TABLE_NAME}"),
        sa.ForeignKeyConstraint(
            ("dispatcher_id",),
            ("attempt_dispatcher.id",),
            name="fk_first_claim_dispatcher",
        ),
        *(
            sa.CheckConstraint(
                first_claim_basis_domain(backend),
                name=conv("ck_first_claim_basis_domain"),
            ).ddl_if(dialect=backend)
            for backend in (SQLITE, POSTGRESQL)
            if dialect in (None, backend)
        ),
    )
