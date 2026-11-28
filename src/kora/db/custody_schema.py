from dataclasses import dataclass, replace
from typing import Final

from kora.db.custody_models import MAX_CUSTODY_COUNTER, MAX_CUSTODY_ROW_BYTES


@dataclass(frozen=True)
class CustodyColumn:
    name: str
    kind: str
    domain: str
    nullable: bool = False

    @property
    def predicate(self) -> str:
        present = f"typeof({self.name}) = '{self.kind}' AND ({self.domain})"
        return f"{self.name} IS NULL OR ({present})" if self.nullable else present

    @property
    def declaration(self) -> str:
        affinity = {"integer": "INTEGER", "blob": "BLOB", "text": "TEXT"}[self.kind]
        required = "" if self.nullable else " NOT NULL"
        return f"{self.name} {affinity}{required} CHECK({self.predicate})"

    @property
    def width(self) -> str:
        return (
            "10" if self.kind == "integer" else f"2 + coalesce(length({self.name}), 0)"
        )


def integer(
    name: str,
    *,
    minimum: int = 1,
    maximum: int = MAX_CUSTODY_COUNTER,
    null: bool = False,
) -> CustodyColumn:
    return CustodyColumn(
        name, "integer", f"{name} BETWEEN {minimum} AND {maximum}", null
    )


def binary(
    name: str, width: int, *, nonzero: bool = False, null: bool = False
) -> CustodyColumn:
    domain = f"length({name}) = {width}"
    if nonzero:
        domain += f" AND {name} != zeroblob({width})"
    return CustodyColumn(name, "blob", domain, null)


def ascii_text(
    name: str, maximum: int = 256, *, spaces: bool = False, null: bool = False
) -> CustodyColumn:
    allowed = " -~" if spaces else "!-~"
    return CustodyColumn(
        name,
        "text",
        f"length(CAST({name} AS BLOB)) BETWEEN 1 AND {maximum} "
        f"AND instr({name}, char(0)) = 0 AND {name} NOT GLOB '*[^{allowed}]*'",
        null,
    )


def enumeration(
    name: str, values: tuple[str, ...], *, null: bool = False
) -> CustodyColumn:
    choices = ", ".join(f"'{value}'" for value in values)
    return CustodyColumn(name, "text", f"{name} IN ({choices})", null)


def anchor(prefix: str, *, null: bool = False) -> tuple[CustodyColumn, ...]:
    path = f"{prefix}_path"
    return (
        binary(f"{prefix}_device", 8, null=null),
        binary(f"{prefix}_inode", 8, null=null),
        CustodyColumn(
            path,
            "blob",
            f"length({path}) BETWEEN 1 AND 4096 AND instr({path}, X'00') = 0",
            null,
        ),
    )


def process(prefix: str = "") -> tuple[CustodyColumn, ...]:
    return (
        *(
            ascii_text(f"{prefix}{name}")
            for name in ("host_id", "boot_id", "pid_namespace")
        ),
        integer(f"{prefix}pid", maximum=2**31 - 1),
        integer(f"{prefix}start_time", minimum=0),
    )


def frontier(
    prefix: str, *, initial: bool = False, null: bool = False
) -> tuple[CustodyColumn, ...]:
    sequence = f"{prefix}commit" if prefix else "commit_sequence"
    return (
        integer(f"{prefix}generation", minimum=0 if initial else 1, null=null),
        integer(sequence, minimum=0 if initial else 1, null=null),
        binary(f"{prefix}token", 16, nonzero=not initial, null=null),
    )


def initial_frontier_check(prefix: str) -> str:
    return (
        f"({prefix}generation = 0 AND {prefix}commit = 0 "
        f"AND {prefix}token = zeroblob(16)) OR "
        f"({prefix}generation > 0 AND {prefix}commit > 0 "
        f"AND {prefix}token != zeroblob(16))"
    )


def metadata_binding() -> tuple[CustodyColumn, ...]:
    return (
        binary("metadata_id", 16, nonzero=True),
        enumeration("metadata_backend", ("sqlite", "postgresql")),
        binary("qualification_digest", 32),
        *anchor("metadata_sqlite", null=True),
        binary("system_identifier", 8, null=True),
        integer("database_oid", maximum=2**32 - 1, null=True),
        binary("connection_authority_digest", 32, null=True),
    )


METADATA_SHAPE: Final[str] = (
    "(metadata_backend = 'sqlite' AND metadata_sqlite_device IS NOT NULL "
    "AND metadata_sqlite_inode IS NOT NULL AND metadata_sqlite_path IS NOT NULL "
    "AND system_identifier IS NULL AND database_oid IS NULL "
    "AND connection_authority_digest IS NULL) OR "
    "(metadata_backend = 'postgresql' AND metadata_sqlite_device IS NULL "
    "AND metadata_sqlite_inode IS NULL AND metadata_sqlite_path IS NULL "
    "AND system_identifier IS NOT NULL AND database_oid IS NOT NULL "
    "AND connection_authority_digest IS NOT NULL)"
)


def transaction_binding(*, null: bool = False) -> tuple[CustodyColumn, ...]:
    return (
        *(
            integer(name, null=null)
            for name in ("connection_sequence", "transaction_sequence")
        ),
        binary("settings_digest", 32, null=null),
        binary("native_xid", 8, null=True),
        integer("backend_pid", maximum=2**31 - 1, null=True),
        integer("backend_start_us", minimum=-(2**63), null=True),
        integer("server_start_us", minimum=-(2**63), null=True),
    )


def receiver() -> tuple[CustodyColumn, ...]:
    return (
        enumeration("gate", ("open", "closing", "closed")),
        integer("allocated_receiver", minimum=0),
        integer("allocated_invocation", minimum=0),
        integer("receiver_sequence", null=True),
        enumeration("receiver_state", ("active", "closing"), null=True),
    )


RECEIVER_SHAPE: Final[str] = (
    "(receiver_sequence IS NULL AND receiver_state IS NULL) OR "
    "(receiver_sequence IS NOT NULL AND receiver_state IS NOT NULL "
    "AND receiver_sequence <= allocated_receiver AND gate != 'closed')"
)


@dataclass(frozen=True)
class CustodyRelation:
    name: str
    columns: tuple[CustodyColumn, ...]
    constraints: tuple[str, ...] = ()
    key: str = "singleton"

    @property
    def width(self) -> str:
        return " + ".join(column.width for column in self.columns)

    @property
    def create(self) -> str:
        declarations = [column.declaration for column in self.columns]
        declarations.append(f"PRIMARY KEY ({self.key})")
        declarations.extend(f"CHECK({condition})" for condition in self.constraints)
        declarations.append(f"CHECK({self.width} <= {MAX_CUSTODY_ROW_BYTES})")
        return f"CREATE TABLE {self.name} ({', '.join(declarations)})"


VERSION: Final[CustodyColumn] = integer("format_version", maximum=1)
SINGLETON: Final[CustodyColumn] = integer("singleton", maximum=1)
AUTHORITY: Final[CustodyRelation] = CustodyRelation(
    "authority",
    (
        SINGLETON,
        integer("format_version", minimum=2, maximum=2),
        binary("journal_id", 16, nonzero=True),
        ascii_text("host_id"),
        *(
            field
            for name in (
                "parent",
                "root",
                "journal",
                "lifetime",
                "commit",
                "recovery",
                "journal_order",
            )
            for field in anchor(name)
        ),
        CustodyColumn(
            "provider_identity", "blob", "length(provider_identity) BETWEEN 1 AND 16384"
        ),
        *anchor("provider_root"),
        *metadata_binding(),
        enumeration("mode", ("provisioning", "active", "fenced", "restoring")),
        integer("generation"),
        integer("allocated_commit", minimum=0),
        integer("allocated_issuer", minimum=0),
        *frontier("confirmed_", initial=True),
        integer("recovery_priority_incarnation", null=True),
    ),
    (
        METADATA_SHAPE,
        initial_frontier_check("confirmed_"),
        "confirmed_generation <= generation AND confirmed_commit <= allocated_commit",
        "confirmed_generation != 0 OR mode = 'provisioning'",
    ),
)
PENDING: Final[CustodyRelation] = CustodyRelation(
    "pending_commit",
    (
        SINGLETON,
        VERSION,
        binary("journal_id", 16, nonzero=True),
        *frontier(""),
        *frontier("previous_", initial=True),
        integer("issuer_sequence"),
        enumeration("issuer_kind", ("ordinary", "recovery", "provisioning")),
        binary("metadata_id", 16, nonzero=True),
        binary("system_identifier", 8, null=True),
        integer("database_oid", maximum=2**32 - 1, null=True),
        *transaction_binding(),
        enumeration("phase", ("prepared", "dispatching", "committed", "aborted")),
    ),
    (
        initial_frontier_check("previous_"),
        "previous_generation <= generation AND previous_commit < commit_sequence",
        "previous_token != token",
        "(system_identifier IS NULL AND database_oid IS NULL AND native_xid IS NULL "
        "AND backend_pid IS NULL AND backend_start_us IS NULL "
        "AND server_start_us IS NULL) "
        "OR (system_identifier IS NOT NULL AND database_oid IS NOT NULL "
        "AND native_xid IS NOT NULL AND backend_pid IS NOT NULL "
        "AND backend_start_us IS NOT NULL AND server_start_us IS NOT NULL)",
    ),
)
PARTICIPANT: Final[CustodyRelation] = CustodyRelation(
    "participant",
    (
        integer("slot", minimum=0, maximum=63),
        VERSION,
        binary("journal_id", 16, nonzero=True),
        integer("generation"),
        integer("issuer_sequence"),
        *process(),
        *receiver(),
        integer("closure_revision", null=True),
    ),
    (
        RECEIVER_SHAPE,
        "(gate = 'closed' AND closure_revision IS NOT NULL "
        "AND receiver_sequence IS NULL) "
        "OR (gate != 'closed' AND closure_revision IS NULL)",
    ),
    key="slot",
)
INVOCATION_COLUMNS: Final[tuple[CustodyColumn, ...]] = (
    enumeration("owner_kind", ("upload_staging", "retention_delete_intent"), null=True),
    ascii_text("owner_upload_ref", 26, null=True),
    integer("owner_fence_incarnation", null=True),
    enumeration(
        "semantic_slot",
        (
            "begin",
            *(f"part_{index}" for index in range(16)),
            "prefix",
            "seal",
            "prepare",
            "promote",
            "retire",
            "release",
            "delete",
        ),
        null=True,
    ),
    ascii_text("operation_store_id", null=True),
    ascii_text("operation_id", null=True),
    enumeration(
        "operation_kind",
        (
            "begin_stage",
            "write_part",
            "capture_prefix",
            "seal_stage",
            "prepare_content",
            "promote",
            "retire_stage",
            "delete_content",
        ),
        null=True,
    ),
    ascii_text("operation_request_digest", 71, null=True),
    integer("invocation_issuer", null=True),
    integer("invocation_receiver", null=True),
    integer("invocation_sequence", null=True),
    enumeration("invocation_phase", ("registered", "entered", "closing"), null=True),
)
RECOVERY: Final[CustodyRelation] = CustodyRelation(
    "recovery",
    (
        SINGLETON,
        VERSION,
        integer("incarnation"),
        *process(),
        *receiver(),
        *INVOCATION_COLUMNS,
    ),
    (
        RECEIVER_SHAPE,
        "(owner_kind IS NULL AND "
        + " AND ".join(f"{column.name} IS NULL" for column in INVOCATION_COLUMNS[1:])
        + ") OR (owner_kind IS NOT NULL AND "
        + " AND ".join(
            f"{column.name} IS NOT NULL" for column in INVOCATION_COLUMNS[3:]
        )
        + " AND ((owner_kind = 'upload_staging' AND owner_upload_ref IS NOT NULL "
        "AND owner_fence_incarnation IS NULL) OR "
        "(owner_kind = 'retention_delete_intent' AND owner_upload_ref IS NULL "
        "AND owner_fence_incarnation IS NOT NULL)))",
    ),
)
IMAGE: Final[CustodyRelation] = CustodyRelation(
    "image_transition",
    (
        SINGLETON,
        VERSION,
        integer("transition_sequence"),
        integer("recovery_incarnation"),
        *metadata_binding(),
        *anchor("source"),
        *anchor("destination"),
        *frontier("expected_"),
        *frontier("pending_", null=True),
        enumeration("phase", ("copying", "complete", "installed")),
        binary("manifest_digest", 32, null=True),
    ),
    (
        METADATA_SHAPE,
        "(pending_generation IS NULL AND pending_commit IS NULL "
        "AND pending_token IS NULL) "
        "OR (pending_generation IS NOT NULL AND pending_commit IS NOT NULL "
        "AND pending_token IS NOT NULL)",
        "(phase = 'copying' AND manifest_digest IS NULL) "
        "OR (phase != 'copying' AND manifest_digest IS NOT NULL)",
    ),
)
PROVISIONING: Final[CustodyRelation] = CustodyRelation(
    "provisioning",
    (
        SINGLETON,
        VERSION,
        integer("recovery_incarnation"),
        *process(),
        *metadata_binding(),
        ascii_text("alembic_revision", 128, spaces=True),
        enumeration("phase", ("schema_running", "schema_complete", "frontier_running")),
        integer("migration_connection_sequence", null=True),
        integer("backend_pid", maximum=2**31 - 1, null=True),
        integer("backend_start_us", minimum=-(2**63), null=True),
        integer("server_start_us", minimum=-(2**63), null=True),
    ),
    (
        METADATA_SHAPE,
        "(migration_connection_sequence IS NULL AND backend_pid IS NULL "
        "AND backend_start_us IS NULL AND server_start_us IS NULL) OR "
        "(migration_connection_sequence IS NOT NULL AND "
        "((metadata_backend = 'sqlite' AND backend_pid IS NULL "
        "AND backend_start_us IS NULL AND server_start_us IS NULL) OR "
        "(metadata_backend = 'postgresql' AND backend_pid IS NOT NULL "
        "AND backend_start_us IS NOT NULL AND server_start_us IS NOT NULL)))",
    ),
)
RELATIONS: Final[tuple[CustodyRelation, ...]] = (
    AUTHORITY,
    PENDING,
    PARTICIPANT,
    RECOVERY,
    IMAGE,
    PROVISIONING,
)


ISOLATED_AUTHORITY_VERSION: Final = 1073741826
ISOLATED_FRONTIER_VERSION: Final = 1073741825
ISOLATED_FRONTIER_REVISION: Final = "isolated_sqlite_frontier_v1"


def isolated_relation(relation: CustodyRelation) -> CustodyRelation:
    version = ISOLATED_AUTHORITY_VERSION if relation is AUTHORITY else 1
    columns = {
        "format_version": integer("format_version", minimum=version, maximum=version),
        "qualification_digest": CustodyColumn(
            "qualification_digest", "blob", "0", True
        ),
    }
    return replace(
        relation,
        columns=tuple(columns.get(column.name, column) for column in relation.columns),
        constraints=(*relation.constraints, "metadata_backend = 'sqlite'"),
    )


ISOLATED_AUTHORITY: Final = isolated_relation(AUTHORITY)
ISOLATED_PROVISIONING: Final = isolated_relation(PROVISIONING)
ISOLATED_RELATIONS: Final = (
    *RELATIONS[1:-1],
    ISOLATED_AUTHORITY,
    ISOLATED_PROVISIONING,
)
